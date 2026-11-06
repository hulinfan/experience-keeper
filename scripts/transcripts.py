#!/usr/bin/env python3
"""五端转录解析。

每个客户端把会话落到磁盘的格式都不一样，但采集只需要同一件事：
「本轮的提问、本轮的终局回复、本轮的工具调用与错误、本轮成本、压缩次数」。

解析只读磁盘上的原始记录，不读模型当前上下文，因此压缩不影响证据完整性。
每个解析函数都按端隔离，某一端格式变动只影响该端。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# 工具输出里命中这些标记时，只记一条文本线索（tool.error_text_hint），绝不据此判定成败。
# 读一份含堆栈的日志、grep 报错、单测打印 "0 failed" 都会命中标记，据此判失败会产出垃圾候选。
ERROR_MARKERS = (
    "error:",
    "error executing",
    "traceback (most recent call last)",
    "command not found",
    "no such file or directory",
    "permission denied",
    "exit code: 1",
    "failed",
    "exception",
)

# 工具状态：called 表示已发起尚未拿到结果，unknown 表示来源没有给出可判定的结构化状态。
STATUS_CALLED = "called"
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"

ERROR_CHARS = 400
HINT_CHARS = 200
# 工具输出留一份截断后的原文：判据型信号（实测数值、状态旧值、幂等差异）只能从这里取，
# 不留就等于「工具结果」这一取词面永远为空。上限按单条调用算，长输出（构建日志、全量抓包）
# 只保留开头，够复盘定位就行。
RESULT_CHARS = 1200

TOOL_CALL_TYPES = ("function_call", "custom_tool_call", "local_shell_call", "tool_call")
TOOL_OUTPUT_TYPES = ("function_call_output", "custom_tool_call_output", "local_shell_call_output")

# dsh 适配器落盘的事件词汇，与 adapters/dsh/experience-keeper.mjs 逐字对应。
# 两侧任一方改名都会让 dsh 采集静默失效，因此由单元测试锁住。
DSH_EVENT_TYPES = ("user_message", "agent_message", "tool_call", "tool_result", "turn_aborted", "compaction")

# 新版 codex 把对话与工具包进 event_msg/item_completed 的 item 里，类型名用大驼峰。
# 与旧版（payload.type 直接是 user_message / agent_message）两代并存，见 parse_codex。
CODEX_ITEM_MESSAGE_ROLES = {"UserMessage": "user", "AgentMessage": "assistant"}
CODEX_ITEM_COMMAND = "CommandExecution"
CODEX_ITEM_FILE_CHANGE = "FileChange"
CODEX_ITEM_COMPACTION = "ContextCompaction"
CODEX_ITEM_TOOL_TYPES = (CODEX_ITEM_COMMAND, CODEX_ITEM_FILE_CHANGE)


class Turn:
    """一轮对话的归一化结果。"""

    def __init__(self) -> None:
        self.user = ""
        self.prev_user = ""
        # 同一轮内的追问历史与上一轮结局：qa_reflex、fallback_switch 的判定输入。
        # 边界：只留最近若干条，累计全文会随会话长度线性膨胀。
        self.prev_users: list[str] = []
        self.prev_outcome = ""
        self.assistant = ""
        self.tools: list[dict] = []
        self.tool_calls = 0
        self.tokens = 0
        self.duration_ms = 0
        self.outcome = "success"
        self.compaction_count = 0
        self.turn_index = 0
        self.line_start = 0
        self.line_end = 0

    def to_dict(self) -> dict:
        return {
            "user": self.user,
            "prev_user": self.prev_user,
            "prev_users": self.prev_users[-5:],
            "prev_outcome": self.prev_outcome,
            "assistant": self.assistant,
            "tools": self.tools,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "compaction_count": self.compaction_count,
            "turn_index": self.turn_index,
            "line_start": self.line_start,
            "line_end": self.line_end,
        }


def _read_jsonl(path: Path) -> list[tuple[int, dict]]:
    """读取 JSONL，返回 (行号, 对象)；坏行跳过，不影响整份文件。"""
    records: list[tuple[int, dict]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    records.append((number, payload))
    except OSError:
        return []
    return records


def _flatten_text(content) -> str:
    """把多种内容结构压成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in ("text", "thinking", "content", "output"):
                    value = block.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
                else:
                    nested = block.get("content")
                    if isinstance(nested, list):
                        parts.append(_flatten_text(nested))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return _flatten_text(content.get("content") or content.get("text"))
    return ""


