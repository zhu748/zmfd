# glm2api

本目录是可独立启动的本地 Web 版 GLM 反代工具。公开模型路由固定为八项：`glm-5.3`、`x-preview-l`（即 GLM-5.3-Flash）、`GLM-5-Turbo`（注意大小写，上游 ID 敏感）、`glm-5.2`，以及各自的 `-forcehistory` 变体。GLM-5.1 已从官网下架（上游 500），旧 ID 自动重映射到 `glm-5.2`。

## 一键启动

双击：

```text
一键启动.cmd
```

或在 PowerShell 中运行：

```powershell
.\start_glm2api.ps1
```

启动后访问：

```text
http://127.0.0.1:8008/
```

一键脚本会先检查 `8008`：只有 `/healthz` 明确返回 `service: "glm2api"` 才会打开已有页面，普通的 `{"ok":true}` 不再被误认；若被其他程序占用，会显示对应 PID。服务端在 Windows 还会使用独占端口绑定，直接重复运行命令也不能再出现多个新旧进程共同监听、请求随机命中旧代码的情况；冲突会给出简短的端口占用提示并以退出码 2 结束。

## 登录方式

支持无账号启动。没有 HAR 也能打开网页，在左侧导航进入「账号」页操作：

1. 点击「拉起官方浏览器登录」。
2. 在弹出的官方 `chat.z.ai` Chrome 窗口中手动登录并完成验证码。
3. 登录成功后，本地服务会读取登录后的 token/user/device 摘要，保存 profile 并切换。

登录后的消息验证码按服务端模式自动处理（fresh-captcha 默认优先本地求解器），账号页不再需要手动采集；浏览器只用于需要时的登录态获取。

Playwright 现在是可选的浏览器登录/验证码回退组件：一键启动检测到 happy-dom 可用时不会为了它额外安装 Playwright。未安装时面板会禁用“浏览器登录”并提示改用 Token/HAR；如确实需要浏览器登录，可运行 `python -m pip install -r requirements.txt`。

浏览器登录与旧版浏览器验证码流程在服务端共用独占流程锁：请求体完整校验后只有一个请求能进入浏览器工作器，其余请求返回 409 `auth_flow_busy` 并附带当前模式/阶段。进度读写使用独立锁，非持有者不会清空正在运行流程的状态。

也支持上传 HAR：

1. 选择 `.har` 文件。
2. 填备注名。
3. 点击「上传 HAR 并提取登录态」。

也支持直接粘贴 token（无需 HAR / 浏览器窗口）：

1. 从浏览器 DevTools（`localStorage.token`）、其他登录流程或抓包中获得会话 token（三段式 JWT）。
2. 粘贴进“或粘贴会话 token”输入框（可填备注名）。
3. 点击“使用 token 添加账号”，服务解析 payload 中的用户 id 并合成设备指纹，保存 profile 并切换。

如果存在 `chat.z.ai.har`，一键脚本会自动预加载：

- 优先使用 `glm2api\chat.z.ai.har`
- 找不到时使用上一级目录的 `chat.z.ai.har`
- 都找不到也会正常启动为无账号模式

## 源码布局

为避免把结构、样式、交互和全部回归用例堆在单文件中，Web 控制台和协议测试按职责拆分：

- `web/index.html`：只保留页面语义结构；
- `web/styles.css`：控制台主题和组件样式；
- `web/core.js`：共享状态、本地会话、Markdown 渲染和页面路由；
- `web/history.js`：请求镜像列表、详情和导出；
- `web/chat.js`：聊天会话、附件上传、SSE 和停止/删除操作；
- `web/admin.js`：统计、日志、设置、API Key 和账号管理；
- `tests/test_protocol_adapters.py`：`unittest` 可发现的聚合入口；
- `tests/protocol_cases/support.py`：共享服务 fixture 与辅助函数；
- `tests/protocol_cases/*_cases.py`：按工具与上下文、流式与验证码、历史与清理、服务运行时、Web 与兼容性分组的用例。

前端仍是无构建步骤的原生 HTML/CSS/JavaScript。服务端只暴露上述静态资源的固定 allowlist，`/assets/` 不会映射任意磁盘路径。

拆分依据、后端剩余热点及后续提取顺序见 [维护布局说明](docs/maintenance-layout.md)。

## 面板结构

Web UI 是控制台式布局：左侧导航 + 顶栏状态，共八页——「概览」（请求信号轨/状态磁贴/快捷操作/最近日志）、「统计」（请求趋势、Token 构成、模型与入口分布、运行态）、「对话」（可收起会话抽屉 + 紧凑控制条 + 输入框）、「历史」（请求级镜像记录：思维链/流式输出/最终提示词）、「日志」（结构化实时事件流）、「账号」（登录态管理）、「设置」（默认行为与上游参数）、「接口」（API Key/端点/示例）。

浏览器本地会话按最新活动顺序保存：每个账号最多 30 个、全局最多 120 个，每个会话保留最近 40 条消息，正文/思考单条各 20,000 字符，最多扫描 1000 个旧存储条目，最终 JSON 不超过约 150 万字符。保存时会把 `File` 转成文件名、大小、类型和修改时间，不再因浏览器原生对象序列化而丢失附件名；损坏或非数组的 localStorage 会安全恢复为空列表。存储配额不足时再降级为最近 8 个会话、每个 12 条/4000 字符的精简副本，并只提示一次。已有会话收到新消息时会移动到列表首位，避免旧的 `slice(-30)` 逻辑误留最旧会话。

## 统计与可观测性

- 「概览」展示轻量请求信号轨，可切换最近 1 小时 / 24 小时 / 7 天，并显示请求量、成功率、P95、估算 Token 和进程运行时长。
- 独立「统计」页基于已保留的请求镜像聚合成功/错误/停止、耗时分位数、历史拆分占比、Token 构成、模型排行和调用入口；请求镜像还提供工具回合数、原生调用数、格式纠错请求/成功率与 thinking 恢复次数，且不会把提示词、回复、工具参数、账号 ID 或错误正文带入统计响应。
- 统计页可把当前时间窗口导出为带版本标识的 JSON；导出内容与 `/api/metrics` 一致，仅含脱敏聚合值，便于留档和离线比较。
- 运行态计数器只统计真实业务/管理请求，忽略 `/api/status`、`/api/logs`、`/api/metrics` 等面板轮询，提供本次进程 HTTP 总量、4xx/5xx（单列 408 请求体超时与 413 正文超限）、近 5 分钟请求数、真实 handler P50/P95 和当前并发。路径计数最多保留 256 个槽位、单个路径最多 300 字符；继续出现的新路径统一进入 `/:other` 并累计 `path_overflow_total`，随机 URL 探测不会让 Counter 永久增长。
- `GET /api/metrics?hours=24` 返回 `runtime`、`history`、`logs` 三组聚合值；`history.tools` 是脱敏工具链健康度，`runtime.upstream_readers` 提供阻塞读取线程的活跃/峰值、静默轮询、错误和强制关闭计数，`runtime.sse_heartbeat` 提供下游心跳线程与实际发送/错误计数，`runtime.captcha_worker` 提供浏览器验证码回退队列的线程、活跃、积压和回压状态，`runtime.context_cache` 提供上下文附件缓存和上传失败/降级状态占用，`runtime.upload_slots` 提供附件/HAR 上传的活跃、峰值与累计回压，`runtime.upstream_responses` 提供非流式响应拒绝、错误正文截断，以及整次流式原始事件/语义输出/事件数的拒绝计数；`hours` 支持 1–720，面板常用 1 / 24 / 168。

- 整次上游 SSE 采用三重硬预算：原始事件累计 32 MiB、解析后的正文与思考累计 16 MiB、最多 100,000 个事件。核心上游读取与 OpenAI Chat、Responses、Anthropic 适配出口独立校验；超过预算会关闭上游流、按上游响应异常返回/发送协议错误、清理本次会话并把历史标记为失败，不会把半截工具标记当作有效调用。历史实时镜像只累计其实际会保留的回答 30,000 字符和思考 20,000 字符，避免每 750ms 对完整长输出反复拼接。
- **显式流完成判定**：依据网页抓包中的两类真实终止帧，只有收到 `data.done=true` 或 `[DONE]` 才把上游 SSE 记为成功。无正文时提前 EOF 可换新 chat 有界重试；已有正文时提前 EOF 不会重放造成重复内容，而是返回上游中断、把历史标为 error，并无视正常完成后的保留开关登记该半截 chat 删除。`runtime.upstream_responses.stream_incomplete_total` 记录此类连接。网页自身也必须收到本地 `done`：网络中断时保留已显示的部分回答并以 `incomplete` 标记存入本地会话，后续作为历史发送时会附带“仅保留部分内容”的说明。

## 历史对话浏览

