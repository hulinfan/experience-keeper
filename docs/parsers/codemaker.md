# codemaker 转录解析契约

| 项 | 内容 |
| --- | --- |
| 核对版本 | 1.4.3-prod-0.1.75-26.4.29 |
| 解析函数 | `transcripts.parse_codemaker` |
| 转录路径 | `~/.codemaker/session-store/v1/<哈希>/<uuid>.json`，插件按会话 id 在两个候选位置查找：`<哈希>/sessions/<uuid>.json` 与 `<哈希>/<uuid>.json` |
| 格式 | 单个 JSON 对象；消息数组的键名不固定，`_find_message_list` 按结构探测（实测在 `data.messages`） |

## 轮次边界

`role: user` 开一轮；`assistant`、`tool` 并入当前轮。`turn_id` 是数字时按第 N 轮取，否则取最后一轮。

## 字段来源

| 候选字段 | 来源 |
| --- | --- |
| `user` | `role: user` 消息的文本 |
| `assistant` | `role: assistant` 消息的文本 |
| `tools` | `role: assistant` 消息的 `tool_calls[].function`（`name` + `arguments`，字符串按 JSON 解析，失败保留原文）；`role: tool` 的文本存 `tools[].result`（截断 1200 字符） |
| 工具状态 | `role: tool` 消息的 `isError` |
| `tokens` | `role: assistant` 消息的 `usage` |
| `compaction_count` | `compression.totalCompressionsCount`，缺失时退回 `compression.compressionHistory` 长度 |

## 工具状态

`role: tool` 消息实测**基本不带状态字段**：主会话 177/177 缺 `isError`，子会话 81/82 缺。
契约：带则采信，不带记 `unknown`，输出文本只进 `error_text_hint`（原文照旧存 `tools[].result`）。

后果：本端不产出 `error_recovered` 信号；同签名重复调用仍由 `retry` 命中。

## 丢弃内容

- 顶层元信息：`chatRepo`、`chatType`、`chatWorkspace`、`ownerUser`、`topic`、各类同步时间戳等。
- 消息里的 `_originalRequestData`、`systemPrompt`、`specPrompt`、`requestText`、`context` 等字段。
- `<哈希>/subagents/` 下的子会话文件（只解析当前会话文件）。

## 已知不可得

- 工具成败（无字段，见上）。
- 压缩发生的行位置：只有计数。
- 多会话文件的归属关系：一个哈希目录下可能有多份会话，只按传入路径解析。

## 兜底与验证

- 文件不存在、JSON 解析失败、找不到消息数组、没有任何轮次 → 返回 `None`，采集入口记 `parse/no_transcript` 诊断。
- 用例：`TranscriptTest.test_codemaker`。
