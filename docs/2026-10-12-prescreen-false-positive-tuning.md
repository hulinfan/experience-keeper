# 预筛表误报调优建议（2026-10-12）

> 来源：一次 8 条候选的复盘（8 条中 6 条为噪声）。方案已实施，实施记录与偏差见 §10。
> 依据全部来自真实转录复跑（脚本 `prescreen.detect_signals` + 命中原文打印），非推测。

## 1. 观测：8 条候选的沉淀价值

| 轮次 | 命中信号（现状） | 沉淀价值 | 最终处置 |
| --- | --- | --- | --- |
| gwsvc `fixture` 季度总结文档 | env_hazard, perf_limit, cost_decision, protocol_edge | 无 | drop |
| gwsvc `fixture` core_leave 移植核查 | multi_source | **有** | 转条目 |
| platform `fixture` 仓库结构方案 | slow, review_rejected, cost_decision | 无（载体已覆盖） | drop |
| platform `fixture` 契约归属 | user_correction | 无（载体已覆盖） | drop |
| platform `fixture` 流程改造 | error_recovered, retry, retry_loop, multi_source, slow, silent_failure_suspected, state_machine_stuck | 无（载体已覆盖） | drop |
| platform `fixture` 克隆子仓验证 | error_recovered, retry, multi_source, slow, blast_radius_guard, asset_query, silent_failure_suspected | 无（载体已覆盖） | drop |
| platform `fixture` 提交改动 | error_recovered, retry, retry_loop, multi_source, slow, perf_limit, silent_failure_suspected | 无 | drop |
| platform `fixture` lb 文档 review | error_recovered, retry, retry_loop, multi_source, slow, cost_trap, cost_decision, protocol_edge | **有** | 转条目 |

**判据型信号（env_hazard / perf_limit / cost_decision / protocol_edge / cost_trap）在这批样本里 100% 误报。**

## 2. 逐信号归因（复跑命中原文）

| 轮次 | 信号 | 命中原文与来源 |
| --- | --- | --- |
| `fixture` | env_hazard | 用户消息「线上」（`prescreen.py:133-154` 词表含环境名词；`:727-730` 的用户消息分支只要求「首条工具 + 用户消息命中」） |
| `fixture` | cost_decision | 用户消息「成本」（`:1054` 只查词，且取词面含用户/assistant 正文） |
| `fixture` | protocol_edge | 现象词「NAT64」（用户模板）+ 闭环词「调整」（assistant 叙述），两者都不来自工具（`:777`） |
| `fixture` | perf_limit | 硬限词「耗尽」+ 量词「三条/三个」（`:1037` 用 `VALUE_UNIT_RE` 的量词分支当数值） |
| `fixture` | perf_limit | 硬限词「limit」+ 量词「25 个/38 个」（同上） |
| `fixture` | error_recovered / retry_loop | 工具名 `exec` 全轮同名，2 次失败实为两个 CLI 参数错误（`experience.py` 未知参数、`delivery.py unknown repository`）（`:645-673`） |
| `fixture` | multi_source | 读了 `cross-component-change`、`experience-keeper` 两个 `SKILL.md`（`:51` 的 `SKILL_PATH_RE` 认路径，`Get-Content` 读文档也算） |
| `fixture` | state_machine_stuck | 「轮询」= `Get-Content`/`workspace.py status` 的参数文本；「解决动作」= assistant 里的「依赖」一词（`:786-802`） |
| `fixture`/`fixture` | silent_failure_suspected | `INTENSIFIER_RE` 命中 assistant 正文与参数里的 `--check` / `--dry-run` |
| `fixture`/`fixture` | error_recovered | 唯一/首个失败是测试结果 FAIL（TDD 红→绿） |
| `fixture` | protocol_edge | 现象词「NAT64」+ 闭环词「MTU」（来自 `rg -n "...mtu..."` 的检索模式） |
| `fixture` | cost_trap | 量词「2026」（`ENV_MEASURE_RE:368-373` 第三分支 `\d{3,}` 把年份当量词）+ 口径词「阈值」 |
| `fixture` | perf_limit | 读 `.md` 文档得到「压测/限速」语境词（文档转述进入工具结果） |
| `fixture` | cost_decision | assistant 正文「配额」 |

## 3. 根因