「历史」页对齐 ds2api 的 chat history：**每条请求一条镜像记录**，直接看思维链、流式回复和发给上游的提示词。

### 请求记录（ds2api 同款请求级镜像）

- **两阶段落盘 + 流中节流持久化（ds2api progress 同款）**：请求进入 `stream_zai_completion` 时先写一条 `streaming` 记录（含完整出站消息、文件清单、上下文文本、最终 prompt、账号、调用方），流式进行中按详情页读取频率每 750ms 把已读到的思维链/回复节流写盘（换会话重试时立即清掉上一轮残留），流结束时更新为 `success` / `stopped`（客户端断开/停止生成，保留已读部分，状态码 200）/ `error`（上游失败，保留错误摘要）；`thinking` 与正文按 phase 持续分流积累。所有协议面（OpenAI Chat/Responses、Anthropic Messages、控制台、CLI）共用这一个汇聚点；同一 `history_ctx`、上游瞬时重试和工具格式纠错都会复用同一条记录，并用最终实际 prompt 覆盖首轮失败 prompt，不产生重复条目。
- **每条记录保存**：状态生命周期、接口面（openai_chat / anthropic_messages / panel_chat…）、模型、账号（上游 user_id 的 sha16 指纹）、调用方（panel / cli / api）、流式标记、`messages`（实际发出的消息数组，图片/文件块以 `[图片]` / `[文件: 名]` 占位）、`files` 元数据、`delivery_mode`（实际直传/附件拆分）、拆分请求与降级原因、`context_files`（内部生成的历史/工具文本附件：真实文件名、用途、大小、分片顺序头和镜像正文，单文件镜像最多 80000 字符并标记是否截断）、`final_prompt`（真实上游输入框文本）、思维链、回复、错误、耗时、估算 tokens（prompt 会计入内部附件正文），以及最终协议语义（`finish_reason`、工具数量/名称、解析来源、格式纠错次数/错误；不保存工具参数）。用户上传文件仍只保存文件名/大小/类型，**不保存用户附件内容**。读取旧记录时会把历史遗留的账号前缀原地迁移为指纹。
- 存储（`glm2api.history.v4`，已 gitignore 绝不入库）：`history.local.json` 是携带摘要条目（id/状态/预览/账号/调用方等）的小索引，完整记录在 `history.local.json.d/<id>.json` 每条一个文件——写入只重写单条 detail + 小索引，无全量重写写放大；detail 与索引均通过同目录临时文件原子替换，detail 或索引写入失败会保留脏标记，在下一次历史变更时重试，不会把失败误记为已持久化；旧 v3 ids 索引与 v2/v1 单文件格式首次读取时自动迁移。保留上限可在设置页配置（`history_max_records`，50–2000，默认 300），并同时受 256 MiB detail 总量预算约束；达到任一上限都从最旧记录开始清理，且至少保留最新一条。启动恢复还限制索引为 8 MiB、单条 detail 为 16 MiB、每轮最多扫描 4096 个 detail 文件，并校验安全记录 ID 与索引/详情 ID 一致性；异常文件保留在磁盘供人工恢复，但不会被无界读入进程。
- 删除单条或清空历史时，内存变更与磁盘确认分开报告：成功响应含 `persisted`，索引/detail 删除失败时返回 `persisted:false`、脱敏 `history_store_error` 和重启恢复风险说明，同时保留 `pending_writes` / `pending_deletes` 供下一次历史变更重试。历史页会持续展示该异常，恢复成功后自动消失；不存在的记录返回 404 `history_record_not_found`。
- **列表**：状态徽标（成功/失败/已停止/进行中）+ 内容预览（回复→思维链→错误→用户输入）+ 模型 · 接口面 · 账号 · 时间 + 条数徽章，每条内嵌 ✕ 删除；仅在历史页可见且停留第一页时每 1.5s 静默自动刷新（新请求自动出现、进行中状态实时变化），翻看更早页或隐藏标签页时暂停；无筛选的常规刷新只构建当前 50 条摘要，不再为最多 2000 条记录做无用转换。侧栏支持关键字搜索（标题/预览/模型/账号）与状态下拉筛选（后端 `?text=&status=`）。
- **详情**：顶部实发状态条明确标出 `INLINE`、`FILES + PROMPT` 或降级直传；中部三视图——「对话记录」按气泡展示协议消息与助手回复（内部拆分附件不会伪装成用户附件），「发送概览」用发送路径和清单回答“附了哪些文件、文件名是什么、输入框是什么”，「实发详情」按真实文件名逐个展开内部历史/工具附件正文，最后单独展示真实上游输入框。普通直传模式只显示输入框，不再把 `history_text` 渲染成一个并不存在的附件；旧镜像可推断实发模式，无法恢复的旧文件名/工具附件会明确提示。底部 meta 网格保留接口面/模型/耗时/状态码/请求时间/账号/Tokens 等信息；「导出 MD」同步按附件与输入框结构导出。打开 `streaming` 状态的记录每 750ms 自动刷新，终态、离开历史页、隐藏标签页或达到 240 次上限时停止。
- 端点：`GET /api/history/records?page=N&text=&status=`（摘要列表）、`GET /api/history/record?id=<req_id>`（完整记录）、`POST /api/history/record/delete`（body `{"id":"<req_id>"}`）、`POST /api/history/clear`（清空），均受 API Key 保护。日志只记 `history_recorded`（记录指纹 + 状态 + 字符数），不落对话内容。
- 管理查询统一限制为最多 32 个字段、字段名 128 字符、单值 4096 字符；历史搜索再收紧为 256 字符，历史页码限制为 1–1000。超限请求返回 400，带未读取正文的上传请求会关闭连接，避免剩余正文被当成下一条 keep-alive 请求。

### 上游会话（仅 API，不在面板展示）

面板历史页只展示请求镜像记录；上游账号侧的会话列表/详情/删除保留为 API（`GET /api/history/chats?page=N`、`GET /api/history/chat?id=<UUID>`、`POST /api/history/delete`，body `{"chat_id":"<UUID>"}`，404 视为已删除），供脚本或外部工具调用。上游详情消息按 `parentId` 链从 `currentId` 回溯定序，缺链时按时间戳排序兜底；上游不可达时这些端点返回错误信息。

## 聊天功能

对话页工具条与会话抽屉支持：

- 模型切换：四种基础模型（`glm-5.3` / `x-preview-l` (Flash) / `GLM-5-Turbo` / `glm-5.2`），以及各自的 `-forcehistory` 历史文件拆分版本；输入别名 `glm-5.3-flash`、`glm5.3flash`、`flash` 也会路由到 `x-preview-l`，`glm-5.1` 自动重映射到 `glm-5.2`
- 联网搜索：对应 HAR 中的 `features.auto_web_search`
- 深度思考：对应 HAR 中的 `features.enable_thinking`
- 思考挡位：按模型区分——`glm-5.3` / `x-preview-l` 支持 `low` / `high` / `max` 三挡；`glm-5.2` 仅 `high` / `max`（收到 low 自动升为 high）；`GLM-5-Turbo` 只能开关思考、不发送挡位
- 显示 thinking 片段：默认不显示，可手动打开
- 保持当前会话：成功发送后保存返回的 `chat_id`，后续请求可复用
- 编辑上一条：把上一条用户消息放回输入框，发送时复用当前 `chat_id` 并生成新的 message id
- 附件上传：支持一次选择多个文件、拖放或直接粘贴剪贴板图片/文件（自动去重），先上传到 `/api/v1/files/`，发送时按 HAR 的 `files` 字段带入 completion；首个文件确定账号后，其余文件由 3 个有界 worker 上传，不会因一次选择大量文件而无界并发。批次中途失败、发送失败或手动停止后，代理会尽力清理所有已成功上传的孤儿附件，避免在官方后台残留。附件**内容注入**由上游按模型决定：`glm-5.2` 实测正常；`x-preview-l`（Flash）上游不支持（官方前端同样看不到）；`glm-5.3` 视上游池而定，若回答“看不到附件”，可先用 `glm-5.2` 复测以区分链路问题与上游行为
- 停止生成：首个回答片段出现后可停止当前上游任务；上游 stop 最多等待 10 秒，接受实测 `{}`、204 空正文、`{"status":true}` 以及任务已结束的 404。即使 stop 请求未获确认，只要已有 `chat_id` 就会先持久化登记删除，中断的上游 chat 不会因控制接口故障而跳过回收；已输出文本仍保留在本地会话和历史镜像中
- 删除当前对话：完成或停止后可从上游删除当前 `chat_id`；删除不可恢复，下一次发送会创建新会话
- 调用完成删除本对话：默认开启；成功响应后自动删除该次上游 chat，并自动关闭“保持当前会话”。关闭后才可复用 / 编辑当前 chat

