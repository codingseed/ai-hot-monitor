# AI 热探 Signal Desk 方案设计

## 1. 设计原则

- 在现有实现上增量演进，不重写已可用的本地工作台。
- 先统一多来源数据，再聚合事件、判断证据、通知和展示。
- 把“AI 判断”与“可验证证据”分开：最终可信状态由明确的来源门槛约束。
- 外部 API 契约以开发当日官方文档为准；未经 MCP 核验的请求参数不进入实现。

## 2. 当前实现基线

- `app.py` 使用 Python 标准库 HTTP 服务、SQLite、Google News RSS、TwitterAPI.io、OpenRouter-compatible Chat Completions 和可选 Telegram Bot API；SMTP 与 WebSocket 已接入。
- `web/` 已有响应式工作台、关键词管理、定时扫描、来源证据展开和本地设置表单。
- `story_sources` 已保存同一热点的多条来源，核验门槛按官方域名/账号或可信发布者判定；模型判断本身不能单独升级为可信。
- 当前搜索源为 Google News RSS、Bing RSS、DuckDuckGo 根路径和 Hacker News；国内搜索入口因访问控制/robots 限制未启用，国内替代 RSS/API 尚待逐项实测。
- X 搜索使用 `Top` 与 `Latest`，每种排序最多 2 页并按 cursor 翻页；采集阶段保留候选，AI 分析后再应用点赞 10/转发 5/浏览 500/粉丝 100 的原创帖门槛，缺失指标不通过。显式 X 账号标识会调用资料与原创动态接口。
- `/api/state` 对新旧数据统一执行 X 最终门槛；不合格的 X-only 热点不返回，混合事件会剔除不合格 X 证据并避免继续以该 X 帖作为主链接。

## 3. 目标架构

```text
定时器 / 手动扫描
        |
        +--> Google News RSS 适配器
        +--> Bing RSS 适配器（仅个人非商业聚合）
        +--> Hacker News Algolia 搜索适配器
        +--> 国内公开 RSS/API 适配器（逐项验证后启用）
        +--> TwitterAPI.io 适配器（Top + Latest / 分页 / 账号查询）
                    |
            尽量完整采集候选内容
                    |
           基础去重 / 时间过滤
                    |
       OpenRouter AI 相关性 / 真实性分析
                    |
       X 原创类型 + 互动阈值最终过滤
                    |
       官方来源/来源数量规则 + 展示 / 通知
                    |
        SQLite --> Web 工作台 / WebSocket 实时推送 / SMTP 邮件
                    |
              Agent Skill 本地 API
```

## 4. 模块职责

### 4.1 来源适配器

- 定义统一的来源记录结构，至少包含 `platform`、`publisher`、`external_id`、`title`、`summary`、`url`、`published_at`、`matched_keyword`。
- 保留现有 RSS 解析器并将其包装为一个适配器；分别请求中文/中国区域与英文/全球区域的 Google News RSS，结果标记检索区域与原始语言后再去重。
- Bing RSS 保留为个人非商业聚合来源；Hacker News 保留为国际技术来源。DuckDuckGo 只有在当地网络实测可用且入口符合 robots.txt 时才启用；不得请求 `/html`。国内来源优先采用免 Key 的公开 RSS/API，不抓取被 robots 禁止的结果页，不模拟浏览器、不使用登录 Cookie、不规避 412/432 等访问控制。候选媒体 RSS（如量子位、InfoQ、中国区官方技术博客）均须先核验 robots、条款、响应格式和有效条目，再纳入启用清单。
- TwitterAPI.io 候选采集改为 `Top` 与 `Latest` 两类搜索，各最多 2 页（每页最多 20 条），以 `has_next_page`/`next_cursor` 控制分页；中文词同时检索原词和英文译词。采集阶段不以最终互动门槛截断候选；按“去重/时间过滤 → AI 相关性与真实性分析 → 确定性最终过滤”处理。最终展示前排除回复、转发、引用以及未达到点赞 ≥ 10、转发 ≥ 5、浏览 ≥ 500、作者粉丝 ≥ 100 的帖子；指标缺失按不通过处理。蓝标为加权信号，不作为可信事实证明。
- 账号查询仅由明确标识触发（`@handle`、平台主页 URL、UID）；普通文本仍按话题搜索。X 可调用用户信息和用户最近推文端点；账号动态请求明确使用 `includeReplies=false`，并在应用层二次校验原创类型和最终门槛。相似显示名不得自动当成账号。
- 每个适配器独立报错，扫描器聚合成功结果并记录部分失败。

