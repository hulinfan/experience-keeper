# 准入闸门收紧：设计讨论轮与取词面错认（2026-10-16）

> 来源：一次 8 条候选的复盘——8 条全部出自方案设计与讨论轮。方案已实施，验证方式与结果见 §5。

## 1. 观测：8 条候选的准入情况

用**原始转录**复跑 `detect_signals` + `candidate_skip_reason`（不是拿候选记录复跑）：

| 候选 | 候选信号 | 现行闸门（改前） |
| --- | --- | --- |
| claude `0099073a#t24` | blast_radius_guard, cost_trap | ALLOW |
| claude `0099073a#t73` | evidence_capture, cost_trap | ALLOW |
| codex `fixture` | error_recovered, asset_query, grounded_verdict | ALLOW |
| codex `fixture` | error_recovered, prereq_discovered | ALLOW |
| codex `fixture` | error_recovered, asset_query, grounded_verdict | ALLOW |
| codex `fixture` | perf_limit | SKIP（carrier_written） |
| codex `fixture` | retry_loop, user_correction | SKIP（no_reusable_evidence） |
| codex `fixture` | 过程信号 + grounded_verdict | SKIP（no_reusable_evidence） |

后三条落在 2026-10-14 14:19 闸门上线之前（`prescreen.py` mtime 14:24，三条候选 ts 为 14:11/14:16/14:18），
是存量；**前五条现行代码仍会准入**，全部为零变更类调用、零现场读写的讨论轮。

## 2. 逐条归因（复跑命中原文）

| 候选 | 信号 | 命中原文与来源 |
| --- | --- | --- |
| t24 | blast_radius_guard | `Agent` 工具的 **prompt 正文**：「不能引入 DACQ 汇总**快照**方案（用户没有选）」 |
| t24 | cost_trap | 同一 prompt：「删除**三个**非 service 对象」+「统计**口径**沿用**实测**值」 |
| t73 | evidence_capture | `SendMessage` **消息正文**：「让查指定**基线**」 |
| t73 | cost_trap | 同一消息：「**一次**有效结果」+「**阈值**」 |
| `fixture` | asset_query | `Select-String -Pattern 'struct dp_vs_service\|dests\|…\|stats\|refcnt'` 里的 `\|stats` |
| `fixture` | asset_query | `rg "^(include/ipvs/(service\|dest\|stats)\.h\|…)"` 里的 `\|stats` |
| `fixture` | prereq_discovered | git 输出里的分支名 `Mars/feature/gameproxy` 命中 `proxy` 子串 |

## 3. 根因

| 编号 | 根因 | 波及 |
| --- | --- | --- |
| T-A | 取词面把「工具参数」一律当现场证据，但 `Agent`/`Task`/`SendMessage`/`AskUserQuestion` 的参数本身是叙述正文 | blast_radius_guard, evidence_capture, cost_trap |
| T-B | `QUERY_STATEMENT_RE` 的 `\|\s*(?:stats\|where\|timechart\|table)\b` 分支把源码检索模式当成日志查询；检索类命令未像 `PROTOCOL_FIX_RE` 那样排除 | asset_query |
| T-C | `PREREQ_ERROR_RE` 的 `proxy`/`kms` 无词边界，命中分支名 `gameproxy` | prereq_discovered |
| T-D | 硬限词 `limit`/`max` 在源码标识符里遍地，±40 字符窗口挡不住 | perf_limit（预防性收紧） |
| T-E | 载体闸门只扫本轮写盘调用：写盘落在 `Agent` 子会话里时 `carrier` 为空 | 载体已覆盖的轮次仍落候选 |
| T-F | 闸门缺少「本轮是否触达现场」维度，设计/评审轮与实测轮判定同构 | 全部五条 |
| T-G | 闸门收紧不回判存量候选 | 三条存量噪声要躺满 30 天 |

## 4. 调整清单

