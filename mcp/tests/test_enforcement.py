from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_ledger import graph as graph_module
from swarm_ledger import sessions
from swarm_ledger.db import write_tx
from swarm_ledger.hooks import events
from swarm_ledger.identity import LedgerError
from swarm_ledger.ledger import Ledger


@pytest.fixture(autouse=True)
def no_claude_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], cwd: Path | None = None) -> str:
        del cwd
        assert args[:2] == ["agents", "--json"], args
        return "[]"

    monkeypatch.setattr(sessions, "_run", fake_run)


@pytest.fixture
def fake_repo(tmp_path: Path, repo_root: Path) -> Path:
    host = tmp_path / "host"
    (host / ".git").mkdir(parents=True)
    claude_dir = host / ".claude"
    claude_dir.mkdir()
    template = (repo_root / "templates" / "sentinel-swarm.local.md.example").read_text(
        encoding="utf-8"
    )
    text = template.replace("test_command:\n", "test_command: pytest -q {target}\n")
    (claude_dir / "sentinel-swarm.local.md").write_text(text, encoding="utf-8")
    return host


@pytest.fixture
def ledger(fake_repo: Path) -> Ledger:
    return Ledger(fake_repo, db_path=fake_repo / ".sentinel-swarm" / "ledger.db")


def _oracle(ledger: Ledger) -> dict:
    started = ledger.run_start(prd="Build X", session_id="oracle-sess")
    return {"run_id": started["run"]["run_id"], "oracle_id": started["oracle"]["agent_id"]}


def _manager(ledger: Ledger, ctx: dict, phase_name: str, agent_id: str) -> dict:
    phase = ledger.phase_add("oracle", ctx["oracle_id"], phase_name)
    ledger.phase_update("oracle", ctx["oracle_id"], phase["phase_id"], "unlocked")
    name = f"mgr-{phase['name']}"
    ledger.brief_create(
        "oracle", ctx["oracle_id"], name, "manager", "opus", "Own it.", phase_id=phase["phase_id"]
    )
    ledger.agent_register_start(agent_id, "manager", parent_agent_id=ctx["oracle_id"])
    ledger.brief_ack(name, agent_id)
    return {"name": name, "agent_id": agent_id, "phase": phase}


def _lead(ledger: Ledger, manager: dict, module_name: str, agent_id: str) -> dict:
    phase = manager["phase"]
    module = ledger.module_add(manager["name"], manager["agent_id"], phase["phase_id"], module_name)
    name = f"lead-p{phase['ordinal']}-{module_name}"
    ledger.brief_create(
        manager["name"],
        manager["agent_id"],
        name,
        "lead",
        "sonnet",
        "Own it.",
        module_id=module["module_id"],
    )
    ledger.agent_register_start(agent_id, "lead", parent_agent_id=manager["agent_id"])
    ledger.brief_ack(name, agent_id)
    return {"name": name, "agent_id": agent_id, "module": module, "phase": phase}


def _coder(
    ledger: Ledger,
    lead: dict,
    slug: str,
    path: str,
    *,
    depends_on: list[int] | None = None,
    contract: str | None = None,
    bind: bool = True,
) -> dict:
    name = f"coder-p{lead['phase']['ordinal']}-{lead['module']['name']}-{slug}"
    claimed = ledger.claim_file(lead["name"], lead["agent_id"], path, None, name, depends_on)
    ledger.brief_create(
        lead["name"],
        lead["agent_id"],
        name,
        "coder",
        "sonnet",
        "Write it.",
        file_id=claimed["file_id"],
        contract=contract,
    )
    agent_id = f"{name}-agent"
    if bind:
        ledger.agent_register_start(agent_id, "coder", parent_agent_id=lead["agent_id"])
        ledger.brief_ack(name, agent_id)
    return {"name": name, "agent_id": agent_id, "file_id": claimed["file_id"], "path": path}


@pytest.fixture
def tree(ledger: Ledger) -> dict:
    ctx = _oracle(ledger)
    manager = _manager(ledger, ctx, "core", "mgr-core-agent")
    lead = _lead(ledger, manager, "auth", "lead-auth-agent")
    return {**ctx, "manager": manager, "lead": lead}


# -- 7. Names -------------------------------------------------------------------------


