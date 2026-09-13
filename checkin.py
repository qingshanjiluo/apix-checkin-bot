#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多站点 AI 中转站 每日签到 + 余额巡检（纯 HTTP，无需浏览器）

自动识别两套面板：
  ① Apix 面板（apix.chat 及其同类站）
       GET  {base}/api/v1/settings/public          -> checkin_enabled
       POST {base}/api/v1/auth/login               -> data.access_token
       GET  {base}/api/v1/user/checkin             -> 今日是否已签到 + 奖励
       POST {base}/api/v1/user/checkin             -> 领取（409 = 已领过）
       GET  {base}/api/v1/user/profile             -> 余额
  ② New API 面板（多数公益中转站）
       POST {base}/api/user/login                  -> data.access_token 或 session cookie + user id
       GET  {base}/api/user/self                   -> quota / used_quota
       GET  {base}/api/user/checkin                -> {enabled, min_quota, max_quota, stats}
       POST {base}/api/user/checkin                -> 领取

配置（三选一，优先级从高到低）：
  1) Secret  SITES_JSON  ——  JSON 数组：
     [{"name":"芯算AI","base":"https://xinsuanai.com","user":"邮箱或用户名","pass":"密码"},
      {"name":"Apix","base":"https://apix.chat","user":"...","pass":"...","flavor":"apix"}]
     可选字段：flavor(apix|newapi|auto，默认 auto)、quota_per_unit(默认 500000)、skip(true 跳过)
  2) 本地文件  --config accounts.json （同 1 的结构；也可用环境变量 SITES_FILE 指定路径）
  3) 单站兼容：APIX_BASE_URL / APIX_EMAIL / APIX_PASSWORD

其它环境变量：
  CHECKIN_DRY_RUN=1     只查询不签到
  CHECKIN_ATTEMPTS=4    每步重试次数
  CHECKIN_TIMEOUT=30    单请求超时（秒）
  CHECKIN_BACKOFF=5     退避基数
  CHECKIN_NOTIFY_ON=always|on_failure|on_success（默认 on_failure）
  PUSHPLUS_TOKEN / SERVERCHAN_KEY / TG_BOT_TOKEN+TG_CHAT_ID / NOTIFY_WEBHOOK   通知

退出码：0 全部正常；1 有站点失败；2 配置缺失。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

UA = os.environ.get(
    "CHECKIN_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
ATTEMPTS = max(1, int(os.environ.get("CHECKIN_ATTEMPTS", "4")))
TIMEOUT = int(os.environ.get("CHECKIN_TIMEOUT", "30"))
BACKOFF = max(1, int(os.environ.get("CHECKIN_BACKOFF", "5")))
DRY_RUN = os.environ.get("CHECKIN_DRY_RUN", "").lower() in ("1", "true", "yes")
USD_RATE = float(os.environ.get("CHECKIN_USD_RATE", "7.2"))   # 余额折算人民币展示用
LOW_USD = float(os.environ.get("CHECKIN_LOW_USD", "0") or 0)  # 低于该美元值标红


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z] {msg}", flush=True)


def money(quota, per_unit=500000):
    """New API / Apix 的 quota 折算成可读金额。"""
    try:
        usd = float(quota) / (per_unit or 500000)
    except (TypeError, ValueError):
        return str(quota)
    return f"${usd:.4f} (≈¥{usd * USD_RATE:.3f})"


def usd(v):
    """Apix 面板的金额字段本身就是美元，不做除法。"""
    try:
        d = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"${d:.4f} (≈¥{d * USD_RATE:.3f})"


