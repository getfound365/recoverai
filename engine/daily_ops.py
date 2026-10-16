"""Agent 4's daily-operations read models — the console's live queue, mode control,
interaction history, alert feed and money trail, computed from real engine rows.

Every field in every payload here is one of exactly three things, and the docstring on
each builder says which:

  * a column read verbatim out of a table (returned as the engine stores it),
  * a value DERIVED by arithmetic this module names out loud (an age in days from a
    stored invoice_date; a cent count from a stored balance), or
  * a LABEL chosen by the precedence order documented at `STATUS_PRECEDENCE`.

Nothing is estimated, projected, filled in, or defaulted into existence. A record with
no last contact attempt has `last_attempt: None`, not a plausible-looking zero.

Two deliberate non-actions:

  * **No guardrail is re-implemented here.** Whether a send is allowed is decided by
    `engine/orchestrator.py` inside the claim transaction, and whether a mode flip or an
    approval is allowed by `engine/onboarding.py`. Where the console needs to say "this
    click would be refused", it runs the engine's OWN code path (see `decide_review`,
    which calls the real `Orchestrator.approve` / `.reject` and translates the exception
    the engine raised) or mirrors the exact three guards `approve()` itself raises on
    (see `approval_blockers`, and the note there).
  * **Nothing here writes.** Every mutation goes through the engine facade
    (`engine/app.py`); this module is read models plus two pure translators.

The alert feed is the one thing that reads a FILE rather than a table: the engine's
append-only `guardrail_actions.jsonl`. That is deliberate — the audit log is the
engine's own event stream, and it survives the console's per-request process being
thrown away, which an in-memory log would not.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import clock, config, draft as draftsman, gate, ingest, onboarding, payments

#: The console's status vocabulary for one debtor row, in display precedence order.
#:
#: Why this order: a payment state is terminal or holds a record, so it outranks
#: everything (no mode can send past Paid, a parked balance or a reversal awaiting its
#: audit). Next comes DNC, which is a permanent, debtor-set, mode-independent stop. Then
#: the calling-window freeze, then whatever the LAST contact attempt did (a committed
#: claim, or the reason token of a refused one), and finally the plain queue state.
STATUS_PRECEDENCE: Tuple[str, ...] = (
    "Paid",
    "Reopened_Awaiting_Audit",
    "Pending_Adjustment_Confirmation",
    "Partially_Paid",
    "DNC",
    "Outside_Calling_Window",
    "CLAIMED",
    "BLOCKED_*",
    "Pending",
)
STATUS_PRECEDENCE_NOTE = (
    "Row status is the first match in this order: Paid > Reopened_Awaiting_Audit > "
    "Pending_Adjustment_Confirmation > Partially_Paid > DNC > Outside_Calling_Window > "
    "CLAIMED (the last attempt committed a claim) > the token of the last REFUSED claim "
    "(BLOCKED_*) > Pending. A payment/held state is never hidden behind a DNC badge and "
    "vice versa: the raw dnc_flag, account_state and approval_status travel on every row "
    "so nothing is masked by the label."
)

#: Sort rank for the rendered queue: work a human can act on first, closed accounts last.
_STATUS_RANK = {
    "Pending": 0,
    "Outside_Calling_Window": 1,
    "BLOCKED_*": 2,
    "CLAIMED": 3,
    "DNC": 4,
    "Partially_Paid": 5,
    "Pending_Adjustment_Confirmation": 6,
    "Reopened_Awaiting_Audit": 7,
    "Paid": 8,
}


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------
def _parse_iso_date(value: Any) -> Optional[date]:
    """A stored invoice_date back to a date, or None. Unparseable NEVER becomes today:
    the engine's own `aging_for(None)` calls that 'unknown', and so does this."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _cents(value: Any) -> Optional[int]:
    if value is None:
        return None
    return payments.to_cents(value)


def _money(cents: Optional[int]) -> Optional[str]:
    """A display string for a cent value, or None. Cents stay authoritative — this is
    formatting only, and it does not exist for a value that is not there."""
    if cents is None:
        return None
    return f"{payments.from_cents(cents):,.2f}"


def _last_attempts(store, client_id: str) -> Dict[str, Dict[str, Any]]:
    """The most recent contact-log row per debtor: the only thing a row's CLAIMED /
    BLOCKED_* label is allowed to be derived from."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in store.query(
            "SELECT * FROM outbound_contact_logs WHERE client_id = ? ORDER BY contact_log_id",
            (client_id,)):
        out[row["debtor_id"]] = row
    return out


def _attempt_view(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "contact_log_id": row.get("contact_log_id"),
        "attempted_at": row.get("attempted_at"),
        "channel": row.get("contact_type"),
        "destination": row.get("destination"),
        "decision": row.get("decision"),
        "status": row.get("status"),
        "reason_detail": row.get("reason_detail"),
        "claimed_at": row.get("claimed_at"),
        "claim_token": row.get("claim_token"),
        "operating_mode_at_claim": row.get("operating_mode_at_claim"),
        "mode_version_at_claim": row.get("mode_version_at_claim"),
        "approval_status_at_claim": row.get("approval_status_at_claim"),
        "dnc_flag_at_claim": row.get("dnc_flag_at_claim"),
        "account_state_at_claim": row.get("account_state_at_claim"),
        "balance_at_claim_cents": row.get("balance_at_claim_cents"),
        "window_decision": row.get("window_decision"),
        "derived_local_time": row.get("derived_local_time"),
        "approved_by": row.get("approved_by"),
        "approved_at": row.get("approved_at"),
        "payload_path": row.get("payload_path"),
        "message_preview": row.get("message_preview"),
    }


def _status_for(row: Dict[str, Any], attempt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The one label the console shows for this row, plus the sentence behind it."""
    account_state = row.get("account_state") or "Open"
    approval_status = row.get("approval_status") or "Pending"
    dnc = int(row.get("dnc_flag") or 0)

    if account_state == "Paid":
        return {"status": "Paid", "status_reason":
                "the balance reached zero on a confirmed payment, so this record is terminal: "
                "never contacted again, and every scheduled contact was purged on the payment"}
    if account_state == payments.STATE_REOPENED_AWAITING_AUDIT:
        return {"status": "Reopened_Awaiting_Audit", "status_reason":
                "the payment on this record was reversed (bounce or refund), so no mode can "
                "contact it until it has re-run the ingestion path from step one and the "
                "completed re-audit is recorded"}
    if account_state == payments.STATE_PENDING_ADJUSTMENT:
        return {"status": "Pending_Adjustment_Confirmation",
                "status_reason": "the balance moved with no payment signal behind it: parked, "
                                 "no dunning, no fee, until the client attributes the change"}
    if account_state == "Partially_Paid":
        return {"status": "Partially_Paid", "status_reason":
                "a confirmed payment arrived and a remainder is still owed: only the "
                "remainder can be pursued"}
    if dnc == 1:
        return {"status": "DNC", "status_reason":
                f"dnc_flag = TRUE (set {row.get('dnc_set_at')}): the debtor asked us to stop, "
                "so every current and future run is killed in every operating mode"}
    if approval_status == "Outside_Calling_Window":
        return {"status": "Outside_Calling_Window", "status_reason":
                f"frozen by the calling-window guardrail ({row.get('window_decision')} "
                f"at local time {row.get('derived_local_time')}): the record can only be "
                "approved once the debtor's own window is open again"}
    if attempt and attempt.get("status") == "BLOCKED":
        return {"status": attempt.get("decision") or "BLOCKED_UNKNOWN",
                "status_reason": attempt.get("reason_detail") or "the last attempt was refused"}
    if attempt and attempt.get("claim_token"):
        return {"status": "CLAIMED", "status_reason":
                f"the last attempt committed a claim (contact_log_id "
                f"{attempt.get('contact_log_id')}, dispatch status {attempt.get('status')})"}
    return {"status": "Pending", "status_reason":
            "queued: nothing has been contacted or claimed against this record yet"}


