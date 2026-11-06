# 五端部署说明

本文记录 `experience-keeper` 在 Claude Code、Codex、dsh、codemaker、pi 五个客户端上的接线方式与验证方法。
设计方案与决策见 `2026-09-22-agent-experience-sedimentation-design.md`，方法论输入见 `agent-experience-engineering.md`。

## 部署目标

一处代码，五端共用。skill 真身放在全局目录 `~/.agents/skills/experience-keeper/`，**不建任何链接**；
经验数据全部落在 `~/.agents/experience/`，不写入任何代码仓库。

```text
~/.agents/skills/experience-keeper/     # 全局目录，代码真身
├── SKILL.md
├── scripts/
│   ├── experience.py              # 管理 CLI（提示、复盘、检索、维护、升级）
│   ├── experience_hook.py         # 采集入口
│   ├── exit_sentinel.py           # 退出哨兵：会话结束标记
│   ├── experience_lib.py          # 路径、原子写、脱敏、frontmatter
│   ├── transcripts.py             # 五端转录解析
│   └── prescreen.py               # 经验预筛表：信号与人类纠正判定
├── adapters/
│   ├── dsh/experience-keeper.mjs  # cordis 插件
│   ├── pi/experience-keeper.ts    # pi 扩展
│   └── codemaker/                 # OpenCode 体系插件
├── docs/
│   ├── 2026-09-22-agent-experience-sedimentation-design.md   # 设计方案与决策
│   ├── agent-experience-engineering.md                       # 方法论输入
│   ├── parsers/<client>.md                                   # 各端转录解析契约
│   └── deployment.md                                         # 本文
├── schema/
│   ├── candidate-v1.schema.json   # 候选记录写入契约
│   └── entry-v1.schema.json       # 条目 frontmatter 写入契约
└── tests/
```

脚本与适配器都不写死仓库路径：`install.py` 由自身位置推导 skill 目录，三个适配器的
hook 脚本默认从 `~/.agents/skills/experience-keeper/scripts/` 解析，可用
`EXPERIENCE_HOOK_SCRIPT` 覆盖。因此把整个目录放到全局位置即可，换机器只需复制目录。

## 一键接线

接线由脚本完成，默认只预演，加 `--apply` 才写入，每次改动前自动备份原文件：

```bash
cd ~/.agents/skills/experience-keeper
python scripts/install.py                # 预演，打印将要做的全部改动
python scripts/install.py --apply        # 执行
python scripts/install.py --client codex --apply   # 只装单端
python scripts/install.py --check        # 只做接线自检，不写任何文件；缺项退出码 1
```

两条维护行为：

- 配置文件里已有指向别处的经验采集钩子时，`--apply` 会把命令**替换**为当前脚本路径，
  不会追加出第二条；`pi` 扩展与 `codemaker` 插件同理。
- `--apply` 会顺带把 `~/.agents/AGENTS.md` 里的「## 经验库」小节刷成当前版本（只替换该小节，
  其余内容原样保留，替换前备份）。

下面各节记录脚本实际做了什么，以及各端单独的手工做法与验证方式。

## 通用前置

无论哪一端，先做这三步。

**第一步，把 skill 放到全局目录。** claude、codex、codemaker 的 skills 目录已经是指向
`~/.agents/skills` 的链接，pi 原生读 `~/.agents/skills/`，因此目录放对位置就完成了发现，
不需要再为 skill 本体建链接：

```powershell
# Windows：整目录复制，排除缓存
robocopy <来源>\experience-keeper "$env:USERPROFILE\.agents\skills\experience-keeper" /E /XD __pycache__
```

```bash
# Linux / macOS
rsync -a --exclude __pycache__ <来源>/experience-keeper/ ~/.agents/skills/experience-keeper/
```

`install.py --check` 会核对这一点：skill 目录必须是实体目录，存在链接或目录联接时判为缺失。

