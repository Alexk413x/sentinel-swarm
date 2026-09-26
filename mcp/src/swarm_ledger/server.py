from __future__ import annotations

import threading
import time
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
on_run_finish: Callable[[str | None], None] | None = None
last_call_at = 0.0

_TOOL_NAMES: tuple[str, ...] = (
    "run_start",
    "run_status",
    "run_finish",
    "run_pause",
    "phase_resume",
    "repo_check",
    "repo_branch_create",
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
    "module_review",
    "phase_review",
    "deferral_propose",
    "agreement_decide",
    "cr_open",
    "cr_accept",
    "cr_complete",
    "cr_verify",
    "cr_list",
    "departure_record",
    "departure_decide",
    "shortfall_record",
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
    global last_call_at
    last_call_at = time.monotonic()
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
        with _CALL_LOCK:
            oracle = (
                _ledger()
                .conn.execute(
                    "SELECT agent_id FROM agents WHERE run_id = ? AND role = 'oracle' "
                    "ORDER BY ended_at DESC LIMIT 1",
                    (result["run_id"],),
                )
                .fetchone()
            )
        on_run_finish(oracle["agent_id"] if oracle is not None else None)
    return result


@mcp.tool
def run_pause(
    caller: str,
    reason: str,
    phases: list[int] | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Pauses the run, or just the named phases, on a blocker only the user can fix; the Oracle
    calls this. With phases, the run stays active: agent_spawn refuses those phases until
    phase_resume clears them, and the others keep going."""
    return _call(
        _ledger().run_pause, caller=caller, agent_id=agent_id, reason=reason, phases=phases
    )


@mcp.tool
def phase_resume(caller: str, phase_ids: list[int], agent_id: str | None = None) -> dict[str, Any]:
    """Clears a scoped pause from the named phases so agent_spawn can start work in them again;
    the Oracle calls this."""
    return _call(_ledger().phase_resume, caller=caller, agent_id=agent_id, phase_ids=phase_ids)


# -- Repo -----------------------------------------------------------------------------


@mcp.tool
def repo_check(caller: str, fetch: bool = False, agent_id: str | None = None) -> dict[str, Any]:
    """Checks the repo's branch and clean state against its base branch and upstream, and
    records the result on the run; the Oracle calls this before planning."""
    return _call(_ledger().repo_check, caller=caller, agent_id=agent_id, fetch=fetch)


@mcp.tool
def repo_branch_create(caller: str, name: str, agent_id: str | None = None) -> dict[str, Any]:
    """Creates a run branch from the base branch once repo_check reports obvious_start; the
    Oracle calls this."""
    return _call(_ledger().repo_branch_create, caller=caller, agent_id=agent_id, name=name)


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
    """Submits a directive to the run from chat, a skill, or the watchdog; no identity required.

    Sources: user_chat, outside_session, skill, watchdog (the old user-chat and
    outside-session spellings are still accepted and normalized). A reply_to that
    names any open directive of the run resolves it, whatever its outcome.
    """
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
    """Resolves an open directive with an outcome; the Oracle calls this.

    Outcomes: applied, scheduled, declined, needs_user. A needs_user directive stays open
    until a directive_submit with reply_to answers it, or the Oracle resolves it again.
    """
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
    """Grants a one-time override of a rule for an agent; the Oracle calls this.

    Refuses a target_agent_name that names the Oracle itself: the Oracle must never
    grant itself a write or shell override.
    """
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
    """Opens an issue against a file; any registered agent calls this. An issue a Manager
    opens starts at round 2, and one the Oracle opens starts at round 3."""
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
    """Lists the run's issues, optionally filtered to one file; any registered agent calls this.
    For a Lead, hides issues its own module's self-review opened until the Lead records its
    own score for the file's current handoff."""
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
    """Escalates an issue to its next round, names the receiver in escalated_to, and messages
    it; only the issue's owner and the owner's parent chain call it. Returns the wake-up call
    to make as `next`."""
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
    """Approves a handoff once its lead review passes and no issue is open; a Lead calls this.
    Also accepts a floor pass: every dimension at or above the rubric floor, no criterion
    below the criterion floor, and the file's attempts at the full escalation budget
    (rounds times attempts_per_round), once the review itself misses the target. A floor
    pass records a shortfall for each dimension still below target and lists them in
    `floor_pass_dimensions`."""
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
    """Classifies a file's latest attempt as improved, plateau, or regression; a Lead calls it.
    When a plateau or regression moves an issue to round 2 or 3, `escalated` lists each
    escalation with its `escalated_to` agent and the wake-up call to make as its `next`."""
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


# -- Oversight: Manager and Oracle reviews -------------------------------------------------------


@mcp.tool
def module_review(
    caller: str,
    module_id: int,
    outcome: Literal["accepted", "returned"],
    notes: str,
    disagreement_notes: dict[str, str] | None = None,
    scores: list[dict[str, Any]] | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Records the Manager's review of a module before it hands the phase up; an accepted
    review refuses while a departure in the module waits on the Lead or the Manager, and a
    returned module owes its live Lead a wake-up as `next`. An accepted review requires
    `scores`: one rating 1..10 for each of completeness, integration, and open items, with a
    reason below 9."""
    return _call(
        _ledger().module_review,
        caller=caller,
        agent_id=agent_id,
        module_id=module_id,
        outcome=outcome,
        notes=notes,
        disagreement_notes=disagreement_notes,
        scores=scores,
    )


@mcp.tool
def phase_review(
    caller: str,
    phase_id: int,
    outcome: Literal["accepted", "returned"],
    notes: str,
    low_score_notes: dict[str, str] | None = None,
    scores: list[dict[str, Any]] | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Records the Oracle's review of a handed-up phase before phase_update(approved); an
    accepted review refuses while a departure in the phase is not signed off or reworked, and
    a returned phase owes its live Manager a wake-up as `next`. An accepted review requires
    `scores`: one rating 1..10 for each of completeness, integration, and open items, with a
    reason below 9."""
    return _call(
        _ledger().phase_review,
        caller=caller,
        agent_id=agent_id,
        phase_id=phase_id,
        outcome=outcome,
        notes=notes,
        low_score_notes=low_score_notes,
        scores=scores,
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


@mcp.tool
def cr_open(caller: str, path: str, body: str, agent_id: str | None = None) -> dict[str, Any]:
    """Opens a change request on a path the caller does not own; any role calls this. The
    ledger routes it to the file's owner, or up the chain when the owner has ended."""
    return _call(_ledger().cr_open, caller=caller, agent_id=agent_id, path=path, body=body)


@mcp.tool
def cr_accept(
    caller: str, cr_id: int, accept: bool, reason: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Accepts or declines an open change request; only its recipient calls this. A decline
    needs a non-empty reason."""
    return _call(
        _ledger().cr_accept,
        caller=caller,
        agent_id=agent_id,
        cr_id=cr_id,
        accept=accept,
        reason=reason,
    )


@mcp.tool
def cr_complete(caller: str, cr_id: int, notes: str, agent_id: str | None = None) -> dict[str, Any]:
    """Completes an accepted change request with evidence; only its recipient calls this. It
    refuses without a passing test run or an approved handoff for the path since acceptance."""
    return _call(_ledger().cr_complete, caller=caller, agent_id=agent_id, cr_id=cr_id, notes=notes)


@mcp.tool
def cr_verify(
    caller: str, cr_id: int, ok: bool, notes: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Verifies a completed change request; only the requester or its nearest live ancestor
    calls this. A failed verification goes back to accepted and owes the recipient a wake-up."""
    return _call(
        _ledger().cr_verify, caller=caller, agent_id=agent_id, cr_id=cr_id, ok=ok, notes=notes
    )


@mcp.tool
def cr_list(
    caller: str, state: str | None = None, agent_id: str | None = None
) -> list[dict[str, Any]]:
    """Lists the change requests the caller sent or received; the Oracle sees every one."""
    return _call(_ledger().cr_list, caller=caller, agent_id=agent_id, state=state)


@mcp.tool
def departure_record(
    caller: str,
    body: str,
    file_id: int | None = None,
    guideline_id: int | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Records a departure from the guidelines; a Coder for its own file, or a Lead, Manager,
    or Oracle for work in its scope."""
    return _call(
        _ledger().departure_record,
        caller=caller,
        agent_id=agent_id,
        body=body,
        file_id=file_id,
        guideline_id=guideline_id,
    )


@mcp.tool
def departure_decide(
    caller: str,
    departure_id: int,
    decision: Literal["agree", "push_back"],
    reason: str,
    solution: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Decides a departure at the next level of its sign-off chain: the file's Lead decides an
    open one, the phase's Manager a lead_agreed one, and the Oracle a manager_agreed one, whose
    agreement signs it off. A push_back needs a solution; above the Lead, it reopens the file
    for the same Coder, resumes the agents below the decider, and returns the first wake-up
    as `next`."""
    return _call(
        _ledger().departure_decide,
        caller=caller,
        agent_id=agent_id,
        departure_id=departure_id,
        decision=decision,
        reason=reason,
        solution=solution,
    )


@mcp.tool
def shortfall_record(
    caller: str, body: str, file_id: int | None = None, agent_id: str | None = None
) -> dict[str, Any]:
    """Records a shortfall: a solution that works but that nobody found better; any role
    calls this, and it needs no decision."""
    return _call(
        _ledger().shortfall_record, caller=caller, agent_id=agent_id, body=body, file_id=file_id
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