| 编号 | 根因 | 波及信号 |
| --- | --- | --- |
| R-A | **取词面无来源分层**：判据从用户消息、assistant 叙述、读取 `.md` 的结果里取，把「转述」当「实测」 | env_hazard, cost_decision, protocol_edge, perf_limit, cost_trap |
| R-B | **闭环词来源不限**：闭环动作词从任何文本（含检索模式与叙述名词）取，现象+词汇即闭环 | protocol_edge, state_machine_stuck, silent_failure_suspected |
| R-C | **键粒度过粗**：`tool.name` 在 codex 只有一个 `exec`；`SKILL.md` 路径读取等于编排；量词/年份/提交号等于数值 | error_recovered, retry, retry_loop, multi_source, perf_limit, cost_trap |
| R-D | **无触发点的过程信号直接落候选**：`slow` 在信号表里对应触发点是「—」 | slow |

## 4. 调整清单

| 编号 | 信号 | 现状锚点 | 改法 | 预期 |
| --- | --- | --- | --- | --- |
| A1 | env_hazard | `prescreen.py:133-154`、`:727-730` | 词表拆成 `HAZARD_ACTION_KEYWORDS`（drop database/mkfs/iptables -F/ip route flush/wipefs…，只扫工具参数）与 `ONESHOT_ENV_KEYWORDS`（生产环境/线上/灰度/回滚…，写候选 `scene` 不落信号）；删除用户消息分支 | 环境叙述类清零，真高危动作保留 |
| A2 | protocol_edge | `prescreen.py:777` | 现象词取工具错误原文/非文档结果；闭环词只在**变更类调用参数**里匹配（排除 `rg/grep/Get-Content/git show` 等只读检索） | 文档写作/评审轮不再命中 |
| A3 | perf_limit / soft_limit | `:1035-1053` | 删除 `:1037` 用 `values`（含中文量词）替代数值的写法，只用 `BARE_LARGE_NUMBER_RE`；`:1040` 分支要求 `PERFORMANCE_UNIT_RE` 且极限词与数值同窗口；取词面按 R-A 限定 | 「三条/25 个」类计数不再算容量 |
| A4 | cost_decision | `:1054-1055` | 词 + 同句（±80 字符）二选一结构与「采用/选择/决定」判词 | 叙述性提到「成本/配额」不再命中 |
| A5 | cost_trap | `:1030-1031`、`:368-373` | `ENV_MEASURE_RE` 第 3 分支去掉裸 `\d{3,}`；数字须与量词/单位紧邻，排除 `(19\|20)\d{2}` 年份与 ≥8 位连续数字 | 年份/提交号不再当量词 |
| A6 | multi_source | `:51`、`:680-683` | `SKILL_PATH_RE` 拆两支：`skills/<名>/scripts/…`（调用资产）计分，`skills/<名>/SKILL.md`（读文档）不计分；排除 `experience-keeper`（每会话固定入口）；保留「工具种类 ≥4」 | 读 skill 文档不再算编排 |
| A7 | error_recovered / retry / retry_loop | `:645-673` | 键改 `(tool.name, 命令族)`，命令族先去 shell 包装（`powershell -Command`/`cmd /c`/`bash -c`）再取首个有效 token（解释器取脚本/`-c`/`-m`）；`retry_loop` 只算**连续**失败段；CLI 用法错误（unrecognized argument / usage: / IO error / no such file）与测试 FAIL 不计恢复 | 长会话不再恒定命中；TDD 红绿归正常循环 |
| A8 | state_machine_stuck | `:786-802` | 轮询命令限状态读取命令族且目标标识一致；解决动作只在工具参数里匹配 | 代码/文档文本不再构成「状态机卡死」 |
| A9 | silent_failure_suspected | `:805+` | `INTENSIFIER_RE` 只看同一工具族的后续调用参数，且从词表移除 `--check`/`--dry-run` 这类幂等校验开关；不接受 assistant 正文命中 | 幂等开关不再等于「成功没让人放心」 |
| A10 | slow | `:685-686`、信号表 H2 | 移出 `signal` 数组，只保留 `cost.duration_ms`；文档 H2 标注「仅记录，不触发」 | 候选列表不再被时长噪声拉长 |

