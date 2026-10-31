# AI 热探 Signal Desk 方案设计

## 1. 设计原则

- 在现有实现上增量演进，不重写已可用的本地工作台。
- 先统一多来源数据，再聚合事件、判断证据、通知和展示。
- 把“AI 判断”与“可验证证据”分开：最终可信状态由明确的来源门槛约束。
- 外部 API 契约以开发当日官方文档为准；未经 MCP 核验的请求参数不进入实现。

## 2. 当前实现基线

- `app.py` 使用 Python 标准库 HTTP 服务、SQLite、Google News RSS、OpenAI-compatible Chat Completions 和 Telegram Bot API。
- `web/` 已有响应式工作台、关键词管理、定时扫描和设置表单。
- `skills/ai-hot-monitor/SKILL.md` 已有本地 API 与手动实时搜索指引。
- 当前扫描只有 Google News RSS；`stories` 仅存单一来源，相关标题数会被用作交叉报道数；该计数不等价于独立来源核验，需要修正。
- 当前模型默认 Ollama，现有 Base URL/模型/API Key 配置可扩展为 OpenRouter；当前 UI 没有 TwitterAPI.io 凭据字段。
- 当前的核验状态使用 `credible/suspicious/uncertain/unverified`，需求侧的“已核实”需与实际来源门槛绑定，不能只由模型返回值决定。

## 3. 目标架构

```text
定时器 / 手动扫描
        |
        +--> Google News RSS 适配器
        +--> 公共网页搜索抓取适配器（低频、遵守站点规则）
        +--> TwitterAPI.io 适配器
                    |
            统一内容模型与来源记录
                    |
         URL/事件去重与独立来源归并
                    |
       官方来源/来源数量规则 + OpenRouter AI
                    |
        SQLite --> Web 工作台 / WebSocket 实时推送 / SMTP 邮件
                    |
              Agent Skill 本地 API
```

## 4. 模块职责

### 4.1 来源适配器

- 定义统一的来源记录结构，至少包含 `platform`、`publisher`、`external_id`、`title`、`summary`、`url`、`published_at`、`matched_keyword`。
- 保留现有 RSS 解析器并将其包装为一个适配器；分别请求中文/中国区域与英文/全球区域的 Google News RSS，结果标记检索区域与原始语言后再去重。
- 新增网页搜索抓取适配器。仅请求无需登录且允许自动访问的公开结果页；设置每域名最小间隔、超时、结果大小上限、`Retry-After`/退避处理和缓存；遇到访问限制时停止该域抓取，不尝试规避。
- 新增 TwitterAPI.io 适配器。中文关键词通过配置模型生成英文检索词并缓存；无 AI 配置时仍用原词检索并在状态中标明国际覆盖受限。只在本机配置凭据；超时和限流独立处理；保留原始帖子语言、ID 与作者信息作为可追溯证据。
- 每个适配器独立报错，扫描器聚合成功结果并记录部分失败。

### 4.2 聚合与数据库

- 继续使用 SQLite，保留 `stories` 作为事件/热点主体。
- 新增关联来源表（建议 `story_sources`），为每个事件保存多条发布记录；避免把多个报道压成一个 `source` 字段。
- 存来源平台、发布者、原始链接、平台 ID、发布时间、来源内容摘要；按平台+外部 ID 或规范化 URL 去重。
- 事件相似度只用于候选聚类；来源独立数按规范化发布者/域名计数，并合并相同原稿的转载链。聚类结果不确定时保留分开的事件，不强行合并。
- 对现有 SQLite 数据通过启动时幂等迁移增加表/字段，不删除用户数据库。

### 4.3 真实性评估

- 先形成有出处的证据包，再请求模型输出受限 JSON：`verdict`、`confidence`、`rationale`、`evidence_gaps`。
- 模型负责内容一致性、冒名风险、旧闻/夸张风险和证据缺口的辅助分析；模型输出必须校验字段和范围。
- 应用层执行确定性门槛：只有官方来源命中维护的官方清单，或至少两个独立可信发布者支持同一事实，才允许最终进入“已核实”。其他模型结论映射为“待核实/疑似伪造/未核验”。
- 初版交叉来源按代码内的可信媒体域名清单计数（如 Reuters、AP、BBC、财新等）；未列入清单的域名不因数量达到两个就自动成为可信证据。
- 每条结论记录模型判断和应用层规则依据，界面显示证据来源及数量。

### 4.4 通知与界面

- 新热点通过 WebSocket 广播给本机打开的工作台；页面可在用户授权后显示浏览器桌面通知。WebSocket 服务与现有 HTTP 服务分开监听 loopback 端口，不对公网开放。
- 配置 SMTP 后，对新“已核实”热点发送邮件；邮件包含证据链接、来源数、发布时间和核验理由。保留现有 Telegram 发送函数作为可选兼容通道，但界面默认提供 SMTP 配置与测试邮件。
- 配置界面增加 TwitterAPI.io Key；AI 默认切换为 OpenRouter Base URL 与 `deepseek/deepseek-v4.1-flash`，保留模型名输入和 Key 掩码/留空保留行为。SMTP 主机、端口、用户名、密码、发件人、收件人和 TLS 模式只在本地保存；API 状态只返回配置布尔值。
- 配置界面增加 TwitterAPI.io Key；AI 默认切换为 OpenRouter Base URL 与 `deepseek/deepseek-v4.1-flash`，保留模型名输入和 Key 掩码/留空保留行为。OpenRouter、TwitterAPI.io、SMTP 和服务端口也可通过被 Git 忽略的 `.env` 配置；系统环境变量优先于 `.env`，两者优先于本地设置。API 状态只返回配置布尔值。
- 凭据已在聊天中暴露：撤销并轮换已发送的 OpenRouter 与 Context7 Key；新凭据只通过本机 `.env` 或本地 UI 配置，不放入 Markdown、命令、日志或 Git。
- 展示每条来源和对应平台标识；更新来源状态文案，展示多来源部分故障，而不是固定写成单一 RSS 来源。
- Skill 说明可信条件及证据链接输出；不要把模型置信度称为真实性概率。

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

- 搜索结果页结构变化或条款不允许抓取：适配器隔离，失败可见；保留 RSS 与 X 来源，绝不绕过反爬。
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