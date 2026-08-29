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

一键脚本会先检查 `8008`：若已是 glm2api 服务，会直接打开已有页面；若被其他程序占用，会显示对应 PID，而不是再启动一个失败的副本。

## 登录方式

支持无账号启动。没有 HAR 也能打开网页，在左侧导航进入「账号」页操作：

1. 点击「拉起官方浏览器登录」。
2. 在弹出的官方 `chat.z.ai` Chrome 窗口中手动登录并完成验证码。
3. 登录成功后，本地服务会读取登录后的 token/user/device 摘要，保存 profile 并切换。

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

## 面板结构

Web UI 是控制台式布局：左侧导航 + 顶栏状态，共七页——「概览」（状态磁贴/快捷操作/最近日志/调用入口）、「对话」（可收起会话抽屉 + 紧凑控制条 + 输入框）、「历史」（请求级镜像记录：思维链/流式输出/最终提示词）、「日志」（实时运行日志）、「账号」（登录态管理）、「设置」（默认行为与上游参数）、「接口」（API Key/端点/示例）。

## 历史对话浏览

「历史」页对齐 ds2api 的 chat history：**每条请求一条镜像记录**，直接看思维链、流式回复和发给上游的提示词。

### 请求记录（ds2api 同款请求级镜像）

- **两阶段落盘 + 流中节流持久化（ds2api progress 同款）**：请求进入 `stream_zai_completion` 时先写一条 `streaming` 记录（含完整出站消息、文件清单、上下文文本、最终 prompt、账号、调用方），流式进行中按详情页读取频率每 750ms 把已读到的思维链/回复节流写盘（换会话重试时立即清掉上一轮残留），流结束时更新为 `success` / `stopped`（客户端断开/停止生成，保留已读部分，状态码 200）/ `error`（上游失败，保留错误摘要）；`thinking` 与正文按 phase 持续分流积累。所有协议面（OpenAI Chat/Responses、Anthropic Messages、控制台、CLI）共用这一个汇聚点；同一 `history_ctx` 复用同一条记录，面板降级重试不会产生重复条目。
- **每条记录保存**：状态生命周期、接口面（openai_chat / anthropic_messages / panel_chat…）、模型、账号（上游 user_id 前 8 位）、调用方（panel / cli / api）、流式标记、`messages`（实际发出的消息数组，图片/文件块以 `[图片]` / `[文件: 名]` 占位）、`files` 元数据、`delivery_mode`（实际直传/附件拆分）、拆分请求与降级原因、`context_files`（内部生成的历史/工具文本附件：真实文件名、用途、大小和镜像正文，单文件最多 80000 字符并标记是否截断）、`final_prompt`（真实上游输入框文本）、思维链、回复、错误、耗时、估算 tokens（prompt 会计入内部附件正文）。用户上传文件仍只保存文件名/大小/类型，**不保存用户附件内容**。
- 存储（`glm2api.history.v4`，已 gitignore 绝不入库）：`history.local.json` 是携带摘要条目（id/状态/预览/账号/调用方等）的小索引，完整记录在 `history.local.json.d/<id>.json` 每条一个文件——写入只重写单条 detail + 小索引，无全量重写写放大；detail 与索引均通过同目录临时文件原子替换，detail 或索引写入失败会保留脏标记，在下一次历史变更时重试，不会把失败误记为已持久化；旧 v3 ids 索引与 v2/v1 单文件格式首次读取时自动迁移。保留上限可在设置页配置（`history_max_records`，50–2000，默认 300），超出裁掉最旧并同步清理 detail 文件。
- **列表**：状态徽标（成功/失败/已停止/进行中）+ 内容预览（回复→思维链→错误→用户输入）+ 模型 · 接口面 · 账号 · 时间 + 条数徽章，每条内嵌 ✕ 删除；仅在历史页可见且停留第一页时每 1.5s 静默自动刷新（新请求自动出现、进行中状态实时变化），翻看更早页或隐藏标签页时暂停；无筛选的常规刷新只构建当前 50 条摘要，不再为最多 2000 条记录做无用转换。侧栏支持关键字搜索（标题/预览/模型/账号）与状态下拉筛选（后端 `?text=&status=`）。
- **详情**：顶部实发状态条明确标出 `INLINE`、`FILES + PROMPT` 或降级直传；中部三视图——「对话记录」按气泡展示协议消息与助手回复（内部拆分附件不会伪装成用户附件），「发送概览」用发送路径和清单回答“附了哪些文件、文件名是什么、输入框是什么”，「实发详情」按真实文件名逐个展开内部历史/工具附件正文，最后单独展示真实上游输入框。普通直传模式只显示输入框，不再把 `history_text` 渲染成一个并不存在的附件；旧镜像可推断实发模式，无法恢复的旧文件名/工具附件会明确提示。底部 meta 网格保留接口面/模型/耗时/状态码/请求时间/账号/Tokens 等信息；「导出 MD」同步按附件与输入框结构导出。打开 `streaming` 状态的记录每 750ms 自动刷新，终态、离开历史页、隐藏标签页或达到 240 次上限时停止。
- 端点：`GET /api/history/records?page=N&text=&status=`（摘要列表）、`GET /api/history/record?id=<req_id>`（完整记录）、`POST /api/history/record/delete`（body `{"id":"<req_id>"}`）、`POST /api/history/clear`（清空），均受 API Key 保护。日志只记 `history_recorded`（记录指纹 + 状态 + 字符数），不落对话内容。

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
- 附件上传：支持一次选择多个文件、拖放或直接粘贴剪贴板图片/文件（自动去重），先上传到 `/api/v1/files/`，发送时按 HAR 的 `files` 字段带入 completion；发送失败或手动停止后，代理会尽力清理本次已上传的孤儿附件，避免在官方后台残留。附件**内容注入**由上游按模型决定：`glm-5.2` 实测正常；`x-preview-l`（Flash）上游不支持（官方前端同样看不到）；`glm-5.3` 视上游池而定，若回答“看不到附件”，可先用 `glm-5.2` 复测以区分链路问题与上游行为
- 停止生成：首个回答片段出现后可停止当前上游任务，已输出的文本会保留
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