# --------------------------------------------------------------------------- client
class Client:
    """带 cookie jar 的极简 JSON 客户端，自动处理两种面板的鉴权头。"""

    def __init__(self, base: str, timeout: int = TIMEOUT):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.token: str = ""
        self.uid: str = ""

    def call(self, method: str, path: str, payload=None, auth: bool = True,
             expect: tuple[int, ...] = ()) -> tuple[int, dict]:
        url = path if path.startswith("http") else self.base + path
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
        elif method == "POST":
            body = b"{}"
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Content-Type", "application/json")
        req.add_header("Referer", self.base + "/")
        req.add_header("Origin", self.base)
        if auth:
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            if self.uid:
                req.add_header("New-Api-User", str(self.uid))
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                code, raw = resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code = e.code
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                raw = ""
        except Exception as e:  # noqa: BLE001
            return 0, {"_err": f"{type(e).__name__}: {e}"}
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            data = {"_raw": raw[:400]}
        if expect and code not in expect:
            raise RuntimeError(f"{method} {path} -> HTTP {code}: {json.dumps(data, ensure_ascii=False)[:200]}")
        return code, data

    def retry(self, label: str, fn):
        """fn() 返回 (ok, fatal, result)。网络类错误重试，401/403 不重试。"""
        last = None
        for i in range(1, ATTEMPTS + 1):
            try:
                ok, fatal, res = fn()
            except Exception as e:  # noqa: BLE001
                ok, fatal, res = False, False, e
            last = res
            if ok:
                return res
            if fatal:
                raise RuntimeError(f"{label}: {res}")
            if i < ATTEMPTS:
                wait = BACKOFF * (2 ** (i - 1)) + random.uniform(0, 3)
                log(f"  ! {label} 第 {i} 次失败（{res}），{wait:.0f}s 后重试")
                time.sleep(wait)
        raise RuntimeError(f"{label} 连续 {ATTEMPTS} 次失败：{last}")


# --------------------------------------------------------------------------- 通用探测
def detect_flavor(c: Client) -> str:
    st, b = c.call("GET", "/api/v1/settings/public", auth=False)
    if st == 200 and isinstance(b, dict) and "site_name" in json.dumps(b)[:4000]:
        return "apix"
    st, b = c.call("GET", "/api/status", auth=False)
    if st == 200 and isinstance(b, dict) and b.get("success"):
        return "newapi"
    return "unknown"


def first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


# --------------------------------------------------------------------------- Apix 面板
def apix_run(c: Client, cfg: dict) -> dict:
    user = cfg["user"]
    pwd = cfg["pass"]

    def do_login():
        st, b = c.call("POST", "/api/v1/auth/login", {"email": user, "password": pwd}, auth=False)
        data = b.get("data") if isinstance(b, dict) else None
        if st == 200 and isinstance(data, dict) and data.get("access_token"):
            return True, False, data
        fatal = st in (401, 403)
        return False, fatal, f"HTTP {st} {json.dumps(b, ensure_ascii=False)[:180]}"

    tok = c.retry("登录", do_login)
    c.token = tok["access_token"]
    st, prof = c.call("GET", "/api/v1/user/profile")
    pd = prof.get("data", {}) if isinstance(prof, dict) else {}

    def bal(dd):
        b = first(dd, "balance", "available_balance", default=0)
        t = first(dd, "trial_balance", default=0)
        try:
            parts = []
            if float(b or 0):
                parts.append(f"余额 {usd(b)}")
            if float(t or 0):
                parts.append(f"试用金 {usd(t)}")
            return " + ".join(parts) or f"$0.0000（试用金 {first(dd, 'trial_validity_hours', default=24)}h 过期，用完归零）"
        except (TypeError, ValueError):
            return str(b)

    res = {"flavor": "apix", "user": first(pd, "username", "email", default=user)}
    res["balance"] = bal(pd)
    try:
        res["usd"] = round(float(pd.get("balance") or 0) + float(pd.get("trial_balance") or 0), 6)
    except (TypeError, ValueError):
        res["usd"] = None

    st, stat = c.call("GET", "/api/v1/user/checkin")
    sd = stat.get("data", {}) if isinstance(stat, dict) else {}
    if sd.get("enabled") is False or sd.get("checkin_enabled") is False:
        res["checkin"] = "站点未开启签到"
        return res

    skipped = bool(sd.get("today_checked") or sd.get("checked_in_today"))
    reward = first(sd, "reward_amount", "quota_awarded", "amount")
    res["range"] = f"每次 {usd(reward)}（{first(sd, 'trial_validity_hours', default='?')}h 内有效）"
    res["extra"] = {"累计签到": first(sd, "total_checkins", "checkin_count", default=None),
                    "上次签到": first(sd, "last_checkin_at", default=None)}
    if DRY_RUN:
        res["checkin"] = "（试运行）今日" + ("已签到" if skipped else "未签到")
        return res
    if skipped:
        res["checkin"] = "今日已签到，跳过"
        return res

    def do_claim():
        st2, b2 = c.call("POST", "/api/v1/user/checkin")
        if st2 == 200 and (b2.get("code") == 0 or b2.get("success")):
            return True, False, (b2.get("data") or {})
        if st2 == 409 or (isinstance(b2, dict) and "ALREADY" in json.dumps(b2).upper()):
            return True, False, {"already": True}
        return False, st2 in (401, 403), f"HTTP {st2} {json.dumps(b2, ensure_ascii=False)[:180]}"

    got = c.retry("签到", do_claim)
    if got.get("already"):
        res["checkin"] = "今日已签到（409）"
    else:
        r2 = first(got, "reward_amount", "quota_awarded", "amount", default=reward)
        res["checkin"] = f"签到成功 +{usd(r2)}" if r2 else "签到成功"
    st, prof2 = c.call("GET", "/api/v1/user/profile")
    if isinstance(prof2, dict) and prof2.get("data"):
        res["balance"] = bal(prof2["data"])
    return res


