---
name: experience-keeper
description: Agent 经验库的复盘、检索与维护。当会话开场出现经验库提示行、用户要求把踩坑或纠正沉淀成经验、或要按现场字样检索历史经验时使用。触发词：复盘经验、沉淀经验、经验库、查经验、有没有类似经验、把这条记下来。
---

# 经验库管理

把对话里验证过的做法、踩过的坑、用户的纠正沉淀成分类经验条目，供全局与项目两级复用。

沉淀分两步：采集由五端钩子在每轮结束时自动完成（落候选），提炼由 agent 在用户发起时完成
——用户当场说「记下来」，或任务收尾时用户点头。开场提示不构成授权。
哪一轮落候选、落候选时记什么，由 `docs/experience-prescreen-table-design.md`（核心设计）定义。

## 会话开场：闸门派发，一次会话只复盘一次

一次会话只在首次启动复盘一次：`gate` 把会话标识写进 `~/.agents/experience/.session-review.json`，
之后同会话再跑 `gate`（复盘已结束、会话中途又冒出候选）都只回「本会话已复盘过」，直到会话退出
（客户端进程结束或会话 id 变化）才重新开闸。

```bash
python <skill_dir>/scripts/experience.py gate    # 开场唯一动作：决定派不派复盘
```

`gate` 只输出四种结果，照做即可：

| 输出 | 动作 |
| --- | --- |
| 「无待复盘候选，不派发」 | 什么都不做，直接干活；这次开场已用掉本会话唯一的复盘机会 |
| 「复盘进行中…本次不派发」 | 什么都不做：已有复盘在跑，这是防套娃的锁 |
| 「本会话已复盘过…本次不派发」 | 什么都不做：本会话已经复盘过，会话退出前不再复盘 |
| 「后台派发复盘 Agent，brief 原样转发」 | 后台派子 Agent，brief 全文照抄，不等结果，立刻处理用户任务 |

派发规则：

- **一次会话一次复盘**。`gate` 落会话标记后，复盘结束、再跑 `gate`、会话中途出现新候选都不再派发；
  只有会话退出（客户端进程结束或会话 id 变化）标记才失效。开场无候选同样算用掉这一次。
- **只有主 Agent 派发**。子 Agent 也继承全局规则，但复盘期间 `gate` 只会返回「复盘进行中」，拿不到 brief；
  子 Agent 自己也不得再派发。
- 子 Agent 只回一行统计（入库 x、丢弃 y、保留 z），不回候选正文，主上下文不被污染。
- brief 由 `gate` 现场打印，不要自己改写或补充；它写明了动作顺序、可 add / 必须 drop / 必须留人的边界，
  以及收尾命令 `review --end --summary "..."`。
- 复盘锁 15 分钟过期，崩溃不会永久关掉闸门；复盘记录追加到 `~/.agents/experience/reports/<日期>.md`。

顺手也要知道：`prompt` 仍可用来看看库里有什么（条目数、资产数），它不再负责派发。

## 会话内当场沉淀（没有候选时）

用户说「沉淀一下」「把这条记下来」，或一轮任务刚收尾时，当场入库，不要等下次开场：

```bash
python <skill_dir>/scripts/experience.py add --manual \
  --title "XXX" --category pitfall --type recovery --scope project \
  --action "可执行结论" --root-cause "根因"
```

`--manual` 与 `--from` 互斥。手工条目没有转录行号可回溯，置信度强制 `low`；
有候选时一律走 `add --from`，证据链完整。凭据形态内容在写入时自动替换成 `[REDACTED]`，
命中几处会打印在结果里；看到这行提示就把转录或命令历史里的原文一并清掉。

## 自己试错换路：先记备选，不入库

这类弯路不在经验预筛表的覆盖内（漏了哪些、为什么，见核心设计的「盲区与兜底」）。结论只有当事
agent 知道，但别直接写条目——自评结论没有第二方核实，直接入库会把经验库撑大。先记备选，复盘确认后才转条目：

