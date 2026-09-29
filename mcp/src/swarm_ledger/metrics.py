from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import pricing
from .clock import parse_stamp

SHIM_MARKER = ".sentinel-swarm/hook.py"
LATENCY_TOOLS = ("Read", "ToolSearch")
# Claude Code writes both an attachment per Stop hook with output and one summary per stop
# listing every Stop hook, so the attachments for these events would count twice.
_SUMMARIZED_EVENTS = ("Stop", "SubagentStop")


def config_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(raw) if raw else Path.home() / ".claude"


def project_folder(repo_root: Path) -> Path:
    munged = re.sub(r"[^A-Za-z0-9]", "-", str(repo_root.resolve()))
    return config_dir() / "projects" / munged


def transcript_for(agent: dict[str, Any], repo_root: Path) -> Path | None:
    recorded = agent.get("transcript_path")
    if recorded and Path(recorded).is_file():
        return Path(recorded)
    guess = project_folder(repo_root) / f"{agent['agent_id']}.jsonl"
    return guess if guess.is_file() else None


def _records(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _ms(value: object) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def _is_swarm(command: object) -> bool:
    return isinstance(command, str) and SHIM_MARKER in command


class TranscriptStats:
    def __init__(self) -> None:
        self.tool_calls: Counter[str] = Counter()
        self.latencies: defaultdict[str, list[float]] = defaultdict(list)
        self.hook_ms = 0
        self.swarm_hook_ms = 0
        self.hook_runs = 0

    def add(self, path: Path) -> None:
        uses: dict[str, tuple[str, Any]] = {}
        results: dict[str, Any] = {}
        for record in _records(path):
            if record.get("isSidechain"):
                continue
            kind = record.get("type")
            stamp = parse_stamp(record.get("timestamp"))
            if kind == "assistant":
                for block in _blocks(record):
                    if block.get("type") == "tool_use" and block.get("id") not in uses:
                        uses[str(block.get("id"))] = (str(block.get("name")), stamp)
            elif kind == "user":
                for block in _blocks(record):
                    if block.get("type") == "tool_result":
                        results.setdefault(str(block.get("tool_use_id")), stamp)
            elif kind == "attachment":
                self._add_hook_attachment(record.get("attachment"))
            elif kind == "system" and record.get("subtype") == "stop_hook_summary":
                for info in record.get("hookInfos") or []:
                    if isinstance(info, dict):
                        self._add_hook(info.get("command"), info.get("durationMs"))
        for use_id, (name, started) in uses.items():
            self.tool_calls[name] += 1
            ended = results.get(use_id)
            if started is not None and ended is not None:
                self.latencies[name].append((ended - started).total_seconds() * 1000)

    def _add_hook_attachment(self, attachment: object) -> None:
        if not isinstance(attachment, dict):
            return
        if not str(attachment.get("type", "")).startswith("hook_"):
            return
        if attachment.get("hookEvent") in _SUMMARIZED_EVENTS or "durationMs" not in attachment:
            return
        self._add_hook(attachment.get("command"), attachment.get("durationMs"))

    def _add_hook(self, command: object, duration: object) -> None:
        ms = _ms(duration)
        self.hook_runs += 1
        self.hook_ms += ms
        if _is_swarm(command):
            self.swarm_hook_ms += ms

    def medians(self) -> dict[str, float | None]:
        names = sorted(set(self.tool_calls) | set(LATENCY_TOOLS))
        return {
            name: round(statistics.median(self.latencies[name]), 1)
            if self.latencies.get(name)
            else None
            for name in names
        }


def _agent_cost(agent: dict[str, Any]) -> float | None:
    if agent.get("cost_usd") is not None:
        return float(agent["cost_usd"])
    return pricing.estimate(agent.get("model"), agent)


def _sum_costs(costs: list[float | None]) -> float | None:
    if any(cost is None for cost in costs):
        return None
    return round(sum(cost for cost in costs if cost is not None), 4)


def _wall_seconds(run: sqlite3.Row) -> float | None:
    started, ended = parse_stamp(run["started_at"]), parse_stamp(run["ended_at"])
    if started is None or ended is None:
        return None
    return round((ended - started).total_seconds(), 1)


def _count(conn: sqlite3.Connection, sql: str, run_id: int) -> int:
    return int(conn.execute(sql, (run_id,)).fetchone()[0] or 0)


def run_metrics(conn: sqlite3.Connection, run: sqlite3.Row, repo_root: Path) -> dict[str, Any]:
    run_id = run["run_id"]
    agents = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM agents WHERE run_id = ? ORDER BY started_at", (run_id,)
        ).fetchall()
    ]
    by_role: defaultdict[str, list[float | None]] = defaultdict(list)
    for agent in agents:
        by_role[agent["role"]].append(_agent_cost(agent))

    stats = TranscriptStats()
    missing: list[str] = []
    for agent in agents:
        path = transcript_for(agent, repo_root)
        if path is None:
            missing.append(agent["name"])
        else:
            stats.add(path)

    findings = conn.execute(
        "SELECT kind, cleared_at FROM watchdog_findings WHERE run_id = ?", (run_id,)
    ).fetchall()
    tests = conn.execute(
        "SELECT scope, exit_code FROM test_runs WHERE run_id = ?", (run_id,)
    ).fetchall()
    rounds = [
        int(r["round"])
        for r in conn.execute("SELECT round FROM issues WHERE run_id = ?", (run_id,)).fetchall()
    ]
    return {
        "run": {
            "run_id": run_id,
            "state": run["state"],
            "outcome": run["outcome"],
            "started_at": run["started_at"],
            "ended_at": run["ended_at"],
            "wall_seconds": _wall_seconds(run),
        },
        "cost_usd": {
            "total": _sum_costs([cost for costs in by_role.values() for cost in costs]),
            "by_role": {role: _sum_costs(costs) for role, costs in sorted(by_role.items())},
        },
        "agents": dict(sorted(Counter(a["role"] for a in agents).items())),
        "tool_calls": sum(stats.tool_calls.values()),
        "tool_calls_by_tool": dict(sorted(stats.tool_calls.items())),
        "toolsearch_calls": stats.tool_calls.get("ToolSearch", 0),
        "tool_latency_ms_median": stats.medians(),
        "hook_ms": {
            "total": stats.hook_ms,
            "swarm": stats.swarm_hook_ms,
            "runs": stats.hook_runs,
        },
        "lead_returns": _count(
            conn,
            "SELECT COUNT(*) FROM attempts a JOIN files f ON f.file_id = a.file_id "
            "JOIN modules m ON m.module_id = f.module_id "
            "JOIN phases p ON p.phase_id = m.phase_id WHERE p.run_id = ?",
            run_id,
        ),
        "escalation_rounds": sum(r - 1 for r in rounds if r > 1),
        "max_issue_round": max(rounds, default=0),
        "watchdog_findings": {
            "total": len(findings),
            "live": sum(1 for f in findings if f["cleared_at"] is None),
            "by_kind": dict(sorted(Counter(f["kind"] for f in findings).items())),
        },
        "context_overflow_count": sum(int(a.get("context_overflow_count") or 0) for a in agents),
        "test_runs": {
            "total": len(tests),
            "failed": sum(1 for t in tests if t["exit_code"] not in (0, None)),
            "by_scope": dict(sorted(Counter(t["scope"] for t in tests).items())),
        },
        "transcripts": {"read": len(agents) - len(missing), "missing": missing},
    }
