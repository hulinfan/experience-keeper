# dsh 转录解析契约

| 项 | 内容 |
| --- | --- |
| 核对版本 | 0.2.0-rc.2（`dsh --version`；事件形状对照 `@deepseek-ai/dsh-session` 的 `SessionEventMap` 与 `dsh-acp` 的 `toolResultUpdate`） |
| 解析函数 | `transcripts.parse_dsh` |
| 转录来源 | **不是客户端原生文件**：`adapters/dsh/experience-keeper.mjs`（cordis 插件）按轮落盘的归一化事件流 |
| 转录路径 | `~/.agents/experience/transcripts/dsh/<sessionId>-t<turn>.jsonl`，由插件落盘后把该路径当 `transcript_path` 交给采集脚本 |
| 接线方式 | `python scripts/install.py --client dsh --apply`：复制适配器到 `~/.dsh/.agent-presets/liangshen/` 并向 `agent.cordis.yml` 追加插件行 |

## 为什么不用原生日志

dsh 的会话日志由持久化后端写成
`~/.dsh/sessions/<projectKey>/<sessionId>/session.vN.jsonl.zstd`，路径拼接与 zstd 压缩
都属于后端内部实现，`Session` 对象不暴露文件路径。插件改为只搬运事件，把归一化结果落成纯文本 JSONL。

## 事件词表

插件写、解析器读，两侧逐字对应，由 `AdapterContractTest` 双向锁住：

| 事件（归一化） | 来源 dsh 事件 | 读的字段 |
| --- | --- | --- |
| `user_message` | `user/message` | `data.content`（`{type:'text',text}` 块数组） |
| `agent_message` | `assistant/message` | `data.message.content` |
| `tool_call` | `tool/call` | `data.name`、`data.arguments`（模型产出的原始 JSON 字符串，解析失败保留 `{raw: ...}`） |
| `tool_result` | `tool/result` | `data.message.content` 拼正文、`data.message.isError` 判成败；输出原文写 `output`，`error` 只在失败时写 |
| `turn_aborted` | `turn/end` 且 `data.reason.kind !== 'completed'` | `data.reason`（`completed` / `aborted` / `blocked` / `error` / `max-tokens`） |
| `compaction` | `compaction/end`（`dsh-compaction-basic` 追加） | 无，只计数 |

未知事件名一律跳过，不猜、不落候选。

## 轮次边界

一份文件就是一轮（插件按 `turn/end` 切分并落盘），行内不再切轮次。
压缩标记行写在轮首，数量等于本轮之前已发生的压缩次数，解析器的 `compaction_count` 因此与 payload 一致。

## 工具状态

`is_error` 由插件写入：`data.message.isError === true`（dsh 0.2 的 `tool/result` 形状，与 `dsh-acp` 的
`toolResultUpdate` 一致），或事件带结构化 `data.error`；早期「块级 `isError` + 嵌套 `content`」的形态一并容忍。
字段缺失时记 `unknown`。

## 丢弃内容

- 图片、文件引用等非文本块：插件只拼接 `type: text` 的块。
- 客户端日志里的持久化信封、时间戳等字段。
- 未出现在上表里的事件。

## 已知不可得

- 原生日志里的任何字段（拿不到原生文件）。
- 工具的退出码：客户端事件里没有，只有 `isError` 布尔。
- 与原生转录的行号对照：`evidence.lines` 指向的是归一化文件的行号，不是 dsh 日志的行号。

## 托管与升级

预设目录 `~/.dsh/.agent-presets/liangshen/` 由 `@deepseek-harness-tui/dsh-tui` 托管
（`.dsh-tui-managed.json` 记 owner 与 revision）。**dsh-tui 升级时可能重写 `agent.cordis.yml`，
追加的插件行会丢**；适配器副本也可能落后于 skill 真身。两者都靠 `install.py --check` 的
「dsh 采集插件」「dsh 适配器为当前版本」两项发现，升级后重跑 `install.py --client dsh --apply` 即可。
插件在会话启动时装载，改完适配器需要新开会话或重载预设才生效。

## 兜底与验证

- 文件不存在或没有任何事件 → 返回 `None`，采集入口记 `parse/no_transcript` 诊断。
- 插件写文件失败时会把 `transcript_path` 传空，`resolve_session_file("dsh", ...)` 会去默认目录找同名文件；
  仍找不到就记诊断。
- 用例：`TranscriptTest.test_dsh`、`AdapterContractTest.test_dsh_adapter_vocabulary_is_supported`、
  `DshAdapterTest`（真跑插件：`tool/result` 形状、转录落盘、payload 契约、解析后命中判据）。
