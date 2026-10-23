from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from contextvars import ContextVar
from typing import Annotated, Any, Literal, Optional

from fastmcp import FastMCP
from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from pydantic import Field, WithJsonSchema

from . import front, rubric
from .agentfiles import LEDGER_SERVER
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


# Depends(_AGENT_ID.get) fails at call time ("Failed to resolve dependency"); keep the function.
def _stamped_agent_id() -> str | None:
    return _AGENT_ID.get()


mcp = FastMCP(LEDGER_SERVER, middleware=[StampedAgentId()])

_STAMPED_AGENT_ID: str | None = Depends(_stamped_agent_id)
_READ_ONLY: dict[str, Any] = {"readOnlyHint": True}

CallerName = Annotated[str, Field(description="Your own agent name, as the ledger registered it.")]
# An optional argument with a description takes a Field default, never Annotated[X | None, Field]
# = None: Python 3.10 nests that schema, and catalog.json would then differ by Python version.
_FINDING_IDS = Field(
    default=None,
    description="The Driver finding ids the child fixes; [] when it fixes none. The Oracle must "
    "pass it while the run has an open finding. Omitted by a Manager or Lead, the child "
    "inherits the caller's own brief's list.",
)
_MODULE_ADD_DEPENDS_ON = Field(
    default=None, description="Module ids of this phase that this module uses; brief those first."
)
_BRIEF_CREATE_EFFORT = Field(
    default=None,
    description="The child's effort level; leave unset for the role's default. "
    "Raise it for a fresh Coder in escalation round 2 or 3.",
)
_BRIEF_CREATE_CONTRACT = Field(
    default=None,
    description="The public contract of the child's file or module. Required before "
    "a file or module that depends on it can be briefed.",
)
_CLAIM_FILE_DEPENDS_ON = Field(
    default=None, description="File ids of this module that this file uses; brief those first."
)
_DEFERRAL_PROPOSE_PARTIES = Field(
    default=None,
    description="For a dispute: the agent names on the other side. Their closest "
    "shared ancestor decides it.",
)
_AGREEMENT_DECIDE_DIRECTIVE_ID = Field(
    default=None, description="For a prd deferral: the user_chat directive with the user's answer."
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
    dict[str, Optional[str]],
    _schema(
        rubric.applicable_schema(),
        "Every dimension key: null when it applies, or a one-line reason it does not.",
    ),
]


def _call(method: str, **kwargs: Any) -> Any:
    try:
        return front.call_method(_TOOL.get(), _AGENT_ID.get(), method, kwargs)
    except CallError as exc:
        raise ToolError(str(exc)) from exc


_TOOL_NAMES: list[str] = []


