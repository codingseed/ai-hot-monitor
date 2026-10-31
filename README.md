# 热探 Signal Desk

面向 AI 编程创作者的个人热点监控台。按关键词和主题检索国内与国际资讯，保留原始来源，结合 OpenRouter 模型和独立发布者证据核验，并通过浏览器实时提醒和 SMTP 邮件发送紧急/高优先级热点。

页面文件本身是静态 HTML/CSS/JavaScript，但热点数据由本地 Python 服务从 SQLite/API 动态加载。页面打开时通过 WebSocket 实时接收新热点，并以 15 秒轮询作为状态刷新兜底；后台按设置的间隔扫描，默认 30 分钟。

## 启动

需要 Python 3.10 或更高版本。先复制 `.env.example` 为 `.env`，只在本机填写凭据，再安装依赖并启动：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
py -3 -m pip install -r requirements.txt
py -3 app.py
```

打开 <http://127.0.0.1:8765>。首次启动约 20 秒开始扫描，也可以点击“立即扫描”。监控主题默认“AI 编程”，扫描间隔和关键词可在页面中修改。

静态页面由 Python 服务提供；HTTP API 默认端口为 `8765`，WebSocket 默认端口为 `8766`。可通过 `AI_HOT_MONITOR_PORT`、`AI_HOT_MONITOR_WS_PORT` 更改端口。两项服务默认只绑定 `127.0.0.1`，不要把未认证的控制台暴露到公网。

数据保存在 `data/monitor.db`，可通过 `AI_HOT_MONITOR_DB` 指定数据库路径。不要共享数据库文件，其中包含本机配置的凭据。

## 数据来源与核验

- Google News RSS 搜索 URL 同时使用中文/中国区域和英文/全球区域，不需要搜索 API Key；中文查询会在配置 AI 后生成英文检索词并缓存。
- TwitterAPI.io 获取 X 最新公开帖子。为兼容新账号限额，查询之间至少间隔 5 秒；限流或凭据未配置时，RSS 来源仍继续工作。
- 来源按发布者/域名去重并保留原始链接、区域与语言。
- 真实性判断是独立标签：模型可判为 `credible`（可信）、`suspicious`（可疑）、`uncertain`（待核验）或 `unverified`（未核验）。`credible` 还需官方域名/账号，或至少两个不同受信发布者（如 Reuters、AP、BBC、财新等）支持；不满足时降为 `uncertain`。
- 缺少 OpenRouter Key 或证据不足时，内容仍会展示，但标记为未核验/待观察；真实性不会被伪装成“已核实”。

## 模型与通知
在 `.env` 或页面“连接与设置”填写凭据，不要把密钥贴进聊天或提交到仓库。系统环境变量优先于 `.env`，两者优先于页面/数据库设置；空白变量不会覆盖已有值。

- **OpenRouter**：设置 `OPENROUTER_API_KEY`；默认 Base URL 为 `https://openrouter.ai/api/v1`，模型为 `deepseek/deepseek-v4.1-flash`。程序使用严格 JSON Schema 请求结构化核验结果。
- **TwitterAPI.io**：设置 `TWITTERAPI_API_KEY`。缺少此 Key 时不会调用 X 搜索。
- **SMTP 邮件**：设置 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USERNAME`、`SMTP_PASSWORD`、`SMTP_SENDER`、`SMTP_RECIPIENT` 和 TLS 配置；保存后可发送测试邮件。邮件与实时提醒仅对紧急度为 `urgent`（紧急）或 `high`（高）的热点触发，无论其真实性判断如何；消息中会明确标注真实性结论。
- `.env` 已在 `.gitignore` 中。页面设置凭据保存在本机 SQLite；服务状态 API 仅返回是否已配置，不回传密钥。AI/X/SMTP 不可用时，扫描不会因单一依赖失败而停止。
- **浏览器实时提醒**：页面保持打开即可通过 WebSocket 更新；桌面通知需要点击“启用浏览器通知”并授予浏览器权限。
- 现有 Telegram 通道保留为可选兼容方式，不是默认提醒渠道。

凭据保存在本机 SQLite 数据库中，服务状态 API 仅返回是否已配置，不回传密钥。AI/X/SMTP 不可用时，扫描不会因单一依赖失败而停止。

## AI 审核流程

系统从各信息源获取内容后，将每条信息送交 AI 分析，输出结构化结果（严格 JSON Schema）：

1. **真实性判断**：判断信息是否真实可信，过滤标题党、假消息与冒充官方；证据不足时返回 `uncertain`。
2. **相关性评分**：给出 0–100 分，表示与监控关键词的匹配度。
3. **重要性分级**：分为 `urgent`（紧急）、`high`（高）、`medium`（中）、`low`（低）四级，作为主分类与筛选维度。
4. **摘要生成**：自动生成简洁中文摘要，便于快速阅读。

真实性判断、相关性、紧急度是三个独立维度，不会用真实性代替紧急度；提醒与主分类均以紧急度为准。

## Agent Skill

将 [`skills/ai-hot-monitor/SKILL.md`](skills/ai-hot-monitor/SKILL.md) 提供给其他 AI。Skill 描述本地 API、国内外实时搜索、证据核验、结果报告和密钥安全要求。

本地 API：`GET /api/state`、`GET /api/health`、`POST /api/keywords`、`DELETE /api/keywords/{id}`、`POST /api/scan`、`POST /api/settings`、`POST /api/notifications/test-email`。

## 测试

```powershell
py -3 -m unittest discover -s tests -v
```