def _maybe_json(text):
    if isinstance(text, str):
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except ValueError:
                return text
    return text


def _has_error_marker(text: str) -> bool:
    """文本里是否出现错误标记。这是线索，不是判定。"""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ERROR_MARKERS)


def new_tool(name: str, args) -> dict:
    """建一条工具记录，初始状态为已发起。"""
    return {"name": name, "args": args, "status": STATUS_CALLED, "error": "", "result": ""}


def status_from_flag(flag, absent_means_ok: bool = False) -> str:
    """把来源的结构化状态翻译成工具状态。

    `absent_means_ok` 只对「成功路径不写该键、失败必写」且经真实语料核对的端开启；
    其余端缺字段一律记 unknown，不用输出文本补判。
    """
    if flag is True:
        return STATUS_ERROR
    if flag is False:
        return STATUS_OK
    return STATUS_OK if absent_means_ok else STATUS_UNKNOWN


def settle_tool(tool: dict, status: str, text: str = "") -> None:
    """按给定状态落库：错误原文用于召回，输出原文供判据取词。

    输出一律截断保留：`no_convergence` 比对旧态词、`idempotency_mismatch` 比对稳定字段、
    实测数值类信号取现场口径，都要读它。状态不可判定时不拿文本猜成败，只留线索。
    """
    tool["status"] = status
    if text:
        tool["result"] = text[:RESULT_CHARS]
    if status == STATUS_ERROR:
        if text:
            tool["error"] = text[:ERROR_CHARS]
    elif status == STATUS_UNKNOWN and _has_error_marker(text):
        tool["error_text_hint"] = text[:HINT_CHARS]


def _tokens_from_usage(usage) -> int:
    if not isinstance(usage, dict):
        return 0
    for key in ("total_tokens", "totalTokens", "total"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    total = 0
    for key in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "input", "output"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------


def parse_codex(path: Path, turn_id: str) -> Turn | None:
    """Codex rollout JSONL：对话与工具分两代写法。

    两代都按 turn_id 切轮（原生 Stop 钩子不带 turn_id 时，取最后一条用户消息之后）：

    | 代 | 对话 | 工具 |
    | --- | --- | --- |
    | 旧代 | `event_msg` 的 `payload.type` 直接是 `user_message` / `agent_message` | `response_item` 的 `function_call` / `custom_tool_call` |
    | 新代（codex-cli 0.156 起） | `event_msg` / `item_completed` 的 `payload.item.type` 是 `UserMessage` / `AgentMessage` | 同一条目里的 `CommandExecution` / `FileChange` |

    新代仍会往 `response_item` 重复写一份 `message` 与 `custom_tool_call`，因此：
    对话按角色优先取 event_msg 权威层（`response_item` 的 user 消息含注入的 AGENTS.md 文本），
    工具按轮二选一（同一轮里 item 条目齐备就不数 `response_item` 那遍）。
    """
    records = _read_jsonl(path)
    if not records:
        return None
    turn = Turn()
    turn.compaction_count = sum(1 for _, item in records if _codex_compacted(item))

    user_lines = _codex_user_lines(records)
    if turn_id:
        window = [
            (number, item) for number, item in records if _codex_turn_id(item) == str(turn_id)
        ]
        turn.turn_index = 0
        for position, number in enumerate(user_lines, start=1):
            if any(number == item_number for item_number, _ in window):
                turn.turn_index = position
    else:
        start_line = user_lines[-1] if user_lines else 1
        window = [(number, item) for number, item in records if number >= start_line]
        turn.turn_index = len(user_lines)
    if not window:
        return None

    item_tools = any(
        str(_codex_item(item).get("type") or "") in CODEX_ITEM_TOOL_TYPES for _, item in window
    )
    matched_lines: list[int] = []
    users: list[str] = []
    assistants: list[str] = []
    fallback_users: list[str] = []
    fallback_assistants: list[str] = []
    for number, item in window:
        role, text, authoritative = _codex_message(item)
        if role:
            if text:
                if role == "user":
                    (users if authoritative else fallback_users).append(text)
                else:
                    (assistants if authoritative else fallback_assistants).append(text)
                matched_lines.append(number)
            continue
        body = _codex_item(item)
        if str(body.get("type") or "") in CODEX_ITEM_TOOL_TYPES:
            _collect_codex_item_tool(body, turn)
            matched_lines.append(number)
            continue
        payload = item.get("payload") or {}
        if _codex_payload_type(item) == "turn_aborted":
            turn.outcome = "aborted"
            matched_lines.append(number)
        elif item.get("type") == "token_usage_record":
            turn.tokens += _tokens_from_usage(payload)
        elif item.get("type") == "response_item":
            if not item_tools:
                _collect_codex_tool(payload, turn)
            matched_lines.append(number)

    # 权威层拿不到的角色才用 response_item 兜底，避免把注入的指令文本当成提问。
    turn.user = (users or fallback_users)[-1] if (users or fallback_users) else ""
    turn.assistant = (assistants or fallback_assistants)[-1] if (assistants or fallback_assistants) else ""
    if not turn.user and not turn.assistant:
        return None
    turn.line_start = min(matched_lines) if matched_lines else window[0][0]
    turn.line_end = max(matched_lines) if matched_lines else window[-1][0]
    if turn.outcome == "success" and not turn.assistant:
        turn.outcome = "incomplete"
    return turn


def _codex_payload_type(item: dict) -> str:
    payload = item.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("type") or "")
    return ""