def test_phase_add_prefixes_the_ordinal(ledger: Ledger) -> None:
    ctx = _oracle(ledger)
    first = ledger.phase_add("oracle", ctx["oracle_id"], "core")
    second = ledger.phase_add("oracle", ctx["oracle_id"], "api")
    kept = ledger.phase_add("oracle", ctx["oracle_id"], "p3-ui")
    assert (first["name"], second["name"], kept["name"]) == ("p1-core", "p2-api", "p3-ui")


def test_phase_add_and_module_add_refuse_a_name_that_is_not_a_slug(tree: dict, ledger: Ledger):
    with pytest.raises(LedgerError, match="not a slug"):
        ledger.phase_add("oracle", tree["oracle_id"], "Big Phase")
    manager = tree["manager"]
    with pytest.raises(LedgerError, match="not a slug"):
        ledger.module_add(manager["name"], manager["agent_id"], manager["phase"]["phase_id"], "A_b")


def test_brief_create_refuses_a_manager_name_off_the_pattern(ledger: Ledger) -> None:
    ctx = _oracle(ledger)
    phase = ledger.phase_add("oracle", ctx["oracle_id"], "core")
    ledger.phase_update("oracle", ctx["oracle_id"], phase["phase_id"], "unlocked")
    with pytest.raises(LedgerError, match="'mgr-p1-core'"):
        ledger.brief_create(
            "oracle", ctx["oracle_id"], "mgr-core", "manager", "opus", "x", phase_id=1
        )


def test_brief_create_refuses_a_lead_name_off_the_pattern(tree: dict, ledger: Ledger) -> None:
    manager = tree["manager"]
    module = ledger.module_add(
        manager["name"], manager["agent_id"], manager["phase"]["phase_id"], "billing"
    )
    with pytest.raises(LedgerError, match="'lead-p1-billing'"):
        ledger.brief_create(
            manager["name"],
            manager["agent_id"],
            "lead-billing",
            "lead",
            "sonnet",
            "x",
            module_id=module["module_id"],
        )


def test_claim_file_refuses_a_coder_name_off_the_pattern(tree: dict, ledger: Ledger) -> None:
    lead = tree["lead"]
    with pytest.raises(LedgerError, match="coder-p1-auth-<file slug>"):
        ledger.claim_file(lead["name"], lead["agent_id"], "src/login.py", None, "coder-login")
    with pytest.raises(LedgerError, match="coder-p1-auth-<file slug>"):
        ledger.claim_file(lead["name"], lead["agent_id"], "src/login.py", None, "coder-p1-auth-")


def test_the_pattern_names_pass(tree: dict, ledger: Ledger) -> None:
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    assert coder["name"] == "coder-p1-auth-login"
    assert tree["manager"]["name"] == "mgr-p1-core"
    assert tree["lead"]["name"] == "lead-p1-auth"


# -- 4. Brief scope ---------------------------------------------------------------------


def test_a_manager_brief_naming_another_phases_module_is_refused(tree: dict, ledger: Ledger):
    other = _manager(ledger, tree, "api", "mgr-api-agent")
    module = ledger.module_add(other["name"], other["agent_id"], other["phase"]["phase_id"], "rest")
    manager = tree["manager"]
    with pytest.raises(LedgerError, match="module of your own phase"):
        ledger.brief_create(
            manager["name"],
            manager["agent_id"],
            "lead-p2-rest",
            "lead",
            "sonnet",
            "x",
            module_id=module["module_id"],
        )


def test_a_lead_brief_for_another_modules_file_is_refused(tree: dict, ledger: Ledger) -> None:
    other = _lead(ledger, tree["manager"], "billing", "lead-billing-agent")
    coder = _coder(ledger, other, "pay", "src/pay.py", bind=False)
    lead = tree["lead"]
    with pytest.raises(LedgerError, match="a file your module claimed"):
        ledger.brief_create(
            lead["name"],
            lead["agent_id"],
            "coder-p1-auth-pay",
            "coder",
            "sonnet",
            "x",
            file_id=coder["file_id"],
        )


def test_a_lead_brief_for_a_file_claimed_for_another_name_is_refused(tree: dict, ledger: Ledger):
    lead = tree["lead"]
    claimed = ledger.claim_file(
        lead["name"], lead["agent_id"], "src/login.py", None, "coder-p1-auth-login"
    )
    with pytest.raises(LedgerError, match="no live claim for 'coder-p1-auth-other'"):
        ledger.brief_create(
            lead["name"],
            lead["agent_id"],
            "coder-p1-auth-other",
            "coder",
            "sonnet",
            "x",
            file_id=claimed["file_id"],
        )


