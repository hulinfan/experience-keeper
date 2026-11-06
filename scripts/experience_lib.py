#!/usr/bin/env python3
"""经验库共享基础设施。

experience.py（管理 CLI）与 experience_hook.py（采集入口）共用本模块，
让项目归属、序列化、脱敏三件事各只有一份实现，避免两边口径漂移。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CATEGORIES = ("pitfall", "tooling", "workflow", "decision", "prompt", "domain")
TYPES = ("path", "tool-rule", "antipattern", "recovery", "completion", "constraint")
KINDS = ("original", "reference")
VERIFICATIONS = ("confirmed", "preference", "n/a")
STATUSES = ("active", "outdated", "superseded", "deprecated")
CONFIDENCES = ("high", "medium", "low")
CLIENTS = ("claude", "codex", "dsh", "codemaker", "pi")
# agent 当场自记的备选不属于任何一端，但共用同一份候选契约与目录布局。
MANUAL_CLIENT = "manual"
# 候选来源方全集：五端采到的记录加 agent 自记的备选。
CANDIDATE_CLIENTS = (*CLIENTS, MANUAL_CLIENT)
# 自记备选的专用信号，采集侧不产出，由 `experience.py note` 写入。
NOTE_SIGNAL = "agent_note"
SCOPES = ("global", "project")

# 单字段长度上限：候选保留原文，但不允许整段日志灌进经验库。
MAX_FIELD_CHARS = 2000
STALE_DAYS = 90
BEIJING = timezone(timedelta(hours=8))

# frontmatter 的固定键序，写出来的文件才有稳定的 diff。
ENTRY_KEY_ORDER = (
    "id",
    "title",
    "category",
    "type",
    "kind",
    "scope",
    "project",
    "precondition",
    "tags",
    "action",
    "source_of_truth",
    "evidence",
    "cost",
    "confidence",
    "verification",
    "verification_note",
    "created_at",
    "verified_at",
    "status",
    "supersedes",
    "related",
    "use_count",
    "last_used_at",
)

SECRET_KEY_WORDS = (
    "token",
    "password",
    "passwd",
    "pass",
    "pwd",
    "secret",
    "apikey",
    "authorization",
    "credential",
    "cookie",
    "privatekey",
    "passphrase",
    "accesskey",
)
# 带前缀的令牌即使很短也必须脱敏。前缀是固定字面量：大小写在正则里逐段写死，
# 整体加 (?i) 会让 eyJ 命中 eyj 开头的普通标识符。
SECRET_PREFIX_RE = re.compile(
    r"^(?:sk-proj-|sk-ant-|sk-|ghp_|gho_|ghs_|ghu_|github_pat_|AKIA|ASIA|LTAI"
    r"|xox[baprs]-|glpat-|glcbt-|eyJ|ya29\.)"
)
# 长随机串：含大写、小写与数字三种字符类才判定为凭据，纯十六进制（commit 短号）保留。
BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_\-.]{20,}$")
# JWT 三段头身签：不必含数字，形状本身就是凭据。
JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}(?:\.[A-Za-z0-9_\-]*)?$")
# PEM 私钥整块：多行文本靠块首识别，命中即整段替换。
PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
BEARER_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+([A-Za-z0-9._\-+/=]{8,})")
# 写在句子里的 key=value 凭据，例如 token=xxx、api_key: yyy、PGPASSWORD=zzz。
# pass 只整词命中：password 由前面的分支吃掉，避免 "password" 被截成 "pass" 再匹配 "word"。
SECRET_ASSIGN_RE = re.compile(
    r"(?i)(?<![a-z0-9])(token|password|passwd|pass|pwd|pgpassword|secret|api[_-]?key|apikey|authorization"
    r"|credential|cookie|access[_-]?key|private[_-]?key|client[_-]?secret)(?![a-z0-9])\s*[:=]\s*[^\s,;\"')]+"
)
# 句子里的凭据线索，供写入侧拦截使用；与上面两条替换式正则保持同一份字面量。
CREDENTIAL_HINT_RE = re.compile(
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"
    r"|(?<![a-z0-9])(?:token|password|passwd|pass|pwd|pgpassword|secret|api[_-]?key|apikey|authorization"
    r"|credential|cookie|access[_-]?key|private[_-]?key|client[_-]?secret)(?![a-z0-9])\s*[:=]\s*[^\s,;\"')]+"
)

REDACTED = "[REDACTED]"


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------


def now_iso() -> str:
    """返回北京时间 ISO 字符串，秒级精度。"""
    return datetime.now(BEIJING).replace(microsecond=0).isoformat()


def today() -> str:
    """返回北京时间日期，格式 YYYY-MM-DD。"""
    return datetime.now(BEIJING).strftime("%Y-%m-%d")


def days_since(iso_text: str) -> int:
    """返回给定 ISO 时间距今天数；无法解析时返回一个很大的值。"""
    try:
        stamp = datetime.fromisoformat(iso_text)
    except (TypeError, ValueError):
        return 10 ** 6
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=BEIJING)
    return max(0, (datetime.now(BEIJING) - stamp).days)


# --------------------------------------------------------------------------
# 路径与项目归属
# --------------------------------------------------------------------------


def store_home() -> Path:
    """经验库根目录，可用 EXPERIENCE_HOME 覆盖以便测试隔离。"""
    override = os.environ.get("EXPERIENCE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agents" / "experience"


def force_utf8_stdio() -> None:
    """把标准输出与错误输出钉成 UTF-8。

    Windows 上 Python 默认按本地代码页（如 cp936）写管道，读端按 UTF-8 解码时
    中文全是替换字符，CLI 输出与自检报告都会变成乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            continue


