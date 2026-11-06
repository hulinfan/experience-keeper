#!/usr/bin/env python3
"""经验库管理 CLI。

复盘（review/show/add/drop）、备选（note）、检索（search）、复用优先（scan）、
维护（index/gc/regate/stats）与升级（promote）都从这里走，
让经验库的读写只有一份实现。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import exit_sentinel  # noqa: E402
import experience_lib as lib  # noqa: E402
import prescreen  # noqa: E402
import transcripts  # noqa: E402

RULE_SECTION_TITLE = "## 经验沉淀出的规则"
# 路径 token：带斜杠的路径，或带扩展名的文件名。用于从正文推导适用文件。
PATH_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]+|\b[A-Za-z0-9_\-]+\.(?:py|sh|ps1|go|c|h|js|mjs|ts|json|ya?ml|toml|ini|conf|cfg|md|tf|sql|mk)\b",
    re.I,
)
# 裸名：正文里点名的测试或脚本（`unit_test_core_ut`、`nat64-runner`），要靠出现次数与存在性采信。
COMPOUND_NAME_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*[_-][A-Za-z0-9_\-]{3,}\b")
# 通用词：认了等于没收窄，直接排除。
GENERIC_PATH_TOKENS = {
    "runner", "test", "tests", "build", "check", "verify", "main", "make-file",
    "unit-test", "unit_test", "run-all", "run_all", "read-me", "read_me",
}
# 建路径索引时跳过的目录：依赖与构建产物里同名文件太多，采信它们等于没定位。
SKIP_DIRS = {
    "node_modules", "vendor", "third_party", "thirdparty", "dist", "build", "out",
    "target", "__pycache__", ".venv", "venv", "site-packages", "obj", "bin",
}
# 判据型信号误报率高：落候选时标 pending，review 里排最前，核实后才转条目。
PENDING_SIGNALS = ("root_cause_mapped", "counterintuitive")
SCAN_DOC_GLOBS = ("*.md",)
KNOWLEDGE_DIRS = ("kb", "knowledge", "wiki")
SCAN_MAX_HITS = 5


# --------------------------------------------------------------------------
# 公共辅助
# --------------------------------------------------------------------------


def current_project() -> tuple[Path, str]:
    root = lib.find_project_root(Path.cwd())
    return root, lib.ensure_project(root)


def make_id(category: str, title: str) -> str:
    """用标题里的英文词生成短 id；标题无英文时退回稳定短哈希。"""
    words = re.findall(r"[A-Za-z0-9]+", title.lower())
    slug = "-".join(words[:4])
    if not slug:
        import hashlib

        slug = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    return f"{category}-{slug}"


def iter_candidates(project_name: str):
    """遍历某项目下的候选，产出 (candidate_id, record, 文件路径, 行号)。

    只读遍历不加锁：读写盘都走原子替换，读到的要么是旧版要么是新版，不会是半截文件。
    需要读改写的地方一律走 lib.consume_candidate，锁内重新定位。
    """
    directory = lib.candidates_dir(project_name)
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.jsonl")):
        prefix = path.stem
        for number, record in enumerate(lib.load_candidate_rows(path), start=1):
            yield f"{prefix}#t{record.get('turn_id')}", record, path, number


def find_candidate(candidate_id: str) -> tuple[dict, Path, int]:
    prefix, _ = lib.parse_candidate_id(candidate_id)
    for path in sorted(lib.store_home().glob(f"candidates/*/{prefix}.jsonl")):
        for number, record in enumerate(lib.load_candidate_rows(path), start=1):
            if lib.record_matches_id(record, candidate_id):
                return record, path, number
    raise SystemExit(f"未找到候选：{candidate_id}")


# --------------------------------------------------------------------------
# review / show / drop
# --------------------------------------------------------------------------


def iter_all_candidates():
    """遍历所有项目的候选，产出 (项目名, candidate_id, 记录, 文件, 行号)。"""
    for project_name in lib.candidate_projects():
        for cid, record, path, line in iter_candidates(project_name):
            yield project_name, cid, record, path, line


def session_end_hints(current: str, limit: int = 5) -> tuple[list[dict], int]:
    """读取会话结束标记，返回 (其他项目的最近结束会话, 已被清理的标记数)。

    标记只用于提示：候选是否存在由候选文件本身决定，标记过期即删。
    """
    live_projects = set(lib.candidate_projects())
    others: list[dict] = []
    stale = 0
    for record in lib.read_pending():
        project_name = str(record.get("project_name") or "")
        path = Path(str(record.get("marker_path") or ""))
        if project_name not in live_projects:
            try:
                path.unlink()
                stale += 1
            except OSError:
                pass
            continue
        if project_name != current:
            others.append(record)
    return others[:limit], stale


def cmd_doctor(args) -> int:
    """采集体检：最近诊断 + 候选与条目现状，用来回答「为什么一直没有候选」。"""
    days = args.days
    rows = list(lib.iter_diagnostics(days))
    report: dict = {
        "days": days,
        "diagnostics": len(rows),
        "by_reason": {},
        "recent": [],
        "candidates": {},
        "newest_candidate": "",
        "entries": 0,
        "stale_entries": 0,
    }

    by_reason: dict[str, int] = {}
    for _, record in rows:
        reason = str(record.get("reason") or "")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    report["by_reason"] = by_reason
    for date, record in rows[-args.limit :]:
        report["recent"].append(
            {
                "ts": record.get("ts"),
                "stage": record.get("stage"),
                "reason": record.get("reason"),
                "client": record.get("client"),
                "session_id": record.get("session_id"),
                "date": date,
            }
        )

    newest = ""
    for owner in lib.candidate_projects():
        report["candidates"][owner] = len(list(iter_candidates(owner)))
        for _, record, _, _ in iter_candidates(owner):
            newest = max(newest, str(record.get("ts") or ""))
    report["newest_candidate"] = newest

    entries = list(lib.iter_entries(scope="global"))
    for name in lib.entry_projects():
        entries.extend(lib.iter_entries(scope="project", project=name))
    report["entries"] = len(entries)
    report["stale_entries"] = sum(
        1 for _, meta, _ in entries if lib.days_since(str(meta.get("verified_at") or "")) > lib.STALE_DAYS
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"采集诊断（最近 {days} 天）：{len(rows)} 条")
    if by_reason:
        detail = "，".join(f"{reason} {count}" for reason, count in sorted(by_reason.items(), key=lambda item: -item[1]))
        print(f"  按原因：{detail}")
        for item in report["recent"]:
            # 采集诊断与写入侧留痕共用这个视图：后者没有会话，硬拼会印出 "None/"。
            where = "/".join(
                part
                for part in (str(item.get("client") or ""), str(item.get("session_id") or "")[:8])
                if part
            )
            print(f"  {item['ts']}  [{item['stage']}] {item['reason']}  {where}")
    else:
        print("  没有异常记录：解析与写入都没报错。")

    if report["candidates"]:
        detail = "，".join(f"{name} {count} 条" for name, count in sorted(report["candidates"].items()))
        print(f"待复盘候选：{detail}（最近落盘 {report['newest_candidate'] or '未知'}）")
    else:
        print("待复盘候选：无。若刚跑过带信号的会话，看上面的诊断。")
    print(f"经验条目：{report['entries']} 条，其中超过 {lib.STALE_DAYS} 天未复核 {report['stale_entries']} 条")
    return 0


def cmd_validate(args) -> int:
    """按 schema 校验已有候选与条目，用来发现历史遗留的形状漂移。"""
    checked = 0
    problems: list[dict] = []
    for owner in lib.candidate_projects():
        for cid, record, path, line in iter_candidates(owner):
            checked += 1
            violations = lib.validate_candidate(record)
            if violations:
                problems.append({"id": cid, "project": owner, "path": str(path), "line": line, "violations": violations})

    entries_checked = 0
    entry_problems: list[dict] = []
    for path, meta, _ in [
        *list(lib.iter_entries(scope="global")),
        *[item for name in lib.entry_projects() for item in lib.iter_entries(scope="project", project=name)],
    ]:
        entries_checked += 1
        violations = lib.validate_entry(meta)
        if violations:
            entry_problems.append({"id": str(meta.get("id") or path.stem), "path": str(path), "violations": violations})

    if args.json:
        print(
            json.dumps(
                {
                    "checked": checked,
                    "problems": problems,
                    "entries_checked": entries_checked,
                    "entry_problems": entry_problems,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if problems or entry_problems else 0

    print(f"候选校验：检查 {checked} 条，问题 {len(problems)} 条。")
    for item in problems[: args.limit]:
        print(f"- {item['id']}（{item['project']}，{item['path']}:{item['line']}）")
        for violation in item["violations"]:
            print(f"    {violation}")
    if len(problems) > args.limit:
        print(f"  …其余 {len(problems) - args.limit} 条省略，用 --json 看全部。")

    print(f"条目校验：检查 {entries_checked} 条，问题 {len(entry_problems)} 条。")
    for item in entry_problems[: args.limit]:
        print(f"- {item['id']}（{item['path']}）")
        for violation in item["violations"]:
            print(f"    {violation}")
    if len(entry_problems) > args.limit:
        print(f"  …其余 {len(entry_problems) - args.limit} 条省略，用 --json 看全部。")

    if problems:
        print(f"\n候选 schema：{lib.candidate_schema_path()}")
    if entry_problems:
        print(f"条目 schema：{lib.entry_schema_path()}")
    return 1 if problems or entry_problems else 0


# --------------------------------------------------------------------------
# 开场闸门：复盘只能由主 agent 派发，锁在派发前抢下；一次会话只复盘一次
#
# 子 agent 继承全局规则，同样会读到「开场派复盘 Agent」。防套娃靠机械闸：
# 主 agent 先跑 gate，gate 抢到 .review.lock 才打印派发指令；复盘期间任何进程
# 再跑 gate 都只会读到锁，拿到「复盘进行中，本次不派发」。
#
# 会话标记再收一层：本会话进过闸门后写 .session-review.json，之后同会话再跑
# gate（哪怕复盘已结束、候选又冒出来）只回「本会话已复盘过」，直到会话退出。
# --------------------------------------------------------------------------

REVIEW_LOCK_TTL_MINUTES = 15
# 会话标记按会话标识失效（客户端进程退出或会话 id 变化），不按项目区分。
# TTL 只防残留：pid 复用时不至于把闸门永久关掉。
SESSION_MARKER_NAME = ".session-review.json"
SESSION_MARKER_TTL_HOURS = 72
REVIEW_REPORT_DIR = "reports"
REVIEW_BRIEF = """你是经验库复盘 Agent，独立处理候选，结束后只回一行统计。

