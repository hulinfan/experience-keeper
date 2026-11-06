# Codex 转录解析契约

| 项 | 内容 |
| --- | --- |
| 核对版本 | codex-cli 0.156.1 |
| 解析函数 | `transcripts.parse_codex` |
| 转录路径 | `~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl`，由 `Stop` payload 的 `transcript_path` 给出 |
| 格式 | JSONL，记录类型：`event_msg`（对话与工具）、`response_item`（模型产出与工具结果）、`token_usage_record`（用量）、`turn_context` / `world_state` / `session_meta`（上下文） |

## 两代写法

codex-cli 0.156 起换了记录形状，两代在同一个 `~/.codex/sessions` 目录下并存，按文件内实际字段自动分派，没有版本号可读。

| 内容 | 旧代（≤ 0.155） | 新代（0.156 起） |
| --- | --- | --- |
| 用户提问 | `event_msg` 的 `payload.type == "user_message"` | `event_msg` 的 `payload.item.type == "UserMessage"` |
| 终局回复 | `event_msg` 的 `payload.type == "agent_message"` | `event_msg` 的 `payload.item.type == "AgentMessage"` |
| 工具调用 | `response_item` 的 `function_call` / `custom_tool_call` | 同一条 `item_completed` 里的 `CommandExecution` / `FileChange`，`response_item` 里另有一份重复的 `custom_tool_call` |
| 压缩事件 | `event_msg` 的 `payload.type == "context_compacted"` | `event_msg` 的 `payload.item.type == "ContextCompaction"` |
| 用量 | `type: token_usage_record`，两代相同 | 同左 |

## 轮次边界

| 情况 | 处理 |
| --- | --- |
| 有 `turn_id` | 按本轮标识过滤出记录，并据此推出轮次序号 |
| 无 `turn_id`（原生 Stop 钩子常态） | 取最后一条用户消息之后的所有记录作为本轮 |

本轮标识的落点两代相同、两层不同：`event_msg` 写在 `payload.turn_id`，`response_item` 藏在 `payload.internal_chat_message_metadata_passthrough.turn_id`（实测 772/772 条 response_item 都带）。只读顶层会把整轮工具丢掉，因此两处都取。

## 字段来源

| 候选字段 | 来源 |
| --- | --- |
| `user` | 优先 `event_msg` 权威层；该角色在权威层完全缺失时才退回 `response_item` 的 `message.role == "user"` |
| `assistant` | 同上，`role == "assistant"` |
| `tools` | 本轮有 `CommandExecution` / `FileChange` 条目时只取这些条目，否则退回 `response_item` 的 `function_call`、`custom_tool_call`、`local_shell_call` 及其 `*_output` |
| `tool_calls` | 每条工具条目计一次 |
| `tokens` | `type: token_usage_record` 的用量 |
| `outcome` | 命中 `turn_aborted` 记 `aborted`；没有终局回复记 `incomplete` |
| `compaction_count` | 压缩事件计数 |

**为什么提问不直接取 `response_item`**：新代每个会话开头与压缩后都会把 `AGENTS.md`、skills 清单等指令文本写成一条 `role == "user"` 的 `message`（实测 17 条 user 消息对 16 条 `UserMessage` 条目），直接取会把这轮提问认成注入文本。权威层缺失才兜底，兜底值宁可粗也不能错。

**为什么工具按轮二选一**：新代一次 `exec` 脚本会派生出多条 `CommandExecution`（实测 212 条脚本对应 359 条命令加 37 条改文件），`response_item` 那份是脚本级、`item` 那份是命令级，两份都取会把同一次调用数两遍。同一轮里 item 条目齐备（实测 13/13 轮齐备）就用 item，缺了才退回脚本级，读类脚本不至于整轮没有工具记录。

## 工具状态

新代有权威字段：`CommandExecution.exit_code`，`0` 记 `ok`，非 `0` 记 `error`，输出文本进 `tool.error` 与 `tool.result`。`status` 字段只有 `completed` 一类的过程态，不作成败依据；命令被中断时没有 `exit_code`，记 `unknown`，输出文本只进 `tool.error_text_hint`（`tool.result` 同样保留）。

`FileChange` 没有成败字段（只有 `status: completed`），一律 `unknown`。工具名：命令记 `exec`（与该轮 `custom_tool_call.name` 一致），改文件记 `file_change`。

旧代没有权威字段：`function_call_output` 实测只有 `{type, id, call_id, output, internal_chat_message_metadata_passthrough}`，一律记 `unknown`。

后果：新代产出 `error_recovered`（同名工具先 `error` 后 `ok`）、`fallback_switch`；旧代不产出，只靠 `retry` 命中同签名重复调用。

## 丢弃内容

- `session_meta`、`turn_context`、`world_state`、`thread_settings_applied`、`task_started` / `task_complete`：只用于识别会话与阶段，不进候选。
- reasoning 类记录（`response_item/reasoning`、`item_completed` 的 `Reasoning`）。
- 工具输出里的结构化对象（如 `{"task_name": ...}`）按纯文本展平后截断（1200 字符）存 `tool.result`，供判据取词；成败仍只看 `exit_code`。

## 已知不可得

- 旧代的工具成败（无字段，见上）。
- `FileChange` 的成败与失败原因。
- 压缩摘要正文：只数次数，拿不到摘要内容。

## 兜底与验证

- 文件不存在、无记录、既无用户消息也无回复 → 返回 `None`，采集入口记 `parse/no_transcript` 诊断。
- 用例：`TranscriptTest.test_codex`（旧代）、`TranscriptTest.test_codex_item_completed_schema`、
  `TranscriptTest.test_codex_command_without_exit_code_stays_unknown`、
  `NativeHookTest.test_codex_without_turn_id_windows_last_turn`（旧代无 turn_id）、
  `NativeHookTest.test_codex_item_schema_without_turn_id_recovers_status`、
  `ToolStatusAuthorityTest.test_text_marker_does_not_decide_status`。

## 排障

`experience.py doctor` 里出现成片的 `parse/no_transcript codex` 就是本端格式又变了：按「两代写法」表重跑统计，
补齐新形状并把新代写进上表；只看 `payload.type` 是不够的，新代的对话与工具都在 `payload.item` 里。