# --------------------------------------------------------------------------- New API 面板
def is_manual_block(msg) -> bool:
    t = str(msg)
    return "Turnstile" in t or "人机" in t or "recaptcha" in t.lower() or "geetest" in t.lower()


def newapi_run(c: Client, cfg: dict) -> dict:
    per_unit = int(cfg.get("quota_per_unit") or 500000)
    preset = (cfg.get("token") or "").strip()

    def do_login():
        st, b = c.call("POST", "/api/user/login", {"username": cfg["user"], "password": cfg["pass"]}, auth=False)
        d = b.get("data") if isinstance(b, dict) else None
        if st == 200 and isinstance(b, dict) and b.get("success") and d:
            return True, False, d
        txt = json.dumps(b, ensure_ascii=False)
        fatal = st in (401, 403) or is_manual_block(txt)
        return False, fatal, f"HTTP {st} {txt[:200]}"

    if preset:
        c.token = preset
        if cfg.get("uid"):
            c.uid = str(cfg["uid"])
    else:
        try:
            lg = c.retry("登录", do_login)
        except RuntimeError as e:
            if is_manual_block(e):
                return {"flavor": "newapi", "manual": True, "balance": "—（登录需人机验证，拿不到余额）",
                        "checkin": "需手动签到：站点开启人机验证（Turnstile），接口无法代签"}
            raise
        if isinstance(lg, dict):
            c.token = lg.get("access_token") or ""
            u = lg.get("user") if isinstance(lg.get("user"), dict) else {}
            c.uid = str(lg.get("id") or u.get("id") or "")

    def get_self():
        last = None
        for auth in (True, False):                       # 有 token 用 token，否则退回 cookie(+uid)
            st, b = c.call("GET", "/api/user/self", auth=auth)
            if isinstance(b, dict) and b.get("success") and b.get("data"):
                return True, False, b["data"]
            last = f"HTTP {st} {json.dumps(b, ensure_ascii=False)[:160]}"
            if "New-Api-User" in last and c.uid:
                continue
        return False, False, last

    me = c.retry("读取用户信息", get_self)
    c.uid = c.uid or str(me.get("id", ""))
    res = {"flavor": "newapi", "user": me.get("username"),
           "balance": money(me.get("quota", 0), per_unit),
           "extra": {"已用": money(me.get("used_quota", 0), per_unit), "分组": me.get("group")}}

    st, cs = c.call("GET", "/api/user/checkin")
    if not (isinstance(cs, dict) and cs.get("success")):
        res["checkin"] = f"读签到状态失败：{json.dumps(cs, ensure_ascii=False)[:150]}"
        return res
    data = cs.get("data") or {}
    if isinstance(data, bool):
        res["checkin"] = "站点未开启签到"
        return res
    if data.get("enabled") is False:
        res["checkin"] = "站点未开启签到"
        return res

    stats = data.get("stats", {}) or {}
    skipped = bool(stats.get("checked_in_today"))
    res["usd"] = round(float(me.get("quota", 0) or 0) / (per_unit or 500000), 6)
    res["range"] = (f"每次 {money(data.get('min_quota'), per_unit)}~{money(data.get('max_quota'), per_unit)}"
                    if data.get("min_quota") is not None else "")
    res["extra"].update({"签到次数": stats.get("checkin_count", stats.get("total_checkins")),
                         "累计奖励": money(stats.get("total_quota"), per_unit)
                         if stats.get("total_quota") is not None else None})
    elig = data.get("eligibility") or {}
    if elig.get("eligible") is False:
        reason = elig.get("reason") or elig.get("message") or json.dumps(elig, ensure_ascii=False)[:120]
        res["manual"] = True
        res["checkin"] = f"暂不可签到（需先满足条件：{reason}）"
        return res
    if DRY_RUN:
        res["checkin"] = "（试运行）今日" + ("已签到" if skipped else "未签到")
        return res
    if skipped:
        res["checkin"] = "今日已签到，跳过"
        return res

    def do_claim():
        st2, b2 = c.call("POST", "/api/user/checkin")
        txt = json.dumps(b2, ensure_ascii=False)
        if isinstance(b2, dict) and b2.get("success"):
            d2 = b2.get("data")
            return True, False, (d2 if isinstance(d2, dict) else {})
        if isinstance(b2, dict) and ("已签到" in txt or "already" in txt.lower()):
            return True, False, {"already": True}
        # 人机验证类错误不再重试
        fatal = st2 in (401, 403) or "Turnstile" in txt or "人机" in txt
        return False, fatal, f"HTTP {st2} {txt[:200]}"

    try:
        got = c.retry("签到", do_claim)
    except RuntimeError as e:
        if "Turnstile" in str(e) or "人机" in str(e):
            res["checkin"] = ("⚠ 本站开了 Cloudflare 人机验证，签到按钮必须人工点（已为你查好余额）。"
                              "想要全自动请换用未开启人机验证的站点")
        else:
            raise
        return res
    if got.get("already"):
        res["checkin"] = "今日已签到（重复请求）"
    else:
        awarded = first(got, "quota_awarded", "quota", "amount")
        res["checkin"] = f"签到成功 +{money(awarded, per_unit)}" if awarded else "签到成功"
    st, me2 = c.call("GET", "/api/user/self")
    d2 = (me2.get("data") or {}) if isinstance(me2, dict) else {}
    if d2:
        res["balance"] = money(d2.get("quota", me.get("quota", 0)), per_unit)
    return res


