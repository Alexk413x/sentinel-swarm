# Notes: prompt-cache costs for swarm runs

Status: notes to discuss during sentinel-swarm testing. No design decision depends on them yet.

Source: Alex's `projects-2d` session, received 2026-09-20. Prices come from the pricing reference bundled with Claude Code (claude-api skill, cached 2026-06-24). These are API prices. A subscription does not bill them directly, and nobody has verified how plan limits weigh cache writes against reads.

## Multipliers

Relative to the model's normal input price:

| Event | Multiplier |
|---|---|
| Cache read (warm turn) | 0.1x on Opus 5 and Sonnet 5, 0.025x on Fable 5.1 |
| Cache write, 5-minute lifetime | 1.25x |
| Cache write, 1-hour lifetime | 2x |

A cold turn rewrites the whole context at the write price. A warm turn reads the whole context at the read price.

## Prices per million tokens

| Model | Input | Warm read | Cold 5m write | Cold 1h write | Output |
|---|---|---|---|---|---|
| Fable 5.1 | $10.00 | $0.25 | $12.50 | $20.00 | $50 |
| Opus 5 | $5.00 | $0.50 | $6.25 | $10.00 | $25 |
| Sonnet 5 | $2.00 | $0.20 | $2.50 | $4.00 | $10 |

## Worked example: one agent with 50,000 tokens of context

| Model | Warm turn | Cold restart (5m) | Cold restart (1h) | 1-hour premium per agent |
|---|---|---|---|---|
| Sonnet 5 | ~$0.01 | ~$0.13 | ~$0.20 | ~$0.08 |
| Opus 5 | ~$0.03 | ~$0.31 | ~$0.50 | ~$0.19 |

- Costs scale linearly with context. An Opus agent at 150,000 tokens pays ~$0.94 per cold restart.
- A fork of a large Fable 5.1 session (80k+ tokens) pays ~$1 or more per cold restart.

## Which lifetime applies

From the Claude Code prompt-caching docs, through a research agent. Not verified, except where noted.

- Subscription plan, main conversation: 1 hour. Verified: the status line payload reported ttl "1h".
- Subagents, workflows, and compaction requests: 5 minutes by default, even on a subscription.
- API key, usage credits, or cloud provider: 5 minutes for everything.
- Reported overrides (unverified): settings `promptCacheTtl` and `subagentPromptCacheTtl`; environment variables `CLAUDE_CODE_PROMPT_CACHE_TTL`, `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`, `ENABLE_PROMPT_CACHING_1H=1`, `FORCE_PROMPT_CACHING_5M=1`.

## How the timer behaves

API behavior per the bundled reference:

- The timer restarts at the start of each API request that reads or writes the cache. Every agent turn, including each tool-call turn, resets it.
- The timer counts down whenever no request is starting: while a long tool call runs, while a long response generates, and while the agent is idle or finished.
- An agent that makes many short tool calls stays warm indefinitely on the 5-minute cache. An agent that blocks on one 6-minute build or test run goes cold, even though it looks busy.
- The cache is a prefix match, per model. Switching models, adding or removing tools or MCP servers mid-session, and `/compact` all force a rebuild. Same-type agents spawned together share only their system prompt and tool list prefix.

## Guidance for agents that wait more than 5 minutes

- Cheapest option: poll. Run the long command in the background and check on it about every 4 minutes. Each check is a normal turn, resets the timer, and costs only a cache read.
- Break-even on Opus 5 and Sonnet 5: one cold restart costs the same as ~12 checks, so polling wins for waits up to ~50 minutes. Beyond that, let the agent go cold and pay one restart.
- Break-even on Fable 5.1: one cold restart equals ~50 checks (~3 hours).
- The real break-even is slightly lower, because each check adds a little text to the context.
- A 1-hour lifetime for subagents pays off only if most agents routinely wait 5 to 60 minutes. Every agent pays the 2x write price whether it waits or not.
- Claude Code has no known keep-alive setting. It does not expose the API's `max_tokens: 0` re-send technique.
- A follow-up to an existing agent reuses its cache only if the follow-up lands inside its lifetime (5 minutes by default). After that, the agent's whole history is reprocessed.

## Measure during swarm testing

- Per-agent context size at finish. It drives every number above.
- How many agents block on a single command for longer than 5 minutes.
- Cache hit ratio and miss count. The status line payload exposes a `prompt_cache` object for the main conversation only (`warm`, `ttl`, `expires_at`, `hit_ratio`, `misses`, `last_miss_cause`, `recache_tokens_if_cold`). It does not report subagent caches.

## Design questions these notes raise

- Should long test and build runs go through a background-and-poll pattern in the agent prompts, so agents stay warm?
- Which model runs each role? Context size and restart frequency per role decide the cost.
- Does the ledger record per-agent context size and blocking time, so a run can report its own cost profile?
