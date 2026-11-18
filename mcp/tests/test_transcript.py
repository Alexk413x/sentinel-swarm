from __future__ import annotations

import json
from pathlib import Path

from swarm_ledger import transcript


def _write(path: Path, *records: dict) -> str:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _assistant(*blocks: dict, at: str = "2026-11-14T10:00:00Z", side: bool = False) -> dict:
    return {
        "type": "assistant",
        "timestamp": at,
        "isSidechain": side,
        "message": {"role": "assistant", "content": list(blocks)},
    }


def _result(use_id: str, text: str, *, error: bool = False, at: str = "2026-11-14T10:00:05Z"):
    block = {"type": "tool_result", "tool_use_id": use_id, "content": text, "is_error": error}
    return {"type": "user", "timestamp": at, "message": {"role": "user", "content": [block]}}


def _use(use_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": use_id, "name": name, "input": tool_input}


def test_a_tool_call_with_no_result_is_the_pending_request(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        _assistant(_use("u1", "Bash", {"command": "npm ci"})),
        _result("u1", "ok"),
        _assistant(_use("u2", "Write", {"file_path": "src/a.ts", "content": "x"})),
    )
    assert transcript.pending_request(path) == {
        "kind": "tool",
        "tool": "Write",
        "input": "src/a.ts",
    }


def test_an_answered_call_or_a_missing_file_is_not_pending(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        _assistant(_use("u1", "Bash", {"command": "npm ci"})),
        _result("u1", "ok"),
        _assistant({"type": "text", "text": "Blocked on the lint gate."}),
    )
    assert transcript.pending_request(path) is None
    assert transcript.pending_request(str(tmp_path / "missing.jsonl")) is None
    assert transcript.pending_request(None) is None


def test_a_pending_question_carries_its_options(tmp_path: Path) -> None:
    asked = {"questions": [{"question": "Which branch?", "options": [{"label": "main"}]}]}
    path = _write(tmp_path / "t.jsonl", _assistant(_use("q1", "AskUserQuestion", asked)))
    assert transcript.pending_request(path) == {
        "kind": "question",
        "questions": [{"question": "Which branch?", "options": ["main"]}],
    }


def test_recent_activity_lists_the_main_threads_latest_steps(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        _assistant({"type": "text", "text": "Reading the brief.\nThen the tests."}),
        _assistant(_use("u1", "Bash", {"command": "npx vitest run src/a.test.ts"})),
        _result("u1", "1 failed\nexpected 2", error=True),
        _assistant(_use("s1", "Read", {"file_path": "x"}), side=True),
        _assistant(_use("u2", "Edit", {"file_path": "src/a.ts"}), at="2026-11-14T10:01:00Z"),
    )
    assert transcript.recent_activity(path, limit=3) == [
        {"at": "2026-11-14T10:00:00Z", "kind": "tool", "text": "Bash npx vitest run src/a.test.ts"},
        {"at": "2026-11-14T10:00:05Z", "kind": "error", "text": "1 failed"},
        {"at": "2026-11-14T10:01:00Z", "kind": "tool", "text": "Edit src/a.ts"},
    ]
    assert transcript.recent_activity(path, limit=9)[0] == {
        "at": "2026-11-14T10:00:00Z",
        "kind": "say",
        "text": "Reading the brief.",
    }
    assert transcript.recent_activity(None) == []


def test_only_the_tail_of_a_long_transcript_is_read(tmp_path: Path) -> None:
    filler = _assistant({"type": "text", "text": "x" * 1000})
    path = _write(
        tmp_path / "t.jsonl",
        *[filler] * 600,
        _assistant(_use("u9", "Bash", {"command": "npm test"})),
    )
    assert Path(path).stat().st_size > transcript.TAIL_BYTES
    assert transcript.pending_request(path) == {"kind": "tool", "tool": "Bash", "input": "npm test"}


def _blob(*blocks: dict) -> dict:
    return {
        "type": "api-request-blob",
        "hash": "h1",
        "sessionId": "s1",
        "message": {"role": "assistant", "content": list(blocks)},
    }


def test_a_replayed_request_blob_does_not_make_an_answered_call_pending(tmp_path: Path) -> None:
    edit = _use("u1", "Edit", {"file_path": "src/a.ts"})
    path = _write(
        tmp_path / "t.jsonl",
        _assistant(edit),
        _result("u1", "ok"),
        _blob(edit),
    )
    assert transcript.pending_request(path) is None


def test_a_call_is_pending_when_only_an_older_call_has_a_result(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        _assistant(_use("u1", "Bash", {"command": "npm ci"})),
        _assistant(_use("u2", "Edit", {"file_path": "src/a.ts"})),
        _result("u1", "ok"),
        _blob(_use("u1", "Bash", {"command": "npm ci"})),
    )
    assert transcript.pending_request(path) == {
        "kind": "tool",
        "tool": "Edit",
        "input": "src/a.ts",
    }


def test_recent_activity_ignores_request_blobs(tmp_path: Path) -> None:
    edit = _use("u1", "Edit", {"file_path": "src/a.ts"})
    path = _write(tmp_path / "t.jsonl", _assistant(edit), _result("u1", "ok"), _blob(edit))
    assert [s["text"] for s in transcript.recent_activity(path)] == ["Edit src/a.ts"]


def test_a_usage_limit_stop_is_found_behind_bookkeeping_records(tmp_path: Path) -> None:
    limited = {
        "type": "assistant",
        "timestamp": "2026-11-15T06:19:25Z",
        "isApiErrorMessage": True,
        "error": "rate_limit",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "limit hit"}]},
    }
    path = _write(
        tmp_path / "t.jsonl",
        _assistant(_use("u1", "Bash", {"command": "npm test"})),
        limited,
        {"type": "cost-state", "costUSD": 1.0},
        {"type": "last-prompt", "lastPrompt": "go"},
    )
    assert transcript.usage_limit_stop(path) == {"at": "2026-11-15T06:19:25Z", "text": "limit hit"}
    after = _write(tmp_path / "u.jsonl", limited, _assistant({"type": "text", "text": "back"}))
    assert transcript.usage_limit_stop(after) is None
