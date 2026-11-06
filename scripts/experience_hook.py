#!/usr/bin/env python3
"""采集入口：把一轮对话落成待复盘候选。

由五端适配器通过 stdin 传入归一化 JSON 调用。本脚本只做四件事：
解析转录、判定信号、落盘候选、在第一次落候选时派生退出哨兵。
任何异常都必须吞掉并以 0 退出，绝不允许因为采集失败影响客户端的轮次结束。

事件分两类：`turn_end` 系列走转录解析落候选；`session_end` 系列（Claude Code
原生会话结束钩子）只写一份会话结束标记，供下次会话开场提示。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import experience_lib as lib  # noqa: E402
import exit_sentinel  # noqa: E402
import prescreen  # noqa: E402
import transcripts  # noqa: E402

MAX_STDIN_BYTES = 1024 * 1024
# 判据型与疑似类信号：误报率高于词表类，落候选时标 pending，复盘排最前。
PENDING_SIGNALS = (
    "root_cause_mapped",
    "counterintuitive",
    "silent_failure_suspected",
    "offload_matrix",
    "diagnostic_hazard",
    "protocol_edge",
    "state_machine_stuck",
    "grounded_verdict",
)
TURN_END_EVENTS = ("turn_end", "stop", "session.idle", "idle", "turn.idle")
SESSION_END_EVENTS = ("session_end", "sessionend", "session.shutdown", "shutdown")


def _first_text(*values) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_payload(payload: dict) -> dict:
    """把各端字段名归一化，缺项留空而不报错。"""
    client = _first_text(payload.get("client"), payload.get("client_name"))
    event = _first_text(payload.get("event"), payload.get("hook_event_name"), payload.get("type"))
    return {
        "client": client.lower(),
        "event": event.lower(),
        "session_id": _first_text(payload.get("session_id"), payload.get("sessionID"), payload.get("sessionId")),
        "turn_id": _first_text(payload.get("turn_id"), payload.get("turnId")),
        "cwd": _first_text(payload.get("cwd"), payload.get("workspace"), payload.get("project")),
        "transcript_path": _first_text(
            payload.get("transcript_path"), payload.get("transcriptPath"), payload.get("transcript")
        ),
        "compaction_count": int(payload.get("compaction_count") or 0),
    }


def build_candidate(payload: dict, turn: transcripts.Turn, signals: list[str]) -> dict:
    """组装候选记录。原文保留，但脱敏与长度上限在此处生效。

    脱敏命中时额外写一个 `redactions` 计数：复盘的人要能分辨
    「这轮本来就没写凭据」与「原文里的凭据被替换掉了」。
    """
    cwd = payload.get("cwd") or os.getcwd()
    project_root = lib.find_project_root(cwd)
    project_name = lib.ensure_project(project_root)

    correction = prescreen.detect_correction(turn.to_dict())
    record = {
        "ts": lib.now_iso(),
        "client": payload.get("client"),
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "project_name": project_name,
        "cwd": str(cwd),
        "compaction_count": turn.compaction_count or payload.get("compaction_count") or 0,
        "signal": signals,
        "user": lib.truncate(turn.user),
        "prev_user": lib.truncate(turn.prev_user, 500),
        "assistant": lib.truncate(turn.assistant),
        "tools": turn.tools[:40],
        "outcome": turn.outcome,
        "cost": {
            "tool_calls": turn.tool_calls,
            "tokens": turn.tokens,
            "duration_ms": turn.duration_ms,
        },
        "evidence": {
            "path": _contract_home(payload.get("transcript_path") or ""),
            "lines": [turn.line_start, turn.line_end],
        },
    }
    if correction:
        record["correction"] = correction
    # 结论已经写进项目载体（规则、模板、知识库、调研文档）时标出载体路径：
    # 这类候选的信息就在载体里，复盘只需确认载体写全了，不需要再提一条经验。
    carrier = prescreen.detect_carrier_write(turn.to_dict())
    if carrier:
        record["carrier"] = carrier
    # 资产类候选标出可复用性，复盘据此判断值不值得转条目。
    reuse = prescreen.classify_turn_asset(turn.to_dict())
    if reuse != "none":
        record["reuse"] = reuse
    # 判据型与疑似类信号误报率高：标 pending，复盘时排在最前逐条核实。
    if any(signal in PENDING_SIGNALS for signal in signals):
        record["confidence"] = "pending"
    count: list[int] = [0]
    cleaned = lib.redact(record, count=count)
    if count[0]:
        # 改写了几处要留在记录里：复盘时看得到原文被替换过，而不是静默丢内容。
        cleaned["redactions"] = count[0]
    return cleaned


def _contract_home(path: str) -> str:
    """把 home 目录缩成 ~，便于跨机器阅读。"""
    if not path:
        return ""
    home = str(Path.home())
    return path.replace(home, "~", 1) if path.startswith(home) else path


def append_candidate(project_name: str, record: dict) -> None:
    """采到的候选与 agent 自记的备选走同一份幂等写入实现。"""
    lib.append_candidate(project_name, record)


def handle(payload: dict) -> dict:
    """处理一次采集请求，返回诊断信息（不写标准输出，仅供测试调用）。"""
    normalized = normalize_payload(payload)
    trace = {
        "client": normalized["client"],
        "session_id": normalized["session_id"],
        "turn_id": normalized["turn_id"],
    }
    if normalized["client"] not in lib.CLIENTS:
        return diagnose("normalize", "unknown_client", trace)

    # 有原生会话结束钩子的端（Claude Code）直接落标记，不走转录解析。
    if normalized["event"] in SESSION_END_EVENTS:
        return handle_session_end(normalized)

    if normalized["event"] and normalized["event"] not in TURN_END_EVENTS:
        return diagnose("normalize", f"unknown_event:{normalized['event']}", trace)

    transcript_path = normalized["transcript_path"]
    if not transcript_path and normalized["session_id"]:
        transcript_path = transcripts.resolve_session_file(normalized["client"], normalized["session_id"])
    turn = transcripts.parse_transcript(normalized["client"], transcript_path, normalized["turn_id"])
    if turn is None:
        return diagnose("parse", "no_transcript", {**trace, "path": transcript_path})

    signals = prescreen.detect_signals(turn.to_dict())
    if not signals:
        # 平平淡淡的一轮是正常情况，不记诊断，否则日志会被正常流量淹没。
        return {"skipped": "no_signal"}
    skip_reason = prescreen.candidate_skip_reason(turn.to_dict(), signals)
    if skip_reason:
        return {"skipped": skip_reason, "signals": signals}

    # 原生钩子不带 turn_id 时用转录里的轮次序号兜底，
    # 否则同一会话的所有轮次会因为空 turn_id 被当成同一条候选。
    if not normalized["turn_id"]:
        normalized["turn_id"] = str(turn.turn_index or 1)

    record = build_candidate(normalized, turn, signals)
    violations = lib.validate_candidate(record)
    project_name = str(record.get("project_name") or "")
    if violations:
        # 违反 schema 说明解析或组装的某处改了形状：记诊断，但照旧落盘，绝不丢证据。
        diagnose(
            "schema",
            violations[0],
            {**trace, "turn_id": normalized["turn_id"], "project_name": project_name},
            written=bool(project_name),
        )
    if not project_name:
        # 连归属都没有的记录写下去只会污染别处，记诊断后丢弃。
        return diagnose("schema", "missing_project_name", trace, written=False)
    try:
        append_candidate(project_name, record)
    except lib.LockTimeout as error:
        # 拿不到锁这一轮就没落盘：必须留诊断，否则经验静默丢失。
        return diagnose("lock", "lock_timeout", {**trace, "detail": str(error)[:200]}, written=False)
    register_assets(turn, record, project_name)
    start_sentinel(normalized, project_name)
    result = {"written": True, "signals": signals, "project_name": project_name}
    if violations:
        result["schema_violations"] = violations
    return result


def register_assets(turn: transcripts.Turn, record: dict, project_name: str) -> int:
    """把本轮产出的可复用资产登记进跨会话索引。

    候选会被 drop 与 gc 清掉，物理脚本留在盘上，只靠候选会造成「脚本还在、
    没人知道它存在」的断层。登记失败只记诊断：它是索引，不是主存，
    丢了可以用 `experience.py assets` 从候选重建。
    """
    try:
        paths = prescreen.reusable_asset_paths(turn.to_dict())
        if not paths:
            return 0
        cid = lib.candidate_id(
            str(record.get("client") or ""),
            str(record.get("session_id") or ""),
            str(record.get("turn_id") or ""),
            str(record.get("ts") or "")[:10],
        )
        return lib.upsert_assets(paths, record, cid)
    except Exception as error:  # noqa: BLE001
        lib.log_diagnostic(
            "asset",
            f"register_failed:{type(error).__name__}",
            client=str(record.get("client") or ""),
            session_id=str(record.get("session_id") or ""),
            project_name=project_name,
            detail=str(error)[:200],
        )
        return 0


def diagnose(stage: str, reason: str, fields: dict, written: bool = False) -> dict:
    """记一条诊断并返回给调用方，让静默失效在 doctor 里可见。"""
    lib.log_diagnostic(stage, reason, **fields)
    return {"skipped": reason, "stage": stage, "written": written}


def handle_session_end(normalized: dict) -> dict:
    """原生会话结束钩子：写一份会话结束标记，供下次开场提示。"""
    cwd = normalized.get("cwd") or os.getcwd()
    project_name = lib.ensure_project(lib.find_project_root(cwd))
    exit_sentinel.write_session_end(
        normalized["client"],
        normalized["session_id"],
        project_name,
        str(cwd),
        "native_hook",
        exit_sentinel.count_candidates(project_name),
    )
    return {"session_end": True, "project_name": project_name}


def start_sentinel(normalized: dict, project_name: str) -> int | None:
    """第一次落候选时派生退出哨兵；同一会话只派一个。失败一律静默。"""
    try:
        return exit_sentinel.spawn(
            normalized["client"],
            normalized["session_id"],
            project_name,
            str(normalized.get("cwd") or os.getcwd()),
            exit_sentinel.count_candidates(project_name),
        )
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="经验采集入口")
    parser.add_argument(
        "--client",
        default="",
        choices=["", *lib.CLIENTS],
        help="客户端标识。原生钩子 payload 里没有 client 字段，用本参数补齐。",
    )
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
        if len(raw) > MAX_STDIN_BYTES:
            return 0
        # Windows PowerShell 的 Set-Content/重定向默认写 BOM：留着会让 json 解析在第一个字符就失败，
        # 采集静默失效，因此解码后统一去掉。
        payload = json.loads(raw.decode("utf-8", errors="replace").lstrip("\ufeff"))
    except Exception as error:  # noqa: BLE001
        lib.log_diagnostic("stdin", f"unreadable:{type(error).__name__}")
        return 0
    if not isinstance(payload, dict):
        lib.log_diagnostic("stdin", "not_an_object")
        return 0
    if args.client and not payload.get("client"):
        payload["client"] = args.client
    try:
        handle(payload)
    except Exception as error:  # noqa: BLE001
        # 采集失败不影响客户端，但必须留下痕迹，否则解析器坏了没人知道。
        lib.log_diagnostic(
            "hook",
            f"exception:{type(error).__name__}",
            client=str(payload.get("client") or args.client or ""),
            session_id=str(payload.get("session_id") or ""),
            detail=str(error)[:200],
        )
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
