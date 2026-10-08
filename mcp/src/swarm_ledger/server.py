from __future__ import annotations

from contextvars import ContextVar
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import Field, WithJsonSchema

from . import front, rubric
from .front import configure as configure
from .pool import REPO_ROOT_METHOD, CallError

_TOOL: ContextVar[str | None] = ContextVar("swarm_ledger_tool", default=None)
_AGENT_ID: ContextVar[str | None] = ContextVar("swarm_ledger_agent_id", default=None)


class StampedAgentId(Middleware):
    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        arguments = dict(context.message.arguments or {})
        agent_id = arguments.pop("agent_id", None)
        message = context.message.model_copy(update={"arguments": arguments})
        tool_token = _TOOL.set(context.message.name)
        agent_token = _AGENT_ID.set(agent_id if isinstance(agent_id, str) else None)
        try:
            return await call_next(context.copy(message=message))
        finally:
            _AGENT_ID.reset(agent_token)
            _TOOL.reset(tool_token)


def _stamped_agent_id() -> str | None:
    return _AGENT_ID.get()


mcp = FastMCP("swarm-ledger", middleware=[StampedAgentId()])

_STAMPED_AGENT_ID: str | None = Depends(_stamped_agent_id)
_READ_ONLY: dict[str, Any] = {"readOnlyHint": True}

CallerName = Annotated[str, Field(description="Your own agent name, as the ledger registered it.")]
_FINDING_IDS = Field(
    default=None,
    description="The Driver finding ids the child fixes; [] when it fixes none. The Oracle must "
    "pass it while the run has an open finding. Omitted by a Manager or Lead, the child "
    "inherits the caller's own brief's list.",
)


def _schema(schema: dict[str, Any], description: str) -> WithJsonSchema:
    return WithJsonSchema({**schema, "description": description})


Dimensions = Annotated[list[str], WithJsonSchema(rubric.dimensions_schema())]
TargetedDimensions = Annotated[
    list[str], _schema(rubric.dimensions_schema(), "The dimension keys the fix must raise.")
]
_LEAD_TARGETED = Field(
    default=None,
    description="For a Lead review after a return: the dimension keys the fix aimed at.",
)
Ratings = Annotated[
    list[dict[str, Any]],
    _schema(
        rubric.ratings_schema(), "One rating for every criterion of every applicable dimension."
    ),
]
Applicable = Annotated[
    dict[str, str | None],
    _schema(
        rubric.applicable_schema(),
        "Every dimension key: null when it applies, or a one-line reason it does not.",
    ),
]

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
    "test_run_get",
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
    "drive_request",
    "drive_issue",
    "drive_checkin",
    "drive_done",
    "drive_unavailable",
)


def _call(method: str, **kwargs: Any) -> Any:
    try:
        return front.call_method(_TOOL.get(), _AGENT_ID.get(), method, kwargs)
    except CallError as exc:
        raise ToolError(str(exc)) from exc


# -- Run and plan -------------------------------------------------------------