# --------------------------------------------------------------------------- 站点调度
def run_site(cfg: dict) -> dict:
    name = cfg.get("name") or cfg["base"]
    out = {"name": name, "base": cfg["base"], "ok": False, "checkin": "-", "balance": "-"}
    if cfg.get("skip"):
        out.update(ok=True, flavor="已跳过", checkin="按配置跳过（skip=true）", balance="—")
        return out
    t0 = time.time()
    try:
        c = Client(cfg["base"])
        flavor = (cfg.get("flavor") or "auto").lower()
        if flavor in ("auto", "", "unknown"):
            flavor = detect_flavor(c)
        out["flavor"] = flavor
        if flavor == "apix":
            out.update(apix_run(c, cfg))
        elif flavor == "newapi":
            out.update(newapi_run(c, cfg))
        else:
            out["checkin"] = "无法识别面板类型（既不是 Apix 也不是 New API）"
            return out
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        out["checkin"] = f"失败：{out['error']}"
    out["secs"] = round(time.time() - t0, 1)
    return out


def load_sites(args) -> list[dict]:
    raw = os.environ.get("SITES_JSON", "").strip()
    src = "SITES_JSON"
    if not raw and (args.config or os.environ.get("SITES_FILE")):
        path = args.config or os.environ["SITES_FILE"]
        with open(path, encoding="utf-8") as fh:
            raw, src = fh.read(), path
    if raw:
        try:
            sites = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"配置 JSON 解析失败（{src}）：{e}")
        if isinstance(sites, dict):
            sites = sites.get("sites", [sites])
        out = []
        for s in sites or []:
            s = dict(s)
            s.setdefault("user", first(s, "user", "username", "email"))
            s.setdefault("pass", first(s, "pass", "password", "pwd"))
            if not s.get("base"):
                continue
            out.append(s)
        return out
    # 单站兼容模式（沿用原 apix.chat 的 Secret 名）
    email = os.environ.get("APIX_EMAIL", "").strip()
    pwd = os.environ.get("APIX_PASSWORD", "").strip()
    if email and pwd:
        return [{"name": "Apix", "base": os.environ.get("APIX_BASE_URL", "https://apix.chat"),
                 "user": email, "pass": pwd, "flavor": "apix"}]
    return []