```bash
python <skill_dir>/scripts/experience.py note \
  --title "CMS 查询改用 SDK 分页" \
  --action "改用 SDK 分页查询，单页 100 条" \
  --root-cause "CLI 走旧网关，Throttling 重试绕不开" \
  --detail "Throttling: Request was denied due to request throttling" \
  --pre-tool cms_cli --pre-error Throttling --tags cms,throttling
```

`note` 落的是候选（`client: manual`、`signal: agent_note`）：`review` 能看到，`show` 读全文，
`drop` 丢弃，`add --from <id>` 才入库，届时 `--action`、`--root-cause`、`--pre-tool`、`--pre-error`、
`--tags` 自动带上，置信度停在 `low`。`--detail` 放报错原文，后来者才搜得到。

## 经验库登记表

`~/.agents/experience/INDEX.md` 是跨项目的一份登记表，`library` 直接打印它：

| 列 | 口径 |
| --- | --- |
| 经验条目名 | 条目标题 + id（`use` / `remove` 都用 id） |
| 描述 | 条目 `action` 结论摘要，超过 80 字截断；没写结论时取正文第一句 |
| hit_count / use_count | 召回与采纳计数，口径见上一节 |
| 所属项目 | 全局条目写「全局」，其余写项目名 |

- 触发：用户问「经验库有哪些经验」「列出完整经验库」「查询经验召回情况」「查询经验复用情况」时跑
  `library`，原样展示表格，不另做总结替代表格。
- 同步：`add` 入库、`remove` 删除、`search` 计数、`use` 采纳四处写盘后自动重写登记表；`index` 手动重建。
  写入失败只在 stderr 提示一行，不挡住条目本身的操作。
- 删除：`remove <id>` 默认预演并打印文件路径，`--apply` 才删文件并同步登记表。

## 复盘一条候选的标准动作

复盘子 Agent 与人工复盘走同一套动作，子 Agent 另有三条硬边界：单轮最多处理 5 条候选；拿不准一律保留、
不得 drop；没有删条目权限（`remove` 只由用户发起），`promote` 也必须用户点头。

`review` 已按复盘动作分组，从省事的组往下判：**载体已覆盖候选**（候选带 `carrier`，结论已写进规则/模板/
知识库/调研文档，核对载体写全即可判）→ **待核实候选**（判据型信号，必须读原文）→ **过程信号候选**
（只有过程信号，扫一眼用户诉求即可判）→ 其余候选。

候选表示存在待确认的可复用新知识，不等于阶段性决策值得入库。当前变更中的设计取舍、被评审否决的方案、
普通纠正和源码行号引用，若没有独立的实测、根因、协议边缘、状态异常或可复用资产证据，
应由采集钩子跳过；已写入 OpenSpec、规则或知识库的结论直接以载体为准。只有用户明确表达长期约束
（如“以后默认”“始终”“记住”）时，纠正才可单独成为候选。详细准入判据以
`docs/experience-prescreen-table-design.md` 的「候选准入闸门」为准。

纯设计讨论轮（全程只读源码与文档、没有变更类调用与外部 CLI）由讨论轮闸门直接跳过：这类轮次的
结论归 OpenSpec 等项目载体。`Agent`/`SendMessage` 的 prompt 正文不是现场证据，方案里写到「快照」
「三个对象」「统计口径」不会被当成实测数据。

1. 读原文：`experience.py show <candidate-id>`（自记备选的正文就是 agent 写的现象、弯路、结论，没有转录行号）。
   带 `carrier` 的候选通常不必 show，直接查那个载体文件是否已写全。
2. **过可复用闸门**：候选带 `reuse: one_off` 说明本轮脚本是按现场环境写死、用完即弃的一次性产出。
   结论值钱就提成判据型条目，脚本本身不写进条目，随候选一起 `drop`；结论也不值钱则整条 `drop`。
   看不清可复用性时按 `one_off` 处理，宁可漏一个脚本，不要把一次性流水搬进经验库。
