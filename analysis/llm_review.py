"""
llm_review.py — the part you asked for: an LLM that reads the logs daily and
tells you what it noticed and what it thinks should change.

It is deliberately boxed in so it teaches you instead of fooling you:

  1. It gets a COMPACT, AGGREGATED view of the logs (distributions, outcomes,
     cumulative counts) — not raw rows it can pattern-match noise from.
  2. The system prompt forces it to weigh SAMPLE SIZE and to output structured
     proposals, each with a hypothesis and an explicit "requires walk-forward".
  3. Its proposals are written to the ProposalBacklog as PENDING_VALIDATION.
     It has NO path to the live config. None. That guarantee is in code
     (proposals.py status machine + guardrails.py), not in this prompt.

So "the bot keeps suggesting improvements" becomes a stream of testable
hypotheses you (or an offline agent) run through the gate — real learning,
without letting a confident narrator tune your live money on a good Tuesday.
"""
from __future__ import annotations

import json
from datetime import date

from core.logstore import DecisionLog
from core.proposals import ProposalBacklog, ChangeProposal

SYSTEM_PROMPT = """You are a quantitative research analyst reviewing logs from a \
FROZEN, paper-trading sentiment strategy. You are NOT a trader and you cannot \
change anything. Your job is to surface honest observations and at most 3 \
testable change proposals.

Hard rules:
- Weigh sample size explicitly. If the evidence rests on few trades, say so and \
propose FEWER or zero changes. A good day is not evidence.
- Never claim a change "will" help. Frame every proposal as a hypothesis that \
MUST be validated out-of-sample via walk-forward before any deployment.
- Only propose changes to existing config parameters. Name the exact param.
- Distinguish signal from regime: a profitable week in a trending market is not \
proof the signal works.

Return ONLY valid JSON, no prose, no markdown fences:
{
  "observations": ["..."],
  "proposals": [
    {"param_path": "params.X", "direction": "increase|decrease|replace",
     "proposed_value": <value>, "hypothesis": "...", "evidence_summary": "...",
     "sample_size": <int>}
  ]
}"""


def summarize_logs(log: DecisionLog) -> dict:
    """Compact aggregate view — never the raw firehose."""
    decisions = list(log.read("decision"))
    outcomes = list(log.read("outcome"))
    hold_reasons: dict[str, int] = {}
    actions: dict[str, int] = {}
    for d in decisions:
        actions[d["action"]] = actions.get(d["action"], 0) + 1
        if d["action"] == "HOLD":
            r = d.get("reason", "")
            hold_reasons[r] = hold_reasons.get(r, 0) + 1
    net = sum(o["net_pnl"] for o in outcomes)
    wins = [o for o in outcomes if o["net_pnl"] > 0]
    return {
        "cumulative_decisions": len(decisions),
        "cumulative_trades": len(outcomes),
        "win_rate": (len(wins) / len(outcomes)) if outcomes else None,
        "net_pnl": net,
        "action_counts": actions,
        "top_hold_reasons": dict(sorted(hold_reasons.items(), key=lambda kv: -kv[1])[:6]),
    }


def review(log: DecisionLog, backlog: ProposalBacklog, current_config: dict,
           call_model, today: date | None = None) -> dict:
    """
    `call_model(system, user) -> str` is injected so this stays testable and
    SDK-agnostic. See anthropic_call() below for a real implementation.
    """
    summary = summarize_logs(log)
    user = json.dumps({"current_config": current_config, "log_summary": summary})
    raw = call_model(SYSTEM_PROMPT, user)

    try:
        parsed = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except json.JSONDecodeError:
        return {"observations": ["LLM review returned unparseable output; skipped."],
                "proposals_added": 0}

    added = 0
    for pr in parsed.get("proposals", []):
        param = pr.get("param_path", "")
        backlog.add(ChangeProposal.new(
            source="llm_review",
            param_path=param,
            current_value=_lookup(current_config, param),
            proposed_value=pr.get("proposed_value"),
            direction=pr.get("direction", "replace"),
            hypothesis=pr.get("hypothesis", ""),
            evidence_summary=pr.get("evidence_summary", ""),
            sample_size=int(pr.get("sample_size", 0)),
        ))
        added += 1
    return {"observations": parsed.get("observations", []), "proposals_added": added}


def _lookup(config: dict, dotted: str):
    cur = config
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def anthropic_call(model: str):
    """Returns a call_model closure backed by the Anthropic API. Set ANTHROPIC_API_KEY.
    Pick the model in config; a Sonnet-class model is plenty for this review."""
    def _call(system: str, user: str) -> str:
        from anthropic import Anthropic              # pip install anthropic
        client = Anthropic()
        resp = client.messages.create(
            model=model, max_tokens=1500, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _call
