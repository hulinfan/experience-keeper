# pi 转录解析契约

| 项 | 内容 |
| --- | --- |
| 核对版本 | 0.84.2 |
| 解析函数 | `transcripts.parse_pi` |
| 转录路径 | `~/.pi/agent/sessions/--<路径转义>--/<时间戳>_<uuid>.jsonl`，由扩展用 `ctx.sessionManager.getSessionFile()` 给出 |
| 格式 | JSONL，`type: message` 承载对话，`type: compaction` 标记压缩 |

## 轮次边界

`message.role` 为 `user` 时开一轮；`assistant`、`toolResult`、`bashExecution` 并入当前轮。

## 字段来源

| 候选字段 | 来源 |
| --- | --- |
| `user` | `role: user` 消息的文本 |
| `assistant` | `role: assistant` 消息里 `type: text` 块的最后一段 |
| `tools` | `role: assistant` 消息里的 `type: toolCall` 块（`name` + `arguments`）；结果文本存 `tools[].result`（截断 1200 字符） |
| 工具状态 | `role: toolResult` 的 `isError`；`role: bashExecution` 的 `exitCode` 与 `cancelled` |
| `tokens` | `role: assistant` 消息的 `usage` |
| `compaction_count` | `type: compaction` 条目计数（扩展另外按 `session_compact` 事件累加，取两者较大语义一致值） |

## 工具状态

| 来源字段 | 实测 | 语义 |
| --- | --- | --- |
| `toolResult.isError` | 175/175 都带该键 | 显式布尔，直接采信 |
| `bashExecution.exitCode` | 逐条判断 | 是整数时 `0` 记 `ok`、非 `0` 记 `error` |
| `bashExecution.cancelled` | 逐条判断 | `true` 记 `error` |

两者都缺时记 `unknown`，输出文本只进 `error_text_hint`；输出原文照旧截断存 `tools[].result`，供判据取词。

## 丢弃内容

- 非 `message`、非 `compaction` 的条目。
- `message.content` 里非 `text` 的块（图片、附件等）。
- `bashExecution` 的结构化字段除 `exitCode`、`cancelled`、`command`、`output` 之外的部分。

## 已知不可得

- 压缩摘要正文：只计数。
- 谁发起了 `bashExecution`（模型调用还是用户快捷键），消息里不区分。
- 工具调用的真实耗时：只有轮次首尾时间戳。

## 兜底与验证

- 文件不存在或没有任何轮次 → 返回 `None`，采集入口记 `parse/no_transcript` 诊断。
- 用例：`TranscriptTest.test_pi`、`HookTest.test_signal_turn_writes_candidate`、
  `ToolStatusAuthorityTest.test_pi_bash_execution_uses_exit_code`。