3. **扫描项目内已有载体**：`experience.py scan --query "<关键词>"`
   - 命中同一知识点时不要重复记录。载体可维护就更新载体，条目只写引用与差异；
     载体不宜改就写 `kind: reference` 的条目，正文不复述载体内容；载体已覆盖且无新增信息则直接 `drop`。
4. **纠正类候选必须先核实**（`correction` 字段非空时）：
   - 用只读手段拿独立依据：实跑一次、查源码或文档、查数据、对照已有条目证据。
   - 用户正确 → `--verification confirmed`；属于偏好无对错 → `--verification preference`；
     用户说法有误 → **不入库**，直接 `drop`，依据留在候选里即可。
   - 找不到独立依据时必须落 `preference`，不得判用户错。`--verification-note` 必须写清依据出处。
5. **丢弃时写原因**：`drop <id> --reason carrier|one_off|noise|dup|unspecified`。所有 `add`/`drop` 都进判决
   台账，`stats` 据此算每个信号的转正率，低于 20% 的信号会被标进复核队列——原因写不清，下次调判据就没有依据。
6. 写条目（`--scope` 按下一节「归属：全局还是项目」定，不写默认为项目级）：

```bash
python <skill_dir>/scripts/experience.py add \
  --from <candidate-id> \
  --title "CMS 查询触发 Throttling 的退避方式" \
  --category pitfall --type recovery --scope project \
  --action "指数退避重试，时间窗口收窄到 5 分钟" \
  --root-cause "服务端按秒限流，原参数重试必然再次失败" \
  --tags cms,throttling --pre-tool cms_query --pre-error Throttling \
  --pre-files scripts/cms_query.py \
  --verification confirmed --verification-note "CMS 接口文档第 3 节 + 实测第三次重试成功"
```

`--pre-files` 声明这条经验适用于哪些文件或目录（带扩展名按后缀比，目录按前缀比）。
**按需召回靠它**：不声明就只能靠关键词命中，声明了才能在「正在动这个文件」时被精确召回。

写入后用编辑工具润色正文，把 `现象` 段补齐成可复现的描述。

## 四个时机检索经验

接手任务、调用关键工具前、出现错误或空结果后、准备交付前——都按**现场字样**召回，不预先通读清单：

```bash
# 正在动某类文件：只召回适用的经验，没声明的条目不冒出来
python <skill_dir>/scripts/experience.py search --path tests/unit/test_nat64.c

# 调用关键工具前 / 出现错误后：拿现场字样精确召回
python <skill_dir>/scripts/experience.py search --query "undefined reference"
python <skill_dir>/scripts/experience.py search --query "单测,unittest,make test" --any
```

**按需召回，不要无脑全库检索。** 只在当前动作与历史经验可能相关时搜，判定如下：

| 当前动作 | 是否检索 | 怎么搜 |
| --- | --- | --- |
| 动的文件有对应经验（改单测、改配置模板） | 检索 | `--path <文件>` |
| 要用某个工具或接口 | 检索 | `--query "<工具名/接口名>"` |
| 出现报错或空结果 | 检索 | `--query "<报错原文>"` |
| 接手陌生任务 | 等动作 | 先不搜；等到改文件、调工具、报错时按现场字样召回 |
| 改的是从没碰过的新代码、或已在按条目结论执行 | **不检索** | — |
| 同一任务里已搜过同一关键词 | **不重搜** | 结论已在上下文里 |

`--query` 按原文子串匹配条目标题、tags、结论、前置条件与正文，**必须取现场字样**：
报错原文、工具名、接口名、字段名。只记得话题词时用逗号分隔多个候选词加 `--any`，
先召回再筛。泛化描述（如「编译失败怎么办」）零命中，这不是没经验，是搜法错了。

`--query` 留空即列出本项目全部条目，只在确实猜不出关键词时用一次；它的用途是兜底，
不是开场动作，不要每个子任务都跑一遍。

### 召回计数与采纳计数

两个口径分开记：`search` 记召回，机械可观测；`use` 记采纳，agent 自报。

