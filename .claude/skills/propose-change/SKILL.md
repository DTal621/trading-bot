---
name: propose-change
description: Use when the agent or the LLM review notices something in the trading logs that might justify a strategy change. Turns a raw observation into a well-formed ChangeProposal in the backlog. Never edits live config.
---

# Skill: propose-change

A change starts as a hypothesis in the backlog, never as an edit to
`config/strategy.yaml`.

## Steps
1. Identify the single config parameter the observation implicates
   (e.g. `params.entry_score_threshold`). One parameter per proposal.
2. State a **testable hypothesis**: "Lowering X from A to B should increase
   trade count without reducing out-of-sample win rate, because ...".
3. Record the **evidence** and the **sample size** it rests on. If sample size is
   small, say so and consider not proposing at all.
4. Create a `ChangeProposal` via `core/proposals.py` with status
   PENDING_VALIDATION. Do NOT touch the live config.
5. Hand off to the `run-walkforward` skill.

## Refuse if
- The change requires loosening `core/guardrails.py` or `fills.py` costs.
- The "evidence" is a single good day/week.
- The proposal bundles multiple parameter changes (split them).
