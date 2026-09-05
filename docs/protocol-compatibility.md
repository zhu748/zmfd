# 协议兼容说明

## 目的与数据流

`glm2api` 将 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 的请求先归一化为同一份内部会话，再通过既有的 GLM 上游通道发送。调用客户端获得的仍是它原先选择的协议格式。

```text
客户端协议请求
  -> 归一化消息 / 工具 / 选项
  -> 历史 / 工具转写文件（需要时上传为上游附件；GLM-5.3 长文本自动多文件分片）
  -> GLM 上游流
  -> 文本、thinking、工具调用解析
  -> 对应协议的 JSON 或 SSE
```

完整上下文不会被拼成一条无角色的 prompt：历史文件按参考项目格式保留 `[system]` / `[user]` / `[assistant]` / `[tool]` 标签、顺序和工具调用 ID（工具结果带 `[function=... invocation_id=...]` 头，助手侧工具调用以 DSML 块呈现）。这样工具结果、Responses 链式响应和多轮 Anthropic 消息都能在下一轮继续使用。

## 路径与模型别名

| 客户端协议 | 推荐路径 | 兼容别名 | 流式开关 |
| --- | --- | --- | --- |
| OpenAI Chat Completions | `POST /v1/chat/completions` | `/chat/completions` | `stream: true` |
| OpenAI Responses | `POST /v1/responses` | `/responses` | `stream: true` |
| Responses 读取 | `GET /v1/responses/{id}` | `/responses/{id}` | 无 |
| Anthropic Messages | `POST /anthropic/v1/messages` | `/v1/messages`、`/messages` | `stream: true` |
| Anthropic token 估算 | `POST /anthropic/v1/messages/count_tokens` | `/v1/messages/count_tokens`、`/messages/count_tokens` | 无 |

模型发现返回八条本地路由：`glm-5.3`、`x-preview-l`、`GLM-5-Turbo`、`glm-5.2` 及各自的 `-forcehistory` 变体。常见 `gpt-*`、`codex-*`、`o*` 和 `claude-*` 仍可作为输入别名，默认映射到 `glm-5.3`；`glm-5.3-flash` / `glm5.3flash` / `flash` 映射到 `x-preview-l`，旧 `glm-5.1` 映射到 `glm-5.2`。响应中的 `model` 保留原始请求名，避免影响 SDK 的模型分支逻辑。

服务按本地账号池调度上游：每个 profile 最多 3 个生成，新请求优先使用默认账号，满载后按 `routing_order` 顺延到下一账号。账号池最多保留 64 个 profile；同 token/user_id 的重新导入原位更新，新增达到上限时返回 409 `profile_capacity_reached`。登录态进入内存/DPAPI 存储前统一限制 token 16 KiB、captcha 参数 64 KiB、其余字段 4 KiB。`Authorization`、`x-api-key` 和 `anthropic-version` 会被接受，以兼容常见 SDK；它们不是本地账号登录方式，也不会覆盖账号池选择。

创建 chat 或流首阶段若收到实测瞬时错误（模型繁忙/限流、HTTP 429/500/502/503/504、“上游中断”、验证码失效），代理会在尚未输出任何回答时清理空会话并按设置重试。首个正文或 thinking delta 出现后不再重放，避免客户端看到重复内容或重复工具调用；鉴权和内容审核错误同样不参与重试。

工具输出转换采用原子严格模式：同一显式工具块中出现未声明/被策略禁止的名称、不可转换的 arguments、缺失 schema 必填字段或模板占位符时，整批拒绝并进入一次有界格式纠错，不会静默执行其中一部分。确定纠错后先释放首轮完整字符串和流式分片列表，再开始替换流，防止两个最多 16 MiB 的合法输出同时驻留；正常解析完成后也立即清空原始分片。请求最多声明 128 个工具且定义总量不超过 1 MiB；单轮最多转换 64 个调用，每个 arguments 不超过 256 KiB。

