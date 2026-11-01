# AI 热探 Signal Desk

面向 AI 编程创作者的个人热点监控台。按关键词和主题检索国内与国际资讯，保留原始来源，结合 OpenAI 兼容模型和独立发布者证据核验，并通过浏览器实时提醒和 SMTP 邮件发送紧急/高优先级热点。

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
- Bing RSS、DuckDuckGo 根路径和 Hacker News Algolia 搜索补充发现来源；DuckDuckGo 仅请求 robots.txt 允许的根路径查询，不请求被禁止的 `/html`，也不绕过访问限制。当前网络探测曾超时，只有请求成功时才算本轮补充结果。Hacker News 只检索故事帖。
- Bing RSS 结果仅用于本机个人、非商业聚合展示，并保留来源链接；不再分发其 RSS 结果。
- TwitterAPI.io 使用 `Top` 与 `Latest` 搜索，每种排序最多 2 页、每页最多 20 条，并按游标翻页；中文词在可用时同时搜索英文译词。采集阶段保留候选，AI 分析后再执行最终门槛：仅展示原创帖，点赞至少 10、转发至少 5、浏览至少 500、作者粉丝至少 100，任一指标缺失即过滤。请求之间至少间隔 5 秒。
- 添加明确的 `@handle`、X 主页 URL 或 UID 时，直接读取账号资料与原创动态；普通文本仍按话题搜索。Bilibili/微博账号 URL 会被识别，但因当前探测到访问控制且没有已验证的合规公开接口，扫描会说明未启用，不会尝试绕过。
- 来源按发布者/域名去重并保留原始链接、区域与语言。
- 真实性判断是独立标签：模型可判为 `credible`（可信）、`suspicious`（可疑）、`uncertain`（待核验）或 `unverified`（未核验）。`credible` 还需官方域名/账号，或至少两个不同受信发布者（如 Reuters、AP、BBC、财新等）支持；不满足时降为 `uncertain`。
- 缺少远程 AI API Key 或证据不足时，内容仍会展示，但标记为未核验/待观察；真实性不会被伪装成“已核实”。

## 模型与通知
在 `.env` 或页面“连接与设置”填写凭据，不要把密钥贴进聊天或提交到仓库。系统环境变量优先于 `.env`，两者优先于页面/数据库设置；空白变量不会覆盖已有值。