**第二步，给 pi 补全局规则文件。** pi 只读 `~/.pi/agent/AGENTS.md` 与工作目录链上的 `AGENTS.md`，不读 `~/.agents/AGENTS.md`：

```powershell
New-Item -ItemType SymbolicLink -Path "$env:USERPROFILE\.pi\agent\AGENTS.md" `
  -Target "$env:USERPROFILE\.agents\AGENTS.md"
```

```bash
ln -s ~/.agents/AGENTS.md ~/.pi/agent/AGENTS.md
```

必须用符号链接，不要用硬链接或复制：全局规则由编辑工具原子替换更新（临时文件加重命名），硬链接会被打断，留下读旧规则的副本，而且不报错。`install.py` 建链失败时才退回内容副本，此时 `--check` 会逐字节比对全局规则并报「内容与全局规则不一致」，重跑 `--apply` 即重建。五端规则文件都按同一套状态判定：`linked` 通过，`copy`、`drift`、`elsewhere`、`missing` 都算待修。

**第三步，把索引段追加到 `~/.agents/AGENTS.md`。** 这一步会被五端同时读到：

```markdown
## 经验库

- 复盘闸门：会话开场跑 `python "$HOME/.agents/skills/experience-keeper/scripts/experience.py" gate`；一次会话只复盘这一次，直到会话退出都不再复盘；它说派发就后台派复盘子 Agent（原样转发 brief），仅主 Agent 派发
- 按需加载：不通读条目；接手任务、调工具前、报错或空结果后、交付前，按现场字样（报错原文、工具名、接口名、路径）`search` 召回，命中才读
- 沉淀入口：用户说「沉淀一下」「记下来」时，读 `~/.agents/skills/experience-keeper/SKILL.md` 入库
- 登记表：用户问「经验库有哪些经验」「列出完整经验库」「查询经验召回情况」「查询经验复用情况」时跑 `python "$HOME/.agents/skills/experience-keeper/scripts/experience.py" library`，原样展示表格
```

这段是常驻的全部：复盘闸门（含开场命令与「一次会话只复盘一次」的口径）、按需加载、沉淀入口、登记表触发词各一条；命令写法、备选流程、检索打分全部按需从 `SKILL.md` 读，条目正文按现场字样召回时才读。

## 五端接线点

| 端 | 挂载文件 | 键 | 需要的额外参数 |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | `hooks.Stop` 与 `hooks.SessionEnd` 各一项 | `--client claude` |
| Codex | `~/.codex/hooks.json`（0.156 起）或 `~/.codex/config.toml`（旧版本） | `hooks.Stop` / `[[hooks.Stop]]` | `--client codex` |
| dsh | `~/.dsh/.agent-presets/liangshen/agent.cordis.yml` | 插件行 | 无，插件自带 client |
| codemaker | `~/.config/codemaker/codemaker.json` | `plugin` 数组 | 无，插件自带 client |
| pi | `~/.pi/agent/settings.json` | `extensions` 数组 | 无，扩展自带 client |

Claude Code 与 Codex 的原生钩子 payload 里没有客户端标识，也没有轮次标识，所以命令里要带 `--client`；轮次标识由脚本从转录里推出来。

## 会话结束信号

只有 Claude Code 提供原生会话结束钩子。其余四端由「退出哨兵」补：采集脚本在第一次落候选时
派生一个脱离父进程的 `exit_sentinel.py`，哨兵沿父进程链认出客户端主进程并等它退出，
退出后写 `~/.agents/experience/pending/<client>-<session8>.json`。

| 端 | 认进程用的名字 | 等待方式 |
| --- | --- | --- |
| Claude Code | `claude`（先用原生 `SessionEnd`，哨兵只是兜底） | Windows 同步句柄，POSIX 轮询 |
| Codex | `codex` | 同上 |
| dsh | `dsh`，退到宿主运行时 `node`/`bun`/`deno` | 同上 |
| codemaker | `codemaker`、`opencode` | 同上 |
| pi | `pi`，退到宿主运行时 `node`/`bun`/`deno` | 同上 |

约束与边界：

- 哨兵的工作目录钉在 `scripts/`，绝不占用项目目录（Windows 上进程工作目录会被锁住）。
- 父进程链上认不出客户端时不派生哨兵：宁可没有标记，也不能把「钩子进程退出」误判成「会话结束」。
- 同一会话只留一个哨兵，pid 文件在 `~/.agents/experience/sentinel/`，哨兵退出时自删。
- 整棵进程树被强杀（例如 `taskkill /T /F`）时哨兵可能一起死，此时没有标记，候选仍在，开场 `prompt` 照样能看到。
- 默认等 24 小时；pid 被复用时可能迟到或漏写标记，标记只是提示，不影响候选与条目。

## Claude Code

`~/.claude/settings.json` 的 `hooks.Stop` 已有其他记录，**追加一项，不要覆盖**；
`hooks.SessionEnd` 同样追加一项：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python ~/.agents/skills/experience-keeper/scripts/experience_hook.py --client claude"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python ~/.agents/skills/experience-keeper/scripts/experience_hook.py --client claude"
          }
        ]
      }
    ]
  }
}
```