@mcp.tool
def run_start(
    prd: str,
    session_id: str,
    oracle_name: str = "oracle",
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Starts a run from a PRD and registers the Oracle; the Oracle calls this."""
    return _call("run_start", prd=prd, session_id=agent_id or session_id, oracle_name=oracle_name)


@mcp.tool(annotations=_READ_ONLY)
def run_status(
    caller: CallerName, include_prd: bool = False, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Returns the run's phases, modules, files, agents, issues, and directives; any agent may.

    The run's PRD text is left out unless include_prd is true.
    """
    return _call("run_status", caller=caller, agent_id=agent_id, include_prd=include_prd)


@mcp.tool
def run_finish(
    caller: CallerName, outcome: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Finishes the active run once every phase is approved; the Oracle calls this."""
    return _call("run_finish", caller=caller, agent_id=agent_id, outcome=outcome)


@mcp.tool
def run_pause(
    caller: CallerName,
    reason: str,
    phases: list[int] | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Pauses the run, or just the named phases, on a blocker only the user can fix; the Oracle
    calls this. With phases, the run stays active: agent_spawn refuses those phases until
    phase_resume clears them, and the others keep going."""
    return _call("run_pause", caller=caller, agent_id=agent_id, reason=reason, phases=phases)


@mcp.tool
def phase_resume(
    caller: CallerName, phase_ids: list[int], agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Clears a scoped pause from the named phases so agent_spawn can start work in them again;
    the Oracle calls this."""
    return _call("phase_resume", caller=caller, agent_id=agent_id, phase_ids=phase_ids)


# -- Repo -----------------------------------------------------------------------------


@mcp.tool
def repo_check(
    caller: CallerName, fetch: bool = False, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Checks the repo's branch and clean state against its base branch and upstream, and
    records the result on the run; the Oracle calls this before planning."""
    return _call("repo_check", caller=caller, agent_id=agent_id, fetch=fetch)


@mcp.tool
def repo_branch_create(
    caller: CallerName, name: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Creates a run branch from the base branch once repo_check reports obvious_start; the
    Oracle calls this."""
    return _call("repo_branch_create", caller=caller, agent_id=agent_id, name=name)


@mcp.tool
def profile_set(
    caller: CallerName,
    test_command: str | None = None,
    build_command: str | None = None,
    lint_command: str | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Sets the run's test, build, and lint commands; the Oracle calls this. Refuses a build
    command that serves or watches (--watch, serve, dev-server, npm run dev, npm start)."""
    return _call(
        "profile_set",
        caller=caller,
        agent_id=agent_id,
        test_command=test_command,
        build_command=build_command,
        lint_command=lint_command,
    )


@mcp.tool
def guidelines_set(
    caller: CallerName, body: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Records the run's latest guidelines body; the Oracle calls this."""
    return _call("guidelines_set", caller=caller, agent_id=agent_id, body=body)


@mcp.tool(annotations=_READ_ONLY)
def guidelines_get(caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID) -> dict[str, Any]:
    """Returns the run's latest guidelines body; any registered agent calls this."""
    return _call("guidelines_get", caller=caller, agent_id=agent_id)


@mcp.tool
def phase_add(
    caller: CallerName,
    name: str,
    depends_on: list[int] | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Adds a phase to the plan with optional phase dependencies; the Oracle calls this. The
    stored name is p<ordinal>-<name>, and its Manager is named mgr-<that name>."""
    return _call("phase_add", caller=caller, agent_id=agent_id, name=name, depends_on=depends_on)


@mcp.tool
def phase_update(
    caller: CallerName, phase_id: int, state: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Updates a phase's state; handed_up returns the wake-up call to make as `next`."""
    return _call("phase_update", caller=caller, agent_id=agent_id, phase_id=phase_id, state=state)


@mcp.tool(annotations=_READ_ONLY)
def plan_unlocked(
    caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID
) -> list[dict[str, Any]]:
    """Lists phases whose dependencies are all approved; any registered agent calls this."""
    return _call("plan_unlocked", caller=caller, agent_id=agent_id)


@mcp.tool
def module_add(
    caller: CallerName,
    phase_id: int,
    name: str,
    depends_on: Annotated[
        list[int] | None,
        Field(description="Module ids of this phase that this module uses; brief those first."),
    ] = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Adds a module to the caller's own phase; a Manager calls this. The name is a slug, and
    its Lead is named lead-p<phase ordinal>-<name>."""
    return _call(
        "module_add",
        caller=caller,
        agent_id=agent_id,
        phase_id=phase_id,
        name=name,
        depends_on=depends_on,
    )


# -- Briefs ---------------------------------------------------------------------


@mcp.tool
def brief_create(
    caller: CallerName,
    child_name: str,
    child_role: str,
    model: str,
    body: str,
    phase_id: int | None = None,
    module_id: int | None = None,
    file_id: int | None = None,
    finding_ids: list[int] | None = _FINDING_IDS,
    effort: Annotated[
        Literal["low", "medium", "high", "xhigh", "max"] | None,
        Field(
            description="The child's effort level; leave unset for the role's default. "
            "Raise it for a fresh Coder in escalation round 2 or 3."
        ),
    ] = None,
    contract: Annotated[
        str | None,
        Field(
            description="The public contract of the child's file or module. Required before "
            "a file or module that depends on it can be briefed."
        ),
    ] = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Creates a brief for the caller's child role (Oracle->Manager, Manager->Lead, Lead->Coder).
    A Manager brief needs an unlocked phase_id of the run; a Lead brief a module_id of the
    caller's phase; a Coder brief the file_id claimed for child_name. A refusal for missing
    `finding_ids` lists the open ids and titles. Refuses an unknown finding, one that hit a
    stop rule, or one whose area has an open pattern stop directive. Refuses a Driver: only
    drive_request starts one."""
    return _call(
        "brief_create",
        caller=caller,
        agent_id=agent_id,
        child_name=child_name,
        child_role=child_role,
        model=model,
        body=body,
        phase_id=phase_id,
        module_id=module_id,
        file_id=file_id,
        finding_ids=finding_ids,
        effort=effort,
        contract=contract,
    )


@mcp.tool(annotations=_READ_ONLY)
def brief_get(caller_name: CallerName, child_name: str) -> dict[str, Any]:
    """Returns the latest brief for a child name; a new agent calls it before it has an agent_id.
    `findings` lists the id, fingerprint, title, severity, area, steps, expected and actual
    result, and evidence paths of each finding it fixes; `depends_on_contracts` the contract
    of each file or module it depends on."""
    return _call("brief_get", caller_name=caller_name, child_name=child_name)


@mcp.tool
def brief_ack(
    caller: CallerName, agent_type: str | None = None, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Binds the caller's name to its agent_id via an unacked brief; a new child agent calls it."""
    return _call("brief_ack", caller=caller, agent_id=agent_id, agent_type=agent_type)


# -- Agent lifecycle --------------------------------------------------------------


@mcp.tool
def agent_release(
    caller: CallerName, target_agent_id: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Releases an agent it directly parented; any parent role calls this for its own child."""
    return _call(
        "agent_release",
        caller=caller,
        agent_id=agent_id,
        target_agent_id=target_agent_id,
    )


@mcp.tool
def agent_spawn(
    caller: CallerName, child_name: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Starts the session for a briefed child and registers it; the brief's parent calls this."""
    return _call("agent_spawn", caller=caller, agent_id=agent_id, child_name=child_name)


@mcp.tool
def agent_resume(
    caller: CallerName, target_name: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Resumes a stopped session of the run with the wake-ups owed to it; any agent calls it."""
    return _call("agent_resume", caller=caller, agent_id=agent_id, target_name=target_name)


# -- File ownership -----------------------------------------------------------------


@mcp.tool
def claim_file(
    caller: CallerName,
    path: str,
    test_path: str | None,
    for_name: str,
    depends_on: Annotated[
        list[int] | None,
        Field(description="File ids of this module that this file uses; brief those first."),
    ] = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Claims a file path for a coder under the caller's module; a Lead calls this. for_name is
    coder-p<phase ordinal>-<module>-<file slug>."""
    return _call(
        "claim_file",
        caller=caller,
        agent_id=agent_id,
        path=path,
        test_path=test_path,
        for_name=for_name,
        depends_on=depends_on,
    )


@mcp.tool
def release_file(
    caller: CallerName, path: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Releases a live claim on a file path; a Lead calls this."""
    return _call("release_file", caller=caller, agent_id=agent_id, path=path)


@mcp.tool(annotations=_READ_ONLY)
def who_owns(path: str) -> dict[str, Any]:
    """Looks up the live owner of a file path; any caller may call this."""
    return _call("who_owns", path=path)


# -- Messages -----------------------------------------------------------------------


@mcp.tool
def message_post(
    caller: CallerName, to_name: str, body: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Posts a message of at most 32,000 characters to the caller's parent, child, or sibling
    by name; returns the wake-up call as `next`."""
    return _call("message_post", caller=caller, agent_id=agent_id, to_name=to_name, body=body)


@mcp.tool
def message_inbox(caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID) -> dict[str, Any]:
    """Returns and marks read the caller's unread messages in its run, oldest first, up to
    40,000 characters of bodies; `remaining` counts the unread ones left for the next call."""
    return _call("message_inbox", caller=caller, agent_id=agent_id)


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
        "directive_submit",
        source=source,
        sender_name=sender_name,
        body=body,
        reply_to=reply_to,
    )


@mcp.tool
def directive_inbox(
    caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID
) -> list[dict[str, Any]]:
    """Returns the run's open directives; the Oracle calls this."""
    return _call("directive_inbox", caller=caller, agent_id=agent_id)


@mcp.tool
def directive_resolve(
    caller: CallerName,
    directive_id: int,
    outcome: str,
    resolution: str,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Resolves an open directive with an outcome; the Oracle calls this.

    Outcomes: applied, scheduled, declined, needs_user. A needs_user directive stays open
    until a directive_submit with reply_to answers it, or the Oracle resolves it again.
    """
    return _call(
        "directive_resolve",
        caller=caller,
        agent_id=agent_id,
        directive_id=directive_id,
        outcome=outcome,
        resolution=resolution,
    )


# -- Overrides ------------------------------------------------------------------------


@mcp.tool
def override_grant(
    caller: CallerName,
    rule: str,
    target_agent_name: str,
    target: str,
    reason: str,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Grants a one-time override of a rule for an agent; the Oracle calls this.

    Refuses a target_agent_name that names the Oracle itself: the Oracle must never
    grant itself a write or shell override.
    """
    return _call(
        "override_grant",
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
    caller: CallerName,
    file_id: int,
    title: str,
    body: str,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Opens an issue against a file; any registered agent calls this. An issue a Manager
    opens starts at round 2, and one the Oracle opens starts at round 3."""
    return _call(
        "issue_open",
        caller=caller,
        agent_id=agent_id,
        file_id=file_id,
        title=title,
        body=body,
    )


@mcp.tool(annotations=_READ_ONLY)
def issue_list(
    caller: CallerName, file_id: int | None = None, agent_id: str | None = _STAMPED_AGENT_ID
) -> list[dict[str, Any]]:
    """Lists the run's issues, optionally filtered to one file; any registered agent calls this.
    For a Lead, hides issues its own module's self-review opened until the Lead records its
    own score for the file's current handoff."""
    return _call("issue_list", caller=caller, agent_id=agent_id, file_id=file_id)


@mcp.tool
def issue_close(
    caller: CallerName, issue_id: int, resolution: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Closes an open issue with a resolution; the file's Lead, its Manager, or the Oracle."""
    return _call(
        "issue_close",
        caller=caller,
        agent_id=agent_id,
        issue_id=issue_id,
        resolution=resolution,
    )


@mcp.tool
def idea_record(
    caller: CallerName,
    issue_id: int,
    body: str,
    outcome: str,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records an idea tried against an issue and its outcome; any registered agent calls this."""
    return _call(
        "idea_record",
        caller=caller,
        agent_id=agent_id,
        issue_id=issue_id,
        body=body,
        outcome=outcome,
    )


@mcp.tool
def issue_escalate(
    caller: CallerName, issue_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Escalates an issue to its next round, names the receiver in escalated_to, and messages
    it; only the issue's owner and the owner's parent chain call it. Returns the wake-up call
    to make as `next`."""
    return _call("issue_escalate", caller=caller, agent_id=agent_id, issue_id=issue_id)


# -- Events -----------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
def events(target_agent_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Lists recent agent lifecycle events, optionally filtered to one agent; any caller may."""
    return _call("events", agent_id=target_agent_id, limit=limit)


# -- Tests --------------------------------------------------------------------------------


@mcp.tool
def tests_run(
    caller: CallerName,
    scope: Literal["file", "module", "phase", "full"],
    target: str | None = None,
    force: Annotated[
        bool,
        Field(description="Run the tests even when an earlier passing run can be reused."),
    ] = False,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Runs the profile's test command for a scope; the required role scales Coder to Oracle.

    The result's output is a summary: the tail of a pass, or the first traceback and the
    short test summary of a failure. test_run_get returns the full output. When a passing
    run in this run used the same command on an unchanged working tree, the ledger records
    a copy of it instead of running again and returns reused: true with reused_from.
    """
    return _call(
        "tests_run",
        caller=caller,
        agent_id=agent_id,
        scope=scope,
        target=target,
        force=force,
    )


@mcp.tool(annotations=_READ_ONLY)
def test_run_get(
    caller: CallerName, test_run_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Returns one recorded test run of this run, with its full output."""
    return _call("test_run_get", caller=caller, agent_id=agent_id, test_run_id=test_run_id)


# -- Code graph -----------------------------------------------------------------------------


@mcp.tool
def graph_upsert(
    caller: CallerName, nodes: list[dict[str, Any]], agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Upserts code graph nodes. A Coder's nodes anchor only on its own claimed file; a Lead's
    may span the files of its module."""
    return _call("graph_upsert", caller=caller, agent_id=agent_id, nodes=nodes)


# -- Scoring ----------------------------------------------------------------------------------


@mcp.tool
def score_record(
    caller: CallerName,
    file_id: int,
    ratings: Ratings,
    applicable: Applicable,
    kind: Literal["self", "lead"],
    targeted: Dimensions | None = _LEAD_TARGETED,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records a self or lead review; a Coder records self, a Lead records lead. Every
    dimension is scored: rate each criterion of an applicable dimension, and give a reason and
    a ref for any rating below 9."""
    return _call(
        "score_record",
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
    caller: CallerName,
    file_id: int,
    open_issues: list[str],
    departures: list[str],
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Submits a Coder's file once its tests pass and its graph is current; returns `next`."""
    return _call(
        "handoff_submit",
        caller=caller,
        agent_id=agent_id,
        file_id=file_id,
        open_issues=open_issues,
        departures=departures,
    )


# -- Review -------------------------------------------------------------------------------------


@mcp.tool
def review_compare(
    caller: CallerName, handoff_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Compares a handoff's self and lead review scores and flags disagreements; a Lead calls it."""
    return _call("review_compare", caller=caller, agent_id=agent_id, handoff_id=handoff_id)


@mcp.tool
def approve(
    caller: CallerName,
    handoff_id: int,
    notes: str | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Approves a handoff once its lead review passes and no issue is open; a Lead calls this.
    Also accepts a floor pass: every dimension at or above the rubric floor, no criterion
    below the criterion floor, and the file's attempts at the full escalation budget
    (rounds times attempts_per_round), once the review itself misses the target. A floor
    pass records a shortfall for each dimension still below target and lists them in
    `floor_pass_dimensions`."""
    return _call("approve", caller=caller, agent_id=agent_id, handoff_id=handoff_id, notes=notes)


@mcp.tool
def return_work(
    caller: CallerName,
    handoff_id: int,
    issues: list[str],
    targeted: TargetedDimensions,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Returns a handoff to its Coder with issues to fix; a Lead calls this; returns `next`."""
    return _call(
        "return_work",
        caller=caller,
        agent_id=agent_id,
        handoff_id=handoff_id,
        issues=issues,
        targeted=targeted,
    )


@mcp.tool
def attempt_record(
    caller: CallerName, file_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Classifies a file's latest attempt as improved, plateau, or regression; a Lead calls it.
    When a plateau or regression moves an issue to round 2 or 3, `escalated` lists each
    escalation with its `escalated_to` agent and the wake-up call to make as its `next`."""
    return _call("attempt_record", caller=caller, agent_id=agent_id, file_id=file_id)


@mcp.tool
def accept_incomplete(
    caller: CallerName, handoff_id: int, reason: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Accepts a handoff as incomplete and opens a file deferral for the Manager; a Lead calls
    this after review_compare, for work the Coder or an open issue reports as incomplete."""
    return _call(
        "accept_incomplete",
        caller=caller,
        agent_id=agent_id,
        handoff_id=handoff_id,
        reason=reason,
    )


# -- Oversight: Manager and Oracle reviews -------------------------------------------------------


@mcp.tool
def module_review(
    caller: CallerName,
    module_id: int,
    outcome: Literal["accepted", "returned"],
    notes: str,
    disagreement_notes: dict[str, str] | None = None,
    scores: list[dict[str, Any]] | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records the Manager's review of a module before it hands the phase up; an accepted
    review refuses while a departure in the module waits on the Lead or the Manager, and a
    returned module owes its live Lead a wake-up as `next`. An accepted review requires
    `scores`: one rating 1..10 for each of completeness, integration, and open items, with a
    reason below 9."""
    return _call(
        "module_review",
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
    caller: CallerName,
    phase_id: int,
    outcome: Literal["accepted", "returned"],
    notes: str,
    low_score_notes: dict[str, str] | None = None,
    scores: list[dict[str, Any]] | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records the Oracle's review of a handed-up phase before phase_update(approved); an
    accepted review refuses while a departure in the phase is not signed off or reworked, and
    a returned phase owes its live Manager a wake-up as `next`. An accepted review requires
    `scores`: one rating 1..10 for each of completeness, integration, and open items, with a
    reason below 9."""
    return _call(
        "phase_review",
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
    caller: CallerName,
    body: str,
    kind: Literal["file", "module", "cross_module", "phase", "plan", "prd"],
    file_id: int | None = None,
    parties: Annotated[
        list[str] | None,
        Field(
            description="For a dispute: the agent names on the other side. Their closest "
            "shared ancestor decides it."
        ),
    ] = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Proposes a deferral or scope change, or with parties a dispute; any registered agent
    calls this. kind sets who decides: file or module, the Lead; cross_module or phase, the
    Manager; plan or prd, the Oracle (prd with the user)."""
    return _call(
        "deferral_propose",
        caller=caller,
        agent_id=agent_id,
        body=body,
        file_id=file_id,
        kind=kind,
        parties=parties,
    )


@mcp.tool
def agreement_decide(
    caller: CallerName,
    deferral_id: int,
    decision: Literal["agreed", "denied"],
    reason: str,
    directive_id: Annotated[
        int | None,
        Field(description="For a prd deferral: the user_chat directive with the user's answer."),
    ] = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Decides an open deferral as agreed or denied: a dispute by its arbiter, any other by a
    caller at or above both the proposer's parent and the kind's level."""
    return _call(
        "agreement_decide",
        caller=caller,
        agent_id=agent_id,
        deferral_id=deferral_id,
        decision=decision,
        reason=reason,
        directive_id=directive_id,
    )


@mcp.tool
def cr_open(
    caller: CallerName, path: str, body: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Opens a change request on a path the caller does not own; any role calls this. The
    ledger routes it to the file's owner, or up the chain when the owner has ended."""
    return _call("cr_open", caller=caller, agent_id=agent_id, path=path, body=body)


@mcp.tool
def cr_accept(
    caller: CallerName,
    cr_id: int,
    accept: bool,
    reason: str,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Accepts or declines an open change request; only its recipient calls this. A decline
    needs a non-empty reason."""
    return _call(
        "cr_accept",
        caller=caller,
        agent_id=agent_id,
        cr_id=cr_id,
        accept=accept,
        reason=reason,
    )


@mcp.tool
def cr_complete(
    caller: CallerName, cr_id: int, notes: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Completes an accepted change request with evidence; only its recipient calls this. It
    refuses without a passing test run or an approved handoff for the path since acceptance."""
    return _call("cr_complete", caller=caller, agent_id=agent_id, cr_id=cr_id, notes=notes)


@mcp.tool
def cr_verify(
    caller: CallerName, cr_id: int, ok: bool, notes: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Verifies a completed change request; only the requester or its nearest live ancestor
    calls this. A failed verification goes back to accepted and owes the recipient a wake-up."""
    return _call("cr_verify", caller=caller, agent_id=agent_id, cr_id=cr_id, ok=ok, notes=notes)


@mcp.tool(annotations=_READ_ONLY)
def cr_list(
    caller: CallerName, state: str | None = None, agent_id: str | None = _STAMPED_AGENT_ID
) -> list[dict[str, Any]]:
    """Lists the change requests the caller sent or received; the Oracle sees every one."""
    return _call("cr_list", caller=caller, agent_id=agent_id, state=state)


@mcp.tool
def departure_record(
    caller: CallerName,
    body: str,
    file_id: int | None = None,
    guideline_id: int | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records a departure from the guidelines; a Coder for its own file, or a Lead, Manager,
    or Oracle for work in its scope."""
    return _call(
        "departure_record",
        caller=caller,
        agent_id=agent_id,
        body=body,
        file_id=file_id,
        guideline_id=guideline_id,
    )


@mcp.tool
def departure_decide(
    caller: CallerName,
    departure_id: int,
    decision: Literal["agree", "push_back"],
    reason: str,
    solution: str | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Decides a departure at the next level of its sign-off chain: the file's Lead decides an
    open one, the phase's Manager a lead_agreed one, and the Oracle a manager_agreed one, whose
    agreement signs it off. A push_back needs a solution; above the Lead, it reopens the file
    for the same Coder, resumes the agents below the decider, and returns the first wake-up
    as `next`."""
    return _call(
        "departure_decide",
        caller=caller,
        agent_id=agent_id,
        departure_id=departure_id,
        decision=decision,
        reason=reason,
        solution=solution,
    )


@mcp.tool
def shortfall_record(
    caller: CallerName,
    body: str,
    file_id: int | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records a shortfall: a solution that works but that nobody found better; any role
    calls this, and it needs no decision."""
    return _call("shortfall_record", caller=caller, agent_id=agent_id, body=body, file_id=file_id)


# -- Versions -------------------------------------------------------------------------------------


@mcp.tool
def version_restore(
    caller: CallerName, version_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Restores a saved version onto the coder's own owned file; a Coder calls this."""
    return _call("version_restore", caller=caller, agent_id=agent_id, version_id=version_id)


# -- Reporting ------------------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
def status_tree(
    caller: CallerName, include_prd: bool = False, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Returns the run's phase, module, and file status tree with live agents; any agent may.
    `open_findings` lists the Driver findings not yet fixed or stopped, and `fixes` lists each
    brief that names findings, with their ids and titles. The run's PRD text is left out
    unless include_prd is true."""
    return _call("status_tree", caller=caller, agent_id=agent_id, include_prd=include_prd)


@mcp.tool
def report_build(caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID) -> dict[str, Any]:
    """Builds and writes the run's report.md from its current state; the Oracle calls this."""
    return _call("report_build", caller=caller, agent_id=agent_id)


@mcp.tool(annotations=_READ_ONLY)
def analytics_query(
    caller: CallerName, sql: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Runs a single read-only SELECT against the ledger database; the Oracle calls it."""
    return _call("analytics_query", caller=caller, agent_id=agent_id, sql=sql)


@mcp.tool
def drive_request(
    caller: CallerName, focus: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Requests an exploration and starts its Driver session; the Oracle calls this once the
    previous exploration and every fix it spawned have finished. Refuses when the host has no
    Driver available (cartographer and a driver plugin), while an exploration is still open,
    or while a Manager, Lead, or Coder is still live. It releases an earlier Driver whose
    exploration ended but whose session is still live. The result's `loop_status` reports the
    stop-rule state computed from every exploration and finding so far."""
    return _call("drive_request", caller=caller, agent_id=agent_id, focus=focus)


@mcp.tool
def drive_issue(
    caller: CallerName,
    request_id: int,
    finding: dict[str, Any],
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records one Driver finding against an open exploration and wakes the Oracle; the Driver
    calls this for each finding, including a failed build. `finding` holds `fingerprint`,
    `title`, `steps`, `expected`, `actual`, `severity` (blocker, major, or minor), `area`, and
    `evidence` (paths into cartographer's run folder). The result's `next_checkin_due_at` and
    `next_checkin_in_s` give when the exploration's next check-in is due."""
    return _call(
        "drive_issue",
        caller=caller,
        agent_id=agent_id,
        request_id=request_id,
        finding=finding,
    )


@mcp.tool
def drive_checkin(
    caller: CallerName,
    request_id: int,
    covered: str,
    steps: str,
    notes: str,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records a 30-minute progress check-in for an open exploration and wakes the Oracle; the
    Driver calls this. The watchdog does not report the Driver as stuck while its check-ins are
    on time. The result's `next_checkin_due_at` and `next_checkin_in_s` give when the next
    check-in is due."""
    return _call(
        "drive_checkin",
        caller=caller,
        agent_id=agent_id,
        request_id=request_id,
        covered=covered,
        steps=steps,
        notes=notes,
    )


@mcp.tool
def drive_done(
    caller: CallerName,
    request_id: int,
    blocked: str | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Ends an exploration; the Driver calls this. Any newly detected stop-rule directive is
    filed for the Oracle, and the Driver owes the Oracle a wake-up that says how the
    exploration ended: make the call in `next`, then stop. The ledger releases and stops the
    Driver's session once that wake-up is sent. Pass `blocked` with what failed when the
    Driver cannot continue, such as a failed build or a device that will not boot; it needs a
    finding already recorded with drive_issue. A clean exploration, a new stop rule, or
    `blocked` records a notification for the user."""
    return _call(
        "drive_done",
        caller=caller,
        agent_id=agent_id,
        request_id=request_id,
        blocked=blocked,
    )


@mcp.tool
def drive_unavailable(
    caller: CallerName, reason: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Files a [driver-unavailable] directive when the Driver's plugin servers fail to load;
    the Driver or the Oracle calls this with what failed. Any open exploration is abandoned and
    the user is notified. Called by the Oracle, it releases the Driver at once. Called by the
    Driver, the Driver owes the Oracle a wake-up: make the call in `next`, then stop; the
    ledger releases and stops the Driver's session once that wake-up is sent. drive_request
    and run_finish refuse while the directive is open. Resolved applied or scheduled,
    explorations resume; resolved declined, the run skips them and run_finish needs none."""
    return _call("drive_unavailable", caller=caller, agent_id=agent_id, reason=reason)


@mcp.tool(annotations=_READ_ONLY)
def ledger_info() -> dict[str, Any]:
    """Returns the server's name, version, status, tool count, and repo root."""
    return front.ledger_info(_call(REPO_ROOT_METHOD), len(_TOOL_NAMES))