### 4.2 聚合与数据库

- 继续使用 SQLite，保留 `stories` 作为事件/热点主体。
- 新增关联来源表（建议 `story_sources`），为每个事件保存多条发布记录；避免把多个报道压成一个 `source` 字段。
- 存来源平台、发布者、原始链接、平台 ID、发布时间、来源内容摘要；按平台+外部 ID 或规范化 URL 去重。
- 事件相似度只用于候选聚类；来源独立数按规范化发布者/域名计数，并合并相同原稿的转载链。聚类结果不确定时保留分开的事件，不强行合并。
- 对现有 SQLite 数据通过启动时幂等迁移增加表/字段，不删除用户数据库。

### 4.3 真实性评估

- 先形成有出处的证据包，再请求模型输出受限 JSON：独立真实性 `verdict`/`confidence`、`relevance_score`（0–100）、`urgency`（urgent/high/medium/low）、`summary_zh`、`translation`、`translation_language`、`rationale` 和 `evidence_gaps`。
- 真实性、相关性、紧急度是三个独立维度；列表主分类按紧急度，真实性作为单独标签。
- 模型负责内容一致性、冒名风险、旧闻/夸张风险和证据缺口的辅助分析；模型输出必须校验字段和范围。
- 应用层执行确定性门槛：只有官方来源命中维护的官方清单，或至少两个独立可信发布者支持同一事实，才允许最终进入“已核实”。其他模型结论映射为“待核实/疑似伪造/未核验”。
- 初版交叉来源按代码内的可信媒体域名清单计数（如 Reuters、AP、BBC、财新等）；未列入清单的域名不因数量达到两个就自动成为可信证据。
- 每条结论记录模型判断和应用层规则依据，界面显示证据来源及数量。

### 4.4 通知与界面

- 新热点通过 WebSocket 广播给本机打开的工作台；页面可在用户授权后显示浏览器桌面通知。WebSocket 服务与现有 HTTP 服务分开监听 loopback 端口，不对公网开放。
- 配置 SMTP 后，仅对真实性通过应用层证据门槛且紧急度为 `urgent/high` 的新热点发送邮件；邮件包含紧急度、相关性、真实性结论、摘要和证据链接。Telegram 为可选兼容通道。
- 配置界面增加 TwitterAPI.io Key；AI 默认切换为 OpenRouter Base URL 与 `deepseek/deepseek-v4.1-flash`，保留模型名输入和 Key 掩码/留空保留行为。SMTP 主机、端口、用户名、密码、发件人、收件人和 TLS 模式只在本地保存；API 状态只返回配置布尔值。
- 配置界面增加 TwitterAPI.io Key；AI 默认切换为 OpenRouter Base URL 与 `deepseek/deepseek-v4.1-flash`，保留模型名输入和 Key 掩码/留空保留行为。OpenRouter、TwitterAPI.io、SMTP 和服务端口也可通过被 Git 忽略的 `.env` 配置；系统环境变量优先于 `.env`，两者优先于本地设置。API 状态只返回配置布尔值。
- 凭据已在聊天中暴露：撤销并轮换已发送的 OpenRouter 与 Context7 Key；新凭据只通过本机 `.env` 或本地 UI 配置，不放入 Markdown、命令、日志或 Git。
- 展示每条来源和对应平台标识；更新来源状态文案，展示多来源部分故障，而不是固定写成单一 RSS 来源。
- Skill 说明可信条件及证据链接输出；不要把模型置信度称为真实性概率。

### 4.5 搜索词与账号识别

- `Codex 5.3 发布` 等自然语言词组作为话题检索，不应因出现产品名/人名就误判为账号。
- X `Top` 用于提高热门讨论召回，`Latest` 用于补充新鲜消息；两者分别分页并按帖子 ID 去重。查询端可用的高级搜索排除语法必须以当前 API 文档和真实响应验证为准，不能把未证实的 filter token 当作唯一安全措施。
- 对明确 X 用户标识，使用 `GET /twitter/user/info?userName=...` 获取账号资料，并使用 `GET /twitter/user/last_tweets` 获取原创动态；指定 `includeReplies=false`。两接口均需现有 TwitterAPI.io Key，绝不在前端暴露 Key。
- 页面最终过滤除新抓取候选外，还要约束历史数据返回；不能只依赖抓取适配器，因为旧记录和混合来源聚类可能继续展示不合格 X 来源。