def test_a_coder_brief_without_a_file_id_is_refused(tree: dict, ledger: Ledger) -> None:
    lead = tree["lead"]
    with pytest.raises(LedgerError, match="needs the file_id"):
        ledger.brief_create(
            lead["name"], lead["agent_id"], "coder-p1-auth-x", "coder", "sonnet", "x"
        )


# -- 9. Phase unlock ----------------------------------------------------------------------


def test_unlocking_a_phase_with_an_unapproved_dependency_is_refused(ledger: Ledger) -> None:
    ctx = _oracle(ledger)
    first = ledger.phase_add("oracle", ctx["oracle_id"], "core")
    second = ledger.phase_add("oracle", ctx["oracle_id"], "api", depends_on=[first["phase_id"]])
    with pytest.raises(LedgerError, match=r"not approved: 1 \(p1-core, planned\)"):
        ledger.phase_update("oracle", ctx["oracle_id"], second["phase_id"], "unlocked")


def test_briefing_or_spawning_a_manager_into_a_planned_phase_is_refused(ledger: Ledger) -> None:
    ctx = _oracle(ledger)
    phase = ledger.phase_add("oracle", ctx["oracle_id"], "core")
    with pytest.raises(LedgerError, match="still planned"):
        ledger.brief_create(
            "oracle",
            ctx["oracle_id"],
            "mgr-p1-core",
            "manager",
            "opus",
            "x",
            phase_id=phase["phase_id"],
        )
    ledger.phase_update("oracle", ctx["oracle_id"], phase["phase_id"], "unlocked")
    ledger.brief_create(
        "oracle", ctx["oracle_id"], "mgr-p1-core", "manager", "opus", "x", phase_id=1
    )
    with write_tx(ledger.conn) as conn:
        conn.execute("UPDATE phases SET state = 'planned' WHERE phase_id = 1")
    with pytest.raises(LedgerError, match="is not unlocked"):
        ledger.agent_spawn("oracle", ctx["oracle_id"], "mgr-p1-core")


# -- 1. Dependencies and contracts ---------------------------------------------------------


def test_claim_file_refuses_a_dependency_from_another_module(tree: dict, ledger: Ledger) -> None:
    other = _lead(ledger, tree["manager"], "billing", "lead-billing-agent")
    helper = _coder(ledger, other, "money", "src/money.py", bind=False)
    lead = tree["lead"]
    with pytest.raises(LedgerError, match="not claimed files of your module"):
        ledger.claim_file(
            lead["name"],
            lead["agent_id"],
            "src/login.py",
            None,
            "coder-p1-auth-login",
            [helper["file_id"]],
        )


def test_module_add_refuses_a_dependency_outside_the_phase(tree: dict, ledger: Ledger) -> None:
    other = _manager(ledger, tree, "api", "mgr-api-agent")
    foreign = ledger.module_add(
        other["name"], other["agent_id"], other["phase"]["phase_id"], "rest"
    )
    manager = tree["manager"]
    with pytest.raises(LedgerError, match="outside phase 1"):
        ledger.module_add(
            manager["name"],
            manager["agent_id"],
            manager["phase"]["phase_id"],
            "billing",
            depends_on=[foreign["module_id"]],
        )


def test_a_dependent_coder_brief_waits_on_the_helpers_contract(tree: dict, ledger: Ledger):
    lead = tree["lead"]
    helper = ledger.claim_file(
        lead["name"], lead["agent_id"], "src/token.py", None, "coder-p1-auth-token"
    )
    claimed = ledger.claim_file(
        lead["name"],
        lead["agent_id"],
        "src/login.py",
        None,
        "coder-p1-auth-login",
        [helper["file_id"]],
    )
    with pytest.raises(LedgerError, match=r"src/token.py \(id 1\)"):
        ledger.brief_create(
            lead["name"],
            lead["agent_id"],
            "coder-p1-auth-login",
            "coder",
            "sonnet",
            "x",
            file_id=claimed["file_id"],
        )

    ledger.brief_create(
        lead["name"],
        lead["agent_id"],
        "coder-p1-auth-token",
        "coder",
        "sonnet",
        "Write it.",
        file_id=helper["file_id"],
        contract="issue(user: str) -> str",
    )
    ledger.brief_create(
        lead["name"],
        lead["agent_id"],
        "coder-p1-auth-login",
        "coder",
        "sonnet",
        "x",
        file_id=claimed["file_id"],
    )
    brief = ledger.brief_get("coder-p1-auth-login", "coder-p1-auth-login")
    assert brief["depends_on_contracts"] == [
        {"id": helper["file_id"], "name": "src/token.py", "contract": "issue(user: str) -> str"}
    ]