从最新 HAR 看，编辑已发送消息后再次发送没有额外编辑接口；上游表现为复用同一个 `chat_id`，然后再次调用 `/api/v2/chat/completions`。

从“上传文件” HAR 看，附件流程是：

```text
POST /api/v1/files/                 -> 返回 file 对象
POST /api/v1/chats/new              -> 创建 chat 和 user message id
POST /api/v2/chat/completions files -> 顶层 files: [{type:"file", file:{...}, ref_user_msg_id}]
```

从“删除对话 / 取消请求” HAR 确认的控制请求为：

```text
DELETE /api/v1/chats/{chat_id}             -> true
POST   /api/tasks/stop/{assistant_msg_id}  -> {} / {"status": true}
```

其中 `assistant_msg_id` 是 completion 请求顶层 `id`，不是 `chat_id`。页面会在有首个回答片段后才启用“停止生成”，避免在上游任务尚未建立时发送无效停止请求。stop 使用独立 10 秒网络上限；空对象/空正文按官网成功确认处理。若 stop 超时或失败但已知 `chat_id`，`POST /api/chat/cancel` 返回 202 `ok:true`、`upstream_stopped:false`，同时将 chat 删除意图写入 journal；前端立即断开本地流并提示“停止未确认，已安排删除”。

## 保存位置

登录态会保存到：

```text
profiles.local.json
```

保存内容使用 Windows DPAPI 当前用户加密，只能在当前 Windows 用户下解密。下次启动会自动加载上次保存的账号和当前选择。

预加载 HAR 后也会自动写入该加密保存区；保存采用“写临时文件后原子替换”，因此中断启动或同时操作账号时不会留下半截 JSON。

账号新增、切换、删除和重复项整理的响应都会返回顶层 `persisted`。若 DPAPI 或磁盘写入失败，变更仍在当前进程立即生效，但返回 `persisted: false` 和脱敏后的 `profile_store_error`，账号页同步显示“重启后可能丢失/恢复旧状态”的警告，不再误报“已加密保存”。`GET /api/auth/profiles` 与 `/api/status` 的 `profile_store.persisted/error` 可持续查看最近一次保存健康状态，运行日志以 `profile_store_write_error` 记录失败原因。

登录态以账号 `user_id` 和 token 指纹识别：同一个 token 会覆盖旧记录；同一个账号重新登录或重新上传 HAR 时也会更新既有记录，避免越积越多。历史上已存在的同账号重复项不会被启动时自动删除，可在网页“登录态管理”点击“清理同账号重复登录态”手动合并；会优先保留当前启用项。正在生成的 profile 受生命周期保护：删除接口返回 409 `profile_busy`，账号页同步禁用删除按钮；重复项清理会保留所有忙碌 profile，并在响应中返回 `skipped_busy_count`，避免进行中的取消、附件清理或后续会话失去所属登录态。
网页本地会话按账号隔离：切换账号后只显示该账号的历史会话，多轮上下文不会跨账号混用；旧版无账号标记的会话仍对所有账号可见。

网页默认设置会保存到 `settings.local.json`，只包含模型、联网搜索、深度思考、思考挡位、是否显示 Thinking、是否自动删除等非敏感选项，不包含 token。点击“保存当前为默认设置”后立即生效，下次启动自动恢复。

尚未完成的上游会话/孤儿附件删除意图会原子保存到 `pending_deletes.local.json`。文件只包含资源类型、chat UUID 或上游 file ID、不可逆账号指纹、原因和重试状态，不保存 token；删除成功即移除，服务重启时会使用已加载的匹配账号自动补偿，最多保留 256 条、30 天。旧版 chat-only v1 journal 会在首次变更时自动迁移到资源级 v2。

## 安全说明

- 本地网页不接收账号密码；密码只输入到官方 Chrome 登录页。
- 完整 token、captcha 不会显示在页面或文档中。
- `profiles.local.json` 包含加密后的登录态，请不要发给别人或提交到仓库。
- `pending_deletes.local.json` 包含待清理的上游 chat/file 标识，已加入 gitignore，同样不要复制或提交。
- 所有带 `Origin` 的浏览器 GET/POST 会在进入 handler 前校验来源，阻止恶意网页用无需预检的简单请求操作本机服务；同端口的 `127.0.0.1`、`localhost`、`::1` UI 自动允许，额外来源必须显式配置 `--cors-origin`。不带 `Origin` 的 SDK/CLI 请求不受影响。

## 运行日志

服务全程输出结构化运行日志，三路同写：`logs/glm2api.log`（UTF-8，8MB×5 轮转）、stderr（控制台）、内存环形缓冲（1500 条）。每条格式化日志最多 16,384 字符，超出部分统一追加 `…[log truncated]`；事件名和 RID 作为独立 LogRecord 元数据保留，因此超长结构化事件被截断后仍能筛选，`truncated_total` 可在日志页和统计页观察。面板「日志」页按时间、级别、线程、事件名、RID 和正文分栏展示，支持级别/类型/事件/RID/关键字组合过滤、异常快捷筛选、折行、复制与自动刷新。自动刷新使用序号游标只追加新事件，不再周期性下载并重建全部 500 行；慢请求期间不会叠加下一次轮询，游标因环形淘汰或服务重启失效时会自动回退到完整快照。

- 请求访问日志：`[rid] REQ/RES 方法 路径 -> 状态码 (耗时 ms)`；面板轮询端点（`/api/status` 等）静默，仅在失败时记录。
- 调用链事件（JSON `{"state": ...}` 风格，带 `rid` 关联同一次请求）：
  - `upstream_call_start`：模型、思考挡位、工具数、tool_choice、上下文模式（inline/file）、prompt 字数、是否复用会话、是否自动删除；
  - `upstream_chat_created` / `upstream_chat_reused`：上游会话创建或复用（只含 `chat_id_fp` 指纹）；
  - `fresh_captcha_*`：验证码求解全过程（backend、attempt、耗时、降级）；
  - `context_files_split`：模型特异的历史/工具多文件分片数量与单片字节上限；`context_file_uploaded` / `context_file_cache_hit`：各分片上传与缓存命中；
  - `context_upload_degrade_enter/skip`：Mode B 连续失败降级到模式 A 及窗口内跳过；
  - `profile_route_missing` / `profile_remove_blocked` / `profile_removed` / `profiles_compacted`：无效路由、忙碌删除保护和账号整理结果（profile 只记指纹）；
  - `tool_calls_parsed`：解析出的工具调用名与来源（output/thinking）；`tool_call_format_retry`：required/forced 未调用或工具标记无法转换时的一次纠错重采样；
  - `upstream_stream_open/done`：SSE 开始与结束（事件数、耗时）；`upstream_http_error` / `upstream_connect_failed`：上游错误摘要；
  - `upstream_chat_deleted` / `auto_delete_failed`：自动删除结果；
  - `server_started` / `server_stopped`：启动配置与退出；`server_bind_failed`：端口冲突（只记录 host、port 和系统错误码）。
- 协议 handler 抛出的异常会带完整堆栈写入日志；HTTP 层未捕获异常兜底记录并返回 500。日志 formatter 会在最终写入边界统一脱敏，因此 traceback 或上游错误中夹带的 Bearer/JWT/API key/captcha/cookie、URL 查询参数和本机用户绝对路径都不会落盘；文件名后的行号等排障信息仍保留。模块尚未初始化正式日志时用 `NullHandler` 禁止 Python `lastResort` 把原始异常直接写到 stderr，初始化时所有既有 handler 都会升级为脱敏 formatter。
- JSON 与 SSE 的客户端错误出口使用独立净化边界：删除常见凭据、HTTP URL 查询串、本机绝对路径和控制字符，并把异常正文限制在 1200 字符；OpenAI/Responses/Anthropic/Web 四个流式错误保持各自协议结构。该规则只处理错误负载，不修改成功回答中的代码、路径或示例文本。
- 内存日志 API 默认同时返回向后兼容的 `lines` 与结构化 `entries`，并附带游标、各级别计数、事件类型计数和热门 `state`；`format=structured` 可省略重复文本数组，`after_seq` 可增量读取。点击日志行的事件名或 RID 可直接追踪同类事件/单次请求。
- 日志文件按 8 MiB × 6 段轮转（活动文件 + 5 个备份，约 48 MiB 总预算）；状态页、统计页和日志页展示全部轮转段的真实总占用，不再只显示活动文件大小。
- 网页除长连接聊天 SSE 外不再使用无界 `fetch`：历史读取/删除、设置、API Key、账号操作和清理请求默认 10 秒，停止/删除上游会话 15 秒，统计与日志 8 秒，浏览器登录 330 秒，HAR/附件上传 10 分钟。统一封装会合并调用方 AbortSignal：用户在附件仍上传时点击停止会立即中止上传，同时保留 `AbortError` 语义；只有真正的计时器中止才提示“本地服务响应超时”。30 秒全局状态轮询和 1.5 秒浏览器登录进度轮询均使用单飞保护，后一个旧轮询结果不会覆盖登录终态。
- 流式正文超过 250,000 字符后切换为纯文本 DOM 更新，避免每个增量都重新解析整段 Markdown；重绘间隔会从 200ms 自适应放宽到 600ms/1200ms。完整正文仍保留在本次请求内，只有渲染方式降级。
- 模型回复、历史正文、账号/HAR 标签、文件名和 localStorage 会话元数据进入动态 DOM 前统一转义；Markdown 渲染只在转义后生成固定标签，`http/https` 之外的链接不会变成可点击元素。代码块占位符会避开正文中已有标记，防止同名文本导致代码块错位或重复。前端测试直接执行真实渲染函数并注入 HTML/占位符碰撞样例。
- 日志级别用 `--log-level {DEBUG,INFO,WARNING,ERROR}` 调整（默认 INFO）。
- 安全约定：token、captcha、API key、cookie、password、user_id、chat_id 等敏感或可关联标识一律只记录 sha16 指纹或脱敏占位，不落全文；访问日志丢弃整个 query string，并规范化动态 response id；结构化字段、异常文本和客户端错误会在各自最终出口再次递归脱敏。`logs/` 目录请勿提交仓库。

