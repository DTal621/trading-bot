"""
change_pipeline.py — orchestrates one change through every gate, in order.

    validate()   walk-forward gate; pass -> AWAITING_APPROVAL (with evidence sent)
    approve()    human yes/no via the approval channel; yes -> deploy + start trial
    finalize()   when the trial window closes, send the A/B report; keep -> ADOPT,
                 cancel -> revert config + REVERTED

Dependencies are injected (backtest_fn, approval channel, deployer) so this stays
testable and transport-agnostic. Nothing here can move a change forward out of
order — the ProposalBacklog state machine enforces that independently.
"""
from __future__ import annotations

from datetime import datetime

from core.proposals import ProposalBacklog, ProposalStatus
from core.schema import utcnow
from ops.approval import ApprovalChannel, format_proposal
from ops.deploy import Deployer
from ops.trial import start_trial, TrialWindow, build_trial_report
from core.logstore import DecisionLog
from backtest.walkforward import run_walk_forward, promote


def validate(proposal_id: str, backlog: ProposalBacklog, base_config: dict,
             approval: ApprovalChannel, backtest_fn, start: datetime, end: datetime,
             deployer: Deployer) -> str:
    p = backlog.get(proposal_id)
    backlog.transition(proposal_id, ProposalStatus.VALIDATING)

    candidate = deployer.make_candidate(base_config, p["param_path"], p["proposed_value"],
                                        new_version=_next_version(base_config, p))
    cand_oos = run_walk_forward(candidate, start, end, backtest_fn)
    inc_oos = run_walk_forward(base_config, start, end, backtest_fn)
    ok, reason = promote(cand_oos, inc_oos)

    if not ok:
        backlog.transition(proposal_id, ProposalStatus.VALIDATION_FAILED, {"reason": reason})
        return f"validation failed: {reason}"

    evidence = {"folds": cand_oos,
                "passed_folds": sum(1 for f in cand_oos if f.get("net_pnl", 0) > 0),
                "oos_net_pnl": round(sum(f.get("net_pnl", 0) for f in cand_oos), 2)}
    backlog.transition(proposal_id, ProposalStatus.AWAITING_APPROVAL,
                       {"candidate_version": candidate["version"], "evidence": evidence})
    approval.send_for_approval(format_proposal(p, evidence))
    return "passed gate; sent for human approval"


def approve(proposal_id: str, backlog: ProposalBacklog, base_config: dict,
            approval: ApprovalChannel, deployer: Deployer, trial_days: int,
            now: datetime | None = None) -> str:
    now = now or utcnow()
    decision = approval.poll_decision(proposal_id)
    if decision is None:
        return "no decision yet"
    if decision == "reject":
        backlog.transition(proposal_id, ProposalStatus.REJECTED)
        return "rejected by human"

    p = backlog.get(proposal_id)
    candidate_version = backlog.payload(proposal_id, "candidate_version")
    candidate = deployer.make_candidate(base_config, p["param_path"], p["proposed_value"],
                                        new_version=candidate_version)
    previous_version = deployer.deploy(candidate)  # candidate now live
    window = start_trial(candidate_version, previous_version, now, trial_days)
    backlog.transition(proposal_id, ProposalStatus.DEPLOYED_TRIAL, {
        "candidate_version": candidate_version,
        "incumbent_version": previous_version,
        "trial_start": window.start.isoformat(),
        "trial_end": window.end.isoformat(),
    })
    return f"deployed {candidate_version}; shadow A/B trial until {window.end.date()}"