def find_project_root(cwd: str | os.PathLike | None) -> Path:
    """取含 .git 的最近祖先目录；没有则取当前工作目录。"""
    start = Path(cwd).expanduser() if cwd else Path.cwd()
    try:
        current = start.resolve()
    except OSError:
        current = Path.cwd()
    for candidate in [current, *current.parents]:
        for marker in (".git", ".hg", ".svn"):
            if (candidate / marker).exists():
                return candidate
    return current


def _meta_path(name: str) -> Path:
    return store_home() / "projects" / name / "META.json"


def read_meta(name: str) -> dict:
    """读取项目登记信息；缺失或损坏时返回空字典。"""
    path = _meta_path(name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolve_project_name(project_root: Path) -> str:
    """返回该项目的经验目录名。

    默认用项目根目录名；同名目录已被别的项目占用时用父目录名消歧，
    仍冲突则追加序号。META.json 里的 project_root 是唯一权威判据。
    """
    root = str(project_root)
    base = project_root.name or "root"
    parent = project_root.parent.name or ""
    candidates = [base]
    if parent:
        candidates.append(f"{parent}-{base}")
    for suffix in range(2, 100):
        candidates.append(f"{parent}-{base}-{suffix}" if parent else f"{base}-{suffix}")
    for name in candidates:
        recorded = read_meta(name).get("project_root")
        if recorded is None or recorded == root:
            return name
    raise RuntimeError(f"无法为项目 {root} 分配经验目录名")


def ensure_project(project_root: Path) -> str:
    """登记项目并返回目录名。已登记过则原样返回。"""
    name = resolve_project_name(project_root)
    path = _meta_path(name)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "project_root": str(project_root),
            "project_name": name,
            "registered_at": now_iso(),
        }
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return name


def global_dir() -> Path:
    return store_home() / "global"


def project_dir(name: str) -> Path:
    return store_home() / "projects" / name


def candidates_dir(name: str) -> Path:
    return store_home() / "candidates" / name


def candidate_projects() -> list[str]:
    """列出所有仍有待复盘候选的项目目录名。

    候选按项目分目录存放，消费时若只看当前项目，别的项目产生的候选会永远
    躺在磁盘上。任何涉及积压的口径都必须以本函数为入口。
    """
    root = store_home() / "candidates"
    if not root.is_dir():
        return []
    names: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if any(entry.glob("*.jsonl")):
            names.append(entry.name)
    return names


def entry_projects() -> list[str]:
    """列出所有登记过的项目目录名，供全局统计与清理遍历。"""
    root = store_home() / "projects"
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def verdicts_dir() -> Path:
    """候选判决台账目录。

    候选被 `add` 或 `drop` 后就从盘上消失，信号好不好没人记得住；
    台账只记「哪个信号的第几条候选被判成了什么」，供 stats 算转正率。
    """
    return store_home() / "verdicts"


def append_verdict(project_name: str, record: dict) -> Path:
    """追加一条判决记录：decision 为 add 或 drop。"""
    directory = verdicts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{project_name or 'unknown'}.jsonl"
    line = json.dumps(record, ensure_ascii=False)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
    return path


def iter_verdicts() -> list[dict]:
    """读全部判决记录；坏行跳过，不让一条脏数据挡住统计。"""
    root = verdicts_dir()
    if not root.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(root.glob("*.jsonl")):
        for line in read_text(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def pending_dir() -> Path:
    """会话结束标记目录。

    没有会话结束事件的客户端由退出哨兵在进程退出后写标记，供下次会话开场提示。
    """
    return store_home() / "pending"


# --------------------------------------------------------------------------
# 可复用资产登记表
#
# 候选生命周期短（可 drop、可 gc），物理资产长期留在盘上，只靠候选会出现
# 「脚本还在、没人知道」的断层。登记表把路径指针固化成跨会话的索引。
# 它是索引不是主存：由候选 + 文件系统 stat 重算得到，整份删掉可重建。
# --------------------------------------------------------------------------

ASSET_STALE_DAYS = 30
# 候选最长存活天数：超期不转正即淘汰。资产登记表按同一口径，两者对齐避免候选没了资产还在飘。
CANDIDATE_MAX_AGE_DAYS = 30
# 底层与性能类经验的复现周期以月计（网卡队列在百万并发下的中断绑定、连接跟踪容量），
# 用应用层脚本的 30 天口径会把它们清在复现之前。命中这些信号的候选按季度计。
DEEP_LAYER_SIGNALS = (
    "nonfunctional_floor",
    "soft_limit",
    "no_convergence",
    "cost_trap",
)
DEEP_LAYER_MAX_AGE_DAYS = 90


def candidate_ttl(record: dict, default: int = CANDIDATE_MAX_AGE_DAYS) -> int:
    """候选的存活上限：底层与性能类给更长的复核窗口。"""
    signals = {str(item) for item in (record.get("signal") or [])}
    if signals & set(DEEP_LAYER_SIGNALS):
        return DEEP_LAYER_MAX_AGE_DAYS
    return default


def assets_dir() -> Path:
    return store_home() / "assets"


def asset_registry_path() -> Path:
    """单一全局登记表，项目归属写在记录字段里。

    按项目分文件会让「同一个脚本被两个项目改写」的 uses 统计断成两条；
    单一文件便于按 path 判重，也有利于整体重建。
    """
    return assets_dir() / "registry.jsonl"


def asset_id_for(path: str) -> str:
    """资产稳定标识：同一路径改写后仍是同一 id，供人引用与检索。"""
    import hashlib

    normalized = str(path).replace("\\", "/").lower().strip()
    return "asset-" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]