def _codex_item(item: dict) -> dict:
    """取 event_msg/item_completed 里的 item；不是该形状返回空字典。"""
    payload = item.get("payload")
    if not isinstance(payload, dict) or str(payload.get("type") or "") != "item_completed":
        return {}
    body = payload.get("item")
    return body if isinstance(body, dict) else {}


def _codex_turn_id(item: dict) -> str:
    """本轮标识：event_msg 写在 payload 顶层，response_item 藏在 metadata 里。"""
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("turn_id")
    if direct:
        return str(direct)
    meta = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(meta, dict) and meta.get("turn_id"):
        return str(meta["turn_id"])
    return ""


def _codex_message(item: dict) -> tuple[str, str, bool]:
    """返回 (role, text, 是否权威层)；不是对话消息则返回空串与 False。"""
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return "", "", False
    payload_type = str(payload.get("type") or "")
    if payload_type in ("user_message", "agent_message"):
        role = "user" if payload_type == "user_message" else "assistant"
        return role, str(payload.get("message") or payload.get("text") or ""), True
    body = _codex_item(item)
    role = CODEX_ITEM_MESSAGE_ROLES.get(str(body.get("type") or ""), "")
    if role:
        return role, _flatten_text(body.get("content")) or str(body.get("message") or ""), True
    if item.get("type") == "response_item" and payload_type == "message":
        role = str(payload.get("role") or "")
        if role in ("user", "assistant"):
            return role, _flatten_text(payload.get("content")) or str(payload.get("message") or ""), False
    return "", "", False


def _codex_user_lines(records: list[tuple[int, dict]]) -> list[int]:
    """用户提问所在行：优先 event_msg 权威层，整份文件都没有时退回 response_item。"""
    authoritative: list[int] = []
    fallback: list[int] = []
    for number, item in records:
        role, _, is_authoritative = _codex_message(item)
        if role != "user":
            continue
        (authoritative if is_authoritative else fallback).append(number)
    return authoritative or fallback


def _codex_compacted(item: dict) -> bool:
    """压缩事件：旧代是 payload.type=context_compacted，新代是 item 里的 ContextCompaction。"""
    if _codex_payload_type(item) == "context_compacted":
        return True
    return str(_codex_item(item).get("type") or "") == CODEX_ITEM_COMPACTION


