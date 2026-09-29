from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from swarm_ledger import checklist, metrics
from swarm_ledger.db import connect, ledger_path


def _line(**record: Any) -> str:
    return json.dumps(record) + "\n"


def _use(stamp: str, use_id: str, name: str) -> str:
    return _line(
        type="assistant",
        timestamp=stamp,
        message={"content": [{"type": "tool_use", "id": use_id, "name": name, "input": {}}]},
    )


def _result(stamp: str, use_id: str) -> str:
    return _line(
        type="user",
        timestamp=stamp,
        message={"content": [{"type": "tool_result", "tool_use_id": use_id, "content": "ok"}]},
    )


def _hook(event: str, command: str, ms: int) -> str:
    return _line(
        type="attachment",
        timestamp="2026-01-01T00:00:00.000Z",
        attachment={
            "type": "hook_success",
            "hookEvent": event,
            "command": command,
            "durationMs": ms,
        },
    )


SHIM = "python3 .sentinel-swarm/hook.py hook pre_ledger || python .sentinel-swarm/hook.py"


def _transcript(path: Path) -> Path:
    path.write_text(
        "".join(
            [
                _use("2026-01-01T00:00:00.000Z", "t1", "Read"),
                _result("2026-01-01T00:00:01.000Z", "t1"),
                _use("2026-01-01T00:00:02.000Z", "t2", "Read"),
                _result("2026-01-01T00:00:05.000Z", "t2"),
                _use("2026-01-01T00:00:06.000Z", "t3", "ToolSearch"),
                _result("2026-01-01T00:00:06.500Z", "t3"),
                _use("2026-01-01T00:00:07.000Z", "t4", "mcp__swarm-ledger__run_status"),
                _hook("PreToolUse", SHIM, 300),
                _hook("PostToolUse", "python3 other.py", 200),
                _hook("Stop", "python3 other.py", 999),
                _line(
                    type="system",
                    subtype="stop_hook_summary",
                    hookInfos=[{"command": SHIM, "durationMs": 50}, {"command": "x"}],
                ),
                _line(
                    type="assistant",
                    isSidechain=True,
                    timestamp="2026-01-01T00:00:08.000Z",
                    message={"content": [{"type": "tool_use", "id": "s1", "name": "Grep"}]},
                ),
                "not json\n",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _ledger(tmp_path: Path, transcript: Path | None) -> Path:
    conn = connect(ledger_path(tmp_path))
    try:
        run_id = conn.execute(
            "INSERT INTO runs (state, outcome, started_at, ended_at) VALUES "
            "('finished', 'success', '2026-01-01T00:00:00.000Z', '2026-01-01T00:07:30.000Z')"
        ).lastrowid
        phase_id = conn.execute(
            "INSERT INTO phases (run_id, name, ordinal, state) VALUES (?, 'p', 0, 'approved')",
            (run_id,),
        ).lastrowid
        module_id = conn.execute(
            "INSERT INTO modules (phase_id, name, state) VALUES (?, 'm', 'approved')", (phase_id,)
        ).lastrowid
        file_id = conn.execute(
            "INSERT INTO files (module_id, path, state) VALUES (?, 'a.py', 'approved')",
            (module_id,),
        ).lastrowid
        conn.execute("INSERT INTO attempts (file_id, round) VALUES (?, 1)", (file_id,))
        conn.execute("INSERT INTO attempts (file_id, round) VALUES (?, 2)", (file_id,))
        agents = [
            (
                "oracle-id",
                "oracle",
                "oracle",
                1.25,
                None,
                0,
                str(transcript) if transcript else None,
            ),
            ("coder-id", "coder-a", "coder", None, "sonnet", 2, None),
        ]
        for agent_id, name, role, cost, model, overflows, path in agents:
            conn.execute(
                "INSERT INTO agents (agent_id, name, role, run_id, state, cost_usd, model, "
                "input_tokens, output_tokens, context_overflow_count, transcript_path) "
                "VALUES (?, ?, ?, ?, 'released', ?, ?, 1000000, 0, ?, ?)",
                (agent_id, name, role, run_id, cost, model, overflows, path),
            )
        for round_ in (1, 3):
            conn.execute(
                "INSERT INTO issues (run_id, title, state, round) VALUES (?, 't', 'closed', ?)",
                (run_id, round_),
            )
        conn.execute(
            "INSERT INTO watchdog_findings (run_id, agent_id, kind, detail, first_seen_at, "
            "last_seen_at, cleared_at) VALUES (?, 'coder-id', 'stuck', 'd', 'x', 'x', 'x')",
            (run_id,),
        )
        for scope, code in (("file", 1), ("file", 0), ("full", 0), ("full", None)):
            conn.execute(
                "INSERT INTO test_runs (run_id, scope, exit_code) VALUES (?, ?, ?)",
                (run_id, scope, code),
            )
    finally:
        conn.close()
    return tmp_path


@pytest.fixture(autouse=True)
def _no_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SENTINEL_SWARM_CLAUDE", str(tmp_path / "no-claude-here"))


def test_run_metrics_counts_ledger_and_transcripts(tmp_path: Path) -> None:
    root = _ledger(tmp_path, _transcript(tmp_path / "oracle.jsonl"))
    doc = checklist.document(root)
    m = doc["metrics"]
    assert m["run"]["wall_seconds"] == 450.0
    assert m["cost_usd"]["by_role"]["oracle"] == 1.25
    assert m["cost_usd"]["by_role"]["coder"] == pytest.approx(2.0)
    assert m["cost_usd"]["total"] == pytest.approx(3.25)
    assert m["agents"] == {"coder": 1, "oracle": 1}
    assert m["tool_calls"] == 4
    assert m["toolsearch_calls"] == 1
    assert "Grep" not in m["tool_calls_by_tool"]
    assert m["tool_latency_ms_median"]["Read"] == 2000.0
    assert m["tool_latency_ms_median"]["ToolSearch"] == 500.0
    assert m["tool_latency_ms_median"]["mcp__swarm-ledger__run_status"] is None
    assert m["hook_ms"] == {"total": 550, "swarm": 350, "runs": 4}
    assert m["lead_returns"] == 2
    assert m["escalation_rounds"] == 2
    assert m["max_issue_round"] == 3
    assert m["watchdog_findings"] == {"total": 1, "live": 0, "by_kind": {"stuck": 1}}
    assert m["context_overflow_count"] == 2
    assert m["test_runs"] == {"total": 4, "failed": 1, "by_scope": {"file": 2, "full": 2}}
    assert m["transcripts"] == {"read": 1, "missing": ["coder-a"]}


def test_transcript_falls_back_to_the_project_folder(tmp_path: Path) -> None:
    root = tmp_path / "host dir"
    root.mkdir()
    folder = metrics.project_folder(root)
    assert folder.name == "".join(c if c.isalnum() else "-" for c in str(root.resolve()))
    folder.mkdir(parents=True)
    _transcript(folder / "oracle-id.jsonl")
    _ledger(root, None)
    m = checklist.document(root)["metrics"]
    assert m["transcripts"]["missing"] == ["coder-a"]
    assert m["tool_calls"] == 4


def test_unpriced_agent_makes_the_totals_unknown(tmp_path: Path) -> None:
    root = _ledger(tmp_path, None)
    conn = connect(ledger_path(root))
    try:
        conn.execute("UPDATE agents SET model = 'no-such-model' WHERE agent_id = 'coder-id'")
    finally:
        conn.close()
    cost = checklist.document(root)["metrics"]["cost_usd"]
    assert cost["total"] is None
    assert cost["by_role"] == {"coder": None, "oracle": 1.25}


def test_json_flag_prints_the_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _ledger(tmp_path, None)
    code = checklist.main(["--repo", str(root), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == 1
    assert {c["status"] for c in doc["checks"]} <= {"pass", "warn", "fail"}
    assert doc["passed"] is (code == 0)
    assert doc["metrics"]["run"]["state"] == "finished"


def test_json_flag_without_a_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert checklist.main(["--repo", str(tmp_path), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["passed"] is False
    assert doc["metrics"] is None
    assert doc["checks"][0]["status"] == "fail"