其中 `assistant_msg_id` 是 completion 请求顶层 `id`，不是 `chat_id`。页面会在有首个回答片段后才启用“停止生成”，避免在上游任务尚未建立时发送无效停止请求。

## 保存位置

登录态会保存到：

```text
profiles.local.json
```

保存内容使用 Windows DPAPI 当前用户加密，只能在当前 Windows 用户下解密。下次启动会自动加载上次保存的账号和当前选择。

预加载 HAR 后也会自动写入该加密保存区；保存采用“写临时文件后原子替换”，因此中断启动或同时操作账号时不会留下半截 JSON。

登录态以账号 `user_id` 和 token 指纹识别：同一个 token 会覆盖旧记录；同一个账号重新登录或重新上传 HAR 时也会更新既有记录，避免越积越多。历史上已存在的同账号重复项不会被启动时自动删除，可在网页“登录态管理”点击“清理同账号重复登录态”手动合并；会优先保留当前启用项。
网页本地会话按账号隔离：切换账号后只显示该账号的历史会话，多轮上下文不会跨账号混用；旧版无账号标记的会话仍对所有账号可见。

网页默认设置会保存到 `settings.local.json`，只包含模型、联网搜索、深度思考、思考挡位、是否显示 Thinking、是否自动删除等非敏感选项，不包含 token。点击“保存当前为默认设置”后立即生效，下次启动自动恢复。

## 安全说明

- 本地网页不接收账号密码；密码只输入到官方 Chrome 登录页。
- 完整 token、captcha 不会显示在页面或文档中。
- `profiles.local.json` 包含加密后的登录态，请不要发给别人或提交到仓库。