| 字段 | 谁写 | 证明什么 | 覆盖 |
| --- | --- | --- | --- |
| `hit_count` / `last_hit_at` | `search` | 条目被检索到 | 满：检索动作可观测 |
| `use_count` / `last_used_at` / `last_use_result` | `use <id>` | agent 声称按这条执行过 | 靠自觉：不跑就不计 |
| `use_failed_count` | `use --result failed` | 执行了但没解决 | 同上 |

| 场景 | 召回计数 |
| --- | --- |
| `--query` / `--path` / `--tool` / `--error` 命中并展示 | +1 |
| `--query` 留空的清单用法、「可能相关」段 | 不计数 |
| `search --no-count` | 不计数（人工盘点、脚本自检） |

按条目执行完（成功或失败）跑一次：

```bash
python <skill_dir>/scripts/experience.py use <条目-id> --result ok --note "重试第 3 次成功"
```

`use` 只写采纳，不动 `hit_count`，也不刷新 `verified_at`：采纳是自报，不能让未复核条目靠自报逃过 90 天复核。

`search` 每条显示 `hit=N use=M`，`stats` 分别汇总召回与采纳，`index` 表格有「召回」「采纳」两列。
计数写回条目 frontmatter；召回计数失败（锁超时、文件不可写）只在 stderr 提示一行并落一条诊断，
检索结果照常输出；`use` 写盘失败退出码 1——采纳本来就是靠命令攒的，写不进去必须让 agent 看见。

口径边界：`hit_count > 0` 只证明被检索到，`use_count > 0` 只证明 agent 声称执行过。
判断「是不是真经验」把 `hit_count`、`use_count`、`use_failed_count` 与 `evidence.sessions`、`verification`
一起看。变更前的存量条目没有 `hit_count`：旧 `use_count` 按召回读、采纳按 0 读，首次写盘时自动搬迁。

`--path` 按条目 `precondition.files` 收窄：带扩展名的按后缀比，目录按前缀比；
声明了 `files` 但不是当前的文件的条目会被排除，没声明的降级列在「可能相关」段，不静默丢弃。
`--tool` / `--error` 也是收窄，但它们是**硬过滤**：条目 `precondition` 不含该值直接排除，
所以只在已经确认条目前置条件写的就是这个名字时才加，否则会把要找的条目一起滤掉。
收窄把结果清空时 `search` 会说明是哪一类过滤造成的，看到提示就去掉那个参数重搜。
命中 precondition 的每条加 3 分。

结果末尾另列**可复用资产**（登记表里路径命中该关键词的脚本、模板）：条目告诉你上次怎么判，
资产告诉你上次写的工具在哪，能直接用就不必重写。无匹配时输出「没有匹配的经验条目。」。

## 反复出现的纠正：升级为规则

同一纠正被三个及以上独立会话验证后，它就不再是条目而是规则：

```bash
python <skill_dir>/scripts/experience.py promote <id>          # 预演，打印待追加的规则文本
python <skill_dir>/scripts/experience.py promote <id> --apply  # 用户确认后再执行
```

`promote` 默认只打印。**必须先把规则文本给用户看过并得到同意，才能加 `--apply`**，
不得自行修改 `~/.agents/AGENTS.md` 等全局规则文件。追加后条目自动转为引用型。

## 分类与类型

`--category` 说明经验讲什么：`pitfall` 踩坑、`tooling` 工具用法、`workflow` 流程、
`decision` 决策、`prompt` 与 agent 协作方式、`domain` 业务事实。

`--type` 说明经验要求做什么：`path` 调用顺序、`tool-rule` 参数规则、`antipattern` 避免做法、
`recovery` 恢复策略、`completion` 完成判定、`constraint` 人类要求的长期约束。

被纠正的对象决定落点：事实认知落 `domain`，做法顺序落 `workflow`/`tooling`，
协作方式落 `prompt` + `constraint`，决策取舍落 `decision`。

### 归属：全局还是项目