## 上游繁忙自动重试

上游偶发返回流首错误 `MODEL_CONCURRENCY_LIMIT`（"当前模型使用人数较多，请稍后再试或切换到其他模型。"，HAR 实测），典型场景是上一条消息的上游会话尚未完全释放时下一条请求就到达（Claude Code 连续工具调用时常见）。项目会内部消化这类瞬时错误：

- 仅当回答尚未输出任何内容（流首错误）且本次调用自建会话时重试：删除被中断的空会话 → 等待 → 新建会话重发；对客户端完全透明，错误不透传。
- 默认等待 **3 秒**、最多尝试 **3 次**；可在面板「设置」页调整"繁忙重试等待（秒）"（0–120）与"繁忙重试次数上限"（1–6），保存到 `settings.local.json`。
- 复用会话（continue/reuse）模式不内部重试（换会话会破坏"保持当前会话 ID"语义），错误原样透传。
- 可重试错误：`MODEL_CONCURRENCY_LIMIT`、繁忙/限流类关键词、实测创建 chat 阶段的“上游中断”、HTTP 429/500/502/503/504，以及**验证码被上游拒绝**（`FRONTEND_CAPTCHA_REQUIRED` F018/F019，见下文验证码时效）。所有重试都限定在尚未向客户端输出回答时；鉴权、内容审核类错误及已开始的回答一律原样结束，不做内部重放。
- **验证码失效重试不等待**：繁忙类错误等待 `wait_sec` 再试；验证码类错误无需等待（验证码是一次性凭据，不存在"繁忙"），立即换会话并**强制现场重解**（绕开验证码池与降级缓存，日志 `fresh_captcha_ready` 带 `force_fresh: true`）。
- 内部重试耗尽后的错误按协议规范返回，让客户端 SDK 走标准退避而非把 500 当未知故障：OpenAI 面 `503 server_error`、Anthropic 面 `529 overloaded_error`，均带 `Retry-After: 3`；Anthropic 流式错误事件同样标记 `overloaded_error`。
- 相关日志事件：`upstream_transient_retry`（attempt/wait_sec/error/reason）、`retry_cleanup_delete_error`（清理失败）。

## 上游繁忙自动重试段示例日志

```
{"rid":"ab12cd34","state":"upstream_chat_created","chat_id_fp":"...","attempt":1}
{"rid":"ab12cd34","state":"upstream_transient_retry","attempt":1,"max_attempts":3,"wait_sec":3.0,"error":"MODEL_CONCURRENCY_LIMIT: ..."}
{"rid":"ab12cd34","state":"upstream_chat_created","chat_id_fp":"...","attempt":2}
```

验证码被拒（超龄池码）的重试段：

```
{"rid":"cd56ef78","state":"upstream_transient_retry","attempt":1,"wait_sec":0.0,"reason":"captcha_rejected","error":"FRONTEND_CAPTCHA_REQUIRED: ...verify_code\":\"F019\"..."}
{"rid":"cd56ef78","state":"fresh_captcha_ready","attempt":2,"force_fresh":true,...}
{"rid":"cd56ef78","state":"upstream_chat_created","chat_id_fp":"...","attempt":2}
```

## 性能优化

