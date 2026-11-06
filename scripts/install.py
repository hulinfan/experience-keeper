#!/usr/bin/env python3
"""五端接线安装脚本。

skill 真身就在全局目录 ~/.agents/skills/experience-keeper/，不建任何链接，
五端按各自的方式发现它。本脚本只做注册与索引段维护，不改写已有配置内容，
每次修改前先备份；默认预演，加 --apply 才真正写入。

解释器按本机解析（Windows 用 python，POSIX 常见的是 python3），钩子与全局规则里
写的是解析出的绝对路径；换 Python 或换机器后重跑一次 --apply 即可覆盖。

    python install.py                 # 预演全部（POSIX 上用 python3）
    python install.py --apply         # 执行全部
    python install.py --client claude --apply
    python install.py --check         # 接线自检
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import experience_lib as lib  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILL_HOME = Path.home() / ".agents" / "skills" / "experience-keeper"
HOOK_SCRIPT = SKILL_DIR / "scripts" / "experience_hook.py"
EXPERIENCE_CLI = SKILL_DIR / "scripts" / "experience.py"
SKILL_ENTRY = SKILL_DIR / "SKILL.md"
HOME = Path.home()
AGENTS_GLOBAL = HOME / ".agents" / "AGENTS.md"


def resolved_interpreter() -> str:
    """钩子与全局规则里使用的解释器。

    写死 `python` 在不少 Linux 发行版上直接失败（只有 `python3`，没有 `python`），
    钩子是静默执行的，失败只在数据里表现为「一条候选都没有」。优先用当前解释器的
    绝对路径，取不到时按 python3 → python 顺序找 PATH。
    """
    candidates: list[str] = []
    if sys.executable and Path(sys.executable).is_file():
        candidates.append(sys.executable)
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)
    return candidates[0] if candidates else "python"


# 全局规则与钩子都按本机解析出的解释器写绝对路径：换 Python 后重跑一次 --apply 覆盖。
INTERPRETER = resolved_interpreter()

# 钩子命令的识别标记：配置里带这个文件名就说明是经验采集钩子。
HOOK_MARKER = "experience_hook.py"
# Windows 上判断目录联接用的重解析点属性。
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

INDEX_SECTION = f"""
## 经验库