## 运行日志

服务全程输出结构化运行日志，三路同写：`logs/glm2api.log`（UTF-8，8MB×5 轮转）、stderr（控制台）、内存环形缓冲（1500 条，面板「日志」页可实时查看，支持级别/关键字过滤与自动刷新）。

- 请求访问日志：`[rid] REQ/RES 方法 路径 -> 状态码 (耗时 ms)`；面板轮询端点（`/api/status` 等）静默，仅在失败时记录。
- 调用链事件（JSON `{"state": ...}` 风格，带 `rid` 关联同一次请求）：
  - `upstream_call_start`：模型、思考挡位、工具数、tool_choice、上下文模式（inline/file）、prompt 字数、是否复用会话、是否自动删除；
  - `upstream_chat_created` / `upstream_chat_reused`：上游会话创建或复用（只含 `chat_id_fp` 指纹）；
  - `fresh_captcha_*`：验证码求解全过程（backend、attempt、耗时、降级）；
  - `context_file_uploaded` / `context_file_cache_hit`：历史拆分附件上传（label=history/tools、文件名、字数）与缓存命中；
  - `context_upload_degrade_enter/skip`：Mode B 连续失败降级到模式 A 及窗口内跳过；
  - `tool_calls_parsed`：解析出的工具调用名与来源（output/thinking）；`tool_choice_missing_retry`：required 模式重采样；
  - `upstream_stream_open/done`：SSE 开始与结束（事件数、耗时）；`upstream_http_error` / `upstream_connect_failed`：上游错误摘要；
  - `upstream_chat_deleted` / `auto_delete_failed`：自动删除结果；
  - `server_started` / `server_stopped`：启动配置与退出。
- 协议 handler 抛出的异常会带完整堆栈写入日志；HTTP 层未捕获异常兜底记录并返回 500。
- 日志级别用 `--log-level {DEBUG,INFO,WARNING,ERROR}` 调整（默认 INFO）。
- 安全约定：token、captcha、API key、user_id、chat_id 等敏感或可关联标识一律只记录 sha16 指纹，不落全文；`logs/` 目录请勿提交仓库。

## 上游繁忙自动重试

上游偶发返回流首错误 `MODEL_CONCURRENCY_LIMIT`（"当前模型使用人数较多，请稍后再试或切换到其他模型。"，HAR 实测），典型场景是上一条消息的上游会话尚未完全释放时下一条请求就到达（Claude Code 连续工具调用时常见）。项目会内部消化这类瞬时错误：

- 仅当回答尚未输出任何内容（流首错误）且本次调用自建会话时重试：删除被中断的空会话 → 等待 → 新建会话重发；对客户端完全透明，错误不透传。
- 默认等待 **3 秒**、最多尝试 **3 次**；可在面板「设置」页调整"繁忙重试等待（秒）"（0–120）与"繁忙重试次数上限"（1–6），保存到 `settings.local.json`。
- 复用会话（continue/reuse）模式不内部重试（换会话会破坏"保持当前会话 ID"语义），错误原样透传。
- 可重试错误：`MODEL_CONCURRENCY_LIMIT`、繁忙/限流类关键词、HTTP 429/500/502/503/504，以及**验证码被上游拒绝**（`FRONTEND_CAPTCHA_REQUIRED` F018/F019，见下文验证码时效）。鉴权、内容审核类错误一律原样透传，不做内部重试。
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
- **HEAD 探活**：支持 HEAD 方法（按 GET 语义应答、只省略响应体），并新增 `GET/HEAD /api/hello` 探测端点；此前 `HEAD /api/hello` 返回 501 Unsupported method 会让探测方误判服务不可用（日志 03:58 实测）。
- **自动删除异步化**：删除上游会话实测需 1-4 秒，已从响应路径移到后台线程池（2 worker），客户端不再等待。响应中 `chat_deleted` 恒为 `false` 并以 `chat_delete_pending: true` 标注；结果看日志（`auto_delete_scheduled` / `auto_delete_completed` / `auto_delete_failed`），删除失败可用 `POST /api/chat/delete` 手动补删。
- **繁忙重试覆盖会话创建**：`POST /api/v1/chats/new` 阶段的 429/5xx 瞬时错误同样进入"等待后重试"循环（日志 `upstream_transient_retry` 带 `stage=new_chat`）。
- **删除 404 视为成功**：会话已不存在（创建即被拒/已删除）不再报 `failed_chat_cleanup_error` 噪音。
- **429 限流响应**：本地并发槽满时返回的 429 带 `Retry-After: 3` 头，客户端（如 Claude Code）可据此退避。
- **线程命名**：请求线程前缀 `proxy-`、删除线程前缀 `autodel-`、预解线程 `captcha-prefetch`，日志线程字段一目了然。