def test_a_dependent_lead_brief_waits_on_the_helper_modules_contract(
    tree: dict, ledger: Ledger
) -> None:
    manager = tree["manager"]
    phase_id = manager["phase"]["phase_id"]
    helper = ledger.module_add(manager["name"], manager["agent_id"], phase_id, "store")
    user = ledger.module_add(
        manager["name"], manager["agent_id"], phase_id, "users", [helper["module_id"]]
    )
    with pytest.raises(LedgerError, match=r"store \(id"):
        ledger.brief_create(
            manager["name"],
            manager["agent_id"],
            "lead-p1-users",
            "lead",
            "sonnet",
            "x",
            module_id=user["module_id"],
        )
    ledger.brief_create(
        manager["name"],
        manager["agent_id"],
        "lead-p1-store",
        "lead",
        "sonnet",
        "x",
        module_id=helper["module_id"],
        contract="Store.get(key) -> bytes",
    )
    ledger.brief_create(
        manager["name"],
        manager["agent_id"],
        "lead-p1-users",
        "lead",
        "sonnet",
        "x",
        module_id=user["module_id"],
    )
    brief = ledger.brief_get("lead-p1-users", "lead-p1-users")
    assert brief["depends_on_contracts"][0]["contract"] == "Store.get(key) -> bytes"


def _insert_full_run(ledger: Ledger, ctx: dict) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO test_runs (run_id, agent_id, scope, target, command, exit_code, "
            "passed, failed, skipped, output) VALUES (?, ?, 'full', 'x', 'x', 0, 1, 0, 0, '')",
            (ctx["run_id"], ctx["oracle_id"]),
        )


def _scores() -> list[dict]:
    return [
        {"dimension": "completeness", "value": 10},
        {"dimension": "integration", "value": 10},
        {"dimension": "open_items", "value": 10},
    ]


def _approve_empty_phase(ledger: Ledger, ctx: dict, manager: dict) -> None:
    phase_id = manager["phase"]["phase_id"]
    ledger.phase_update(manager["name"], manager["agent_id"], phase_id, "handed_up")
    _insert_full_run(ledger, ctx)
    ledger.phase_review("oracle", ctx["oracle_id"], phase_id, "accepted", "ok", scores=_scores())
    ledger.phase_update("oracle", ctx["oracle_id"], phase_id, "approved")


def test_a_join_points_newest_full_run_follows_every_feeding_hand_up(ledger: Ledger) -> None:
    ctx = _oracle(ledger)
    first = _manager(ledger, ctx, "core", "mgr-core-agent")
    second = _manager(ledger, ctx, "api", "mgr-api-agent")
    join = ledger.phase_add(
        "oracle",
        ctx["oracle_id"],
        "ui",
        depends_on=[first["phase"]["phase_id"], second["phase"]["phase_id"]],
    )
    _approve_empty_phase(ledger, ctx, second)
    with pytest.raises(LedgerError, match="not approved"):
        ledger.phase_update("oracle", ctx["oracle_id"], join["phase_id"], "unlocked")
    _approve_empty_phase(ledger, ctx, first)
    ledger.phase_update("oracle", ctx["oracle_id"], join["phase_id"], "unlocked")

    hand_ups = [
        row["handed_up_at"]
        for row in ledger.conn.execute("SELECT handed_up_at FROM phases WHERE phase_id IN (1, 2)")
    ]
    newest_full = ledger.conn.execute(
        "SELECT MAX(created_at) AS at FROM test_runs WHERE scope = 'full'"
    ).fetchone()["at"]
    assert all(newest_full >= at for at in hand_ups)


# -- 5. Messages through the chain -----------------------------------------------------------


def test_a_managers_post_to_a_coder_is_refused_and_lists_its_peers(tree: dict, ledger: Ledger):
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    manager = tree["manager"]
    with pytest.raises(LedgerError, match=r"\['lead-p1-auth', 'oracle'\]"):
        ledger.message_post(manager["name"], manager["agent_id"], coder["name"], "do it")


def test_the_oracles_post_to_a_lead_is_refused(tree: dict, ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="parent, its children, and its siblings"):
        ledger.message_post("oracle", tree["oracle_id"], tree["lead"]["name"], "do it")


