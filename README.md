# experience-keeper

把 AI Agent 会话里验证过的做法、踩过的坑和用户的纠正，沉淀成**可检索、可复用**的经验条目，供多个客户端共用。

一次会话只在首次启动做一次复盘：采集由客户端钩子自动落候选，复盘时人工/Agent 判定是否入库，
下次接手同类任务时按现场字样召回。数据全部留在本机 `~/.agents/experience/`，不写仓库、不发网络请求。

## 工作方式

```mermaid
flowchart LR
  A[轮次结束钩子] --> B[预筛：信号 + 准入闸门]
  B -->|命中载体/讨论轮/无证据| X[跳过]
  B -->|证据型信号| C[候选池 ~/.agents/experience/candidates]
  C --> D[复盘 Agent / 人工]
  D -->|add| E[经验条目 + 登记表 INDEX.md]
  D -->|drop| F[判决台账 verdicts]
  E --> G[search 召回 / use 采纳]
```

| 环节 | 说明 |
| --- | --- |
| 采集 | 五端钩子在每轮结束解析转录，命中信号才落候选；载体已写入、纯讨论轮、一次性产出直接跳过 |
| 复盘 | 开场 `gate` 派发一次复盘；`review` 按「载体已覆盖 → 待核实 → 过程信号 → 其余」分组 |
| 入库 | 条目带证据链（转录文件与行号），置信度、分类、类型、归属（全局/项目）齐全 |
| 召回 | `search --query/--path/--tool/--error` 按现场字样精确召回；`use` 记录采纳 |
| 维护 | `library` 登记表、`stats` 转正率、`regate` 收紧后复判、`gc` 过期淘汰 |

## 支持的客户端

| 客户端 | 接线方式 |
| --- | --- |
| Codex | `install.py` 写入 `~/.codex/` 钩子与配置 |
| Claude Code | `SessionEnd` 原生钩子 + `settings.json` |
| dsh | cordis 插件 `adapters/dsh/` |
| pi | 扩展 `adapters/pi/` |
| codemaker | 插件 `adapters/codemaker/` |

各端转录解析契约见 [`docs/parsers/`](docs/parsers/README.md)，接线细节见 [`docs/deployment.md`](docs/deployment.md)。

## 安装

前提：Python 3.10+、git，且要接线的客户端已装好并至少启动过一次（各端配置目录存在才会接钩子）。
Windows 下面用 `python`，macOS / Linux 用 `python3`。

```bash
git clone https://github.com/hulinfan/experience-keeper.git ~/.agents/skills/experience-keeper
cd ~/.agents/skills/experience-keeper
python scripts/install.py --check    # 先看缺什么，不写任何文件（有缺失时退出码 1；未安装的客户端按跳过处理）
python scripts/install.py --apply    # 写入接线（改动前自动备份）
python scripts/install.py --check    # 复查：应输出「全部接线就绪。」（退出码 0）
python scripts/experience.py gate    # 首跑：应输出「无待复盘候选，不派发。」
```

`--apply` 一次性完成五件事：全局规则 `~/.agents/AGENTS.md` 的「## 经验库」索引段（文件不存在会建出来）、
五端规则文件链接（`.codex/AGENTS.md`、`.claude/CLAUDE.md`、`.dsh/AGENTS.md`、`.pi/agent/AGENTS.md`、`.config/codemaker/AGENTS.md`）、
Claude Code（`hooks.Stop` + `hooks.SessionEnd`）与 Codex 采集钩子、dsh 预设插件与 pi / codemaker 插件注册，
以及数据目录 `~/.agents/experience/`。客户端没装或配置缺失时跳过该端并继续，不会中断其余接线。

只装其中一端：`python scripts/install.py --client codex --apply`（可选值 `claude` `codex` `pi` `dsh` `codemaker` `all`）。

要求 Python 3.10+，无第三方运行时依赖；开发与测试另需 `pytest`、`PyYAML`。

### 装好之后会怎样

不需要额外初始化：会话正常结束就由钩子落候选，下次开场 `gate` 自动派发一次复盘。