| 编号 | 位置 | 改法 |
| --- | --- | --- |
| F1 | `prescreen._tool_evidence` / `detect_signals` | 新增 `NARRATIVE_TOOLS` 与 `_judgement_args()`：叙述承载类工具的参数不进判据取词面（`dep_fix`/`env_recovery`/`blast_radius_guard`/`evidence_capture`/`env_hazard`/`asset_query`/`_change_args`） |
| F2 | `prescreen.is_query_statement` | 参数命中只读检索或只读 git 读取时不做查询匹配 |
| F3 | `PREREQ_ERROR_RE` | `proxy`/`kms` 加词边界断言（`re.I` 下写 `[A-Za-z0-9]`） |
| F4 | `prescreen._has_word_marker` | ASCII 语境词要求独立成词，`MAX_DESTS`/`max_dests`/`tcp_rmem_max` 不再算上限语境；用于 `HARD_LIMIT_MARKERS` 与 `PERFORMANCE_MARKERS` |
| F5 | `prescreen.is_discussion_only` | 新增讨论轮闸门：全程只读源码/文档才拦；写盘、现场路径（`/proc/`、`.log`、`.pcap`）、外部 CLI 一律判为执行轮；无工具的纯聊天轮不算讨论轮；置于长期约束之后、证据白名单之前 |
| F6 | `CARRIER_WRITE_TOOLS` | 纳入委托类工具：`Agent`/`Task`/`SendMessage` 参数点名载体路径即算结论已进载体 |
| F7 | `experience.py regate` | 新增存量复判命令：优先用原始转录重建轮次，预演列出「现在会被跳过」的候选，`--apply` 丢弃并记 `regate:<原因>` 台账 |
| 附带 | `READONLY_GIT_SUB` / `READONLY_GIT_RE` / `READONLY_CMD_RE` / `AGENT_TOOLING_RE` | 补齐 `ls-tree`/`cat-file`/`rev-list` 等只读子命令、`git -C <路径> status` 形态、`Test-Path`/`Get-ChildItem` 只读探测、以及 agent 工具链自身入口：这些是 F5 判「未触达现场」的依据 |

## 5. 验证

| 项 | 结果 |
| --- | --- |
| 8 条候选复跑 | 全部 SKIP（5 条 `discussion_only`、2 条 `carrier_written`、1 条 `no_reusable_evidence`） |
| 回归样本集 | 新增 5 条真实轮次（t24、t73、`fixture`、`fixture`、`fixture`），共 13 条 |
| 逐信号用例 | 新增 `PrescreenTighteningTest` 8 项：叙述承载取词面、消息正文、检索模式、词边界、标识符语境、讨论轮闸门、委托载体、`regate` 预演与执行 |
| 全量测试 | `python -m pytest tests/test_experience.py` → 267 项通过（原 254 项） |
| 存量用例 | 19 项旧用例在闸门上线时未同步（断言「过程信号轮也落候选」），本次按新设计补齐证据信号：这是存量测试债，与本次改动无关（关掉本次新增闸门复跑，仍是同样的 19 项失败） |

## 6. 同日追加：工具输出落库（判据取词面的第三处）

复盘时发现「非文档类工具结果」这一取词面在实现上是空的：候选 `tools` 只有 `name`/`args`/`status`/`error`,
`_tool_evidence()` 的结果分支、`no_convergence` 的旧值比对与 `idempotency_mismatch` 的返回体比对在真实转录上都不生效。
这意味着**实测数值只能从命令参数里取**——真实场景里压测结果、`conntrack -S` 计数、状态读取的旧值全在输出里。

| 编号 | 位置 | 改法 |
| --- | --- | --- |
| G1 | `transcripts.py` | 新增 `RESULT_CHARS = 1200`；`settle_tool()` 一律把输出原文截断存 `tools[].result`（成功路径也存）；所有建工具记录的地方统一走 `new_tool()` |
| G2 | `schema/candidate-v1.schema.json` | `tools.items.properties.result`，`maxLength: 1200` |
| G3 | `prescreen._judgement_result()` | 结果取词面按顺序排除：读文档（`.md`/`.txt`…，或结果本身是文档正文）→ 路径全是源码/文档后缀 → 仓库检索命令与只读 git 子命令。其余视为现场：`cat /proc/…`、`Get-Content *.log`、`dmesg`、`conntrack -S`、`tcpdump`、`curl`、控制面 CLI 返回值 |
| G4 | `prescreen` 取词面调用点 | `_tool_evidence()`、`_detect_deep_layer()` 的 haystack、`_detect_state_machine()` 的冲突判定统一走 `_judgement_result()` |
| G5 | `prescreen.normalized_result()` | 抽不出 `字段=值` 时只有短小标量结果可比（≤200 字符且 <3 行）；构建日志、测试日志这类长输出每次都有差异，返回空串跳过，不把「跑了一次命令」记成幂等被破坏 |
| G6 | `adapters/dsh/experience-keeper.mjs` | `tool_result` 事件补 `output` 字段（成功路径也有），解析器优先取它 |