def _forwarding(fn: Callable[..., Any]) -> Callable[..., Any]:
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def forward(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return _call(fn.__name__, **bound.arguments)

    return forward


# A forwarding tool sends every parameter, agent_id included, to the ledger method of the same
# name. A tool that renames or drops one (run_start, events) or calls no method takes forward=False.
def _tool(*, read_only: bool = False, forward: bool = True) -> Callable[[Callable[..., Any]], Any]:
    def register(fn: Callable[..., Any]) -> Any:
        _TOOL_NAMES.append(fn.__name__)
        target = _forwarding(fn) if forward else fn
        return mcp.tool(target, annotations=_READ_ONLY) if read_only else mcp.tool(target)

    return register


@_tool(forward=False)
def run_start(
    prd: str,
    session_id: str,
    oracle_name: str = "oracle",
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Starts a run from a PRD and registers the Oracle; the Oracle calls this."""
    return _call("run_start", prd=prd, session_id=agent_id or session_id, oracle_name=oracle_name)


@_tool(read_only=True)
def run_status(
    caller: CallerName, include_prd: bool = False, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Returns the run's phases, modules, files, agents, issues, and directives; any agent may.

    The run's PRD text is left out unless include_prd is true.
    """
    ...


@_tool()
def run_finish(
    caller: CallerName, outcome: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Finishes the active run once every phase is approved; the Oracle calls this."""
    ...


@_tool()
def run_pause(
    caller: CallerName,
    reason: str,
    phases: list[int] | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Pauses the run, or just the named phases, on a blocker only the user can fix; the Oracle
    calls this. With phases, the run stays active: agent_spawn refuses those phases until
    phase_resume clears them, and the others keep going."""
    ...


@_tool()
def phase_resume(
    caller: CallerName, phase_ids: list[int], agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Clears a scoped pause from the named phases so agent_spawn can start work in them again;
    the Oracle calls this."""
    ...


@_tool()
def repo_check(
    caller: CallerName, fetch: bool = False, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Checks the repo's branch and clean state against its base branch and upstream, and
    records the result on the run; the Oracle calls this before planning."""
    ...


@_tool()
def repo_branch_create(
    caller: CallerName, name: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Creates a run branch from the base branch once repo_check reports obvious_start; the
    Oracle calls this."""
    ...


@_tool()
def profile_set(
    caller: CallerName,
    test_command: str | None = None,
    build_command: str | None = None,
    lint_command: str | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Sets the run's test, build, and lint commands; the Oracle calls this. Refuses a build
    command that serves or watches (--watch, serve, dev-server, npm run dev, npm start)."""
    ...


@_tool()
def guidelines_set(
    caller: CallerName, body: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Records the run's latest guidelines body; the Oracle calls this."""
    ...


@_tool(read_only=True)
def guidelines_get(caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID) -> dict[str, Any]:
    """Returns the run's latest guidelines body; any registered agent calls this."""
    ...


@_tool()
def phase_add(
    caller: CallerName,
    name: str,
    depends_on: list[int] | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Adds a phase to the plan with optional phase dependencies; the Oracle calls this. The
    stored name is p<ordinal>-<name>, and its Manager is named mgr-<that name>."""
    ...


@_tool()
def phase_update(
    caller: CallerName, phase_id: int, state: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Updates a phase's state; handed_up returns the wake-up call to make as `next`."""
    ...


@_tool(read_only=True)
def plan_unlocked(
    caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID
) -> list[dict[str, Any]]:
    """Lists phases whose dependencies are all approved; any registered agent calls this."""
    ...


@_tool()
def module_add(
    caller: CallerName,
    phase_id: int,
    name: str,
    depends_on: list[int] | None = _MODULE_ADD_DEPENDS_ON,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Adds a module to the caller's own phase; a Manager calls this. The name is a slug, and
    its Lead is named lead-p<phase ordinal>-<name>."""
    ...


@_tool()
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
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = _BRIEF_CREATE_EFFORT,
    contract: str | None = _BRIEF_CREATE_CONTRACT,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Creates a brief for the caller's child role (Oracle->Manager, Manager->Lead, Lead->Coder).
    A Manager brief needs an unlocked phase_id of the run; a Lead brief a module_id of the
    caller's phase; a Coder brief the file_id claimed for child_name. A refusal for missing
    `finding_ids` lists the open ids and titles. Refuses an unknown finding, one that hit a
    stop rule, or one whose area has an open pattern stop directive. Refuses a Driver: only
    drive_request starts one."""
    ...


@_tool(read_only=True)
def brief_get(caller_name: CallerName, child_name: str) -> dict[str, Any]:
    """Returns the latest brief for a child name; a new agent calls it before it has an agent_id.
    `findings` lists the id, fingerprint, title, severity, area, steps, expected and actual
    result, and evidence paths of each finding it fixes; `depends_on_contracts` the contract
    of each file or module it depends on."""
    ...


@_tool()
def brief_ack(caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID) -> dict[str, Any]:
    """Binds the caller's name to its agent_id via an unacked brief; a new child agent calls it."""
    ...


@_tool()
def agent_release(
    caller: CallerName, target_agent_id: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Releases an agent it directly parented; any parent role calls this for its own child."""
    ...


@_tool()
def agent_spawn(
    caller: CallerName, child_name: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Starts the session for a briefed child and registers it; the brief's parent calls this."""
    ...


@_tool()
def agent_resume(
    caller: CallerName, target_name: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Resumes a stopped session of the run with the wake-ups owed to it; any agent calls it."""
    ...


@_tool()
def claim_file(
    caller: CallerName,
    path: str,
    test_path: str | None,
    for_name: str,
    depends_on: list[int] | None = _CLAIM_FILE_DEPENDS_ON,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Claims a file path for a coder under the caller's module; a Lead calls this. for_name is
    coder-p<phase ordinal>-<module>-<file slug>."""
    ...


@_tool()
def release_file(
    caller: CallerName, path: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Releases a live claim on a file path; a Lead calls this."""
    ...


@_tool(read_only=True)
def who_owns(path: str) -> dict[str, Any]:
    """Looks up the live owner of a file path; any caller may call this."""
    ...


@_tool()
def message_post(
    caller: CallerName, to_name: str, body: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Posts a message of at most 32,000 characters to the caller's parent, child, or sibling
    by name; returns the wake-up call as `next`."""
    ...


@_tool()
def message_inbox(caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID) -> dict[str, Any]:
    """Returns and marks read the caller's unread messages in its run, oldest first, up to
    40,000 characters of bodies; `remaining` counts the unread ones left for the next call."""
    ...


@_tool()
def directive_submit(
    source: str, sender_name: str | None, body: str, reply_to: int | None = None
) -> dict[str, Any]:
    """Submits a directive to the run from chat, a skill, or the watchdog; no identity required.

    Sources: user_chat, outside_session, skill, watchdog. A reply_to that names any
    open directive of the run resolves it, whatever its outcome.
    """
    ...


@_tool()
def directive_inbox(
    caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID
) -> list[dict[str, Any]]:
    """Returns the run's open directives; the Oracle calls this."""
    ...


@_tool()
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
    ...


@_tool()
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
    ...


@_tool()
def issue_open(
    caller: CallerName,
    file_id: int,
    title: str,
    body: str,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Opens an issue against a file; any registered agent calls this. An issue a Manager
    opens starts at round 2, and one the Oracle opens starts at round 3."""
    ...


@_tool(read_only=True)
def issue_list(
    caller: CallerName, file_id: int | None = None, agent_id: str | None = _STAMPED_AGENT_ID
) -> list[dict[str, Any]]:
    """Lists the run's issues, optionally filtered to one file; any registered agent calls this.
    For a Lead, hides issues its own module's self-review opened until the Lead records its
    own score for the file's current handoff."""
    ...


@_tool()
def issue_close(
    caller: CallerName, issue_id: int, resolution: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Closes an open issue with a resolution; the file's Lead, its Manager, or the Oracle."""
    ...


@_tool()
def idea_record(
    caller: CallerName,
    issue_id: int,
    body: str,
    outcome: str,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records an idea tried against an issue and its outcome; any registered agent calls this."""
    ...


@_tool()
def issue_escalate(
    caller: CallerName, issue_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Escalates an issue to its next round, names the receiver in escalated_to, and messages
    it; only the issue's owner and the owner's parent chain call it. Returns the wake-up call
    to make as `next`."""
    ...


@_tool(read_only=True, forward=False)
def events(target_agent_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Lists recent agent lifecycle events, optionally filtered to one agent; any caller may."""
    return _call("events", agent_id=target_agent_id, limit=limit)


@_tool()
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
    ...


@_tool(read_only=True)
def test_run_get(
    caller: CallerName, test_run_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Returns one recorded test run of this run, with its full output."""
    ...


@_tool()
def graph_upsert(
    caller: CallerName, nodes: list[dict[str, Any]], agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Upserts code graph nodes. A Coder's nodes anchor only on its own claimed file; a Lead's
    may span the files of its module."""
    ...


@_tool()
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
    ...


@_tool()
def handoff_submit(
    caller: CallerName,
    file_id: int,
    open_issues: list[str],
    departures: list[str],
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Submits a Coder's file once its tests pass and its graph is current; returns `next`."""
    ...


@_tool()
def review_compare(
    caller: CallerName, handoff_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Compares a handoff's self and lead review scores and flags disagreements; a Lead calls it."""
    ...


@_tool()
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
    ...


@_tool()
def return_work(
    caller: CallerName,
    handoff_id: int,
    issues: list[str],
    targeted: TargetedDimensions,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Returns a handoff to its Coder with issues to fix; a Lead calls this; returns `next`."""
    ...


@_tool()
def attempt_record(
    caller: CallerName, file_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Classifies a file's latest attempt as improved, plateau, or regression; a Lead calls it.
    When a plateau or regression moves an issue to round 2 or 3, `escalated` lists each
    escalation with its `escalated_to` agent and the wake-up call to make as its `next`."""
    ...


@_tool()
def accept_incomplete(
    caller: CallerName, handoff_id: int, reason: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Accepts a handoff as incomplete and opens a file deferral for the Manager; a Lead calls
    this after review_compare, for work the Coder or an open issue reports as incomplete."""
    ...


@_tool()
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
    ...


@_tool()
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
    ...


@_tool()
def deferral_propose(
    caller: CallerName,
    body: str,
    kind: Literal["file", "module", "cross_module", "phase", "plan", "prd"],
    file_id: int | None = None,
    parties: list[str] | None = _DEFERRAL_PROPOSE_PARTIES,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Proposes a deferral or scope change, or with parties a dispute; any registered agent
    calls this. kind sets who decides: file or module, the Lead; cross_module or phase, the
    Manager; plan or prd, the Oracle (prd with the user)."""
    ...


@_tool()
def agreement_decide(
    caller: CallerName,
    deferral_id: int,
    decision: Literal["agreed", "denied"],
    reason: str,
    directive_id: int | None = _AGREEMENT_DECIDE_DIRECTIVE_ID,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Decides an open deferral as agreed or denied: a dispute by its arbiter, any other by a
    caller at or above both the proposer's parent and the kind's level."""
    ...


@_tool()
def cr_open(
    caller: CallerName, path: str, body: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Opens a change request on a path the caller does not own; any role calls this. The
    ledger routes it to the file's owner, or up the chain when the owner has ended."""
    ...


@_tool()
def cr_accept(
    caller: CallerName,
    cr_id: int,
    accept: bool,
    reason: str,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Accepts or declines an open change request; only its recipient calls this. A decline
    needs a non-empty reason."""
    ...


@_tool()
def cr_complete(
    caller: CallerName, cr_id: int, notes: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Completes an accepted change request with evidence; only its recipient calls this. It
    refuses without a passing test run or an approved handoff for the path since acceptance."""
    ...


@_tool()
def cr_verify(
    caller: CallerName, cr_id: int, ok: bool, notes: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Verifies a completed change request; only the requester or its nearest live ancestor
    calls this. A failed verification goes back to accepted and owes the recipient a wake-up."""
    ...


@_tool(read_only=True)
def cr_list(
    caller: CallerName, state: str | None = None, agent_id: str | None = _STAMPED_AGENT_ID
) -> list[dict[str, Any]]:
    """Lists the change requests the caller sent or received; the Oracle sees every one."""
    ...


@_tool()
def departure_record(
    caller: CallerName,
    body: str,
    file_id: int | None = None,
    guideline_id: int | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records a departure from the guidelines; a Coder for its own file, or a Lead, Manager,
    or Oracle for work in its scope."""
    ...


@_tool()
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
    ...


@_tool()
def shortfall_record(
    caller: CallerName,
    body: str,
    file_id: int | None = None,
    agent_id: str | None = _STAMPED_AGENT_ID,
) -> dict[str, Any]:
    """Records a shortfall: a solution that works but that nobody found better; any role
    calls this, and it needs no decision."""
    ...


@_tool()
def version_restore(
    caller: CallerName, version_id: int, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Restores a saved version onto the coder's own owned file; a Coder calls this."""
    ...


@_tool(read_only=True)
def status_tree(
    caller: CallerName, include_prd: bool = False, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Returns the run's phase, module, and file status tree with live agents; any agent may.
    `open_findings` lists the Driver findings not yet fixed or stopped, and `fixes` lists each
    brief that names findings, with their ids and titles. The run's PRD text is left out
    unless include_prd is true."""
    ...


@_tool()
def report_build(caller: CallerName, agent_id: str | None = _STAMPED_AGENT_ID) -> dict[str, Any]:
    """Builds and writes the run's report.md from its current state; the Oracle calls this."""
    ...


@_tool(read_only=True)
def analytics_query(
    caller: CallerName, sql: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Runs a single read-only SELECT against the ledger database; the Oracle calls it."""
    ...


@_tool()
def drive_request(
    caller: CallerName, focus: str, agent_id: str | None = _STAMPED_AGENT_ID
) -> dict[str, Any]:
    """Requests an exploration and starts its Driver session; the Oracle calls this once the
    previous exploration and every fix it spawned have finished. Refuses when the host has no
    Driver available (cartographer and a driver plugin), while an exploration is still open,
    or while a Manager, Lead, or Coder is still live. It releases an earlier Driver whose
    exploration ended but whose session is still live. The result's `loop_status` reports the
    stop-rule state computed from every exploration and finding so far."""
    ...


@_tool()
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
    ...


@_tool()
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
    ...


@_tool()
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
    ...


@_tool()
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
    ...


@_tool(read_only=True, forward=False)
def ledger_info() -> dict[str, Any]:
    """Returns the server's name, version, status, tool count, and repo root."""
    return front.ledger_info(_call(REPO_ROOT_METHOD), len(_TOOL_NAMES))