def test_a_leads_post_to_a_sibling_lead_passes(tree: dict, ledger: Ledger) -> None:
    other = _lead(ledger, tree["manager"], "billing", "lead-billing-agent")
    lead = tree["lead"]
    posted = ledger.message_post(lead["name"], lead["agent_id"], other["name"], "contract?")
    assert posted["to_name"] == "lead-p1-billing"


def _session(ledger: Ledger, agent_id: str, name: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = ? WHERE agent_id = ?", (f"host-r1-{name}", agent_id)
        )


def test_a_managers_send_message_to_a_coder_needs_an_owed_wake_up(tree: dict, ledger: Ledger):
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    manager = tree["manager"]
    for agent in (manager, tree["lead"], coder):
        _session(ledger, agent["agent_id"], agent["name"])
    data = {
        "session_id": manager["agent_id"],
        "tool_input": {"to": f"host-r1-{coder['name']}", "message": "x"},
    }
    denied = events.handle_pre_send_message(ledger, data)
    assert denied is not None
    assert "host-r1-lead-p1-auth" in denied["hookSpecificOutput"]["permissionDecisionReason"]

    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO wakeups (run_id, from_agent_id, to_agent_id, to_name, to_session_name, "
            "reason, pointer) VALUES (1, ?, ?, ?, ?, 'cr_open', 'x')",
            (manager["agent_id"], coder["agent_id"], coder["name"], f"host-r1-{coder['name']}"),
        )
    assert events.handle_pre_send_message(ledger, data) is None


# -- 2. accept_incomplete -------------------------------------------------------------------


def _handoff(ledger: Ledger, coder: dict, open_issues: list[str], compared: bool = True) -> int:
    with write_tx(ledger.conn) as conn:
        cur = conn.execute(
            "INSERT INTO handoffs (file_id, agent_id, open_issues_json, departures_json, state, "
            "compared_at) VALUES (?, ?, ?, '[]', 'submitted', ?)",
            (
                coder["file_id"],
                coder["agent_id"],
                json.dumps(open_issues),
                "2026-01-01T00:00:00.000Z" if compared else None,
            ),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_accept_incomplete_refuses_an_empty_reason(tree: dict, ledger: Ledger) -> None:
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    handoff_id = _handoff(ledger, coder, ["the token store is missing"])
    lead = tree["lead"]
    with pytest.raises(LedgerError, match="needs a reason"):
        ledger.accept_incomplete(lead["name"], lead["agent_id"], handoff_id, "  ")


def test_accept_incomplete_refuses_before_review_compare(tree: dict, ledger: Ledger) -> None:
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    handoff_id = _handoff(ledger, coder, ["the token store is missing"], compared=False)
    lead = tree["lead"]
    with pytest.raises(LedgerError, match="review_compare has not run"):
        ledger.accept_incomplete(lead["name"], lead["agent_id"], handoff_id, "blocked")


def test_accept_incomplete_refuses_work_nobody_reported_incomplete(tree: dict, ledger: Ledger):
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    handoff_id = _handoff(ledger, coder, [])
    lead = tree["lead"]
    with pytest.raises(LedgerError, match="nobody reported this work as incomplete"):
        ledger.accept_incomplete(lead["name"], lead["agent_id"], handoff_id, "blocked")


def test_accept_incomplete_records_the_issues_on_the_deferral(tree: dict, ledger: Ledger) -> None:
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    handoff_id = _handoff(ledger, coder, ["the token store is missing"])
    lead = tree["lead"]
    issue = ledger.issue_open(lead["name"], lead["agent_id"], coder["file_id"], "slow", "x")
    deferral = ledger.accept_incomplete(lead["name"], lead["agent_id"], handoff_id, "blocked")
    assert deferral["kind"] == "file"
    assert json.loads(deferral["open_issues_json"]) == ["the token store is missing"]
    assert json.loads(deferral["issue_ids_json"]) == [issue["issue_id"]]
    assert deferral["proposed_by"] == lead["agent_id"]


# -- 8. The responsible level ------------------------------------------------------------------


def test_deferral_propose_refuses_an_unknown_kind(tree: dict, ledger: Ledger) -> None:
    lead = tree["lead"]
    with pytest.raises(LedgerError, match="unknown deferral kind"):
        ledger.deferral_propose(lead["name"], lead["agent_id"], "later", kind="someday")


def test_a_lead_may_not_decide_a_cross_module_deferral(tree: dict, ledger: Ledger) -> None:
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    deferral = ledger.deferral_propose(
        coder["name"], coder["agent_id"], "move it", coder["file_id"], kind="cross_module"
    )
    lead = tree["lead"]
    with pytest.raises(LedgerError, match="decided by the manager or above"):
        ledger.agreement_decide(
            lead["name"], lead["agent_id"], deferral["deferral_id"], "agreed", "ok"
        )
    manager = tree["manager"]
    decided = ledger.agreement_decide(
        manager["name"], manager["agent_id"], deferral["deferral_id"], "agreed", "ok"
    )
    assert decided["state"] == "agreed"


def test_a_manager_may_not_decide_a_plan_deferral(tree: dict, ledger: Ledger) -> None:
    lead = tree["lead"]
    deferral = ledger.deferral_propose(lead["name"], lead["agent_id"], "later", kind="plan")
    manager = tree["manager"]
    with pytest.raises(LedgerError, match="decided by the oracle or above"):
        ledger.agreement_decide(
            manager["name"], manager["agent_id"], deferral["deferral_id"], "agreed", "ok"
        )


def test_a_prd_decision_needs_a_later_user_chat_directive(tree: dict, ledger: Ledger) -> None:
    early = ledger.directive_submit("user_chat", "user", "earlier")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE directives SET created_at = '2000-01-01T00:00:00.000Z' WHERE directive_id = ?",
            (early["directive_id"],),
        )
    manager = tree["manager"]
    deferral = ledger.deferral_propose(
        manager["name"], manager["agent_id"], "drop export", kind="prd"
    )
    with pytest.raises(LedgerError, match="which the user decides"):
        ledger.agreement_decide(
            "oracle", tree["oracle_id"], deferral["deferral_id"], "agreed", "ok"
        )
    with pytest.raises(LedgerError, match="which the user decides"):
        ledger.agreement_decide(
            "oracle",
            tree["oracle_id"],
            deferral["deferral_id"],
            "agreed",
            "ok",
            directive_id=early["directive_id"],
        )
    answer = ledger.directive_submit("user_chat", "user", "yes, drop it")
    decided = ledger.agreement_decide(
        "oracle",
        tree["oracle_id"],
        deferral["deferral_id"],
        "agreed",
        "the user agreed",
        directive_id=answer["directive_id"],
    )
    assert decided["directive_id"] == answer["directive_id"]