刻意不做的两件：**不按命令名一刀切**（`cat`/`Get-Content` 读 `/proc` 与日志是现场，读 `x.c` 是转述，按路径判）；
**不全量留存输出**（按 1200 字符截断，否则候选文件撑大几十倍）。

验证：真实 codex 转录复跑，21 条工具里 20 条带上输出；新增 `ToolResultCaptureTest` 5 项
（截断口径、端到端 `cost_trap`、源码/文档/检索结果被排除而 `/proc` 结果被采信、
长输出不触发幂等不一致、schema 挡超长 `result`）；全量 267 项通过。

## 7. 同日追加：dsh 接线与事件形状适配

`install.py --check` 一直报「dsh 采集插件」缺失：适配器从未装进预设目录。落地时对着
dsh `0.2.0-rc.2` 的 `SessionEventMap`（`@deepseek-ai/dsh-session`）与 `dsh-acp` 的 `toolResultUpdate`
核对字段路径，发现适配器读的 `tool/result` 形状与这一版对不上。

| 编号 | 位置 | 改法 |
| --- | --- | --- |
| H1 | `adapters/dsh/experience-keeper.mjs` | `toolResultRecord()` 改按 dsh 0.2 形状取：`isError` 在 **message 级**（`data.message.isError`）、正文是 `data.message.content` 内容块数组；早期「块级 `isError` + 嵌套 `content`」的形态保留兼容分支 |
| H2 | `install.py` | `--check` 增加「dsh 适配器为当前版本」：副本与 skill 真身逐字节比对，抓「装了但过期」 |
| H3 | 接线 | `install.py --client dsh --apply`：复制适配器到 `~/.dsh/.agent-presets/liangshen/`（原文件已备份为 `agent.cordis.yml.bak-*`）并追加插件行 |

事件词表逐项核对结果（全部命中）：`turn/start`（`data.turn`）、`turn/end`（`data.turn` + `data.reason.kind`）、
`user/message`（`data.content`）、`assistant/message`（`data.message.content`）、
`tool/call`（`data.name` + `data.arguments`）、`tool/result`（`data.message.content` + `data.message.isError`）、
`compaction/end`（`dsh-compaction-basic` 追加）。

验证：新增 `DshAdapterTest` 真跑插件（node 可用时执行，否则跳过）——`tool/result` 三种形状、
一轮事件落盘后被 `parse_dsh` 解析出 `tools[].result` 并命中 `cost_trap`、hook payload 契约字段齐全；
新增 3 项接线用例（副本一致通过、副本过期被标出、`--apply` 刷新副本且插件行不重复）；
`install.py --check` 全项通过；全量 272 项测试通过。
落地时实测到了这条自检的价值：改完适配器后忘了重装，`--check` 立刻报「dsh 适配器为当前版本」缺失。

已知托管风险：预设目录由 `@deepseek-harness-tui/dsh-tui` 托管（`.dsh-tui-managed.json` 记 revision），
**TUI 升级可能重写 `agent.cordis.yml` 并丢掉插件行**——升级后重跑 `--apply`，`--check` 会先报出来。

## 9. 复盘当日候选：连带抓出三处误报

当天候选池 4 条，其中 2 条来自并行的 claude 会话（`platform-gwsvc`），都是设计轮：一条是**文档评审轮**
（评审 `openspec/changes/lb-aligned-config-model`），一条是**主代理编排轮**（改动交给子代理、
自己只转发消息）。两条都带 `no_convergence`——这是工具输出落库（§6）之后才可能命中的信号，
说明它一上线就带进了两个新的取词面错误：

