#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Apix.chat 每日自动签到（调用 Apix Console 官方接口，无需浏览器）

接口实测：
  POST {base}/api/v1/auth/login    {"email","password"}  -> {"code":0,"data":{"access_token",...}}
  POST {base}/api/v1/auth/refresh  {"refresh_token"}     -> 同上（备用路径）
  GET  {base}/api/v1/user/checkin  Bearer <token>        -> {"data":{"today_checked","reward_amount",...}}
  POST {base}/api/v1/user/checkin  Bearer <token>        -> 200 成功 / 409 CHECKIN_ALREADY_CLAIMED 已领过

仅依赖 Python 标准库，可直接在 GitHub Actions ubuntu-latest 上运行。

环境变量（Secrets）：
  APIX_EMAIL        登录邮箱（必填，除非只提供 APIX_REFRESH_TOKEN）
  APIX_PASSWORD     登录密码（必填，同上）
  APIX_BASE_URL     站点地址，默认 https://apix.chat
  APIX_REFRESH_TOKEN 可选；无密码时用 refresh token 换 access token
  APIX_ATTEMPTS     每步重试次数，默认 4
  APIX_TIMEOUT      单请求超时秒数，默认 30
  APIX_NOTIFY_ON    通知策略：always / on_failure / on_success，默认 on_failure
  SERVERCHAN_KEY / PUSHPLUS_TOKEN   Server酱³（sctftest 同款 push 接口）
  TG_BOT_TOKEN + TG_CHAT_ID         Telegram Bot 通知
  NOTIFY_WEBHOOK                    通用 webhook，POST {"text": "..."}
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 控制台可能是 GBK（Windows）等编码，统一按 UTF-8 输出并容忍无法编码字符
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

BASE_URL = os.environ.get("APIX_BASE_URL", "https://apix.chat").rstrip("/")
API = f"{BASE_URL}/api/v1"
EMAIL = os.environ.get("APIX_EMAIL", "").strip()
PASSWORD = os.environ.get("APIX_PASSWORD", "").strip()
REFRESH_TOKEN = os.environ.get("APIX_REFRESH_TOKEN", "").strip()