**取词面统一规则（A1/A3/A4/A5 共用的前提）**：判据型信号的取词面 = 工具参数 + 工具错误原文 + **非文档类**工具结果；排除用户消息与 assistant 叙述正文、以及读取 `.md/.txt/.rst/.docx` 的结果（文档是转述）。
B 族判决类（`user_correction`/`review_rejected` 等）与 §5 新信号不受此限。

## 5. 召回侧：新增 `grounded_verdict`

A6 收紧后，本次唯一产出条目的 `fixture` 会失去 `multi_source`——必须补一条正信号，否则真值被误杀。

| 项 | 内容 |
| --- | --- |
| 判据 | 本轮 assistant 结论出现 **≥3 处 `文件:行号` 引用**（路径带扩展名，正则 `[A-Za-z0-9_./\\-]+\.(c\|h\|py\|json\|yaml\|yml\|md\|go\|ts\|sh):\d+`） |
| 层与代价 | L1，零额外 IO（只扫本轮 assistant 文本） |
| 落候选 | 标 `confidence: pending`，加入 `experience_hook.py:29-37` 的 `PENDING_SIGNALS` |
| 语义 | 结论被锚定到源码行号，说明本轮产出了「文档与源码都查不到的对照结论」，正对判定准则第二类「现象与原因的映射」 |
| 实测 | `fixture` 12 处、`fixture` 12 处（两条正是本次唯二产出条目的轮次）；`fixture`/`fixture` 为 0 处 |

## 6. 预期效果（影子复算，已跑）

影子实现只覆盖被调整的信号（A1–A10 + `grounded_verdict`）；未调整的 `review_rejected`/`blast_radius_guard`/`asset_query` 等不参与对比，故「改后」不是最终候选数，而是被调整信号的变化。

| 轮次 | 改前信号数 | 改后 | 说明 |
| --- | ---: | --- | --- |
| `fixture` 季度总结 | 4 | 0 | 4 个判据型误报全消 |
| `fixture` core_leave 核查 | 1 | 1 | multi_source → **grounded_verdict**（真值保留） |
| `fixture` 仓库结构方案 | 3 | 1 | cost_decision、slow 消除（review_rejected 为真命中，未调整） |
| `fixture` 契约归属 | 0 | 0 | — |
| `fixture` 流程改造 | 7 | 2 | 判据型噪声（state_machine_stuck / silent_failure_suspected / multi_source / slow / retry_loop）清零，余 error_recovered + retry 属真实恢复 |
| `fixture` 克隆子仓验证 | 7 | 3 | error_recovered、multi_source、slow、silent_failure_suspected 消除，余 retry / blast_radius_guard / asset_query 属真实命中 |
| `fixture` 提交改动 | 8 | 0 | — |
| `fixture` lb review | 8 | 1 | 只留 **grounded_verdict** |
| **合计** | **38** | **8** | 判据型误报清零，两个真值轮各留 1 个正信号 |

「改后」列由真实转录复跑 `prescreen.detect_signals` 得到（非影子实现），与 §6 目标一致。

## 7. 回归样本集（防止改完再退化）

| 项 | 内容 |
| --- | --- |
| 位置 | `tests/fixtures/prescreen-regression/cases.jsonl` |
| 每行字段 | `label`、`client`、`transcript`、`turn_id`、`expect_present`、`expect_absent`、`value`(yes/no) |
| 种子 | 本文 8 个真实轮次（2 条 yes、6 条 no）；转录路径见上表对应的 codex 会话 |
| 断言 | 噪声轮 `detect_signals` 不得含 `expect_absent` 列出的信号；真值轮必须命中 `expect_present`（`grounded_verdict`） |
| 运行 | 纳入 `tests/test_experience.py::PrescreenTableTest`，与逐信号「一命中一不命中」用例并列 |

## 8. 同步清单（按设计文档 §与实现的对齐）

| 位置 | 动作 |
| --- | --- |
| `scripts/prescreen.py` | 常量拆分、`detect_signals` 与 `_detect_*` 谓词按 A1–A10 调整，新增 `grounded_verdict` |
| `docs/experience-prescreen-table-design.md` | 信号总表 G4/I3/F1/F3/D3/H1/H2/A1/A2 行更新判定与取词面；新增取词面统一规则与 `grounded_verdict` 行；§已知不可得补充「文档转述不参与判据取词」 |
| `scripts/experience_hook.py` | `PENDING_SIGNALS` 增加 `grounded_verdict` |
| `schema/candidate-v1.schema.json` | `signal` 枚举增加 `grounded_verdict` |
| `tests/test_experience.py` | 每个改动信号补命中/不命中用例 + 回归样本集断言 |