# -- 6. Disputes ---------------------------------------------------------------------------


def test_a_dispute_between_two_coders_goes_to_their_lead(tree: dict, ledger: Ledger) -> None:
    lead = tree["lead"]
    one = _coder(ledger, lead, "login", "src/login.py")
    two = _coder(ledger, lead, "token", "src/token.py")
    for agent in (lead, one, two):
        _session(ledger, agent["agent_id"], agent["name"])
    dispute = ledger.deferral_propose(
        one["name"], one["agent_id"], "who owns hash()?", kind="module", parties=[two["name"]]
    )
    assert dispute["arbiter"] == lead["name"]
    owed = ledger.owed_wakeups(one["agent_id"])
    assert [w["to_name"] for w in owed] == [lead["name"]]
    assert dispute["next"] is not None

    manager = tree["manager"]
    with pytest.raises(LedgerError, match="only their closest shared ancestor"):
        ledger.agreement_decide(
            manager["name"], manager["agent_id"], dispute["deferral_id"], "agreed", "x"
        )
    decided = ledger.agreement_decide(
        lead["name"], lead["agent_id"], dispute["deferral_id"], "agreed", "token owns it"
    )
    assert decided["state"] == "agreed"


def test_a_dispute_between_two_managers_leads_reaches_only_the_oracle(
    tree: dict, ledger: Ledger
) -> None:
    other_manager = _manager(ledger, tree, "api", "mgr-api-agent")
    other_lead = _lead(ledger, other_manager, "rest", "lead-rest-agent")
    lead = tree["lead"]
    dispute = ledger.deferral_propose(
        lead["name"], lead["agent_id"], "shared file", kind="phase", parties=[other_lead["name"]]
    )
    assert dispute["arbiter"] == "oracle"
    manager = tree["manager"]
    with pytest.raises(LedgerError, match="only their closest shared ancestor, oracle"):
        ledger.agreement_decide(
            manager["name"], manager["agent_id"], dispute["deferral_id"], "agreed", "x"
        )
    decided = ledger.agreement_decide(
        "oracle", tree["oracle_id"], dispute["deferral_id"], "agreed", "api owns it"
    )
    assert decided["state"] == "agreed"