```text
$ python scripts/experience.py gate
经验库闸门：待复盘候选 1 条…后台派发复盘 Agent，brief 原样转发：
$ python scripts/experience.py review
另有项目存在积压：my-service 1 条。用 review --all 查看。
```

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `本脚本不在全局目录里运行` | skill 没放在 `~/.agents/skills/experience-keeper`，或以链接形式存在；改成实体克隆 |
| `未发现配置，跳过` | 该客户端还没启动过或配置不存在；先启动一次客户端再跑 `--apply` |
| 某个客户端接线失败 | 单端异常只报错不中断其余端，末尾会提示 `--client <名称> --apply` 重试 |
| Codex 提示「新钩子首次运行需确认信任」 | 正常行为，按提示确认一次 |
| 用户名或安装路径带空格 | 钩子命令里的脚本路径已加引号，可直接用 |
| Windows 配置带 UTF-8 BOM | 已兼容（读配置时剥 BOM），不用手工改文件 |
| 自检报 `[缺失] 索引段为当前版本` | `~/.agents/AGENTS.md` 里是旧版索引段；重跑 `install.py --apply` 会原地替换 |
| 想换安装位置 | 整体搬到 `~/.agents/skills/experience-keeper` 后再跑一次 `--apply`，旧路径的钩子会被替换而不是追加 |

### 升级与卸载

- 升级：`git -C ~/.agents/skills/experience-keeper pull` 后重跑 `install.py --apply`（配置改动前自动备份为 `*.bak-时间戳`）。
- 卸载：删掉 `~/.agents/skills/experience-keeper`，再把各端配置里的 `experience-keeper` 钩子/插件行与 `~/.agents/AGENTS.md` 的「## 经验库」段删除；经验数据在 `~/.agents/experience/`，按需保留或删除。

## 常用命令

| 场景 | 命令 |
| --- | --- |
| 会话开场 | `python scripts/experience.py gate` |
| 查看待复盘 | `python scripts/experience.py review [--all]` |
| 读候选原文 | `python scripts/experience.py show <候选id>` |
| 入库 / 丢弃 | `python scripts/experience.py add --from <id> ...` / `drop <id> --reason carrier\|one_off\|noise\|dup` |
| 检索经验 | `python scripts/experience.py search --query "undefined reference"` |
| 按文件召回 | `python scripts/experience.py search --path tests/unit/test_core.c` |
| 登记表 / 统计 | `python scripts/experience.py library` / `stats` |
| 收紧后复判 | `python scripts/experience.py regate [--apply]` |
| agent 自记备选 | `python scripts/experience.py note --title ... --action ...` |

判据与信号表以 [`docs/experience-prescreen-table-design.md`](docs/experience-prescreen-table-design.md) 为唯一权威，
`SKILL.md` 是 Agent 加载入口。

## 目录结构

```text
experience-keeper/
├── SKILL.md                 # Agent 入口：会话开场、复盘动作、召回时机
├── scripts/
│   ├── experience.py        # 管理 CLI：gate/review/add/drop/library/search/stats/regate/gc
│   ├── experience_hook.py   # 采集入口：解析转录 → 判定信号 → 落候选
│   ├── prescreen.py         # 经验预筛表：信号判定、准入闸门、载体识别
│   ├── transcripts.py       # 五端转录解析
│   ├── experience_lib.py    # 路径、原子写、脱敏、frontmatter、登记表
│   ├── exit_sentinel.py     # 会话结束哨兵
│   └── install.py           # 五端接线与自检
├── adapters/                # dsh / pi / codemaker 插件
├── docs/                    # 设计文档、解析契约、部署说明
├── schema/                  # 候选与条目写入契约
└── tests/                   # 用例与真实轮次回归样本
```

## 隐私与边界

- 经验与候选只落本机 `~/.agents/experience/`，不写入任何代码仓库、不发起网络请求。
- 凭据形态内容在四个写盘口统一替换为 `[REDACTED]`，命中处数会被打印。
- 条目必须可回溯到转录文件与行号，禁止无证据条目。

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

改判定条件时同步三处：`scripts/prescreen.py`、`docs/experience-prescreen-table-design.md`、`tests/test_experience.py`。

## License

[MIT](LICENSE)