实测效果（glm-5.2 短请求，`--fresh-captcha-browser` 开启）：冷启动 15.0s → 池命中 6.6s；面板/CLI/协议调用全部受益。

## 内存说明

HAR 是浏览器网络日志，不只是登录态，可能包含接口响应、SSE、埋点和静态资源记录；几十 MB 属于正常现象。

服务模式启动时只提取必要登录态字段。为了降低常驻内存，程序会：

- 优先复用已保存 profile，遇到相同 `har_fp` 时跳过重复解析 HAR。
- 需要解析 HAR 时使用短生命周期 worker，主服务只保留提取后的登录态对象。
- 网页上传 HAR 使用 raw 文件上传，不再在浏览器 JS 中 `file.text()` 后 JSON 包装；服务端直接流式写入临时文件，再交给 worker 提取。
- 聊天附件同样先流式落到临时文件，再以 multipart 分块转发到上游，不会同时在内存保留“原始文件 + multipart 完整副本”。
- 临时 HAR/附件在请求结束后立即删除。

## 本地访问边界

服务默认只监听 `127.0.0.1`，且默认不发送 CORS 响应头，因此其他网页不能直接读取或调用本机的登录态接口；本机网页与原生 OpenAI 客户端不受影响。

如确实需要局域网访问，必须同时显式确认远程绑定并配置 API Key：

```powershell
python .\glm2api.py --serve --host 0.0.0.0 --allow-remote --api-key "your-local-secret" --cors-origin http://your-ui.example
```

非回环地址缺少 `--allow-remote` 或 API Key 时服务会拒绝启动。局域网暴露会让持有密钥的客户端以当前账号向上游发送请求，请只用于受信任网络；不要使用 `--cors-origin *`，除非你明确理解其影响。