所有 JSON 与 SSE 错误在离开代理前会统一移除常见凭据、Authorization/Cookie、带查询参数 URL、本机绝对路径和控制字符，并限制为 1200 字符；协议的状态码、错误类型、错误代码和可操作的上游说明仍保留。净化只应用于错误负载，模型成功输出中的路径、代码和文本不会被改写。日志使用独立但等价的最终 formatter 边界，尚未初始化日志时不会回退到 Python 的原始 stderr handler。

每个生成响应都会在 `X-GLM2API-Profile-ID` 响应头返回实际接管请求的本地 profile。需要复用上游 `chat_id` 或已上传文件时，客户端应在后续请求中原样发送该请求头；固定 profile 满载会返回 429 `chat_slot_busy` 且 `scope: "profile"`，不会静默切换账号。没有固定 profile 时，仅所有账号均满载才返回 `scope: "pool"`。未知或已删除的 profile 返回 `profile_not_found`，不会与并发满载混淆。配置 CORS 后，该响应头和 `Retry-After` 都会通过 `Access-Control-Expose-Headers` 暴露。

生成槽位与账号生命周期绑定。仍有生成请求的 profile 不允许删除（409 `profile_busy`）；重复登录态整理同样会保护忙碌 profile，并以 `skipped_busy_count` 返回被保护数量。这样流式请求结束后的停止、文件清理和会话删除仍能找到原登录态。

账号管理列表遵循最小披露：profile 条目只保留本地路由 ID、标签、安全来源展示、用户名/不可逆指纹、重复状态和并发字段。原始 HAR 来源路径、profile 中残留的上游 chat ID、设备/验证码/HAR 指纹以及重复的模型列表不会进入响应；需要继续某次生成时仍以生成响应中的 `X-GLM2API-Profile-ID` 与 `chat_id` 为准。

