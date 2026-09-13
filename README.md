# Apix.chat 每日自动签到（GitHub Actions）

每天自动登录 <https://apix.chat> 并领取 **每日签到奖励**（当前为 `trial_balance 0.5`，24 小时有效），
全程无需浏览器、无需 Cookie，直接调用站点官方接口。

## 工作原理

| 步骤 | 接口 | 说明 |
| --- | --- | --- |
| 1. 登录 | `POST /api/v1/auth/login` | `{"email","password"}` → `access_token`（有效期 86400s） |
| 2. 查状态 | `GET /api/v1/user/checkin` | 返回 `today_checked` / `today_date` / 奖励信息 |
| 3. 签到 | `POST /api/v1/user/checkin` | `200` 成功；`409 CHECKIN_ALREADY_CLAIMED` = 今日已领，视为成功 |

脚本只有标准库依赖，自带 4 次指数退避重试（401/403 不重试），并把结果写入 Actions 的 Job Summary。

## 目录结构

```
apix-checkin-bot/
├─ apix_checkin.py                    # 签到脚本（本地也能跑）
├─ .github/workflows/
│  ├─ daily-checkin.yml               # 每日定时签到
│  └─ keep-alive.yml                  # 每半月空提交，防止 Actions 因仓库 60 天无活动被停用
└─ README.md
```

## 部署步骤

1. 在 GitHub 新建仓库（**Private 即可**，Actions 私有仓库 Linux 分钟数免费 unlimited 到 2000/月，足够）。
2. 把本目录内容推上去：

   ```bash
   cd apix-checkin-bot
   git init -b main
   git add . && git commit -m "feat: apix daily check-in"
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```

3. 配置 Secrets：仓库 **Settings → Secrets and variables → Actions → New repository secret**

   | 名称 | 值 | 必填 |
   | --- | --- | --- |
   | `APIX_EMAIL` | 登录邮箱 | ✅ |
   | `APIX_PASSWORD` | 登录密码 | ✅ |
   | `SERVERCHAN_KEY` / `PUSHPLUS_TOKEN` | Server酱³ token，失败时推送 | 可选 |
   | `TG_BOT_TOKEN` + `TG_CHAT_ID` | Telegram 通知 | 可选 |
   | `NOTIFY_WEBHOOK` | 通用 webhook，`POST {"text": "..."}` | 可选 |
   | `APIX_REFRESH_TOKEN` | 只想用 token 不想放密码时填 | 可选 |

4. 仓库 **Settings → Actions → General**：
   - *Workflow permissions* 保持 `Read repository contents`（`keep-alive.yml` 单独声明了 `contents: write`）；
   - 确认 **Allow all workflows and reusable workflows** 已开启。
5. 到 **Actions** 页面，手动点一次 **Run workflow** 验证。日志里看到
   `签到成功` 或 `今日已签到` 且 Job Summary 出现奖励表格，即部署完成。

> 新注册的 GitHub 账号必须先验证邮箱，否则 Schedule 不会触发。

## 定时策略

```cron
10 0,12 * * *   # UTC 00:10 / 12:10  →  北京时间 08:10 / 20:10
```

一天两次机会：第一次成功则第二次只会提示"已签到"（接口幂等）；第一次因为网络抖动失败，第二次还能补上。
签到日期按**服务器时区 Asia/Shanghai** 计算，所以北京时间 00:00 之后任意时刻跑都算当天。

## 本地手动跑一次

```powershell
# Windows PowerShell
$env:APIX_EMAIL="你的邮箱"; $env:APIX_PASSWORD="你的密码"
python .\apix_checkin.py
```

```bash
# Linux / macOS
APIX_EMAIL=你的邮箱 APIX_PASSWORD=你的密码 python3 apix_checkin.py
```

退出码：`0` 成功（含"今日已签到"）、`1` 失败、`2` 缺少配置。

## 可调参数（环境变量 / Workflow env）

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `APIX_BASE_URL` | `https://apix.chat` | 换域名 / 镜像站 |
| `APIX_ATTEMPTS` | `4` | 每步重试次数 |
| `APIX_BACKOFF` | `5` | 退避基数秒（5 → 10 → 20 → 40） |
| `APIX_TIMEOUT` | `30` | 单请求超时 |
| `APIX_NOTIFY_ON` | `on_failure` | `always` / `on_failure` / `on_success` |
| `APIX_USER_AGENT` | Chrome UA | 站点风控若变严可换 |

## 常见问题

- **401 / 登录失败**：密码被改，或站点启用了人机验证（`/api/v1/settings/public` 里的
  `turnstile_enabled`、`tencent_captcha_enabled`）。目前这两项均为 `false`，纯账号密码即可；
  若以后开启，需要在 `obtain_token()` 中补上验证码字段。
- **409 CHECKIN_ALREADY_CLAIMED**：正常情况，脚本按成功处理。
- **想停掉**：GitHub 仓库 Actions 页面 → 左侧 *Workflows* → **Disable workflows**。
- **安全性**：密码放在 GitHub Secrets 里（仓库加密存储，只有本工作流可读），不会出现在代码或日志中；
  脚本从不打印 token。建议定期轮换 apix 密码。