USER_AGENT = os.environ.get(
    "APIX_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
ATTEMPTS = max(1, int(os.environ.get("APIX_ATTEMPTS", "4")))
TIMEOUT = int(os.environ.get("APIX_TIMEOUT", "30"))
BACKOFF_BASE = max(1, int(os.environ.get("APIX_BACKOFF", "5")))


# --------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC] {msg}", flush=True)


def http(method: str, url: str, payload=None, token: str | None = None,
         allow_statuses: tuple[int, ...] = ()) -> dict:
    """发送 JSON 请求。返回 {"status", "body", "raw"}；非预期状态码抛 RuntimeError。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if url.startswith(BASE_URL):
        headers["Origin"] = BASE_URL
        headers["Referer"] = f"{BASE_URL}/console-v2/checkin"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    elif method.upper() == "POST":
        data = b"{}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            raw = ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"网络异常 {type(exc).__name__}: {getattr(exc, 'reason', exc)}")

    try:
        body = json.loads(raw) if raw and raw.strip() else {}
    except json.JSONDecodeError:
        body = {"_raw": raw[:400]}

    if status >= 400 and status not in allow_statuses:
        raise RuntimeError(f"HTTP {status} {method.upper()} {url} -> {raw[:300]}")
    return {"status": status, "body": body, "raw": raw}


def data_of(resp: dict) -> dict:
    body = resp.get("body")
    if isinstance(body, dict):
        inner = body.get("data")
        if isinstance(inner, dict):
            return inner
        return body
    return {}


def message_of(resp: dict) -> str:
    body = resp.get("body")
    return str(body.get("message", "")) if isinstance(body, dict) else ""


def retry(step: str, fn):
    last: Exception | None = None
    for i in range(1, ATTEMPTS + 1):
        try:
            return fn()
        except RuntimeError as exc:
            last = exc
            # 明确的凭证错误不重试
            text = str(exc)
            if "HTTP 401" in text or "HTTP 403" in text:
                raise
            if i < ATTEMPTS:
                wait = BACKOFF_BASE * (2 ** (i - 1)) + random.uniform(0, 3)
                log(f"{step} 第 {i}/{ATTEMPTS} 次失败：{exc} -> {wait:.0f}s 后重试")
                time.sleep(wait)
    raise RuntimeError(f"{step} 连续 {ATTEMPTS} 次失败：{last}")


# --------------------------------------------------------------------------- auth
def obtain_token() -> str:
    def _try() -> str:
        if EMAIL and PASSWORD:
            resp = http("POST", f"{API}/auth/login",
                        {"email": EMAIL, "password": PASSWORD},
                        allow_statuses=(401, 403))
            token = data_of(resp).get("access_token")
            if token:
                user = data_of(resp).get("user") or {}
                log(f"登录成功：{user.get('email') or EMAIL}（user_id={user.get('id', '-')}）")
                return token
            raise RuntimeError(f"登录失败 HTTP {resp['status']}：{message_of(resp) or resp['raw'][:200]}")

        if REFRESH_TOKEN:
            resp = http("POST", f"{API}/auth/refresh", {"refresh_token": REFRESH_TOKEN},
                        allow_statuses=(401, 403))
            token = data_of(resp).get("access_token")
            if token:
                log("通过 refresh_token 获取 access_token 成功")
                return token
            raise RuntimeError(f"refresh 失败 HTTP {resp['status']}：{message_of(resp)}")

        raise RuntimeError("缺少凭据：请配置 APIX_EMAIL / APIX_PASSWORD")

    return retry("登录", _try)


# --------------------------------------------------------------------------- check-in
def checkin(token: str) -> dict:
    def _try() -> dict:
        status = data_of(http("GET", f"{API}/user/checkin", None, token))
        if not status:
            raise RuntimeError("读取签到状态失败")
        if status.get("enabled") is False:
            log("站点当前未开启签到功能")
            return {"ok": True, "already": True, "status": status, "disabled": True}
        if status.get("today_checked"):
            log(f"今日（{status.get('today_date')}）已签到，跳过")
            return {"ok": True, "already": True, "status": status}

        resp = http("POST", f"{API}/user/checkin", {}, token, allow_statuses=(409,))
        reason = (resp["body"] or {}).get("reason") if isinstance(resp["body"], dict) else None
        if resp["status"] == 409 or reason == "CHECKIN_ALREADY_CLAIMED":
            log("奖励已领取（409 CHECKIN_ALREADY_CLAIMED）")
            fresh = data_of(http("GET", f"{API}/user/checkin", None, token))
            return {"ok": True, "already": True, "status": fresh}
        if resp["status"] not in (200, 201):
            raise RuntimeError(f"签到失败 HTTP {resp['status']}：{message_of(resp) or resp['raw'][:200]}")

        log(f"签到成功：{message_of(resp) or 'success'}")
        fresh = data_of(http("GET", f"{API}/user/checkin", None, token))
        return {"ok": True, "already": False, "status": fresh or status}

    return retry("签到", _try)


# --------------------------------------------------------------------------- notify
def send_notify(title: str, content: str) -> None:
    key = os.environ.get("PUSHPLUS_TOKEN") or os.environ.get("SERVERCHAN_KEY")
    if key:
        url = f"https://push.plus/v1/{urllib.parse.quote(key)}"
        body = {"title": title, "content": content, "markdown": 1}
        try:
            http("POST", url, body, allow_statuses=(400, 401, 403))
            log("已推送 Server酱³")
        except RuntimeError as exc:
            log(f"Server酱³ 推送失败：{exc}")

    bot, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if bot and chat:
        url = f"https://api.telegram.org/bot{bot}/sendMessage"
        try:
            http("POST", url, {"chat_id": chat, "text": f"{title}\n{content}",
                               "disable_web_page_preview": True},
                 allow_statuses=(400, 401, 403))
            log("已推送 Telegram")
        except RuntimeError as exc:
            log(f"Telegram 推送失败：{exc}")

    hook = os.environ.get("NOTIFY_WEBHOOK")
    if hook:
        try:
            http("POST", hook, {"text": f"{title}\n{content}"}, allow_statuses=(400, 401, 403))
            log("已推送 Webhook")
        except RuntimeError as exc:
            log(f"Webhook 推送失败：{exc}")


def should_notify(ok: bool) -> bool:
    mode = (os.environ.get("APIX_NOTIFY_ON") or "on_failure").strip().lower()
    if mode == "always":
        return True
    if mode == "on_success":
        return ok
    return not ok


# --------------------------------------------------------------------------- report
def build_report(result: dict) -> str:
    data = result.get("status") or {}
    records = data.get("recent_records") or []
    head = "🎉 本次签到成功" if not result.get("already") else (
        "⏸️ 站点未开启签到" if result.get("disabled") else "✅ 今日此前已签到")
    lines = [
        f"- 结果：{head}",
        f"- 服务器日期：{data.get('today_date', '未知')}",
        f"- 奖励：{data.get('reward_type', '-')} {data.get('reward_amount', '-')}"
        + (f"（{data.get('trial_validity_hours')} 小时有效）"
           if data.get("trial_validity_hours") else ""),
        f"- 最近签到时间：{data.get('last_checkin_at', '-')}",
    ]
    if records:
        lines.append("- 最近记录：")
        for r in records[-5:]:
            lines.append(f"  - {r.get('checkin_date')}  {r.get('reward_type')} "
                         f"{r.get('amount')}  ({r.get('created_at')})")
    return "\n".join(lines)


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError as exc:
        log(f"写入 GITHUB_STEP_SUMMARY 失败：{exc}")


# --------------------------------------------------------------------------- main
def main() -> int:
    log(f"目标：{BASE_URL}  账号：{EMAIL or '(仅 refresh_token)'}")
    try:
        token = obtain_token()
        result = checkin(token)
        report = build_report(result)
        print("\n" + report + "\n", flush=True)
        write_summary(f"## Apix 每日签到\n\n{report}")
        if should_notify(ok=True):
            send_notify("Apix 签到成功" if not result.get("already") else "Apix 今日已签到",
                        report.replace("- ", "").replace("**", ""))
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"❌ 签到失败：{exc}")
        write_summary(f"## ❌ Apix 签到失败\n\n```\n{exc}\n```")
        if should_notify(ok=False):
            send_notify("Apix 签到失败", f"{EMAIL}\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