# --------------------------------------------------------------------------- 通知 / 汇总
def total_usd(rows):
    vals = [r["usd"] for r in rows if isinstance(r.get("usd"), (int, float))]
    return round(sum(vals), 4) if vals else None


def low_flag(r):
    thr = LOW_USD
    if not thr or not isinstance(r.get("usd"), (int, float)) or r.get("manual"):
        return ""
    return "🔴低余额" if r["usd"] < thr else ""


def build_report(rows: list[dict]) -> str:
    ok = sum(1 for r in rows if r["ok"])
    man = sum(1 for r in rows if r.get("manual"))
    tot = total_usd(rows)
    lines = [f"【中转站签到】{time.strftime('%Y-%m-%d %H:%M')} UTC",
             f"成功 {ok}/{len(rows)}" + (f"，其中 {man} 家需手动" if man else "")
             + (f"｜合计余额 ${tot}" if tot is not None else "")
             + ("（试运行）" if DRY_RUN else ""), ""]
    for r in sorted(rows, key=lambda x: -(x["usd"] if isinstance(x.get("usd"), (int, float)) else -1)):
        mark = "⚠️" if r.get("manual") else ("✅" if r["ok"] else "❌")
        low = low_flag(r)
        head = f"{mark} {r['name']} · {r.get('flavor', '?')}" + (f" {low}" if low else "")
        body = f"{head}\n   签到：{r['checkin']}"
        if r.get("balance"):
            body += f"\n   余额：{r['balance']}"
        if r.get("range"):
            body += f"\n   区间：{r['range']}"
        for k, v in (r.get("extra") or {}).items():
            if v not in (None, "", "-"):
                body += f"\n   {k}：{v}"
        if r.get("user"):
            body += f"\n   账号：{r['user']}"
        if r.get("error"):
            body += f"\n   错误：{r['error']}"
        lines.append(body)
    return "\n".join(lines)