两条钩子共用同一个命令：`Stop` 每轮结束落候选，`SessionEnd` 只写会话结束标记（脚本按
payload 里的 `hook_event_name` 分流，不解析转录）。

冒烟测试：

```bash
printf '%s\n' '{"hook_event_name":"Stop","session_id":"claude-smoke","cwd":"<项目目录>","transcript_path":"~/.claude/projects/<slug>/<uuid>.jsonl"}' \
  | python ~/.agents/skills/experience-keeper/scripts/experience_hook.py --client claude

printf '%s\n' '{"hook_event_name":"SessionEnd","session_id":"claude-smoke","cwd":"<项目目录>"}' \
  | python ~/.agents/skills/experience-keeper/scripts/experience_hook.py --client claude
```

## Codex

落点随版本变，`install.py` 会自动选：存在 `~/.codex/hooks.json`（codex-cli 0.156 起读它）就在它的 `hooks.Stop` 追加一项，否则退回 `~/.codex/config.toml`。

`hooks.json` 形态：

```json
{
  "hooks": {
    "Stop": [
      {"hooks": [{"type": "command", "command": "python ~/.agents/skills/experience-keeper/scripts/experience_hook.py --client codex"}]}
    ]
  }
}
```

`config.toml` 形态（旧版本；位置必须在原有 Stop 块之后、`[hooks.state]` 之前）：

```toml
[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "python ~/.agents/skills/experience-keeper/scripts/experience_hook.py --client codex"
```

两种落点都按数组下标记信任（`[hooks.state]` 里的 `...:stop:2:0`），因此替换旧路径必须原地改那一项，不能删了再追加：下标一变，用户要重新确认信任。

Codex 的 Stop payload 带 `transcript_path`，脚本按 `turn_id` 切出本轮（没有 `turn_id` 时按最后一条用户消息切），并数出压缩事件作为 `compaction_count`。

冒烟测试：

```bash
printf '%s\n' '{"hook_event_name":"Stop","session_id":"<codex-session>","cwd":"<项目目录>","transcript_path":"~/.codex/sessions/2026/09/22/rollout-xxx.jsonl"}' \
  | python ~/.agents/skills/experience-keeper/scripts/experience_hook.py --client codex
```

## dsh

cordis 插件按 `./` 相对路径解析，因此要把适配器复制进预设目录，再补插件行。两步都由安装脚本完成：

```bash
python ~/.agents/skills/experience-keeper/scripts/install.py --client dsh --apply
```

它做两件事：复制 `adapters/dsh/experience-keeper.mjs` 到
`~/.dsh/.agent-presets/liangshen/`，并向同目录的 `agent.cordis.yml` 追加：