def _collect_codex_item_tool(body: dict, turn: Turn) -> None:
    """新代的工具条目：命令带 exit_code，成败可直接判定，不再只靠文本线索。"""
    body_type = str(body.get("type") or "")
    turn.tool_calls += 1
    if body_type == CODEX_ITEM_COMMAND:
        command = body.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        tool = new_tool("exec", {"command": str(command or ""), "cwd": str(body.get("cwd") or "")})
        exit_code = body.get("exit_code")
        text = (
            _flatten_text(body.get("aggregated_output"))
            or _flatten_text(body.get("stdout"))
            or _flatten_text(body.get("stderr"))
        )
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            # 命令被中断时没有退出码：记 unknown，不拿输出文本猜成败。
            settle_tool(tool, STATUS_UNKNOWN, text)
        else:
            settle_tool(tool, STATUS_OK if exit_code == 0 else STATUS_ERROR, text)
        turn.tools.append(tool)
        return
    # FileChange：只有 changes 与 status=completed 这类过程态，没有成败字段。
    changes = body.get("changes")
    tool = new_tool(
        "file_change", {"paths": sorted(changes) if isinstance(changes, dict) else []}
    )
    settle_tool(tool, STATUS_UNKNOWN, _flatten_text(body.get("stderr")) or _flatten_text(body.get("stdout")))
    turn.tools.append(tool)


def _collect_codex_tool(payload: dict, turn: Turn) -> None:
    """旧代的工具层（response_item）；本轮没有 item 级工具条目时才走这里。"""
    payload_type = str(payload.get("type") or "")
    if payload_type in TOOL_CALL_TYPES:
        name = payload.get("name") or payload.get("tool_name") or payload.get("command") or "unknown"
        args = payload.get("arguments") or payload.get("action") or payload.get("input") or {}
        turn.tool_calls += 1
        turn.tools.append(new_tool(str(name), _maybe_json(args)))
    elif payload_type in TOOL_OUTPUT_TYPES:
        text = _flatten_text(payload.get("output") or payload.get("content"))
        # response_item 层的 function_call_output 没有状态字段：实测最近 5 份 rollout 的 23/23 条只有
        # {type, id, call_id, output, internal_chat_message_metadata_passthrough}，其中 metadata
        # 只含 turn_id 与 create_time。拿不到权威状态就记 unknown，输出文本只留线索。
        status = status_from_flag(payload.get("is_error"))
        for tool in reversed(turn.tools):
            if tool["status"] == STATUS_CALLED:
                settle_tool(tool, status, text)
                break


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------


def parse_claude(path: Path, turn_id: str) -> Turn | None:
    """Claude Code 会话 JSONL：user / assistant 记录，assistant 内含 tool_use 块。"""
    records = _read_jsonl(path)
    if not records:
        return None
    turns: list[dict] = []
    current: dict | None = None
    for number, item in records:
        item_type = item.get("type")
        if item_type == "summary":
            # 压缩摘要单独出现，计入次数即可。
            continue
        if item_type == "user":
            text = _claude_user_text(item)
            if text is None:
                if current is not None:
                    _claude_absorb_user(current, item)
                continue
            current = {"user": text, "assistant": "", "tools": [], "start": number, "end": number, "tokens": 0}
            turns.append(current)
            continue
        if item_type == "assistant" and current is not None:
            current["end"] = number
            message = item.get("message") or {}
            current["tokens"] += _tokens_from_usage(message.get("usage"))
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    current["assistant"] = str(block["text"])
                elif block.get("type") == "tool_use":
                    current["tools"].append(
                        new_tool(str(block.get("name") or "unknown"), block.get("input") or {})
                    )
        elif item_type == "user" and current is not None:
            _claude_absorb_user(current, item)

    if not turns:
        return None
    index = int(turn_id) - 1 if str(turn_id).isdigit() and int(turn_id) > 0 else len(turns) - 1
    if index >= len(turns) or index < 0:
        index = len(turns) - 1
    picked = turns[index]
    turn = Turn()
    turn.user = picked["user"]
    turn.assistant = picked["assistant"]
    turn.tools = picked["tools"]
    turn.tool_calls = len(picked["tools"])
    turn.tokens = picked["tokens"]
    turn.line_start = picked["start"]
    turn.line_end = picked["end"]
    turn.outcome = "success" if picked["assistant"] else "incomplete"
    turn.turn_index = index + 1
    turn.compaction_count = sum(1 for _, item in records if item.get("isCompactSummary") is True)
    return turn


def _claude_user_text(item: dict) -> str | None:
    """真正的用户提问返回文本；工具结果等非提问返回 None。"""
    if item.get("isCompactSummary") is True:
        return None
    message = item.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return None
        texts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        joined = "\n".join(text for text in texts if text)
        return joined or None
    return None