def test_the_report_lists_disputes(tree: dict, ledger: Ledger) -> None:
    other = _lead(ledger, tree["manager"], "billing", "lead-billing-agent")
    lead = tree["lead"]
    ledger.deferral_propose(
        lead["name"], lead["agent_id"], "who owns money.py", kind="module", parties=[other["name"]]
    )
    text = ledger.write_report(tree["run_id"])["text"]
    assert "## Disputes" in text
    assert "between lead-p1-auth and lead-p1-billing, arbiter mgr-p1-core" in text


# -- 3. The Driver's build and map-test order --------------------------------------------------


def test_profile_set_refuses_a_server_or_watcher_build_command(ledger: Ledger) -> None:
    ctx = _oracle(ledger)
    for command in ("npm run dev", "npm start", "yarn dev", "pnpm dev", "ng serve", "tsc --watch"):
        with pytest.raises(LedgerError, match="starts a server or a watcher"):
            ledger.profile_set("oracle", ctx["oracle_id"], build_command=command)
    for command in ("npm run build", "gradlew assembleDebug", "./gradlew assembleDebug"):
        assert (
            ledger.profile_set("oracle", ctx["oracle_id"], build_command=command)["build_command"]
            == command
        )


def _driver(ledger: Ledger, ctx: dict) -> str:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, name, role, parent_agent_id, run_id, state) "
            "VALUES ('driver-sess', 'driver-e1', 'driver', ?, ?, 'working')",
            (ctx["oracle_id"], ctx["run_id"]),
        )
        conn.execute(
            "INSERT INTO drive_requests (run_id, ordinal, opened_by, focus, agent_id) "
            "VALUES (?, 1, ?, 'everything', 'driver-sess')",
            (ctx["run_id"], ctx["oracle_id"]),
        )
    return "driver-sess"


def _skill(session_id: str, skill: str) -> dict:
    return {"session_id": session_id, "tool_name": "Skill", "tool_input": {"skill": skill}}


def test_map_explore_before_map_test_is_denied(ledger: Ledger) -> None:
    ctx = _oracle(ledger)
    driver = _driver(ledger, ctx)
    denied = events.handle_pre_skill(ledger, _skill(driver, "cartographer:map-explore"))
    assert denied is not None
    assert "map-test" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert events.handle_pre_skill(ledger, _skill(driver, "map-test")) is None
    assert events.handle_pre_skill(ledger, _skill(driver, "map-explore")) is None
    assert events.handle_pre_skill(ledger, _skill(driver, "some-other-skill")) is None


def test_a_non_driver_skill_call_passes(tree: dict, ledger: Ledger) -> None:
    data = _skill(tree["lead"]["agent_id"], "map-explore")
    assert events.handle_pre_skill(ledger, data) is None
    assert events.handle_pre_skill(ledger, _skill("not-a-swarm-session", "map-explore")) is None


# -- 10. Graph gaps ----------------------------------------------------------------------------


def test_a_coders_grep_records_one_graph_gap(tree: dict, ledger: Ledger, fake_repo: Path):
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    data = {
        "session_id": coder["agent_id"],
        "tool_name": "Grep",
        "tool_input": {"pattern": "def hash", "path": "src"},
        "tool_response": {
            "mode": "files_with_matches",
            "filenames": [str(fake_repo / "src" / "a.py"), str(fake_repo / "src" / "b.py")],
        },
    }
    events.handle_post_activity(ledger, data)
    rows = [dict(r) for r in ledger.conn.execute("SELECT * FROM graph_gaps")]
    assert len(rows) == 1
    assert rows[0]["agent_id"] == coder["agent_id"]
    assert (rows[0]["tool"], rows[0]["pattern"], rows[0]["path"]) == ("Grep", "def hash", "src")
    assert json.loads(rows[0]["results_json"]) == ["src/a.py", "src/b.py"]


def test_a_non_swarm_grep_records_no_gap(tree: dict, ledger: Ledger) -> None:
    data = {
        "session_id": "someone-else",
        "tool_name": "Glob",
        "tool_input": {"pattern": "**/*.py"},
        "tool_response": {"filenames": ["a.py"]},
    }
    events.handle_post_activity(ledger, data)
    assert ledger.conn.execute("SELECT COUNT(*) AS n FROM graph_gaps").fetchone()["n"] == 0


def test_the_report_lists_graph_gaps(tree: dict, ledger: Ledger) -> None:
    lead = tree["lead"]
    ledger.graph_gap(lead["agent_id"], "Glob", "**/*.py", None, ["src/a.py"])
    text = ledger.write_report(tree["run_id"])["text"]
    assert "## Graph gaps" in text
    assert "lead-p1-auth ran Glob '**/*.py': 1 path(s), src/a.py" in text