```yaml
- id: experience-keeper
  name: ./experience-keeper.mjs
```

插件只订阅 `session/event`，在 `turn/end` 时把本轮事件归一化成转录并调用 `experience_hook.py`，
**不向上下文注入任何内容**，不影响预设的最小注入策略。它依赖预设目录里的 `session-events.mjs`，缺它会因 import 失败而不生效。事件形状按 dsh 0.2 的 `SessionEventMap` 取（`tool/result` 的 `isError` 在 message 级、`content` 是内容块数组），细节见 `docs/parsers/dsh.md`。

预设目录由 `@deepseek-harness-tui/dsh-tui` 托管（`.dsh-tui-managed.json`）：**TUI 升级可能重写
`agent.cordis.yml`，追加的插件行会丢**。`install.py --check` 用「dsh 采集插件」与
「dsh 适配器为当前版本」两项分别盯住插件行与副本新旧，升级后重跑一次 `--apply` 即可。
插件在会话启动时装载，改完适配器要新开会话或重载预设才生效。

## codemaker

codemaker 走 OpenCode 的插件机制，插件是纯 JavaScript，无需构建。把路径加入 `~/.config/codemaker/codemaker.json` 的 `plugin` 数组：

```json
{
  "plugin": [
    "superpowers@git+https://github.com/obra/superpowers.git",
    "~/.agents/skills/experience-keeper/adapters/codemaker"
  ]
}
```

也可以在改配置后用 `codemaker plugin <路径>` 让 CLI 自己写入。会话记录从 `~/.codemaker/session-store/v1/<hash>/<uuid>.json` 读取。

## pi

pi 的扩展支持在 `settings.json` 里注册绝对路径，代码真身留在全局目录，升级 pi 不会覆盖：

```json
{
  "extensions": [
    "~/.agents/skills/experience-keeper/adapters/pi/experience-keeper.ts"
  ]
}
```

扩展订阅 `turn_end`、`session_compact`、`session_shutdown`；`turn_end` 触发采集，`session_compact` 递增压缩计数，`session_shutdown` 做最后刷盘。会话记录从 `~/.pi/agent/sessions/--<path>--/<ts>_<uuid>.jsonl` 读取。

也可以临时验证：`pi -e ~/.agents/skills/experience-keeper/adapters/pi/experience-keeper.ts`

## 环境变量

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `EXPERIENCE_HOME` | 经验库根目录，测试隔离用 | `~/.agents/experience` |
| `EXPERIENCE_HOOK_SCRIPT` | 适配器调用的采集脚本路径 | `~/.agents/skills/experience-keeper/scripts/experience_hook.py` |
| `EXPERIENCE_PYTHON` | 指定 Python 解释器 | `python` |

## 采集诊断

解析失败、事件名不认识、schema 违规、采集入口抛异常都会往
`~/.agents/experience/diagnostics/<日期>.jsonl` 追加一行（`ts`、`stage`、`reason`、`client`、`session_id`）。
正常但没命中信号的轮次不记，所以「候选一直是空的」与「解析器坏了」分得开。

```bash
python ~/.agents/skills/experience-keeper/scripts/experience.py doctor            # 最近 7 天诊断 + 现状
python ~/.agents/skills/experience-keeper/scripts/experience.py doctor --json     # 机器可读
python ~/.agents/skills/experience-keeper/scripts/experience.py validate          # 按 schema 复核历史候选
python ~/.agents/skills/experience-keeper/scripts/experience.py gc --older-than 90d --apply   # 含清理超期诊断
```

`stage` 取值：`stdin`（输入读不了）、`normalize`（客户端或事件不认识）、`parse`（转录解析不出内容）、
`schema`（组装结果违反契约）、`hook`（未预期的异常）。

## 常见问题