def _claude_absorb_user(current: dict, item: dict) -> None:
    """把工具结果并入上一轮的工具记录。"""
    message = item.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        text = _flatten_text(block.get("content"))
        # Claude Code 只在失败时写 is_error：实测 479 条 tool_result 里失败 32 条全部带该键，
        # 缺键的 166 条都是文件、搜索、任务类工具的成功载荷，因此缺键按成功。
        status = status_from_flag(block.get("is_error"), absent_means_ok=True)
        for tool in reversed(current["tools"]):
            if tool["status"] == STATUS_CALLED:
                settle_tool(tool, status, text)
                break


# --------------------------------------------------------------------------
# pi
# --------------------------------------------------------------------------


def parse_pi(path: Path, turn_id: str) -> Turn | None:
    """pi 会话 JSONL：message 条目承载对话，compaction 条目承载压缩。"""
    records = _read_jsonl(path)
    if not records:
        return None
    turns: list[dict] = []
    current: dict | None = None
    compactions = 0
    for number, item in records:
        entry_type = str(item.get("type") or "")
        if entry_type == "compaction":
            compactions += 1
            continue
        if entry_type != "message":
            continue
        message = item.get("message") or {}
        role = str(message.get("role") or "")
        if role == "user":
            current = {
                "user": _flatten_text(message.get("content")),
                "assistant": "",
                "tools": [],
                "start": number,
                "end": number,
                "tokens": 0,
            }
            turns.append(current)
        elif role == "assistant" and current is not None:
            current["end"] = number
            current["tokens"] += _tokens_from_usage(message.get("usage"))
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    current["assistant"] = str(block["text"])
                elif block.get("type") == "toolCall":
                    current["tools"].append(
                        new_tool(str(block.get("name") or "unknown"), block.get("arguments") or {})
                    )
        elif role == "toolResult" and current is not None:
            current["end"] = number
            text = _flatten_text(message.get("content"))
            # pi 的 toolResult 必带 isError（实测 175/175），直接采信。
            status = status_from_flag(message.get("isError"))
            for tool in reversed(current["tools"]):
                if tool["status"] == STATUS_CALLED:
                    settle_tool(tool, status, text)
                    break
        elif role == "bashExecution" and current is not None:
            current["end"] = number
            exit_code = message.get("exitCode")
            cancelled = message.get("cancelled") is True
            if cancelled:
                status = STATUS_ERROR
            elif isinstance(exit_code, int) and not isinstance(exit_code, bool):
                status = STATUS_ERROR if exit_code != 0 else STATUS_OK
            else:
                status = STATUS_UNKNOWN
            tool = new_tool("bash", {"command": message.get("command")})
            settle_tool(tool, status, _flatten_text(message.get("output")))
            current["tools"].append(tool)

    if not turns:
        return None
    index = int(turn_id) - 1 if str(turn_id).isdigit() and int(turn_id) > 0 else len(turns) - 1
    if index >= len(turns) or index < 0:
        index = len(turns) - 1
    picked = turns[index]
    turn = Turn()
    turn.user = picked["user"]
    turn.assistant = picked["assistant"]
    turn.tools = picked["tools"]
    turn.tool_calls = len(picked["tools"])
    turn.tokens = picked["tokens"]
    turn.line_start = picked["start"]
    turn.line_end = picked["end"]
    turn.outcome = "success" if picked["assistant"] else "incomplete"
    turn.turn_index = index + 1
    turn.compaction_count = compactions
    return turn


# --------------------------------------------------------------------------
# dsh
# --------------------------------------------------------------------------