### 4.6 数据源实测与启用门槛

- 启用数据源必须满足：无需用户未确认的 API Key/Cookie；robots/服务条款允许所用方式；无验证挑战或访问控制绕过；实测能返回可解析且非空的结果；故障能单独显示。
- 2026-11-01 一次性无密钥探测：Google News RSS 与 DuckDuckGo 根路径在当前网络超时；Bing RSS HTTP 200 并返回 10 个 RSS 项；Hacker News Algolia HTTP 200 并返回 5 条命中。
- 同日国内入口探测：百度搜索返回“安全验证”，且 robots.txt 禁止 `/s?` 并对通用爬虫禁止访问；搜狗 `/web` 返回验证页，robots.txt 禁止 `/web?` 且通用规则禁止全站；Bilibili 公开搜索 API 返回 HTTP 412；微博移动搜索 API 返回 HTTP 432，robots.txt 对通用爬虫禁止全站。以上不得标记为可用来源，不得添加伪装头、Cookie 或规避逻辑。
- 国内替代 RSS/API 候选尚未完成 robots 和非空条目实测，不能作为已可用来源写入默认启用清单。

## 5. OpenRouter 与 TwitterAPI.io 官方 API 核验结果

### OpenRouter

- MCP 核验日期：2026-10-31。官方 Chat Completions 使用 `POST https://openrouter.ai/api/v1/chat/completions`，请求头为 `Authorization: Bearer <key>` 和 `Content-Type: application/json`；请求体包含 `model` 与 `messages`，响应内容在 `choices[0].message.content`。
- 结构化核验结果可使用 `response_format.type = "json_schema"`，在 `json_schema` 中声明名称、`strict: true` 和 JSON Schema；Schema 应设置 `additionalProperties: false`。仍需在应用层验证字段与取值，且应选择支持 `structured_outputs` 的模型/端点。
- OpenRouter 官方模型目录 API `GET https://openrouter.ai/api/v1/models` 提供模型 ID、上下文、价格和 `supported_parameters` 等字段。实查 `deepseek/deepseek-v4.1-flash`：上下文长度 1,048,576，目录价格为输入 `$0.000000045/token`、输出 `$0.0000005/token`（即 `$0.045/$0.50` 每百万 token），支持 `response_format` 与 `structured_outputs`。
- 初始建议选标准版 `openai/gpt-6.1-terra`，不选 `openai/gpt-6.1-terra-pro`：官方页面称 Pro 使用更高推理模式、单次成本显著增加，并建议日常任务使用标准版。模型页对事实核验质量没有提供独立基准，因此这只是适合试跑的性价比起点；上线前应以固定真假消息样本集评测中文判断、结构化输出成功率和每条成本。
- 官方资料：[Chat Completions/API 概览](https://openrouter.ai/docs/api_reference/overview)、[结构化输出](https://openrouter.ai/docs/guides/features/structured-outputs)、[模型目录 API](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)、[GPT-6.1 Terra](https://openrouter.ai/openai/gpt-6.1-terra)、[GPT-6.1 Terra Pro](https://openrouter.ai/openai/gpt-6.1-terra-pro)。

### TwitterAPI.io

- MCP 核验日期：2026-10-31。高级搜索使用 `GET https://api.twitterapi.io/twitter/tweet/advanced_search`，请求头 `X-API-Key: <key>`。必填查询参数为 `query` 和 `queryType`；`queryType` 支持 `Latest`、`Top`。`query` 支持关键词/账号及时间过滤，官方示例使用 `since_time`、`until_time` Unix 时间戳，并明确不支持旧式 `since:YYYY-MM-DD_HH:mm:ss_UTC` / `until:` 格式。
- 分页参数为 `cursor`，首请求传空字符串；响应包含 `tweets`、`has_next_page`、`next_cursor`。文档示例的推文字段包括 `id`、`url`、`text`、`createdAt`、`author`；作者对象包括 `userName`、`url`、`id`、`name`。
- 官方速率限制按 API Key 每秒计数：从未充值的新账户为每 5 秒 1 次；充值账户及 Starter/Builder/Pro 为 20 req/s，Scale 为 50、Business 为 500；超限返回 HTTP 429。应用侧仍采用低频串行/有间隔请求，并对 429 退避，不能靠接近上限轮询。
- 官方资料：[高级推文搜索](https://docs.twitterapi.io/api-reference/endpoint/tweet_advanced_search)、[速率限制](https://docs.twitterapi.io/rate-limits)。具体计费随账户计划和返回量变化，配置前应在供应商控制台核对现行账单规则。
- Context7 最新核对日期：2026-11-01。`advanced_search` 支持 `queryType=Latest|Top`，每页最多 20 条，使用 `cursor`/`has_next_page`/`next_cursor` 翻页。官方 endpoint 页面只展示通用搜索语法示例，没有完整枚举回复/转发过滤语法；实现不能仅依赖未经验证的过滤 token。
- 账号资料端点为 `GET /twitter/user/info?userName=...`；用户搜索端点为 `GET /twitter/user/search?query=...`；账号最近推文端点为 `GET /twitter/user/last_tweets`，支持 `userId` 或 `userName`、`cursor` 和 `includeReplies`（默认 false），每页最多 20 条。官方资料：[用户资料](https://docs.twitterapi.io/api-reference/endpoint/get_user_by_username)、[用户搜索](https://docs.twitterapi.io/api-reference/endpoint/search_user)、[用户最近推文](https://docs.twitterapi.io/api-reference/endpoint/get_user_last_tweets)。

### MCP 可用性

- Context7 `resolve-library-id` 与 `query-docs` 已成功；Firecrawl 单页 `scrape` 对 OpenRouter 和 TwitterAPI.io 官方文档均返回 HTTP 200。OpenRouter 模型目录 API 本次读取消耗 5 Firecrawl credits；单页页面抓取通常消耗 1 credit。
- Firecrawl `agent` 工具本次被服务端以 HTTP 403 拒绝，原因是该能力在当前地区默认不可用；因此检索使用官方已知 URL 的单页 `scrape` 完成，没有重试 Agent/浏览器会话。

## 6. 开发顺序

1. 需求与方案文档审阅：确认通知渠道、搜索页面范围、账号监控和官方清单维护方式。
2. 用户确认本文与需求文档后开始开发；OpenRouter、TwitterAPI.io 官方接口契约已由 MCP 核验并记录。
3. 实现统一来源记录、SQLite 兼容迁移、独立来源统计和应用层核验门槛。
4. 实现 X 与网页搜索适配器及其低频/限流处理；改进前端来源展示和设置表单。
5. 更新 Agent Skill、README 与测试。
6. 先跑窄测试，再跑完整 `py -3 -m unittest discover -s tests -v`；最后本地启动验证关键页面和接口。

## 7. 风险与缓解

- 搜索结果页结构变化或条款不允许抓取：适配器隔离，失败可见；DuckDuckGo 按 robots.txt 排除 `/html`，Bing RSS 仅作个人非商业聚合，绝不绕过访问限制。
- X API 配额或第三方服务变化：单独显示限流/故障；其他来源继续运行。
- 错误聚类导致假交叉验证：只按独立发布者统计；不确定就不升级可信状态。
- AI 幻觉或把相似报道当证据：应用层确定性门槛控制通知，UI 同时展示原始来源。
- 凭据已在聊天中暴露：撤销并轮换已发送的 OpenRouter 与 Context7 Key；新凭据只通过本地配置，不放入 Markdown、命令、日志或 Git。

## 8. 进入开发的条件

- Leo 审阅并确认 `REQUIREMENTS.md` 与本方案中的待确认事项。
- OpenRouter 和 TwitterAPI.io 的当前官方接口资料已由 MCP 核验；Firecrawl Agent 的地区限制不影响单页抓取工具。
- 已暴露的密钥完成撤销/轮换；实现阶段只通过本地 UI 配置新凭据。

## 9. 用户已确认的实施选择

- 资讯范围：国内中文与国际英文资讯均需覆盖；使用区域化检索，并保留原始语言和来源信息。
- 默认模型：`deepseek/deepseek-v4.1-flash`。此项覆盖上文初始的 GPT-6.1 Terra 建议；开始自动提醒前要通过固定真假样例评测。
- 通知：浏览器打开时 WebSocket 实时推送，另配置 SMTP 邮件；Telegram 仅作为可选兼容通道，Webhook 不纳入当前范围。

## 10. 上一轮实现记录（已由第 11 节新规则覆盖）

- X 仅保留原创帖；点赞 ≥ 50、转发 ≥ 20、浏览 ≥ 2,000、作者粉丝 ≥ 500；缺少任一必需指标时严格过滤。继续使用 `Latest` 查询，并在应用层执行最终筛选。
- 信息源扩展为 Google News RSS、Bing RSS、DuckDuckGo 和 Hacker News；用户确认 Bing 仅在公开入口稳定且合规时接入。DuckDuckGo 禁止路径不请求；Bing RSS 仅用于本地个人非商业展示。
- 只有紧急/高优先级且最终判为 `credible` 的热点发送实时/邮件通知；高互动本身不构成可信证据。

## 11. 2026-11-01 后续优化方案（实施记录）

### X 搜索与最终展示

- X 门槛按新确认值执行：点赞 ≥ 10、转发 ≥ 5、浏览 ≥ 500、作者粉丝 ≥ 100；缺少任一必需互动/作者指标按不通过处理。蓝标只加权，不直接等价于官方或可信。
- 采集使用 `Top` 和 `Latest`，每种排序最多 2 页；先广泛收集候选，再做 URL/帖子去重与时间过滤，之后交给 AI 评估相关性和真实性，最终执行原创类型及互动门槛过滤。
- 不合格帖子不得进入最终展示；对已存在数据库记录也必须在 API/展示层重新校验，避免历史回复/低互动来源继续出现。是否物理删除历史行不在本方案默认范围内，优先保留审计数据并阻止返回。
- 显式 `@handle`、主页 URL 或 UID 触发账号直查；X 资料使用 `/twitter/user/info`，动态使用 `/twitter/user/last_tweets` 并指定 `includeReplies=false`。普通名称作为话题搜索；不做模糊账号自动切换。

### 国内来源策略

- 百度与搜狗搜索结果页因 robots 限制不得爬取；微博通用抓取也被 robots 禁止。当前实测的 Bilibili API 412、微博 432 不可通过换 Cookie、浏览器伪装或反爬绕过解决。
- Google 与 DuckDuckGo 当前网络超时，不作为“国内可用”来源；Bing RSS 和 HN Algolia 当前单次实测可返回，但前者仍受个人非商业展示限制，后者偏国际技术社区。
- 下一轮先完成量子位、InfoQ 等候选公开 RSS/API 的 robots/条款/有效条目测试，仅将实际可用且无需 Key/Cookie 的来源启用；未验证的候选仅作研究项。

### X 实测记录

- 用户授权最多两次只读查询；截至本次文档更新仅执行一次 Top 查询，未输出 Key 或帖子正文：HTTP 200，返回 20 条，`has_next_page=true`；20 条均包含点赞、转发、浏览和粉丝数，18 条达到新阈值，0 条被 API 标记为回复。
- Latest 查询尚未执行；账号端点的实时实测也未执行。开发阶段应在用户允许的请求额度内完成必要的只读验证，并记录实际调用数/错误；如费用或限流风险变化，先停止并报告。

### 实施状态

- 已实现 X 候选采集、Top/Latest 各最多两页游标分页、最终质量门槛、历史数据 API 过滤、显式 X 账号资料/原创动态查询，以及可信来源门槛后的通知控制。
- Bilibili/微博账号 URL 会被识别并明确报告未启用；既有探测发现访问控制或 robots 限制，未尝试绕过。国内替代 RSS/API 仍须先验证 robots、条款和非空解析结果。
- 自动化单元测试覆盖来源解析、X 质量门槛、分页、账号接口、历史记录过滤和通知门槛。AI 相关性最低展示分、待核实内容策略、国内 RSS 实测和固定真假样例评测仍待确认/完成；不隐藏相关但未核实内容。

## 12. 2026-11-01 列表、相关性与翻译扩展

- `/api/state` 支持 `limit` 参数，默认最多 500 条并单独返回数据库总量；前端每批展示 30 条，支持继续加载。
- 工作台按现有来源动态生成来源筛选，提供紧急度、相关性阈值和相关性排序。默认相关性下限为 35；AI 未配置或未核验的故事不因 0 分被隐藏。
- 来源卡片直接展示标题、摘要摘录和时间，前三条默认展示，其余来源折叠；外链仍可用于需要时的原文核对。
- AI 使用两个 few-shot 示例校准“主题不匹配”与“直接相关”，要求相关性、真实性、紧急度分开判断；严格 JSON Schema 新增译文字段。
- 数据库通过幂等迁移新增 `translation` 与 `translation_language`；日/韩文等优先翻译为中文，西/法语翻译为英文。具体判断质量仍需固定样例集评测，不以 few-shot 代替验收。