**候选目录一直是空的。** 采集只对命中信号的轮次落盘：出错后恢复、重复调用、人类纠正、跨 skill 编排、超长耗时、轮次中断。一轮平平淡淡地聊完不会产生候选。

**Codex、codemaker 的候选变少了。** 这两端的转录没有结构化工具状态字段（实测 Codex 23/23、codemaker 177/177 都没有），工具状态记 `unknown`，因此不产出 `error_recovered`。这是刻意的：用输出文本判失败会把 `grep error`、读日志、`0 failed` 误判成失败。原文仍留在 `tool.error_text_hint` 与 `evidence.sources` 里。

**候选里出现了两次同一轮。** 检查该端适配器是否传了 `turn_id`；原生钩子不带时会由脚本按转录轮次序号补齐，若转录里用户消息数对不上就会出现重复。

**复盘时看到 `compaction_count` 大于 0。** 说明该轮发生在压缩之后，agent 当时看到的是摘要而非完整对话，复核应回到 `evidence.sources` 的行号读原文。

**改了解析器要不要重启客户端。** Claude Code、Codex 需要重启；dsh 需要重载预设；pi 与 codemaker 需要重启客户端。

**换了项目，之前项目的候选怎么办。** 候选按项目分目录，但 `prompt` 与 `review` 都会提示其他项目的积压条数，`review --all` 列出全部；`stats` 与 `gc` 的候选口径也是全局的，不会漏。

**pending 目录里有标记，但没有候选。** 说明该会话的候选已被复盘消费完，标记会在下次 `review` 时自动删除；`gc --older-than` 也会清理超期标记。

**开场提示是一行什么内容。** 形如 `经验库：当前项目 2 条，另有 gwsvc 1 条待复盘候选；最近结束 codex/beef1234（项目 gwsvc）。需要复盘就回复「复盘」，不需要就直接派活。` 无积压时 `prompt` 零输出，开场不产生任何 token 开销。

**开场会不会自己开始复盘。** 不会，也不会拦着当前任务。索引段要求 agent 把提示行原样转达后接着干活，只有用户主动说「复盘」才执行 `review`；候选不自动复盘、不自动转条目。提示行里「已有 N 条经验条目」只说明库里有东西，既不要求开场通读，也不需要用户确认——等改文件、调工具、报错这些动作出现时，agent 按现场字样 `search` 精确召回。

**agent 自记的备选和采到的候选有区别吗。** 只有来源与证据不同：`note` 写的候选 `client` 为 `manual`、`signal` 为 `agent_note`、`evidence.path` 为空，`review`、`show`、`add --from`、`drop` 照常可用。入库时 `--action`、`--root-cause`、`--pre-tool`、`--pre-error`、`--tags` 从 `note` 字段补齐，置信度停在 `low`。

**条目写不进去，报 frontmatter 契约。** `add` 在写盘前按 `schema/entry-v1.schema.json` 校验，违规会列出字段路径并拒绝写入。常见原因是枚举值拼错（`confidence` 只能是 high/medium/low）或标量字段写成了字符串（`precondition.tools` 必须是列表）。手工编辑 Markdown 后可用 `experience.py validate` 复核。

**手动改过的条目会不会读不出来。** frontmatter 解析失败的条目会被跳过并记 `entry/unreadable:*` 诊断，`experience.py doctor` 能看到；标量写法（`tags: cms`）会被读取端按单元素列表收敛，不会静默改变检索结果。

**退出哨兵是不是多了一个常驻进程。** 每个会话一个，只在第一次落候选时派生，Windows 上走同步句柄等待、不轮询，会话退出即结束；认不出客户端进程时不派生。

**skill 之前用链接指向某个仓库，现在怎么办。** 删掉链接，把目录复制到 `~/.agents/skills/experience-keeper`，再从新位置跑一次 `install.py --apply`：五端配置里的旧路径会被替换成新路径，不会留下重复钩子。`install.py --check` 会指出哪些端还指向旧路径。