- **验证码预热池**：fresh-captcha 模式下验证码求解实测约 8 秒，且 captcha 实测一次性使用（复用会被上游 `F018 verify_failed` 拒绝，不能缓存复用）。改为在请求间隙由后台线程（`captcha-prefetch`）预解下一个验证码放入池中，请求到来直接取用——求解延迟对客户端归零；池空或预解失败时同步求解兜底（行为同旧版）。日志事件 `fresh_captcha_pool_hit`。
- **验证码远端时效（实测）**：池码 ~61 秒仍被上游接受，**~114 秒即被 `F019 verify_failed` 拒绝**（2026-08-29 03:57/04:02 日志对照；此前空闲 2 分钟后取池码导致 Claude Code 连续 500 并进入分钟级退避）。池 TTL 据此设为 75 秒：连续请求间隔通常 <30 秒，池码几乎总是热的；空闲超龄直接丢弃、现场重解。漏网时由"验证码被拒 → 免等待换会话强制重解"兜底，客户端无感。
- **浏览器验证码并发隔离**：Playwright 回退 worker 为每个求解任务分配独立的单结果通道，反序完成时不会再由并发等待者取走并丢弃彼此的结果。排队上限为 8；满载立即回压，调用方超时/断连后任务会标记取消，worker 在启动昂贵浏览器操作和发布结果前跳过它。Playwright 启动失败会立即通知全部已排队等待者，空闲线程退出与新任务入队也通过同一锁消除竞态。`/api/status.captcha_worker` 和 `runtime.captcha_worker` 可查看线程、活跃、积压与累计回压。
- **HEAD 探活**：支持 HEAD 方法（按 GET 语义应答、只省略响应体），并新增 `GET/HEAD /api/hello` 探测端点；此前 `HEAD /api/hello` 返回 501 Unsupported method 会让探测方误判服务不可用（日志 03:58 实测）。
- **资源清理异步化 + 持久化补偿**：删除上游会话实测需 1-4 秒；失败 chat 的用户附件以及只上传了一部分的内部历史/工具分片此前还会在请求线程逐文件使用 60 秒通用超时。现在 chat/file 清理统一移到后台线程池（2 worker），并先批量原子登记到 `pending_deletes.local.json`（最多 256 条、30 天；容量紧张时优先保留 chat）。成功后移除；失败、进程退出时取消的排队任务或当时没有匹配账号的记录会在下次启动自动重放。重放由独立 `pending-delete-replay` feeder 按执行队列空位持续补料，即使 journal 多于 64 条也会在同一次运行中逐批处理，不阻塞服务启动；关闭时 feeder 会先停止，尚未提交的记录继续留在 journal。删除网络调用单次最多 10 秒，执行队列最多 64；journal 已提供一致性保障，因此满载时不再把资源清理拉回请求线程，而是保留待补偿记录。退出最多等待 10 秒，随后取消尚未开始的 future，运行中的任务在最近请求/重试边界停下，避免进程无上限假死。正常完成 chat 删除受 `delete_chat_after_completion` 控制；停止、断连和 `service_shutdown` 会无条件登记 chat 与孤儿附件回收。状态页/统计页分别显示 chat/file journal 数、feeder 是否活跃/待提交数、取消和回压。响应中 `chat_deleted` 通常为 `false` 并以 `chat_delete_pending: true` 标注；结果看日志（`auto_delete_*`、`interrupted_chat_cleanup_*`、`orphan_file_cleanup_*`、`context_file_cleanup_*`、`pending_delete_replay_*`），也可使用手动删除端点补删。
- **清理接口非阻塞化**：`POST /api/files/cleanup` 也走“批量登记后异步删除”，不会再由一个 HTTP handler 串行等待最多 64 次上游删除。响应通过 `accepted_count` / `journal_capacity_dropped` 区分实际进入 journal 与容量保护淘汰；队列满时 `scheduled:false`，已登记项仍保留给重启 replay；`journal_persisted:false` 明确提示本次 journal 落盘异常。对应事件使用 `client_file_cleanup_*`。
- **停止控制有界化**：`POST /api/tasks/stop/{assistant_message_id}` 使用独立 10 秒超时，并兼容 `{}` / 204 / `status|success|ok: true` 与任务已结束的 404。停止成功后再登记删除；停止失败但已有 chat 时仍返回 202 接受状态并持久化删除，避免此前异常分支提前返回导致的上游残留。日志以 `upstream_task_stopped` 或 `upstream_task_stop_cleanup_fallback` 区分“已确认停止”和“停止未确认、删除兜底”。
- **HTTP handler 有界化**：标准库 `ThreadingHTTPServer` 默认每个连接直接创建新线程；现在全局最多同时运行 128 个 handler，额外连接在 64 深度的监听队列及 server 回压点等待。由于服务面向本机浏览器/SDK，每连接正文空闲超时收紧为 10 秒，避免慢上传、半包请求或高频管理调用无限堆线程，并让退出时的半包请求能在自然排空窗口内结束。请求体超时统一返回 HTTP 408 `request_timeout`；四个生成入口只在 JSON 完整读取并通过协议校验后申请账号生成槽位，因此半包/无效请求不会占满每账号 3 个稀缺槽位。`/api/status` 与统计页提供当前活跃、历史峰值、等待数和累计回压次数；面板自身轮询不计入展示中的活跃值。
- **大上传独立回压**：附件落盘/上游上传最多同时 4 个，HAR 导入最多同时 1 个，与 128 个通用 HTTP handler 分开计数；超出时在读取正文前返回 429 `upload_capacity_busy`、`Retry-After: 2` 并关闭当前连接，避免未消费正文污染 keep-alive 请求边界。原始 HAR 仍可流式上传至 512 MiB，旧版 `application/json` 包装 HAR 因需整包驻留内存而限制为 64 MiB。状态页与 `runtime.upload_slots` 可查看附件/HAR 当前占用、峰值及拒绝次数。
- **请求体 framing**：JSON、HAR 和附件上传同时支持 `Content-Length` 与 HTTP/1.1 `Transfer-Encoding: chunked`。chunked 正文逐块执行总量限制，文件上传逐块写临时文件；chunk size 行最多 128 字节、trailer 总量最多 8 KiB。为避免边界歧义，`Content-Length + Transfer-Encoding`、重复 Content-Length、未知 transfer coding、畸形分块明确返回 400；超过路由正文预算则统一返回 413 `request_too_large` 并关闭连接，OpenAI/Anthropic/网页管理错误包保持各自协议形状。
- **上游非流响应有界化**：创建/浏览/删除会话等 JSON 响应最多读取 32 MiB，上游错误正文最多读取 64 KiB，附件上传响应最多读取 1 MiB。超过预算会立即关闭上游响应、记录 `upstream_response_too_large`，并以 502 暴露为上游故障；超长错误正文只保留安全摘要并计入 `runtime.upstream_responses.error_truncated_total`，不会整包进入内存或日志。附件上传的上游连接/响应异常同样返回 502 `upstream_upload_error`，不再误报为客户端 400。
- **有界优雅关闭**：Ctrl+C 后先关闭监听并设置全局 shutdown event，现有 handler 有 15 秒自然收束；仍未结束时尽力中断活跃客户端 socket，再等待 5 秒。OpenAI/Responses/Anthropic/网页生成会在每个上游 chunk、空闲心跳、验证码阶段和繁忙退避中检查该事件，状态记为 `stopped`，中断 chat 以 `service_shutdown` 原因进入删除 journal/队列；用户附件上传也在每个 multipart 分片间检查，且上游文件连接 idle timeout 为 15 秒。happy-dom 改用受控 `Popen`，关闭时会终止 Node helper；验证码预热线程被跟踪并在 handler 排空前取消。最后停止接收新的自动删除任务并最多等待 10 秒，未开始的任务安全取消、留待下次启动补偿。
- **繁忙重试覆盖会话创建**：`POST /api/v1/chats/new` 阶段的 429/5xx 瞬时错误同样进入"等待后重试"循环（日志 `upstream_transient_retry` 带 `stage=new_chat`）。
- **删除 404 视为成功**：会话已不存在（创建即被拒/已删除）不再报 `failed_chat_cleanup_error` 噪音。
- **429 限流响应**：本地并发槽满时返回的 429 带 `Retry-After: 3` 头，客户端（如 Claude Code）可据此退避。
- **工具调用原子校验**：显式工具块中的调用按整批处理；只要包含未声明/被策略禁止的工具、无法转换为声明 schema 的 arguments、缺失必填参数或模板占位符，就不会执行剩余“看似合法”的部分，而是进入已有的一次格式纠错重采样。确定重采样后会先清空首轮完整字符串及调用者分片列表，再启动第二轮，避免两轮各自合法但合计接近 32 MiB 时同时驻留。客户端最多声明 128 个工具（定义总量 1 MiB），模型单轮最多返回 64 个调用、每个 arguments 最多 256 KiB，超限同样按格式错误处理。
- **线程命名**：请求线程前缀 `proxy-`、请求级心跳线程 `sse-heartbeat`、上游阻塞读取线程 `upstream-sse-reader`、删除线程前缀 `autodel-`、预解线程 `captcha-prefetch`，日志线程字段一目了然。
- **单实例端口保护**：Windows 使用 `SO_EXCLUSIVEADDRUSE`，禁止多个服务进程共享同一监听端口；监听队列提升到 64，短时多客户端连接不会被系统默认的小队列过早拒绝。

实测效果（glm-5.2 短请求，`--fresh-captcha` 开启）：冷启动 15.0s → 池命中 6.6s；面板/CLI/协议调用全部受益。

## 内存说明

HAR 是浏览器网络日志，不只是登录态，可能包含接口响应、SSE、埋点和静态资源记录；几十 MB 属于正常现象。

服务模式启动时只提取必要登录态字段。为了降低常驻内存，程序会：

- 优先复用已保存 profile，遇到相同 `har_fp` 时跳过重复解析 HAR。
- 需要解析 HAR 时使用短生命周期 worker，主服务只保留提取后的登录态对象。
- 网页上传 HAR 使用 raw 文件上传，不再在浏览器 JS 中 `file.text()` 后 JSON 包装；服务端直接流式写入临时文件，再交给 worker 提取。HAR worker 串行运行，旧 JSON 包装兼容入口限 64 MiB，避免多个大对象同时驻留主进程。
- 聊天附件同样先流式落到临时文件，再以 multipart 分块转发到上游，不会同时在内存保留“原始文件 + multipart 完整副本”。
- 临时 HAR/附件在请求结束后立即删除。
- Responses API 的 `store/previous_response_id` 内存缓存同时受 128 条、32 MiB 总量和 8 MiB 单条预算约束，保留 1 小时；达到条数或字节上限时优先淘汰最旧项。超过单条预算的响应仍正常返回客户端，但不进入缓存，并记录脱敏的 `response_store_item_rejected` 事件。
- 历史/工具附件上传复用缓存使用 10 分钟 TTL 和精确 512 条 LRU 上限；任意查询、写入或状态读取都会全局清理过期项。按账号记录的连续上传失败/降级状态同样最多 512 个，过期降级窗口会全局清扫，频繁增删账号不会让常驻字典无限增长。状态页和统计页只显示条数、估算字节与失败/降级计数，不包含附件正文或账号 ID。

- 账号登录态统一经过字段预算校验：会话 token 最多 16 KiB、captcha 参数最多 64 KiB、其余遥测字段最多 4 KiB；异常 HAR/Token 会在进入常驻内存和 DPAPI profile store 前被拒绝。新账号达到 64 个上限时返回 409 `profile_capacity_reached`，刷新已有账号仍然允许。

## 本地访问边界

服务默认只监听 `127.0.0.1`；未授权浏览器 Origin 会在 handler 执行前被拒绝，因此其他网页既不能读取响应，也不能通过简单 POST 触发本机操作。本机同端口网页与原生 OpenAI 客户端不受影响。

如确实需要局域网访问，必须同时显式确认远程绑定并配置 API Key：

```powershell
python .\glm2api.py --serve --host 0.0.0.0 --allow-remote --api-key "your-local-secret" --cors-origin http://your-ui.example
```

非回环地址缺少 `--allow-remote` 或 API Key 时服务会拒绝启动。局域网暴露会让持有密钥的客户端以当前账号向上游发送请求，请只用于受信任网络；不要使用 `--cors-origin *`，除非你明确理解其影响。

回环地址也可按需为外部客户端启用鉴权：在面板「接口」页启用/更新/清除服务端 API Key（无需重启）。密钥会用 Windows DPAPI 加密保存到 `apikey.local.json`，仅供本机当前用户解密；修改或清除已启用密钥时需输入当前密钥。面板、DPAPI 存储、`GLM2API_API_KEY` 与 `--api-key` 共用同一输入规则：最多 4096 字符且不允许嵌入控制字符；鉴权使用固定长度摘要的恒定时间比较。CLI / 环境变量配置的密钥优先级更高，此时面板会显示为只读。

