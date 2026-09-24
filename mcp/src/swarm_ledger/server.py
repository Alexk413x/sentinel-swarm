from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import __version__, env, rubric
from .identity import LedgerError
from .ledger import Ledger

mcp = FastMCP("swarm-ledger")

T = TypeVar("T")

_instance: Ledger | None = None
_root: Path | None = None
_CALL_LOCK = threading.RLock()
on_run_finish: Callable[[], None] | None = None

_TOOL_NAMES: tuple[str, ...] = (
    "run_start",
    "run_status",
    "run_finish",
    "run_pause",
    "profile_set",
    "guidelines_set",
    "guidelines_get",
    "phase_add",
    "phase_update",
    "plan_unlocked",
    "module_add",
    "brief_create",
    "brief_get",
    "brief_ack",
    "agent_release",
    "agent_spawn",
    "agent_resume",
    "claim_file",
    "release_file",
    "who_owns",
    "message_post",
    "message_inbox",
    "directive_submit",
    "directive_inbox",
    "directive_resolve",
    "override_grant",
    "issue_open",
    "issue_list",
    "issue_close",
    "idea_record",
    "issue_escalate",
    "events",
    "tests_run",
    "graph_upsert",
    "score_record",
    "handoff_submit",
    "review_compare",
    "approve",
    "return_work",
    "attempt_record",
    "accept_incomplete",
    "deferral_propose",
    "agreement_decide",
    "version_restore",
    "status_tree",
    "report_build",
    "analytics_query",
    "ledger_info",
)


def configure(root: Path) -> None:
    global _instance, _root
    _root = root
    _instance = None


def _ledger() -> Ledger:
    global _instance
    if _instance is None:
        _instance = env.open_ledger(_root)
    return _instance