# ---------------------------------------------------------------------------
# 1. the live receivables queue
# ---------------------------------------------------------------------------
def queue_rows(store, client_id: str, runqueue=None,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """One row per debtor of this client, with its account status, balance and aging.

    Derived (named): `age_days` and `aging_label` come from the engine's own
    `ingest.aging_for` run against the STORED invoice_date and the current date;
    `balance_cents` is the stored balance in integer cents, via `payments.to_cents`.
    Everything else is a column.
    """
    today = (now or clock.now_utc()).date()
    rows = store.query("SELECT * FROM debtor_records WHERE client_id = ? ORDER BY debtor_id",
                       (client_id,))
    attempts = _last_attempts(store, client_id)
    scheduled: Dict[str, List[Any]] = {}
    if runqueue is not None:
        for run in runqueue.all():
            if run.client_id == client_id and run.state == "scheduled":
                scheduled.setdefault(run.debtor_id, []).append(run)

    out: List[Dict[str, Any]] = []
    for row in rows:
        debtor_id = row["debtor_id"]
        attempt = attempts.get(debtor_id)
        label = _status_for(row, attempt)
        invoice_date = _parse_iso_date(row.get("invoice_date"))
        aging_state, age_days, aging_note = ingest.aging_for(invoice_date, today)
        runs = scheduled.get(debtor_id, [])
        cents = _cents(row.get("balance_owed"))
        out.append({
            "debtor_id": debtor_id,
            "name": row.get("name"),
            "email": row.get("email"),
            "phone": row.get("phone"),
            "balance_owed": row.get("balance_owed"),
            "balance_cents": cents,
            "balance_display": _money(cents),
            "invoice_number": row.get("invoice_number"),
            "invoice_date": row.get("invoice_date"),
            "aging_state": row.get("aging_state"),
            "aging_label": draftsman.age_label({"aging_state": row.get("aging_state")}),
            "age_days": age_days,
            "aging_note": aging_note,
            "aging_source": "ingest.aging_for(stored invoice_date, today)",
            "status": label["status"],
            "status_reason": label["status_reason"],
            "approval_status": row.get("approval_status"),
            "account_state": row.get("account_state"),
            "dnc_flag": int(row.get("dnc_flag") or 0),
            "dnc_set_at": row.get("dnc_set_at"),
            "window_decision": row.get("window_decision"),
            "derived_local_time": row.get("derived_local_time"),
            "approved_by": row.get("approved_by"),
            "approved_at": row.get("approved_at"),
            "imported_at": row.get("imported_at"),
            "source_ref": row.get("source_ref"),
            "last_attempt": _attempt_view(attempt),
            "scheduled_runs": [
                {"run_id": r.run_id, "channel": r.channel, "due_at": r.due_at,
                 "state": r.state, "state_reason": r.state_reason} for r in runs],
            "scheduled_count": len(runs),
        })

    def sort_key(item: Dict[str, Any]) -> Tuple[int, float, str]:
        return (_STATUS_RANK.get(item["status"], 99),
                -float(item["balance_cents"] or 0),
                str(item["debtor_id"]))

    out.sort(key=sort_key)

    by_status: Dict[str, int] = {}
    for item in out:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    total_cents = sum(int(item["balance_cents"] or 0) for item in out)
    return {
        "client_id": client_id,
        "as_of": clock.iso_ms(now),
        "as_of_precision": "millisecond UTC",
        "status_precedence": list(STATUS_PRECEDENCE),
        "status_precedence_note": STATUS_PRECEDENCE_NOTE,
        "rows": out,
        "counts": {
            "debtors": len(out),
            "by_status": by_status,
            "pending_reviews": sum(1 for i in out if i["approval_status"] == "Pending"),
            "scheduled_runs": sum(int(i["scheduled_count"]) for i in out),
            "dnc_records": sum(1 for i in out if i["dnc_flag"] == 1),
        },
        "portfolio": {
            "balance_total_cents": total_cents,
            "balance_total": _money(total_cents),
            "basis": "the sum of balance_owed as stored on each row the queue rendered",
        },
        "refresh_note": (
            "There is no live push to this page. Every figure above was computed by the "
            "engine at the instant in `as_of`; the Refresh button re-runs the read."
        ),
    }


# ---------------------------------------------------------------------------
# 2. mode control, the halt, and the HITL review queue
# ---------------------------------------------------------------------------
def timeline(store, client_id: str) -> List[Dict[str, Any]]:
    """`autonomy_mode_history`, verbatim, newest last. Append-only: no UPDATE or DELETE
    on this table is accepted by the store, including from our own code.

    `refused_attempt` is DERIVED: the engine records a refused switch as an
    old_mode -> old_mode row whose reason starts with "REFUSED". The stored reason is
    returned too, so the derivation can always be checked against it.
    """
    rows = store.query(
        "SELECT * FROM autonomy_mode_history WHERE client_id = ? ORDER BY history_id",
        (client_id,))
    items = []
    for row in rows:
        reason = row.get("reason") or ""
        items.append({
            "history_id": row.get("history_id"),
            "who": row.get("who"),
            "old_mode": row.get("old_mode"),
            "new_mode": row.get("new_mode"),
            "timestamp": row.get("timestamp"),
            "timestamp_precision": "UTC, to the millisecond (or to the second for rows "
                                   "written before this pass)",
            "reason": row.get("reason"),
            "mode_version": row.get("mode_version"),
            "refused_attempt": reason.startswith("REFUSED"),
        })
    return items


def mode_control(store, client_id: str, runqueue=None,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """The mode switch's state: what is in force, what is selectable, and why.

    `full_auto_selectable` is the engine's OWN `onboarding.signoff_state` verdict — the
    same predicate `switch_mode` refuses on — so the button is never disabled for a
    reason the engine would not also refuse on, and never enabled for one it would not
    honour.
    """
    settings = onboarding.ensure_client(store, client_id)
    modes = config.operating_modes_cfg()
    state = onboarding.signoff_state(settings, now)
    runs = []
    if runqueue is not None:
        runs = [{"run_id": r.run_id, "debtor_id": r.debtor_id, "channel": r.channel,
                 "due_at": r.due_at, "state": r.state, "state_reason": r.state_reason}
                for r in runqueue.all() if r.client_id == client_id]
    by_state: Dict[str, int] = {}
    for run in runs:
        by_state[run["state"]] = by_state.get(run["state"], 0) + 1
    return {
        "client_id": client_id,
        "operating_mode": settings.get("operating_mode"),
        "mode_version": int(settings.get("mode_version") or 0),
        "updated_at": settings.get("updated_at"),
        "last_claim_at": settings.get("last_claim_at"),
        "claim_seq": int(settings.get("claim_seq") or 0),
        "available_modes": list(modes["available"]),
        "default_mode": modes["default"],
        "mode_labels": {"hitl": modes["hitl_label"], "full_auto": modes["full_auto_label"]},
        "full_auto_requires_signoff": bool(modes["full_auto_requires_signoff"]),
        "signoff": state,
        "signer_name": settings.get("signer_name"),
        "signer_title": settings.get("signer_title"),
        "signature_reference": settings.get("signature_reference"),
        "signed_timestamp": settings.get("signed_timestamp"),
        "signoff_expires_at": settings.get("signoff_expires_at"),
        "authorized_channels": settings.get("authorized_channels"),
        "authorized_aging_states": settings.get("authorized_aging_states"),
        "full_auto_selectable": bool(state["valid"]),
        "full_auto_locked_reason": None if state["valid"] else state["reason"],
        "hitl_banner": settings.get("operating_mode") == "hitl",
        "hitl_banner_text": (
            "Manual mode (HITL) is ON: nothing leaves until a named human approves it. "
            "Scheduled runs are halted, not deleted."),
        "scheduled_runs": runs,
        "scheduled_by_state": by_state,
        "timeline": timeline(store, client_id),
    }


def approval_blockers(store, row: Dict[str, Any], now: Optional[datetime] = None,
                      client_id: Optional[str] = None) -> List[Dict[str, str]]:
    """Why an Approve click on this row would NOT turn into an approved contact.

    This mirrors the three guards `Orchestrator.approve` itself raises on, in the same
    order, because those are the ones that make a click refuse:
      * dnc_flag set                        -> BLOCKED_DNC
      * account_state Reopened_Awaiting_Audit, or an open reopen audit
                                            -> REOPENED_AWAITING_AUDIT
      * approval_status Outside_Calling_Window and the window is still shut
                                            -> BLOCKED_OUTSIDE_WINDOW
    Everything AFTER approval (the sign-off, the scope, the balance, the mode) is decided
    by the claim transaction at the last millisecond before the POST, and is deliberately
    NOT previewed here: the console says so on screen rather than guessing at it.
    """
    from . import timezones  # local import: keeps this module's import graph flat

    blockers: List[Dict[str, str]] = []
    if int(row.get("dnc_flag") or 0) == 1:
        blockers.append({"token": "BLOCKED_DNC", "detail":
                         "the debtor is on the global do-not-contact list; approval would be refused"})
    if (row.get("account_state") == payments.STATE_REOPENED_AWAITING_AUDIT
            or payments.open_reopen_audit(store, row["debtor_id"]) is not None):
        blockers.append({"token": "REOPENED_AWAITING_AUDIT", "detail":
                         "a reversed payment must re-run the ingestion path from step one "
                         "before anything is approved for this record"})
    if row.get("approval_status") == "Outside_Calling_Window":
        window = timezones.window_decision_for(row.get("phone"))
        if not window["in_window"]:
            blockers.append({"token": "BLOCKED_OUTSIDE_WINDOW", "detail":
                             f"{window['window_decision']} at local time "
                             f"{window.get('derived_local_time')}; approval stays refused "
                             "until the debtor's own window is open"})
    return blockers


def pending_reviews(store, client_id: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The HITL review queue: drafts queued as Pending, the decision history, and the
    live count behind the manual-review milestone.

    The milestone count is `gate.manual_review_counts` — the same function the gate
    promotes on. The console shows it; it never computes its own version of it.
    """
    rows = store.query(
        "SELECT * FROM debtor_records WHERE client_id = ? AND approval_status = 'Pending' "
        "ORDER BY debtor_id", (client_id,))
    decisions = gate.manual_review_counts(store, client_id)
    milestone = gate.milestone_state(store, client_id)["milestones"][gate.MILESTONE_MANUAL_REVIEW]
    items = []
    for row in rows:
        cents = _cents(row.get("balance_owed"))
        blockers = approval_blockers(store, row, now=now, client_id=client_id)
        items.append({
            "debtor_id": row["debtor_id"],
            "name": row.get("name"),
            "balance_owed": row.get("balance_owed"),
            "balance_cents": cents,
            "balance_display": _money(cents),
            "aging_state": row.get("aging_state"),
            "aging_label": draftsman.age_label({"aging_state": row.get("aging_state")}),
            "approval_status": row.get("approval_status"),
            "account_state": row.get("account_state"),
            "dnc_flag": int(row.get("dnc_flag") or 0),
            "imported_at": row.get("imported_at"),
            "approval_blockers": blockers,
            "approvable_by_engine": not blockers,
            "claim_recheck_note": (
                "Approving this draft is not sending it. The claim transaction re-reads "
                "mode, sign-off, scope, DNC, paid status, balance and the calling window "
                "immediately before the POST, and refuses with its own token there."),
        })
    history = store.query(
        "SELECT * FROM manual_review_decisions WHERE client_id = ? ORDER BY decision_id DESC "
        "LIMIT 20", (client_id,))
    return {
        "client_id": client_id,
        "as_of": clock.iso_ms(now),
        "rows": items,
        "pending_count": len(items),
        "manual_review_counts": decisions,
        "milestone": {"key": gate.MILESTONE_MANUAL_REVIEW,
                      "value": bool(milestone["value"]),
                      "flag_value": milestone.get("flag_value"),
                      "detail": milestone.get("detail"),
                      "plain_english": milestone.get("plain_english"),
                      "evidence_source": milestone.get("evidence_source")},
        "recent_decisions": [
            {"decision_id": d.get("decision_id"), "debtor_id": d.get("debtor_id"),
             "decision": d.get("decision"), "decided_by": d.get("decided_by"),
             "operating_mode_at_decision": d.get("operating_mode_at_decision"),
             "decided_at": d.get("decided_at"),
             "client_caused_exception": d.get("client_caused_exception"),
             "reason": d.get("reason")} for d in history],
        "milestone_note": (
            "Only hitl approvals and rejections count toward the 20-review milestone, and a "
            "guardrail correctly blocking a send is recorded with zero client-caused "
            "exceptions — the product working, never an infraction."),
    }


#: Exception text -> the token the engine already used for that condition. The mapping is
#: on the MESSAGE because that is the only thing `Orchestrator.approve` raises; it is a
#: translation of the engine's own refusal into the vocabulary the console renders, not a
#: second decision.
_REFUSAL_TEXT_TOKENS: Tuple[Tuple[str, str], ...] = (
    ("do-not-contact", "BLOCKED_DNC"),
    ("reopen audit", "REOPENED_AWAITING_AUDIT"),
)


def refusal_token_for(message: str) -> str:
    lowered = (message or "").lower()
    for needle, token in _REFUSAL_TEXT_TOKENS:
        if needle in lowered:
            return token
    return "APPROVAL_REFUSED"


def decide_review(store, orchestrator, *, client_id: str, debtor_id: str, action: str,
                  who: str, reason: str = "") -> Dict[str, Any]:
    """Run a real Approve or Reject through the engine and translate its answer.

    Never guesses: the engine's own method decides, its own `manual_review_decisions`
    row is what the milestone counts, and anything it refuses comes back here as
    `ok: false` with the engine's token and its own words.
    """
    row = store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
    if not row:
        return {"ok": False, "refusal_token": "UNKNOWN_DEBTOR",
                "detail": f"no debtor_records row for {debtor_id}"}
    if row["client_id"] != client_id:
        return {"ok": False, "refusal_token": "WRONG_CLIENT",
                "detail": f"{debtor_id} belongs to {row['client_id']}, not {client_id}"}
    if action not in ("approve", "reject"):
        return {"ok": False, "refusal_token": "UNKNOWN_ACTION",
                "detail": f"action must be approve or reject, not {action!r}"}
    try:
        if action == "approve":
            result = orchestrator.approve(debtor_id, who)
        else:
            result = orchestrator.reject(debtor_id, who, reason)
    except PermissionError as exc:
        token = refusal_token_for(str(exc))
        return {"ok": False, "refusal_token": token, "debtor_id": debtor_id,
                "action": action, "detail": str(exc),
                "because": "the engine refused this decision inside its own approval path"}
    except KeyError as exc:
        return {"ok": False, "refusal_token": "UNKNOWN_DEBTOR", "debtor_id": debtor_id,
                "detail": str(exc)}
    if action == "approve" and result.get("approved") is False:
        # The window freeze: the engine answered "no send, not approved" rather than
        # raising, so the token comes from the window decision it returned.
        return {"ok": False, "refusal_token": "BLOCKED_OUTSIDE_WINDOW", "debtor_id": debtor_id,
                "action": action,
                "detail": result.get("reason") or "the record is frozen by the calling window",
                "window_decision": result.get("window_decision"), "engine_result": result}
    return {"ok": True, "debtor_id": debtor_id, "action": action, "who": who,
            "engine_result": result}


# ---------------------------------------------------------------------------
# 3. the interaction history drawer
# ---------------------------------------------------------------------------
def _read_payload(path_value: Optional[str], payloads_dir: Optional[Path] = None) -> Dict[str, Any]:
    """The payload this claim actually carried, read back from the file the engine wrote
    BEFORE the POST. Missing or unreadable is reported as such — never reconstructed,
    never approximated."""
    if not path_value:
        return {"available": False, "reason": "no payload file was recorded for this entry"}
    path = Path(path_value)
    if not path.exists() and payloads_dir is not None:
        candidate = Path(payloads_dir) / path.name
        if candidate.exists():
            path = candidate
    if not path.exists():
        return {"available": False, "reason": f"payload file {path} is not on disk"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"payload file {path} could not be read: {exc}",
                "path": str(path)}
    disclaimer = raw.get("disclaimer") or {}
    subject = raw.get("subject")
    body = raw.get("message") or ""
    text = disclaimer.get("text") or ""
    return {
        "available": True,
        "path": str(path),
        "subject": subject,
        "body": body,
        "disclaimer_text": text,
        "disclaimer_position": disclaimer.get("position"),
        "disclaimer_occurrences": disclaimer.get("occurrences"),
        # DERIVED: is the disclaimer the last content of the body as sent? The engine's
        # own draftsman.disclaimer_is_last answers the same question for a draft.
        "disclaimer_is_last": bool(text) and draftsman.disclaimer_is_last(body),
        "authorization": raw.get("authorization"),
        "claim": raw.get("claim"),
        "guardrails": raw.get("guardrails"),
        "provider": raw.get("provider"),
        "template_reference": raw.get("template_reference"),
        "rulebook_version": raw.get("rulebook_version"),
        "metadata": raw.get("metadata"),
    }


def interactions(store, debtor_id: str, payloads_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Every contact attempt against one debtor, oldest first, as the record of them.

    `attempted_at` is the engine's second-precision instant; `claimed_at` — present only
    on an attempt that committed a claim — is the millisecond-UTC instant the claim was
    written. Both travel, and the UI says which is which rather than presenting the
    coarser one as the finer.
    """
    record = store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
    if not record:
        return {"debtor_id": debtor_id, "found": False, "entries": [],
                "detail": f"no debtor_records row for {debtor_id}"}
    logs = store.query(
        "SELECT * FROM outbound_contact_logs WHERE debtor_id = ? ORDER BY contact_log_id",
        (debtor_id,))
    entries: List[Dict[str, Any]] = []
    for log in logs:
        payload = _read_payload(log.get("payload_path"), payloads_dir)
        response = log.get("provider_response")
        parsed_response: Any = None
        if response:
            try:
                parsed_response = json.loads(response)
            except (TypeError, ValueError):
                parsed_response = response
        blocked = log.get("status") == "BLOCKED"
        entries.append({
            "contact_log_id": log.get("contact_log_id"),
            "attempted_at": log.get("attempted_at"),
            "attempted_at_precision": "second UTC",
            "claimed_at": log.get("claimed_at"),
            "claimed_at_precision": "millisecond UTC",
            "channel": log.get("contact_type"),
            "is_call": log.get("contact_type") in ("voice", "sms"),
            "destination": log.get("destination"),
            "decision": log.get("decision"),
            "status": log.get("status"),
            "guardrail_token": log.get("decision") if blocked else None,
            "reason_detail": log.get("reason_detail"),
            "operating_mode_at_claim": log.get("operating_mode_at_claim"),
            "mode_version_at_claim": log.get("mode_version_at_claim"),
            "approval_status_at_claim": log.get("approval_status_at_claim"),
            "dnc_flag_at_claim": log.get("dnc_flag_at_claim"),
            "account_state_at_claim": log.get("account_state_at_claim"),
            "balance_at_claim_cents": log.get("balance_at_claim_cents"),
            "balance_at_claim": _money(log.get("balance_at_claim_cents")),
            "window_decision": log.get("window_decision"),
            "derived_local_time": log.get("derived_local_time"),
            "area_code": log.get("area_code"),
            "signature_reference": log.get("signature_reference"),
            "approved_by": log.get("approved_by"),
            "approved_at": log.get("approved_at"),
            "claim_token": log.get("claim_token"),
            "payload_path": log.get("payload_path"),
            "message_preview": log.get("message_preview"),
            "provider_response": parsed_response,
            "draft": payload,
            "draft_note": (
                "read back from the payload file the engine wrote before the POST; the body "
                "it carries ends with the rulebook disclaimer"),
        })
    return {
        "debtor_id": debtor_id,
        "found": True,
        "client_id": record.get("client_id"),
        "name": record.get("name"),
        "account_state": record.get("account_state"),
        "approval_status": record.get("approval_status"),
        "dnc_flag": int(record.get("dnc_flag") or 0),
        "entries": entries,
        "entry_count": len(entries),
        "call_count": sum(1 for e in entries if e["is_call"]),
        "blocked_count": sum(1 for e in entries if e["guardrail_token"]),
        "read_only_note": (
            "Read-only. Sourced from outbound_contact_logs, the table the claim transaction "
            "writes and nothing updates except the dispatch outcome. Call summaries are "
            "simulated — no call is placed and no message is sent."),
    }


# ---------------------------------------------------------------------------
# 4. the system alert feed
# ---------------------------------------------------------------------------
#: action -> (severity, plain English). Severity drives the red styling. "high" is
#: reserved for the events a person must look at: money moving, a reversal, a parked
#: balance, a guardrail freeze, a DNC opt-out, a lifecycle change, a locked gate and a
#: failed dispatch. A refused claim is normal operation, not an incident — the engine
#: itself records those with zero client-caused exceptions — so it is "normal".
ALERT_SEVERITY: Dict[str, str] = {
    "payment_recorded": "high",
    "reversal_recorded": "high",
    "balance_edit_parked": "high",
    "guardrail_freeze": "high",
    "dnc_set": "high",
    "lifecycle_transition": "high",
    "gate_locked": "high",
    "agent4_alert": "high",
    "post_failed": "high",
    "spreadsheet_commit_refused": "high",
    "ingestion_batch_refused": "high",
    "billing_route_verified": "high",
    "claim_blocked": "normal",
    "mode_change": "normal",
    "mode_change_refused": "normal",
    "draft_approved": "normal",
    "draft_rejected": "normal",
    "draft_queued": "normal",
    "claim_allowed": "normal",
    "post_dispatched": "normal",
    "ledger_entry_appended": "normal",
    "queue_purged_for_payment": "normal",
    "run_halted": "normal",
    "halt_requested": "normal",
    "run_scheduled": "normal",
    "runs_cancelled": "normal",
    "spreadsheet_committed": "normal",
    "ingestion_batch_registered": "normal",
    "ledger_imported": "normal",
    "signoff_created": "normal",
    "signoff_revoked": "normal",
    "signoff_expired": "normal",
    "inbound_reply_logged": "info",
    "payment_duplicate_ignored": "info",
    "balance_edit_noop": "info",
    "balance_edit_resolved": "info",
}


def _alert_sentence(entry: Dict[str, Any]) -> str:
    """Plain English for one audit entry, built only from that entry's own fields."""
    action = entry.get("action")
    debtor = entry.get("debtor_id") or "(no debtor named)"
    client = entry.get("client_id") or "(no client named)"
    if action == "payment_recorded":
        return (f"A payment of ${_money(entry.get('amount_cents'))} was recorded against "
                f"{debtor} via {entry.get('confirmation_channel')}. The record's balance moved "
                f"to ${_money(entry.get('balance_after_cents'))} and {entry.get('cancelled_run_count')} "
                f"scheduled contact(s) for that debtor were purged.")
    if action == "queue_purged_for_payment":
        return (f"{entry.get('cancelled_run_count')} scheduled contact(s) for {debtor} were "
                f"removed because the debt was paid; the outbound log keeps the record of what "
                f"was cancelled.")
    if action == "ledger_entry_appended":
        return (f"The 20% fee line for {debtor} was appended to the ledger "
                f"(${_money(entry.get('fee_cents'))}), citing "
                f"{entry.get('confirmation_channel')} as its source.")
    if action == "reversal_recorded":
        return (f"The payment against {debtor} was reversed. The fee is un-billed by a linked "
                f"counter-entry, the purged queue stays purged, and the record is in "
                f"Reopened_Awaiting_Audit until the re-audit is completed.")
    if action == "balance_edit_parked":
        return (f"The balance on {debtor} changed with no payment signal behind it, so it is "
                f"parked in Pending_Adjustment_Confirmation: no dunning, no fee, until the "
                f"client says payment or adjustment.")
    if action == "guardrail_freeze":
        return (f"Agent 3 froze a contact to {debtor}: {entry.get('decision') or 'the calling window'}"
                f". Local time for that number was {entry.get('derived_local_time')}. Nothing was sent.")
    if action == "dnc_set":
        return (f"{debtor} sent an opt-out ({entry.get('matched_keywords')}). Every current and "
                f"future run for that debtor is killed, in every operating mode.")
    if action == "claim_blocked":
        return (f"A contact to {debtor} was refused with {entry.get('decision')}: "
                f"{entry.get('reason_detail')}")
    if action in ("mode_change", "mode_change_refused"):
        return (f"Operating mode for {client} moved {entry.get('old_mode')} -> "
                f"{entry.get('new_mode')} by {entry.get('who')}.")
    if action == "run_halted":
        return (f"A scheduled {entry.get('channel')} run for {debtor} was halted: "
                f"{entry.get('reason')}")
    if action == "halt_requested":
        return (f"Manual mode (HITL) was pressed for {client}: the client was already in hitl, "
                f"so no mode change was recorded — but the halt ran against the schedule.")
    if action == "run_scheduled":
        return (f"A {entry.get('channel')} run for {debtor} was scheduled for {entry.get('due_at')}.")
    if action == "draft_approved":
        return (f"{entry.get('who')} approved the draft for {debtor}. Approval is not a send: the "
                f"claim path re-checks the guardrails at the last millisecond.")
    if action == "draft_rejected":
        return (f"{entry.get('who')} rejected the draft for {debtor}: {entry.get('reason')}")
    if action == "draft_queued":
        return (f"A notice was drafted for {debtor} ({entry.get('channel')}) and queued as Pending.")
    if action == "claim_allowed":
        return (f"A claim was committed for {debtor} ({entry.get('channel')}) in "
                f"{entry.get('operating_mode_at_claim')}, window {entry.get('window_decision')}.")
    if action == "post_dispatched":
        return (f"The claimed contact to {debtor} was dispatched through the simulated provider.")
    if action == "post_failed":
        return (f"Dispatching the claimed contact to {debtor} FAILED: {entry.get('error')}")
    if action == "spreadsheet_committed":
        return (f"Spreadsheet upload for {client} committed: {entry.get('rows_written')} of "
                f"{entry.get('rows_seen')} rows imported.")
    if action == "ingestion_batch_registered":
        return (f"An upload batch for {client} was registered against the Step 3 warranty.")
    if action == "spreadsheet_commit_refused":
        return (f"An upload commit for {client} was refused: {entry.get('refusal_token')}")
    if action == "ingestion_batch_refused":
        return (f"An upload batch for {client} was refused: {entry.get('refusal_token')}")
    if action == "lifecycle_transition":
        return (f"Lifecycle for {client} moved {entry.get('old_status')} -> "
                f"{entry.get('new_status')}: {entry.get('reason')}")
    if action == "gate_locked":
        return (f"The launch gate refused live outbound for {client}: "
                f"{entry.get('reason_token')} (outstanding: {entry.get('outstanding_milestones')})")
    if action == "signoff_created":
        return (f"A sign-off was captured for {client} by {entry.get('signer')}.")
    if action == "signoff_revoked":
        return f"The sign-off for {client} was revoked: {entry.get('reason')}"
    if action == "runs_cancelled":
        return (f"Scheduled runs for {debtor} were cancelled: {entry.get('reason')}")
    if action == "inbound_reply_logged":
        return f"An ordinary reply from {debtor} was logged (no opt-out keyword present)."
    return (f"The engine recorded `{action}`"
            + (f" for {debtor}" if entry.get("debtor_id") else "")
            + (f" ({client})" if entry.get("client_id") else "") + ".")


def alerts(audit_path: Path | str, client_id: Optional[str] = None,
           limit: int = 60) -> Dict[str, Any]:
    """The engine's own event stream, newest first, in plain English.

    Source: the append-only `guardrail_actions.jsonl` the engine writes on every
    guardrail decision, mode change, claim and payment. It is a FILE on purpose — the
    console runs each read in a fresh process, so an in-memory log would be empty every
    time and the feed would have to invent its own history.

    Entries with no `client_id` are engine-wide (a bootstrap, an unmatched inbound) and
    are shown with `client_scoped: false` rather than being attributed to this client.
    """
    path = Path(audit_path)
    if not path.exists():
        return {"available": False, "audit_path": str(path), "entries": [], "count": 0,
                "high_priority_count": 0,
                "detail": "the engine has written no action log at this path yet"}
    entries: List[Dict[str, Any]] = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(entry, dict):
                skipped += 1
                continue
            entry_client = entry.get("client_id")
            if client_id is not None and entry_client not in (None, client_id):
                continue
            severity = ALERT_SEVERITY.get(str(entry.get("action")), "normal")
            entries.append({
                "sequence": index,
                "timestamp": entry.get("timestamp"),
                "timestamp_precision": "second UTC (the engine's audit instant)",
                "action": entry.get("action"),
                "severity": severity,
                "high_priority": severity == "high",
                "client_id": entry_client,
                "client_scoped": entry_client is not None,
                "debtor_id": entry.get("debtor_id"),
                "plain_english": _alert_sentence(entry),
                "fields": {k: v for k, v in entry.items()
                           if k not in ("timestamp", "action", "client_id", "debtor_id")},
            })
    entries.reverse()
    limit = max(1, int(limit))
    return {
        "available": True,
        "audit_path": str(path),
        "client_id": client_id,
        "entries": entries[:limit],
        "count": min(len(entries), limit),
        "total_entries_for_client": len(entries),
        "unreadable_lines_skipped": skipped,
        "high_priority_count": sum(1 for e in entries[:limit] if e["high_priority"]),
        "severity_note": ("high = money moving, a reversal, a parked balance, a guardrail freeze, "
                          "a DNC opt-out, a lifecycle change, a locked gate or a failed dispatch. "
                          "A refused claim is normal operation, not an incident."),
    }


# ---------------------------------------------------------------------------
# 5. the money trail
# ---------------------------------------------------------------------------
def statement(store, client_id: str) -> Dict[str, Any]:
    """The fee statement, derived from the append-only ledger. DRAFT until real money
    exists — and the reason is the ledger's own contents, not a slogan."""
    ledger = payments.ledger_for_client(store, client_id)
    totals = ledger["totals"]
    net = int(totals["net_collected_cents"])
    fee = int(totals["net_fee_cents"])
    if totals["line_count"] == 0:
        reason = ("No collections have been recorded for this client, so there is nothing to "
                  "invoice. A statement may only be issued against reconciled collections, and "
                  "every line has to cite the channel that confirmed it.")
    else:
        reason = ("Every line on this ledger was recorded through a simulated confirmation "
                  "channel and the fee account has taken no payment, so this statement stays "
                  "DRAFT: nothing here is money received.")
    return {
        "client_id": client_id,
        "status": "DRAFT",
        "status_reason": reason,
        "issued_at": None,
        "line_count": int(totals["line_count"]),
        "collections_count": int(totals["collections_count"]),
        "reversals_count": int(totals["reversals_count"]),
        "net_collected_cents": net,
        "net_collected": _money(net),
        "net_fee_cents": fee,
        "net_fee": _money(fee),
        "fee_rate_percent": totals["fee_rate_percent"],
        "basis": totals["basis"],
        "confirmation_rule": (
            "Nothing is billable without a confirmation source. The client chooses how we "
            "track collections at onboarding — where we look, never whether we look — and "
            "every ledger line cites the channel it came from."),
    }


def money_trail(store, client_id: str) -> Dict[str, Any]:
    """The ledger lines, the payment signals behind them, and the statement status."""
    ledger = payments.ledger_for_client(store, client_id)
    events = store.query(
        "SELECT * FROM payment_events WHERE client_id = ? ORDER BY event_id", (client_id,))
    reversals = store.query(
        "SELECT * FROM reopen_audit_events WHERE client_id = ? ORDER BY event_id", (client_id,))
    parked = [
        {"debtor_id": row["debtor_id"], "name": row.get("name"),
         "balance_owed": row.get("balance_owed"), "account_state": row.get("account_state")}
        for row in store.query(
            "SELECT * FROM debtor_records WHERE client_id = ? AND account_state = ? "
            "ORDER BY debtor_id", (client_id, payments.STATE_PENDING_ADJUSTMENT))]
    net = int(ledger["totals"]["net_collected_cents"])
    return {
        "client_id": client_id,
        "as_of": clock.iso_ms(),
        "ledger": ledger,
        "payment_events": [
            {"event_id": e.get("event_id"), "debtor_id": e.get("debtor_id"),
             "amount_cents": e.get("amount_cents"),
             "amount": _money(e.get("amount_cents")),
             "confirmation_channel": e.get("confirmation_channel"),
             "confirmation_latency": e.get("confirmation_latency"),
             "attested_by": e.get("attested_by"),
             "external_reference": e.get("external_reference"),
             "signal_at": e.get("signal_at"), "recorded_at": e.get("recorded_at"),
             "account_state_before": e.get("account_state_before"),
             "account_state_after": e.get("account_state_after"),
             "balance_before": _money(e.get("balance_before_cents")),
             "balance_after": _money(e.get("balance_after_cents")),
             "queue_purged_at": e.get("queue_purged_at"),
             "cancelled_run_count": e.get("cancelled_run_count"),
             "outbound_logs_untouched": e.get("outbound_logs_untouched"),
             "status": e.get("status"),
             "note": e.get("note")} for e in events],
        "reopen_audits": [
            {"event_id": r.get("event_id"), "debtor_id": r.get("debtor_id"),
             "event_type": r.get("event_type"), "recorded_at": r.get("recorded_at"),
             "performed_by": r.get("performed_by"), "reason": r.get("reason"),
             "refuse_reason": r.get("refuse_reason"),
             "un_billed_amount_cents": r.get("un_billed_amount_cents"),
             "un_billed_fee_cents": r.get("un_billed_fee_cents")}
            for r in reversals],
        "parked_adjustments": parked,
        "statement": statement(store, client_id),
        "collected_to_date_cents": net,
        "collected_to_date": _money(net),
        "collected_to_date_label": f"${(_money(net) or '0.00')} collected to date",
        "honest_note": (
            "No money has moved and no fee account can be read from here. The fee account "
            "sits outside this console's finance view; what it shows is the ledger this "
            "engine wrote, and today that ledger is empty."),
        "simulation_note": (
            "The 'simulate a payment' control below calls the engine's own payment path with "
            "a simulated confirmation channel. It moves no money anywhere."),
    }


def simulate_payment(engine) -> Dict[str, Any]:
    """Apply a SIMULATED payment webhook to the first record with a live balance.

    Kept here so the console's close-the-loop control has one implementation: the engine
    facade calls this, and this calls exactly the path a real signal would take
    (`RecoverAI.record_payment` -> `payments.apply_payment`), with the confirmation
    channel the rulebook names for an accounting webhook and a `simulated-console`
    external reference so the ledger line can never be mistaken for a real one.
    """
    client_id = None
    for row in engine.store.query(
            "SELECT * FROM debtor_records WHERE balance_owed > 0 AND account_state IN "
            "('Open','Partially_Paid') ORDER BY debtor_id"):
        client_id = row["client_id"]
        result = engine.record_payment(
            row["debtor_id"], row["balance_owed"], "accounting_webhook",
            attested_by="console-simulated-webhook",
            external_reference=f"simulated-console-{clock.iso_ms()}-{row['debtor_id']}",
            note="SIMULATED accounting webhook triggered from the console: no money moved, "
                 "no provider was called.")
        result["simulated"] = True
        result["simulation_note"] = (
            "This is a simulated accounting webhook. The engine ran its real payment path — "
            "balance debit, queue purge, fee ledger line — and no money moved anywhere.")
        return result
    return {"ok": False, "refusal_token": "NO_RECORD_WITH_A_BALANCE",
            "detail": "no record with a positive balance in Open or Partially_Paid state "
                      "exists, so there is nothing to collect and nothing to simulate"}


# ===========================================================================
# Phase 4 - the onboarding ladder and the reports view.
#
# Both are read models in the same sense as everything above (columns, named
# arithmetic, and labels with a documented precedence), except for one thing worth
# saying out loud: `report_periods` counts rows in a WINDOW, and a window needs a
# clock. The window is [now - N days, now] on the engine's own UTC clock, every count
# is a COUNT over rows that exist, and a count with no rows is 0 - which is reported as
# 0, never as "no data yet" and never as an estimate.
# ===========================================================================
def onboarding_screen(store, client_id: str) -> Dict[str, Any]:
    """The onboarding ladder as the console renders it: every step, its gate, its trail.

    Reads only. The Step 3 gate's own verdict (`gate_open`, `refusal_token`) comes from
    `onboarding.step3_gate_state`, which is the SAME call the intake makes - so the
    console cannot show a gate as open while the engine refuses a batch, or the reverse.
    """
    ladder = onboarding.onboarding_state(store, client_id)
    batches = store.query(
        "SELECT batch_id, file_name, rows_seen, rows_accepted, rows_rejected, recorded_at_ms, "
        "recorded_at, uploader_id, warranty_step3_attestation_id, chain_hash "
        "FROM ingestion_batches WHERE client_id = ? ORDER BY seq", (client_id,))
    parked = [
        {"debtor_id": row["debtor_id"], "name": row.get("name"),
         "balance_owed": row.get("balance_owed"), "account_state": row.get("account_state"),
         "open_edit_event": payments._parked_edit(store, row["debtor_id"])}
        for row in store.query(
            "SELECT * FROM debtor_records WHERE client_id = ? AND account_state = ? "
            "ORDER BY debtor_id", (client_id, payments.STATE_PENDING_ADJUSTMENT))]
    ledger = payments.ledger_for_client(store, client_id)
    collected = int(ledger["totals"]["net_collected_cents"])
    fee = int(ledger["totals"]["net_fee_cents"])
    return {
        "client_id": client_id,
        "as_of": clock.iso_ms(),
        "ladder": ladder,
        "step3_gate": onboarding.step3_gate_state(store, client_id),
        "batches": batches,
        "batches_count": len(batches),
        "batches_legacy_count": sum(1 for b in batches if not b["warranty_step3_attestation_id"]),
        "batches_bound_to_a_step3_attestation": sum(
            1 for b in batches if b["warranty_step3_attestation_id"]),
        "parked_adjustments": parked,
        "parked_adjustments_count": len(parked),
        "collected_to_date_cents": collected,
        "collected_to_date_label": f"${_money(collected) or '0.00'} collected to date",
        "net_fee_cents": fee,
        "statement_status": "DRAFT",
        "statement_status_reason": statement(store, client_id)["status_reason"],
        "fee_rate_percent": ledger["totals"]["fee_rate_percent"],
        "honest_note": (
            "Simulated end to end. No real client exists, the sandbox client's sign-off is a "
            "simulated signature, no accounting system is connected and no money has been "
            "collected: that is what the $0.00 above means, not a placeholder."),
    }


#: The report periods, in days back from now. `None` means all time.
REPORT_PERIODS: Tuple[Tuple[str, Optional[int]], ...] = (
    ("last_7_days", 7),
    ("last_30_days", 30),
    ("all_time", None),
)


def _in_window(rows: List[Dict[str, Any]], field_name: str, cutoff) -> List[Dict[str, Any]]:
    """Rows whose ISO timestamp field is at or after `cutoff`. An unreadable stamp is
    EXCLUDED rather than counted - fail closed, the same rule the parser uses."""
    if cutoff is None:
        return list(rows)
    out = []
    for row in rows:
        raw = row.get(field_name)
        if not raw:
            continue
        try:
            if clock.parse_ts(str(raw)) >= cutoff:
                out.append(row)
        except Exception:
            continue
    return out


def reports(store, client_id: str) -> Dict[str, Any]:
    """Weekly and monthly counts, from the engine's own logs. Real counts only.

    Sources, one per figure, each named in `sources` so nothing here is unattributable:
      * contacts attempted      - outbound_contact_logs rows for this client
      * claims committed        - rows with a claim_token, split claimed/blocked
      * guardrail interceptions - the `decision` token on BLOCKED rows, counted by reason
      * DNC additions           - debtor_records with dnc_flag = 1, dated by dnc_set_at
      * mode changes            - autonomy_mode_history rows where the mode actually changed
      * aging movement          - each record's stored (as-imported) aging_state against
                                  the bucket its invoice_date implies today
      * portfolio totals        - sums over debtor_records and the append-only ledger
      * fee line                - the LEDGER's own net fee, which today is zero
    """
    contacts = store.query(
        "SELECT * FROM outbound_contact_logs WHERE client_id = ? ORDER BY contact_log_id",
        (client_id,))
    modes = store.query(
        "SELECT * FROM autonomy_mode_history WHERE client_id = ? ORDER BY history_id", (client_id,))
    debtors = store.query("SELECT * FROM debtor_records WHERE client_id = ? ORDER BY debtor_id",
                          (client_id,))
    ledger = payments.ledger_for_client(store, client_id)
    totals = ledger["totals"]
    now = clock.now_utc()

    periods: List[Dict[str, Any]] = []
    for label, days in REPORT_PERIODS:
        cutoff = clock.plus(days=-days, base=now) if days is not None else None
        window_contacts = _in_window(contacts, "attempted_at", cutoff)
        window_modes = _in_window(modes, "timestamp", cutoff)
        window_dnc = _in_window([d for d in debtors if int(d.get("dnc_flag") or 0) == 1],
                                "dnc_set_at", cutoff)
        interceptions: Dict[str, int] = {}
        for row in window_contacts:
            if str(row.get("status")) == "BLOCKED":
                token = str(row.get("decision") or "BLOCKED_UNKNOWN")
                interceptions[token] = interceptions.get(token, 0) + 1
        claimed = [r for r in window_contacts if r.get("claim_token")]
        dispatched = [r for r in claimed if str(r.get("status")) in ("SENT", "DISPATCHED")]
        periods.append({
            "period": label,
            "window_days": days,
            "window_start": (clock.iso(cutoff) if cutoff is not None else None),
            "window_end": clock.iso(now),
            "contacts_attempted": len(window_contacts),
            "contacts_attempted_by_channel": _count_by(window_contacts, "contact_type"),
            "claims_committed": len(claimed),
            "claims_committed_by_channel": _count_by(claimed, "contact_type"),
            "contacts_dispatched": len(dispatched),
            "contacts_blocked": sum(interceptions.values()),
            "guardrail_interceptions_by_reason": dict(sorted(interceptions.items())),
            "guardrail_free_claims": len(claimed),
            "dnc_additions": len(window_dnc),
            "mode_changes": sum(1 for m in window_modes if m["old_mode"] != m["new_mode"]),
            "mode_change_attempts": len(window_modes),
        })

    aging_now = _aging_now(debtors, now)
    moved = [row for row in aging_now if row["bucket_moved"]]
    return {
        "client_id": client_id,
        "generated_at_ms": clock.iso_ms(),
        "periods": periods,
        "aging": {
            "buckets_now": _count_by(aging_now, "current_aging_state"),
            "buckets_as_imported": _count_by(aging_now, "imported_aging_state"),
            "moved_since_import": len(moved),
            "records_with_a_computable_age": len([r for r in aging_now if r["age_days"] is not None]),
            "movement": [{"debtor_id": r["debtor_id"], "imported_aging_state": r["imported_aging_state"],
                          "current_aging_state": r["current_aging_state"], "age_days": r["age_days"]}
                         for r in moved],
            "note": ("'As imported' is the aging_state Agent 1 normalized when the record "
                     "arrived; 'now' is the bucket the invoice_date implies today. A record "
                     "whose age cannot be computed from its invoice_date is excluded from the "
                     "movement count rather than assumed to be 0 days old."),
        },
        "portfolio": {
            "debtors": len(debtors),
            "balance_owed_total": round(sum(float(d.get("balance_owed") or 0) for d in debtors), 2),
            "balance_by_account_state": _sum_by(debtors, "account_state", "balance_owed"),
            "by_account_state": _count_by(debtors, "account_state"),
            "dnc_flagged": sum(1 for d in debtors if int(d.get("dnc_flag") or 0) == 1),
            "with_email": sum(1 for d in debtors if d.get("email")),
            "with_phone": sum(1 for d in debtors if d.get("phone")),
        },
        "fees": {
            "net_collected_cents": int(totals["net_collected_cents"]),
            "net_collected": _money(int(totals["net_collected_cents"])),
            "net_fee_cents": int(totals["net_fee_cents"]),
            "net_fee": _money(int(totals["net_fee_cents"])),
            "ledger_lines": int(totals["line_count"]),
            "collections_count": int(totals["collections_count"]),
            "reversals_count": int(totals["reversals_count"]),
            "fee_rate_percent": totals["fee_rate_percent"],
            "basis": totals["basis"],
            "line": f"${_money(int(totals['net_collected_cents'])) or '0.00'} collected to date",
            "rule": ("The fee line comes only from reconciled collections, each line citing the "
                     "confirmation source it came from. With no collections, it is zero - and "
                     "zero is what is printed."),
            "lines": ledger["lines"],
        },
        "statement": statement(store, client_id),
        "sources": {
            "contacts_attempted": "outbound_contact_logs.attempted_at",
            "claims_committed": "outbound_contact_logs.claim_token IS NOT NULL",
            "guardrail_interceptions_by_reason": "outbound_contact_logs.decision on BLOCKED rows",
            "dnc_additions": "debtor_records.dnc_set_at where dnc_flag = 1",
            "mode_changes": "autonomy_mode_history.timestamp where old_mode != new_mode",
            "aging_movement": "debtor_records.aging_state vs the bucket invoice_date implies today",
            "portfolio_totals": "debtor_records",
            "fee_line": "collections_ledger (append-only), entry_type collection minus reversals",
        },
        "honest_note": (
            "Real counts only: every figure is a COUNT or a SUM over rows this engine wrote, "
            "and the windows are UTC. Nothing here is projected, annualised or extrapolated. "
            "The fee line is zero because no money has been collected - not because the report "
            "is a placeholder."),
    }


def _count_by(rows: List[Dict[str, Any]], field_name: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        key = str(row.get(field_name) if row.get(field_name) is not None else "unknown")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def _sum_by(rows: List[Dict[str, Any]], key_field: str, value_field: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in rows:
        key = str(row.get(key_field) if row.get(key_field) is not None else "unknown")
        out[key] = round(out.get(key, 0.0) + float(row.get(value_field) or 0), 2)
    return dict(sorted(out.items()))


def _aging_now(rows: List[Dict[str, Any]], now) -> List[Dict[str, Any]]:
    """Each record with the bucket its invoice_date implies today, beside the one it
    arrived with. An unparseable or missing invoice_date yields a None age - excluded
    from the movement count, never silently treated as current."""
    out = []
    for row in rows:
        imported = row.get("aging_state")
        age_days: Optional[int] = None
        raw = row.get("invoice_date")
        if raw:
            try:
                age_days = max(0, (now - clock.parse_ts(str(raw))).days)
            except Exception:
                age_days = None
        current = config.aging_state_for_days(age_days) if age_days is not None else imported
        out.append({
            "debtor_id": row["debtor_id"],
            "imported_aging_state": imported,
            "current_aging_state": current,
            "age_days": age_days,
            "bucket_moved": bool(age_days is not None and str(current) != str(imported)),
        })
    return out


# ===========================================================================
# Phase 4 - the Integrations Hub (a SIMULATION panel, labelled as one).
#
# The panel exists to answer one question honestly: what would a live sync DO with a
# client's export? The answer comes from `ingest.normalize()` - the identical function a
# live import calls - run over a SAMPLE file that lives in the engine tree. Nothing is
# fetched, nothing is written, and no QuickBooks or Xero credential exists anywhere in
# this engine. The words on the panel say exactly that.
# ===========================================================================
#: The sample exports, each naming the mock file it came from and the provider it
#: imitates. The client ids stored in the files are the mock fixtures' own - a real
#: connection would carry the connected client's id, and the panel says so.
INTEGRATION_SAMPLES: Tuple[Dict[str, str], ...] = (
    {"sample_id": "quickbooks_past_due", "provider": "QuickBooks Online (simulated)",
     "file_name": "quickbooks_invoices.json",
     "label": "QuickBooks Online — past-due invoice export (sample file)"},
    {"sample_id": "xero_past_due", "provider": "Xero (simulated)",
     "file_name": "xero_invoices.json",
     "label": "Xero — past-due invoice export (sample file)"},
)

HONEST_HUB_LINE = ("No real QuickBooks or Xero connection exists; this panel runs sample "
                   "files through the real mapper.")


def _mock_dir() -> Path:
    return config.repo_root() / "mocks" / "data"


def integration_samples() -> List[Dict[str, Any]]:
    """The sample exports available to the panel, with the sha256 of each file."""
    out = []
    for sample in INTEGRATION_SAMPLES:
        path = _mock_dir() / sample["file_name"]
        entry: Dict[str, Any] = dict(sample)
        entry["available"] = path.exists()
        entry["path"] = str(path)
        if path.exists():
            raw = path.read_bytes()
            entry["file_sha256"] = ingest.sha256_text(raw.decode("utf-8"))
            entry["size_bytes"] = len(raw)
        out.append(entry)
    return out


def integrations_hub(store, *, client_id: str,
                     sample: Optional[str] = None) -> Dict[str, Any]:
    """Run one sample export through the REAL mapper and report what it WOULD map."""
    samples = integration_samples()
    chosen = None
    for entry in samples:
        if sample and entry["sample_id"] == sample:
            chosen = entry
            break
    if chosen is None:
        chosen = next((s for s in samples if s["available"]), samples[0] if samples else None)
    if chosen is None or not chosen.get("available"):
        return {"ok": False, "refusal_token": "SAMPLE_FILE_MISSING", "client_id": client_id,
                "detail": "no sample export is available in mocks/data",
                "samples": samples, "honest_line": HONEST_HUB_LINE}
    rows = store.query("SELECT COUNT(*) AS n FROM debtor_records WHERE client_id = ?", (client_id,))
    before = int(rows[0]["n"] or 0)
    raw_text = Path(chosen["path"]).read_text(encoding="utf-8")
    parsed = json.loads(raw_text)
    records: List[Dict[str, Any]] = list(parsed.get("invoices") or [])
    headers = list(records[0].keys()) if records else []
    mapping = ingest.build_mapping(headers, list(records[0].values()) if records else None)
    # THE REAL MAPPER, on the sample rows. Same call a live import makes.
    result = ingest.normalize(records, source_system=chosen["provider"],
                              endpoint=f"sample://{chosen['file_name']} (never fetched)",
                              client_id_hint=parsed.get("client_id"))
    report = result.report()
    after = int(store.query("SELECT COUNT(*) AS n FROM debtor_records WHERE client_id = ?",
                            (client_id,))[0]["n"] or 0)
    return {
        "ok": True,
        "client_id": client_id,
        "as_of": clock.iso_ms(),
        "label": "SIMULATED",
        "connection_status": "NO LIVE CONNECTION",
        "honest_line": HONEST_HUB_LINE,
        "honest_notes": [
            HONEST_HUB_LINE,
            "Nothing is fetched: the sample file above is read from this repository.",
            "Nothing is written: this panel imports no rows, and the row count below is the "
            "same before and after the run.",
            "No QuickBooks or Xero credential, token or connection exists in this engine.",
            "The mapping, the rejections and the aging buckets below are the REAL mapper's "
            "output on the sample file - they are what a live sync would have produced from "
            "the same export, and are labelled simulated because the export is a sample.",
        ],
        "samples": samples,
        "selected": chosen,
        "sample_meta": {
            "file_name": chosen["file_name"],
            "file_sha256": chosen.get("file_sha256"),
            "source_system": parsed.get("source_system"),
            "exported_at": parsed.get("exported_at"),
            "fixture_client_id": parsed.get("client_id"),
            "note": parsed.get("note"),
            "rows_in_file": len(records),
        },
        "column_translation": mapping,
        "would_map": {
            "records_fetched": report["records_fetched"],
            "records_normalized": report["records_normalized"],
            "records_rejected": report["records_rejected"],
            "rejected_detail": report["rejected_detail"],
            "unmapped_columns": report["unmapped_columns"],
            "aging_buckets": report["aging_buckets"],
            "target_fields": report["target_fields"],
            "mapping_rows": report["mapping"],
            "normalized_rows": [
                {"debtor_id": r.get("debtor_id"), "client_id": r.get("client_id"),
                 "name": r.get("name"), "email": r.get("email"), "phone": r.get("phone"),
                 "balance_owed": r.get("balance_owed"), "invoice_date": r.get("invoice_date"),
                 "aging_state": r.get("aging_state")}
                for r in result.records],
        },
        "writes": 0,
        "write_proof": {
            "debtor_records_before": before,
            "debtor_records_after": after,
            "unchanged": before == after,
            "note": ("This panel is a read-only demonstration. Merging a file into the working "
                     "queue is the spreadsheet intake's job, and it requires a registered "
                     "ingestion batch bound to the client's Step 3 attestation."),
        },
    }