# -- 11. Brief re-read before a handoff after a return ---------------------------------------------


def test_a_handoff_after_a_return_needs_a_brief_reread(
    tree: dict, ledger: Ledger, fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph_module, "graph_current_for", lambda root, path: (True, []))
    (fake_repo / "src").mkdir()
    (fake_repo / "src" / "login.py").write_text("x = 1\n", encoding="utf-8")
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "INSERT INTO reviews (file_id, reviewer_agent_id, kind) VALUES (?, ?, 'self')",
            (coder["file_id"], coder["agent_id"]),
        )
    first = ledger.handoff_submit(coder["name"], coder["agent_id"], coder["file_id"], [], [])
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE handoffs SET state = 'returned', "
            "decided_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE handoff_id = ?",
            (first["handoff_id"],),
        )

    with pytest.raises(LedgerError, match="re-read your brief"):
        ledger.handoff_submit(coder["name"], coder["agent_id"], coder["file_id"], [], [])

    events.handle_pre_ledger(
        ledger,
        {
            "session_id": coder["agent_id"],
            "tool_name": "mcp__swarm-ledger__brief_get",
            "tool_input": {"caller_name": coder["name"], "child_name": coder["name"]},
        },
    )
    second = ledger.handoff_submit(coder["name"], coder["agent_id"], coder["file_id"], [], [])
    assert second["handoff_id"] != first["handoff_id"]


def test_another_sessions_brief_get_does_not_count_as_a_reread(tree: dict, ledger: Ledger):
    coder = _coder(ledger, tree["lead"], "login", "src/login.py")
    lead = tree["lead"]
    assert not ledger.brief_read(lead["agent_id"], coder["name"])
    assert ledger.brief_read(coder["agent_id"], coder["name"])


def _as_session(ledger: Ledger, agent_id: str, session_name: str) -> None:
    with write_tx(ledger.conn) as conn:
        conn.execute(
            "UPDATE agents SET session_name = ? WHERE agent_id = ?", (session_name, agent_id)
        )


def _bootstrap(ledger: Ledger) -> dict:
    started = ledger.run_start(prd="Build X", session_id="sess-1")
    oracle_id = started["oracle"]["agent_id"]

    phase_a = ledger.phase_add("oracle", oracle_id, "phase-a")
    phase_b = ledger.phase_add("oracle", oracle_id, "phase-b")
    ledger.phase_update("oracle", oracle_id, phase_a["phase_id"], "unlocked")
    ledger.phase_update("oracle", oracle_id, phase_b["phase_id"], "unlocked")

    ledger.brief_create(
        "oracle",
        oracle_id,
        "mgr-p1-phase-a",
        "manager",
        "opus",
        "Own phase-a.",
        phase_id=phase_a["phase_id"],
    )
    ledger.agent_register_start("mgr-agent", "manager", parent_agent_id=oracle_id)
    manager = ledger.brief_ack("mgr-p1-phase-a", "mgr-agent")
    _as_session(ledger, oracle_id, "host-r1-oracle")
    _as_session(ledger, "mgr-agent", "host-r1-manager-1")
    manager = dict(manager) | {"session_name": "host-r1-manager-1"}

    return {
        "run_id": started["run"]["run_id"],
        "oracle_id": oracle_id,
        "phase_a": phase_a["phase_id"],
        "phase_b": phase_b["phase_id"],
        "manager": manager,
    }


def test_pre_send_message_allows_a_valid_target(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    data = {
        "agent_id": ctx["oracle_id"],
        "tool_input": {"to": ctx["manager"]["session_name"]},
    }
    assert events.handle_pre_send_message(ledger, data) is None


def test_pre_send_message_denies_a_target_outside_the_run(ledger: Ledger) -> None:
    ctx = _bootstrap(ledger)
    data = {"agent_id": ctx["oracle_id"], "tool_input": {"to": "some-other-repo-r9-coder-a"}}
    result = events.handle_pre_send_message(ledger, data)
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "SendMessage may target only a session of this run" in reason
    assert ctx["manager"]["session_name"] in reason


def test_pre_send_message_ignores_a_non_swarm_caller(ledger: Ledger) -> None:
    _bootstrap(ledger)
    assert events.handle_pre_send_message(ledger, {"session_id": "not-a-swarm-agent"}) is None