1. 跑 review（当前项目没有候选时跑 review --all），按分组顺序判：载体已覆盖 → 待核实 → 过程信号 → 其余。
2. 证据齐的入库：add --from <候选id> ...；噪声按原因 drop <id> --reason carrier|one_off|noise|dup。
3. 纠正类必须核实：用户对 → --verification confirmed；偏好无对错 → preference；用户说法有误 → drop。
4. 拿不准的一律保留，不要 drop；不放宽准入闸门。
5. 不派发任何子 Agent，不改规则文件，不动已有条目，promote 不做。
6. 结束跑 review --end --summary "入库 x 条、丢弃 y 条、保留 z 条"，只回这一行统计，不回候选正文。"""


def review_lock_path() -> Path:
    return lib.store_home() / ".review.lock"


def _read_review_lock() -> dict | None:
    """读锁：文件不存在、写坏、超过 TTL 都按无锁处理，避免残留锁永久关掉闸门。"""
    try:
        record = json.loads(review_lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    try:
        stamp = datetime.fromisoformat(str(record.get("ts") or ""))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=lib.BEIJING)
    age_minutes = (datetime.now(lib.BEIJING) - stamp).total_seconds() / 60
    if age_minutes > REVIEW_LOCK_TTL_MINUTES:
        return None
    return record


def _acquire_review_lock() -> dict | None:
    """独占创建锁；已有未过期锁返回 None。先抢锁再派发，嵌套进程才看不到派发指令。"""
    path = review_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"pid": os.getpid(), "ts": lib.now_iso(), "cwd": str(Path.cwd())}
    for attempt in (0, 1):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # 过期或写坏的锁先清掉再试一次；有效锁直接放弃。
            if attempt or _read_review_lock() is not None:
                return None
            try:
                path.unlink()
            except OSError:
                return None
            continue
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False)
        return record
    return None


def _release_review_lock() -> bool:
    try:
        review_lock_path().unlink()
        return True
    except OSError:
        return False


def session_marker_path() -> Path:
    return lib.store_home() / SESSION_MARKER_NAME


def _client_process_pid() -> int:
    """沿父进程链认客户端主进程；认不出返回 0，宁可不记标记也不误绑宿主进程。"""
    try:
        table = exit_sentinel.process_table()
        chain = exit_sentinel.parent_chain(os.getpid(), table)
    except Exception:  # noqa: BLE001
        return 0
    known = {name for group in exit_sentinel.CLIENT_PROCESS_NAMES.values() for name in group}
    for pid, name in chain[1:]:
        if name in known:
            return pid
    return 0


def _session_key() -> str:
    """会话标识：优先客户端注入的会话 id，其次客户端主进程 pid（进程退出即会话结束）。"""
    session_id = (os.environ.get("DSH_SESSION_ID") or "").strip()
    if session_id:
        return f"dsh:{session_id}"
    pid = _client_process_pid()
    return f"pid:{pid}" if pid else ""


def _read_session_marker() -> dict | None:
    """读会话标记：写坏、超期、pid 型标记的进程已退出都按无标记处理。"""
    try:
        record = json.loads(session_marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("key"):
        return None
    try:
        stamp = datetime.fromisoformat(str(record.get("ts") or ""))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=lib.BEIJING)
    if (datetime.now(lib.BEIJING) - stamp).total_seconds() / 3600 > SESSION_MARKER_TTL_HOURS:
        return None
    if str(record["key"]).startswith("pid:"):
        try:
            pid = int(str(record["key"]).split(":", 1)[1])
        except ValueError:
            return None
        if pid <= 0 or not exit_sentinel.process_alive(pid):
            return None
    return record


def _mark_session(key: str, project_name: str, candidates: int, dispatched: bool) -> None:
    """落本会话标记；写不进去只提示，不改闸门结论。"""
    if not key:
        return
    record = {
        "key": key,
        "ts": lib.now_iso(),
        "project": project_name,
        "candidates": candidates,
        "dispatched": dispatched,
    }
    try:
        lib.atomic_write_text(session_marker_path(), json.dumps(record, ensure_ascii=False))
    except OSError as error:
        print(f"[!] 会话标记未写入：{error}", file=sys.stderr)


def cmd_gate(args) -> int:
    """开场闸门：本会话未复盘且有候选、无人在复盘时打印派发指令，其余一律不派发。"""
    _, project_name = current_project()
    local = list(iter_candidates(project_name))
    others = {
        name: len(list(iter_candidates(name))) for name in lib.candidate_projects() if name != project_name
    }
    others = {name: count for name, count in others.items() if count}
    total = len(local) + sum(others.values())
    entries = list(lib.iter_entries(project=project_name))
    suffix = f"库内 {len(entries)} 条经验条目。" if entries else ""
    key = _session_key()
    marker = _read_session_marker() if key else None
    reviewed = bool(marker and marker.get("key") == key)
    if not total:
        _mark_session(key, project_name, 0, False)
        print(f"经验库闸门：{project_name} 无待复盘候选，不派发。{suffix}")
        return 0
    held = _read_review_lock()
    if held:
        _mark_session(key, project_name, total, False)
        print(
            f"经验库闸门：复盘进行中（pid {held.get('pid')}，开始于 {held.get('ts')}），"
            f"本次不派发。{suffix}"
        )
        return 0
    if reviewed:
        print(
            f"经验库闸门：本会话已复盘过（{marker.get('ts')}），会话退出前不再复盘，"
            f"本次不派发。{suffix}"
        )
        return 0
    if not _acquire_review_lock():
        print(f"经验库闸门：复盘锁被占用，本次不派发。{suffix}")
        return 0
    _mark_session(key, project_name, total, True)
    scope = f"当前项目 {len(local)} 条" if local else "当前项目无候选，按 --all 跨项目处理"
    print(f"经验库闸门：待复盘候选 {total} 条（{scope}）。后台派发复盘 Agent，brief 原样转发：")
    print()
    print(REVIEW_BRIEF)
    print()
    print("派发后立刻继续处理用户任务，不要等待复盘结果。")
    return 0


def _end_review(summary: str) -> int:
    """复盘收尾：追加当天记录并释放锁；锁释放失败不影响汇报。"""
    _, project_name = current_project()
    text = lib.redact_scalar(summary or "").strip() or "复盘结束"
    today = lib.now_iso()[:10]
    report_dir = lib.store_home() / REVIEW_REPORT_DIR
    target = report_dir / f"{today}.md"
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        header = "" if target.exists() else f"# 经验库复盘记录 {today}\n\n"
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{header}- {lib.now_iso()[11:16]} {project_name}：{text}\n")
    except OSError as error:
        print(f"[!] 复盘记录未写入：{error}", file=sys.stderr)
        lib.log_diagnostic("review", "report_failed", detail=str(error)[:200])
    released = _release_review_lock()
    print(f"复盘已结束{'，闸门锁已释放' if released else ''}。记录：{target}")
    return 0


def cmd_review(args) -> int:
    if getattr(args, "end", False):
        return _end_review(getattr(args, "summary", ""))
    _, project_name = current_project()
    if args.all:
        rows = list(iter_all_candidates())
    else:
        rows = [(project_name, cid, record, path, line) for cid, record, path, line in iter_candidates(project_name)]
    if args.json:
        payload = [
            {
                "project": owner,
                "id": cid,
                "signal": record.get("signal"),
                "user": record.get("user"),
            }
            for owner, cid, record, _, _ in rows
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.all:
        grouped: dict[str, list] = {}
        for owner, cid, record, _, _ in rows:
            grouped.setdefault(owner, []).append((cid, record))
        if not grouped:
            print("没有任何项目存在待复盘候选。")
            return 0
        total = sum(len(items) for items in grouped.values())
        print(f"全部项目待复盘候选 {total} 条，分布在 {len(grouped)} 个项目：\n")
        for owner in sorted(grouped):
            print(f"[{owner}] {len(grouped[owner])} 条")
            for cid, record in grouped[owner]:
                print(_candidate_line(cid, record))
            print()
        print("逐条用 show 读原文，再决定 add 或 drop。")
        return 0

    if not rows:
        print(f"项目 {project_name} 没有待复盘候选。")
    else:
        print(f"项目 {project_name} 待复盘候选 {len(rows)} 条：\n")
        for _, hint, items in _review_groups(rows):
            if not items:
                continue
            print(f"{hint}（{len(items)} 条）：")
            for _, cid, record, _, _ in items:
                print(_candidate_line(cid, record))
            print()
        print("逐条用 show 读原文，再决定 add 或 drop。")

    backlog = {name: len(list(iter_candidates(name))) for name in lib.candidate_projects() if name != project_name}
    backlog = {name: count for name, count in backlog.items() if count}
    if backlog:
        detail = "，".join(f"{name} {count} 条" for name, count in sorted(backlog.items()))
        print(f"\n另有项目存在积压：{detail}。用 review --all 查看。")
    others, stale = session_end_hints(project_name)
    for record in others:
        print(
            f"最近结束的会话：{record.get('client')}/{str(record.get('session_id') or '')[:8]}"
            f" 于 {record.get('ts')}（项目 {record.get('project_name')}，"
            f"本地改动 {record.get('candidates')} 条候选）"
        )
    if stale:
        print(f"已清理 {stale} 个候选已消费完的会话结束标记。")
    return 0


def cmd_prompt(args) -> int:
    """开场提示行：库里当前项目有东西时输出一行，没有则什么都不输出。

    开场不该为一个可能不存在的积压付出复盘成本，也不该阻塞用户的第一条指令。
    本命令只读目录、不解析转录，输出行由 agent 原样转达给用户。

    两类线索都要报：
    - 待复盘候选 → 只报数量，派发与否由 gate 决定（复盘走后台子 Agent，不占用这条提示行）
    - 已有经验条目 → 只报「库里有东西」，不等确认也不通读：具体召回由 agent 在动作出现时
      按现场字样做（只报候选会让「有经验但无候选」的新会话不知道库里有东西）
    """
    _, project_name = current_project()
    local = list(iter_candidates(project_name))
    backlog: dict[str, int] = {}
    for owner, _, _, _, _ in iter_all_candidates():
        backlog[owner] = backlog.get(owner, 0) + 1
    others = {name: count for name, count in backlog.items() if name != project_name}
    pending_assets = [
        row for row in lib.load_assets(refresh=True) if row.get("status") in ("active", "stale")
    ]
    entries = list(lib.iter_entries(scope="project", project=project_name))
    if not local and not others and not pending_assets and not entries:
        return 0

    parts = []
    if local:
        parts.append(f"当前项目 {len(local)} 条")
    if others:
        parts.append("另有 " + "、".join(f"{name} {count} 条" for name, count in sorted(others.items())))
    lines = []
    if parts:
        lines.append(f"{'，'.join(parts)}待复盘候选")
    if entries:
        lines.append(f"{project_name} 已有 {len(entries)} 条经验条目")
    if pending_assets:
        # 资产跨会话存续：候选被清掉后，脚本仍在盘上，下次开工要能想起来。
        lines.append(f"{len(pending_assets)} 个未确认的可复用资产")
    # 段与段之间只用「，」：别的项目那段的「另有」写在段内，拼进来会变成「另有 gwsvc 已有 2 条经验条目」。
    line = "经验库：" + "，".join(lines) + "。"
    if entries:
        # 条目只提示存在，不要求开场通读：召回发生在动作出现时，按现场字样精确命中。
        line += "需要时按现场字样 search 召回，不必等我确认。"
    recent, _ = session_end_hints(project_name)
    if recent:
        top = recent[0]
        line += (
            f"最近结束 {top.get('client')}/{str(top.get('session_id') or '')[:8]}"
            f"（项目 {top.get('project_name')}）。"
        )
    if local or others or pending_assets:
        line += "复盘由开场闸门自动派发，直接派活即可。"
    elif entries:
        line += "直接派活即可。"
    print(line)
    return 0


def _candidate_line(cid: str, record: dict) -> str:
    signals = ",".join(record.get("signal") or [])
    correction = record.get("correction") or {}
    kind = f" 纠正={correction.get('kind')}" if correction else ""
    # 一次性产出要在列表里就看得见，复盘才不必逐条 show 才知道不值钱。
    reuse = f" 产出={record.get('reuse')}" if record.get("reuse") == "one_off" else ""
    # 结论已进载体的候选同样在列表里标出：不必 show，确认载体写全即可。
    carrier = f" 载体={record.get('carrier')}" if record.get("carrier") else ""
    note = record.get("note") if isinstance(record.get("note"), dict) else {}
    # 自记备选没有用户提问，改显示它自己的标题。
    text = str(record.get("user") or note.get("title") or record.get("assistant") or "")
    text = lib.redact_scalar(text.replace("\n", " "))[:60]
    return f"- {cid}  [{signals}]{kind}{reuse}{carrier}\n    {text}"


# 过程族信号：命中它们只说明「本轮值得看一眼」，不代表产生了判据。
# 全部由这些信号构成的候选，复盘时扫一眼用户诉求就能判掉。
PROCESS_SIGNALS = (
    "multi_source",
    "asset_query",
    "asset_recipe",
    "blast_radius_guard",
    "evidence_capture",
    "probe_discipline",
    "batch_mutation",
    "failure",
    "slow",
    "retry",
    "retry_loop",
)


def _review_groups(rows: list) -> list[tuple[str, str, list]]:
    """按复盘动作分组：载体已覆盖 → 待核实 → 过程信号 → 其余。"""
    carrier: list = []
    pending: list = []
    process: list = []
    rest: list = []
    for row in rows:
        record = row[2]
        signals = set(record.get("signal") or [])
        if record.get("carrier"):
            carrier.append(row)
        elif record.get("confidence") == "pending":
            pending.append(row)
        elif signals and signals <= set(PROCESS_SIGNALS):
            process.append(row)
        else:
            rest.append(row)
    return [
        ("carrier", "载体已覆盖候选（结论已写进载体，核对写全即可 drop）", carrier),
        ("pending", "待核实候选（判据型信号误报高，必须读原文核实）", pending),
        ("process", "过程信号候选（只有过程信号，扫一眼用户诉求即可判）", process),
        ("rest", "其余候选", rest),
    ]


def cmd_show(args) -> int:
    record, _, _ = find_candidate(args.candidate_id)
    # 展示前脱敏：历史候选可能早于脱敏口径落地，原样打印会把明文送进 agent 上下文。
    # 只改这一份输出，磁盘上的候选不动。
    print(json.dumps(lib.redact(record), ensure_ascii=False, indent=2))
    if lib.has_credential(record):
        print("[!] 该候选原文含凭据形态内容，以上为脱敏视图；磁盘上仍是明文，记得清理。")
    return 0


def cmd_drop(args) -> int:
    try:
        record, _, _ = find_candidate(args.candidate_id)
    except SystemExit:
        record = {}
    if not lib.consume_candidate(args.candidate_id):
        print(f"候选 {args.candidate_id} 已不在候选文件里，无需丢弃。")
        return 0
    lib.append_verdict(
        str(record.get("project_name") or ""),
        {
            "ts": lib.now_iso(),
            "candidate_id": args.candidate_id,
            "turn": _verdict_turn(args.candidate_id),
            "project": record.get("project_name"),
            "signal": record.get("signal") or [],
            "decision": "drop",
            "reason": args.reason,
        },
    )
    print(f"已丢弃候选 {args.candidate_id}（原因：{args.reason}）")
    return 0


def _verdict_turn(candidate_id: str) -> str:
    """台账用转录轮次做键：条目证据里的会话串与候选 id 前缀格式不同，只有轮次一致。"""
    return candidate_id.split("#t", 1)[1] if "#t" in candidate_id else candidate_id


def _regate_turn(record: dict) -> dict:
    """复判用的轮次：优先从原始转录重建。

    候选记录里的 `assistant` 常被截断，判据要看原文才准；转录读不到时退回记录本身。
    """
    evidence = record.get("evidence") or {}
    path = str(evidence.get("path") or "")
    client = str(record.get("client") or "")
    if path and client:
        resolved = Path(os.path.expanduser(path))
        if resolved.exists():
            try:
                parsed = transcripts.parse_transcript(client, str(resolved), str(record.get("turn_id") or ""))
            except Exception:  # noqa: BLE001
                parsed = None
            if parsed is not None:
                return parsed.to_dict()
    return dict(record)


def cmd_regate(args) -> int:
    """用当前准入闸门复判存量候选。

    闸门收紧只对新增候选生效：不回头复判，收紧前落下的噪声会一直躺到 30 天超期，
    复盘时还得逐条人工核对。复判只读不写，`--apply` 才按台账口径丢弃。
    """
    rows = []
    for owner, cid, record, _, _ in iter_all_candidates():
        turn = _regate_turn(record)
        signals = prescreen.detect_signals(turn)
        reason = prescreen.candidate_skip_reason(turn, signals)
        if not reason:
            continue
        rows.append((owner, cid, record, reason, signals))
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "candidate_id": cid,
                        "project": owner,
                        "reason": reason,
                        "signal": record.get("signal") or [],
                        "signals_now": signals,
                    }
                    for owner, cid, record, reason, signals in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"按当前准入闸门复判：{len(rows)} 条候选现在会被跳过。")
        for owner, cid, record, reason, signals in rows:
            print(f"- {cid}  [{owner}]  现在跳过：{reason}")
            print(f"    候选信号：{record.get('signal') or []}")
            print(f"    现在信号：{signals}")
        if rows and not args.apply:
            print("\n这是预演。确认后加 --apply 丢弃这些候选（判决台账记 regate:<原因>）。")
    if not args.apply:
        return 0
    dropped = 0
    for owner, cid, record, reason, _ in rows:
        if not lib.consume_candidate(cid):
            print(f"[!] {cid} 已不在候选文件里，跳过。")
            continue
        lib.append_verdict(
            str(record.get("project_name") or owner),
            {
                "ts": lib.now_iso(),
                "candidate_id": cid,
                "turn": _verdict_turn(cid),
                "project": record.get("project_name"),
                "signal": record.get("signal") or [],
                "decision": "drop",
                "reason": f"regate:{reason}",
            },
        )
        dropped += 1
    print(f"已按当前闸门丢弃 {dropped} 条候选。")
    return 0


# --------------------------------------------------------------------------
# note：agent 自记备选
# --------------------------------------------------------------------------


def cmd_note(args) -> int:
    """把 agent 自己的试错结论记成备选，走复盘流程，不直接生成条目。

    采集侧只在同名工具先错后对时落候选，换工具、换接口、换思路的换路抓不到；
    这类只有当事 agent 知道的结论先记这里，复盘确认后才入库，避免条目膨胀。

    写入前一律脱敏：与采集钩子同口径，凭据形态内容不进磁盘。
    """
    root, project_name = current_project()
    stamp = datetime.now(lib.BEIJING)
    count: list[int] = [0]
    title = lib.redact_scalar(args.title, count)
    action = lib.redact_scalar(args.action, count)
    root_cause = lib.redact_scalar(args.root_cause or "", count)
    detail = lib.redact_scalar(args.detail or "", count)
    args.title, args.action, args.root_cause, args.detail = title, action, root_cause, detail
    record = {
        "ts": stamp.replace(microsecond=0).isoformat(),
        "client": lib.MANUAL_CLIENT,
        "session_id": lib.MANUAL_CLIENT,
        "turn_id": f"note-{stamp:%H%M%S}-{uuid.uuid4().hex[:4]}",
        "project_name": project_name,
        "cwd": str(root),
        "compaction_count": 0,
        "signal": [lib.NOTE_SIGNAL],
        "user": "",
        "assistant": _note_text(args),
        "tools": [],
        "outcome": "success",
        "cost": {"tool_calls": 0, "tokens": 0, "duration_ms": 0},
        "evidence": {"path": "", "lines": []},
        "note": {
            "title": title,
            "action": action,
            "root_cause": root_cause,
            "tags": [tag.strip() for tag in (args.tags or "").split(",") if tag.strip()],
            "precondition": _parse_precondition(args),
        },
    }
    record["note"]["tags"] = lib.redact(record["note"]["tags"], count=count)
    record["note"]["precondition"] = lib.redact(record["note"]["precondition"], count=count)
    record["assistant"] = lib.redact_scalar(record["assistant"], count)
    violations = lib.validate_candidate(record)
    if violations:
        raise SystemExit("备选违反候选契约，已阻止写入：\n  " + "\n  ".join(violations))
    try:
        lib.append_candidate(project_name, record)
    except lib.LockTimeout as error:
        # 自记备选写不进去必须让用户知道：静默返回等于这条经验没落盘。
        raise SystemExit(f"候选文件被占用，备选未写入：{error}") from None
    cid = lib.candidate_id(record["client"], record["session_id"], record["turn_id"], record["ts"][:10])
    print(f"已记入备选 {cid}")
    _log_credential_redaction("note", cid, count)
    _warn_redactions(count)
    print("复盘时用 show 读全文，add --from 入库，drop 丢弃；备选不参与检索，也不会自动生效。")
    return 0


def _log_credential_redaction(stage: str, target: str, count: list[int]) -> None:
    """脱敏命中要留一行采集诊断：doctor 里看得到哪次写入被改写过。"""
    if count[0] <= 0:
        return
    lib.log_diagnostic(stage, "credential_redacted", target=target, redactions=count[0])


def _warn_redactions(count: list[int]) -> None:
    """写入前命中的凭据都已被替换：告诉用户改过，别让脱敏变成静默篡改。"""
    if count[0] <= 0:
        return
    print(
        f"[!] 写入前命中 {count[0]} 处凭据形态内容，已替换为 {lib.REDACTED}；"
        "原文仍在转录或命令历史里，长期留存请自行清理。"
    )


def _note_text(args) -> str:
    """备选正文：现象、弯路、结论各一行；标题由 note.title 承载，不在这里重复。"""
    lines = []
    if args.detail:
        lines.append(f"现象：{args.detail}")
    if args.root_cause:
        lines.append(f"弯路：{args.root_cause}")
    lines.append(f"结论：{args.action}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# scan：复用优先
# --------------------------------------------------------------------------


def scan_carriers(root: Path) -> list[Path]:
    """列出项目内可能已经维护了知识的所有载体。"""
    carriers: list[Path] = []
    for base in (".agents" / Path("skills"), Path(".pi") / "skills", Path("skills")):
        directory = root / base
        if directory.is_dir():
            carriers.extend(sorted(directory.glob("*/SKILL.md")))
            carriers.extend(sorted(directory.glob("*/references/*.md")))
    docs = root / "docs"
    if docs.is_dir():
        carriers.extend(sorted(docs.rglob("*.md")))
    readme = root / "README.md"
    if readme.is_file():
        carriers.append(readme)
    specs = root / "openspec" / "specs"
    if specs.is_dir():
        carriers.extend(sorted(specs.rglob("spec.md")))
    project_md = root / "openspec" / "project.md"
    if project_md.is_file():
        carriers.append(project_md)
    for name in KNOWLEDGE_DIRS:
        directory = root / name
        if directory.is_dir():
            carriers.extend(sorted(directory.rglob("*.md")))
    return carriers


def cmd_scan(args) -> int:
    root, _ = current_project()
    query = (args.query or "").lower()
    results = []
    for path in scan_carriers(root):
        try:
            lines = lib.read_text(path).splitlines()
        except OSError:
            continue
        title = next((line.lstrip("# ").strip() for line in lines if line.startswith("#")), path.stem)
        hits = [number for number, line in enumerate(lines, start=1) if query and query in line.lower()]
        if query and not hits and query not in title.lower():
            continue
        results.append(
            {
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "title": title,
                "hits": hits[:SCAN_MAX_HITS],
            }
        )
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    if not results:
        print("项目内没有检索到覆盖该知识点的载体，可以写原创条目。")
        return 0
    print(f"项目内已有 {len(results)} 个可能覆盖该知识点的载体：\n")
    for item in results:
        location = ",".join(str(number) for number in item["hits"]) or "标题命中"
        print(f"- {item['path']}  ({location})  {item['title']}")
    print("\n命中同一知识点时不要重复记录：可维护的载体直接更新，条目只写引用与差异。")
    return 0


# --------------------------------------------------------------------------
# add：写入条目
# --------------------------------------------------------------------------


def build_body(category: str, action: str, record: dict, root_cause: str, scope_note: str) -> str:
    """按类别生成正文骨架；内容全部来自候选事实，不留空占位。"""
    manual = str(record.get("client") or "") == lib.MANUAL_CLIENT
    correction = record.get("correction") or {}
    if manual:
        # 自记备选没有转录行号，现象段直接用 agent 写下的原文。
        phenomenon = [
            str(record.get("assistant") or "").strip() or "（备选未记录现象）",
            "",
            "候选证据：agent 自记（无转录）",
        ]
    else:
        user = str(record.get("user") or "").strip()
        if record.get("client") and record.get("session_id"):
            evidence_line = (
                f"候选证据：{record.get('client')}/{str(record.get('session_id') or '')[:8]}"
                f"#t{record.get('turn_id')}，转录 {((record.get('evidence') or {}).get('path') or '未记录')}"
            )
        else:
            # add --manual 没有候选：写一行占位，避免出现 "None/#tNone" 这种伪证据。
            evidence_line = "候选证据：手工条目（无候选、无转录）"
        phenomenon = [user or "（候选未记录用户提问）", "", evidence_line]
    if correction.get("agent_did"):
        phenomenon.insert(1, f"被纠正的原做法：{correction['agent_did']}")
    if category == "pitfall":
        return (
            "## 现象\n\n"
            + "\n".join(phenomenon)
            + f"\n\n## 根因\n\n{root_cause}\n\n## 解法\n\n{action}\n"
        )
    scope = scope_note or "本次仅在本轮验证，未发现反例。"
    return (
        "## 现象\n\n"
        + "\n".join(phenomenon)
        + f"\n\n## 结论\n\n{action}\n\n## 适用范围与反例\n\n{scope}\n"
    )


def normalize_title(title: str) -> str:
    """标题归一化：去首尾空白、连续空白压成一个、转小写。"""
    return re.sub(r"\s+", " ", (title or "").strip()).lower()


def find_duplicate(category: str, title: str, tags: list[str], scope: str, project: str | None):
    """按确定性规则找同一经验：标题归一化相等，或 tags 交集达到 2 个。"""
    wanted = normalize_title(title)
    wanted_tags = {str(tag) for tag in lib.as_list(tags)}
    for path, meta, _ in lib.iter_entries(scope=scope, project=project):
        if meta.get("category") != category:
            continue
        if normalize_title(str(meta.get("title") or "")) == wanted:
            return path, meta
        # tags 可能是标量（人工编辑或别的 agent 写的），先收敛成列表再比，避免按字符比较。
        existing_tags = {str(tag) for tag in lib.as_list(meta.get("tags"))}
        if len(wanted_tags & existing_tags) >= 2:
            return path, meta
    return None, None


def cmd_add(args) -> int:
    if args.manual and args.from_id:
        raise SystemExit("--manual 与 --from 互斥：有候选时必须走 --from，保证条目可回溯到转录行号。")
    if args.from_id:
        record, _, _ = find_candidate(args.from_id)
        # 历史候选可能早于脱敏口径落地，入条目前再洗一遍。
        record = lib.redact(record)
    else:
        record = {}
    note = record.get("note") if isinstance(record.get("note"), dict) else {}
    # 自记备选已经把结论与根因写好了，入库时不必重敲。
    if not args.action and note.get("action"):
        args.action = str(note["action"])
    if not args.root_cause and note.get("root_cause"):
        args.root_cause = str(note["root_cause"])
    if not args.title:
        raise SystemExit("写入条目必须提供 --title")
    if args.category == "pitfall" and not args.root_cause and not args.body_file:
        raise SystemExit("pitfall 条目必须提供 --root-cause 或 --body-file")
    if args.category == "decision" and not args.body_file:
        raise SystemExit("decision 条目结构固定，请用 --body-file 提供 ADR 正文")
    if not args.action and not args.body_file:
        raise SystemExit("请用 --action 给出可执行结论，或用 --body-file 提供正文")
    if args.kind == "reference" and not args.source_of_truth:
        raise SystemExit("引用型条目必须提供 --source-of-truth，指明项目内载体的位置与章节")

    # 条目是五端共读的长期资产，落盘前与采集钩子同口径脱敏。
    # 正文由 build_body 用命令行参数拼出来，所以脱敏必须在拼正文之前做。
    count: list[int] = [0]
    for name in (
        "title",
        "action",
        "root_cause",
        "tags",
        "scope_note",
        "verification_note",
        "source_of_truth",
        "pre_tool",
        "pre_error",
        "pre_scene",
    ):
        value = getattr(args, name, None)
        if isinstance(value, str):
            setattr(args, name, lib.redact_scalar(value, count))

    if args.body_file:
        body = lib.redact_scalar(lib.read_text(Path(args.body_file)), count)
    else:
        body = build_body(args.category, args.action, record, args.root_cause or "", args.scope_note or "")

    now = lib.today()
    sessions = []
    if record:
        sessions.append(f"{record.get('client')}/{str(record.get('session_id') or '')[:8]}#t{record.get('turn_id')}")
    sources = []
    evidence = record.get("evidence") or {}
    if evidence.get("path"):
        sources.append({"path": evidence["path"], "lines": evidence.get("lines") or []})

    correction = record.get("correction") or {}
    verification = "n/a"
    verification_note = ""
    if correction:
        if args.verification not in ("confirmed", "preference"):
            raise SystemExit("纠正类条目必须用 --verification confirmed|preference，核实不通过就不要入库")
        verification = args.verification
        verification_note = args.verification_note or ""
        if not verification_note:
            raise SystemExit("纠正类条目必须用 --verification-note 写明独立依据")

    confidence = "low"
    if len(set(sessions)) >= 3 and verification != "preference":
        confidence = "high"
    elif len(set(sessions)) >= 2:
        confidence = "medium"
    if not sources:
        # 没有转录行号可回溯的条目（自记备选、手工条目）证据强度上限就是 low。
        confidence = "low"
    if getattr(args, "confidence", ""):
        # 复核人显式定级：判据型候选核实通过后可提到 medium，不必等到跨三轮。
        confidence = args.confidence

    meta = {
        "id": make_id(args.category, args.title),
        "title": args.title,
        "category": args.category,
        "type": args.type,
        "kind": args.kind,
        "scope": args.scope,
        "project": None if args.scope == "global" else args.project,
        "precondition": _parse_precondition(args, note),
        "tags": [tag.strip() for tag in (args.tags or "").split(",") if tag.strip()] or [
            str(tag) for tag in note.get("tags") or []
        ],
        "action": args.action or "",
        "source_of_truth": args.source_of_truth,
        "evidence": {"sessions": sessions, "counters": [], "sources": sources},
        "cost": {
            "avg_tool_calls": (record.get("cost") or {}).get("tool_calls", 0),
            "avg_tokens": (record.get("cost") or {}).get("tokens", 0),
        },
        "confidence": confidence,
        "verification": verification,
        "verification_note": verification_note,
        "created_at": now,
        "verified_at": now,
        "status": "active",
        "supersedes": None,
        "related": [],
        # 两个口径分开：hit 由 search 机械累加，use 由 use 命令自报。
        "hit_count": 0,
        "last_hit_at": None,
        "use_count": 0,
        "use_failed_count": 0,
        "last_used_at": None,
        "last_use_result": None,
    }
    directory = lib.category_dir(meta["scope"], meta["project"], meta["category"])
    target = directory / f"{meta['id']}.md"
    violations = lib.validate_entry(meta)
    if violations:
        # 条目是五端共读的资产，写坏一条会让别的 agent 认不出来，这里直接拒绝写入。
        raise SystemExit("条目违反 frontmatter 契约，已阻止写入：\n  " + "\n  ".join(violations))
    existing_path, existing_meta = find_duplicate(
        meta["category"], meta["title"], meta["tags"], meta["scope"], meta["project"]
    )
    if existing_path is not None and not args.force:
        raise SystemExit(
            f"已有同类条目 {existing_meta.get('id')}（{existing_path}）："
            "同一条经验不要新增，请走 supersede 更新旧条目，或用 --force 强制新建。"
        )
    if target.exists() and not args.force:
        raise SystemExit(f"条目已存在：{target}。确要覆盖请加 --force，或改用 supersede 流程。")
    lib.atomic_write_text(target, lib.dump_entry(meta, body))
    if args.from_id:
        if not lib.consume_candidate(args.from_id):
            # 条目已写成功，候选多半是被并发消费掉了：报出来，但不回滚条目。
            print(f"[!] 候选 {args.from_id} 在写入期间已被消费，未能清理，请用 review 确认。")
        lib.append_verdict(
            str(record.get("project_name") or meta.get("project") or ""),
            {
                "ts": lib.now_iso(),
                "candidate_id": args.from_id,
                "turn": _verdict_turn(args.from_id),
                "project": record.get("project_name") or meta.get("project"),
                "signal": record.get("signal") or [],
                "decision": "add",
                "entry_id": meta.get("id"),
            },
        )
    archived = archive_assets_of(args.from_id)
    print(f"已写入条目 {target}")
    if archived:
        # 资产转成条目后归档：后续检索由条目承担，登记表不再重复提示。
        print(f"已归档 {archived} 个资产登记（条目已成为它的出口）")
    _log_credential_redaction("add", str(target), count)
    _warn_redactions(count)
    write_library_table()
    return 0


def archive_assets_of(candidate_id_text: str) -> int:
    """把某个候选登记过的资产标为已归档，返回条数。

    条目写成功了才归档：归档意味着「这条知识有正式出口了」，条目没落地就不能标。
    """
    if not candidate_id_text:
        return 0
    rows = lib.load_assets()
    changed = 0
    for row in rows:
        produced = row.get("produced_by") if isinstance(row.get("produced_by"), dict) else {}
        if str(produced.get("candidate") or "") != candidate_id_text:
            continue
        if row.get("status") == "archived":
            continue
        row["status"] = "archived"
        changed += 1
    if changed:
        lib.save_assets(rows)
    return changed


def _parse_precondition(args, note: dict | None = None) -> dict:
    """命令行的前置条件；没给的部分从自记备选的 note 里补齐。"""
    precondition: dict = {}
    if args.pre_tool:
        precondition["tools"] = [item.strip() for item in args.pre_tool.split(",") if item.strip()]
    if args.pre_error:
        precondition["errors"] = [item.strip() for item in args.pre_error.split(",") if item.strip()]
    if getattr(args, "pre_files", ""):
        precondition["files"] = [item.strip() for item in args.pre_files.split(",") if item.strip()]
    if args.pre_scene:
        precondition["scene"] = args.pre_scene
    auto = (note or {}).get("precondition") or {}
    for key in ("tools", "errors", "files", "scene"):
        if not precondition.get(key) and auto.get(key):
            precondition[key] = auto[key]
    return precondition


# --------------------------------------------------------------------------
# search：硬过滤 + 打分
# --------------------------------------------------------------------------


def _precondition_text(meta: dict) -> str:
    """把前置条件拍平成检索文本：现场报错原文就存在这里。"""
    precondition = meta.get("precondition")
    if isinstance(precondition, str):
        # 历史条目出现过 precondition: "{}" 这种被转义成字符串的写法。
        return precondition
    if not isinstance(precondition, dict):
        return ""
    parts: list[str] = []
    for key in ("tools", "errors", "files", "scene"):
        parts.extend(str(item) for item in lib.as_list(precondition.get(key)))
    return " ".join(parts)


def _path_matches(meta: dict, focus_path: str) -> tuple[bool, bool]:
    """判断条目与当前文件的关系，返回 (是否适用, 是否声明过 files)。

    声明了 files 的按它判：带扩展名的按后缀比，目录按前缀比。
    没声明的返回 (False, False)——调用方据此把它列成「可能相关」而不是直接丢掉，
    存量条目大多没声明 files，一刀切排除等于让按路径召回对老条目完全失效。
    """
    precondition = meta.get("precondition")
    declared = []
    if isinstance(precondition, dict):
        declared = [str(item).replace("\\", "/").lower() for item in lib.as_list(precondition.get("files"))]
    if not declared:
        return False, False
    current = focus_path.replace("\\", "/").lower()
    for item in declared:
        if "." in item.rsplit("/", 1)[-1]:
            if current.endswith(item):
                return True, True
        elif item in current:
            return True, True
    return False, True


def derive_files(meta: dict, body: str, root: Path | None = None, index: dict[str, str] | None = None) -> list[str]:
    """从条目正文与字段里推导适用文件，供存量补全。

    取两类线索：带斜杠或扩展名的路径 token，以及带下划线或连字符的复合名
    （`unit_test_core_ut` 这类测试与脚本名）。两者都要求出现 ≥2 次：
    正文里随手提一句的名字不是「这条经验适用于它」。

    给了 root 就顺带确认存在性：存在的写成仓内相对路径，不存在的按目录前缀处理
    （补 `/`），让 `--path` 的子串匹配不会误命中别的名字。
    推导有偏差可以在复盘时改条目 precondition.files，比让老条目永远召不回要好。
    """
    text = f"{meta.get('title') or ''}\n{meta.get('action') or ''}\n{body}"
    counts: dict[str, int] = {}
    for token in PATH_TOKEN_RE.findall(text) + COMPOUND_NAME_RE.findall(text):
        cleaned = token.strip("`'\"()[]<>，。：;,")
        if cleaned:
            counts[cleaned] = counts.get(cleaned, 0) + 1
    candidates = [token for token, count in counts.items() if count >= 2 and not is_generic_token(token)]
    for token in PATH_TOKEN_RE.findall(str(meta.get("source_of_truth") or "")):
        cleaned = token.strip("`'\"()[]<>，。:;,")
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    if root is not None:
        index = index if index is not None else build_path_index(root)
        candidates = [_resolve_token(token, root, index) for token in candidates]
    return candidates[:10]


def is_generic_token(token: str) -> bool:
    """通用词与大写常量不做适用文件线索。

    `runner`、`build` 这类词到处都是，认了等于没收窄；
    `GW_TYPE`、`NAT64_GW` 是常量或环境变量名，文件名不会全大写。
    """
    if token.lower() in GENERIC_PATH_TOKENS:
        return True
    return not re.search(r"[a-z]", token)


def _resolve_token(token: str, root: Path, index: dict[str, str] | None = None) -> str:
    """把 token 落成仓内相对路径；不存在的按目录前缀处理，避免子串误命中。

    查表而不是逐个 rglob：大仓上每个 token 一次全仓遍历会直接超时。
    """
    if "/" in token or "\\" in token:
        return token.replace("\\", "/")
    if index is not None:
        resolved = index.get(token.lower())
        return resolved if resolved else f"{token}/"
    return f"{token}/"


def build_path_index(root: Path, limit: int = 20000) -> dict[str, str]:
    """一次有界遍历建 basename → 仓内相对路径的索引。

    规则：深度 ≤6、跳过隐藏目录与常见依赖目录、最多收 limit 个条目。
    只在唯一同名时采信（唯一性由调用方按命中次数判断，这里只记录首个）。
    """
    index: dict[str, str] = {}
    seen = 0
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > 6 or seen >= limit:
            continue
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            seen += 1
            if seen >= limit:
                break
            if child.is_dir():
                stack.append((child, depth + 1))
                continue
            try:
                relative = str(child.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            index.setdefault(name.lower(), relative)
            if "." in name:
                # 以最后一个点之前的完整名字也建一条，`unit_test_core_ut` 可能是目录前缀。
                continue
            index[name.lower()] = relative
    return index


def apply_derived_files(entries: list, apply: bool, root: Path | None = None) -> list[tuple[str, list[str]]]:
    """给没声明 files 的条目补上推导结果，返回改动清单。"""
    changed: list[tuple[str, list[str]]] = []
    index = build_path_index(root) if root is not None else None
    for path, meta, body in entries:
        precondition = meta.get("precondition")
        if isinstance(precondition, dict) and lib.as_list(precondition.get("files")):
            continue
        derived = derive_files(meta, body, root, index)
        if not derived:
            continue
        changed.append((str(meta.get("id") or Path(path).stem), derived))
        if not apply:
            continue
        if not isinstance(precondition, dict):
            precondition = {}
        precondition["files"] = derived
        meta["precondition"] = precondition
        lib.atomic_write_text(Path(path), lib.dump_entry(meta, body))
    return changed


def precondition_hits(meta: dict, query: str, tool: str | None, error: str | None) -> int:
    """统计 precondition 命中维度，并执行显式维度过滤。"""
    precondition = meta.get("precondition") or {}
    if isinstance(precondition, str):
        # 历史条目里出现过 precondition: "{}" 这种被转义成字符串的写法，按无前置条件处理。
        precondition = {}
    if not isinstance(precondition, dict):
        return 0
    hits = 0
    # 值可能是标量：字符串直接参与 `in` 判断会退化成子串匹配，必须收敛成列表。
    tools = [str(item) for item in lib.as_list(precondition.get("tools"))]
    if tools:
        if tool and tool not in tools:
            raise ValueError("tool_mismatch")
        if tool and tool in tools:
            hits += 1
        elif query and any(item.lower() in query for item in tools):
            hits += 1
    errors = [str(item) for item in lib.as_list(precondition.get("errors"))]
    if errors:
        if error and error not in errors:
            raise ValueError("error_mismatch")
        if error and error in errors:
            hits += 1
        elif query and any(item.lower() in query for item in errors):
            hits += 1
    scene = precondition.get("scene")
    if scene and query and str(scene).lower() in query:
        hits += 1
    return hits


def _asset_hits(query: str, limit: int = 5) -> list[dict]:
    """按路径子串匹配可复用资产。

    资产没有正文可检索，能匹配的只有路径；它的用途写在转成的条目里，这里只保证
    「下次同类问题能想起来盘上有这个脚本」。
    """
    hits: list[dict] = []
    for row in lib.load_assets(refresh=True):
        if row.get("status") not in ("active", "stale"):
            continue
        path = str(row.get("path") or "")
        if query and query not in path.lower():
            continue
        hits.append(row)
    hits.sort(key=lambda row: (int(row.get("uses") or 0), str(row.get("last_seen") or "")), reverse=True)
    return hits[:limit]


def cmd_search(args) -> int:
    _, project_name = current_project()
    query = (args.query or "").lower()
    tool = args.tool
    error = args.error
    focus_path = getattr(args, "path", "") or ""
    terms = [term.strip().lower() for term in query.split(",") if term.strip()]
    scored = []
    possible = []
    filtered_out = 0
    for path, meta, body in lib.iter_entries(scope=args.scope, project=project_name):
        if args.category and meta.get("category") != args.category:
            continue
        if args.type and meta.get("type") != args.type:
            continue
        if args.project and meta.get("project") != args.project:
            continue
        if focus_path:
            applicable, declared = _path_matches(meta, focus_path)
            if not applicable:
                # 声明过 files 却不适用的：排除。
                if declared:
                    continue
                # 没声明的：列成「可能相关」，不丢——存量条目大多没声明。
                possible.append((meta, path))
                continue
        haystack = " ".join(
            [
                str(meta.get("title") or ""),
                " ".join(str(tag) for tag in lib.as_list(meta.get("tags"))),
                str(meta.get("action") or ""),
                # 前置条件必须进检索面：条目的 precondition.errors 存的就是报错原文，
                # 拿现场报错来搜却搜不到，等于把最可靠的那条检索通路堵死。
                _precondition_text(meta),
                body,
            ]
        ).lower()
        if terms:
            # 默认全词命中（精确召回）；--any 改成任一命中，--all 保留全词语义。
            # 加 --any 是为了让模型能用本地话题词召回（「单测」这种词条目里未必逐字出现），
            # 又不必背整句原文。
            if getattr(args, "any", False):
                if not any(term in haystack for term in terms):
                    continue
            elif not all(term in haystack for term in terms):
                continue
        try:
            hits = precondition_hits(meta, query, tool, error)
        except ValueError:
            # 被 --tool / --error 硬过滤掉的条数要计数：全被它滤掉时必须说清原因，
            # 否则「没有匹配」会被误读成「经验库没这条经验」。
            filtered_out += 1
            continue
        confidence = {"high": 3, "medium": 2, "low": 1}.get(str(meta.get("confidence") or "low"), 1)
        fresh = 1 if lib.days_since(str(meta.get("verified_at") or "")) <= lib.STALE_DAYS else 0
        evidence = meta.get("evidence") if isinstance(meta.get("evidence"), dict) else {}
        supports = len(lib.as_list(evidence.get("sessions")))
        counters = len(lib.as_list(evidence.get("counters")))
        score = 3 * hits + 2 * confidence + fresh + min(supports, 3) - 2 * min(counters, 2)
        scored.append((score, meta, path))
    scored.sort(key=lambda item: item[0], reverse=True)
    picked = scored[: args.limit]
    assets = _asset_hits(query)
    # 召回计数只在真实召回时累加：带检索条件才算，空 query 的清单用法与
    # 「可能相关」段都不算——盘点不是召回，猜测命中更不是。
    count_stamp = ""
    hit: dict[str, int] = {}
    counted = False
    # 只有「输入了检索条件」才算召回：--query / --path / --tool / --error 任一即可。
    if picked and (terms or focus_path or tool or error) and not getattr(args, "no_count", False):
        counted = True
        count_stamp = lib.now_iso()
        hit, failures = lib.bump_hit([path for _, _, path in picked], stamp=count_stamp)
        if failures:
            # 计数失败绝不影响召回结果：打印一行提示并留诊断，结果照常输出。
            print(f"[!] 召回计数未写入：{'; '.join(failures)}", file=sys.stderr)
            lib.log_diagnostic("recall", "count_failed", detail="; ".join(failures)[:400])
        # 登记表的 hit_count 列跟着召回动，否则用户看到的表与库对不上。
        write_library_table()
    if args.json:
        print(
            json.dumps(
                {
                    "entries": [
                        {
                            "score": score,
                            "id": meta.get("id"),
                            "title": meta.get("title"),
                            "category": meta.get("category"),
                            "type": meta.get("type"),
                            "confidence": meta.get("confidence"),
                            "verification": meta.get("verification"),
                            "action": meta.get("action"),
                            "hit_count": hit.get(str(path), lib.hit_count(meta)),
                            "last_hit_at": count_stamp if str(path) in hit else meta.get("last_hit_at"),
                            "use_count": lib.use_count(meta),
                            "last_used_at": meta.get("last_used_at"),
                            "path": str(path),
                        }
                        for score, meta, path in picked
                    ],
                    "possible": [
                        {
                            "id": meta.get("id"),
                            "title": meta.get("title"),
                            "action": meta.get("action"),
                            "path": str(path),
                        }
                        for meta, path in possible[: args.limit]
                    ],
                    "assets": [
                        {
                            "asset_id": row.get("asset_id"),
                            "path": row.get("path"),
                            "uses": row.get("uses"),
                            "last_seen": row.get("last_seen"),
                            "status": row.get("status"),
                        }
                        for row in assets
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not picked and not possible and not assets:
        if filtered_out:
            # 最常见的原因是把 --tool 当成「我这次用的工具」填了，而条目的 precondition 记的是别的工具。
            print(f"{filtered_out} 条经验被 --tool / --error 硬过滤排除（条目前置条件不含该值）。")
            print("先去掉 --tool / --error 用现场字样重搜，命中后再按需收窄。")
        elif terms:
            print("没有匹配的经验条目。")
            print("换现场字样再搜：报错原文、工具名、接口名、字段名。泛化描述（如「编译失败怎么办」）搜不到。")
            print("多个候选词用逗号分隔并加 --any，例如 --query \"单测,unittest,make test\" --any。")
        elif focus_path:
            print(f"没有条目声明适用于 {focus_path}。")
            print("去掉 --path 用关键词重搜；也可以给相关条目补 --pre-files（见经验预筛表设计的「按需召回」）。")
        else:
            print(f"当前项目 {project_name} 还没有经验条目。沉淀入口：review 复盘候选，或 add --manual 当场入库。")
        return 0
    if not terms and not focus_path and picked:
        # 空 query 是「本项目有什么」的清单用法：不确定关键词时才用，正常召回取现场字样。
        # 带了 --path 就按需收窄，不套清单标题。
        scopes = sorted({str(meta.get("scope")) for _, meta, _ in picked})
        print(f"[{project_name}] 经验清单 {len(picked)} 条（{'、'.join(scopes)}）：")
    if focus_path and picked:
        print(f"适用于 {focus_path} 的条目 {len(picked)} 条：")
    elif focus_path and possible:
        # 一条都没声明：先讲清楚，否则用户会以为 --path 没生效。
        print(f"没有条目声明适用于 {focus_path}。以下是可能相关的：")
    for score, meta, path in picked:
        hits = hit.get(str(path), lib.hit_count(meta))
        used = lib.use_count(meta)
        print(f"[{score}] {meta.get('id')}  {meta.get('title')}  hit={hits} use={used}")
        print(f"    {meta.get('category')}/{meta.get('type')}  confidence={meta.get('confidence')} verification={meta.get('verification')}")
        if meta.get("action"):
            print(f"    结论：{meta['action']}")
        if meta.get("kind") == "reference":
            print(f"    出处：{meta.get('source_of_truth')}")
    if possible:
        # 没声明 files 的存量条目：列出来但标明不确定，别让它们永远召不回。
        print(f"\n可能相关 {len(possible)} 条（未声明适用文件，需自行判断）：")
        for meta, _ in possible[: args.limit]:
            print(f"    {meta.get('id')}  {meta.get('title')}")
            if meta.get("action"):
                print(f"        结论：{meta['action']}")
        print("\n给这些条目补 --pre-files 后，它们会进入精确匹配段。")
    if assets:
        print(f"可复用资产（{len(assets)} 个，未被条目引用，可直接复用）:")
        for row in assets:
            print(
                f"    {row.get('asset_id')}  {row.get('path')}\n"
                f"        最近产出 {row.get('last_seen')}  uses={row.get('uses')}"
            )
    if not terms and not focus_path:
        print("\n逐条用经验条目正文里的结论执行；要按关键词精确召回时带上 --query 现场字样。")
    if counted:
        # 召回是机械信号，采纳要 agent 自己报：不提醒就没人跑，use_count 会一直是 0。
        print("\n按条目执行完跑 `use <id> --result ok|failed` 记采纳；hit 只证明检索命中，不证明执行过。")
    return 0


# --------------------------------------------------------------------------
# 采纳上报：search 只会记召回，执行与否只有 agent 自己知道
# --------------------------------------------------------------------------


def _find_entry(entry_id: str) -> tuple[Path | None, dict | None]:
    """按 id 找条目：先全局 + 本项目，再扫其余项目。

    登记表是跨项目口径，用户会直接拿表里的 id 调 use / remove；
    只看本项目会让别处项目的条目「存在但找不到」。
    """
    _, project_name = current_project()
    for path, meta, _ in lib.iter_entries(project=project_name):
        if str(meta.get("id")) == entry_id:
            return path, meta
    for name in lib.entry_projects():
        if name == project_name:
            continue
        for path, meta, _ in lib.iter_entries(scope="project", project=name):
            if str(meta.get("id")) == entry_id:
                return path, meta
    return None, None


def cmd_use(args) -> int:
    path, meta = _find_entry(args.id)
    if meta is None:
        raise SystemExit(f"未找到条目：{args.id}（先用 search 确认 id）")
    note = lib.redact_scalar(getattr(args, "note", "") or "")
    stamp = lib.now_iso()
    counts, failures = lib.bump_use([path], result=args.result, note=note, stamp=stamp)
    if failures:
        # 采纳计数写不进去要报出来：这条自报信号本来就是靠 agent 主动跑命令攒的。
        print(f"[!] 采纳计数未写入：{'; '.join(failures)}", file=sys.stderr)
        lib.log_diagnostic("use", "count_failed", path=str(path), detail="; ".join(failures)[:400])
        return 1
    total = counts.get(str(path), lib.use_count(meta))
    print(f"已记采纳 {args.id} 第 {total} 次（result={args.result}，hit_count 不动）")
    if args.result == "failed":
        print("失败也留档：下次检索能看到 use_failed_count，别把这条当已验证结论。")
    write_library_table()
    return 0


def cmd_remove(args) -> int:
    """删除条目：删除是不可逆动作，默认预演，--apply 才落盘并同步登记表。"""
    path, meta = _find_entry(args.id)
    if meta is None:
        raise SystemExit(f"未找到条目：{args.id}（先用 library 或 search 确认 id）")
    if not args.apply:
        print(f"待删除：{args.id}  {_table_cell(meta.get('title'))}")
        print(f"文件：{path}")
        print("\n这是预演。确认后加 --apply 删除条目并同步登记表。")
        return 0
    try:
        path.unlink()
    except OSError as error:
        raise SystemExit(f"删除失败：{error}")
    write_library_table()
    print(f"已删除条目 {args.id}，登记表已同步")
    return 0


# --------------------------------------------------------------------------
# 经验库登记表：入库、删除、计数变更都同步同一张表
# --------------------------------------------------------------------------

LIBRARY_DESCRIPTION_MAX = 80
LIBRARY_COLUMNS = "| 经验条目名 | 描述 | 类别/类型 | 置信度 | hit_count | use_count | 所属项目 |"
LIBRARY_DIVIDER = "| --- | --- | --- | --- | --- | --- | --- |"


def _table_cell(text, limit: int = 0) -> str:
    """单元格文本：竖线会把 markdown 表撑坏，换行一律折成空格。"""
    flat = " ".join(str(text or "").split()).replace("|", "\\|")
    if limit and len(flat) > limit:
        flat = flat[:limit] + "…"
    return flat


def _entry_description(meta: dict, body: str) -> str:
    """描述取条目结论；没写结论时退回正文第一句，保证登记表不留空单元格。"""
    action = str(meta.get("action") or "")
    if not action.strip():
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                action = stripped
                break
    return _table_cell(action, LIBRARY_DESCRIPTION_MAX) or "（无描述）"


def _entry_project_label(meta: dict) -> str:
    if str(meta.get("scope") or "project") == "global":
        return "全局"
    return _table_cell(meta.get("project")) or "（未记录项目）"


def _library_entries() -> list[tuple[Path, dict, str]]:
    """登记表是跨项目口径：全局条目 + 所有项目条目，与 stats 一致。"""
    entries = list(lib.iter_entries(scope="global"))
    for name in lib.entry_projects():
        entries.extend(lib.iter_entries(scope="project", project=name))
    return entries


def library_table() -> str:
    entries = _library_entries()
    global_count = sum(1 for _, meta, _ in entries if str(meta.get("scope") or "project") == "global")
    rows = [
        (_entry_project_label(meta), str(meta.get("id") or ""), meta, body) for _, meta, body in entries
    ]
    # 全局排前面，项目内按项目名与 id 排：一眼能看出这条属于哪个仓库。
    rows.sort(key=lambda row: (row[0] != "全局", row[0], row[1]))
    lines = [
        "# 经验库登记表",
        "",
        f"更新时间：{lib.now_iso()}",
        f"条目数：{len(entries)}（全局 {global_count} / 项目 {len(entries) - global_count}）",
        "",
        LIBRARY_COLUMNS,
        LIBRARY_DIVIDER,
    ]
    for project_label, entry_id, meta, body in rows:
        lines.append(
            f"| {_table_cell(meta.get('title'))}（`{entry_id}`） | {_entry_description(meta, body)} "
            f"| {meta.get('category')}/{meta.get('type')} | {meta.get('confidence')} "
            f"| {lib.hit_count(meta)} | {lib.use_count(meta)} | {project_label} |"
        )
    if not rows:
        lines.append("| （暂无条目） | — | — | — | 0 | 0 | — |")
    return "\n".join(lines) + "\n"


def write_library_table(text: str | None = None) -> Path:
    """重写登记表。写失败只提示一行：条目已经落盘，登记表不能反过来挡住它。"""
    target = lib.index_path()
    try:
        lib.atomic_write_text(target, library_table() if text is None else text)
    except OSError as error:
        print(f"[!] 登记表未更新：{error}", file=sys.stderr)
        lib.log_diagnostic("library", "write_failed", path=str(target), detail=str(error)[:200])
    return target


def cmd_library(args) -> int:
    """展示登记表：用户问「有哪些经验」「召回/复用情况」时跑这个。"""
    text = library_table()
    print(text, end="")
    if not getattr(args, "no_refresh", False):
        write_library_table(text)
    return 0


def cmd_index(args) -> int:
    target = write_library_table()
    print(f"已重建经验库登记表 {target}")
    return 0


def cmd_stats(args) -> int:
    _, project_name = current_project()
    entries = list(lib.iter_entries(scope="global"))
    for name in lib.entry_projects():
        entries.extend(lib.iter_entries(scope="project", project=name))
    by_category: dict[str, int] = {}
    by_confidence: dict[str, int] = {}
    by_status: dict[str, int] = {}
    sessions: set[str] = set()
    counters = 0
    stale = 0
    tool_calls = 0
    tokens = 0
    hit_total = 0
    hit_entries = 0
    hit_rows: list[tuple[int, str, str]] = []
    use_total = 0
    adopted_entries = 0
    use_failed_total = 0
    use_rows: list[tuple[int, str, str]] = []
    for _, meta, _ in entries:
        by_category[str(meta.get("category"))] = by_category.get(str(meta.get("category")), 0) + 1
        by_confidence[str(meta.get("confidence"))] = by_confidence.get(str(meta.get("confidence")), 0) + 1
        by_status[str(meta.get("status"))] = by_status.get(str(meta.get("status")), 0) + 1
        hits = lib.hit_count(meta)
        hit_total += hits
        if hits > 0:
            hit_entries += 1
            hit_rows.append((hits, str(meta.get("id")), str(meta.get("title"))))
        used = lib.use_count(meta)
        use_total += used
        use_failed_total += lib.use_failed_count(meta)
        if used > 0:
            adopted_entries += 1
            use_rows.append((used, str(meta.get("id")), str(meta.get("title"))))
        evidence = meta.get("evidence") if isinstance(meta.get("evidence"), dict) else {}
        sessions.update(str(item) for item in lib.as_list(evidence.get("sessions")))
        counters += len(lib.as_list(evidence.get("counters")))
        cost = meta.get("cost") if isinstance(meta.get("cost"), dict) else {}
        if isinstance(cost, dict):
            tool_calls += int(cost.get("avg_tool_calls") or 0)
            tokens += int(cost.get("avg_tokens") or 0)
        if lib.days_since(str(meta.get("verified_at") or "")) > lib.STALE_DAYS:
            stale += 1
    pending = list(iter_candidates(project_name))
    backlog: dict[str, int] = {}
    for owner, _, _, _, _ in iter_all_candidates():
        backlog[owner] = backlog.get(owner, 0) + 1
    other_backlog = {name: count for name, count in backlog.items() if name != project_name}
    rates = signal_conversion_rates()
    report = {
        "entries": len(entries),
        "by_category": by_category,
        "by_confidence": by_confidence,
        "by_status": by_status,
        "support_sessions": len(sessions),
        "counter_sessions": counters,
        "pending_candidates": len(pending),
        "pending_candidates_all": sum(backlog.values()),
        "projects_with_backlog": other_backlog,
        "stale_entries": stale,
        "avg_tool_calls": round(tool_calls / len(entries), 2) if entries else 0,
        "avg_tokens": round(tokens / len(entries), 1) if entries else 0,
        "recall_total": hit_total,
        "recalled_entries": hit_entries,
        "unused_entries": len(entries) - hit_entries,
        "recall_top": [
            {"id": entry_id, "title": title, "hit_count": hits}
            for hits, entry_id, title in sorted(hit_rows, reverse=True)[:5]
        ],
        "adopt_total": use_total,
        "adopted_entries": adopted_entries,
        "adopt_failed_total": use_failed_total,
        "adopt_rate": round(use_total / hit_total, 2) if hit_total else 0.0,
        "adopt_top": [
            {"id": entry_id, "title": title, "use_count": used}
            for used, entry_id, title in sorted(use_rows, reverse=True)[:5]
        ],
        "signal_conversion": rates,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"条目总数：{report['entries']}")
    print(f"按类别：{by_category or '（空）'}")
    print(f"按置信度：{by_confidence or '（空）'}")
    print(f"按状态：{by_status or '（空）'}")
    print(f"支持会话 {report['support_sessions']} 个，反例会话 {counters} 个")
    print(f"待复盘候选：当前项目 {report['pending_candidates']} 条，全部项目 {report['pending_candidates_all']} 条")
    if other_backlog:
        detail = "，".join(f"{name} {count} 条" for name, count in sorted(other_backlog.items()))
        print(f"其他项目积压：{detail}")
    print(f"超过 {lib.STALE_DAYS} 天未复核的条目：{stale} 条")
    print(f"条目平均工具调用 {report['avg_tool_calls']} 次，平均 token {report['avg_tokens']}")
    print(
        f"召回（search 命中）：累计 {report['recall_total']} 次，被召回 {report['recalled_entries']} 条，"
        f"零召回 {report['unused_entries']} 条"
    )
    for row in report["recall_top"]:
        if row["hit_count"] > 0:
            print(f"  {row['hit_count']:>4} 次  {row['id']}  {row['title']}")
    print(
        f"采纳（use 自报）：累计 {report['adopt_total']} 次，被采纳 {report['adopted_entries']} 条，"
        f"记失败 {report['adopt_failed_total']} 次，采纳率 {int(report['adopt_rate'] * 100)}%"
    )
    for row in report["adopt_top"]:
        if row["use_count"] > 0:
            print(f"  {row['use_count']:>4} 次  {row['id']}  {row['title']}")
    if rates:
        print(f"\n信号转正率（≥{CONVERSION_MIN_CASES} 次判决才计入；低于 {int(CONVERSION_FLOOR * 100)}% 进复核队列）：")
        for name, item in sorted(rates.items(), key=lambda kv: (kv[1]["rate"], kv[0])):
            flag = "  ← 复核" if item["rate"] < CONVERSION_FLOOR else ""
            print(f"  {name:<28} {item['add']}/{item['total']} = {int(item['rate'] * 100)}%{flag}")
    return 0


# 转正率：某信号被判为条目的次数 / 该信号的全部判决次数。
# 低于这个比例说明这条信号主要在产噪声，该进复核队列而不是继续加词表。
CONVERSION_FLOOR = 0.2
CONVERSION_MIN_CASES = 3


def signal_conversion_rates() -> dict[str, dict]:
    """从判决台账算每个信号的转正率；同一轮多条判决只算最后一次。"""
    latest: dict[str, dict] = {}
    for record in lib.iter_verdicts():
        turn = str(record.get("turn") or "")
        latest[turn or str(record.get("candidate_id") or "")] = record
    stats: dict[str, dict] = {}
    for record in latest.values():
        for signal in lib.as_list(record.get("signal")):
            item = stats.setdefault(str(signal), {"add": 0, "drop": 0, "total": 0, "rate": 0.0})
            item["total"] += 1
            if str(record.get("decision")) == "add":
                item["add"] += 1
            else:
                item["drop"] += 1
    for item in stats.values():
        item["rate"] = item["add"] / item["total"] if item["total"] else 0.0
    return {name: item for name, item in stats.items() if item["total"] >= CONVERSION_MIN_CASES}


def cmd_gc(args) -> int:
    _, project_name = current_project()
    horizon = _parse_days(args.older_than) if args.older_than else lib.CANDIDATE_MAX_AGE_DAYS
    stale_candidates = []
    promote_first = []
    reusable_assets = {str(row.get("path")): row for row in lib.load_assets() if row.get("reuse") == "reusable"}
    for owner, cid, record, path, line in iter_all_candidates():
        # 域差异化 TTL：底层与性能类经验的复现周期以月计，用应用层口径会清在复现之前。
        limit = horizon if args.older_than else lib.candidate_ttl(record)
        if lib.days_since(str(record.get("ts") or "")) <= limit:
            continue
        if "asset_script" in (record.get("signal") or []) and reusable_assets:
            # 候选是路径指针：先转条目或明确丢弃，别让超期淘汰把「盘上有可用脚本」这件事一起清掉。
            promote_first.append((owner, cid, path, str(record.get("turn_id") or "")))
            continue
        stale_candidates.append((owner, cid, path, str(record.get("turn_id") or "")))
    live_projects = set(lib.candidate_projects())
    stale_pending = []
    for record in lib.read_pending():
        marker = Path(str(record.get("marker_path") or ""))
        project_name_of_marker = str(record.get("project_name") or "")
        if project_name_of_marker not in live_projects or lib.days_since(str(record.get("ts") or "")) > horizon:
            stale_pending.append(marker)
    stale_diagnostics = [
        path
        for path in lib.diagnostics_dir().glob("*.jsonl")
        if lib.days_since(str(path.stem)) > lib.DIAGNOSTIC_KEEP_DAYS
    ]
    stale_entries = [
        (path, meta)
        for path, meta, _ in [
            *list(lib.iter_entries(scope="global")),
            *[item for name in lib.entry_projects() for item in lib.iter_entries(scope="project", project=name)],
        ]
        if lib.days_since(str(meta.get("verified_at") or "")) > lib.STALE_DAYS
    ]
    print(f"候选超过存活上限：{len(stale_candidates)} 条（超过 {horizon} 天；底层与性能类按 {lib.DEEP_LAYER_MAX_AGE_DAYS} 天）")
    if promote_first:
        print(f"其中 {len(promote_first)} 条指向可复用资产，先转正或明确丢弃，本次不淘汰：")
        for _, cid, _, _ in promote_first[:10]:
            print(f"  - {cid}")
        if args.force:
            print("  --force 已指定：连同这些候选一起淘汰。")
            stale_candidates.extend(promote_first)
    print(f"失效的会话结束标记：{len(stale_pending)} 个")
    print(f"超过 {lib.DIAGNOSTIC_KEEP_DAYS} 天的采集诊断：{len(stale_diagnostics)} 份")
    print(f"条目超过 {lib.STALE_DAYS} 天未复核：{len(stale_entries)} 条")
    if not args.apply:
        print("\n这是预演。确认后加 --apply 执行清理与状态转移。")
        return 0
    # 按文件分组逐个加锁：锁内重读并按 turn_id 重新计算该删哪些行，
    # 遍历时算出的行号在并发追加下会指错行。
    doomed: dict[Path, set[str]] = {}
    for _, _, path, turn_id in stale_candidates:
        doomed.setdefault(path, set()).add(turn_id)
    for path, turns in sorted(doomed.items(), key=lambda item: str(item[0])):
        try:
            with lib.FileLock(path):
                if path.exists():
                    rows = lib.load_candidate_rows(path)
                    lib._rewrite_candidate_file(path, turns, rows)
        except lib.LockTimeout as error:
            # 其他端正在写同一个候选文件：跳过它，下次 gc 再清，不阻塞整轮清理。
            print(f"[!] {path.name} 正被其他进程写入，本次跳过：{error}")
    for marker in stale_pending:
        try:
            marker.unlink()
        except OSError:
            pass
    for report in stale_diagnostics:
        try:
            report.unlink()
        except OSError:
            pass
    for path, meta in stale_entries:
        meta["status"] = "outdated"
        _, body = lib.load_entry(path)
        lib.atomic_write_text(path, lib.dump_entry(meta, body))
    print(f"已清理过期候选与会话结束标记，并把超期条目标记为 outdated（当前项目 {project_name}）。")
    return 0


def cmd_sweep(args) -> int:
    """跨会话巡检：把散在候选里的历史线索重算一遍。

    只处理钩子逐轮无状态看不见的两种信号（E1 intent_repeat、E3 artifact_churn），
    判定输入是历史候选的工具族与文件路径，不解析转录、不读条目正文。
    """
    window = args.days
    families: dict[str, dict] = {}
    artifacts: dict[str, dict] = {}
    sessions_seen: dict[str, set] = {}
    for owner, cid, record, path, line in iter_all_candidates():
        age = lib.days_since(str(record.get("ts") or ""))
        if age > window:
            continue
        session = f"{record.get('session_id')}"
        touched: set[str] = set()
        for tool in record.get("tools") or []:
            args_text = prescreen._args_text(tool.get("args", ""))
            for match in prescreen.DIAGNOSTIC_FAMILY_RE.finditer(args_text):
                touched.add(match.group(0).lower())
            for asset_path in prescreen._arg_paths(tool.get("args", "")):
                if not prescreen.SCRIPT_SUFFIX_RE.search(asset_path):
                    continue
                row = artifacts.setdefault(asset_path, {"count": 0, "sessions": set(), "candidates": []})
                row["count"] += 1
                row["sessions"].add(session)
                row["candidates"].append(cid)
        for family in touched:
            row = families.setdefault(family, {"count": 0, "sessions": set(), "candidates": []})
            row["count"] += 1
            row["sessions"].add(session)
            row["candidates"].append(cid)
    repeat = {
        name: row for name, row in families.items()
        if row["count"] >= prescreen.INTENT_REPEAT_THRESHOLD and len(row["sessions"]) >= 2
    }
    churn = {
        path: row for path, row in artifacts.items()
        if row["count"] >= prescreen.ARTIFACT_CHURN_THRESHOLD and len(row["sessions"]) >= 2
    }
    written = 0
    if args.apply:
        written += _mark_sweep(repeat, "intent_repeat", window)
        written += _mark_sweep(churn, "artifact_churn", window)
    if args.json:
        print(json.dumps(
            {
                "window_days": window,
                "intent_repeat": {name: sorted(row["sessions"]) for name, row in repeat.items()},
                "artifact_churn": {name: sorted(row["sessions"]) for name, row in churn.items()},
                "marked": written,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    print(f"跨会话巡检（窗口 {window} 天）：")
    if repeat:
        print(f"重复出现的操作族 {len(repeat)} 个（≥{prescreen.INTENT_REPEAT_THRESHOLD} 次且跨 ≥2 会话）：")
        for name, row in sorted(repeat.items(), key=lambda item: -item[1]["count"]):
            print(f"  - {name}  {row['count']} 次 / {len(row['sessions'])} 个会话")
    if churn:
        print(f"反复改写的脚本 {len(churn)} 个（≥{prescreen.ARTIFACT_CHURN_THRESHOLD} 次且跨 ≥2 会话）：")
        for name, row in sorted(churn.items(), key=lambda item: -item[1]["count"]):
            print(f"  - {name}  {row['count']} 次 / {len(row['sessions'])} 个会话")
    if not repeat and not churn:
        print("没有发现跨会话重复：窗口内没有操作族或脚本被反复产出。")
    if not args.apply:
        print("\n这是预演。加 --apply 把这些线索标进对应候选（标信号、不动正文）。")
    else:
        print(f"\n已标记 {written} 条候选。")
    return 0


def _mark_sweep(hits: dict, signal: str, window: int) -> int:
    """把巡检结论标进候选记录：只追加信号与巡检字段，不改原文。"""
    marked = 0
    by_file: dict[Path, dict[str, dict]] = {}
    for owner, cid, record, path, line in iter_all_candidates():
        if lib.days_since(str(record.get("ts") or "")) > window:
            continue
        for name, row in hits.items():
            if cid not in row["candidates"]:
                continue
            by_file.setdefault(path, {})[cid] = {"signal": signal, "name": name}
    for path, wanted in by_file.items():
        try:
            with lib.FileLock(path):
                if not path.exists():
                    continue
                rows = lib.load_candidate_rows(path)
                changed = False
                for row in rows:
                    cid = lib.candidate_id(
                        str(row.get("client") or ""),
                        str(row.get("session_id") or ""),
                        str(row.get("turn_id") or ""),
                        str(row.get("ts") or "")[:10],
                    )
                    hit = wanted.get(cid)
                    if not hit:
                        continue
                    signals = list(row.get("signal") or [])
                    if hit["signal"] not in signals:
                        signals.append(hit["signal"])
                        row["signal"] = signals
                        changed = True
                    sweep = row.get("sweep") if isinstance(row.get("sweep"), dict) else {}
                    sweep.setdefault(hit["signal"], []).append(hit["name"])
                    row["sweep"] = sweep
                    marked += 1
                if changed:
                    lib.atomic_write_text(
                        path,
                        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
                    )
        except lib.LockTimeout as error:
            print(f"[!] {path.name} 正被其他进程写入，本次跳过：{error}")
    return marked


def _parse_days(text: str) -> int:
    match = re.fullmatch(r"(\d+)d?", (text or "").strip())
    if not match:
        raise SystemExit("--older-than 需要形如 90d 的天数")
    return int(match.group(1))


def cmd_backfill(args) -> int:
    """给存量条目补 precondition.files：按需召回的精度取决于它。"""
    entries = list(lib.iter_entries(scope="global"))
    for name in lib.entry_projects():
        entries.extend(lib.iter_entries(scope="project", project=name))
    declared = sum(
        1
        for _, meta, _ in entries
        if isinstance(meta.get("precondition"), dict) and lib.as_list(meta["precondition"].get("files"))
    )
    root = Path(args.root) if args.root else None
    if root is not None and not root.is_dir():
        raise SystemExit(f"--root 不是目录：{root}")
    changed = apply_derived_files(entries, apply=args.apply, root=root)
    if args.json:
        print(json.dumps(
            {
                "entries": len(entries),
                "declared": declared,
                "derived": [{"id": cid, "files": files} for cid, files in changed],
                "applied": bool(args.apply),
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    print(f"条目 {len(entries)} 条，已声明适用文件 {declared} 条。")
    if not changed:
        print("没有可推导的条目：正文里没出现足够明确的文件线索，或都已声明。")
        return 0
    print(f"可从正文推导出适用文件 {len(changed)} 条：")
    for cid, files in changed:
        print(f"  - {cid}: {', '.join(files)}")
    if args.apply:
        print(f"\n已写入 {len(changed)} 条。推导有偏差时复盘时改条目 precondition.files。")
    else:
        print("\n这是预演。确认后加 --apply 写入。")
    return 0


def cmd_assets(args) -> int:
    """可复用资产登记表：列表与重建。

    登记表是索引不是主存，本命令可从候选里的路径指针加磁盘 stat 整体重算。
    候选被 drop 或 gc 之后，登记表仍保留记录：它证明这个资产曾经产出过。
    """
    rows = lib.load_assets(refresh=not args.no_refresh)
    if not args.gone:
        rows = [row for row in rows if row.get("status") != "gone"]
    if args.stale_days:
        for row in rows:
            if row.get("status") == "active" and lib.days_since(str(row.get("last_seen") or "")) > args.stale_days:
                row["status"] = "stale"
    rows.sort(key=lambda row: (str(row.get("status") or ""), str(row.get("last_seen") or "")), reverse=True)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("资产登记表为空。产出可复用脚本的轮次会自动登记；表丢失时本命令会从候选重建。")
        return 0
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row.get("status"))] = counts.get(str(row.get("status")), 0) + 1
    summary = "，".join(f"{name} {count}" for name, count in sorted(counts.items()))
    print(f"可复用资产 {len(rows)} 个（{summary}）：")
    for row in rows:
        uses = int(row.get("uses") or 0)
        flag = " ⟵ 跨会话重复产出，建议转条目" if uses >= 2 else ""
        print(
            f"- {row.get('asset_id')}  [{row.get('status')}] uses={uses} last={row.get('last_seen')}{flag}\n"
            f"    {row.get('path')}"
        )
    return 0


def cmd_promote(args) -> int:
    target_path, target_meta = _find_entry(args.id)
    if target_meta is None:
        raise SystemExit(f"未找到条目：{args.id}")

    evidence = target_meta.get("evidence") if isinstance(target_meta.get("evidence"), dict) else {}
    supports = lib.as_list(evidence.get("sessions"))
    counters = lib.as_list(evidence.get("counters"))
    if counters:
        raise SystemExit("该条目存在反例会话，先补齐核实再升级")
    if len(supports) < 3:
        raise SystemExit(f"支持会话只有 {len(supports)} 个，未达到 3 个独立会话的升级门槛")

    scope = str(target_meta.get("scope") or "global")
    if scope == "global":
        rule_file = Path.home() / ".agents" / "AGENTS.md"
    else:
        rule_file = lib.find_project_root(Path.cwd()) / "AGENTS.md"
        if not rule_file.exists():
            rule_file = lib.find_project_root(Path.cwd()) / ".agents" / "rules.md"
    rule_text = f"- {target_meta.get('action') or target_meta.get('title')}（来源：经验库 {args.id}）"
    # 规则要写进 AGENTS.md 这类全局文件：入口处再脱敏一次，写入盘外文件的内容不进凭据。
    rule_text = lib.redact_scalar(rule_text)

    print(f"规则追加位置：{rule_file}")
    print(f"规则小节：{RULE_SECTION_TITLE}")
    print("待追加内容：")
    print(rule_text)
    if not args.apply:
        print("\n这是预演。经用户确认后加 --apply 执行追加，并把条目转为引用。")
        return 0

    existing = lib.read_text(rule_file) if rule_file.exists() else ""
    if RULE_SECTION_TITLE not in existing:
        existing = existing.rstrip("\n") + f"\n\n{RULE_SECTION_TITLE}\n\n"
    updated = existing.rstrip("\n") + "\n" + rule_text + "\n"
    lib.atomic_write_text(rule_file, updated)

    target_meta["kind"] = "reference"
    target_meta["source_of_truth"] = f"{rule_file.name}#经验沉淀出的规则"
    _, body = lib.load_entry(target_path)
    lib.atomic_write_text(target_path, lib.dump_entry(target_meta, body))
    print(f"已写入规则并更新条目 {args.id} 为引用型。")
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="经验库管理 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="列出待复盘候选（默认当前项目，--all 全部项目）")
    review.add_argument("--json", action="store_true")
    review.add_argument("--all", action="store_true", help="列出所有项目的积压候选")
    review.add_argument("--end", action="store_true", help="复盘收尾：追加当天记录并释放闸门锁")
    review.add_argument("--summary", default="", help="收尾统计，一行，如 入库 2 条、丢弃 1 条、保留 0 条")
    review.set_defaults(func=cmd_review)

    gate = sub.add_parser("gate", help="开场闸门：有待复盘候选且无人在复盘时打印派发指令")
    gate.set_defaults(func=cmd_gate)

    prompt = sub.add_parser("prompt", help="开场提示行；无积压时不输出任何内容")
    prompt.set_defaults(func=cmd_prompt)

    doctor = sub.add_parser("doctor", help="采集体检：最近诊断与现状")
    doctor.add_argument("--days", type=int, default=7)
    doctor.add_argument("--limit", type=int, default=10, help="明细最多打印几条")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    validate = sub.add_parser("validate", help="按 schema 校验已有候选")
    validate.add_argument("--limit", type=int, default=10, help="问题最多打印几条")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_validate)

    show = sub.add_parser("show", help="打印候选原文")
    show.add_argument("candidate_id")
    show.set_defaults(func=cmd_show)

    drop = sub.add_parser("drop", help="丢弃候选")
    drop.add_argument("candidate_id")
    drop.add_argument(
        "--reason",
        default="unspecified",
        help="丢弃原因，落进判决台账供 stats 算信号转正率（如 one_off / carrier / noise / dup）",
    )
    drop.set_defaults(func=cmd_drop)

    note = sub.add_parser("note", help="把 agent 自己的试错结论记成备选，等复盘再决定是否入库")
    note.add_argument("--title", required=True, help="一句话说清这条经验讲什么")
    note.add_argument("--action", required=True, help="B 的做法与适用边界，即可执行结论")
    note.add_argument("--root-cause", default="", help="A 为何不通")
    note.add_argument("--detail", default="", help="现象补充：报错原文、命令、版本等现场字样")
    note.add_argument("--tags", default="")
    note.add_argument("--pre-tool", default="", help="A 用到的工具名，供后续 --tool 检索命中")
    note.add_argument("--pre-error", default="", help="A 的报错原文，供后续 --error 检索命中")
    note.add_argument("--pre-scene", default="")
    note.add_argument("--pre-files", default="", help="逗号分隔的文件后缀或目录")
    note.set_defaults(func=cmd_note)

    scan = sub.add_parser("scan", help="扫描项目内已有知识载体")
    scan.add_argument("--query", required=True)
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    add = sub.add_parser("add", help="写入经验条目")
    add.add_argument("--from", dest="from_id", default="")
    add.add_argument(
        "--manual",
        action="store_true",
        help="会话内当场沉淀、没有候选时使用；无转录证据，置信度强制 low",
    )
    add.add_argument("--title", required=True)
    add.add_argument("--category", required=True, choices=lib.CATEGORIES)
    add.add_argument("--type", required=True, choices=lib.TYPES)
    add.add_argument("--kind", default="original", choices=lib.KINDS)
    add.add_argument(
        "--scope",
        default="project",
        choices=lib.SCOPES,
        help="归属：默认 project（本项目专用）；与仓库无关才写 global，判据见 SKILL.md「归属：全局还是项目」",
    )
    add.add_argument("--project", default=None)
    add.add_argument("--action", default="")
    add.add_argument("--root-cause", default="")
    add.add_argument("--scope-note", default="")
    add.add_argument("--tags", default="")
    add.add_argument("--pre-tool", default="")
    add.add_argument("--pre-error", default="")
    add.add_argument("--pre-files", default="", help="逗号分隔的文件后缀或目录：按 --path 召回时用来收窄")
    add.add_argument("--pre-scene", default="")
    add.add_argument("--source-of-truth", default=None)
    add.add_argument("--verification", default="n/a", choices=lib.VERIFICATIONS)
    add.add_argument("--confidence", default="", choices=["", "high", "medium", "low"], help="显式定级；默认按证据强度自动算")
    add.add_argument("--verification-note", default="")
    add.add_argument("--body-file", default="")
    add.add_argument("--force", action="store_true")
    add.set_defaults(func=cmd_add)

    search = sub.add_parser("search", help="检索经验")
    search.add_argument("--query", default="")
    search.add_argument("--any", action="store_true", help="逗号分隔的多个词任一命中即算（默认要求全词命中）")
    search.add_argument("--path", default="", help="只保留适用于该文件或目录的条目")
    search.add_argument("--category", choices=lib.CATEGORIES)
    search.add_argument("--type", choices=lib.TYPES)
    search.add_argument("--scope", choices=lib.SCOPES)
    search.add_argument("--project")
    search.add_argument("--tool")
    search.add_argument("--error")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--json", action="store_true")
    search.add_argument(
        "--no-count",
        dest="no_count",
        action="store_true",
        help="只读检索：命中条的 hit_count 不累加（人工盘点、脚本自检用）",
    )
    search.set_defaults(func=cmd_search)

    use = sub.add_parser("use", help="记一条经验被采纳执行（自报口径，与 search 的召回计数分开）")
    use.add_argument("id", help="条目 id，先用 search 拿到")
    use.add_argument(
        "--result",
        default="ok",
        choices=["ok", "failed"],
        help="执行结果：ok 结论生效；failed 按条目做了但没解决，落 use_failed_count",
    )
    use.add_argument("--note", default="", help="一句话现场补充，落 last_use_note")
    use.set_defaults(func=cmd_use)

    index = sub.add_parser("index", help="重建经验库登记表 INDEX.md")
    index.set_defaults(func=cmd_index)

    library = sub.add_parser(
        "library",
        help="展示经验库登记表：条目名、描述、hit_count、use_count、所属项目",
    )
    library.add_argument(
        "--no-refresh",
        dest="no_refresh",
        action="store_true",
        help="只打印表格，不回写 INDEX.md（脚本自检用）",
    )
    library.set_defaults(func=cmd_library)

    remove = sub.add_parser("remove", help="删除经验条目（默认预演），并同步登记表")
    remove.add_argument("id", help="条目 id，先用 library 或 search 拿到")
    remove.add_argument("--apply", action="store_true", help="确认删除")
    remove.set_defaults(func=cmd_remove)

    stats = sub.add_parser("stats", help="输出度量")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    gc = sub.add_parser("gc", help="清理过期候选与条目")
    gc.add_argument("--older-than", default="", help=f"候选存活上限，默认 {lib.CANDIDATE_MAX_AGE_DAYS}d")
    gc.add_argument("--apply", action="store_true")
    gc.add_argument("--force", action="store_true", help="连指向可复用资产的超期候选一起淘汰")
    gc.set_defaults(func=cmd_gc)

    regate = sub.add_parser("regate", help="用当前准入闸门复判存量候选（默认预演）")
    regate.add_argument("--apply", action="store_true", help="丢弃复判后仍会被闸门拦下的候选")
    regate.add_argument("--json", action="store_true")
    regate.set_defaults(func=cmd_regate)

    promote = sub.add_parser("promote", help="把反复成立的纠正升级为规则")
    promote.add_argument("id")
    promote.add_argument("--apply", action="store_true")
    promote.set_defaults(func=cmd_promote)

    assets = sub.add_parser("assets", help="可复用资产登记表：列表与重建")
    assets.add_argument("--json", action="store_true")
    assets.add_argument("--gone", action="store_true", help="连已删除的记录一起列出")
    assets.add_argument("--no-refresh", action="store_true", help="只读登记表，不重算磁盘指纹与状态")
    assets.add_argument("--stale-days", type=int, default=lib.ASSET_STALE_DAYS, help="多久未再产出算陈旧")
    assets.set_defaults(func=cmd_assets)

    backfill = sub.add_parser("backfill", help="给存量条目补 precondition.files")
    backfill.add_argument("--apply", action="store_true")
    backfill.add_argument("--root", default="", help="项目根目录：用于确认推导出的文件是否真实存在")
    backfill.add_argument("--json", action="store_true")
    backfill.set_defaults(func=cmd_backfill)

    sweep = sub.add_parser("sweep", help="跨会话巡检：重算 intent_repeat 与 artifact_churn")
    sweep.add_argument("--days", type=int, default=7, help="回看的候选窗口天数")
    sweep.add_argument("--apply", action="store_true", help="把线索标进对应候选")
    sweep.add_argument("--json", action="store_true")
    sweep.set_defaults(func=cmd_sweep)
    return parser


def main(argv: list[str] | None = None) -> int:
    lib.force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "scope", None) == "project" and getattr(args, "command", "") == "add" and not args.project:
        args.project = current_project()[1]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