## 9. 取舍与不做的事

| 项 | 结论 |
| --- | --- |
| 引入模型判定取词面 | 不做：钩子必须零模型调用（设计文档 §触发点与分层） |
| 直接删掉 `slow` 字段 | 不做：保留 `cost.duration_ms`，只移出 `signal`，候选成本统计仍需要 |
| 用「文档轮次」整体负向闸门 | 不做：改为按**工具调用**粒度排除文档取词，避免误杀「改知识库且含实测数值」的轮次 |
| `grounded_verdict` 是否要求用户判定句式 | 不要求：只锚定 `文件:行号` 引用，`fixture`（判定式提问）与 `fixture`（文档评审）都能召回 |
| 存量条目影响 | 无：本次只改采集侧判定，不动已有条目与候选 |

## 10. 实施记录（2026-10-12）

已按本方案落地，与计划的三处偏差与理由：

| 项 | 计划 | 实际 | 理由 |
| --- | --- | --- | --- |
| A1 环境名词 | 移入 `ONESHOT_ENV_KEYWORDS` 并写候选 `scene` | 直接删除该词表 | 候选 schema 无 `scene` 字段，为一个提示性元数据新增属性，收益不抵校验面漂移风险 |
| A3 判据取词面 | 按「读文档类文件」排除 | 追加「结果本身是文档正文（≥2 个 Markdown 标题行）」 | 复跑发现文档内容会经 `rg`/skill 抓取回来，参数里看不到 `.md`，只按路径排除会漏 |
| A3 硬限判定 | 裸数字 + 硬限语境 | 追加「硬限词与数值同窗口、≥7 位数字不算数值」 | 复跑发现代码里的 `MAX`/`limit` 与 URL 里的长数字能隔几十字符配成一对 |
| A8 状态机 | 解决动作只认工具参数 | 只收紧轮询侧（限控制面 CLI），解决动作仍可来自叙述 | 轮询侧收紧已消除误报；解决动作写成叙述是该信号的原设计意图 |
| A2 `retry` | 键改命令族 | 追加「重复调用中至少一次不是 ok」 | 两次都成功只是重复执行（重复跑测试），复跑里占了 2 组噪声 |

验证：`tests/test_experience.py` 248 项全绿，新增回归样本集
`tests/fixtures/prescreen-regression/cases.jsonl`（8 条真实轮次特征）与逐信号命中/不命中用例。

## 11. 后续：无效候选的三个来源（同日追加）

判定收紧后残余噪声的构成变了：9 条候选里 5 条属于「结论已进项目载体」，与信号判据无关。
按此追加三项，已实施：

| 项 | 内容 | 实现点 |
| --- | --- | --- |
| A 载体标记 | 写盘落点命中 `AGENTS.md`、`.harness/rules/**`、`.ai/rules/**`、`.ai-knowledge/**`、`openspec/**`、`**/SKILL.md` 时，候选带 `carrier` 字段 | `prescreen.detect_carrier_write()`、hook 写入、schema 加 `carrier` 属性 |
| C 复盘分组 | `review` 按动作分组：载体已覆盖 → 待核实 → 过程信号 → 其余；每组的处置成本写在组标题里 | `experience._review_groups()`、`_candidate_line()` 显示载体路径 |
| D 判决台账 | `add --from` 与 `drop --reason` 各记一条台账（轮次、信号、判决、原因）；`stats` 出「信号转正率」，低于 20% 的证据量足够时标进复核队列 | `lib.append_verdict()` / `lib.iter_verdicts()`、`experience.signal_conversion_rates()` |

不做两件：继续收紧信号（会伤召回，`grounded_verdict` 兜的就是这类）、钩子里读载体正文做语义比对
（钩子逐轮跑且必须零额外 IO）。

验证：`tests/test_experience.py` 254 项全绿（新增载体判定、hook 标记、复盘分组、drop 原因台账、
add 台账、stats 转正率六项用例）。
