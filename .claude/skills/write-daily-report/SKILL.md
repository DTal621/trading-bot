---
name: write-daily-report
description: Use to generate the end-of-day report. Summarizes the day's decisions from the logs, runs the LLM review to surface observations and proposals, and renders the report. Reports and proposes only — it never changes the live strategy.
---

# Skill: write-daily-report

The report is a lab notebook, not a scoreboard, and not a control panel.

## Steps
1. Load the frozen `config/strategy.yaml` and the `DecisionLog`.
2. Run `analysis/llm_review.review(log, backlog, config, call_model)`:
   - it sends an AGGREGATE summary (not raw rows) to the model,
   - parses structured proposals, and writes each to the backlog as
     PENDING_VALIDATION.
3. Call `reports/daily.build_daily_report(log, today, backlog, observations)`.
4. Output the report. Do nothing else — no config edits, no deployments.

## Tone discipline (enforced in the prompt and the report)
- Lead with sample size. Few trades → conclude nothing.
- Frame every suggested change as a hypothesis requiring walk-forward.
- Note when results may be regime-driven rather than signal-driven.
- It is a good day when the report proposes zero changes.