| 编号 | 根因 | 证据 |
| --- | --- | --- |
| I1 | 变更动作词没有词边界：`address-delivery.md` 里的 `add`、`core_vpc_add()` 里的 `add` 被当成「改了配置」，于是 `mutated` 成立 | 评审轮的 `for f in ... address-delivery.md ...` 命令行 |
| I2 | 旧值比对直接读 `tools[].result`，读到的是 `git show include/core/res.h` 的**源码注释**（`EGWSVC_IDLE (unchanged)`） | 编排轮的 pinned 提交源码核对 |

处置：两条 `drop`（一条 `noise`——评审结论属于评审流程本身；一条 `carrier`——改动落进 openspec，
但由子代理执行、父轮看不到写盘路径，载体闸门因此漏判，已记为 detector 缺口）。

| 编号 | 位置 | 改法 |
| --- | --- | --- |
| I1 | `prescreen.mutation_action()` | 抽成函数判定：左侧不能是标识符字符（挡函数名），右侧小写/数字续写不算（挡 `address`/`added`/`settings`/`started`），右侧「大写 + 紧跟小写」才算 camelCase 动作名（认 `DeleteRouteEntry`、`ModifyInstanceAttribute`），全大写串（`ADDRESS_MAP_SIZE`、`SETUP_TIMEOUT`）按标识符处理。不能写成正则后置断言：`re.I` 会 casefold 否定前瞻里的字符类，把 camelCase 一起排除 |
| I2 | `prescreen._detect_prereq()` | 旧值比对改用 `_judgement_result(tool)`：读源码、读文档、检索仓库的结果是转述，不进现场取词面 |
| I3 | `prescreen._command_text()` / `strip_script_bodies()` | 参数里的 here-doc 与 PowerShell here-string **正文**不算命令行：`cat > x <<'EOF' … EOF`、`@'…'@` 里的「回滚」「一次本机」「dry-run」是脚本内容。`_tool_evidence()`、`_judgement_args()`、`_change_args()`、`_detect_prereq()`（mutated/stale）、`_detect_ops()`、`_detect_recipe()` 的查询判定、`_detect_state_machine()`、`_detect_silent_failure()` 与 `detect_carrier_write()` 统一走它 |

I3 是复核时新发现的一条：修完 I1/I2 后，同一会话又落了一条候选（`3c4d5408#t8`，批量清理 Markdown 空格 + 契约迁移），
它靠 `--dry-run`/`回滚` 与「一次/本机」命中 `blast_radius_guard` 与 `cost_trap`——这些词全在
`cat > /tmp/l4fix/split.py <<'EOF'` 的脚本正文里。判定改为「只看命令行」后该轮只剩 `error_recovered`，
闸门判 `no_reusable_evidence`。处置：`drop`（`noise`——文档格式化与契约搬迁轮，改动已在工作区）。

验证：三条真实轮次复跑后目标信号全部消失，真信号对照（`aliyun vpc ModifyRouteEntry` +
`DescribeRouteEntry` 返回 `pending`）仍命中 `no_convergence`；`mutation_action` 探针 12/12 符合预期；
回归样本集补 3 条（共 16 条）；新增 `test_no_convergence_needs_field_output`、
`test_heredoc_script_body_is_not_a_command`，扩展 `test_camelcase_assertions_do_not_casefold`；
全量 274 项通过。

## 10. 已知残留

| 项 | 结论 |
| --- | --- |
| 讨论轮闸门的判定方向 | 宁可判成执行轮：认不出的调用、空参数、外部 CLI 全部算触达现场，只有「确定只在读源码与文档」才拦 |
| 输出截断口径 | 1200 字符按单条调用算，一条命令的输出超过它就只剩开头；复盘需要更长的原文时按 `evidence.path` + 行号回读转录 |
| dsh 侧未做活体验证 | 适配器端到端用例在 node 里真跑插件，但没有起一个真实 dsh 会话：预设目录由 TUI 托管，装好后需新开会话或重载预设才装载插件 |