默认设置与面板 API Key 的更新使用各自独立的状态锁，把“校验 → 原子落盘 → 发布运行时状态”串成一个提交顺序；并发请求不会再出现磁盘保存 B、进程却回退为 A 的分叉。设置或 API Key 写入失败时保留原运行状态，分别返回 HTTP 500 `settings_store_write_failed` / `api_key_store_write_failed`，同时在状态页和结构化日志中提供脱敏后的失败原因；设置页会持续显示最近一次落盘健康状态。

本地状态恢复同样有明确资源预算：设置文件 64 KiB、删除补偿 journal 512 KiB、加密账号库外层 24 MiB/解密载荷 16 MiB。账号库恢复时重新校验每个登录态、64 个账号上限、profile ID 格式与重复 ID；超限或结构损坏时不会把部分不可信状态装入服务，状态页会报告脱敏错误并允许重新登录恢复。

也可以在启动时通过环境变量或命令行参数配置（优先级高于面板配置）：

```powershell
$env:GLM2API_API_KEY = "your-local-secret"
python .\glm2api.py --serve --host 0.0.0.0 --allow-remote --cors-origin http://your-ui.example
```

也可以用命令行参数：

```powershell
python .\glm2api.py --serve --api-key "your-local-secret"
```

启用后，除 `/healthz` 和最小化 `/api/status`（含面板密钥状态）外，所有 API 入口都必须携带 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`。面板「接口」页分两个区：服务端密钥（持久加密保存）和当前标签页访问密钥（`sessionStorage`，刷新不丢、关闭标签页清除）。

## 接口

- `GET /`：聊天页面。
- `GET /healthz`：本地服务健康状态，带固定 `service: "glm2api"` 身份标识且不泄露登录态。
- `GET /api/status`：状态和当前账号脱敏摘要；只返回 `user_id_fp` 等不可逆指纹及浏览器能力布尔值，不返回用户名、账号标签、原始 user_id 或本机浏览器绝对路径。账号名称仅由账号管理接口 `/api/auth/profiles` 提供给管理页面；启用 API Key 时该接口受鉴权保护。
- `GET /api/settings`：读取本地默认设置。
- `POST /api/settings`：保存本地默认设置。
- `GET/POST /api/settings/api-key`：读取或配置服务端 API Key（DPAPI 加密保存到 `apikey.local.json`；修改/清除已启用密钥需提交 `current_key`）。
- `GET /api/auth/profiles`：列出已加载 profile、每个账号的实时并发占用和保存状态；单账号只返回本地 profile ID、标签、安全来源展示、用户名/不可逆指纹、重复状态和并发字段，不返回原始 HAR 来源路径、上游 chat ID、设备/验证码/HAR 指纹，也不再为每个账号重复附带模型列表。响应中的 `concurrency` 汇总账号池容量、占用、可接入槽位与调度顺序；账号池最多保留 64 个 profile，同 token 或 user_id 的重新导入会原位更新，不占新槽位。
- `POST /api/auth/browser-login`：打开 Chrome 手动登录并采集 profile。
- `POST /api/auth/har`：上传 HAR 并切换。
- `POST /api/auth/token`：粘贴会话 token（三段式 JWT）添加账号并切换，body 为 `{"token":"...","label":"可选备注"}`。
- `POST /api/auth/switch`：切换 profile。
- `POST /api/auth/remove`：删除指定的本机已保存 profile；仍有生成请求时返回 409 `profile_busy`。
- `POST /api/auth/compact`：手动清理同一账号的重复 profile；忙碌项会保留并通过 `skipped_busy_count` 报告。
- `POST /api/files/upload`：上传单个附件到上游，raw body；文件名放 query `filename`。
- `POST /api/files/cleanup`：登记并异步删除已上传但未进入成功会话的孤儿附件，body 为 `{"files":["<file_id>", ...],"profile_id":"<可选 profile>"}`。有效项返回 202；`accepted_count` 是成功进入内存 journal 的数量，`journal_capacity_dropped` 表示因容量保护未接纳的文件，`scheduled:false` 表示后台队列暂满但已登记项仍会在重启后补偿，`journal_persisted:false` 表示本次 journal 落盘异常。未知 profile 返回 404 `profile_not_found`，文件 ID 只接受字符串且单次最多 64 项。
- `POST /api/chat`：网页聊天 SSE 接口。
- `POST /api/chat/cancel`：停止当前网页聊天请求并安排删除中断的上游 chat，body 为 `{"assistant_message_id":"<UUID>","chat_id":"<UUID>"}`；`chat_id` 可省略（尚未建立上游会话时）。停止已确认返回 200；停止失败但删除已登记返回 202 `ok:true` 和 `upstream_stopped:false`；既未停止又没有 chat 可清理时保留 502/504 错误。
- `POST /api/chat/delete`：删除指定上游会话，body 为 `{"chat_id":"<UUID>"}`。
- `GET /api/history/chats?page=1`：上游账号历史对话列表（分页）。
- `GET /api/history/chat?id=<UUID>`：单条历史对话详情与消息链。
- `POST /api/history/delete`：删除上游历史对话，body 为 `{"chat_id":"<UUID>"}`。
- `GET /api/history/records?page=1`：请求镜像记录摘要列表（每条请求一条，新→旧）。
- `GET /api/history/record?id=<req_id>`：单条请求镜像完整记录（出站消息、实际实发模式、内部附件清单/正文、上游输入框、回复与思维链）。
- `POST /api/history/record/delete`：删除单条请求镜像记录，body 为 `{"id":"<req_id>"}`。
- `POST /api/history/clear`：清空全部请求镜像记录。
- `GET /api/metrics?hours=24`：读取历史调用、进程运行态与日志健康度聚合统计；不返回提示词/回复正文。
- `GET /api/logs?lines=300&level=&kind=&state=&rid=&text=&after_seq=`：读取最近运行日志（`lines` 1-2000；支持级别下限、事件/访问/系统/异常类型、事件名、RID 与关键字过滤），默认返回兼容 `lines`、结构化 `entries`、`cursor` 和 `stats`；传 `format=structured` 时省略重复的 `lines`，传 `after_seq=<cursor.last_seq>` 时只返回新事件。若游标已被环形缓冲淘汰或来自旧进程，`cursor.reset_required=true` 且响应自动携带当前尾部快照。
- `GET /v1/models` / `POST /v1/chat/completions`：OpenAI Chat Completions 兼容接口。
- `POST /v1/responses`、`GET /v1/responses/{response_id}`：OpenAI Responses 兼容接口。
- `POST /anthropic/v1/messages`：Anthropic Messages 兼容接口。
- `POST /anthropic/v1/messages/count_tokens`：本地兼容的输入 token 估算接口。

## 命令行快速发送

```powershell
python .\glm2api.py --har ..\chat.z.ai.har --prompt "你好" --model glm-5.3 --fresh-captcha
python .\glm2api.py --har ..\chat.z.ai.har --prompt "你好" --model glm-5.3-flash --fresh-captcha
python .\glm2api.py --har ..\chat.z.ai.har --prompt "今天有什么新闻" --model glm-5.3 --web-search --fresh-captcha
python .\glm2api.py --har ..\chat.z.ai.har --prompt "只输出 OK" --model glm-5.2 --no-thinking --fresh-captcha
```

`--fresh-captcha` 表示“每次请求获取新验证码”，不再把是否启用 fresh-captcha 与浏览器实现绑定。旧参数 `--fresh-captcha-browser` 仍作为隐藏兼容别名。验证码求解器由 `--captcha-mode auto|happydom|browser` 控制：`happydom` 仅使用本地 Node 解码器，不启动浏览器；`browser` 使用常驻复用的 Playwright headless Chrome；`auto` 优先 happy-dom，失败时才回落浏览器。

一键启动会先验证 happy-dom（`captcha_happy.mjs`，同目录需有 `node` 与 `node_modules`）；可用时明确传入 `--captcha-mode happydom`，不会检查、安装或启动 Playwright。只有 happy-dom/Node 不可用时才准备 Playwright 并切换到 `browser` 回退模式。每次请求内建两次尝试（间隔 20 秒）；`--captcha-timeout-ms` 默认 75 秒。`/api/status` 会返回 `captcha_strategy`、`captcha_fresh_enabled`、`captcha_solver`、`captcha_happydom_available`、`captcha_browser_fallback_enabled` 和 `legacy_browser_captcha_refresh_enabled`，面板显示实际生效的求解链路；旧 `captcha_mode=browser_fresh` 值继续保留兼容。隐藏的旧版 `/api/auth/captcha-refresh` 仅在 `auto/browser + --fresh-captcha` 时可用；`happydom` 模式会直接返回 409，不会拉起 Playwright。

上游流式响应有“无数据间隔”超时（默认 300 秒）：深度思考时上游可能长时间不吐数据，超时会被视为中断。可在面板“模型与推理控制 → 上游响应超时（秒）”调整并保存，立即生效；也可用 `--upstream-timeout-sec <60-3600>` 在启动时锁定，此时面板不可覆盖。`--quiet` 可关闭逐请求访问日志。

`POST /api/chat` 和 `POST /v1/chat/completions` 可传这些扩展字段：

```json
{
  "model": "glm-5.3",
  "auto_web_search": false,
  "enable_thinking": true,
  "reasoning_effort": "max",
  "include_thinking": false,
  "delete_chat_after_completion": true,
  "mode": "new",
  "chat_id": "",
  "files": [],
  "history": []
}
```

`mode` 可为：

- `new`：新建上游 chat。
- `continue` / `reuse`：复用传入的 `chat_id`。若上游会话已失效（被删除/不存在），会自动降级为 `new` 并携带 `history` 重试一次，不会直接报错。
- `edit`：按 HAR 行为复用传入的 `chat_id` 并重新发送当前输入。

每个已保存账号最多允许 3 个同时进行中的上游生成（网页聊天、OpenAI Chat Completions、Responses、Anthropic Messages 共用同一槽位池）。不带路由提示的新会话按“当前默认账号 → 其它已保存账号”的顺序自动寻找空槽：当前账号满 3 个后第 4 个请求会接管下一个账号，再按同样规则顺延，账号池总容量为“账号数 × 3”；全部满载时才返回 HTTP 429 `chat_slot_busy`，`/api/status` 的 `concurrency` 可查看总量、逐账号占用和 `routing_order`。网页会把首次上传/新会话绑定到实际接管的 profile，后续继续、编辑、取消、删除和附件清理都携带 `X-GLM2API-Profile-ID`，避免跨账号误用上游 chat 或文件。协议响应也通过同名响应头返回实际接管的 profile，外部客户端需要复用上游会话/文件时可在下一轮原样回传；跨域调用可读取该响应头。固定 profile 已满时返回的 429 带 `scope: "profile"`，不会误称整个账号池满载，也不会为腾槽而跨账号串话；无固定 profile 且全池已满时为 `scope: "pool"`。请求头引用已删除或未知 profile 时明确返回 `profile_not_found`（网页/上传为 404，OpenAI/Anthropic 为 400），不再伪装成容量不足。槽位绑定到申请时所在的 profile：即使生成期间在面板切换了账号，旧的进行中请求结束后也会正确释放原账号槽位。上游流一旦消费完（或确认失败），对应槽位会立即释放，后续自动删除等收尾不再占用槽位。

`history`（网页面板与 `/api/chat` 自动附带）：`[{"role":"user"|"assistant","content":"..."}, ...]`。`new` 模式会作为 `messages` 历史随当前输入一起发送到新建的上游 chat；`continue`/`edit` 正常时不使用该字段（依赖上游 `chat_id` 已存的上下文），仅在上游会话失效并自动降级为 `new` 时才会携带。服务端最多保留最近 20 条、单条 8000 字符，并自动剥离本地工具调用标记。

`/v1/chat/completions` 还兼容常见的 `messages[].content` 文本数组形式；历史中的 `developer` 按参考项目归一化为 `system`，assistant 的 `reasoning_content` 会以带标签块保留在历史转写中，tool 结果会保留函数名与调用 ID。单条 user 文本消息的既有行为不变。

## 协议兼容与函数调用

除既有网页接口外，服务提供三种本地协议入口：

- OpenAI Chat Completions：`POST /v1/chat/completions`（别名 `/chat/completions`）。
- OpenAI Responses：`POST /v1/responses`（别名 `/responses`）。响应可在内存中保留，并用 `GET /v1/responses/{response_id}` 或下一轮的 `previous_response_id` 取回上下文。
- Anthropic Messages：`POST /anthropic/v1/messages`（兼容别名 `/v1/messages`、`/messages`），以及对应的 `count_tokens` 路径。

调用端可继续传常见的 `Authorization`、`x-api-key`、`anthropic-version` 请求头；未配置本地 API Key 时它们只用于兼容 SDK，身份仍由当前已保存并选中的本机 profile 决定。配置 `GLM2API_API_KEY` 或 `--api-key` 后，`Authorization: Bearer <key>` / `x-api-key` 会先用于本地鉴权。`GET /api/status` 的 `protocol_compatibility` 字段可用于探测能力。

### 思维链回传

API 协议面**默认回传思维链**，不受面板"显示 Thinking"开关影响（该开关只管网页聊天展示）：

- OpenAI Chat Completions：非流式在 `message.reasoning_content` 返回完整思维链；流式先推 `delta.reasoning_content` 增量再推 `content` 增量。
- OpenAI Responses：`output` 携带 `reasoning` 项，`reasoning.summary` 为完整思维链文本。
- Anthropic Messages：`content` 首块为 `{"type":"thinking","thinking":"...","signature":""}`；流式对应 `thinking_delta`。`signature` 恒为空串——本地无法合成 Anthropic 签名，客户端可据此区分兼容性思维链与 Claude 原生签名块。
- 显式关闭：请求体传 `"include_thinking": false`（三种协议均支持）；模型为 `*-nothinking` 后缀或思考被关闭时思维链自然为空。
- Anthropic 请求体 `thinking: {"type":"enabled","budget_tokens":N}` 会自动开启思考与回传。

`GET /v1/models` 和网页模型下拉框只公布八项：`glm-5.3`、`x-preview-l`、`GLM-5-Turbo`、`glm-5.2` 及各自的 `-forcehistory` 变体。常见 OpenAI / Anthropic 模型名仍可作为**输入别名**接受：例如 `gpt-5`、`gpt-5-codex`、`gpt-4.1`、`claude-sonnet-4-6`、`claude-opus-4-6`、`claude-haiku-4-5`（默认路由到 `glm-5.3`）；`glm-5.3-flash` / `glm5.3flash` / `flash` 路由到 `x-preview-l`。响应中的 `model` 保留客户端请求的名称，方便 SDK 工作。

函数工具支持 OpenAI 的 `tools: [{type: "function", function: ...}]`、Responses 的 `tools: [{type: "function", name: ...}]` 与 Anthropic 的 `tools: [{name, input_schema, ...}]`；`inputSchema` 与 `schema` 也会归一化。适配器会把 JSON Schema、工具选择策略和完整对话一起封装，并将模型返回值转回原协议的 `tool_calls` / `function_call` / `tool_use` 结构。

- 工具只会返回给调用客户端，glm2api 不会自行执行文件、命令、HTTP 或任何其他函数。
- 支持 `tool_choice` 的 `auto`、`none`、`required` / `any`、指定函数和 Responses `allowed_tools` 子集；子集会同步过滤提示词、工具附件和解析结果，空集合/未声明名称直接返回 400。
- 支持 OpenAI 的 `parallel_tool_calls: false` 与 Anthropic 的 `disable_parallel_tool_use: true`；模型若违反约束返回多个调用，整批进入一次格式纠错，不会只执行第一个并静默丢掉其余调用。
- 下一轮将工具执行结果作为 OpenAI `role: "tool"`、Responses `function_call_output` 或 Anthropic `tool_result` 传回，即可继续工具链；旧版 OpenAI `assistant.function_call → role:"function"` 也会保留，并将缺失的调用 ID 关联到最近同名函数调用。
- OpenAI / Responses 里声明的 `web_search*` 内置工具会映射为当前上游的联网搜索开关；自定义函数仍由客户端负责执行。

工具调用提示词与参考项目 dkceshi 对齐：英文 DSML 指令（7 条规则 + 5 个反例 + 按当前请求工具动态生成的正确示例），并保留 `required` / 指定工具的强制调用句与 Read 类工具的缓存守卫提示；内联上下文按工具 schema/规则在前、对话历史在后的语义顺序发送。额外的 completion guard 仍让模型自行判断是否需要工具，但明确禁止用“稍后读取/接下来调用”之类的进度计划冒充已完成回答，也明确说明真正完成时应正常无工具收尾。解析兼容 glm2api JSON 外壳、标准 XML、DeepSeek 分隔符/下划线/全角与 CJK 标签漂移、可安全补齐的外层结束标签，以及实测 Claude 风格 `<tool_call>…<arg_key>/<arg_value>…`（含后续 arg_key 漏开标签）、`Tool: … <tool_input>`、`**Calling:**` 和 `<function_call>`；同时修复嵌套 XML 参数、JSON 尾逗号/未引号键、HTML 实体和路径反斜杠。反引号/波浪线代码围栏（含未闭合围栏和内部反引号）里的示例不会被识别或剥离，未声明工具会被拒绝，声明为 `string` 的参数会在模型误输出对象 / 数组时转为紧凑 JSON 字符串。`tool_choice=auto` 完全以模型的可见最终通道为准：输出工具标记才转换为调用，没有工具标记就作为正常收尾返回，不猜测自然语言，也不把 thinking 中的候选调用提升执行。只有可见工具标记明显畸形时才附带纠错提示重采样一次；`required` / forced 仍会强制调用，并可从 thinking 恢复遗漏的调用。首轮正文/reasoning 不会泄露给客户端；纠错耗尽会返回协议错误并把历史镜像标成 error。工具调用标记在上游流中会先缓冲和解析；协议请求验证后先发送响应头，请求级心跳随即覆盖历史附件准备、创建 chat、验证码、连接和响应读取的全阶段，每 10 秒向 OpenAI Chat、Responses、Anthropic 和网页聊天发送协议安全的 SSE keep-alive 注释。所有 SSE 帧经同一写锁串行输出；断连会在最近的阶段边界停止继续请求，并关闭尚未消费的上游响应，同时回收心跳及读取线程。普通无工具文本仍保持流式转发。

## 上下文文件封装

默认不进行历史文件拆分：三种协议都会把归一化上下文直接放入上游网页输入框。只有明确开启字段或选择 `-forcehistory` 模型时，才按参考项目格式拆成**附件 + 一个聊天框**发送：附件一类为对话历史转写（`[system]` / `[user]` / `[assistant]` / `[tool]` 标签，assistant 推理使用 `[reasoning_content]` 块，工具结果带 `[function=... invocation_id=...]` 头）；附件二类为工具声明（`name/description/schema` 逐项），仅当前 `tool_choice` 允许调用工具时存在，`none` 不上传，指定函数时只写入该函数。实测 GLM-5.3 单个文本附件在 48 KiB 可读到末尾，50 KiB 起稳定只读到约 49 KiB，而 GLM-5.2 可完整读取 256 KiB；因此实际模型为 `glm-5.3` 时，历史和工具定义都会按不超过 **40 KiB/片** 自动打包为多个附件，每片正文包含 `segment X/Y` 顺序头，最新用户消息保留在最后一个历史片。GLM-5.2 保持参考项目的单文件行为。聊天框按 System→User 语义顺序扁平化：工具指引与 DSML 契约在前，附件续写提示在最后，并明确要求按分片头数字顺序读取全部附件。**Output integrity guard** 只由输入框内仍生效的上下文触发：拆分成功后旧 tool 历史已在附件中，不会误触发；当前轮允许调用工具时仍前置守卫。附件使用随机短文件名；同账号同内容 10 分钟内复用已上传文件。内部附件按一个完整包原子组装：任一分片上传失败时不会把半个附件包送入 completion，而是清理已上传的孤儿文件并回落为整段上下文直传。「历史」页按 `历史对话 3/5` / `工具定义 1/2` 展示真实文件、正文与输入框。

Z.ai 单条 completion 最多携带 10 个附件。GLM-5.3 的生成分片与用户附件合计超过该限制时，代理会启用同一 chat 内的多波次预载：每个中间波次最多附 10 个历史/工具文本，输入框明确说明“只读取并开始思考，等待后续”，收到首个 thinking delta 后立即调用 `/api/tasks/stop/{assistant_message_id}`；下一波把该 assistant ID 写入 `current_user_message_parent_id` 以接续上下文。各波次按需即时上传，不会在首波前先上传整个上下文包；最终波只在上一波停止成功后上传剩余分片（工具定义尽量位于后段）并附上真正执行提示。中间或最终波次若在尚未产生语义输出时返回 `MODEL_CONCURRENCY_LIMIT`，会在配置的有界 attempt 内保持同一 parent、换新 user/assistant message ID、重新上传该波文件后再试；本请求新建且已被替代的附件会立即登记删除并从本地复用缓存失效，缓存命中的共享附件则不会被误删，首波重试同时新建 chat 并回收失败 chat。此多波次 chat 属于内部临时上下文容器，最终成功、失败或客户端中断都会进入持久化删除流程，即使调用方关闭普通 `delete_chat_after_completion` 也不会把预载会话留在官网历史中；若请求原本要求复用旧 chat，会用完整历史转写安全降级到新的临时 chat，避免猜测旧会话当前 parent。非分批路径的附件总数超过 10 时会在创建 generation 前返回 400。历史镜像从预载开始即为 `streaming`，失败也会落盘，并记录 `context_preload_waves/files`；最多保留 64 个上下文分片清单，足以覆盖 2 MiB 请求预算。聚合统计公开对应的 staged request、波次和文件计数。日志以 `context_preload_stopped`、`upstream_transient_retry`、`context_wave_file_cleanup` 和 `auto_delete_*` 提供不含正文/真实 ID 的证据。

可通过以下任一字段显式启用或关闭（模型后缀优先）：

```json
{
  "context_as_file": true,
  "current_input_file": true,
  "history_as_file": true
}
```

模型名带 `-forcehistory` 时始终启用，例如 `glm-5.2-forcehistory` 或 `gpt-5-forcehistory`；带 `-nothinking` 时关闭上游深度思考，例如 `gpt-5-nothinking`。生成该文本附件的本地临时文件会在上传完成后立即删除。

## 调用完成自动删除

`delete_chat_after_completion`（别名 `delete_after_completion` / `auto_delete`）默认是 `true`。`/api/chat`、Chat Completions、Responses、Anthropic Messages 以及 CLI 直连模式（`--prompt`）在**成功完成**后都会删除本次上游 chat；本地的 Responses 历史缓存不受影响。

传入 `false` 可保留正常完成的 chat。取消/断连会无条件异步回收已创建的中断 chat；即使 stop 控制请求本身失败，只要 `chat_id` 已建立也会写入删除 journal。其它上游错误与工具格式重试仍遵循该开关。自动删除失败不会丢弃已生成答案，网页会显示失败信息，协议入口会写入脱敏本地日志。

删除和停止生成会按浏览器 HAR 补齐 `Sec-Fetch-*` / `sec-ch-ua*` 同源请求头；如果上游仍返回 401/403，会自动用当前 profile 的 token、设备 ID 和前端版本重试一次。旧 profile 没有保存 Client Hints 时会从 User-Agent 自动推导，通常不需要重新登录。

这里的 `files` / `attachments` 仍是本项目既有上传接口返回的 Z.ai 文件对象，不是 OpenAI Files API 的 `file_id` 下载代理。标准 `input_file` / 图片内容块会保留为上下文中的文件或图像说明，但不会自动从第三方下载原始文件。

## 调用示例

OpenAI Responses：

```powershell
$body = @{
  model = 'gpt-5'
  input = '北京现在天气如何？如需外部数据请调用工具。'
  tools = @(@{
    type = 'function'
    name = 'get_weather'
    description = '查询城市天气'
    parameters = @{ type = 'object'; properties = @{ city = @{ type = 'string' } }; required = @('city') }
  })
} | ConvertTo-Json -Depth 12

