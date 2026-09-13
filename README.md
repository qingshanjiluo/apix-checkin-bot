# 中转站每日自动签到 + 余额巡检（GitHub Actions）

[![每日签到](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/qingshanjiluo/apix-checkin-bot/main/status/badge.json)](../../actions/workflows/daily-checkin.yml)
[![Workflow](https://github.com/qingshanjiluo/apix-checkin-bot/actions/workflows/daily-checkin.yml/badge.svg)](../../actions/workflows/daily-checkin.yml)

> 最新一次结果自动回写在 **[签到结果.md](签到结果.md)**（表格含每站签到结果、余额、每次区间、累计奖励）。

给一堆 AI 中转站（API Relay）自动做两件事：**每日签到白嫖额度** + **余额巡检**。
纯 HTTP 调用站点自己的接口，**不需要浏览器、不需要过验证码的站全自动**，跑在 GitHub Actions 上，每天两次（北京时间 08:10 / 20:10），互不干扰、可重复运行。

- 已适配 **Apix 面板**（`apix.chat` 及同类站）：`/api/v1/auth/login` → `/api/v1/user/checkin`
- 已适配 **New API 面板**（绝大多数公益中转站）：`/api/user/login` → `/api/user/self` → `/api/user/checkin`
  - 自动兼容两种鉴权：`session cookie + New-Api-User` 头，或 `access_token` 走 `Authorization: Bearer`
- 面板类型自动探测，不用手写；只有探测失败时才需要手动加 `"flavor": "apix"` 或 `"newapi"`

## 一、准备站点配置

复制 `sites.example.json` 的内容，改成你自己的账号，形如：

```json
[
  {"name": "芯算AI",   "base": "https://xinsuanai.com",          "user": "你@qq.com",        "pass": "密码"},
  {"name": "小鲸AI",   "base": "https://open.xiaojingai.com",    "user": "另一个@gmail.com", "pass": "密码"},
  {"name": "Apix",     "base": "https://apix.chat",              "user": "你@qq.com",        "pass": "密码", "flavor": "apix"}
]
```

可选字段：

| 字段 | 作用 |
|---|---|
| `flavor` | `auto`（默认）/ `apix` / `newapi`，探测不准时手动指定 |
| `quota_per_unit` | 额度换算除数，New API 默认 `500000`（=1$） |
| `token` + `uid` | 不想给密码时用「系统访问令牌」：站点控制台 → 个人设置 → 系统访问令牌，填 `"token": "…"`, `"uid": "123"`（开了人机验证的站也能查余额/是否已签到，只差最后手点签到） |
| `skip` | `true` 时该站只保留在配置里不执行 |

> `user` 对 New API 站点填**邮箱或用户名都行**（接口字段是 `username`）。

## 二、存进 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → 新建 **`SITES_JSON`**，把整个 JSON 数组粘进去（压缩成一行也可以）。

```powershell
# 或用 gh CLI
gh secret set SITES_JSON --body (Get-Content sites.local.json -Raw)
```

只跑单站也可以用旧的两个 Secret：`APIX_EMAIL` / `APIX_PASSWORD`（脚本会自动回退到 apix.chat 单站模式）。

## 三、手动验证一次

```powershell
# 本地试运行（只查询，不签到）；凭据文件放本地，已被 .gitignore 忽略
python checkin.py --config sites.local.json --dry-run

# 本地真签到
python checkin.py --config sites.local.json

# 只跑某一个站
python checkin.py --config sites.local.json --site xinsuanai
```

Actions 页面 → **Run workflow** 可以勾选「只查询不签到」、指定站点、指定通知策略。
每次运行结束后，Run 页面的 **Summary** 里有一张表：每个站的签到结果、余额、签到区间、累计奖励。

## 四、通知（可选，全部不填也能用）

| Secret | 说明 |
|---|---|
| `PUSHPLUS_TOKEN` | 微信 PushPlus 推送 |
| `SERVERCHAN_KEY` | Server酱³ |
| `TG_BOT_TOKEN` + `TG_CHAT_ID` | Telegram Bot |
| `NOTIFY_WEBHOOK` | 通用 webhook，`POST {"text": "…"}` |

策略由 `CHECKIN_NOTIFY_ON` 控制：`on_failure`（默认，有失败/有站需手动才推）/ `always` / `on_success`。

## 四点五、看板参数

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `CHECKIN_LOW_USD` | 0（关） | 余额低于该美元值的站在表里标 🔴低余额（在 workflow 的 `env:` 里加一行即可启用） |
| `CHECKIN_USD_RATE` | 7.2 | 折算人民币展示用汇率 |

Summary 表按余额从高到低排序，并给出**合计余额**。

## 五、已知边界（重要）

1. **开了 Cloudflare 人机验证（Turnstile）的站不能自动签到。** 这类站的路由是
   `selfRoute.POST("/checkin", middleware.TurnstileCheck(), controller.DoCheckin)`，
   没有 `turnstile` token 直接返回 `Turnstile token 为空`，登录接口同理。
   脚本会把它们标成 ⚠️ **需手动签到**，不再当成报错（例如 `api.uiuihao.com`）。
   想半自动：在该站生成「系统访问令牌」填 `token`/`uid`，脚本就能查余额和是否已签到，只差最后点一下。
2. **签到额度会过期。** Apix 的试用金 24 小时有效、New API 的赠送额度多为长期，具体看站。
3. **公益站随时可能改规则、限流或跑路。** 建议只白嫖不充值；要充值也别大额，敏感数据别过第三方中转。
4. 同一天重复运行安全：已签到会返回 `今日已签到` / `409`，脚本识别为成功。
5. 仓库 60 天无活动 GitHub 会停用 schedule，`keep-alive.yml` 每半月自动空提交保活。
6. 新注册 GitHub 账号需**验证邮箱**才会执行定时任务。

## 文件

| 文件 | 说明 |
|---|---|
| `checkin.py` | 主脚本：多站点、双面板、签到 + 余额 + 通知 + Summary |
| `sites.example.json` | 配置模板（不含真实凭据） |
| `run-local.ps1` | 本地交互式跑一次 |
| `.github/workflows/daily-checkin.yml` | 每日定时（08:10 / 20:10 北京时间） |
| `.github/workflows/keep-alive.yml` | 保活空提交，防止 schedule 被停用 |
| `apix_checkin.py` | 旧版单站脚本，保留备查，已由 `checkin.py` 取代 |