def parse_dsh(path: Path, turn_id: str) -> Turn | None:
    """dsh 插件落盘的归一化事件流，一行一个事件。"""
    records = _read_jsonl(path)
    if not records:
        return None
    turn = Turn()
    matched: list[int] = []
    for number, item in records:
        event = str(item.get("type") or "")
        if event not in DSH_EVENT_TYPES:
            # 适配器新增了词汇而解析器还没跟上时，宁可跳过也不能猜。
            continue
        if event == "compaction":
            turn.compaction_count += 1
        elif event == "user_message":
            turn.user = str(item.get("text") or "")
            matched.append(number)
        elif event == "agent_message":
            turn.assistant = str(item.get("text") or "")
            matched.append(number)
        elif event == "tool_call":
            turn.tool_calls += 1
            turn.tools.append(new_tool(str(item.get("name") or "unknown"), item.get("args") or {}))
            matched.append(number)
        elif event == "tool_result":
            # output 是成功路径的输出原文，error 是失败路径的原文；两者都要进结果取词面。
            text = str(item.get("output") or item.get("error") or "")
            # dsh 适配器按客户端结构化字段（block.isError / data.error）写 is_error，可直接采信。
            status = status_from_flag(item.get("is_error"))
            for tool in reversed(turn.tools):
                if tool["status"] == STATUS_CALLED:
                    settle_tool(tool, status, text)
                    break
        elif event == "turn_aborted":
            turn.outcome = "aborted"
    if not turn.user and not turn.assistant:
        return None
    # dsh 适配器每轮单独落一个缓冲文件，因此固定为第 1 轮。
    turn.turn_index = 1
    turn.line_start = min(matched) if matched else 1
    turn.line_end = max(matched) if matched else len(records)
    if turn.outcome == "success" and not turn.assistant:
        turn.outcome = "incomplete"
    return turn


# --------------------------------------------------------------------------
# codemaker
# --------------------------------------------------------------------------