回环地址也可按需为外部客户端启用鉴权：在面板「接口」页启用/更新/清除服务端 API Key（无需重启）。密钥会用 Windows DPAPI 加密保存到 `apikey.local.json`，仅供本机当前用户解密；修改或清除已启用密钥时需输入当前密钥。CLI / 环境变量配置的密钥优先级更高，此时面板会显示为只读。

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
- `GET /healthz`：本地服务健康状态，不泄露登录态。
- `GET /api/status`：状态和当前账号脱敏摘要。
- `GET /api/settings`：读取本地默认设置。
- `POST /api/settings`：保存本地默认设置。
- `GET/POST /api/settings/api-key`：读取或配置服务端 API Key（DPAPI 加密保存到 `apikey.local.json`；修改/清除已启用密钥需提交 `current_key`）。
- `GET /api/auth/profiles`：列出已加载 profile 和保存状态。
- `POST /api/auth/browser-login`：打开 Chrome 手动登录并采集 profile。
- `POST /api/auth/har`：上传 HAR 并切换。
- `POST /api/auth/token`：粘贴会话 token（三段式 JWT）添加账号并切换，body 为 `{"token":"...","label":"可选备注"}`。
- `POST /api/auth/switch`：切换 profile。
- `POST /api/auth/remove`：删除指定的本机已保存 profile。
- `POST /api/auth/compact`：手动清理同一账号的重复 profile。
- `POST /api/files/upload`：上传单个附件到上游，raw body；文件名放 query `filename`。
- `POST /api/files/cleanup`：删除已上传但未进入成功会话的孤儿附件，body 为 `{"files":["<file_id>", ...]}`；仅作尽力而为，上游无删除端点时会静默跳过。
- `POST /api/chat`：网页聊天 SSE 接口。
- `POST /api/chat/cancel`：停止当前网页聊天请求，body 为 `{"assistant_message_id":"<UUID>"}`。
- `POST /api/chat/delete`：删除指定上游会话，body 为 `{"chat_id":"<UUID>"}`。
- `GET /api/history/chats?page=1`：上游账号历史对话列表（分页）。
- `GET /api/history/chat?id=<UUID>`：单条历史对话详情与消息链。
- `POST /api/history/delete`：删除上游历史对话，body 为 `{"chat_id":"<UUID>"}`。
- `GET /api/history/records?page=1`：请求镜像记录摘要列表（每条请求一条，新→旧）。
- `GET /api/history/record?id=<req_id>`：单条请求镜像完整记录（出站消息、实际实发模式、内部附件清单/正文、上游输入框、回复与思维链）。
- `POST /api/history/record/delete`：删除单条请求镜像记录，body 为 `{"id":"<req_id>"}`。
- `POST /api/history/clear`：清空全部请求镜像记录。
- `GET /api/logs?lines=300&level=&text=`：读取最近运行日志（内存环形缓冲，`lines` 1-2000，`level` 可选 INFO/WARNING/ERROR 下限，`text` 为关键字过滤）。
- `GET /v1/models` / `POST /v1/chat/completions`：OpenAI Chat Completions 兼容接口。
- `POST /v1/responses`、`GET /v1/responses/{response_id}`：OpenAI Responses 兼容接口。
- `POST /anthropic/v1/messages`：Anthropic Messages 兼容接口。
- `POST /anthropic/v1/messages/count_tokens`：本地兼容的输入 token 估算接口。

## 命令行快速发送

```powershell
python .\glm2api.py --har ..\chat.z.ai.har --prompt "你好" --model glm-5.3 --fresh-captcha-browser
python .\glm2api.py --har ..\chat.z.ai.har --prompt "你好" --model glm-5.3-flash --fresh-captcha-browser
python .\glm2api.py --har ..\chat.z.ai.har --prompt "今天有什么新闻" --model glm-5.3 --web-search --fresh-captcha-browser
python .\glm2api.py --har ..\chat.z.ai.har --prompt "只输出 OK" --model glm-5.2 --no-thinking --fresh-captcha-browser
```

`--fresh-captcha-browser` 模式使用常驻复用的 headless Chrome：服务启动后只拉起一次浏览器，后续每条消息复用同一页面获取验证码，不再每次开/关浏览器；页面或验证失败会自动重建并重试一次。空闲 `--captcha-worker-idle-sec` 秒（默认 900）后自动关闭浏览器释放内存，下一条消息再自动拉起。网页“浏览器登录”仍为独立的一次性窗口，不受影响。

验证码求解器由 `--captcha-mode auto|happydom|browser` 控制（默认 `auto`）：`auto` 优先调用本地 happy-dom Node 解码器（`captcha_happy.mjs`，同目录需有 `node` 与 `node_modules`，单次约 5–10 秒、不依赖真实浏览器），失败再回落到 Playwright headless Chrome；上游对 headless 浏览器的挑战拦截明显更频繁，因此 auto 模式最稳。每次请求内建两次尝试（间隔 20 秒）；`--captcha-timeout-ms` 默认 75 秒。当账号没有存量验证码时，失败冷却不会再抑制浏览器采集（裸发请求必被 `FRONTEND_CAPTCHA_REQUIRED` 拒绝，重试才是正解）。`/api/status` 的 `captcha_solver` 字段可查看当前模式。

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

