# 转录解析契约

每个客户端一份契约文档，写清「读哪些字段、怎么切轮次、丢弃什么、什么拿不到」。
解析器改动必须同步对应文档，否则契约与实现会分叉。

| 客户端 | 解析函数 | 转录来源 | 工具状态字段 | 核对版本 |
| --- | --- | --- | --- | --- |
| [claude](claude.md) | `transcripts.parse_claude` | `~/.claude/projects/<slug>/<uuid>.jsonl` | `tool_result.is_error` | 2.1.270 |
| [codex](codex.md) | `transcripts.parse_codex` | `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl` | 新代 `CommandExecution.exit_code`；旧代无 | codex-cli 0.156.1 |
| [dsh](dsh.md) | `transcripts.parse_dsh` | 适配器落盘的归一化事件流 | `is_error`（适配器写入） | 未取到 |
| [codemaker](codemaker.md) | `transcripts.parse_codemaker` | `~/.codemaker/session-store/v1/<hash>/<uuid>.json` | `isError`（实测基本不存在） | 1.4.3-prod-0.1.75-26.4.29 |
| [pi](pi.md) | `transcripts.parse_pi` | `~/.pi/agent/sessions/--<path>--/<ts>_<uuid>.jsonl` | `isError`、`exitCode` | 0.84.2 |

共同约定：

| 约定 | 行为 |
| --- | --- |
| 坏行 | JSONL 逐行解析，坏行跳过，不影响整份文件 |
| 空结果 | 解析不出内容返回 `None`，由采集入口记 `no_transcript` 诊断，不落候选 |
| 工具状态 | 只取结构化字段，取不到记 `unknown`；输出不参与成败判定 |
| 工具输出 | 一律截断保留进 `tools[].result`（1200 字符），它是判据型信号的第三个取词面：实测数值、状态旧值、幂等差异都从这里取。读文档与读源码的结果是转述，解析照存，判定侧按「判据取词面」排除 |
| 文本截断 | 工具输出 1200 字符，工具错误原文 400 字符，文本线索 200 字符 |
| 长内容 | 图片、附件、文件引用等非文本块一律丢弃，避免灌大候选 |

## 新增一端时怎么做

1. **先摸清契约再写代码。** 用只读脚本统计真实转录里的记录类型、字段出现率，把结论写进本文档；
   不要凭猜测写解析逻辑（`letta-ai/trajectory` 的 `PARITY.md`、`SOURCE_VERSION_AUDIT.md` 就是这个思路）。
2. **确认轮次边界。** 找到「一条用户提问开一轮」的可靠标记；原生钩子不带 `turn_id` 时还要能按结构推出轮次序号。
3. **确认工具状态字段。** 有结构化布尔就用它，并在本文档写下实测覆盖率；没有就记 `unknown`，不要用文本推断。
4. **写脱敏样本用例。** 在 `tests/test_experience.py` 的 `TranscriptTest` 加样本解析用例，
   样本要覆盖：正常一轮、工具失败一轮、压缩标记、坏行、空文件。
5. **接线。** 走 `install.py` 的对应端安装函数，并在 `docs/deployment.md` 补冒烟测试命令。
6. **更新本页表格与 `SKILL.md` 的参考文档表。**

客户端升级后格式变了怎么办：重跑第 1 步的统计，对照本文档「丢弃内容」与「已知不可得」两节，
改解析器并更新核对版本号。
