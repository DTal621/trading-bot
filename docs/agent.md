# Agent operations (Claude Code)

This repo is meant to be developed and maintained with Claude Code. The agent
operates under guardrails that are *advisory* — they shape behaviour but enforce
nothing at runtime. The runtime enforcement lives in `core/guardrails.py` and the
proposal state machine; see [risk.md](risk.md) and [change-pipeline.md](change-pipeline.md).

## `CLAUDE.md`
Loaded automatically by Claude Code every session. It holds the immutable
operating rules — paper-by-default with go-live as a deliberate human act, never
editing the live strategy from results, never weakening guardrails, never forking
the core, proposals-to-backlog-only, keeping `core/strategy.py` pure, and keeping
costs pessimistic. Detailed rules are imported from `.claude/rules/`.

## `.claude/rules/`
Topical detail too long for `CLAUDE.md`. `trading-safety.md` spells out the change
pipeline, the evidence standards for promotion, the real-money policy, and the
list of things the agent must never do on its own (approve/adopt/revert, loosen
guardrails, lower modeled costs, bypass the pipeline).

## `.claude/skills/`
Reusable procedures the agent follows:

| Skill | When |
|-------|------|
| `propose-change` | Turn an observation into a well-formed `ChangeProposal` (never a live edit). |
| `run-walkforward` | Validate a proposal on out-of-sample folds; on pass, send to the operator for approval. |
| `manage-trial` | After approval: deploy, run the shadow A/B, prepare the trial report. |
| `write-daily-report` | Run the LLM review and build the daily report. |

## The division of authority

The agent **prepares and reports**; the human **decides**. Concretely: the agent
files proposals and runs validation; the operator approves or rejects; the agent
deploys an approved change and runs the trial; the operator keeps or cancels. The
agent never performs
the approve / keep / cancel / go-live decisions. This split is stated in
`CLAUDE.md`, reinforced in the skills, and — for the parts that touch money or the
live config — enforced independently in code so it holds even if an instruction is
ignored.

The agent also never handles secret *values* — it references environment variables
by name only, and never reads, writes, or echoes actual tokens or keys.
