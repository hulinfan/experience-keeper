#!/usr/bin/env python3
"""退出哨兵：给没有会话结束事件的客户端补一个「会话已结束」标记。

有的客户端没有会话结束钩子（Codex、dsh、codemaker 只有轮次结束信号），
退出时拿不到任何回调。本脚本由采集脚本在第一次落候选时派生一个脱离父进程的
哨兵进程，哨兵盯住客户端主进程的 pid，等它退出后写一份会话结束标记：

    ~/.agents/experience/pending/<client>-<session8>.json

标记只是提示，不带模型调用、不写候选；下次任意项目开场跑 `experience.py review`
时会读它并提示「某个项目的会话刚结束，候选待复盘」。候选是否存在始终以
candidates 目录为准，标记与候选对不上时会被自动清理。

派生与等待都失败时静默退出：采集链路不允许影响客户端。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import experience_lib as lib  # noqa: E402

# 各端主进程的可执行文件名（小写、不带扩展名），用于在父进程链上认出客户端本体。
CLIENT_PROCESS_NAMES = {
    "claude": ("claude",),
    "codex": ("codex",),
    "dsh": ("dsh",),
    "codemaker": ("codemaker", "opencode"),
    "pi": ("pi",),
}

# 客户端可能以宿主运行时进程运行，认不出本名时退而求其次找宿主进程。
# Claude Code 是自带会话结束钩子的原生二进制，不走这条兜底：认错进程只会写假标记。
HOST_PROCESS_NAMES = ("node", "bun", "deno")
HOST_FALLBACK_CLIENTS = ("pi", "dsh", "codemaker")

DEFAULT_TIMEOUT_SECONDS = 24 * 60 * 60
DEFAULT_POLL_SECONDS = 2.0
WAIT_SLICE_MS = 60_000

SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


# --------------------------------------------------------------------------
# 进程表：跨平台读取 pid -> (父 pid, 进程名)
# --------------------------------------------------------------------------


def normalize_name(name: str) -> str:
    return Path(name.strip().strip('"')).name.lower().removesuffix(".exe")


def _windows_process_table() -> dict[int, tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == -1:
        return {}
    table: dict[int, tuple[int, str]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), normalize_name(entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def _posix_process_table() -> dict[int, tuple[int, str]]:
    if sys.platform.startswith("linux"):
        table: dict[int, tuple[int, str]] = {}
        for path in Path("/proc").glob("[0-9]*"):
            try:
                fields = (path / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            close = fields.rfind(")")
            if close < 0:
                continue
            name = normalize_name(fields[:close].split("(", 1)[-1])
            rest = fields[close + 1 :].split()
            if len(rest) < 2:
                continue
            table[int(path.name)] = (int(rest[1]), name)
        return table

    try:
        output = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        table[int(parts[0])] = (int(parts[1]), normalize_name(parts[2]))
    return table


def process_table() -> dict[int, tuple[int, str]]:
    try:
        return _windows_process_table() if os.name == "nt" else _posix_process_table()
    except Exception:  # noqa: BLE001
        return {}


def parent_chain(pid: int, table: dict[int, tuple[int, str]]) -> list[tuple[int, str]]:
    """返回 [自己, 父, 祖父, ...]，遇到环或缺项即停。"""
    chain: list[tuple[int, str]] = []
    seen: set[int] = set()
    current = pid
    while current and current not in seen:
        seen.add(current)
        entry = table.get(current)
        if entry is None:
            break
        parent, name = entry
        chain.append((current, name))
        current = parent
    return chain


def resolve_watch_pid(client: str, start_pid: int, table: dict[int, tuple[int, str]] | None = None) -> int | None:
    """沿父进程链找客户端主进程。

    先按客户端自己的进程名精确匹配，再退到宿主运行时进程，最后退回链上最顶端的进程。
    链上看不到客户端时返回 None：宁可没有标记，也不能把「钩子进程退出」误判成「会话结束」。
    """
    names = CLIENT_PROCESS_NAMES.get(client, ())
    chain = parent_chain(start_pid, table if table is not None else process_table())
    for pid, name in chain[1:]:
        if name in names:
            return pid
    if client in HOST_FALLBACK_CLIENTS:
        for pid, name in chain[1:]:
            if name in HOST_PROCESS_NAMES:
                return pid
    return None


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        # 拿不到句柄（权限或已退出）时退回进程表判断，避免把「打不开」当成「已退出」。
        return pid in process_table()
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def wait_for_exit(pid: int, timeout_seconds: float, poll_seconds: float = DEFAULT_POLL_SECONDS) -> bool:
    """等 pid 退出；返回 True 表示观察到退出，False 表示超时或无法观察。"""
    deadline = time.monotonic() + timeout_seconds
    if os.name == "nt":
        observed = _windows_wait(pid, deadline)
        if observed is not None:
            return observed
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(max(0.05, poll_seconds))
    return False


def _windows_wait(pid: int, deadline: float) -> bool | None:
    """Windows 上用同步句柄等待，事件驱动、不轮询；拿不到句柄时返回 None 退回轮询。"""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return None
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            slice_ms = int(min(remaining, WAIT_SLICE_MS / 1000) * 1000)
            code = kernel32.WaitForSingleObject(handle, slice_ms)
            if code == WAIT_OBJECT_0:
                return True
            if code == WAIT_TIMEOUT:
                continue
            return None
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------
# 标记与派生
# --------------------------------------------------------------------------


def sentinel_pid_path(client: str, session_id: str) -> Path:
    short = (session_id or "nosession")[:8]
    safe_client = client if client in lib.CLIENTS else "unknown"
    return lib.store_home() / "sentinel" / f"{safe_client}-{short}.pid"


def sentinel_alive(client: str, session_id: str) -> bool:
    """同一会话只留一个哨兵：pid 文件存在且进程还活着就复用。"""
    path = sentinel_pid_path(client, session_id)
    try:
        pid = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    if pid > 0 and process_alive(pid):
        return True
    try:
        path.unlink()
    except OSError:
        pass
    return False


def spawn(
    client: str,
    session_id: str,
    project_name: str,
    cwd: str,
    candidates: int,
    start_pid: int | None = None,
) -> int | None:
    """派生脱离父进程的哨兵；返回哨兵 pid，未派生时返回 None。"""
    if client not in lib.CLIENTS or not session_id:
        return None
    if sentinel_alive(client, session_id):
        return None
    watch_pid = resolve_watch_pid(client, start_pid if start_pid is not None else os.getpid())
    if watch_pid is None or watch_pid == os.getpid():
        return None

    script = str(Path(__file__).resolve())
    command = [
        sys.executable or "python",
        script,
        "--client",
        client,
        "--session-id",
        session_id,
        "--project",
        project_name,
        "--cwd",
        cwd,
        "--candidates",
        str(candidates),
        "--watch-pid",
        str(watch_pid),
    ]
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        # 哨兵要活到会话结束，绝不能占住项目目录：Windows 上进程的工作目录会被锁住，
        # 目录改名或删除都会失败。这里把它的工作目录钉在 skill 自己的 scripts 目录。
        "cwd": str(Path(__file__).resolve().parent),
    }
    if os.name == "nt":
        options["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    else:
        options["start_new_session"] = True
    try:
        process = _popen(command, options)
    except OSError:
        if os.name != "nt":
            return None
        # 宿主 Job 不允许 breakaway 时退回普通派生，代价是随 Job 一起被杀。
        options["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            process = _popen(command, options)
        except OSError:
            return None
    try:
        lib.atomic_write_text(sentinel_pid_path(client, session_id), f"{process.pid}\n")
    except OSError:
        pass
    return process.pid


def _popen(command: list[str], options: dict):
    return subprocess.Popen(command, **options)  # noqa: S603


def write_session_end(client: str, session_id: str, project_name: str, cwd: str, reason: str, candidates: int) -> Path:
    record = {
        "ts": lib.now_iso(),
        "client": client,
        "session_id": session_id,
        "project_name": project_name,
        "cwd": cwd,
        "reason": reason,
        "candidates": candidates,
    }
    return lib.write_pending(record)


def count_candidates(project_name: str) -> int:
    directory = lib.candidates_dir(project_name)
    if not directory.is_dir():
        return 0
    total = 0
    for path in directory.glob("*.jsonl"):
        try:
            total += sum(1 for line in lib.read_text(path).splitlines() if line.strip())
        except OSError:
            continue
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="等待客户端进程退出并写会话结束标记")
    parser.add_argument("--client", required=True, choices=list(lib.CLIENTS))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--project", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--candidates", type=int, default=0)
    parser.add_argument("--watch-pid", type=int, default=0, help="0 表示从自身父进程链上自动识别")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)

    watch_pid = args.watch_pid or resolve_watch_pid(args.client, os.getpid()) or 0
    pid_path = sentinel_pid_path(args.client, args.session_id)
    if not watch_pid:
        return 0
    try:
        if args.watch_pid:
            lib.atomic_write_text(pid_path, f"{os.getpid()}\n")
        exited = wait_for_exit(watch_pid, args.timeout_seconds, args.poll_seconds)
        if exited:
            project_name = args.project or lib.ensure_project(lib.find_project_root(args.cwd or None))
            write_session_end(
                args.client,
                args.session_id,
                project_name,
                args.cwd,
                "process_exit",
                args.candidates if args.candidates else count_candidates(project_name),
            )
    except Exception:  # noqa: BLE001
        return 0
    finally:
        try:
            if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