Invoke-RestMethod http://127.0.0.1:8008/v1/responses -Method Post -ContentType 'application/json' -Body $body
```

Anthropic Messages：

```powershell
$body = @{
  model = 'claude-sonnet-4-6'
  max_tokens = 512
  messages = @(@{ role = 'user'; content = '请读取配置。' })
  tools = @(@{
    name = 'read_config'
    description = '读取配置内容'
    input_schema = @{ type = 'object'; properties = @{ path = @{ type = 'string' } }; required = @('path') }
  })
} | ConvertTo-Json -Depth 12

Invoke-RestMethod http://127.0.0.1:8008/anthropic/v1/messages -Method Post -ContentType 'application/json' -Body $body
```

完整字段、事件序列、流式示例和已知边界见 [协议兼容说明](docs/protocol-compatibility.md)。

## 兼容边界

- 这是本地协议适配层，不是 OpenAI 或 Anthropic 的官方服务实现；`usage` / `count_tokens` 是确定性的本地估算值，不是上游计费 token。
- Responses 的 `store: true` 仅在进程内保存最近 128 个响应（插入后立即裁剪，不会短暂越界），默认有效期 1 小时；重启、过期或 `store: false` 后不能再用 `previous_response_id`。
- `max_tokens` 会做基本正数校验，但当前上游接口没有等价的严格输出长度控制字段。
- Anthropic `thinking` 兼容块的 `signature` 为空，表示本地适配出的未签名内容；它不能当作 Anthropic 已签名的思考块使用。

## 验证

```powershell
python -m py_compile .\glm2api.py
python -m unittest discover -s .\tests -p "test_*.py" -v
npm test
npm run release-check
```

`release-check` 扫描 Git 已跟踪及未忽略候选文件，拒绝 HAR、日志、本地登录态/设置/历史、node_modules、私钥、常见云端/API token、JWT、用户目录绝对路径和非占位凭据字面量，并同步校验 Python/JSON 语法。提交前可用 `python .\scripts\public_release_check.py --staged` 只检查暂存候选；没有暂存文件时会失败关闭。