def file_fingerprint(path: str) -> tuple[str, int]:
    """返回 (sha256 前 16 位, 字节数)；文件不可读时返回空指纹。"""
    import hashlib

    try:
        data = Path(path).read_bytes()
    except OSError:
        return "", 0
    return hashlib.sha256(data).hexdigest()[:16], len(data)


def logic_fingerprint(text: str) -> str:
    """逻辑指纹：剥掉注释与空白后再哈希，用于识别「只是格式化或改注释」的改动。

    用严格哈希判同一性会把开发者在 IDE 里补一行注释当成新资产，uses 计数断链，
    跨会话复用证据（uses ≥ 2）就永远攒不够。
    """
    import hashlib
    import re as _re

    stripped = _re.sub(r"/\*.*?\*/", "", text, flags=_re.S)
    stripped = _re.sub(r"(?m)^\s*#.*$", "", stripped)
    # 行尾的 # 注释：`x = 1  # note`。只认 # 前是空白的情况，避免吃掉颜色值 `#fff`。
    stripped = _re.sub(r"(?m)(?<=\s)#.*$", "", stripped)
    stripped = _re.sub(r"(?m)(?<!\S)//.*$", "", stripped)
    stripped = _re.sub(r'"""(?:.|\n)*?"""', '""', stripped)
    stripped = _re.sub(r"'''(?:.|\n)*?'''", "''", stripped)
    stripped = _re.sub(r"\s+", "", stripped)
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]


def is_git_tracked(raw_path: str) -> bool:
    """Check the nearest repository index, including nested repositories and worktrees."""
    path = Path(raw_path).absolute()
    directory = path.parent
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    relative = path.relative_to(directory).as_posix()
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "ls-files", "-z", "--", relative],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and os.fsencode(relative) in result.stdout.split(b"\0")


def load_assets(refresh: bool = False, stale_days: int = ASSET_STALE_DAYS) -> list[dict]:
    """读取登记表；refresh 时重算指纹并按缺失、陈旧改状态。

    状态判定：
    - 文件不存在 → `gone`（保留记录，它是「曾产出过这个资产」的证据）
    - 超过 stale_days 未被再次产出 → `stale`
    - 已转成条目 → `archived`，不再被检索命中
    """
    path = asset_registry_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("path"):
            rows.append(record)
    if not refresh:
        return rows
    rows = [record for record in rows if not is_git_tracked(str(record["path"]))]
    for record in rows:
        if record.get("status") == "archived":
            continue
        if not Path(str(record.get("path"))).exists():
            record["status"] = "gone"
            continue
        digest, size = file_fingerprint(str(record["path"]))
        logic = logic_fingerprint_of(str(record["path"]))
        if digest and record.get("hash") and digest != record["hash"]:
            # 会话外被改：严格指纹变了。逻辑指纹一致说明只是格式或注释，不算漂移。
            if record.get("logic_hash") and logic and logic != record["logic_hash"]:
                record["status"] = "drift_detected"
                record["modified_out_of_band"] = True
        record["hash"] = digest
        record["logic_hash"] = logic
        record["size"] = size
        if record.get("status") not in ("drift_detected",):
            if days_since(str(record.get("last_seen") or "")) > stale_days:
                record["status"] = "stale"
            else:
                record["status"] = "active"
    return rows


def save_assets(rows: list[dict]) -> None:
    """整表重写。行数在千级，重写比就地改更简单也更安全。"""
    path = asset_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    atomic_write_text(path, text + "\n" if text else "")


def upsert_assets(paths: list[str], record: dict, candidate_id_text: str = "") -> int:
    """把本轮产出的资产登记进表，返回登记条数。

    同一 path 再次出现时 `uses` +1、`last_seen` 刷新——`uses ≥ 2` 是唯一不需要人工
    判断的复用证据。拿不到锁或写失败抛异常，由调用方决定吞掉还是报错。
    """
    if not paths:
        return 0
    target = asset_registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = str(record.get("ts") or now_iso())
    written = 0
    with FileLock(target):
        rows = load_assets()
        by_path = {str(row.get("path")): row for row in rows}
        for raw in paths:
            path = str(raw)
            if is_git_tracked(path):
                continue
            digest, size = file_fingerprint(path)
            logic = logic_fingerprint_of(path)
            existing = by_path.get(path)
            if existing is None:
                existing = {
                    "asset_id": asset_id_for(path),
                    "path": path,
                    "hash": digest,
                    "logic_hash": logic,
                    "size": size,
                    "reuse": str(record.get("reuse") or "reusable"),
                    "first_seen": stamp,
                    "last_seen": stamp,
                    "uses": 1,
                    "status": "active",
                    "produced_by": {
                        "project": str(record.get("project_name") or ""),
                        "session": str(record.get("session_id") or ""),
                        "candidate": candidate_id_text,
                    },
                }
                rows.append(existing)
                by_path[path] = existing
            else:
                # 会话外被改过（IDE、vim）：逻辑指纹没变就只是格式或注释，不打断 uses 累积。
                if existing.get("logic_hash") and logic and existing["logic_hash"] != logic:
                    existing["modified_out_of_band"] = True
                existing["uses"] = int(existing.get("uses") or 0) + 1
                existing["last_seen"] = stamp
                existing["hash"] = digest
                existing["logic_hash"] = logic
                existing["size"] = size
                if existing.get("status") == "gone":
                    # 文件曾被删掉又被重新产出，说明它确实在被反复需要。
                    existing["status"] = "active"
                existing["produced_by"] = {
                    "project": str(record.get("project_name") or ""),
                    "session": str(record.get("session_id") or ""),
                    "candidate": candidate_id_text,
                }
            written += 1
        save_assets(rows)
    return written


