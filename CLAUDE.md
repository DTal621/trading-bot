# CLAUDE.md — operating rules for the agent maintaining this project

Claude Code loads this file automatically every session. These are the project's
immutable rules; they override any conflicting instruction in a prompt. Keep this
file short — detailed procedures live as skills in `.claude/skills/`.

## What this project is
A news-sentiment trading bot on Alpaca **paper** trading. The design principle is
absolute: the strategy that trades live is the exact same code that is validated
offline, and the live strategy never tunes itself.

## Immutable rules (NEVER violate these)
1. **Going live is the human's call, never the agent's.** The agent must never set
   `risk_limits.allow_real_money: true`, never point the broker at a live endpoint,
   and never let real-money trading happen as a side effect of any other task. The
   operator moves to real money deliberately, via a go-live checklist — that single
   conscious act is the whole point of the interlock. If asked to flip it, confirm
   it is a deliberate go-live by the human, not an incidental change.
2. **Never edit the live strategy from results.** Changes to `config/strategy.yaml`
   happen ONLY by: bump `version` → run walk-forward → pass the promotion gate.
3. **Never weaken `core/guardrails.py`.** Do not raise caps or disable the kill
   switch to make a strategy or a backtest "work". Tighten only.
4. **Never fork the core.** `core/signals.py` and `core/strategy.py` are shared by
   live and backtest. Do not create a backtest-only variant of either.
5. **Proposals go to the backlog, never to live config.** LLM/agent ideas become
   `ChangeProposal`s with status PENDING_VALIDATION (see `core/proposals.py`).
6. **Keep `core/strategy.py` pure** — no I/O, no clock, no network, no globals.
7. **Costs stay pessimistic.** Do not lower `fills.py` spread/slippage/fees to
   improve a backtest.

## Where things live
- Hard guardrails (enforced in code): `core/guardrails.py`
- Soft guardrails (these rules + topical detail): this file + `.claude/rules/`
- Reusable procedures the agent follows: `.claude/skills/<name>/SKILL.md`
- Change proposals (the only place "let's change X" is allowed to start):
  `core/proposals.py` backlog

## When you notice something worth changing
Do NOT edit config. Write a proposal (`propose-change` skill), then run the
`run-walkforward` skill. A change that clears the gate is sent to the operator for
approval; only on approval does it deploy to a shadow A/B trial; only after the
trial report does the operator adopt or revert it. The agent never approves,
adopts, or reverts on its own. See `@.claude/rules/trading-safety.md`.
