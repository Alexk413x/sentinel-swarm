---
name: plan
description: The Oracle's conventions for turning a PRD into a sentinel-swarm phase graph. Use for "plan this PRD with the swarm", "show me the phase breakdown", or "what would sentinel-swarm build first".
---

# plan

How the Oracle shapes a phase graph. This skill is for the Oracle; no other role
plans phases, and `phase_add` and `phase_update` refuse any other caller.

## The shape of a run

A plan is a dependency graph of phases, not an ordered list. A phase unlocks when
every phase it depends on is approved. Phases with no dependency between them run at
the same time.

- **Foundation.** Setup, shared types, and shared helpers come first, because
  everything else is blocked on them. Keep this phase small: the run cannot widen
  until it is approved.
- **Scale out.** The approved foundation unlocks the parallel work, for example
  front end and back end, or one phase per service. This is where the run is widest.
- **Scale down.** Integration phases bring the parallel work together. They need
  fewer agents, so the run narrows as it closes.

The same ordering applies one layer down: a Manager orders its modules and a Lead
orders its files, so helpers come before the files that use them.

## One Manager per phase

A phase is a chunk of work with exactly one Manager. Size a phase so that its
Manager has real work to break down but can still hold the whole phase in view: a
handful of modules, not one file and not a whole product.

Draw the module boundaries so that two Managers rarely need the same file. Where a
file is genuinely shared, the two Managers agree one owner before either Lead claims
it. A claim is exclusive, so the second claim is refused.

## Join points

A join point is a phase that several phases feed, such as an integration phase. At
each join point the Oracle runs `tests_run(scope="full")`, once every Manager that
feeds it has reported. Put the join points in the plan deliberately: they are where
a contract mismatch between parallel phases surfaces.

## Contracts before implementations

When one phase or module produces something another consumes, fix the contract in
the brief of the phase that produces it, and state the same contract in the brief of
the phase that consumes it. A Coder whose file depends on a helper writes its tests
against that contract with test doubles instead of waiting. "Blocked" then means the
contract is missing or wrong, not that the helper is unfinished.

## Recording the plan

1. `phase_add(name, depends_on=[...])` once per phase, in dependency order, so each
   phase can name the ids it depends on. Name a phase so that it reads in an agent
   name: `p1-foundation` gives `mgr-p1-foundation`.
2. `phase_update(phase_id, state="unlocked")` for every phase with no dependency.
3. `plan_unlocked()` after each approval lists the phases whose dependencies are now
   met.

## The plan changes

The plan is not fixed at the start. Validated findings from the lower layers add work
now or schedule it for a later phase. A deferral that reaches the Oracle is decided
with `agreement_decide(deferral_id, decision, reason)`, and a directive that changes
the plan is applied and then resolved with `directive_resolve`. Work that is already
approved is not reopened unless the directive says so.