本地 API Key 可通过面板“API 调试”页启用/更新/清除（DPAPI 加密保存到 `apikey.local.json`），也可用 `GLM2API_API_KEY` 或 `--api-key` 在启动时配置（后者优先级更高）。四条配置/读取路径统一限制为 4096 字符并拒绝嵌入控制字符，鉴权先生成固定长度摘要再执行恒定时间比较。启用后，所有 API 入口（除 `/healthz` 和最小化 `/api/status` 外）都会校验本地密钥，可传 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`。

默认设置和面板 API Key 各自通过独立状态锁串行提交，校验、原子文件替换和运行时状态发布不会被并发请求交错。存储失败不更新运行时值，返回 HTTP 500 及稳定错误码 `settings_store_write_failed` / `api_key_store_write_failed`；`GET /api/settings`、`GET /api/settings/api-key` 和授权后的 `/api/status.settings_store` 提供 `persisted/error` 状态，错误日志进入 `settings_store_write_error` / `api_key_store_write_error` 前先脱敏。

网页对话抽屉的 localStorage 镜像使用最新优先的有界规范化：全局 120 个会话、每账号 30 个、每会话最近 40 条消息、每段正文/思考 20,000 字符、附件 32 个、输入扫描 1000 项、序列化结果约 150 万字符。会话更新后移到首位；附件仅保存可序列化元数据；重复/空 ID、非法 chat UUID、非数组根或消息列表会被丢弃/归一。浏览器配额不足时降级保存最近 8 个会话的精简副本。

本地状态文件在 JSON 解析前使用真实读取字节预算而非只相信 `stat`：设置 64 KiB、删除补偿 journal 512 KiB、DPAPI 账号库外层 24 MiB/解密载荷 16 MiB、历史索引 8 MiB、单条历史详情 16 MiB。历史目录每轮最多扫描 4096 个详情文件；账号恢复重新执行登录态字段、64 个账号、profile ID 格式及重复 ID 校验，历史恢复拒绝路径型 ID 和索引/详情 ID 不一致。各上限通过 `/api/status` 的 `limits`、`profile_store`、`history_store` 与 `auto_delete` 脱敏公开。

历史删除与清空接口不会把内存移除等同于持久化完成。`POST /api/history/record/delete` 和 `/api/history/clear` 返回 `persisted`；索引或 detail 操作失败时仍保持当前进程中的删除结果，但返回脱敏 `history_store_error`，并在 `/api/status.history_store` 暴露 `pending_writes`、`pending_deletes`、`error/error_at`。dirty/deleted 标记只在 detail 与索引提交成功后清除，下一次历史变更会自动重试；恢复后 `persisted` 回到 true。记录不存在统一返回 404 `history_record_not_found`，非法 ID 仍为 400。

账号变更接口区分“当前进程已生效”和“DPAPI 已持久化”：新增、切换、删除、整理及兼容验证码更新均返回顶层 `persisted`。写入失败时 HTTP 操作仍成功并保留可用的内存状态，但 `persisted: false`、`profile_store_error` 和 `profile_store.persisted/error` 会明确提示重启风险；错误中的凭据、URL 查询和本机路径在响应及 `profile_store_write_error` 日志进入前统一脱敏。未发生实际整理写入时 `persisted: true`，不会把历史存储故障误算成本次空操作失败。

HTTP 管理查询统一经有界解析：最多 32 个字段，字段名最多 128 字符，解码后单值最多 4096 字符；本地/上游历史页码限制为 1–1000，历史关键字限制为 256 字符。查询超限返回 400；若上传端点尚未读取请求体便发现非法查询，响应显式关闭连接，防止残留正文污染下一条 keep-alive 请求。

流式上游响应除单个未分帧事件 2 MiB 缓冲外，还限制整次原始 SSE 为 32 MiB、正文与思考合计为 16 MiB、事件数为 100,000。核心读取器和三个协议流式适配器分别计量，嵌入方替换核心生成器也不能绕过；超限使用 `UpstreamResponseTooLarge`，非流式映射为 502，已开始的 SSE 返回对应协议错误并停止，随后关闭生成器、释放账号槽位和清理上游 chat。上游只有出现抓包实证的 `data.done=true` 或 `[DONE]` 才算完整结束；提前 EOF 使用 `UpstreamStreamIncomplete`，零输出时可按既有 attempt 上限换 chat 重试，部分输出时禁止重放并强制把 chat 写入删除 journal，历史记录为 error。`runtime.upstream_responses` 分别公开 wire/output/event 拒绝及 `stream_incomplete_total` 计数，不包含响应内容。

`POST /api/auth/browser-login` 与兼容保留的 `/api/auth/captcha-refresh` 共用原子独占流程。并发请求不会同时拉起多个浏览器；未取得流程锁时返回 409 `auth_flow_busy`，响应中的 `flow` 只包含当前模式、阶段和更新时间。旧验证码端点仅在启用 fresh-captcha 且求解器为 `auto` 或 `browser` 时开放；纯 `happydom` 模式返回 409 `legacy_browser_captcha_disabled`，不会加载 Playwright 或占用浏览器流程锁。`GET /api/auth/browser-login/status` 从线程安全快照读取进度。浏览器启动最多等待 10 秒；首次授权页导航按 5 秒切片重试（总预算 60 秒），每个切片之间检查服务关闭信号；页面内登录态探测的网络请求最多等待 5 秒，避免异常网络使登录 handler 无期限阻塞。

本地可观测性端点同样受 API Key 保护：`GET /api/metrics?hours=24` 只返回请求状态、耗时、Token、模型/入口与运行态聚合值，不包含提示词/回复正文；运行期路径 Counter 最多保留 256 个、每个 300 字符，溢出路由聚合到 `/:other` 并由 `path_overflow_total` 计数。`runtime.captcha_worker` 只报告 Playwright 回退 worker 是否启用、线程/活跃状态、当前积压、上限和累计回压，`runtime.auto_delete` 报告执行队列、取消/回压计数，以及不含资源/account 标识的 chat/file 持久化补偿 journal 条数、replay feeder 活跃状态和待提交数，`runtime.context_cache` 仅报告附件缓存条数/估算字节和上传失败/降级状态数，`runtime.upload_slots` 仅报告附件/HAR 上传槽位的活跃、峰值和累计回压，`runtime.upstream_responses` 仅报告非流式上游响应拒绝/错误截断计数与公开大小预算。浏览器验证码并发任务使用独立结果通道，反序完成不会串扰；队列最多保留 8 项，超时/断连任务会被跳过，worker 启动失败立即通知等待者。异步 chat 删除、失败请求的用户附件和部分上传成功的内部上下文分片会先批量原子登记到资源级 v2 journal，单次网络等待 10 秒；成功移除，失败、队列满载或退出取消后保留，并在下次启动按不可逆账号指纹匹配已加载 profile。独立 feeder 随执行队列释放逐项补料，因此 256 条 journal 不再只处理首批 64 条；关闭时 feeder 先退出，未提交项保持持久化。旧 chat-only v1 journal 可兼容读取并自动升级。退出等待执行队列最多 10 秒，不再受 60 秒请求、串行多文件清理和全部积压任务无上限拖住。附件复用缓存采用 10 分钟 TTL、512 条精确 LRU，失败/降级账号状态也限制为 512 个，查询/写入/状态读取会全局清扫过期项。统计页可直接导出同结构的脱敏 JSON。`GET /api/logs` 默认同时返回兼容文本行和结构化事件，可按 `level`、`kind`、`state`、`rid`、`text` 组合过滤；每条日志最多 16,384 字符，截断累计值由 `stats.truncated_total` 公开，事件元数据不会因正文截断而丢失；`format=structured&after_seq=<seq>` 支持无重复文本数组的游标增量读取，游标失效时以 `cursor.reset_required` 指示并返回当前快照。日志最终 formatter 除凭据外还统一删除 URL 查询参数和 Windows/Unix 用户绝对路径，traceback 仍保留源码行号。面板后台状态/账号、统计和概览日志请求分别有 10/8/4 秒超时，30 秒状态轮询不会重入。

网页网络请求按语义分档而不是无限等待：短管理请求 10 秒、上游停止/删除控制 15 秒、统计/完整日志 8 秒、浏览器登录 330 秒、HAR 与普通附件上传 10 分钟；长连接聊天 SSE 继续由用户取消和服务端心跳负责。超时封装会把调用方 signal 转发到内部 controller，附件上传与当前生成共享取消信号，因此尚未创建 chat 时停止也能中止上传；调用方取消保持原生 `AbortError`，计时器触发才转换为超时消息。浏览器登录进度轮询采用 single-flight，并在登录终态后忽略晚到响应。

网页 Markdown 渲染先转义全部模型/历史文本，再生成固定结构的标题、引用、列表、代码与仅限 `http/https` 的链接；账号标签、文件元数据和 localStorage 会话时间等其他动态字段同样不会裸插入 HTML。围栏代码使用与原文避碰的内部占位符，正文中的 marker-like 字符串不会改变代码块位置或数量。发布前端检查会执行真实 renderer 验证原始 HTML 不产生元素，并覆盖占位符碰撞。

孤儿附件清理端点与内部失败路径共享资源级 journal 和有界后台执行器。`POST /api/files/cleanup` 先严格规范化最多 64 个字符串 file ID，再原子登记并立即返回：至少一项接纳时为 202，`accepted_count` / `journal_capacity_dropped` 区分已登记与容量淘汰，`scheduled:false` 只表示当前执行队列满载，已登记项不会因此回到请求线程阻塞；`journal_persisted` 显示登记是否成功落盘。后台删除失败会累加 attempts 并留待重启 replay，未知 profile 返回 404，内部登记故障返回 500 `file_cleanup_enqueue_failed`。

服务默认只绑定回环地址。绑定 `0.0.0.0` 或其他非回环地址时，必须同时传 `--allow-remote` 并配置 API Key，否则服务拒绝启动。Windows 使用独占端口绑定，重复启动不会与旧实例共享同一端口；端口冲突以退出码 2 返回明确提示。

请求体同时支持固定 `Content-Length` 和 HTTP/1.1 `Transfer-Encoding: chunked`。chunked JSON 在受限聚合后进入同一协议归一化流程，HAR/附件则逐块写临时文件；正文总量仍分别受现有 JSON、HAR、附件上限约束。附件上传最多 4 个并行任务，HAR 导入最多 1 个；超出时在读取正文前返回 429 `upload_capacity_busy`、`Retry-After: 2` 并关闭连接。原始 HAR 流式上限为 512 MiB，旧版 JSON 包装 HAR 因内存解析限制为 64 MiB。HAR 状态提取在独立 worker 中执行，最长 120 秒；服务关闭会主动终止 worker 并回收临时文件。服务拒绝 `Content-Length + Transfer-Encoding` 双 framing、重复 Content-Length、非 chunked transfer coding、过长 chunk size/trailer 和畸形分隔符并返回 400；正文总量超限返回 413 `request_too_large` 并关闭未消费正文的连接，避免不同解析器对请求边界产生歧义。本地请求正文空闲超时为 10 秒。

非流式上游响应也有独立内存预算：JSON 32 MiB、错误正文 64 KiB、附件上传响应 1 MiB。超过预算时关闭响应并记录脱敏事件；上游附件上传故障返回 502 `upstream_upload_error`。流式 completion 继续使用逐行 SSE 解析和 2 MiB 单事件缓冲上限，不受非流式 JSON 总量预算影响。

服务关闭采用两阶段排空：设置 shutdown event 后等待 handler 15 秒，必要时中断活跃客户端 socket 并再等待 5 秒；上游流、繁忙退避、happy-dom 求解和附件分片都会检查取消。HTTP handler 达到 128 个上限时，接入层不再阻塞 accept 主循环，而是立即返回 503 `handler_capacity_exhausted`、`Retry-After: 1` 并关闭连接；状态/统计中的 `http_handlers.rejected_total` 记录累计拒绝数。中断记录使用 `service_shutdown` 原因并安排删除上游 chat。happy-dom Node helper 与后台预热线程先取消，handler 收束后才关闭自动删除执行器，已接收的删除任务不取消。

## 上下文文件策略

| 入口 | 默认行为 |
| --- | --- |
| Chat Completions | 直接发送归一化后的上下文，不自动上传历史文件 |
| Responses | 直接发送归一化后的上下文，不自动上传历史文件 |
| Anthropic Messages | 直接发送归一化后的上下文，不自动上传历史文件 |

可以通过 `context_as_file`、`current_input_file`、`history_as_file` 或 `forcehistory` 明确开关；模型名含 `-forcehistory` 时总是封装。该后缀可与 `-nothinking` 叠加，且只会在模型 ID 结尾生效。2026-08-30 使用现有上游账号做位置标记实测：GLM-5.3 对单个 48 KiB 文本能看到末尾，50/52/256 KiB 均停在约 49 KiB；GLM-5.2 对 256 KiB 可看到最终标记。因此 `glm-5.3` 的历史和工具转写按不超过 40 KiB/片上传，每片带 `segment X/Y` 内容头，输入框要求按该编号（而非随机文件名）读取全部分片；160 KiB 五片复测能看到第 160 KiB 和最终标记。其他模型保留单文件策略，单个生成文件的上传保护上限为 4 MiB。历史与工具定义使用随机短数字文件名（`111.txt` 风格）；同账号同分片内容 10 分钟内复用已上传的 file id。生成文件只在本地临时目录存活到上游上传结束，随后立即删除；任一分片失败时整包回退直传，不会发送半包。

2026-09-05 的官网 HAR 进一步确认单条 completion 最多携带 10 个附件，也确认了可复用的续传状态机：首条消息进入 thinking 后以顶层 assistant `id` 调用 `/api/tasks/stop/{id}`，下一条 completion 复用同一 `chat_id`，并把被停止的 assistant `id` 填入 `current_user_message_parent_id`。因此 GLM-5.3 生成分片与用户附件总数超过 10 时，代理把前部历史/工具分片按每波最多 10 个预载；每波在发送前才即时上传，输入框只要求读取、思考并等待续传，收到第一个 thinking delta 后立刻停止。最终波在上一波停止成功后才上传剩余生成分片、保留用户附件并发送带接续说明的真实执行提示。后续波次若在零语义输出时收到 `MODEL_CONCURRENCY_LIMIT`，保持同一 chat/parent，换新 user/assistant message ID，并重新上传当前波附件后按全局繁忙 attempt 上限重试；只有本请求实际新建且被替代的旧附件才会进入删除 journal 并从上下文附件缓存失效，缓存命中的共享 file ID 不会被并发请求误删，首波繁忙则沿用原有“回收旧 chat、创建新 chat”策略。预载 chat 始终是临时资源，最终响应结束或任一阶段失败/中断后都会强制写入删除 journal，不受普通保留会话开关影响。若调用方要求复用现有 chat，完整历史转写允许代理降级为新的临时 chat，避免在未知 current parent 上追加预载波次。历史镜像在预载开始前建立，记录 `context_preload_waves` / `context_preload_files`，预载或最终上传失败也保留 error 记录；上下文附件清单最多保留 64 个分片，覆盖 2 MiB 请求预算，聚合指标提供 staged request、预载波次和文件计数。不能使用该 GLM-5.3 分批协议的路径如果附件总数超过 10，会在创建上游 generation 前以 400 拒绝。

输入中的图像和标准 `input_file` 内容块会写入可读说明，因此文本历史不会丢失；代理不会替客户端从 OpenAI/Anthropic 文件服务下载二进制数据。若要上传真正的上游附件，请先调用既有的 `POST /api/files/upload`，然后在协议请求的 `files` 或 `attachments` 中传入返回的 Z.ai 文件对象。

## 函数工具

### 接受的工具定义

OpenAI Chat Completions：

```json
{
  "type": "function",
  "function": {
    "name": "get_weather",
    "description": "查询天气",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}
  }
}
```

OpenAI Responses：

```json
{
  "type": "function",
  "name": "get_weather",
  "description": "查询天气",
  "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}
}
```

Anthropic Messages：

```json
{
  "name": "get_weather",
  "description": "查询天气",
  "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}
}
```

`inputSchema` 与 `schema` 也会作为参数 schema 接受。模型把对象、数组、数字或布尔值填入一个明确声明为 `string` 的字段时，适配层会将该字段转为紧凑 JSON 字符串；其他 schema 类型不会被改写。

工具名称必须是字母、数字、下划线或连字符组成的合法标识符。重复名称、`tool_choice` 指向未声明工具，或在没有函数定义时要求 `required`，都会返回 400。

### 工具选择与执行边界

- `tool_choice` 支持 `auto`、`none`、`required` / `any`、指定函数，以及 Responses 风格的 `{"type":"allowed_tools","mode":"auto|required","tools":[...]}`。`allowed_tools` 会同时收窄提示词、工具附件和结果解析，空集合或未声明名称直接返回 400，不会静默扩大权限。
- OpenAI 的 `parallel_tool_calls: false` 与 Anthropic 的 `disable_parallel_tool_use: true` 都会限制为至多一个调用；模型若仍返回多个调用，严格输出路径会整批拒绝并进入一次纠错，而不是截取第一个造成部分执行。
- 指定函数（forced）始终只返回该函数的一个调用，即使模型重复输出调用块。
- OpenAI/Responses 中 `web_search*` 类型的内置工具会打开 GLM 的联网搜索；其余工具全部是客户端函数。
- glm2api 只解析和返回调用请求，绝不会执行工具内容。这包括命令、文件、网络、数据库和 MCP 风格工具。

工具调用提示词与参考项目 dkceshi 对齐（英文 7 条规则 + 5 反例 + 动态正确示例），并按“工具 schema / 调用规则 → 对话历史”的 System→User 语义顺序放入上游输入框。completion guard 保留模型对 `auto` 的自主判断，同时要求：若任务尚未完成且工具能推进工作，就在当前响应调用，不能只输出未来计划；任务真正完成时正常给出无工具最终答复，不需要为了守卫制造调用。内部推荐模型在需要调用时输出半角管道 DSML XML 外壳：`<|DSML|tool_calls> → <|DSML|invoke> → <|DSML|parameter>`。解析同时兼容标准 XML、早期 glm2api JSON 外壳、DeepSeek 分隔符/本地名/全角和 CJK 标签漂移、缺失但可安全补齐的外层结束标签，以及实测 Claude 风格 `<tool_call>…<arg_key>/<arg_value>…`、`Tool: … <tool_input>`、`**Calling:**`、`<function_call>`；XML 子节点可还原对象/数组，参数可修复 HTML 实体、JSON 尾逗号、未加引号的键和无效路径反斜杠。反引号或波浪线代码围栏（包括未闭合围栏和正文含反引号）内的示例不会触发解析或被清洗；未声明工具、强制工具之外的名称不会进入调用结果。工具标记会在转协议前移除；普通文本不会看到它，流式工具请求会等待完整语义块解析后再作为原生工具块发送。

生成的工具调用 ID 按协议使用原生前缀和标准长度：OpenAI Chat Completions 为 `call_` + 24 位十六进制，Responses 为 `fc_`，Anthropic 为 `toolu_`，避免严格 SDK 校验拒绝。截断或畸形的工具块在移除时会解开 CDATA 并删除残留标签，只保留非调用正文，不会向客户端泄漏适配器外壳。`auto` 以模型的可见最终通道为唯一调用决策：存在工具标记才转换，不存在就按无工具回答结束；代理不会用关键词猜测任务状态，也不会执行 thinking 中的候选调用。若可见输出明显尝试了工具块但无法转换，或未满足 `required` / forced，代理会清理首轮上游会话、附加一条精确纠错提示并只重新采样一次；纠错不会把 `auto` 强制改成 `required`。`required` / `any` 模式下若调用出现在思考流中，即使正文有文字也会回退解析。请求镜像会额外记录最终 `finish_reason`、工具数量/名称、解析来源和格式纠错次数（不保存工具参数）；纠错耗尽会把镜像从上游流暂记的 success 改成 error，`/api/metrics` 的 `history.tools` 汇总工具回合、调用数、纠错成功率和 thinking 恢复次数。

### 客户端工具循环

当客户端收到工具调用后，应自行执行受信任的本地函数，再发送下一轮结果：

| 协议 | 本轮工具调用 | 下一轮工具结果 |
| --- | --- | --- |
| Chat Completions | `choices[0].message.tool_calls` | `{"role":"tool","tool_call_id":"...","content":"..."}` |
| Responses | `output` 内 `function_call` | `{"type":"function_call_output","call_id":"...","output":"..."}` |
| Anthropic | `content` 内 `tool_use` | user content 内 `{"type":"tool_result","tool_use_id":"...","content":"..."}` |

旧版 OpenAI `assistant.function_call` 与后续 `role:"function"` 同样兼容；旧结果没有 `tool_call_id` 时，适配器会按顺序关联最近同名函数调用，避免工具结果在历史转写中被丢弃。

工具结果会被写入下一次上下文包。不要把不可信工具输出直接当作 system/developer 指令；它会作为“工具结果”角色保留。

## OpenAI Responses 链式上下文

`store` 默认为 `true`。完成的 Response 会连同归一化后的历史保存在进程内缓存中，最多 128 条、有效期 1 小时。下一轮可使用：

```json
{
  "model": "gpt-5",
  "previous_response_id": "resp_xxx",
  "input": "继续回答，并结合前面的工具结果。"
}
```

缓存只服务于本地兼容；服务重启、条目过期、使用 `store: false`，或 ID 不存在时，`previous_response_id` 会返回 400。它不等同于 OpenAI 的持久化 Response 存储。

## 调用完成自动删除

`delete_chat_after_completion` 默认是 `true`，`delete_after_completion` 与 `auto_delete` 是等价别名。一次成功 completion 结束后，代理会删除对应的 Z.ai chat；即使这样，本地 Responses 缓存依然可用于 `previous_response_id`。

请求中传入 `false` 即保留正常完成的会话。客户端明确停止或断开流时，会独立异步回收已经创建的中断会话，不受该开关影响；上游流错误和工具格式重试仍遵循开关。网页 stop 控制请求使用独立 10 秒上限，接受官网实测空对象/空正文、显式成功字段和任务已结束的 404。若 stop 失败但请求已取得 `chat_id`，取消端点仍先把删除意图持久化并返回 202 `ok:true`、`upstream_stopped:false`，本地流立即断开；只有尚无 chat 可回收时才向客户端保留 502/504。清理失败不会撤销已经生成的协议响应：网页接口会携带失败说明，协议接口会输出只含 chat ID 指纹的本地诊断日志。

## 流式事件

Chat Completions 返回标准 `data: {...}` 片段并以 `data: [DONE]` 结束。Responses 会发送 `response.created`、`response.in_progress`、输出项/文本/函数参数事件以及 `response.completed`；开启 `include_thinking` 且上游思考可用时，还会以标准 `reasoning` 输出项（索引 0）发送 `response.output_item.added`、`response.content_part.added`、`response.reasoning_summary_text.delta/.done`、`response.content_part.done`、`response.output_item.done`，思考摘要与 completed 对象中的 `reasoning.summary` 一致。Anthropic 会发送 `message_start`、`content_block_start`、`content_block_delta`、`content_block_stop`、`message_delta` 和 `message_stop`。

带工具或 Anthropic thinking 的响应会在语义块完整后再写入 SSE，以确保调用端不会收到半截 JSON、内部工具标记、不完整 thinking 块，或格式重试前一轮的 reasoning。协议请求验证并取得账号槽位后会先发响应头，请求级 `sse-heartbeat` 线程随后覆盖历史附件准备、创建 chat、验证码求解、等待上游响应头和读取响应体的整个阶段；距离上次下游写入达到 10 秒时，向四个流式入口发送标准 SSE 注释 `: keep-alive`。所有正文、协议事件、`[DONE]` 与心跳共用单个写锁，帧不会交叉拼接。响应体本身另由容量为 8 的有界队列读取，以便静默时让主线程检查断连并关闭上游响应。心跳检测到客户端断开后，准备/建会话/验证码/连接各阶段会在最近的安全边界停止继续请求；已取得但尚未消费的响应会主动关闭。注释不进入正文、thinking、工具解析或历史镜像；断开时会回收心跳和读取线程。无工具的普通文本继续边生成边转发。上游 SSE 分帧同时接受 LF 与 CRLF，并支持标准 `event:`/多 `data:` 字段。

## 思考、计量与限制

- `reasoning.effort`（Responses）会映射到 GLM 思考档位；Anthropic `thinking: {"type":"enabled"}` 会请求 thinking 内容。
- 模型名附加 `-nothinking` 可强制关闭思考。
- Anthropic thinking 块中的 `signature` 固定为空字符串，明确表示该内容不是 Anthropic 签名结果。
- Responses 的 completed 对象在开启 `include_thinking` 时会把 GLM 思考内容放入 `reasoning.summary[].text`，同时输出列表会包含对应的 `reasoning` 项，便于 SDK 直接读取；原生 OpenAI 在该字段只放摘要，本项目为保真放置完整思考文本。
- `usage` 和 `count_tokens` 是本地估算值，不可用于计费核对。`max_tokens` 只进行正数校验，当前上游没有严格等价的长度上限字段。
- 不支持从第三方 Files API 拉取文件，也不模拟 provider 侧的批处理、异步任务、加密签名或持久化响应存储。

## 本地验证

```powershell
Set-Location D:\open-reverselab\glm2api
python -m py_compile .\glm2api.py
python -m unittest discover -s .\tests -p "test_*.py" -v
```

测试使用伪造登录态和模拟上游 SSE，不会使用或输出实际 token，也不会访问真实上游账号。