def parse_codemaker(path: Path, turn_id: str) -> Turn | None:
    """codemaker 会话是单个 JSON，消息数组的键名不固定，按结构探测。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    messages = _find_message_list(payload)
    if not messages:
        return None
    turns: list[dict] = []
    current: dict | None = None
    for index, message in enumerate(messages):
        role = str(message.get("role") or "")
        if role == "user":
            current = {
                "user": _flatten_text(message.get("content")),
                "assistant": "",
                "tools": [],
                "start": index,
                "end": index,
                "tokens": 0,
            }
            turns.append(current)
        elif role == "assistant" and current is not None:
            current["end"] = index
            current["tokens"] += _tokens_from_usage(message.get("usage"))
            text = _flatten_text(message.get("content"))
            if text:
                current["assistant"] = text
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                name = (function or {}).get("name") if isinstance(function, dict) else call.get("name")
                args = (function or {}).get("arguments") if isinstance(function, dict) else call.get("input")
                current["tools"].append(new_tool(str(name or "unknown"), _maybe_json(args)))
        elif role == "tool" and current is not None:
            current["end"] = index
            text = _flatten_text(message.get("content"))
            # codemaker 的 tool 消息基本不带状态字段（实测 177/177 无 isError），
            # 带则采信，不带记 unknown。
            status = status_from_flag(message.get("isError"))
            for tool in reversed(current["tools"]):
                if tool["status"] == STATUS_CALLED:
                    settle_tool(tool, status, text)
                    break

    if not turns:
        return None
    index = int(turn_id) - 1 if str(turn_id).isdigit() and int(turn_id) > 0 else len(turns) - 1
    if index >= len(turns) or index < 0:
        index = len(turns) - 1
    picked = turns[index]
    turn = Turn()
    turn.user = picked["user"]
    turn.assistant = picked["assistant"]
    turn.tools = picked["tools"]
    turn.tool_calls = len(picked["tools"])
    turn.tokens = picked["tokens"]
    turn.line_start = picked["start"]
    turn.line_end = picked["end"]
    turn.outcome = "success" if picked["assistant"] else "incomplete"
    turn.turn_index = index + 1
    turn.compaction_count = _codemaker_compactions(payload)
    return turn


def _find_message_list(payload: dict) -> list[dict]:
    """找出会话 JSON 里承载消息的那个数组。"""
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "role" in value[0]:
            return [item for item in value if isinstance(item, dict)]
    return []


def _codemaker_compactions(payload: dict) -> int:
    compression = payload.get("compression")
    if isinstance(compression, dict):
        count = compression.get("totalCompressionsCount")
        if isinstance(count, int):
            return count
        history = compression.get("compressionHistory")
        if isinstance(history, list):
            return len(history)
    return 0


PARSERS = {
    "codex": parse_codex,
    "claude": parse_claude,
    "pi": parse_pi,
    "dsh": parse_dsh,
    "codemaker": parse_codemaker,
}


def parse_transcript(client: str, transcript_path: str, turn_id: str) -> Turn | None:
    """按客户端选择解析器；路径为空或解析不出内容时返回 None。"""
    if not transcript_path:
        return None
    parser = PARSERS.get(client)
    if parser is None:
        return None
    path = Path(transcript_path).expanduser()
    if not path.exists() or not path.is_file():
        return None
    try:
        turn = parser(path, turn_id)
    except Exception:
        # 解析失败只影响本轮采集，绝不影响客户端。
        return None
    if turn is None:
        return None
    turn.duration_ms = _duration_ms(path, turn.line_start, turn.line_end)
    history = _previous_users(client, path, turn_id)
    turn.prev_users = history
    turn.prev_user = history[-1] if history else ""
    return turn


def _timestamp_of(item: dict) -> float:
    """从记录里取时间戳，返回 Unix 秒；取不到返回 0。"""
    for key in ("timestamp", "ts", "time", "created_at"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value) / 1000 if value > 10 ** 11 else float(value)
        if isinstance(value, str):
            text = value.strip().replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(text).timestamp()
            except ValueError:
                continue
    payload = item.get("payload")
    if isinstance(payload, dict):
        return _timestamp_of(payload)
    return 0.0


def _duration_ms(path: Path, start: int, end: int) -> int:
    """用转录首尾行的时间戳估算本轮墙钟耗时。"""
    if end <= start:
        return 0
    records = _read_jsonl(path)
    stamps = [_timestamp_of(item) for _, item in records if start <= _ <= end]
    stamps = [stamp for stamp in stamps if stamp > 0]
    if len(stamps) < 2:
        return 0
    return max(0, int((max(stamps) - min(stamps)) * 1000))


def _previous_users(client: str, path: Path, turn_id: str) -> list[str]:
    """取本轮之前的所有用户提问，供「需求重述」与「反复追问」判定对照。"""
    try:
        if client == "codemaker":
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            messages = _find_message_list(raw) if isinstance(raw, dict) else []
            users = [_flatten_text(item.get("content")) for item in messages if item.get("role") == "user"]
        else:
            records = _read_jsonl(path)
            users = []
            codex_users: list[str] = []
            codex_fallback: list[str] = []
            for _, item in records:
                if client == "codex":
                    role, text, is_authoritative = _codex_message(item)
                    if role == "user" and text:
                        (codex_users if is_authoritative else codex_fallback).append(text)
                elif client == "claude" and item.get("type") == "user":
                    text = _claude_user_text(item)
                    if text:
                        users.append(text)
                elif client == "pi" and str(item.get("type")) == "message":
                    message = item.get("message") or {}
                    if message.get("role") == "user":
                        users.append(_flatten_text(message.get("content")))
                elif client == "dsh" and str(item.get("type")) == "user_message":
                    users.append(str(item.get("text") or ""))
            if client == "codex":
                users = codex_users or codex_fallback
    except Exception:
        return []
    users = [text for text in users if text]
    if len(users) < 2:
        return []
    index = int(turn_id) - 1 if str(turn_id).isdigit() and int(turn_id) > 0 else len(users) - 1
    if index <= 0 or index >= len(users):
        index = len(users) - 1
    return users[:index]


def _previous_user(client: str, path: Path, turn_id: str) -> str:
    """取上一轮的用户提问，供「需求重述」判定对照。"""
    history = _previous_users(client, path, turn_id)
    return history[-1] if history else ""


def resolve_session_file(client: str, session_id: str) -> str:
    """适配层没给转录路径时，按会话 id 去默认目录里找。"""
    home = Path.home()
    if client == "codex":
        roots = [home / ".codex" / "sessions"]
        pattern = f"*{session_id}*.jsonl"
    elif client == "claude":
        roots = [home / ".claude" / "projects"]
        pattern = f"{session_id}.jsonl"
    elif client == "pi":
        roots = [home / ".pi" / "agent" / "sessions"]
        pattern = f"*{session_id}*.jsonl"
    elif client == "codemaker":
        roots = [home / ".codemaker" / "session-store" / "v1"]
        pattern = f"{session_id}.json"
    else:
        return ""
    for root in roots:
        if not root.exists():
            continue
        matches = sorted(root.rglob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
        if matches:
            return str(matches[0])
    return ""