- **AI 核验**：支持 OpenAI 兼容接口。默认使用 CloseAI Base URL `https://api.openai-proxy.org/v1` 与模型 `deepseek-v4.1-flash`，通过 `AI_API_KEY` 配置 CloseAI Key；也可在页面设置切换到 OpenRouter 等服务。旧环境变量 `OPENROUTER_BASE_URL`、`OPENROUTER_MODEL`、`OPENROUTER_API_KEY` 仍兼容。CloseAI 使用 JSON 对象模式请求结构化核验结果，并单独生成相关性理由。
- **TwitterAPI.io**：设置 `TWITTERAPI_API_KEY`。缺少此 Key 时不会调用 X 搜索。
- **SMTP 邮件**：设置 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USERNAME`、`SMTP_PASSWORD`、`SMTP_SENDER`、`SMTP_RECIPIENT` 和 TLS 配置；保存后可发送测试邮件。邮件与实时提醒仅对紧急度为 `urgent`（紧急）或 `high`（高）且通过应用层证据门槛判为可信的热点触发。
- `.env` 已在 `.gitignore` 中。页面设置凭据保存在本机 SQLite；服务状态 API 仅返回是否已配置，不回传密钥。AI/X/SMTP 不可用时，扫描不会因单一依赖失败而停止。
- **浏览器实时提醒**：页面保持打开即可通过 WebSocket 更新；仅紧急/高优先级且应用层判为可信的热点发送提醒。桌面通知需要点击“启用浏览器通知”并授予浏览器权限。
- 现有 Telegram 通道保留为可选兼容方式，不是默认提醒渠道。

凭据保存在本机 SQLite 数据库中，服务状态 API 仅返回是否已配置，不回传密钥。AI/X/SMTP 不可用时，扫描不会因单一依赖失败而停止。

## AI 审核流程

系统从各信息源获取内容后，将每条信息送交 AI 分析，输出结构化结果（严格 JSON Schema）：

1. **真实性判断**：判断信息是否真实可信，过滤标题党、假消息与冒充官方；证据不足时返回 `uncertain`。
2. **相关性评分**：给出 0–100 分，表示与监控关键词的匹配度。
3. **重要性分级**：分为 `urgent`（紧急）、`high`（高）、`medium`（中）、`low`（低）四级，作为主分类与筛选维度。
4. **摘要生成**：自动生成简洁中文摘要，便于快速阅读。

真实性判断、相关性、紧急度是三个独立维度，不会用真实性代替紧急度；提醒与主分类均以紧急度为准。



## 前端视觉与动效

- **主题**：深色科技主题（深空蓝黑底 + 电蓝/青色高光），玻璃拟态卡片与侧栏，提升长时间监控的舒适度与科技感。
- **动效（克制、可访问）**：
  - 背景极光（Aurora）：双色柔光缓动，营造 AI 感。
  - 斜向光束（Beam drift）：重复渐变缓慢漂移。
  - 流星雨（Meteors）：3 条斜向流星循环下落，交错延迟。
  - 扫描按钮移动边框（Spin border）：旋转渐变边框，突出主操作。
  - 卡片 Spotlight：鼠标跟随高光，仅悬停卡片更新，避免性能开销。
  - 标题打字机（Typewriter）：副标题逐字显现。
  - 标题文字生成（Text generate）：模糊淡入。
- **可访问性**：所有动画在 `prefers-reduced-motion: reduce` 下自动关闭；保留键盘焦点样式；移动端响应式布局不变。
- **实现**：纯 CSS/轻量 JS，不依赖 React/Aceternity 运行时，避免影响加载与使用；配色与动效参考 Aceternity UI / UI-UX Pro Max 推荐的 HUD/Sci-Fi FUI 方向，但保持简洁。



## 最近改动

- **扫描间隔**：默认 30 分钟扫描一次资讯，可在页面调整（最小 5 分钟）。
- **紧急度与提醒**：AI 输出 `urgent/high/medium/low` 四级紧急度作为主分类；仅 `urgent` 与 `high` 触发邮件、WebSocket 与 Telegram 提醒，消息中明确标注真实性结论。
- **真实性独立标签**：`credible/suspicious/uncertain/unverified` 不再作为主分组，仅作为真实性标签展示。
- **来源扩展与 X 质量门槛**：新增 Bing RSS、DuckDuckGo 和 Hacker News；X 使用 Top/Latest 游标分页，候选采集与最终质量门槛分离，历史数据库记录也在 API 响应前复核。
- **提醒门槛**：邮件与实时提醒仅发送紧急/高优先级且通过应用层可信门槛的热点。
- **相关性与摘要**：AI 输出 0–100 相关性评分、独立相关性理由与中文摘要；理由可逐条展开，或一键展开/折叠全部。
- **前端体验**：深色科技主题与克制动效（极光、光束、流星、Spotlight、打字机等），`prefers-reduced-motion` 下自动关闭。
- **刷新策略**：移除 15 秒全量轮询，改为 30 秒轻量监听扫描完成后再刷新，WebSocket 即时推送 urgent/high，避免界面闪烁。
- **更多信号与筛选**：状态 API 对全量记录筛选和排序后再分页，界面每次加载 20 条并可继续加载；支持按来源、紧急度、最低相关性筛选，以及按最新发布、最新发现、相关性、重要程度和热度综合排序。
- **来源就地预览**：每条热点直接显示最多 3 条来源标题、摘要摘录和时间，其余来源可展开；打开原文仍是可选操作。
- **相关性 few-shot**：审核提示包含主题不匹配与直接相关的示例，要求真实性与相关性分开判断，并降低泛化/重复/无实质更新内容的分数。相关性 55+ 为默认筛选，未核验内容仍可见。
- **多语言译文**：审核结果增加译文；日语、韩语等非中英内容优先译为中文，西语/法语译为英文，译文在工作台内显示。

## Agent Skill

将 [`skills/ai-hot-monitor/SKILL.md`](skills/ai-hot-monitor/SKILL.md) 提供给其他 AI。Skill 描述本地 API、国内外实时搜索、证据核验、结果报告和密钥安全要求。

本地 API：`GET /api/state?page=1&pageSize=20&sort=recent&urgency=all&source=all&relevance=0`、`GET /api/health`、`POST /api/keywords`、`DELETE /api/keywords/{id}`、`POST /api/scan`、`POST /api/settings`、`POST /api/notifications/test-email`。`sort` 支持 `recent`、`discovered`、`relevance`、`importance`、`hot`。

## 测试

```powershell
py -3 -m unittest discover -s tests -v
```