`--scope` 不写就是 `project`，项目名自动取当前项目根，不需要手填 `--project`。判定按顺序走，命中即停：

| 顺序 | 判据 | 落点 |
| --- | --- | --- |
| 1 | 结论里出现项目专有标识（仓库名、内部服务或接口名、字段口径、目录、环境别名），剥离后不再成立 | `project` |
| 2 | 换个仓库、换套接口或数据源就不成立 | `project` |
| 3 | 讲的是与本用户协作的方式（表达顺序、给结论的粒度、边界、验证习惯），与仓库无关 | `global` |
| 4 | 项目内载体（`docs/`、`skills/`、`kb/`）已维护同一知识 | `project` 加 `--kind reference --source-of-truth "<相对路径#章节>"`；载体本身在全局目录时改 `global` |
| 5 | 以上都不命中：与仓库无关的工具用法、通用排查顺序与验证方式 | `global` |

硬性约束，不得自主放宽：

- 拿不准一律写项目级。写全局条目前必须先跑 `scan`，确认它不是项目内某条知识的泛化。
- 同一知识只留一级。项目级已存在就不再加全局条目；确需跨项目复用时走 `promote`，或改写为全局条目并删掉项目级旧条目。
- 全局条目正文里不得出现项目专有名词，出现即改写为项目级。
- 改判要同时做两件事：在 `global/<category>/` 与 `projects/<名>/<category>/` 之间移动文件，并改 frontmatter 的 `scope`、`project` 字段。没有专用命令，只改一处会让位置与字段互相矛盾。

## 维护

```bash
python <skill_dir>/scripts/experience.py library            # 展示登记表（条目名、描述、hit/use、所属项目）
python <skill_dir>/scripts/experience.py index              # 重建登记表 INDEX.md
python <skill_dir>/scripts/experience.py remove <id>        # 删除条目：预演
python <skill_dir>/scripts/experience.py remove <id> --apply # 删除并同步登记表
python <skill_dir>/scripts/experience.py stats            # 条目数、置信度分布、召回次数、全局候选积压、平均成本、信号转正率
python <skill_dir>/scripts/experience.py search --query "<关键词>" --no-count   # 只读检索，不累加 hit_count
python <skill_dir>/scripts/experience.py doctor           # 采集体检：最近诊断 + 候选与条目现状
python <skill_dir>/scripts/experience.py validate         # 按 schema 校验已有候选；有问题退出码 1
python <skill_dir>/scripts/experience.py assets           # 可复用资产登记表：列表、确认跨会话复用次数
python <skill_dir>/scripts/experience.py backfill          # 预演：从正文推导存量条目的适用文件
python <skill_dir>/scripts/experience.py backfill --apply  # 写入 precondition.files（可加 --root 确认存在性）
python <skill_dir>/scripts/experience.py sweep            # 跨会话巡检预演：重复操作族、反复改写的脚本
python <skill_dir>/scripts/experience.py sweep --apply    # 把巡检线索标进对应候选（标信号，不动正文）
python <skill_dir>/scripts/experience.py gc                          # 预演，候选上限默认 30 天
python <skill_dir>/scripts/experience.py gc --apply                  # 淘汰超期候选、清理标记与超期诊断
python <skill_dir>/scripts/experience.py gc --apply --force          # 连指向可复用资产的超期候选一起淘汰
python <skill_dir>/scripts/experience.py regate                       # 预演：用当前准入闸门复判存量候选
python <skill_dir>/scripts/experience.py regate --apply               # 丢弃复判后仍会被闸门拦下的候选
python <skill_dir>/scripts/install.py --check             # 五端接线自检，缺失项逐条列出
```

候选存活 30 天，超期不转正即淘汰。带 `asset_script` 且登记表里有对应可复用资产的候选会被
`gc` 拦下并单列，必须先用 `add --from` 转正、或明确 `drop`，再加 `--force` 才能清掉——
否则会把「盘上有这个脚本」这件事一起淘汰。资产登记表与候选分开存，候选淘汰后登记仍在。

