# 协议兼容说明

## 目的与数据流

`glm2api` 将 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 的请求先归一化为同一份内部会话，再通过既有的 GLM 上游通道发送。调用客户端获得的仍是它原先选择的协议格式。

```text
客户端协议请求
  -> 归一化消息 / 工具 / 选项
  -> 历史 / 工具转写文件（需要时上传为上游附件，随机短文件名）
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

服务只使用当前已登录的本地 profile 调用上游。`Authorization`、`x-api-key` 和 `anthropic-version` 会被接受，以兼容常见 SDK；它们不是本地账号登录方式，也不会覆盖当前 profile。

本地 API Key 可通过面板“API 调试”页启用/更新/清除（DPAPI 加密保存到 `apikey.local.json`），也可用 `GLM2API_API_KEY` 或 `--api-key` 在启动时配置（后者优先级更高）。启用后，所有 API 入口（除 `/healthz` 和最小化 `/api/status` 外）都会校验本地密钥，可传 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`。

本地可观测性端点同样受 API Key 保护：`GET /api/metrics?hours=24` 只返回请求状态、耗时、Token、模型/入口与运行态聚合值，不包含提示词/回复正文；统计页可直接导出同结构的脱敏 JSON。`GET /api/logs` 默认同时返回兼容文本行和结构化事件，可按 `level`、`kind`、`state`、`rid`、`text` 组合过滤；`format=structured&after_seq=<seq>` 支持无重复文本数组的游标增量读取，游标失效时以 `cursor.reset_required` 指示并返回当前快照。

服务默认只绑定回环地址。绑定 `0.0.0.0` 或其他非回环地址时，必须同时传 `--allow-remote` 并配置 API Key，否则服务拒绝启动。

## 上下文文件策略

| 入口 | 默认行为 |
| --- | --- |
| Chat Completions | 直接发送归一化后的上下文，不自动上传历史文件 |
| Responses | 直接发送归一化后的上下文，不自动上传历史文件 |
| Anthropic Messages | 直接发送归一化后的上下文，不自动上传历史文件 |

可以通过 `context_as_file`、`current_input_file`、`history_as_file` 或 `forcehistory` 明确开关；模型名含 `-forcehistory` 时总是封装。该后缀可与 `-nothinking` 叠加，且只会在模型 ID 结尾生效。上下文文件最大 4 MiB，超过限制会返回 400。历史与工具定义分别使用随机短数字文件名（`111.txt` 风格）上传；同账号同内容 10 分钟内复用已上传的 file id。生成文件只在本地临时目录存活到上游上传结束，随后立即删除。

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
- OpenAI 的 `parallel_tool_calls: false` 与 Anthropic 的 `disable_parallel_tool_use: true` 都会限制为至多一个调用。
- 指定函数（forced）始终只返回该函数的一个调用，即使模型重复输出调用块。
- OpenAI/Responses 中 `web_search*` 类型的内置工具会打开 GLM 的联网搜索；其余工具全部是客户端函数。
- glm2api 只解析和返回调用请求，绝不会执行工具内容。这包括命令、文件、网络、数据库和 MCP 风格工具。

工具调用提示词与参考项目 dkceshi 对齐（英文 7 条规则 + 5 反例 + 动态正确示例），并按“工具 schema / 调用规则 → 对话历史”的 System→User 语义顺序放入上游输入框。内部推荐模型在需要调用时输出半角管道 DSML XML 外壳：`<|DSML|tool_calls> → <|DSML|invoke> → <|DSML|parameter>`。解析同时兼容标准 XML、早期 glm2api JSON 外壳、DeepSeek 尾部竖线变体、常见全角标签符号和缺失但可安全补齐的外层结束标签；XML 子节点可还原对象/数组，参数 JSON 可修复尾逗号、未加引号的键和无效路径反斜杠。代码围栏内的示例不会触发解析；未声明工具、强制工具之外的名称不会进入调用结果。该标记会在转协议前移除；普通文本不会看到它，流式工具请求会等待完整语义块解析后再作为原生工具块发送。

生成的工具调用 ID 按协议使用原生前缀和标准长度：OpenAI Chat Completions 为 `call_` + 24 位十六进制，Responses 为 `fc_`，Anthropic 为 `toolu_`，避免严格 SDK 校验拒绝。截断或畸形的工具块在移除时会解开 CDATA 并删除残留的 `invoke` / `parameter` 标签，只保留其中的参数文本，不会向客户端泄漏适配器外壳。若上游明显尝试了工具块但无法转换，或未满足 `required` / forced，代理会清理首轮上游会话、附加一条精确纠错提示并只重新采样一次；重试仍失败才返回错误。`required` / `any` 模式下若调用出现在思考流中，即使正文有文字也会回退解析；`auto` 模式仅在正文为空时才从思考流提取，防止把思考示例误判为真实调用。

### 客户端工具循环

当客户端收到工具调用后，应自行执行受信任的本地函数，再发送下一轮结果：

| 协议 | 本轮工具调用 | 下一轮工具结果 |
| --- | --- | --- |
| Chat Completions | `choices[0].message.tool_calls` | `{"role":"tool","tool_call_id":"...","content":"..."}` |
| Responses | `output` 内 `function_call` | `{"type":"function_call_output","call_id":"...","output":"..."}` |
| Anthropic | `content` 内 `tool_use` | user content 内 `{"type":"tool_result","tool_use_id":"...","content":"..."}` |

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

请求中传入 `false` 即保留该会话。启用自动删除时，客户端断开、上游流错误和工具格式重试也会异步回收已经创建的失败会话；同一上下文只调度一次，避免重复删除。清理失败不会撤销已经生成的协议响应：网页接口会携带失败说明，协议接口会输出只含 chat ID 指纹的本地诊断日志。

## 流式事件

Chat Completions 返回标准 `data: {...}` 片段并以 `data: [DONE]` 结束。Responses 会发送 `response.created`、`response.in_progress`、输出项/文本/函数参数事件以及 `response.completed`；开启 `include_thinking` 且上游思考可用时，还会以标准 `reasoning` 输出项（索引 0）发送 `response.output_item.added`、`response.content_part.added`、`response.reasoning_summary_text.delta/.done`、`response.content_part.done`、`response.output_item.done`，思考摘要与 completed 对象中的 `reasoning.summary` 一致。Anthropic 会发送 `message_start`、`content_block_start`、`content_block_delta`、`content_block_stop`、`message_delta` 和 `message_stop`。

带工具或 Anthropic thinking 的响应会在语义块完整后再写入 SSE，以确保调用端不会收到半截 JSON、内部工具标记、不完整 thinking 块，或格式重试前一轮的 reasoning。隐藏阶段每 10 秒最多发送一条标准 SSE 注释 `: keep-alive`（上游有事件推进时检查），降低反向代理/SDK 因下游空闲而断开的概率；无工具的普通文本继续边生成边转发。上游 SSE 分帧同时接受 LF 与 CRLF，并支持标准 `event:`/多 `data:` 字段。

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