def write_summary(rows: list[dict]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    ok = sum(1 for r in rows if r["ok"] and not r.get("manual"))
    man = sum(1 for r in rows if r.get("manual"))
    bad = sum(1 for r in rows if not r["ok"])
    tot = total_usd(rows)
    md = ["## 🎯 中转站每日签到 + 余额看板", "",
          f"**{time.strftime('%Y-%m-%d %H:%M')} UTC** ｜ 自动成功 **{ok}** ｜ 需手动 {man} ｜ 失败 {bad}"
          + (f" ｜ 合计余额 **${tot}**（≈¥{tot * USD_RATE:.2f}）" if tot is not None else ""), "",
          "| 站点 | 面板 | 签到结果 | 余额 | 备注 |", "|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: -(x["usd"] if isinstance(x.get("usd"), (int, float)) else -1)):
        ex = "，".join(f"{k} {v}" for k, v in (r.get("extra") or {}).items() if v not in (None, ""))
        if r.get("range"):
            ex = (r["range"] + ("，" + ex if ex else ""))
        icon = "⚠️" if r.get("manual") else ("✅" if r["ok"] else "❌")
        low = low_flag(r)
        bal = r.get("balance", "-") + (f" {low}" if low else "")
        md.append(f"| {icon} **{r['name']}**<br><sub>{r['base'].split('://')[-1]}</sub> "
                  f"| {r.get('flavor', '-')} | {r['checkin']} | {bal} | {ex} |")
    if LOW_USD:
        md += ["", f"<sub>🔴 = 余额低于设定阈值 ${LOW_USD}（Secret/环境变量 `CHECKIN_LOW_USD`）</sub>"]
    md += ["", f"<sub>试运行：{DRY_RUN}｜生成时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC</sub>"]
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(md) + "\n")
    except OSError:
        pass


def send_notify(text: str) -> None:
    keys = []
    if os.environ.get("PUSHPLUS_TOKEN"):
        keys.append(("pushplus", os.environ["PUSHPLUS_TOKEN"]))
    if os.environ.get("SERVERCHAN_KEY"):
        keys.append(("serverchan", os.environ["SERVERCHAN_KEY"]))
    tg = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    hook = os.environ.get("NOTIFY_WEBHOOK")
    if not keys and not (tg and chat) and not hook:
        log("未配置通知渠道，跳过推送")
        return
    for kind, key in keys:
        url = f"https://push.plus/v1/{key}/message" if kind == "pushplus" else f"https://{key}.push.ft07.com/send"
        payload = {"title": "中转站签到", "content": text} if kind == "pushplus" else {"desp": text}
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                log(f"通知[{kind}] -> HTTP {r.status}")
        except Exception as e:  # noqa: BLE001
            log(f"通知[{kind}] 失败：{e}")
    if tg and chat:
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{tg}/sendMessage",
                                         data=json.dumps({"chat_id": chat, "text": text}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                log(f"通知[telegram] -> HTTP {r.status}")
        except Exception as e:  # noqa: BLE001
            log(f"通知[telegram] 失败：{e}")
    if hook:
        try:
            req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                log(f"通知[webhook] -> HTTP {r.status}")
        except Exception as e:  # noqa: BLE001
            log(f"通知[webhook] 失败：{e}")


REPORT_ROWS: list[dict] = []


def main() -> int:
    ap = argparse.ArgumentParser(description="多站点中转站签到 + 余额巡检")
    ap.add_argument("--config", help="站点 JSON 配置文件的本地路径（不进仓库）")
    ap.add_argument("--dry-run", action="store_true", help="只查询不签到")
    ap.add_argument("--site", help="只跑名字/域名包含该关键字的站点")
    args = ap.parse_args()

    global DRY_RUN
    if args.dry_run:
        DRY_RUN = True

    sites = load_sites(args)
    if args.site:
        sites = [s for s in sites if args.site in (s.get("name", "") + s.get("base", ""))]
    if not sites:
        log("没有可用站点：请设置 SITES_JSON，或用 --config 指向本地 accounts.json")
        return 2

    log(f"共 {len(sites)} 个站点" + ("（试运行）" if DRY_RUN else ""))
    rows = []
    for i, cfg in enumerate(sites, 1):
        log(f"[{i}/{len(sites)}] {cfg.get('name')} {cfg['base']}")
        r = run_site(cfg)
        rows.append(r)
        log(f"  -> {r['checkin']}｜余额 {r.get('balance', '-')}" + (f"｜{r['secs']}s" if r.get("secs") else ""))
        if i < len(sites):
            time.sleep(random.uniform(1.5, 4.0))

    global REPORT_ROWS
    REPORT_ROWS = rows
    report = build_report(rows)
    print("\n" + "=" * 72 + "\n" + report + "\n" + "=" * 72)
    write_summary(rows)

    policy = os.environ.get("CHECKIN_NOTIFY_ON", "on_failure")
    failed = [r for r in rows if not r["ok"]]
    manual = [r for r in rows if r.get("manual")]
    if policy == "always" or (policy == "on_failure" and (failed or manual)) or (policy == "on_success" and not failed):
        send_notify(report)

    if failed:
        log(f"❌ {len(failed)} 个站点异常：" + ", ".join(r["name"] for r in failed))
        return 1
    if manual:
        log(f"⚠️ {len(manual)} 个站点需手动签到：" + ", ".join(r["name"] for r in manual))
    log("✅ 全部站点处理完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