闸门收紧只对新增候选生效，存量候选不会自动重判：改完判据跑 `regate`，先把「现在会被跳过」
的候选列出来，确认后再 `--apply`（判决台账记为 `regate:<原因>`）。

**存量条目补适用文件**：按需召回的精度取决于 `precondition.files`，没声明的条目只能靠关键词命中。
`backfill` 从正文推导（出现 ≥2 次的路径或复合名，排除通用词与全大写常量），默认预演，
确认后 `--apply`。加 `--root <项目根>` 可确认推导出的文件是否真实存在（该次会建一次有界路径索引）。

候选一直不出现时先跑 `doctor`：它把「解析器坏了」与「这一轮本来就没什么可沉淀」分开。
无候选分两种：`doctor` 里没有诊断就是本轮没命中经验预筛表，属正常；出现 `parse/no_transcript`、
`normalize/unknown_client` 才是采集坏了。经验预筛表的判定入口是 `scripts/prescreen.py`，
表定义见 `docs/experience-prescreen-table-design.md`（核心设计，唯一权威），改信号要同步表、schema 与测试。
写入契约是 `schema/candidate-v1.schema.json`，枚举与代码常量的一致性由测试锁住。

候选文件上的读改写（`drop`、`add --from`、`gc --apply`）会在锁里重新定位目标，锁默认等 3 秒；
等不到就带原因失败，不装作写成功。`gc --apply` 遇到别的端正在写同一份候选文件会跳过并提示，
下次再跑即可。

候选与统计都是全局口径：在 A 项目产生的候选，开 B 项目时 `review` 也会提示积压，
不会因为换了项目就永久沉睡。

## 会话结束标记

`~/.agents/experience/pending/` 下每个会话一份标记，由两处写入：

| 来源 | 触发 |
| --- | --- |
| 原生会话结束钩子 | Claude Code `SessionEnd`，`reason: native_hook` |
| 退出哨兵 | 其余四端：第一次落候选时派生 `exit_sentinel.py`，盯住客户端主进程 pid，退出后写标记 |

标记只用于开场提示，写它不调用模型、不写候选；候选已被消费完的标记会在下次 `review`
时自动清理。

## 参考文档

| 文档 | 内容 |
| --- | --- |
| `docs/experience-prescreen-table-design.md` | **核心设计**：经验预筛表——信号清单与触发条件、人类纠正判定、可复用闸门、分层、工程域触发点、误报控制、实现进度 |
| `docs/2026-09-22-agent-experience-sedimentation-design.md` | 设计方案与决策记录：存储布局、候选字段、工具状态判定纪律、信号与纠正判定、复盘与召回、方法论映射、测试与验收 |
| `docs/parsers/<client>.md` | 各端转录解析契约：轮次边界、字段来源、工具状态、丢弃内容、已知不可得；索引见 `docs/parsers/README.md` |
| `docs/agent-experience-engineering.md` | 方法论输入：两篇工程文章的对照 |
| `docs/deployment.md` | 五端接线、会话结束信号、环境变量与常见问题 |
| `schema/candidate-v1.schema.json` | 候选记录写入契约，钩子组装后校验 |
| `schema/entry-v1.schema.json` | 条目 frontmatter 写入契约，`add` 写盘前强制校验 |

改行为前先读设计文档对应章节；设计意图与实现不一致时按设计文档的目标改代码，并把两者一起更新。

## 硬性约束

- 经验与候选只存本机 `~/.agents/experience/`，不写入任何仓库、不发起网络请求。
- 条目必须可回溯：`evidence.sources` 指向转录文件与行号，禁止无证据条目。
- 纠正类条目禁止写成对人的评价，只写观点、依据与可执行结论。
- 凭据形态的内容不得进入条目；`hook`、`note`、`add`、`promote` 四个写入盘口在落盘前统一脱敏，
  命中几处会打印提示。看到提示要提醒用户把转录或命令历史里的原文清掉——脱敏只保住经验库这一份。