每个已保存账号最多允许 3 个同时进行中的上游生成（网页聊天、OpenAI Chat Completions、Responses、Anthropic Messages 共用同一槽位池）；第 4 个并发请求会立即收到 HTTP 429 `chat_slot_busy`，`/api/status` 的 `chat_busy_count` 可查看当前占用数。槽位绑定到申请时所在的 profile：即使生成期间在面板切换了账号，旧的进行中请求结束后也会正确释放原账号槽位，不会造成“永久忙碌”。上游流一旦消费完（或确认失败），对应槽位会立即释放，后续的自动删除对话等收尾操作不再占用槽位，因此上一个请求刚结束时立刻发起新请求不会误报 429。

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
- 支持 `tool_choice` 的 `auto`、`none`、`required` / `any` 和指定函数；`required` 未提供函数定义会直接返回 400。
- 支持 OpenAI 的 `parallel_tool_calls: false` 与 Anthropic 的 `disable_parallel_tool_use: true`。
- 下一轮将工具执行结果作为 OpenAI `role: "tool"`、Responses `function_call_output` 或 Anthropic `tool_result` 传回，即可继续工具链。
- OpenAI / Responses 里声明的 `web_search*` 内置工具会映射为当前上游的联网搜索开关；自定义函数仍由客户端负责执行。

工具调用提示词与参考项目 dkceshi 对齐：英文 DSML 指令（7 条规则 + 5 个反例 + 按当前请求工具动态生成的正确示例），并保留 `required` / 指定工具的强制调用句与 Read 类工具的缓存守卫提示；解析同时兼容 glm2api JSON 外壳、标准 XML 和 DeepSeek 风格尾部竖线变体。代码围栏内示例不会被识别为调用，未声明工具会被拒绝，声明为 `string` 的参数会在模型误输出对象 / 数组时转为紧凑 JSON 字符串。工具调用标记在上游流中会先缓冲和解析，不会泄露给 SDK；普通无工具文本仍保持流式转发。

## 上下文文件封装

默认不进行历史文件拆分：三种协议都会把归一化上下文直接放入上游网页输入框。只有明确开启字段或选择 `-forcehistory` 模型时，才按参考项目格式拆成**附件 + 一个聊天框**发送：附件一为对话历史转写文件（`[system]` / `[user]` / `[assistant]` / `[tool]` 标签，assistant 推理使用 `[reasoning_content]` 块，工具结果带 `[function=... invocation_id=...]` 头）；附件二为工具声明文件（`name/description/schema` 逐项），仅当前 `tool_choice` 允许调用工具时存在，`none` 不上传该附件，指定函数时只写入该函数。聊天框按参考项目的 System→User 语义顺序扁平化为 Z.ai 原生单输入框文本：工具指引与 DSML 契约在前，`The attached file holds the earlier conversation. ...` 续写提示在最后；未启用当前轮工具时，输入框就只有这句续写提示。**Output integrity guard** 只由输入框内仍生效的上下文触发：拆分成功后旧 tool 历史已在附件中，不会误触发；当前轮允许调用工具时仍前置守卫。附件使用随机短文件名；同账号同内容 10 分钟内复用已上传文件。内部附件按一个完整包原子组装：任一上传失败时不会把半个附件包送入 completion，而是清理已上传的孤儿文件并回落为整段上下文直传。「历史」页的发送概览和实发详情按实际结果展示直传、拆分或降级，并记录内部附件的真实文件名/正文与真实输入框文本。

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

传入 `false` 可保留 chat。取消、上游错误、工具策略校验失败不会自动删除；自动删除失败不会丢弃已生成答案，网页会显示失败信息，协议入口会写入脱敏本地日志。

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
```
