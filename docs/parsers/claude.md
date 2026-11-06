# Claude Code 转录解析契约

| 项 | 内容 |
| --- | --- |
| 核对版本 | 2.1.270 |
| 解析函数 | `transcripts.parse_claude` |
| 转录路径 | `~/.claude/projects/<slug>/<uuid>.jsonl`，由 `Stop` / `SessionEnd` payload 的 `transcript_path` 给出 |
| 格式 | JSONL，一行一条记录，记录带 `type` |

## 轮次边界

| 记录 | 处理 |
| --- | --- |
| `type: user` 且内容是真实提问 | 开一轮 |
| `type: user` 且内容是工具结果等非提问（`_claude_user_text` 返回 `None`） | 并入上一轮 |
| `type: assistant` | 累积 token、取最后一段 `text` 作为终局回复、把 `tool_use` 块转成工具调用 |
| `type: summary` | 跳过 |

`turn_id` 是数字时按第 N 轮取，否则取最后一轮。

## 字段来源

| 候选字段 | 来源 |
| --- | --- |
| `user` | 真实提问记录的文本 |
| `assistant` | `message.content[].text` 的最后一段 |
| `tools` | `assistant` 记录里的 `tool_use` 块（`name` + `input`）；结果文本存 `tools[].result`（截断 1200 字符） |
| 工具状态 | 后续 `user` 记录里的 `tool_result.is_error` |
| `tokens` | `message.usage` |
| `duration_ms` | 轮次首尾行的时间戳差值，取不到记 0 |
| `compaction_count` | `isCompactSummary is True` 的记录数 |

## 工具状态

字段：`tool_result.is_error`，显式布尔。

实测 479 条 `tool_result`：`false` 281、`true` 32、**缺键 166**。
缺键的 166 条都是文件读写、搜索、任务类工具的成功载荷（`toolUseResult` 为结构化对象），
失败样本 32/32 都带 `is_error: true`。因此本端契约是**缺键按成功**（`absent_means_ok=True`）。

## 丢弃内容

实测出现过的记录类型里，以下全部丢弃：
`last-prompt`、`mode`、`permission-mode`、`attachment`、`system`、`cost-state`、
`atis-latch`、`file-history-snapshot`、`file-history-delta`、`queue-operation`、`ai-title`、`summary`。
内容块里非 `text`、非 `tool_use`、非 `tool_result` 的块也丢弃。

## 已知不可得

- 工具的真实退出码（只有 `is_error` 布尔）。
- 会话是否被用户打断：没有结构化字段，`outcome` 只在 `success` 与 `incomplete` 之间取。
- `compaction_count` 只数摘要记录，拿不到压缩边界对应的行号。

## 兜底与验证

- 文件不存在或没有任何轮次 → 返回 `None`，采集入口记 `parse/no_transcript` 诊断。
- 用例：`TranscriptTest.test_claude_picks_requested_turn`、`ToolStatusAuthorityTest.test_claude_absent_is_error_means_success`。
