"""Inbound reply handling - the global DNC opt-out.

Rule (rulebook, not a constant): any inbound string containing STOP, Unsubscribe or
Stop calling, case-insensitive substring match, instantly sets that debtor's
dnc_flag = TRUE and kills every current and future scheduled run for that debtor.

The kill has two halves and both matter:
  * scheduled runs are cancelled in the run queue (they will never be dispatched), and
  * any run that somehow survives still cannot fire, because the claim transaction
    re-reads dnc_flag before it writes a claim.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import clock, config
from .audit import AuditLog
from .runqueue import RunQueue

CONTENT_KEYS = ("content", "message", "text", "Body", "body", "reply", "transcript")


def matched_keywords(text: Optional[str]) -> List[str]:
    if not text:
        return []
    haystack = str(text).lower()
    return [kw for kw in config.opt_out_keywords() if kw.lower() in haystack]


def is_opt_out(text: Optional[str]) -> bool:
    return bool(matched_keywords(text))


def _digits(phone: Optional[str]) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())[-10:]


def resolve_debtor(store, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    debtor_id = payload.get("debtor_id") or payload.get("debtorId")
    if debtor_id:
        row = store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
        if row:
            return row
    from_phone = payload.get("from") or payload.get("From") or payload.get("phone")
    digits = _digits(from_phone)
    if digits:
        for row in store.query("SELECT * FROM debtor_records"):
            if _digits(row.get("phone")) == digits:
                return row
    return None


def process_inbound(store, payload: Dict[str, Any], audit: Optional[AuditLog] = None,
                    runqueue: Optional[RunQueue] = None) -> Dict[str, Any]:
    content = None
    for key in CONTENT_KEYS:
        if payload.get(key) is not None:
            content = payload[key]
            break
    matched = matched_keywords(content)
    debtor = resolve_debtor(store, payload)

    if debtor is None:
        result = {"processed": False, "reason": "inbound reply does not match a known debtor",
                  "matched_keywords": matched, "content": content}
        if audit:
            audit.record("guardrail_freeze" if matched else "claim_blocked",
                         decision="INBOUND_UNMATCHED_DEBTOR", content=content, matched_keywords=matched)
        return result

    debtor_id = debtor["debtor_id"]
    if not matched:
        result = {"processed": True, "debtor_id": debtor_id, "dnc_set": False,
                  "matched_keywords": [], "content": content,
                  "reason": "ordinary reply; no opt-out keyword present"}
        if audit:
            audit.record("inbound_reply_logged", debtor_id=debtor_id, client_id=debtor["client_id"],
                         content=content)
        return result

    now = clock.iso()
    with store.tx(immediate=True):
        # The instant is taken INSIDE the transaction, after the write lock is held: the
        # opt-out's recorded instant is therefore the instant the halt was recorded, not an
        # instant read before the write might have to wait for another transaction. It is
        # millisecond UTC like the rest of the audit trail, so "which of these two landed
        # first" is a question the record can actually answer.
        halt_at = clock.iso_ms()
        store.execute(
            "UPDATE debtor_records SET dnc_flag = 1, dnc_set_at = ? WHERE debtor_id = ?", (halt_at, debtor_id))
    cancelled: List[Dict[str, Any]] = []
    if runqueue is not None:
        cancelled = [r.as_dict() for r in runqueue.cancel_for_debtor(debtor_id, "global DNC opt-out")]

    result = {
        "processed": True,
        "debtor_id": debtor_id,
        "client_id": debtor["client_id"],
        "content": content,
        "matched_keywords": matched,
        "dnc_set": True,
        "dnc_flag": 1,
        "dnc_set_at": halt_at,
        "cancelled_runs": cancelled,
        "runs_cancelled": len(cancelled),
        "reason": f"opt-out keyword(s) {matched} matched in inbound reply; all runs for this debtor killed",
    }
    if audit:
        audit.record("dnc_set", debtor_id=debtor_id, client_id=debtor["client_id"], content=content,
                     matched_keywords=matched, dnc_set_at=halt_at)
        if cancelled:
            audit.record("runs_cancelled", debtor_id=debtor_id, client_id=debtor["client_id"],
                         run_ids=[r["run_id"] for r in cancelled], reason="global DNC opt-out")
    return result