def _call(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        with _CALL_LOCK:
            return fn(*args, **kwargs)
    except LedgerError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


# -- Run and plan -------------------------------------------------------------


@mcp.tool
def run_start(
    prd: str,
    session_id: str,
    oracle_name: str = "oracle",
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Starts a run from a PRD and registers the Oracle; the Oracle calls this."""
    return _call(_ledger().run_start, prd, agent_id or session_id, oracle_name)


@mcp.tool
def run_status(caller: str, agent_id: str | None = None) -> dict[str, Any]:
    """Returns the run's phases, modules, files, agents, issues, and directives; any agent may."""
    return _call(_ledger().run_status, caller=caller, agent_id=agent_id)


@mcp.tool
def run_finish(caller: str, outcome: str, agent_id: str | None = None) -> dict[str, Any]:
    """Finishes the active run once every phase is approved; the Oracle calls this."""
    result = _call(_ledger().run_finish, caller=caller, agent_id=agent_id, outcome=outcome)
    if on_run_finish is not None:
        on_run_finish()
    return result


@mcp.tool
def run_pause(caller: str, reason: str, agent_id: str | None = None) -> dict[str, Any]:
    """Pauses the run on a blocker only the user can fix; the Oracle calls this."""
    return _call(_ledger().run_pause, caller=caller, agent_id=agent_id, reason=reason)


@mcp.tool
def profile_set(
    caller: str,
    test_command: str | None = None,
    build_command: str | None = None,
    lint_command: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Sets the run's test, build, and lint commands; the Oracle calls this."""
    return _call(
        _ledger().profile_set,
        caller=caller,
        agent_id=agent_id,
        test_command=test_command,
        build_command=build_command,
        lint_command=lint_command,
    )


@mcp.tool
def guidelines_set(caller: str, body: str, agent_id: str | None = None) -> dict[str, Any]:
    """Records the run's latest guidelines body; the Oracle calls this."""
    return _call(_ledger().guidelines_set, caller=caller, agent_id=agent_id, body=body)


@mcp.tool
def guidelines_get(caller: str, agent_id: str | None = None) -> dict[str, Any]:
    """Returns the run's latest guidelines body; any registered agent calls this."""
    return _call(_ledger().guidelines_get, caller=caller, agent_id=agent_id)


@mcp.tool
def phase_add(
    caller: str,
    name: str,
    depends_on: list[int] | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Adds a phase to the plan with optional phase dependencies; the Oracle calls this."""
    return _call(
        _ledger().phase_add, caller=caller, agent_id=agent_id, name=name, depends_on=depends_on
    )


@mcp.tool
def phase_update(
    caller: str, phase_id: int, state: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Updates a phase's state; handed_up returns the wake-up call to make as `next`."""
    return _call(
        _ledger().phase_update, caller=caller, agent_id=agent_id, phase_id=phase_id, state=state
    )


@mcp.tool
def plan_unlocked(caller: str, agent_id: str | None = None) -> list[dict[str, Any]]:
    """Lists phases whose dependencies are all approved; any registered agent calls this."""
    return _call(_ledger().plan_unlocked, caller=caller, agent_id=agent_id)


@mcp.tool
def module_add(
    caller: str, phase_id: int, name: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Adds a module to the caller's own phase; a Manager calls this."""
    return _call(
        _ledger().module_add, caller=caller, agent_id=agent_id, phase_id=phase_id, name=name
    )


# -- Briefs ---------------------------------------------------------------------


@mcp.tool
def brief_create(
    caller: str,
    child_name: str,
    child_role: str,
    model: str,
    body: str,
    phase_id: int | None = None,
    module_id: int | None = None,
    file_id: int | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Creates a brief for the caller's child role (Oracle->Manager, Manager->Lead, Lead->Coder)."""
    return _call(
        _ledger().brief_create,
        caller=caller,
        agent_id=agent_id,
        child_name=child_name,
        child_role=child_role,
        model=model,
        body=body,
        phase_id=phase_id,
        module_id=module_id,
        file_id=file_id,
    )


@mcp.tool
def brief_get(caller_name: str, child_name: str) -> dict[str, Any]:
    """Returns the latest brief for a child name; a new agent calls it before it has an agent_id."""
    return _call(_ledger().brief_get, caller_name=caller_name, child_name=child_name)


@mcp.tool
def brief_ack(
    caller: str, agent_type: str | None = None, agent_id: str | None = None
) -> dict[str, Any]:
    """Binds the caller's name to its agent_id via an unacked brief; a new child agent calls it."""
    return _call(_ledger().brief_ack, caller=caller, agent_id=agent_id, agent_type=agent_type)


# -- Agent lifecycle --------------------------------------------------------------


@mcp.tool
def agent_release(caller: str, target_agent_id: str, agent_id: str | None = None) -> dict[str, Any]:
    """Releases an agent it directly parented; any parent role calls this for its own child."""
    return _call(
        _ledger().agent_release,
        caller=caller,
        agent_id=agent_id,
        target_agent_id=target_agent_id,
    )


@mcp.tool
def agent_spawn(caller: str, child_name: str, agent_id: str | None = None) -> dict[str, Any]:
    """Starts the session for a briefed child and registers it; the brief's parent calls this."""
    return _call(_ledger().agent_spawn, caller=caller, agent_id=agent_id, child_name=child_name)


@mcp.tool
def agent_resume(caller: str, target_name: str, agent_id: str | None = None) -> dict[str, Any]:
    """Resumes a stopped session of the run with the wake-ups owed to it; any agent calls it."""
    return _call(_ledger().agent_resume, caller=caller, agent_id=agent_id, target_name=target_name)


# -- File ownership -----------------------------------------------------------------


@mcp.tool
def claim_file(
    caller: str,
    path: str,
    test_path: str | None,
    for_name: str,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Claims a file path for a coder under the caller's module; a Lead calls this."""
    return _call(
        _ledger().claim_file,
        caller=caller,
        agent_id=agent_id,
        path=path,
        test_path=test_path,
        for_name=for_name,
    )


@mcp.tool
def release_file(caller: str, path: str, agent_id: str | None = None) -> dict[str, Any]:
    """Releases a live claim on a file path; a Lead calls this."""
    return _call(_ledger().release_file, caller=caller, agent_id=agent_id, path=path)


@mcp.tool
def who_owns(path: str) -> dict[str, Any]:
    """Looks up the live owner of a file path; any caller may call this."""
    return _call(_ledger().who_owns, path=path)


# -- Messages -----------------------------------------------------------------------


@mcp.tool
def message_post(
    caller: str, to_name: str, body: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Posts a message to an agent of the run by name; returns the wake-up call as `next`."""
    return _call(
        _ledger().message_post, caller=caller, agent_id=agent_id, to_name=to_name, body=body
    )


@mcp.tool
def message_inbox(caller: str, agent_id: str | None = None) -> list[dict[str, Any]]:
    """Returns and marks read the caller's unread messages; any registered agent calls this."""
    return _call(_ledger().message_inbox, caller=caller, agent_id=agent_id)


# -- Directives ---------------------------------------------------------------------


@mcp.tool
def directive_submit(
    source: str, sender_name: str | None, body: str, reply_to: int | None = None
) -> dict[str, Any]:
    """Submits a directive to the run from chat, a skill, or the watchdog; no identity required."""
    return _call(
        _ledger().directive_submit,
        source=source,
        sender_name=sender_name,
        body=body,
        reply_to=reply_to,
    )


@mcp.tool
def directive_inbox(caller: str, agent_id: str | None = None) -> list[dict[str, Any]]:
    """Returns the run's open directives; the Oracle calls this."""
    return _call(_ledger().directive_inbox, caller=caller, agent_id=agent_id)


@mcp.tool
def directive_resolve(
    caller: str,
    directive_id: int,
    outcome: str,
    resolution: str,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Resolves an open directive with an outcome; the Oracle calls this."""
    return _call(
        _ledger().directive_resolve,
        caller=caller,
        agent_id=agent_id,
        directive_id=directive_id,
        outcome=outcome,
        resolution=resolution,
    )


# -- Overrides ------------------------------------------------------------------------


@mcp.tool
def override_grant(
    caller: str,
    rule: str,
    target_agent_name: str,
    target: str,
    reason: str,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Grants a one-time override of a rule for an agent; the Oracle calls this."""
    return _call(
        _ledger().override_grant,
        caller=caller,
        agent_id=agent_id,
        rule=rule,
        target_agent_name=target_agent_name,
        target=target,
        reason=reason,
    )


# -- Issues -------------------------------------------------------------------------


@mcp.tool
def issue_open(
    caller: str, file_id: int, title: str, body: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Opens an issue against a file; any registered agent calls this."""
    return _call(
        _ledger().issue_open,
        caller=caller,
        agent_id=agent_id,
        file_id=file_id,
        title=title,
        body=body,
    )


@mcp.tool
def issue_list(
    caller: str, file_id: int | None = None, agent_id: str | None = None
) -> list[dict[str, Any]]:
    """Lists the run's issues, optionally filtered to one file; any registered agent calls this."""
    return _call(_ledger().issue_list, caller=caller, agent_id=agent_id, file_id=file_id)


@mcp.tool
def issue_close(
    caller: str, issue_id: int, resolution: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Closes an open issue with a resolution; the file's Lead, its Manager, or the Oracle."""
    return _call(
        _ledger().issue_close,
        caller=caller,
        agent_id=agent_id,
        issue_id=issue_id,
        resolution=resolution,
    )


@mcp.tool
def idea_record(
    caller: str, issue_id: int, body: str, outcome: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Records an idea tried against an issue and its outcome; any registered agent calls this."""
    return _call(
        _ledger().idea_record,
        caller=caller,
        agent_id=agent_id,
        issue_id=issue_id,
        body=body,
        outcome=outcome,
    )


@mcp.tool
def issue_escalate(caller: str, issue_id: int, agent_id: str | None = None) -> dict[str, Any]:
    """Escalates an issue to its next round and messages the agent that takes it; only the
    issue's owner and the owner's parent chain call it."""
    return _call(_ledger().issue_escalate, caller=caller, agent_id=agent_id, issue_id=issue_id)


# -- Events -----------------------------------------------------------------------------


@mcp.tool
def events(agent_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Lists recent agent lifecycle events, optionally filtered to one agent; any caller may."""
    return _call(_ledger().events, agent_id=agent_id, limit=limit)


# -- Tests --------------------------------------------------------------------------------


@mcp.tool
def tests_run(
    caller: str,
    scope: Literal["file", "module", "phase", "full"],
    target: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Runs the profile's test command for a scope; the required role scales Coder to Oracle."""
    return _call(_ledger().tests_run, caller=caller, agent_id=agent_id, scope=scope, target=target)


# -- Code graph -----------------------------------------------------------------------------


@mcp.tool
def graph_upsert(
    caller: str, nodes: list[dict[str, Any]], agent_id: str | None = None
) -> dict[str, Any]:
    """Upserts code graph nodes anchored on the caller's own claimed file; a Coder calls this."""
    return _call(_ledger().graph_upsert, caller=caller, agent_id=agent_id, nodes=nodes)


# -- Scoring ----------------------------------------------------------------------------------


@mcp.tool(
    description="Records a self or lead review; a Coder records self, a Lead records lead. "
    "Ratings cover every criterion of every applicable dimension. " + rubric.schema_help()
)
def score_record(
    caller: str,
    file_id: int,
    ratings: list[dict[str, Any]],
    applicable: dict[str, str | None],
    kind: Literal["self", "lead"],
    targeted: list[str] | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    return _call(
        _ledger().score_record,
        caller=caller,
        agent_id=agent_id,
        file_id=file_id,
        ratings=ratings,
        applicable=applicable,
        kind=kind,
        targeted=targeted,
    )


# -- Handoff ------------------------------------------------------------------------------------


@mcp.tool
def handoff_submit(
    caller: str,
    file_id: int,
    open_issues: list[str],
    departures: list[str],
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Submits a Coder's file once its tests pass and its graph is current; returns `next`."""
    return _call(
        _ledger().handoff_submit,
        caller=caller,
        agent_id=agent_id,
        file_id=file_id,
        open_issues=open_issues,
        departures=departures,
    )


# -- Review -------------------------------------------------------------------------------------


@mcp.tool
def review_compare(caller: str, handoff_id: int, agent_id: str | None = None) -> dict[str, Any]:
    """Compares a handoff's self and lead review scores and flags disagreements; a Lead calls it."""
    return _call(_ledger().review_compare, caller=caller, agent_id=agent_id, handoff_id=handoff_id)


@mcp.tool
def approve(
    caller: str, handoff_id: int, notes: str | None = None, agent_id: str | None = None
) -> dict[str, Any]:
    """Approves a handoff once its lead review passes and no issue is open; a Lead calls this."""
    return _call(
        _ledger().approve, caller=caller, agent_id=agent_id, handoff_id=handoff_id, notes=notes
    )


@mcp.tool
def return_work(
    caller: str,
    handoff_id: int,
    issues: list[str],
    targeted: list[str],
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Returns a handoff to its Coder with issues to fix; a Lead calls this; returns `next`."""
    return _call(
        _ledger().return_work,
        caller=caller,
        agent_id=agent_id,
        handoff_id=handoff_id,
        issues=issues,
        targeted=targeted,
    )


@mcp.tool
def attempt_record(caller: str, file_id: int, agent_id: str | None = None) -> dict[str, Any]:
    """Classifies a file's latest attempt as improved, plateau, or regression; a Lead calls it."""
    return _call(_ledger().attempt_record, caller=caller, agent_id=agent_id, file_id=file_id)


@mcp.tool
def accept_incomplete(
    caller: str, handoff_id: int, reason: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Accepts a handoff as incomplete and opens a deferral; a Lead calls this."""
    return _call(
        _ledger().accept_incomplete,
        caller=caller,
        agent_id=agent_id,
        handoff_id=handoff_id,
        reason=reason,
    )


# -- Agreements -----------------------------------------------------------------------------------


@mcp.tool
def deferral_propose(
    caller: str, body: str, file_id: int | None = None, agent_id: str | None = None
) -> dict[str, Any]:
    """Proposes a deferral, optionally tied to a file; any registered agent calls this."""
    return _call(
        _ledger().deferral_propose, caller=caller, agent_id=agent_id, body=body, file_id=file_id
    )


@mcp.tool
def agreement_decide(
    caller: str,
    deferral_id: int,
    decision: Literal["agreed", "denied"],
    reason: str,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Decides an open deferral as agreed or denied; the proposer's parent role or above calls."""
    return _call(
        _ledger().agreement_decide,
        caller=caller,
        agent_id=agent_id,
        deferral_id=deferral_id,
        decision=decision,
        reason=reason,
    )


# -- Versions -------------------------------------------------------------------------------------


@mcp.tool
def version_restore(caller: str, version_id: int, agent_id: str | None = None) -> dict[str, Any]:
    """Restores a saved version onto the coder's own owned file; a Coder calls this."""
    return _call(_ledger().version_restore, caller=caller, agent_id=agent_id, version_id=version_id)


# -- Reporting ------------------------------------------------------------------------------------


@mcp.tool
def status_tree(caller: str, agent_id: str | None = None) -> dict[str, Any]:
    """Returns the run's phase, module, and file status tree with live agents; any agent may."""
    return _call(_ledger().status_tree, caller=caller, agent_id=agent_id)


@mcp.tool
def report_build(caller: str, agent_id: str | None = None) -> dict[str, Any]:
    """Builds and writes the run's report.md from its current state; the Oracle calls this."""
    return _call(_ledger().report_build, caller=caller, agent_id=agent_id)


@mcp.tool
def analytics_query(caller: str, sql: str, agent_id: str | None = None) -> dict[str, Any]:
    """Runs a single read-only SELECT against the ledger database; the Oracle calls it."""
    return _call(_ledger().analytics_query, caller=caller, agent_id=agent_id, sql=sql)


@mcp.tool
def ledger_info() -> dict[str, Any]:
    """Returns the server's name, version, status, tool count, and repo root."""
    ledger = _call(_ledger)
    return {
        "name": "swarm-ledger",
        "version": __version__,
        "status": "ready",
        "tools": len(_TOOL_NAMES),
        "repo_root": str(ledger.repo_root),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