def finalize(proposal_id: str, backlog: ProposalBacklog, deployer: Deployer,
             log: DecisionLog, approval: ApprovalChannel, now: datetime | None = None) -> str:
    """
    Process a keep/cancel decision for a completed trial.

    Sending the trial prompt and acting on the response are intentionally
    separated: check_expired_trials() sends the prompt (once), and this
    function acts on whatever the human replies.  If the prompt has not been
    sent yet (e.g. called directly before the daily job ran), this function
    sends it — but it will not re-send if mark_trial_prompt_sent() was already
    called, so the two code paths are safe to coexist.
    """
    now = now or utcnow()
    window = TrialWindow(
        candidate_version=backlog.payload(proposal_id, "candidate_version"),
        incumbent_version=backlog.payload(proposal_id, "incumbent_version"),
        start=datetime.fromisoformat(backlog.payload(proposal_id, "trial_start")),
        end=datetime.fromisoformat(backlog.payload(proposal_id, "trial_end")),
    )
    if not window.is_complete(now):
        return "trial still running"

    # Only send the prompt if it hasn't been dispatched yet (idempotency guard).
    if not backlog.trial_prompt_sent(proposal_id):
        approval.send_trial_decision(proposal_id, build_trial_report(log, window, now))
        backlog.mark_trial_prompt_sent(proposal_id)

    decision = approval.poll_decision(proposal_id)  # expect 'keep' or 'cancel'
    if decision == "cancel":
        deployer.revert_to(window.incumbent_version)
        backlog.transition(proposal_id, ProposalStatus.REVERTED,
                           {"reverted_to": window.incumbent_version})
        return f"reverted to {window.incumbent_version}"
    if decision == "keep":
        backlog.transition(proposal_id, ProposalStatus.ADOPTED)
        return f"adopted {window.candidate_version}"
    return "awaiting keep/cancel decision"


def check_expired_trials(backlog: ProposalBacklog, log: DecisionLog,
                         approval: ApprovalChannel, deployer: Deployer,
                         config: dict, now: datetime | None = None) -> list[str]:
    """
    Send the Keep/Cancel trial prompt for every DEPLOYED_TRIAL proposal whose
    trial window has closed, exactly once.

    Designed to run inside the daily-report job (or any scheduled check).
    Call this after the daily report has been sent so the prompt arrives as a
    separate message and is easy to action.

    Returns a list of status strings (one per checked proposal) for logging.

    Idempotency guarantee:
      - mark_trial_prompt_sent() is written to the backlog before this function
        returns, so a crash after the send but before the write is the only gap.
        In that case the prompt would be re-sent on the next daily run — a
        duplicate Telegram message is a minor nuisance, not a correctness bug,
        and is far better than silently missing the notification.
      - A proposal that has already been finalized (ADOPTED / REVERTED) will not
        be returned by by_status(DEPLOYED_TRIAL), so it is never checked here.
    """
    now = now or utcnow()
    results: list[str] = []

    for p in backlog.by_status(ProposalStatus.DEPLOYED_TRIAL):
        pid = p["proposal_id"]

        trial_end_raw = backlog.payload(pid, "trial_end")
        if trial_end_raw is None:
            results.append(f"{pid[:8]}: missing trial_end — skipped")
            continue

        trial_end = datetime.fromisoformat(trial_end_raw)
        if now < trial_end:
            results.append(f"{pid[:8]}: trial still running until {trial_end.date()}")
            continue

        if backlog.trial_prompt_sent(pid):
            results.append(f"{pid[:8]}: prompt already sent — awaiting keep/cancel")
            continue

        # Window is closed and prompt has not been sent yet — send it now.
        window = TrialWindow(
            candidate_version=backlog.payload(pid, "candidate_version"),
            incumbent_version=backlog.payload(pid, "incumbent_version"),
            start=datetime.fromisoformat(backlog.payload(pid, "trial_start")),
            end=trial_end,
        )
        report = build_trial_report(log, window, now)
        approval.send_trial_decision(pid, report)
        backlog.mark_trial_prompt_sent(pid)
        results.append(f"{pid[:8]}: trial expired {trial_end.date()} — Keep/Cancel prompt sent")
        print(f"[trial] {pid[:8]}: window closed {trial_end.date()}, "
              f"Keep/Cancel prompt dispatched")

    return results


def _next_version(base_config: dict, p: dict) -> str:
    base = base_config.get("version", "v0").split("-")[0]
    tag = p["param_path"].split(".")[-1].replace("_", "-")
    return f"{base}+{p['direction']}-{tag}"
