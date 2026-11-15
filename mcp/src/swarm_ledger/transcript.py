from __future__ import annotations

import json
from pathlib import Path

from .permission import input_text, one_line

TAIL_BYTES = 256 * 1024
INPUT_CHARS = 400
STEP_CHARS = 200
RECENT_STEPS = 8


def _tail(raw: object) -> list[dict]:
    if not isinstance(raw, str) or not raw:
        return []
    try:
        with Path(raw).open("rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - TAIL_BYTES)
            handle.seek(start)
            lines = handle.read().split(b"\n")
    except OSError:
        return []
    records: list[dict] = []
    for line in lines[1:] if start > 0 else lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and not record.get("isSidechain"):
            records.append(record)
    return records


def _blocks(record: dict) -> list[dict]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def pending_use(raw: object) -> dict | None:
    for record in reversed(_tail(raw)):
        blocks = _blocks(record)
        if any(b.get("type") == "tool_result" for b in blocks):
            return None
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if uses:
            return uses[-1]
    return None


def request_of(use: dict) -> dict:
    name = str(use.get("name") or "a tool")
    raw = use.get("input")
    tool_input: dict = raw if isinstance(raw, dict) else {}
    if name == "AskUserQuestion":
        questions = []
        for question in tool_input.get("questions") or []:
            if not isinstance(question, dict):
                continue
            options = [
                str(o.get("label")) for o in question.get("options") or [] if isinstance(o, dict)
            ]
            questions.append({"question": str(question.get("question") or ""), "options": options})
        return {"kind": "question", "questions": questions}
    return {"kind": "tool", "tool": name, "input": one_line(input_text(tool_input), INPUT_CHARS)}


def pending_request(raw: object) -> dict | None:
    use = pending_use(raw)
    return None if use is None else request_of(use)


def _first_line(text: str) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return one_line(line, STEP_CHARS)


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return _first_line(str(content or ""))


def recent_activity(raw: object, limit: int = RECENT_STEPS) -> list[dict]:
    steps: list[dict] = []
    for record in _tail(raw):
        at = record.get("timestamp")
        for block in _blocks(record):
            kind = block.get("type")
            if kind == "text" and record.get("type") == "assistant":
                text = _first_line(str(block.get("text") or ""))
                if text:
                    steps.append({"at": at, "kind": "say", "text": text})
            elif kind == "tool_use":
                use = request_of(block)
                text = (
                    f"{use['tool']} {use['input']}" if use["kind"] == "tool" else "AskUserQuestion"
                )
                steps.append({"at": at, "kind": "tool", "text": one_line(text, STEP_CHARS)})
            elif kind == "tool_result" and block.get("is_error"):
                steps.append({"at": at, "kind": "error", "text": _result_text(block)})
    return steps[-limit:]