def logic_fingerprint_of(path: str) -> str:
    """读文件算逻辑指纹；读不到返回空串。"""
    try:
        return logic_fingerprint(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def pending_file(client: str, session_id: str) -> Path:
    short = (session_id or "nosession")[:8]
    safe_client = client if client in CANDIDATE_CLIENTS else "unknown"
    return pending_dir() / f"{safe_client}-{short}.json"


def write_pending(record: dict) -> Path:
    """按 (client, session_id) 幂等写一份会话结束标记。"""
    target = pending_file(str(record.get("client") or ""), str(record.get("session_id") or ""))
    atomic_write_text(target, json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return target


def read_pending(limit: int = 0) -> list[dict]:
    """读取会话结束标记，按时间倒序；损坏的文件直接跳过。"""
    directory = pending_dir()
    if not directory.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("marker_path", str(path))
            records.append(payload)
    records.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    return records[:limit] if limit > 0 else records


def index_path() -> Path:
    return store_home() / "INDEX.md"


def category_dir(scope: str, project: str | None, category: str) -> Path:
    """按 scope 定位某个类别的条目目录。"""
    root = global_dir() if scope == "global" else project_dir(project or "")
    return root / category


def candidate_file(name: str, client: str, session_id: str, date: str) -> Path:
    """候选文件名：日期 + 客户端 + 会话前 8 位。"""
    short = (session_id or "nosession")[:8]
    safe_client = client if client in CANDIDATE_CLIENTS else "unknown"
    return candidates_dir(name) / f"{date}-{safe_client}-{short}.jsonl"


def candidate_id(client: str, session_id: str, turn_id: str, date: str) -> str:
    short = (session_id or "nosession")[:8]
    safe_client = client if client in CANDIDATE_CLIENTS else "unknown"
    return f"{date}-{safe_client}-{short}#t{turn_id}"


def load_candidate_rows(path: Path) -> list[dict]:
    """读取候选文件里的记录；坏行跳过，与 iter_candidates 同口径。"""
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            rows.append(record)
    return rows


def record_matches_id(record: dict, candidate_id_text: str) -> bool:
    """判断一条记录是否就是给定候选。"""
    return candidate_id(
        str(record.get("client") or ""),
        str(record.get("session_id") or ""),
        str(record.get("turn_id") or ""),
        str(record.get("ts") or "")[:10],
    ) == candidate_id_text


def iter_locked_candidates(prefix: str):
    """按文件名前缀定位候选文件，逐个在锁内回调，产出 (路径, 记录列表)。

    同一个前缀可能落在多个项目目录下，所以逐个文件加锁与回调：
    先读后写的窗口必须整个待在锁里，否则并发的追加会被按旧内容重写掉。
    被清空但未删除的候选文件直接跳过：空文件不是候选，别让它出现在积压口径里。
    """
    for path in sorted(store_home().glob(f"candidates/*/{prefix}.jsonl")):
        try:
            if path.stat().st_size == 0:
                continue
        except OSError:
            continue
        with FileLock(path):
            rows = load_candidate_rows(path)
            if not rows:
                continue
            yield path, rows


def find_candidate_in_path(path: Path, candidate_id_text: str) -> int | None:
    """在候选文件里按 id 找行号；调用方必须已持有该文件的锁。"""
    for number, line in enumerate(read_text(path).splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record_matches_id(record, candidate_id_text):
            return number
    return None


def _rewrite_candidate_file(path: Path, drop_turns: set[str], known_rows: list[dict] | None = None) -> bool:
    """丢掉指定 turn 的候选行；文件空了就删文件，返回是否真的动了文件。

    调用方必须已持有该文件的锁，而且 drop_turns 要在锁内算出来：
    行号是易变事实，只有 turn_id 才认得住并发追加。
    """
    rows = known_rows if known_rows is not None else load_candidate_rows(path)
    kept = [row for row in rows if str(row.get("turn_id") or "") not in drop_turns]
    if len(kept) == len(rows):
        return False
    if kept:
        payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in kept) + "\n"
        atomic_write_text(path, payload)
        return True
    if path.exists():
        path.unlink()
    # 锁文件不删：删掉正在被别处等待的锁文件会让两个进程各锁一个 inode。
    return True


def consume_candidate(candidate_id_text: str) -> bool:
    """消费一条候选（复核入库或丢弃）：整段读改写待在锁里，返回是否消费成功。

    锁内重新定位，候选已被别人消费掉就返回 False，不当成错误。
    """
    prefix, _ = parse_candidate_id(candidate_id_text)
    try:
        for path, rows in iter_locked_candidates(prefix):
            hit = next((row for row in rows if record_matches_id(row, candidate_id_text)), None)
            if hit is None:
                continue
            return _rewrite_candidate_file(path, {str(hit.get("turn_id") or "")}, rows)
    except LockTimeout as error:
        # 拿不到锁等于这次改不了：明确报错，不装作成功。
        raise SystemExit(f"候选被其他进程占用，本次未消费：{error}") from None
    return False


def append_candidate(project_name: str, record: dict, timeout: float | None = None) -> Path:
    """按 (client, session_id, turn_id) 幂等写入候选文件，返回落盘路径。

    采集钩子与 `note` 命令共用这一份实现，重复触发同一记录只保留一行。
    拿不到锁抛 LockTimeout，由调用方决定怎么报：钩子吞掉并留诊断，CLI 直接失败。
    """
    date = str(record.get("ts") or "")[:10] or today()
    target = candidate_file(
        project_name, str(record.get("client") or ""), str(record.get("session_id") or ""), date
    )
    key = candidate_key(record)
    with FileLock(target, timeout=timeout):
        rows = [row for row in load_candidate_rows(target) if candidate_key(row) != key]
        rows.append(record)
        payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n"
        atomic_write_text(target, payload)
    return target


def candidate_key(record: dict) -> tuple[str, str, str]:
    """候选的幂等键：同一轮的重复采集只保留一行。"""
    return (
        str(record.get("client") or ""),
        str(record.get("session_id") or ""),
        str(record.get("turn_id") or ""),
    )


def parse_candidate_id(text: str) -> tuple[str, str]:
    """把 candidate-id 拆成 (文件名前缀, turn_id)。"""
    if "#t" not in text:
        raise ValueError(f"候选标识格式应为 <日期>-<client>-<session8>#t<turn>，收到：{text}")
    prefix, turn = text.split("#t", 1)
    return prefix, turn


# --------------------------------------------------------------------------
# 读写：原子写与跨平台文件锁
# --------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str) -> None:
    """写临时文件后原子替换，避免进程中断留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class LockTimeout(RuntimeError):
    """在超时内没拿到文件锁。"""


class FileLock:
    """跨平台文件锁，保护候选文件的读改写。

    锁文件用非阻塞方式反复试拿，超过 timeout 就抛 LockTimeout：Windows 的
    msvcrt 阻塞模式会一直等下去，而钩子路径上无限等待等于卡住客户端轮次结束。
    锁文件本身永不删除：删掉正在被别人等待的锁文件会让新旧持有者各锁一个 inode，
    互斥当场失效。它是零字节的常驻文件，留着无害。
    """

    DEFAULT_TIMEOUT_SEC = 3.0
    POLL_SEC = 0.05

    def __init__(self, target: Path, timeout: float | None = None):
        self.lock_path = target.with_suffix(target.suffix + ".lock")
        self.timeout = self.DEFAULT_TIMEOUT_SEC if timeout is None else timeout
        self._handle = None
        self._use_fcntl = False

    def _try_lock(self) -> None:
        """试拿一次非阻塞锁；拿不到抛 OSError，其余异常照常冒泡。"""
        try:
            import fcntl

            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._use_fcntl = True
        except ImportError:
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            self._use_fcntl = False

    def __enter__(self) -> "FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lock_path, "w", encoding="utf-8")
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                self._try_lock()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise LockTimeout(
                        f"等待 {self.timeout:g} 秒仍未拿到文件锁：{self.lock_path}"
                    ) from None
                time.sleep(self.POLL_SEC)

    def __exit__(self, *exc_info) -> bool:
        try:
            if self._handle is None:
                return False
            if self._use_fcntl:
                import fcntl

                fcntl.flock(self._handle, fcntl.LOCK_UN)
            else:
                import msvcrt

                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            if self._handle is not None:
                self._handle.close()
        return False


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------


def _matches_credential_shape(value: str) -> bool:
    """判断单个词是不是凭据形态：整串与句中出现的都要认。"""
    if PEM_RE.search(value) or SECRET_PREFIX_RE.match(value) or JWT_RE.match(value):
        return True
    if not BLOB_RE.match(value):
        return False
    # 密钥材料不会带空格；多词的句子不按长串判，避免误伤正常描述。
    has_upper = any(char.isupper() for char in value)
    has_lower = any(char.islower() for char in value)
    has_digit = any(char.isdigit() for char in value)
    return has_upper and has_lower and has_digit


def _looks_like_secret(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if _matches_credential_shape(value):
        return True
    # 前缀也可能出现在句子中间：「抓包看到 AWS key AKIA…」必须整段命中。
    return any(_matches_credential_shape(word.strip(",;\"'()[]{}")) for word in value.split())


def has_credential(value) -> bool:
    """判断字段值里是否含凭据形态内容，用于展示侧留痕；不改变值。"""
    if isinstance(value, dict):
        return any(has_credential(item) for item in value.values())
    if isinstance(value, list):
        return any(has_credential(item) for item in value)
    if not isinstance(value, str):
        return False
    return bool(CREDENTIAL_HINT_RE.search(value)) or _looks_like_secret(value)


def is_secret_key(key: str) -> bool:
    """判断字段名是不是在装凭据。

    只认整词或以凭据词结尾的名字：「token」「access_token」「api_key」要脱敏，
    「tokens」「total_tokens」「token_count」是计数，脱敏会把成本指标一起毁掉。
    """
    normalized = re.sub(r"[^a-z0-9]", "", (key or "").lower())
    if not normalized:
        return False
    if normalized in SECRET_KEY_WORDS:
        return True
    return any(normalized.endswith(word) for word in SECRET_KEY_WORDS)


def redact_scalar(value: str, count: list | None = None) -> str:
    """对单个字符串做脱敏，保留其他内容原样；count 用于累计替换处数。"""
    if not isinstance(value, str) or not value:
        return value
    if PEM_RE.search(value):
        if count is not None:
            count[0] += 1
        return REDACTED
    text = BEARER_RE.sub(lambda match: match.group(1) + REDACTED, value)
    text = SECRET_ASSIGN_RE.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    if _looks_like_secret(text):
        if count is not None:
            count[0] += 1
        return REDACTED
    if count is not None and text != value:
        count[0] += 1
    return text


def redact(value, key: str | None = None, count: list | None = None):
    """递归脱敏：命中敏感键名的值整段替换，其余按内容特征判断。

    count 是长度为 1 的列表，调用方传进来即可拿到替换处数：候选与条目都靠它
    告诉用户「这条记录被改写过」，而不是静默换掉原文。
    """
    if isinstance(value, dict):
        cleaned = {}
        for item_key, item_value in value.items():
            if is_secret_key(str(item_key)):
                cleaned[item_key] = REDACTED
                if count is not None:
                    count[0] += 1
            else:
                cleaned[item_key] = redact(item_value, str(item_key), count)
        return cleaned
    if isinstance(value, list):
        return [redact(item, key, count) for item in value]
    if isinstance(value, str):
        if key and is_secret_key(key):
            if count is not None:
                count[0] += 1
            return REDACTED
        return redact_scalar(value, count)
    return value


def truncate(value: str, limit: int = MAX_FIELD_CHARS) -> str:
    """截断过长字段，保留原文头部。"""
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + f"...[截断，原长 {len(value)}]"


# --------------------------------------------------------------------------
# frontmatter：序列化
# --------------------------------------------------------------------------


def _scalar_to_yaml(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    # 不含 #、冒号、引号等 YAML 敏感字符时才用裸标量。
    if re.fullmatch(r"[A-Za-z0-9_./\-]+", text) and text.lower() not in ("null", "true", "false"):
        return text
    return json.dumps(text, ensure_ascii=False)


def _emit(value, indent: int, lines: list) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict) and item:
                lines.append(f"{pad}{key}:")
                _emit(item, indent + 2, lines)
            elif isinstance(item, dict):
                # 空映射必须写成裸 {}；走标量分支会被转义成字符串 "{}"，
                # 读回来是 str，precondition 这类字段会被下游当成非法结构整段失效。
                lines.append(f"{pad}{key}: {{}}")
            elif isinstance(item, list):
                if not item:
                    lines.append(f"{pad}{key}: []")
                else:
                    lines.append(f"{pad}{key}:")
                    _emit(item, indent + 2, lines)
            else:
                lines.append(f"{pad}{key}: {_scalar_to_yaml(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                first = True
                for key, sub in item.items():
                    if isinstance(sub, dict) and sub:
                        if first:
                            lines.append(f"{pad}- {key}:")
                            first = False
                        else:
                            lines.append(f"{pad}  {key}:")
                        _emit(sub, indent + 4, lines)
                    elif isinstance(sub, list):
                        if first:
                            lines.append(f"{pad}- {key}:")
                            first = False
                        else:
                            lines.append(f"{pad}  {key}:")
                        _emit(sub, indent + 4, lines)
                    else:
                        prefix = f"{pad}- " if first else f"{pad}  "
                        lines.append(f"{prefix}{key}: {_scalar_to_yaml(sub)}")
                        first = False
            else:
                lines.append(f"{pad}- {_scalar_to_yaml(item)}")
    else:
        lines.append(f"{pad}{_scalar_to_yaml(value)}")


def dump_entry(meta: dict, body: str) -> str:
    """按固定键序序列化条目。未列出的键追加在末尾，便于将来扩展。"""
    ordered = {key: meta[key] for key in ENTRY_KEY_ORDER if key in meta}
    for key, value in meta.items():
        if key not in ordered:
            ordered[key] = value
    lines: list[str] = []
    _emit(ordered, 0, lines)
    body_text = body.rstrip("\n")
    return "---\n" + "\n".join(lines) + "\n---\n\n" + body_text + "\n"


# --------------------------------------------------------------------------
# frontmatter：解析（只支持本模块写出的子集，遇到意外结构直接报错）
# --------------------------------------------------------------------------


def _split_inline(text: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in text:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            buffer.append(char)
            continue
        if char == ",":
            parts.append("".join(buffer).strip())
            buffer = []
            continue
        buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_scalar(text: str):
    stripped = text.strip()
    if stripped in ("null", "~", ""):
        return None
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    if stripped == "{}":
        return {}
    if stripped == "[]":
        return []
    if stripped.startswith('"'):
        try:
            return json.loads(stripped)
        except ValueError:
            return stripped.strip('"')
    if stripped.startswith("'"):
        return stripped.strip("'")
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_inline(inner)]
    if re.fullmatch(r"-?\d+", stripped):
        return int(stripped)
    return stripped


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_map(lines: list[str], start: int, indent: int) -> tuple[dict, int]:
    result: dict = {}
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        current_indent = _indent_of(line)
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError(f"frontmatter 缩进异常：{line!r}")
        if line.lstrip().startswith("- "):
            break
        key, _, remainder = line.strip().partition(":")
        if not _:
            raise ValueError(f"frontmatter 行缺少冒号：{line!r}")
        remainder = remainder.strip()
        if remainder:
            result[key.strip()] = _parse_scalar(remainder)
            index += 1
            continue
        # 空值：向后看是嵌套映射、列表还是 null
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines) or _indent_of(lines[next_index]) <= indent:
            result[key.strip()] = None
            index = next_index
            continue
        child_indent = _indent_of(lines[next_index])
        if lines[next_index].lstrip().startswith("- "):
            value, index = _parse_list(lines, next_index, child_indent)
        else:
            value, index = _parse_map(lines, next_index, child_indent)
        result[key.strip()] = value
    return result, index


def _parse_list(lines: list[str], start: int, indent: int) -> tuple[list, int]:
    items: list = []
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        current_indent = _indent_of(line)
        if current_indent < indent or not line.lstrip().startswith("- "):
            break
        content = line.lstrip()[2:]
        if ":" in content and not content.startswith(("\"", "'")):
            # 列表项是映射：把首行与后续更深缩进的行一起交给映射解析器。
            block = [" " * (current_indent + 2) + content]
            index += 1
            while index < len(lines):
                following = lines[index]
                if not following.strip():
                    index += 1
                    continue
                if _indent_of(following) <= current_indent:
                    break
                block.append(following)
                index += 1
            item, _ = _parse_map(block, 0, current_indent + 2)
            items.append(item)
            continue
        items.append(_parse_scalar(content))
        index += 1
    return items, index


def parse_entry(text: str) -> tuple[dict, str]:
    """拆出 frontmatter 字典与正文。"""
    if not text.startswith("---"):
        raise ValueError("条目缺少 frontmatter 起始标记")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValueError("条目缺少 frontmatter 结束标记")
    block = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
    lines = [line for line in block.split("\n") if line.strip() and not line.strip().startswith("#")]
    meta, _ = _parse_map(lines, 0, 0)
    return meta, body


def load_entry(path: Path) -> tuple[dict, str]:
    return parse_entry(read_text(path))


def iter_entries(scope: str | None = None, project: str | None = None):
    """遍历条目，产出 (路径, frontmatter, 正文)。"""
    roots: list[tuple[str, str | None, Path]] = []
    if scope in (None, "global"):
        roots.append(("global", None, global_dir()))
    if scope in (None, "project") and project:
        roots.append(("project", project, project_dir(project)))
    for entry_scope, entry_project, root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*/*.md")):
            try:
                meta, body = load_entry(path)
            except (OSError, ValueError) as error:
                # 别的 agent 或人工写坏的条目不能静默消失：留痕，doctor 里能看到。
                log_diagnostic("entry", f"unreadable:{type(error).__name__}", path=str(path), detail=str(error)[:200])
                continue
            meta.setdefault("scope", entry_scope)
            meta.setdefault("project", entry_project)
            yield path, meta, body


# --------------------------------------------------------------------------
# 召回计数与采纳计数
#
# 两个口径分开记，因为可观测性差一截：
# - hit_count 落在检索路径上：search 命中并展示一次，加一。检索动作机械可观测，覆盖满。
# - use_count 落在 use 命令上：agent 主动声明「按这条执行过」，加一。自报，会漏报也会虚报。
# 计数与条目同体（frontmatter），不必另存台账，也不必在统计时回放日志。
# --------------------------------------------------------------------------

# 计数落在不同条目文件上，用一把库级锁串行化，避免每个条目目录散落 .md.lock。
RECALL_LOCK_NAME = ".recall"
# 检索是高频只读命令：等锁上限压到 1 秒，拿不到就放弃计数，不拖住召回。
RECALL_LOCK_TIMEOUT_SEC = 1.0


def _non_negative_int(raw) -> int:
    """frontmatter 读出来的值可能是字符串、None 或被人工写坏：一律折成非负整数。"""
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def hit_count(meta: dict) -> int:
    """读召回次数。存量条目没有 hit_count 时，旧 use_count 记的就是召回次数。"""
    if "hit_count" in meta:
        return _non_negative_int(meta.get("hit_count"))
    return _non_negative_int(meta.get("use_count"))


def use_count(meta: dict) -> int:
    """读采纳次数。存量条目的 use_count 是召回口径，不能当采纳算。"""
    if "hit_count" not in meta:
        return 0
    return _non_negative_int(meta.get("use_count"))


def use_failed_count(meta: dict) -> int:
    return _non_negative_int(meta.get("use_failed_count"))


def normalize_counts(meta: dict) -> dict:
    """把存量条目的旧 use_count（召回口径）搬进 hit_count，use_count 归零。

    只在写盘口调用：读路径按 hit_count() / use_count() 的口径分离就够了，
    不为了统计去改用户文件。
    """
    if "hit_count" in meta:
        return meta
    meta["hit_count"] = _non_negative_int(meta.get("use_count"))
    meta.setdefault("last_hit_at", None)
    if meta["last_hit_at"] is None:
        meta["last_hit_at"] = meta.get("last_used_at")
    meta["use_count"] = 0
    meta["last_used_at"] = None
    return meta


def recall_lock(timeout: float = RECALL_LOCK_TIMEOUT_SEC) -> "FileLock":
    return FileLock(store_home() / RECALL_LOCK_NAME, timeout=timeout)


def _update_entries(paths, updater) -> tuple[dict[str, int], list[str]]:
    """锁内重读条目、交给 updater 改 meta 再原子替换；返回 ({路径: 新值}, 失败原因)。

    检索与采纳计数都是别的命令上的旁路，写盘时别人可能正在同一文件上做
    add --force 或 promote，拿内存里的旧 meta 覆盖会把对方的改动抹掉。
    单次调用里每个路径只改一次，重复传入不会叠加。
    """
    results: dict[str, int] = {}
    failures: list[str] = []
    targets = list(dict.fromkeys(Path(path) for path in paths))
    if not targets:
        return results, failures
    try:
        with recall_lock():
            for target in targets:
                try:
                    meta, body = load_entry(target)
                except (OSError, ValueError) as error:
                    failures.append(f"{target.name}: {error}")
                    continue
                meta = normalize_counts(meta)
                value = updater(meta)
                try:
                    atomic_write_text(target, dump_entry(meta, body))
                except OSError as error:
                    failures.append(f"{target.name}: {error}")
                    continue
                results[str(target)] = value
    except LockTimeout as error:
        failures.append(str(error))
    return results, failures


def bump_hit(paths, stamp: str | None = None) -> tuple[dict[str, int], list[str]]:
    """给检索命中并展示的条目记召回（hit_count），返回 ({路径: 新次数}, 失败原因)。"""
    stamp = stamp or now_iso()

    def updater(meta: dict) -> int:
        meta["hit_count"] = hit_count(meta) + 1
        meta["last_hit_at"] = stamp
        return meta["hit_count"]

    return _update_entries(paths, updater)


def bump_use(paths, result: str = "ok", note: str = "", stamp: str | None = None) -> tuple[dict[str, int], list[str]]:
    """记一条经验的采纳（use_count）：agent 声明按它执行过，失败也记，落 use_failed_count。"""
    stamp = stamp or now_iso()

    def updater(meta: dict) -> int:
        meta["use_count"] = use_count(meta) + 1
        meta["last_used_at"] = stamp
        meta["last_use_result"] = result
        if note:
            meta["last_use_note"] = note
        if result == "failed":
            meta["use_failed_count"] = use_failed_count(meta) + 1
        return meta["use_count"]

    return _update_entries(paths, updater)


# --------------------------------------------------------------------------
# 采集诊断：静默失效必须留痕
# --------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent
DIAGNOSTIC_KEEP_DAYS = 14


def diagnostics_dir() -> Path:
    return store_home() / "diagnostics"


def log_diagnostic(stage: str, reason: str, **fields) -> None:
    """追加一条采集诊断。

    正常但没有命中的轮次（no_signal）不记：诊断只装异常与可恢复问题，
    这样「为什么这轮没候选」与「解析器是不是坏了」才分得开。
    写诊断本身出错一律吞掉，采集链路不允许被诊断拖累。
    """
    record: dict = {"ts": now_iso(), "stage": stage, "reason": reason}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        record[key] = value
    try:
        target = diagnostics_dir() / f"{today()}.jsonl"
        # 诊断是旁路：等锁只等一瞬，丢了也不影响采集，绝不拖慢客户端轮次结束。
        with FileLock(target, timeout=0.2):
            existing = read_text(target) if target.exists() else ""
            atomic_write_text(target, existing + json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001
        return


def iter_diagnostics(days: int = DIAGNOSTIC_KEEP_DAYS):
    """遍历最近若干天的诊断，产出 (日期, 记录)。文件名损坏或行损坏都跳过。"""
    directory = diagnostics_dir()
    if not directory.is_dir():
        return
    cutoff = datetime.now(BEIJING).date() - timedelta(days=max(0, days - 1))
    for path in sorted(directory.glob("*.jsonl")):
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        for line in read_text(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield path.stem, record


# --------------------------------------------------------------------------
# 候选 JSON Schema 校验
# --------------------------------------------------------------------------


def candidate_schema_path() -> Path:
    return SKILL_DIR / "schema" / "candidate-v1.schema.json"


def entry_schema_path() -> Path:
    return SKILL_DIR / "schema" / "entry-v1.schema.json"


def _load_schema(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_candidate_schema() -> dict:
    """读取候选 schema；缺失或损坏时返回空字典，调用方按「不校验」处理。"""
    return _load_schema(candidate_schema_path())


def load_entry_schema() -> dict:
    """读取条目 schema；缺失或损坏时返回空字典。"""
    return _load_schema(entry_schema_path())


_TYPE_NAMES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _type_ok(value, name) -> bool:
    """name 可以是单个类型名，也可以是类型名数组（对应 JSON Schema 的 type 数组）。"""
    if isinstance(name, list):
        return any(_type_ok(value, item) for item in name)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    expected = _TYPE_NAMES.get(name)
    return True if expected is None else isinstance(value, expected)


def _type_label(name) -> str:
    return " 或 ".join(name) if isinstance(name, list) else str(name)


def as_list(value) -> list:
    """把标量或 None 收敛成列表。

    条目可能被人工编辑，也可能由别的 agent 写出：`tags: cms` 这种标量一旦进入
    集合运算会退化成按字符比较，硬过滤与去重会静默走偏，所以读取端统一收敛。
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _validate_node(value, schema, path: str, errors: list[str]) -> None:
    """按 schema 递归校验。只实现候选 schema 用到的关键字，不追求通用。"""
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if expected is not None and not _type_ok(value, expected):
        errors.append(f"{path}：期望 {_type_label(expected)}，实际 {type(value).__name__}")
        return
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        errors.append(f"{path}：取值 {value!r} 不在 {schema['enum']} 内")
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}：长度 {len(value)} 超过上限 {schema['maxLength']}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}：长度 {len(value)} 低于下限 {schema['minLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}：{value} 小于下限 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}：{value} 超过上限 {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}：元素数 {len(value)} 少于下限 {schema['minItems']}")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_node(item, items, f"{path}[{index}]", errors)
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}.{key}：缺必填字段")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{path}：出现未声明字段 {extra}")
        for key, sub_schema in properties.items():
            if key in value:
                _validate_node(value[key], sub_schema, f"{path}.{key}", errors)


def validate_candidate(record) -> list[str]:
    """按候选 schema 校验一条记录，返回问题清单；空列表表示通过。"""
    schema = load_candidate_schema()
    if not schema:
        return []
    errors: list[str] = []
    _validate_node(record, schema, "candidate", errors)
    return errors


def validate_entry(meta) -> list[str]:
    """按条目 schema 校验 frontmatter，返回问题清单；空列表表示通过。"""
    schema = load_entry_schema()
    if not schema:
        return []
    errors: list[str] = []
    _validate_node(meta, schema, "entry", errors)
    return errors