- 复盘闸门：会话开场跑 `"{INTERPRETER}" "{EXPERIENCE_CLI.as_posix()}" gate`；一次会话只复盘这一次，直到会话退出都不再复盘；它说派发就后台派复盘子 Agent（原样转发 brief），仅主 Agent 派发
- 按需加载：不通读条目；接手任务、调工具前、报错或空结果后、交付前，按现场字样（报错原文、工具名、接口名、路径）`search` 召回，命中才读
- 沉淀入口：用户说「沉淀一下」「记下来」时，读 `{SKILL_ENTRY.as_posix()}` 入库
- 登记表：用户问「经验库有哪些经验」「列出完整经验库」「查询经验召回情况」「查询经验复用情况」时跑 `"{INTERPRETER}" "{EXPERIENCE_CLI.as_posix()}" library`，原样展示表格
"""


def hook_command(client: str) -> str:
    """钩子命令行。两段路径都加引号：用户名或安装目录带空格时未加引号会直接执行失败。"""
    return f'"{INTERPRETER}" "{HOOK_SCRIPT.as_posix()}" --client {client}'


CODEX_HOOK_BLOCK = (
    "\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = \"command\"\n"
    f"command = {json.dumps(hook_command('codex'))}\n"
)

CLIENTS = ("claude", "codex", "pi", "dsh", "codemaker")


class Installer:
    def __init__(self, apply: bool) -> None:
        self.apply = apply
        self.changes: list[str] = []

    def note(self, message: str) -> None:
        self.changes.append(message)
        print(("  改 " if self.apply else "  计划 ") + message)

    def backup(self, path: Path) -> None:
        if not path.exists():
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(path.name + f".bak-{stamp}"))
        print(f"  备份 {path.name}.bak-{stamp}")

    def write_json(self, path: Path, payload: dict, description: str) -> None:
        if self.apply:
            self.backup(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        self.note(description)

    def write_text(self, path: Path, text: str, description: str) -> None:
        if self.apply:
            self.backup(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write(path, text)
        self.note(description)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        """按 LF 落盘。

        Windows 的文本模式会把 `\\n` 翻译成 `\\r\\n`，写出来的线索段就永远不等于
        带 LF 的模板，`--check` 会一直报「索引段为当前版本 缺失」，怎么跑都不收敛。
        """
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)


# --------------------------------------------------------------------------
# 通用
# --------------------------------------------------------------------------


def is_reparse_point(path: Path) -> bool:
    """目录联接与符号链接在 Windows 上都带重解析点属性；普通目录没有。"""
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def agents_consumers() -> dict[str, Path]:
    """五端读取全局规则的位置。"""
    return {
        "dsh": HOME / ".dsh" / "AGENTS.md",
        "claude": HOME / ".claude" / "CLAUDE.md",
        "codex": HOME / ".codex" / "AGENTS.md",
        "codemaker": HOME / ".config" / "codemaker" / "AGENTS.md",
        "pi": HOME / ".pi" / "agent" / "AGENTS.md",
    }


# 规则文件与全局规则的关系；copy 与 drift 都会在规则更新后失效，必须重建。
STATUS_LABELS = {
    "linked": "已指向全局规则",
    "elsewhere": "指向其他文件",
    "copy": "是内容一致的副本",
    "drift": "内容与全局规则不一致",
    "missing": "缺失",
}


def _link_points_to(path: Path, target: Path) -> bool:
    r"""符号链接是否指向目标路径。

    Windows 上 `os.readlink` 返回的目标带 `\\?\` 扩展前缀，相对目标以链接所在目录为基准，
    两种情况都必须归一化后再比。
    """
    text = os.readlink(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = path.parent / candidate
    return os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(os.path.abspath(target))


def agents_link_status(path: Path, global_path: Path | None = None) -> str:
    """判断某端规则文件与全局规则的关系：linked / elsewhere / copy / drift / missing。"""
    target = global_path or AGENTS_GLOBAL
    if path.is_symlink():
        try:
            return "linked" if _link_points_to(path, target) else "elsewhere"
        except OSError:
            return "elsewhere"
    if not path.exists():
        return "missing"
    if not target.exists():
        return "missing"
    try:
        # 副本会随全局规则更新而漂移，只有逐字节比对才能发现。
        return "copy" if path.read_bytes() == target.read_bytes() else "drift"
    except OSError:
        return "drift"


def relink_agents_file(installer: "Installer", path: Path) -> str:
    """把某端规则文件重建为指向全局规则的符号链接，返回结果说明。

    建链失败时退回内容副本，并把漂移交给 `--check` 的逐字节比对发现。不用硬链接兜底：
    全局规则走原子替换写入，硬链接会被打断，留下读旧规则的副本。
    """
    if path.is_symlink() or path.exists():
        installer.backup(path)
        try:
            path.unlink()
        except OSError as error:
            return f"失败：{error}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(AGENTS_GLOBAL)
        return f"已建符号链接 {path} -> {AGENTS_GLOBAL}"
    except OSError:
        pass
    try:
        shutil.copy2(AGENTS_GLOBAL, path)
        return f"建链失败，已退回内容副本 {path}（规则更新后需重跑 --apply）"
    except OSError as error:
        return f"失败：{error}"


def skill_home_status() -> tuple[bool, str]:
    """检查 skill 是否是全局目录下的实体目录。"""
    if not SKILL_HOME.exists():
        return False, f"{SKILL_HOME} 不存在，请把 skill 目录放到这里"
    if is_reparse_point(SKILL_HOME):
        return False, f"{SKILL_HOME} 是链接或目录联接，应改为实体目录"
    if SKILL_HOME.resolve() != SKILL_DIR:
        return False, f"本脚本不在全局目录里运行：{SKILL_DIR}"
    return True, str(SKILL_HOME)


def upsert_hook(entries: list, command: str) -> str:
    """就位或替换经验采集钩子，返回 current / appended / replaced。

    路径迁移后旧命令会留在配置里，只追加会让同一条钩子跑两遍，因此命中标记即替换。
    """
    entry = {"hooks": [{"type": "command", "command": command}]}
    hits = [index for index, item in enumerate(entries) if HOOK_MARKER in json.dumps(item, ensure_ascii=False)]
    if not hits:
        entries.append(entry)
        return "appended"
    if len(hits) == 1 and json.dumps(entries[hits[0]], ensure_ascii=False, sort_keys=True) == json.dumps(entry, ensure_ascii=False, sort_keys=True):
        return "current"
    entries[hits[0]] = entry
    for index in reversed(hits[1:]):
        del entries[index]
    return "replaced"


def upsert_path(entries: list, value: str, marker: str = "experience-keeper") -> str:
    """字符串数组里就位或替换路径，语义同 upsert_hook。"""
    hits = [index for index, item in enumerate(entries) if isinstance(item, str) and marker in item]
    if not hits:
        entries.append(value)
        return "appended"
    if len(hits) == 1 and entries[hits[0]] == value:
        return "current"
    entries[hits[0]] = value
    for index in reversed(hits[1:]):
        del entries[index]
    return "replaced"


# --------------------------------------------------------------------------
# 各端
# --------------------------------------------------------------------------


def replace_index_section(text: str) -> str:
    """把「## 经验库」小节整体换成当前版本的索引段，文件其余内容原样保留。"""
    heading = "## 经验库"
    start = text.find(heading)
    if start < 0:
        return text
    rest = text[start + len(heading) :]
    match = re.search(r"\n## ", rest)
    end = start + len(heading) + (match.start() + 1 if match else len(rest))
    head = text[:start].rstrip("\n")
    tail = text[end:].strip("\n")
    body = INDEX_SECTION.strip("\n")
    parts = [part for part in (head, body, tail) if part]
    return "\n\n".join(parts) + "\n"


def index_section_current(text: str) -> bool:
    return replace_index_section(text).strip() == text.strip()


def read_json_config(path: Path) -> dict:
    """读客户端 JSON 配置。

    Windows 上 PowerShell `Set-Content`/记事本常写出 UTF-8 BOM，带 BOM 时 `json.loads`
    在第一个字符就失败，接线会被判成「配置坏了」。这里统一剥掉 BOM，非对象按空对象处理。
    """
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").lstrip("\ufeff")
    payload = json.loads(raw) if raw.strip() else {}
    return payload if isinstance(payload, dict) else {}


def link_consumers(installer: Installer, announce: bool = True) -> None:
    """把五端的规则文件重建成指向全局规则的链接；未安装的端跳过。

    先跑客户端接线再跑一遍：全新机器上客户端目录可能是这一轮才被建出来的。
    """
    for name, path in agents_consumers().items():
        if not path.parent.exists():
            if announce:
                print(f"  {name} 未安装，跳过规则链接")
            continue
        status = agents_link_status(path)
        if status == "linked":
            if announce:
                print(f"  {name} 规则链接已就位，跳过")
            continue
        installer.note(f"{name} 规则文件{STATUS_LABELS[status]}，重建为指向 {AGENTS_GLOBAL} 的链接")
        if installer.apply:
            print(f"  {relink_agents_file(installer, path)}")


def ensure_global_rules(installer: Installer) -> None:
    """就位全局规则文件里的「## 经验库」索引段；文件不存在就建出来。"""
    if AGENTS_GLOBAL.exists():
        text = AGENTS_GLOBAL.read_text(encoding="utf-8")
        if "## 经验库" not in text:
            installer.write_text(AGENTS_GLOBAL, text.rstrip("\n") + "\n" + INDEX_SECTION, "追加索引段到全局规则")
        elif index_section_current(text):
            print("  索引段已是最新，跳过")
        else:
            installer.write_text(AGENTS_GLOBAL, replace_index_section(text), "更新索引段（旧版只认当前项目、且要求开场强制复盘）")
    else:
        # 全新机器上全局规则文件往往还不存在：没有索引段，客户端不会主动跑 gate，
        # 采集与复盘就永远不启动。这里直接建出来，只写入经验库这一段。
        installer.write_text(AGENTS_GLOBAL, INDEX_SECTION, "创建全局规则文件并写入索引段")


def ensure_store(installer: Installer) -> None:
    """经验库根目录：全新机器上先建出来，接线自检与首轮采集都依赖它存在且可写。"""
    store = HOME / ".agents" / "experience"
    if store.is_dir():
        return
    if installer.apply:
        store.mkdir(parents=True, exist_ok=True)
    installer.note(f"创建经验库目录 {store}")


def install_shared(installer: Installer) -> None:
    print("共享层：")
    ok, detail = skill_home_status()
    print(f"  {'skill 目录就位' if ok else '警告'}：{detail}")
    ensure_global_rules(installer)
    ensure_store(installer)
    link_consumers(installer)


def install_claude(installer: Installer) -> None:
    print("Claude Code：")
    path = HOME / ".claude" / "settings.json"
    if not path.exists():
        print("  未发现配置，跳过")
        return
    payload = read_json_config(path)
    hooks = payload.setdefault("hooks", {})
    command = hook_command("claude")
    changed = False
    labels = {"appended": "追加", "replaced": "替换为当前脚本路径", "current": "已就位"}

    for event in ("Stop", "SessionEnd"):
        status = upsert_hook(hooks.setdefault(event, []), command)
        changed = changed or status != "current"
        what = "采集钩子" if event == "Stop" else "会话结束标记钩子"
        if status == "current":
            print(f"  hooks.{event} {what}已就位，跳过")
        else:
            installer.note(f"hooks.{event} {labels[status]}{what}")

    if changed:
        installer.write_json(path, payload, "写入 hooks（保留其他记录）")


def codex_insert_offset(text: str) -> tuple[int, str]:
    """算出新 Stop 块应插入的字符偏移。

    必须插在原有 `[[hooks.Stop]]` 块之后、`[hooks.state]` 之前：
    插到前面会把已信任钩子的数组下标从 0 顶到 1，触发 Codex 重新确认信任。
    """
    state_at = text.find("\n[hooks.state]")
    if state_at >= 0:
        return state_at + 1, "插到 [hooks.state] 之前（位于原有 Stop 块之后）"
    first = text.find("[[hooks.Stop]]")
    if first >= 0:
        cursor = first
        for line in text[first:].split("\n")[1:]:
            if line.startswith(("[[hooks.Stop", "type =", "command =")):
                cursor += len(line) + 1
                continue
            break
        return cursor, "插到原有 Stop 块之后"
    return len(text.rstrip("\n")) + 1, "追加到文件末尾"


def rewrite_codex_command(text: str, command: str) -> str:
    """把已有 Codex 钩子的 command 行换成当前脚本路径。"""
    return re.sub(
        rf'(?m)^command = ".*{re.escape(HOOK_MARKER)}.*"$',
        f'command = "{command}"',
        text,
    )


def install_codex(installer: Installer) -> None:
    """Codex 的钩子落点随版本变：0.156 起读 `~/.codex/hooks.json`，此前读 `config.toml`。

    有 `hooks.json` 就以它为准：0.156 不再解析 config.toml 里的 `[[hooks.Stop]]`，
    往那儿写会得到一个永远不触发的钩子。替换走 `upsert_hook`，保持数组下标不变
    ——Codex 按 `[hooks.state]` 里的下标记录信任，改下标会让用户重新确认。
    """
    print("Codex：")
    command = hook_command("codex")
    hooks_path = HOME / ".codex" / "hooks.json"
    if hooks_path.exists():
        payload = read_json_config(hooks_path)
        if not isinstance(payload, dict):
            print("  警告：hooks.json 顶层不是对象，跳过")
            return
        hooks = payload.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            print("  警告：hooks.json 的 hooks 字段不是对象，跳过")
            return
        status = upsert_hook(hooks.setdefault("Stop", []), command)
        if status == "current":
            print("  hooks.Stop 采集钩子已就位，跳过")
            return
        installer.write_json(hooks_path, payload, f"hooks.Stop {'追加' if status == 'appended' else '替换为当前脚本路径'}采集钩子（保留其他事件）")
        print("  注意：新钩子首次运行需要 Codex 确认信任")
        return

    path = HOME / ".codex" / "config.toml"
    if not path.exists():
        print("  未发现配置，跳过")
        return
    text = path.read_text(encoding="utf-8")
    if HOOK_MARKER in text:
        updated = rewrite_codex_command(text, command)
        if updated == text:
            print("  钩子已是当前路径，跳过")
        else:
            installer.write_text(path, updated, "更新 Codex 采集钩子的脚本路径")
        return
    offset, where = codex_insert_offset(text)
    updated = text[:offset].rstrip("\n") + "\n" + CODEX_HOOK_BLOCK + text[offset:].lstrip("\n")
    installer.write_text(path, updated, f"追加 [[hooks.Stop]] 采集钩子（{where}）")
    print("  注意：新钩子首次运行需要 Codex 确认信任")


def install_pi(installer: Installer) -> None:
    print("pi：")
    path = HOME / ".pi" / "agent" / "settings.json"
    extension = (SKILL_DIR / "adapters" / "pi" / "experience-keeper.ts").as_posix()
    payload = read_json_config(path)
    status = upsert_path(payload.setdefault("extensions", []), extension)
    if status == "current":
        print("  扩展已就位，跳过")
        return
    installer.write_json(path, payload, f"{'替换' if status == 'replaced' else '注册'}扩展 {extension}")


def install_dsh(installer: Installer) -> None:
    print("dsh：")
    source = SKILL_DIR / "adapters" / "dsh" / "experience-keeper.mjs"
    preset_dir = HOME / ".dsh" / ".agent-presets" / "liangshen"
    if not source.exists():
        print(f"  适配器尚未就绪：{source}")
        return
    if not preset_dir.is_dir():
        # 预设目录属于客户端：不做猜测创建，否则会在没装 dsh 的机器上留下半个预设。
        print(f"  未发现 dsh 预设目录，跳过：{preset_dir}")
        return
    target = preset_dir / "experience-keeper.mjs"
    if installer.apply:
        shutil.copy2(source, target)
    installer.note(f"复制适配器到 {target}")
    if not (preset_dir / "session-events.mjs").exists():
        print("  警告：预设目录缺少 session-events.mjs，插件会因 import 失败而不生效")

    config = preset_dir / "agent.cordis.yml"
    if not config.exists():
        print("  未发现预设配置，跳过插件行")
        return
    text = config.read_text(encoding="utf-8")
    if "experience-keeper" in text:
        print("  插件行已存在，跳过")
        return
    block = "\n# ── 经验采集 ────────────────────────────────────────────────────────────────\n"
    block += "- id: experience-keeper\n  name: ./experience-keeper.mjs\n"
    installer.write_text(config, text.rstrip("\n") + "\n" + block, "在预设追加经验采集插件行")


def install_codemaker(installer: Installer) -> None:
    print("codemaker：")
    path = HOME / ".config" / "codemaker" / "codemaker.json"
    plugin = (SKILL_DIR / "adapters" / "codemaker").as_posix()
    payload = read_json_config(path)
    status = upsert_path(payload.setdefault("plugin", []), plugin)
    if status == "current":
        print("  插件已就位，跳过")
        return
    installer.write_json(path, payload, f"{'替换' if status == 'replaced' else '注册'}插件 {plugin}")


INSTALLERS = {
    "claude": install_claude,
    "codex": install_codex,
    "pi": install_pi,
    "dsh": install_dsh,
    "codemaker": install_codemaker,
}


# --------------------------------------------------------------------------
# 接线自检
# --------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def check_wiring() -> list[tuple[str, bool, str]]:
    """逐项检查五端接线，返回 (检查项, 是否通过, 说明)。"""
    results: list[tuple[str, bool, str]] = []
    ok, detail = skill_home_status()
    results.append(("skill 位于全局目录且为实体目录", ok, detail))

    agents_text = _read_text(AGENTS_GLOBAL)
    results.append(("全局规则索引段", "## 经验库" in agents_text, str(AGENTS_GLOBAL)))
    if "## 经验库" in agents_text:
        results.append(("索引段为当前版本", index_section_current(agents_text), "旧版索引需重跑 install.py --apply"))

    # 副本会随全局规则更新而漂移，逐字节比对才看得见；未安装的端跳过。
    for name, path in agents_consumers().items():
        if not path.parent.exists():
            results.append((f"{name} 全局规则链接", True, "未安装，跳过"))
            continue
        status = agents_link_status(path)
        results.append((f"{name} 全局规则链接", status == "linked", f"{STATUS_LABELS[status]} —— {path}"))

    claude_dir = HOME / ".claude"
    claude_path = claude_dir / "settings.json"
    if not claude_dir.is_dir():
        results.append(("Claude Code 采集钩子", True, "未安装，跳过"))
        results.append(("Claude Code 会话结束钩子", True, "未安装，跳过"))
    else:
        claude_text = _read_text(claude_path)
        claude_ok = HOOK_MARKER in claude_text and HOOK_SCRIPT.as_posix() in claude_text
        results.append(("Claude Code 采集钩子", claude_ok, str(claude_path) if claude_ok else f"路径须为 {HOOK_SCRIPT.as_posix()}"))
        session_end_ok = claude_ok and _json_has_hook(claude_text, "SessionEnd")
        results.append(
            ("Claude Code 会话结束钩子", session_end_ok, "已接线" if session_end_ok else "hooks.SessionEnd 未接线，退出标记只能靠哨兵")
        )

    # Codex 0.156 起钩子在 hooks.json 里；config.toml 只对旧版本有效，两种落点都认。
    codex_dir = HOME / ".codex"
    if not codex_dir.is_dir():
        results.append(("Codex 采集钩子", True, "未安装，跳过"))
    else:
        codex_hooks_path = codex_dir / "hooks.json"
        codex_landing = codex_hooks_path if codex_hooks_path.exists() else codex_dir / "config.toml"
        codex_text = _read_text(codex_landing)
        codex_ok = HOOK_MARKER in codex_text and HOOK_SCRIPT.as_posix() in codex_text
        results.append(("Codex 采集钩子", codex_ok, str(codex_landing) if codex_ok else f"路径须为 {HOOK_SCRIPT.as_posix()}"))

    presets_root = HOME / ".dsh" / ".agent-presets"
    preset_dir = presets_root / "liangshen"
    presets = sorted(path.name for path in presets_root.glob("*")) if presets_root.is_dir() else []
    if not presets_root.is_dir():
        # dsh 没装就没有预设目录：不算接线缺口，否则纯 Codex 用户的自检永远是红的。
        results.append(("dsh 采集插件", True, "未安装，跳过"))
        results.append(("dsh 适配器为当前版本", True, "未安装，跳过"))
    else:
        dsh_ok = any(
            (presets_root / name / "experience-keeper.mjs").exists()
            and "experience-keeper" in _read_text(presets_root / name / "agent.cordis.yml")
            for name in presets
        )
        results.append(("dsh 采集插件", dsh_ok, "预设目录 " + (", ".join(presets) if presets else "未发现")))
        # 预设目录由 dsh-tui 托管，升级会重写 agent.cordis.yml；适配器副本也可能落后于 skill 里的真身。
        # 逐字节比对才看得出「装了但过期」，否则改了适配器却以为已生效。
        installed = preset_dir / "experience-keeper.mjs"
        source = SKILL_DIR / "adapters" / "dsh" / "experience-keeper.mjs"
        dsh_fresh = installed.exists() and source.exists() and installed.read_bytes() == source.read_bytes()
        results.append(
            (
                "dsh 适配器为当前版本",
                dsh_fresh,
                "副本与 skill 真身逐字节一致" if dsh_fresh else f"副本过期或缺席，重跑 install.py --client dsh --apply（{installed}）",
            )
        )

    pi_dir = HOME / ".pi"
    pi_path = (SKILL_DIR / "adapters" / "pi" / "experience-keeper.ts").as_posix()
    if not pi_dir.is_dir():
        results.append(("pi 扩展注册", True, "未安装，跳过"))
    else:
        pi_settings = _read_text(pi_dir / "agent" / "settings.json")
        pi_ok = pi_path in pi_settings
        results.append(("pi 扩展注册", pi_ok, pi_path if pi_ok else "扩展路径须指向全局 skill 目录"))

    codemaker_dir = HOME / ".config" / "codemaker"
    codemaker_path = (SKILL_DIR / "adapters" / "codemaker").as_posix()
    if not codemaker_dir.is_dir():
        results.append(("codemaker 插件注册", True, "未安装，跳过"))
    else:
        codemaker_text = _read_text(codemaker_dir / "codemaker.json")
        codemaker_ok = codemaker_path in codemaker_text
        results.append(("codemaker 插件注册", codemaker_ok, codemaker_path if codemaker_ok else "插件路径须指向全局 skill 目录"))

    store = HOME / ".agents" / "experience"
    writable = store.exists() and os.access(store, os.W_OK)
    results.append(("经验库目录可写", writable, str(store)))

    sentinel = SKILL_DIR / "scripts" / "exit_sentinel.py"
    results.append(("退出哨兵脚本", sentinel.exists(), str(sentinel)))
    return results


def _json_has_hook(text: str, event: str) -> bool:
    try:
        payload = json.loads(text.lstrip("\ufeff"))
    except ValueError:
        return False
    hooks = payload.get("hooks") or {}
    entries = hooks.get(event) or []
    return any("experience_hook.py" in json.dumps(item, ensure_ascii=False) for item in entries)


def run_check() -> int:
    print("接线自检：")
    failures = 0
    for name, ok, detail in check_wiring():
        mark = "通过" if ok else "缺失"
        if not ok:
            failures += 1
        print(f"  [{mark}] {name}  —— {detail}")
    print()
    if failures:
        print(f"{failures} 项待修：重跑 install.py --apply 补齐。")
        return 1
    print("全部接线就绪。")
    return 0


def main(argv: list[str] | None = None) -> int:
    lib.force_utf8_stdio()
    parser = argparse.ArgumentParser(description="经验库五端接线")
    parser.add_argument("--client", choices=("all", *CLIENTS), default="all")
    parser.add_argument("--apply", action="store_true", help="真正写入；缺省只预演")
    parser.add_argument("--skip-shared", action="store_true", help="跳过 pi 规则链接与索引段")
    parser.add_argument("--check", action="store_true", help="只做接线自检，不写任何文件")
    args = parser.parse_args(argv)

    if args.check:
        return run_check()

    ok, detail = skill_home_status()
    if not ok:
        print(f"警告：{detail}")
        print(f"接线会把五端指向 {HOOK_SCRIPT.as_posix()}；请先把 skill 放到 {SKILL_HOME} 再执行。")

    installer = Installer(args.apply)
    if not args.skip_shared:
        install_shared(installer)
    targets = CLIENTS if args.client == "all" else (args.client,)
    failed: list[str] = []
    for client in targets:
        try:
            INSTALLERS[client](installer)
        except (OSError, ValueError, KeyError) as error:
            # 全新机器上缺配置、缺目录、配置格式不对都很常见：一处失败不能拖垮其余四端，
            # 但必须报出来，否则用户会以为全部装好了。
            print(f"  失败：{type(error).__name__}: {error}")
            failed.append(client)
    if not args.skip_shared:
        # 客户端目录可能是这一轮才建出来的（刚写进 settings.json / 插件注册），
        # 再补一遍规则链接，避免「装完还要手动再跑一次」。
        link_consumers(installer, announce=False)
    print()
    print(f"共 {len(installer.changes)} 项改动。" + ("" if args.apply else " 这是预演，确认后加 --apply。"))
    if failed:
        print(f"以下客户端接线失败，修好后重跑 --client <名称> --apply：{', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
