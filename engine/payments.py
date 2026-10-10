"""One payment event, three consequences, one transaction.

A payment signal arrives from one of three channels - an accounting webhook, a daily
delta sync, or a manual attestation by the client. Whichever carried it, ONE exclusive
transaction (BEGIN IMMEDIATE on SQLite, BEGIN + row lock on Postgres) does all three of:

  (a) debit the balance and set the debtor's payment state - `Paid` at zero,
      `Partially_Paid` while a remainder exists;
  (b) destructively purge every pending and scheduled contact attempt for that debtor
      from the active runs queue - the queue entries are removed, the immutable
      `outbound_contact_logs` history is untouched, and the cancellation (what, and at
      what instant) is written to `payment_events`;
  (c) append exactly one immutable `collections_ledger` row carrying the flat 20% fee
      accrued on the funds actually confirmed collected, and the channel that confirmed
      them.

If any leg fails nothing commits. The three legs are separate statements inside the one
transaction, and there is exactly one commit point at the end of it - the rollback test
injects a failure into leg (c) and asserts the balance, the state and the queue are all
exactly as they were.

Money is NEVER a float arithmetic path: amounts and fees are integer cents. The single
rounding rule is "half up to the nearest cent", applied in integer arithmetic:

    fee_cents = (abs(amount_cents) * fee_basis_points + 5000) // 10000

with fee_basis_points = 2000 (20%), read from config/rulebook.json at call time. The
same expression is a CHECK constraint on collections_ledger, so the store refuses a fee
row that does not match the stated rate and rule - belt and braces.

What is billable, precisely:

  * the fee is 20% of funds actually collected and confirmed. Never a retainer, never a
    setup fee, never a per-call charge, never a tier. Never on an invoice, a contact or
    a promise - only on money that arrived.
  * a line without a `confirmation_channel` cannot exist: the column is NOT NULL and
    CHECKed against the three channels the rulebook names.
  * an unconfirmed balance edit is NEVER billable. That is the standing default pending
    the owner's ruling (see UNCONFIRMED_ADJUSTMENT_BILLABLE below).
  * a reversal (bounce/refund) writes a linked credit counter-entry; the original row is
    never edited, so net statement value = original + reversal.
  * a reversal is not a resume: it puts the record in Reopened_Awaiting_Audit, and nothing
    can be claimed from it until the record has re-run the ingestion path from step one
    and the completed re-audit is recorded (see run_reopen_audit below).
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from . import clock, config, ingest, timezones
from .audit import AuditLog
from .runqueue import RunQueue

# --- confirmation channels --------------------------------------------------
#: The only sources that can make money billable. Names are stable: they are stored on
#: every ledger line and every payment event.
CHANNEL_ACCOUNTING_WEBHOOK = "accounting_webhook"
CHANNEL_DELTA_SYNC = "delta_sync"
CHANNEL_MANUAL_ATTESTATION = "manual_attestation"
CONFIRMATION_CHANNELS = (CHANNEL_ACCOUNTING_WEBHOOK, CHANNEL_DELTA_SYNC, CHANNEL_MANUAL_ATTESTATION)

# --- debtor payment states --------------------------------------------------
STATE_OPEN = "Open"
STATE_PARTIALLY_PAID = "Partially_Paid"
STATE_PAID = "Paid"
STATE_PENDING_ADJUSTMENT = "Pending_Adjustment_Confirmation"

#: Where a reversal puts a record that was Paid or Partially_Paid. A MANDATORY lifecycle
#: state, not a cosmetic flag: while a record is here (or while an opened reopen audit is
#: still open) the send claim refuses every channel with the reason token
#: `REOPENED_AWAITING_AUDIT`, and the only way out is a completed reopen audit - the record
#: re-runs the ingestion path from step one and that completion is recorded. See
#: `run_reopen_audit()` below and docs/gate.md.
STATE_REOPENED_AWAITING_AUDIT = "Reopened_Awaiting_Audit"

#: Declared for the dispute interceptor (Agent 4, not built yet - the state cannot be stored
#: today because debtor_records.account_state does not accept it). It is named here because
#: the precedence rule below must already cover it: a reversal never exits a hold it finds
#: the record in, and a reversal must not be the thing that quietly releases a dispute.
STATE_DISPUTE_HOLD = "Dispute_Hold"

#: States a reversal (and the reopen audit's state write) must NEVER exit on their own. The
#: reversal still un-bills by linked counter-entry while the record stays exactly where the
#: human put it; a dispute hold is released by a human, a parked balance edit by its client
#: resolution. Anything else the engine would be ending a hold with a payment signal.
HOLD_STATES = (STATE_PENDING_ADJUSTMENT, STATE_DISPUTE_HOLD)


def is_hold_state(state: Any) -> bool:
    return str(state or "") in HOLD_STATES


# --- the reopen audit -------------------------------------------------------
#: Append-only event types in `reopen_audit_events`. The first row is written by the reversal
#: itself, inside the same transaction that un-bills; the second closes it; the third records
#: a re-audit the engine refused to complete (fail closed: the record stays where it is).
REOPEN_AUDIT_OPENED = "reversal_opened_audit"
REOPEN_AUDIT_COMPLETED = "reaudit_completed"
REOPEN_AUDIT_REFUSED = "reaudit_refused"
REOPEN_AUDIT_EVENT_TYPES = (REOPEN_AUDIT_OPENED, REOPEN_AUDIT_COMPLETED, REOPEN_AUDIT_REFUSED)

# --- rounding ---------------------------------------------------------------
#: The one rounding rule, named so the ledger can carry it on every line.
FEE_ROUNDING_RULE = "half_up_to_cent"

#: An unconfirmed adjustment is never billable. This is the team's standing default
#: (the business plan's recommendation) and it stays until the owner rules otherwise.
#: Flipping it to True would need the owner's explicit decision and would make money
#: billable on a balance the client never attributed to a payment - do not.
UNCONFIRMED_ADJUSTMENT_BILLABLE = False

REVERSAL_KINDS = ("bounced", "refunded")


class PaymentRefused(ValueError):
    """A payment signal the engine will not accept. Loud, and nothing is written."""


class ReopenAuditRefused(PaymentRefused):
    """A reopen audit the engine will not complete.

    Fail-closed: the record stays in `Reopened_Awaiting_Audit` (or in the hold it was already
    in) and stays un-claimable, and the refusal itself is recorded on the append-only audit
    log. Nothing is re-normalized, nothing is invented and no contact data is guessed at.
    """


# ---------------------------------------------------------------------------
# money helpers - integer cents only
# ---------------------------------------------------------------------------
def to_cents(amount: Any) -> int:
    """Exact integer cents for a currency amount, rounding half up.

    Decimal(str(...)) is used deliberately: str() of a float gives the shortest decimal
    that round-trips, so 4820.50 does not become 482049.99999999994. The engine never
    does arithmetic on money as a float.
    """
    if amount is None:
        raise PaymentRefused("no amount given")
    return int(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def from_cents(cents: Any) -> float:
    """The currency amount for a cent value. Display only; cents remain authoritative."""
    return float(Decimal(int(cents)) / Decimal(100))


def fee_basis_points() -> int:
    return int(config.contingency_fee().get("basis_points", 2000))


def fee_rate_percent() -> float:
    return fee_basis_points() / 100.0


def fee_for(amount_cents: int, basis_points: Optional[int] = None) -> int:
    """Flat contingency fee on funds actually collected: 20%, half up to the cent.

    (|cents| * bp + 5000) // 10000 is exactly |cents| * (bp/10000) rounded half up:
    adding half of the denominator before integer division is the half-up rule, and
    10000 // 2 == 5000. No float is involved anywhere, so the fee for 1820.53 is
    exactly 36411 cents (182053 * 2000 = 364106000; + 5000 -> 364111000; // 10000 ->
    36411), not 364.10599999999994 of anything.
    """
    bp = fee_basis_points() if basis_points is None else int(basis_points)
    return (abs(int(amount_cents)) * bp + 5000) // 10000


def confirmation_latency(confirmation_channel: str) -> str:
    """How far behind reality this channel can be - stated on every ledger line.

    Latency is a property of how we found out, so it is recorded per line rather than
    smoothed over: a webhook is near-instant, a daily delta sync can be up to a day
    behind, and an attestation is whenever the client acted.
    """
    channels = config.contingency_fee().get("confirmation_channels", {})
    entry = channels.get(confirmation_channel) or {}
    return str(entry.get("latency") or "unknown")


def confirmation_label(confirmation_channel: str) -> str:
    channels = config.contingency_fee().get("confirmation_channels", {})
    entry = channels.get(confirmation_channel) or {}
    return str(entry.get("label") or confirmation_channel)


# ---------------------------------------------------------------------------
# small readers
# ---------------------------------------------------------------------------
def _debtor_row(store, debtor_id: str) -> Dict[str, Any]:
    row = store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
    if not row:
        raise KeyError(f"unknown debtor {debtor_id}")
    return row


def net_collected_cents(store, debtor_id: str) -> int:
    """Collections plus reversals for one debtor. 0 means nothing is confirmed collected."""
    row = store.query_one(
        "SELECT COALESCE(SUM(amount_cents), 0) AS net FROM collections_ledger WHERE debtor_id = ?",
        (debtor_id,))
    return int(row["net"] or 0)


def derive_account_state(balance_cents: int, net_collected: int) -> str:
    """The payment state that follows from the balance and what is confirmed collected.

    Paid            - zero owed
    Partially_Paid  - something is confirmed collected and a remainder is still owed
    Open            - a remainder is owed and nothing is confirmed collected (a reversal
                      that un-billed the only payment leaves the sequence wide open again)
    """
    if balance_cents <= 0:
        return STATE_PAID
    return STATE_PARTIALLY_PAID if net_collected > 0 else STATE_OPEN


def _validate_signal(confirmation_channel: str, attested_by: Optional[str]) -> None:
    if confirmation_channel not in CONFIRMATION_CHANNELS:
        raise PaymentRefused(
            f"confirmation channel {confirmation_channel!r} is not one of {list(CONFIRMATION_CHANNELS)}: "
            "a payment is only billable from a named confirmation source")
    if confirmation_channel == CHANNEL_MANUAL_ATTESTATION and not str(attested_by or "").strip():
        raise PaymentRefused(
            "a manual attestation must name who attested it: the client is the confirmation "
            "source for that channel, so an anonymous attestation is refused")


def _already_recorded(store, debtor_id: str, external_reference: str) -> Optional[Dict[str, Any]]:
    return store.query_one(
        "SELECT * FROM collections_ledger WHERE debtor_id = ? AND external_reference = ?",
        (debtor_id, external_reference))


# ---------------------------------------------------------------------------
# the write legs (each one is a separate function so a test can fail one of them)
# ---------------------------------------------------------------------------
def _set_balance(store, debtor_id: str, balance_cents: int, account_state: str) -> None:
    """Leg (a): the balance and the payment state, in cents, in one statement."""
    store.execute(
        """UPDATE debtor_records SET balance_owed = ?, account_state = ?, balance_updated_at = ?
           WHERE debtor_id = ?""",
        (from_cents(balance_cents), account_state, clock.iso(), debtor_id),
    )


def _run_purge(runqueue: Optional[RunQueue], debtor_id: str, reason: str) -> tuple:
    """Leg (b) helper: destructively remove pending/scheduled attempts for this debtor.

    Returns (removed, purged_at). The caller restores `removed` if the transaction rolls
    back, so a failed ledger write cannot cost us the schedule.
    """
    if runqueue is None:
        return [], None
    removed = runqueue.purge_for_debtor(debtor_id, reason)
    return removed, clock.iso()


def _insert_payment_event(store, **fields: Any) -> int:
    """The intake record: the signal, the state, the fee line and the cancellations."""
    event_id = store.insert(
        """INSERT INTO payment_events
             (client_id, debtor_id, confirmation_channel, amount_cents, currency, attested_by,
              external_reference, signal_at, confirmation_latency, recorded_at,
              balance_before_cents, balance_after_cents, overpayment_cents,
              account_state_before, account_state_after, intake_reference, queue_purged_at,
              cancelled_runs, cancelled_run_count, outbound_logs_untouched, status, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fields["client_id"], fields["debtor_id"], fields["confirmation_channel"],
         fields["amount_cents"], fields.get("currency") or "USD", fields.get("attested_by"),
         fields.get("external_reference"), fields.get("signal_at"), fields["confirmation_latency"],
         fields["recorded_at"], fields["balance_before_cents"], fields["balance_after_cents"],
         fields.get("overpayment_cents", 0), fields.get("account_state_before"),
         fields["account_state_after"], fields["intake_reference"], fields.get("queue_purged_at"),
         json.dumps(fields.get("cancelled_runs") or []), len(fields.get("cancelled_runs") or []),
         fields.get("outbound_logs_untouched"), fields.get("status", "COMMITTED"),
         fields.get("note")),
    )
    return event_id


def _insert_ledger_row(store, **fields: Any) -> int:
    """Leg (c): append exactly one immutable ledger line.

    Every field the read model needs is on the row: the amount collected, the fee, the
    rate and rounding rule that produced it, the confirmation channel and its latency,
    who attested (when that channel requires it) and the balance move it caused.
    """
    entry_id = store.insert(
        """INSERT INTO collections_ledger
             (client_id, debtor_id, entry_type, amount_cents, fee_cents, fee_basis_points,
              fee_rounding_rule, confirmation_channel, confirmation_latency, attested_by,
              external_reference, signal_at, recorded_at, balance_before_cents,
              balance_after_cents, debtor_state_before, debtor_state_after, linked_entry_id,
              payment_event_id, intake_reference, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fields["client_id"], fields["debtor_id"], fields.get("entry_type", "collection"),
         fields["amount_cents"], fields["fee_cents"], fields["fee_basis_points"],
         fields["fee_rounding_rule"], fields["confirmation_channel"],
         fields["confirmation_latency"], fields.get("attested_by"),
         fields.get("external_reference"), fields.get("signal_at"), fields["recorded_at"],
         fields.get("balance_before_cents"), fields.get("balance_after_cents"),
         fields.get("debtor_state_before"), fields["debtor_state_after"],
         fields.get("linked_entry_id"), fields.get("payment_event_id"),
         fields.get("intake_reference"), fields.get("note")),
    )
    return entry_id


def _insert_balance_edit_event(store, **fields: Any) -> int:
    return store.insert(
        """INSERT INTO balance_edit_events
             (client_id, debtor_id, event_type, balance_before_cents, balance_after_cents,
              delta_cents, who, reason, recorded_at, billable, resolution_channel,
              ledger_entry_id, linked_event_id, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fields["client_id"], fields["debtor_id"], fields["event_type"],
         fields["balance_before_cents"], fields["balance_after_cents"], fields["delta_cents"],
         fields["who"], fields.get("reason"), fields["recorded_at"],
         1 if fields.get("billable") else 0, fields.get("resolution_channel"),
         fields.get("ledger_entry_id"), fields.get("linked_event_id"), fields.get("note")),
    )


def _insert_reopen_event(store, **fields: Any) -> int:
    """One append-only row on the reopen-audit log. Both consequences and the re-audit live here.

    The reversal writes the opening row in the same transaction that un-bills, so the links
    (which collection was un-billed, by which counter-entry, from which payment event) and
    the un-billed amounts are recorded next to the ledger rows they describe - and roll back
    with them. The completion row carries what the re-run of the ingestion path actually
    found. Nothing here is ever updated or deleted.
    """
    return store.insert(
        """INSERT INTO reopen_audit_events
             (client_id, debtor_id, event_type, collection_entry_id, reversal_entry_id,
              payment_event_id, un_billed_amount_cents, un_billed_fee_cents,
              balance_before_cents, balance_after_cents, account_state_before,
              account_state_after, source_system, source_ref, renormalized,
              source_balance_cents, source_balance_matches_engine, contact_email_resolved,
              contact_phone_resolved, window_decision, derived_local_time, area_code, tz_name,
              dnc_flag, paid_status, ledger_balance_cents, approval_status_after,
              hold_state_kept, refuse_reason, performed_by, reason, recorded_at, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fields["client_id"], fields["debtor_id"], fields["event_type"],
         fields.get("collection_entry_id"), fields.get("reversal_entry_id"),
         fields.get("payment_event_id"), fields.get("un_billed_amount_cents"),
         fields.get("un_billed_fee_cents"), fields.get("balance_before_cents"),
         fields.get("balance_after_cents"), fields.get("account_state_before"),
         fields.get("account_state_after"), fields.get("source_system"), fields.get("source_ref"),
         1 if fields.get("renormalized") else 0, fields.get("source_balance_cents"),
         (None if fields.get("source_balance_matches_engine") is None
          else (1 if fields["source_balance_matches_engine"] else 0)),
         1 if fields.get("contact_email_resolved") else 0,
         1 if fields.get("contact_phone_resolved") else 0,
         fields.get("window_decision"), fields.get("derived_local_time"),
         fields.get("area_code"), fields.get("tz_name"), fields.get("dnc_flag"),
         fields.get("paid_status"), fields.get("ledger_balance_cents"),
         fields.get("approval_status_after"), 1 if fields.get("hold_state_kept") else 0,
         fields.get("refuse_reason"), fields["performed_by"], fields.get("reason"),
         fields["recorded_at"], fields.get("note")),
    )


def open_reopen_audit(store, debtor_id: str) -> Optional[Dict[str, Any]]:
    """The reversal that opened a reopen audit this debtor has not completed yet, if any.

    This is the second half of the invariant: a reversal writes an opening row and only a
    completion row carrying the same `reversal_entry_id` closes it. The claim path asks this
    question inside its own transaction, so a record whose reopen audit is still open cannot
    be contacted even after another hold on it has been resolved by a human.
    """
    return store.query_one(
        """SELECT o.* FROM reopen_audit_events o
            WHERE o.debtor_id = ? AND o.event_type = ?
              AND NOT EXISTS (SELECT 1 FROM reopen_audit_events c
                               WHERE c.event_type = ? AND c.reversal_entry_id = o.reversal_entry_id)
            ORDER BY o.event_id DESC""",
        (debtor_id, REOPEN_AUDIT_OPENED, REOPEN_AUDIT_COMPLETED))


def reopen_audit_events(store, debtor_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """The whole reopen-audit log (or one debtor's), oldest first. Read-only."""
    if debtor_id:
        return store.query(
            "SELECT * FROM reopen_audit_events WHERE debtor_id = ? ORDER BY event_id", (debtor_id,))
    return store.query("SELECT * FROM reopen_audit_events ORDER BY event_id")


def pending_reopen_audits(store) -> List[Dict[str, Any]]:
    """Every record whose reopen audit is still open, with the reversal that opened it."""
    rows = store.query(
        """SELECT o.* FROM reopen_audit_events o
            WHERE o.event_type = ?
              AND NOT EXISTS (SELECT 1 FROM reopen_audit_events c
                               WHERE c.event_type = ? AND c.reversal_entry_id = o.reversal_entry_id)
            ORDER BY o.debtor_id""",
        (REOPEN_AUDIT_OPENED, REOPEN_AUDIT_COMPLETED))
    for row in rows:
        record = store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?",
                                 (row["debtor_id"],))
        row["account_state"] = record.get("account_state") if record else None
        row["balance_owed"] = record.get("balance_owed") if record else None
        row["dnc_flag"] = record.get("dnc_flag") if record else None
    return rows


def _outbound_log_count(store, debtor_id: str) -> int:
    """How many contact-log rows exist for this debtor. The purge never touches them."""
    row = store.query_one(
        "SELECT COUNT(*) AS n FROM outbound_contact_logs WHERE debtor_id = ?", (debtor_id,))
    return int(row["n"] or 0)


# ---------------------------------------------------------------------------
# 1) the intake: one payment event, three consequences, one transaction
# ---------------------------------------------------------------------------
def apply_payment(store, runqueue: Optional[RunQueue] = None, audit: Optional[AuditLog] = None, *,
                  debtor_id: str, amount: Any, confirmation_channel: str,
                  attested_by: Optional[str] = None, external_reference: Optional[str] = None,
                  signal_at: Optional[str] = None, currency: str = "USD",
                  note: Optional[str] = None) -> Dict[str, Any]:
    """Accept one payment signal and apply all three consequences atomically."""
    _validate_signal(confirmation_channel, attested_by)
    amount_cents = to_cents(amount)
    if amount_cents <= 0:
        raise PaymentRefused(
            f"a payment signal must be a positive amount in cents (got {amount_cents}); "
            "a refund or a bounce is a reversal, not a payment")
    row = _debtor_row(store, debtor_id)
    client_id = row["client_id"]
    latency = confirmation_latency(confirmation_channel)
    intake_reference = f"pay-{uuid.uuid4().hex}"

    purged: List[Dict[str, Any]] = []
    result: Dict[str, Any]
    try:
        with store.tx(immediate=True):
            live = _debtor_row(store, debtor_id)
            if external_reference and _already_recorded(store, debtor_id, external_reference):
                # Idempotency, not ingenuity: a webhook delivered twice must not be
                # billed twice. Nothing is written and nothing is committed.
                result = {
                    "status": "DUPLICATE_IGNORED", "debtor_id": debtor_id, "client_id": client_id,
                    "confirmation_channel": confirmation_channel,
                    "external_reference": external_reference,
                    "reason": "this external_reference is already on the ledger for this debtor",
                }
            else:
                # A record whose reopen audit is still open is NOT in normal flow: its balance
                # came back from a reversal counter-entry and its source data has not been
                # re-read. A new payment signal here would clear the state without the audit -
                # so it is refused, loudly, and nothing is written. The remedy is one call:
                # complete the reopen audit first.
                if (live.get("account_state") == STATE_REOPENED_AWAITING_AUDIT
                        or open_reopen_audit(store, debtor_id) is not None):
                    raise PaymentRefused(
                        f"debtor {debtor_id} is awaiting its reopen audit "
                        f"(account_state = {live.get('account_state')}): a reversed payment must "
                        "re-run the ingestion path from step one and be recorded as audited "
                        "before a new payment signal is applied, so this one is refused and "
                        "nothing is written")
                recorded_at = clock.iso()
                balance_before_cents = to_cents(live.get("balance_owed") or 0)
                if balance_before_cents <= 0:
                    raise PaymentRefused(
                        f"debtor {debtor_id} owes nothing (balance_owed = {live.get('balance_owed')}, "
                        f"account_state = {live.get('account_state')}): a payment signal against a zero "
                        "or credit balance is held for review instead of writing a fee line")
                balance_after_cents = max(0, balance_before_cents - amount_cents)
                overpayment_cents = max(0, amount_cents - balance_before_cents)
                state_before = live.get("account_state")
                state_after = STATE_PAID if balance_after_cents == 0 else STATE_PARTIALLY_PAID

                _set_balance(store, debtor_id, balance_after_cents, state_after)   # leg (a)
                reason = (f"paid: balance settled to {from_cents(balance_after_cents):.2f} "
                          f"({confirmation_label(confirmation_channel)}, {latency})")
                purged, purged_at = _run_purge(runqueue, debtor_id, reason)        # leg (b)
                logs_untouched = _outbound_log_count(store, debtor_id)
                event_id = _insert_payment_event(store, **{
                    "client_id": client_id, "debtor_id": debtor_id,
                    "confirmation_channel": confirmation_channel, "amount_cents": amount_cents,
                    "currency": currency, "attested_by": attested_by,
                    "external_reference": external_reference, "signal_at": signal_at,
                    "confirmation_latency": latency, "recorded_at": recorded_at,
                    "balance_before_cents": balance_before_cents,
                    "balance_after_cents": balance_after_cents,
                    "overpayment_cents": overpayment_cents,
                    "account_state_before": state_before, "account_state_after": state_after,
                    "intake_reference": intake_reference, "queue_purged_at": purged_at,
                    "cancelled_runs": purged, "outbound_logs_untouched": logs_untouched,
                    "note": note,
                })
                fee_cents = fee_for(amount_cents)
                entry_id = _insert_ledger_row(store, **{                             # leg (c)
                    "client_id": client_id, "debtor_id": debtor_id, "entry_type": "collection",
                    "amount_cents": amount_cents, "fee_cents": fee_cents,
                    "fee_basis_points": fee_basis_points(), "fee_rounding_rule": FEE_ROUNDING_RULE,
                    "confirmation_channel": confirmation_channel, "confirmation_latency": latency,
                    "attested_by": attested_by, "external_reference": external_reference,
                    "signal_at": signal_at, "recorded_at": recorded_at,
                    "balance_before_cents": balance_before_cents,
                    "balance_after_cents": balance_after_cents,
                    "debtor_state_before": state_before, "debtor_state_after": state_after,
                    "payment_event_id": event_id, "intake_reference": intake_reference,
                    "note": note,
                })
                result = {
                    "status": "COMMITTED", "client_id": client_id, "debtor_id": debtor_id,
                    "event_id": event_id, "ledger_entry_id": entry_id,
                    "intake_reference": intake_reference,
                    "confirmation_channel": confirmation_channel,
                    "confirmation_channel_label": confirmation_label(confirmation_channel),
                    "confirmation_latency": latency, "attested_by": attested_by,
                    "external_reference": external_reference, "signal_at": signal_at,
                    "recorded_at": recorded_at,
                    "amount_cents": amount_cents, "amount_collected": from_cents(amount_cents),
                    "fee_cents": fee_cents, "fee_amount": from_cents(fee_cents),
                    "fee_basis_points": fee_basis_points(), "fee_rate_percent": fee_rate_percent(),
                    "fee_rounding_rule": FEE_ROUNDING_RULE,
                    "balance_before_cents": balance_before_cents,
                    "balance_after_cents": balance_after_cents,
                    "balance_after": from_cents(balance_after_cents),
                    "overpayment_cents": overpayment_cents,
                    "account_state_before": state_before, "account_state_after": state_after,
                    "cancelled_runs": purged, "cancelled_run_count": len(purged),
                    "queue_purged_at": purged_at, "outbound_logs_untouched": logs_untouched,
                    "note": note,
                }
    except BaseException:
        # The transaction has rolled back; the queue purge is in-memory and must be
        # rolled back with it, or a failed ledger write would silently cost the schedule.
        if purged and runqueue is not None:
            runqueue.restore(purged)
        raise

    if result["status"] == "DUPLICATE_IGNORED":
        if audit:
            audit.record("payment_duplicate_ignored", **result)
        return result
    if audit:
        audit.record("payment_recorded", **{k: v for k, v in result.items()
                                           if k not in ("cancelled_runs", "note")})
        audit.record("queue_purged_for_payment", debtor_id=debtor_id, client_id=client_id,
                     queue_purged_at=result["queue_purged_at"],
                     cancelled_run_ids=[r["run"]["run_id"] for r in purged],
                     cancelled_run_count=len(purged),
                     outbound_logs_untouched=result["outbound_logs_untouched"],
                     reason="payment confirmed: the debtor is no longer chased")
        audit.record("ledger_entry_appended", debtor_id=debtor_id, client_id=client_id,
                     entry_id=result["ledger_entry_id"], entry_type="collection",
                     amount_cents=result["amount_cents"], fee_cents=result["fee_cents"],
                     confirmation_channel=confirmation_channel,
                     confirmation_latency=result["confirmation_latency"],
                     fee_rounding_rule=FEE_ROUNDING_RULE)
    return result


# ---------------------------------------------------------------------------
# 2) reversals: reopen and un-bill, never edit
# ---------------------------------------------------------------------------
def _latest_unreversed_collection(store, debtor_id: str) -> Optional[Dict[str, Any]]:
    rows = store.query(
        """SELECT c.* FROM collections_ledger c
           WHERE c.debtor_id = ? AND c.entry_type = 'collection'
             AND NOT EXISTS (SELECT 1 FROM collections_ledger r
                             WHERE r.entry_type = 'reversal' AND r.linked_entry_id = c.entry_id)
           ORDER BY c.entry_id DESC""",
        (debtor_id,))
    return rows[0] if rows else None


def record_reversal(store, runqueue: Optional[RunQueue] = None, audit: Optional[AuditLog] = None, *,
                    debtor_id: str, kind: str = "bounced", reverses_entry_id: Optional[int] = None,
                    amount: Any = None, confirmation_channel: str = CHANNEL_ACCOUNTING_WEBHOOK,
                    who: Optional[str] = None, external_reference: Optional[str] = None,
                    signal_at: Optional[str] = None, note: Optional[str] = None) -> Dict[str, Any]:
    """A bounced or refunded payment reopens the sequence and un-bills the fee.

    The original ledger row is never edited. This writes a linked counter-entry whose
    amount and fee are the exact negation of the collection it points at, so the net
    statement value is original + reversal. Only a full reversal of a recorded
    collection is supported; a partial reversal would need a proportional fee and is
    deliberately refused rather than guessed at.

    A reversal is NOT a resume. On a record that was Paid or Partially_Paid it sets the
    record to `Reopened_Awaiting_Audit` and writes the opening row of the reopen audit in
    the same transaction, so the record cannot be contacted again until it has re-run the
    ingestion path from step one (run_reopen_audit). It never touches the run queue: the
    entries the payment purged stay purged, and no run is created or restored.

    If the record is already in a hold (`Pending_Adjustment_Confirmation`, and `Dispute_Hold`
    once the dispute interceptor lands) the hold is NOT exited - the reversal opens the same
    audit, records both states, and leaves the record where the human put it.
    """
    if kind not in REVERSAL_KINDS:
        raise PaymentRefused(f"reversal kind {kind!r} is not one of {list(REVERSAL_KINDS)}")
    _validate_signal(confirmation_channel, who if confirmation_channel == CHANNEL_MANUAL_ATTESTATION
                     else "source")
    _debtor_row(store, debtor_id)

    with store.tx(immediate=True):
        live = _debtor_row(store, debtor_id)
        if reverses_entry_id is not None:
            target = store.query_one(
                "SELECT * FROM collections_ledger WHERE entry_id = ? AND debtor_id = ?",
                (reverses_entry_id, debtor_id))
            if not target:
                raise PaymentRefused(
                    f"no ledger entry {reverses_entry_id} for debtor {debtor_id} to reverse")
            if target["entry_type"] != "collection":
                raise PaymentRefused("only a collection can be reversed")
            already = store.query_one(
                "SELECT entry_id FROM collections_ledger WHERE entry_type = 'reversal' "
                "AND linked_entry_id = ?", (reverses_entry_id,))
            if already:
                raise PaymentRefused(f"entry {reverses_entry_id} is already reversed by "
                                     f"entry {already['entry_id']}")
        else:
            target = _latest_unreversed_collection(store, debtor_id)
            if not target:
                raise PaymentRefused(
                    f"debtor {debtor_id} has no un-reversed collection to reverse: nothing was "
                    "billed, so there is nothing to un-bill")
        if amount is not None and to_cents(amount) != abs(int(target["amount_cents"])):
            raise PaymentRefused(
                "only a full reversal of a recorded collection is supported: a partial reversal "
                "would need a proportional fee, which is a billing decision rather than a fact "
                "we can read")

        balance_before_cents = to_cents(live.get("balance_owed") or 0)
        reopened_cents = abs(int(target["amount_cents"]))
        balance_after_cents = balance_before_cents + reopened_cents
        # The reversal's state write, and the precedence rule: a record already in a HOLD
        # stays in it. Un-billing is a fact about the money; releasing a dispute or a parked
        # balance edit is a human decision, and a payment signal must never make it.
        state_before = live.get("account_state")
        hold_kept = is_hold_state(state_before)
        state_after = state_before if hold_kept else STATE_REOPENED_AWAITING_AUDIT
        _set_balance(store, debtor_id, balance_after_cents, state_after)

        recorded_at = clock.iso()
        text = (note or f"{kind} payment: the sequence is reopened and the fee is credited back. "
                         "This counter-entry is the credit adjustment against the next statement.")
        entry_id = _insert_ledger_row(store, **{
            "client_id": live["client_id"], "debtor_id": debtor_id, "entry_type": "reversal",
            "amount_cents": -reopened_cents, "fee_cents": -int(target["fee_cents"]),
            "fee_basis_points": int(target["fee_basis_points"]),
            "fee_rounding_rule": target["fee_rounding_rule"],
            "confirmation_channel": confirmation_channel,
            "confirmation_latency": confirmation_latency(confirmation_channel),
            "attested_by": who, "external_reference": external_reference, "signal_at": signal_at,
            "recorded_at": recorded_at, "balance_before_cents": balance_before_cents,
            "balance_after_cents": balance_after_cents,
            "debtor_state_before": live.get("account_state"), "debtor_state_after": state_after,
            "linked_entry_id": int(target["entry_id"]),
            "intake_reference": f"rev-{uuid.uuid4().hex}", "note": text,
        })
        original = store.query_one("SELECT * FROM collections_ledger WHERE entry_id = ?",
                                   (int(target["entry_id"]),))
        counter = store.query_one("SELECT * FROM collections_ledger WHERE entry_id = ?", (entry_id,))
        net_amount = int(original["amount_cents"]) + int(counter["amount_cents"])
        net_fee = int(original["fee_cents"]) + int(counter["fee_cents"])
        # The reversal's own audit entry, written in the same transaction as the counter-entry
        # it describes: which collection was un-billed, by which counter-entry, from which
        # payment event, for exactly how much, and what the state was before and after. So a
        # failure anywhere rolls the audit entry back together with the ledger row.
        audit_note = ("the reversal opened a mandatory reopen audit: the record re-runs the "
                      "ingestion path from step one before any new outbound entry is generated")
        if hold_kept:
            audit_note += (f"; the record was already in a hold ({state_before}), and a "
                           "reversal never exits a hold")
        reopen_event_id = _insert_reopen_event(store, **{
            "client_id": live["client_id"], "debtor_id": debtor_id,
            "event_type": REOPEN_AUDIT_OPENED,
            "collection_entry_id": int(target["entry_id"]), "reversal_entry_id": entry_id,
            "payment_event_id": target.get("payment_event_id"),
            "un_billed_amount_cents": int(counter["amount_cents"]),
            "un_billed_fee_cents": int(counter["fee_cents"]),
            "balance_before_cents": balance_before_cents,
            "balance_after_cents": balance_after_cents,
            "account_state_before": state_before, "account_state_after": state_after,
            "dnc_flag": int(live.get("dnc_flag") or 0),
            "hold_state_kept": hold_kept,
            "performed_by": who or f"source:{confirmation_channel}",
            "reason": f"{kind} payment reversal ({confirmation_label(confirmation_channel)})",
            "recorded_at": clock.iso_ms(),
            "note": audit_note,
        })
        result = {
            "status": "REVERSED", "client_id": live["client_id"], "debtor_id": debtor_id,
            "kind": kind, "counter_entry_id": entry_id, "reverses_entry_id": int(target["entry_id"]),
            "amount_cents": int(counter["amount_cents"]),
            "fee_cents": int(counter["fee_cents"]),
            "fee_amount": from_cents(int(counter["fee_cents"])),
            "confirmation_channel": confirmation_channel,
            "confirmation_latency": confirmation_latency(confirmation_channel),
            "attested_by": who, "external_reference": external_reference,
            "recorded_at": recorded_at,
            "balance_before_cents": balance_before_cents,
            "balance_after_cents": balance_after_cents, "balance_after": from_cents(balance_after_cents),
            "account_state_before": state_before, "account_state_after": state_after,
            "reopened_cents": reopened_cents,
            # the reopen audit this reversal just opened, and whether the reversal was allowed
            # to move the record's state at all (a hold is never exited by a payment signal)
            "reopen_audit_opened": True,
            "reopen_audit_event_id": reopen_event_id,
            "pre_existing_hold_state": state_before if hold_kept else None,
            "claim_blocked_until": ("reopen_audit_completed" if not hold_kept
                                    else f"reopen_audit_completed and {state_before} resolved"),
            "net_statement_amount_cents": net_amount, "net_statement_fee_cents": net_fee,
            "original_entry": dict(original) if original else None,
            "counter_entry": dict(counter) if counter else None,
            "note": text,
        }
    if audit:
        audit.record("reversal_recorded", **{k: v for k, v in result.items()
                                            if k not in ("original_entry", "counter_entry", "note")})
    return result


# ---------------------------------------------------------------------------
# 2b) the reopen audit: re-run ingestion from step one, record that it happened
# ---------------------------------------------------------------------------
def _renormalize_source(row: Dict[str, Any]) -> Dict[str, Any]:
    """Re-run this record's stored source row through Agent 1's translator.

    The same one normalization path a live import uses: the raw vendor row out of
    `debtor_records.raw_record` goes back through the column-alias translator, so the
    contact data, the invoice date, the aging state and the derived calling window are
    re-resolved from the source rather than re-derived from our own stored conclusion.

    Fail-closed in every branch: no source row, an unparseable source row, a source row that
    now belongs to a different debtor, or an unparseable/missing source amount all raise
    ReopenAuditRefused. Nothing is coerced and no contact detail is invented.
    """
    raw_json = row.get("raw_record")
    if not str(raw_json or "").strip():
        raise ReopenAuditRefused(
            f"debtor {row['debtor_id']} has no stored source row (raw_record is empty): the "
            "reopen audit re-runs ingestion from step one and will not invent a source")
    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise ReopenAuditRefused(
            f"the stored source row for {row['debtor_id']} is unparseable ({exc}): fail closed "
            "rather than re-normalize from a guess") from exc
    if not isinstance(raw, dict):
        raise ReopenAuditRefused(
            f"the stored source row for {row['debtor_id']} is not a row of columns "
            f"({type(raw).__name__}): fail closed")

    result = ingest.normalize([raw], source_system=row.get("source_system") or "unknown",
                              endpoint=row.get("source_ref") or "",
                              client_id_hint=row.get("client_id"))
    if result.rejected:
        raise ReopenAuditRefused(
            f"the stored source row for {row['debtor_id']} no longer normalizes: "
            f"{result.rejected[0]['reason']}")
    record = result.records[0]
    if record["debtor_id"] != row["debtor_id"]:
        raise ReopenAuditRefused(
            f"the stored source row identifies debtor {record['debtor_id']}, not "
            f"{row['debtor_id']}: the record and its source have drifted apart, so fail closed")
    if record["balance_owed"] is None:
        raise ReopenAuditRefused(
            f"the re-normalized source balance for {row['debtor_id']} is missing or unparseable: "
            "an amount is never coerced into a number")
    return record


def _record_reopen_audit_refusal(store, debtor_id: str, performed_by: str, reason: str,
                                 exc: BaseException) -> Optional[int]:
    """Record a re-audit the engine refused to complete. The record does not move."""
    row = store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
    if not row:
        return None
    opening = open_reopen_audit(store, debtor_id) or {}
    balance_cents = to_cents(row.get("balance_owed") or 0)
    with store.tx(immediate=True):
        return _insert_reopen_event(store, **{
            "client_id": row["client_id"], "debtor_id": debtor_id,
            "event_type": REOPEN_AUDIT_REFUSED,
            "collection_entry_id": opening.get("collection_entry_id"),
            "reversal_entry_id": opening.get("reversal_entry_id"),
            "payment_event_id": opening.get("payment_event_id"),
            "balance_before_cents": balance_cents, "balance_after_cents": balance_cents,
            "account_state_before": row.get("account_state"),
            "account_state_after": row.get("account_state"),
            "dnc_flag": int(row.get("dnc_flag") or 0),
            "refuse_reason": str(exc),
            "performed_by": performed_by, "reason": reason,
            "recorded_at": clock.iso_ms(),
            "note": ("the reopen audit was refused and the record stays exactly where it is: "
                     "nothing was re-normalized and no outbound entry can be claimed from it"),
        })


def run_reopen_audit(store, runqueue: Optional[RunQueue] = None, audit: Optional[AuditLog] = None, *,
                     debtor_id: str, performed_by: str, reason: str) -> Dict[str, Any]:
    """Complete the reopen audit for a reversed record: ingestion, step one, again.

    A reversal is not a resume, and this is the only way back into normal flow. In one
    transaction it re-runs the ingestion path over the record's stored source row
    (re-normalize -> re-resolve contact data -> re-evaluate the calling window -> re-check
    DNC/STOP -> re-check paid status and balance), records the completed re-audit with a
    millisecond-UTC timestamp and the administrator, and only then lets the record re-enter
    normal flow - still governed by the client's current operating mode and every guardrail.

    Three things it deliberately does NOT do:
      * it does not resurrect the purged queue, and it creates no run: the scheduled entries
        the payment removed stay gone, and the contact history is untouched;
      * it does not overwrite the balance from the source: the ledger is the money record.
        A source figure that disagrees is recorded as a mismatch and flagged, never applied;
      * it does not exit a hold. A record found in Pending_Adjustment_Confirmation (or, once
        the dispute interceptor lands, Dispute_Hold) completes its audit and stays in the
        hold - a human release is not something a payment signal may perform.
    """
    if not str(performed_by or "").strip() or not str(reason or "").strip():
        raise PaymentRefused(
            "a reopen audit must be attributed: perform it with who ran it and why, because it "
            "is the gate between a reversed payment and any new contact")
    row = _debtor_row(store, debtor_id)
    client_id = row["client_id"]
    if open_reopen_audit(store, debtor_id) is None:
        raise ReopenAuditRefused(
            f"debtor {debtor_id} has no open reopen audit: nothing was reversed against it, so "
            "there is nothing to re-audit (a record that was never reopened is not gated)")

    try:
        normalized = _renormalize_source(row)
    except ReopenAuditRefused as exc:
        _record_reopen_audit_refusal(store, debtor_id, performed_by, reason, exc)
        if audit:
            audit.record("reopen_audit_refused", debtor_id=debtor_id, client_id=client_id,
                         performed_by=performed_by, reason=reason, refuse_reason=str(exc))
        raise

    with store.tx(immediate=True):
        live = _debtor_row(store, debtor_id)
        opening = open_reopen_audit(store, debtor_id)
        if opening is None:
            raise ReopenAuditRefused(
                f"the reopen audit for {debtor_id} was completed by someone else while this one "
                "was being prepared: nothing to do, and nothing is written")
        balance_cents = to_cents(live.get("balance_owed") or 0)
        ledger_state = derive_account_state(balance_cents, net_collected_cents(store, debtor_id))
        state_before = live.get("account_state")
        hold_kept = is_hold_state(state_before)
        state_after = state_before if hold_kept else ledger_state
        window = timezones.window_decision_for(normalized.get("phone"))
        dnc_flag = int(live.get("dnc_flag") or 0)
        recorded_at = clock.iso_ms()
        source_balance_cents = to_cents(normalized["balance_owed"])

        # The re-normalized record, written back: contact data, dates, aging and the window are
        # re-resolved from the source. The balance is NOT taken from here (see the docstring),
        # and approval_status goes back to Pending because any approval predates the reversal -
        # in hitl that means a fresh human review, decided against the re-read record.
        store.execute(
            """UPDATE debtor_records SET name = ?, email = ?, phone = ?, invoice_date = ?,
                 aging_state = ?, aging_note = ?, invoice_number = ?, source_columns = ?,
                 raw_record = ?, area_code = ?, tz_name = ?, derived_local_time = ?,
                 window_decision = ?, window_decision_at = ?, account_state = ?,
                 approval_status = 'Pending' WHERE debtor_id = ?""",
            (normalized.get("name"), normalized.get("email"), normalized.get("phone"),
             normalized.get("invoice_date"), normalized.get("aging_state"),
             normalized.get("aging_note"), normalized.get("invoice_number"),
             normalized.get("source_columns"), normalized.get("raw_record"),
             window.get("area_code"), window.get("tz_name"), window.get("derived_local_time"),
             window.get("window_decision"), recorded_at, state_after, debtor_id))

        event_id = _insert_reopen_event(store, **{
            "client_id": client_id, "debtor_id": debtor_id,
            "event_type": REOPEN_AUDIT_COMPLETED,
            "collection_entry_id": opening.get("collection_entry_id"),
            "reversal_entry_id": opening.get("reversal_entry_id"),
            "payment_event_id": opening.get("payment_event_id"),
            "un_billed_amount_cents": opening.get("un_billed_amount_cents"),
            "un_billed_fee_cents": opening.get("un_billed_fee_cents"),
            "balance_before_cents": balance_cents, "balance_after_cents": balance_cents,
            "account_state_before": state_before, "account_state_after": state_after,
            "source_system": normalized.get("source_system"),
            "source_ref": normalized.get("source_ref"),
            "renormalized": True,
            "source_balance_cents": source_balance_cents,
            "source_balance_matches_engine": source_balance_cents == balance_cents,
            "contact_email_resolved": bool(normalized.get("email")),
            "contact_phone_resolved": bool(normalized.get("phone")),
            "window_decision": window.get("window_decision"),
            "derived_local_time": window.get("derived_local_time"),
            "area_code": window.get("area_code"), "tz_name": window.get("tz_name"),
            "dnc_flag": dnc_flag, "paid_status": ledger_state,
            "ledger_balance_cents": balance_cents,
            "approval_status_after": "Pending",
            "hold_state_kept": hold_kept,
            "performed_by": performed_by, "reason": reason, "recorded_at": recorded_at,
            "note": ("the record re-ran the ingestion path from step one and re-entered normal "
                     "flow; the purged queue was not restored and no run was created"
                     + (f"; it stays in {state_before} because a reopen audit never exits a hold"
                        if hold_kept else "")),
        })
        written = store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
        runs_for_debtor = [r.run_id for r in (runqueue.all() if runqueue else [])
                           if r.debtor_id == debtor_id]
        result = {
            "status": "REOPEN_AUDIT_COMPLETED",
            "event_id": event_id, "client_id": client_id, "debtor_id": debtor_id,
            "reversal_entry_id": opening.get("reversal_entry_id"),
            "collection_entry_id": opening.get("collection_entry_id"),
            "payment_event_id": opening.get("payment_event_id"),
            "renormalized": True,
            "source_system": normalized.get("source_system"),
            "source_ref": normalized.get("source_ref"),
            "source_balance_cents": source_balance_cents,
            "balance_cents": balance_cents,
            "balance_owed": from_cents(balance_cents),
            "source_balance_matches_engine": source_balance_cents == balance_cents,
            "contact": {
                "email": normalized.get("email"), "phone": normalized.get("phone"),
                "email_resolved": bool(normalized.get("email")),
                "phone_resolved": bool(normalized.get("phone")),
                "note": ("contact data is what the source row carries: a missing address or "
                         "number is recorded as missing, never invented"),
            },
            "window": window,
            "dnc_flag": dnc_flag, "dnc_blocked": dnc_flag == 1,
            "paid_status": ledger_state,
            "account_state_before": state_before, "account_state_after": state_after,
            "state_derived_from_ledger": not hold_kept, "hold_state_kept": hold_kept,
            "approval_status_before": live.get("approval_status"),
            "approval_status_after": written.get("approval_status"),
            "approval_requires_fresh_human_review": (written.get("approval_status") == "Pending"),
            "queue_runs_for_debtor": runs_for_debtor,
            "queue_purge_not_restored": not runs_for_debtor,
            "performed_by": performed_by, "reason": reason, "recorded_at": recorded_at,
            "record": dict(written),
        }
    if audit:
        audit.record("reopen_audit_completed",
                     **{k: v for k, v in result.items()
                        if k not in ("record", "window", "contact", "queue_runs_for_debtor")})
        audit.record("reopen_audit_source_rerun", debtor_id=debtor_id, client_id=client_id,
                     source_system=result["source_system"], source_ref=result["source_ref"],
                     source_balance_cents=source_balance_cents,
                     ledger_balance_cents=balance_cents,
                     source_balance_matches_engine=result["source_balance_matches_engine"],
                     contact_email_resolved=result["contact"]["email_resolved"],
                     contact_phone_resolved=result["contact"]["phone_resolved"],
                     window_decision=result["window"]["window_decision"],
                     performed_by=performed_by, recorded_at=result["recorded_at"])
    return result


# ---------------------------------------------------------------------------
# 3) balance edits pause: park, then resolve with an attribution
# ---------------------------------------------------------------------------
def record_balance_edit(store, runqueue: Optional[RunQueue] = None, audit: Optional[AuditLog] = None, *,
                        debtor_id: str, new_balance: Any, who: str, reason: str,
                        note: Optional[str] = None) -> Dict[str, Any]:
    """A balance change with no payment signal behind it parks the record.

    No dunning (the claim transaction refuses every contact while parked), no fee, no
    send, and nothing billable. Resolution is the client's call and it is attributed.
    """
    if not str(who or "").strip() or not str(reason or "").strip():
        raise PaymentRefused("a balance edit must be attributed: who made it and why")
    row = _debtor_row(store, debtor_id)
    client_id = row["client_id"]
    after_cents = to_cents(new_balance)
    with store.tx(immediate=True):
        live = _debtor_row(store, debtor_id)
        # A record awaiting its reopen audit must not be edited out of that state: the state
        # is the gate on every outbound entry, and a hand edit would park the record and leave
        # the audit open behind it. The remedy is the same one call.
        if (live.get("account_state") == STATE_REOPENED_AWAITING_AUDIT
                or open_reopen_audit(store, debtor_id) is not None):
            raise PaymentRefused(
                f"debtor {debtor_id} is awaiting its reopen audit "
                f"(account_state = {live.get('account_state')}): complete the reopen audit "
                "before editing the balance, so the record is re-read from its source and the "
                "change is not parked on top of an un-audited reversal")
        before_cents = to_cents(live.get("balance_owed") or 0)
        delta_cents = after_cents - before_cents
        state_before = live.get("account_state")
        if delta_cents == 0:
            result = {"status": "NO_CHANGE", "debtor_id": debtor_id, "client_id": client_id,
                      "balance_cents": before_cents,
                      "reason": "the edit does not change the balance, so nothing is parked"}
        else:
            store.execute(
                """UPDATE debtor_records SET balance_owed = ?, account_state = ?,
                     balance_updated_at = ? WHERE debtor_id = ?""",
                (from_cents(after_cents), STATE_PENDING_ADJUSTMENT, clock.iso(), debtor_id),
            )
            event_id = _insert_balance_edit_event(store, **{
                "client_id": client_id, "debtor_id": debtor_id, "event_type": "edit_unconfirmed",
                "balance_before_cents": before_cents, "balance_after_cents": after_cents,
                "delta_cents": delta_cents, "who": who, "reason": reason,
                "recorded_at": clock.iso(), "billable": False, "note": note,
            })
            result = {
                "status": "PARKED", "debtor_id": debtor_id, "client_id": client_id,
                "event_id": event_id, "account_state_before": state_before,
                "account_state_after": STATE_PENDING_ADJUSTMENT,
                "balance_before_cents": before_cents, "balance_after_cents": after_cents,
                "balance_before": from_cents(before_cents), "balance_after": from_cents(after_cents),
                "delta_cents": delta_cents, "who": who, "reason": reason,
                "billable": False,
                "pending_runs_left_scheduled": [r.run_id for r in (runqueue.all() if runqueue else [])
                                                if r.debtor_id == debtor_id and r.state == "scheduled"],
                "no_dunning_reason": ("account_state = Pending_Adjustment_Confirmation: the claim "
                                      "transaction refuses every contact while parked"),
            }
    if audit:
        audit.record("balance_edit_parked" if result["status"] == "PARKED" else "balance_edit_noop",
                     **{k: v for k, v in result.items() if k != "note"})
    return result


def _parked_edit(store, debtor_id: str) -> Optional[Dict[str, Any]]:
    """The most recent unconfirmed edit for this debtor that has no resolution row yet."""
    edits = store.query(
        """SELECT * FROM balance_edit_events
           WHERE debtor_id = ? AND event_type = 'edit_unconfirmed' ORDER BY event_id DESC""",
        (debtor_id,))
    resolved = {r["linked_event_id"] for r in store.query(
        "SELECT linked_event_id FROM balance_edit_events WHERE debtor_id = ? "
        "AND event_type <> 'edit_unconfirmed'", (debtor_id,))}
    for edit in edits:
        if edit["event_id"] not in resolved:
            return edit
    return None


def resolve_balance_edit(store, runqueue: Optional[RunQueue] = None, audit: Optional[AuditLog] = None, *,
                         debtor_id: str, resolution: str, who: str, reason: str,
                         amount: Any = None,
                         confirmation_channel: str = CHANNEL_MANUAL_ATTESTATION,
                         external_reference: Optional[str] = None,
                         signal_at: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a parked balance edit: `payment` (billable) or `adjustment` (never billable)."""
    if resolution not in ("payment", "adjustment"):
        raise PaymentRefused("a balance edit resolves as 'payment' or 'adjustment', nothing else")
    if not str(who or "").strip() or not str(reason or "").strip():
        raise PaymentRefused("a resolution must be attributed: who decided it and why")
    _debtor_row(store, debtor_id)
    purged: List[Dict[str, Any]] = []
    try:
        with store.tx(immediate=True):
            live = _debtor_row(store, debtor_id)
            edit = _parked_edit(store, debtor_id)
            if not edit:
                raise PaymentRefused(f"debtor {debtor_id} has no unconfirmed balance edit parked")
            drop_cents = int(edit["balance_before_cents"]) - int(edit["balance_after_cents"])
            recorded_at = clock.iso()

            if resolution == "adjustment":
                balance_cents = to_cents(live.get("balance_owed") or 0)
                net = net_collected_cents(store, debtor_id)
                state_after = derive_account_state(balance_cents, net)
                store.execute(
                    "UPDATE debtor_records SET account_state = ?, balance_updated_at = ? "
                    "WHERE debtor_id = ?", (state_after, recorded_at, debtor_id))
                event_id = _insert_balance_edit_event(store, **{
                    "client_id": live["client_id"], "debtor_id": debtor_id,
                    "event_type": "resolved_as_adjustment",
                    "balance_before_cents": int(edit["balance_after_cents"]),
                    "balance_after_cents": int(edit["balance_after_cents"]),
                    "delta_cents": 0, "who": who, "reason": reason, "recorded_at": recorded_at,
                    "billable": UNCONFIRMED_ADJUSTMENT_BILLABLE, "resolution_channel": None,
                    "ledger_entry_id": None, "linked_event_id": int(edit["event_id"]),
                    "note": "closed as an adjustment: no funds were collected, so there is nothing "
                            "to bill. An unconfirmed adjustment is never billable - the standing "
                            "default until the owner rules otherwise.",
                })
                result = {
                    "status": "RESOLVED_AS_ADJUSTMENT", "debtor_id": debtor_id,
                    "client_id": live["client_id"], "event_id": event_id,
                    "linked_event_id": int(edit["event_id"]), "billable": False,
                    "ledger_entry_id": None, "fee_cents": 0,
                    "account_state_after": state_after, "who": who, "reason": reason,
                    "dunning_resumes": state_after != STATE_PAID,
                }
            else:
                attested_cents = to_cents(amount) if amount is not None else drop_cents
                if attested_cents != drop_cents:
                    raise PaymentRefused(
                        f"the attested payment ({attested_cents} cents) does not match the balance "
                        f"drop that was parked ({drop_cents} cents): correct the balance with another "
                        "edit instead of billing a number that does not reconcile")
                _validate_signal(confirmation_channel, who)
                latency = confirmation_latency(confirmation_channel)
                intake_reference = f"pay-{uuid.uuid4().hex}"
                balance_cents = to_cents(live.get("balance_owed") or 0)
                state_before = live.get("account_state")
                state_after = STATE_PAID if balance_cents <= 0 else STATE_PARTIALLY_PAID
                if state_after != state_before:
                    store.execute(
                        "UPDATE debtor_records SET account_state = ?, balance_updated_at = ? "
                        "WHERE debtor_id = ?", (state_after, recorded_at, debtor_id))
                purged, purged_at = _run_purge(
                    runqueue, debtor_id,
                    f"paid: balance settled to {from_cents(balance_cents):.2f} "
                    f"({confirmation_label(confirmation_channel)}, {latency})")
                logs_untouched = _outbound_log_count(store, debtor_id)
                pay_event_id = _insert_payment_event(store, **{
                    "client_id": live["client_id"], "debtor_id": debtor_id,
                    "confirmation_channel": confirmation_channel, "amount_cents": attested_cents,
                    "attested_by": who, "external_reference": external_reference,
                    "signal_at": signal_at, "confirmation_latency": latency,
                    "recorded_at": recorded_at,
                    "balance_before_cents": int(edit["balance_before_cents"]),
                    "balance_after_cents": balance_cents, "overpayment_cents": 0,
                    "account_state_before": state_before, "account_state_after": state_after,
                    "intake_reference": intake_reference, "queue_purged_at": purged_at,
                    "cancelled_runs": purged, "outbound_logs_untouched": logs_untouched,
                    "note": f"attested as a payment when resolving balance edit event "
                            f"{edit['event_id']}: {reason}",
                })
                fee_cents = fee_for(attested_cents)
                entry_id = _insert_ledger_row(store, **{
                    "client_id": live["client_id"], "debtor_id": debtor_id,
                    "entry_type": "collection", "amount_cents": attested_cents,
                    "fee_cents": fee_cents, "fee_basis_points": fee_basis_points(),
                    "fee_rounding_rule": FEE_ROUNDING_RULE,
                    "confirmation_channel": confirmation_channel,
                    "confirmation_latency": latency, "attested_by": who,
                    "external_reference": external_reference, "signal_at": signal_at,
                    "recorded_at": recorded_at,
                    "balance_before_cents": int(edit["balance_before_cents"]),
                    "balance_after_cents": balance_cents,
                    "debtor_state_before": state_before, "debtor_state_after": state_after,
                    "payment_event_id": pay_event_id, "intake_reference": intake_reference,
                    "note": f"the parked balance drop was attested as a payment: {reason}",
                })
                event_id = _insert_balance_edit_event(store, **{
                    "client_id": live["client_id"], "debtor_id": debtor_id,
                    "event_type": "resolved_as_payment",
                    "balance_before_cents": int(edit["balance_before_cents"]),
                    "balance_after_cents": int(edit["balance_after_cents"]),
                    "delta_cents": -attested_cents, "who": who, "reason": reason,
                    "recorded_at": recorded_at, "billable": True,
                    "resolution_channel": confirmation_channel, "ledger_entry_id": entry_id,
                    "linked_event_id": int(edit["event_id"]),
                    "note": "resolved as a payment: it flows through the normal payment path "
                            "and is billable on the funds confirmed collected.",
                })
                result = {
                    "status": "RESOLVED_AS_PAYMENT", "debtor_id": debtor_id,
                    "client_id": live["client_id"], "event_id": event_id,
                    "payment_event_id": pay_event_id, "ledger_entry_id": entry_id,
                    "linked_event_id": int(edit["event_id"]), "billable": True,
                    "amount_cents": attested_cents, "amount_collected": from_cents(attested_cents),
                    "fee_cents": fee_cents, "fee_amount": from_cents(fee_cents),
                    "fee_rounding_rule": FEE_ROUNDING_RULE,
                    "confirmation_channel": confirmation_channel,
                    "confirmation_latency": latency, "attested_by": who,
                    "account_state_after": state_after, "who": who, "reason": reason,
                    "balance_after_cents": balance_cents,
                    "cancelled_runs": purged, "cancelled_run_count": len(purged),
                    "queue_purged_at": purged_at, "outbound_logs_untouched": logs_untouched,
                }
    except BaseException:
        if purged and runqueue is not None:
            runqueue.restore(purged)
        raise
    if audit:
        audit.record("balance_edit_resolved", **{k: v for k, v in result.items()
                                                if k != "cancelled_runs"})
        if resolution == "payment":
            audit.record("ledger_entry_appended", debtor_id=debtor_id, client_id=result["client_id"],
                         entry_id=result["ledger_entry_id"], entry_type="collection",
                         amount_cents=result["amount_cents"], fee_cents=result["fee_cents"],
                         confirmation_channel=confirmation_channel,
                         fee_rounding_rule=FEE_ROUNDING_RULE)
    return result


# ---------------------------------------------------------------------------
# 4) the ledger read model (plain data, no UI)
# ---------------------------------------------------------------------------
def ledger_for_client(store, client_id: str) -> Dict[str, Any]:
    """Every ledger line for one client plus the net totals, as JSON-serialisable dicts.

    Net (what the monthly statement shows) = the sum of the lines: a collection adds,
    a linked reversal subtracts, so a bounced payment un-bills itself. Every figure here
    comes from a stored row; nothing is estimated, projected or filled in.
    """
    rows = store.query(
        "SELECT * FROM collections_ledger WHERE client_id = ? ORDER BY entry_id", (client_id,))
    reversed_by = {int(r["linked_entry_id"]): int(r["entry_id"]) for r in rows
                   if r["entry_type"] == "reversal" and r["linked_entry_id"] is not None}
    lines: List[Dict[str, Any]] = []
    for row in rows:
        entry_id = int(row["entry_id"])
        event = store.query_one("SELECT event_id, queue_purged_at, cancelled_run_count, attested_by "
                               "FROM payment_events WHERE event_id = ?",
                               (row["payment_event_id"],)) if row["payment_event_id"] else None
        lines.append({
            "entry_id": entry_id,
            "entry_type": row["entry_type"],
            "debtor_id": row["debtor_id"],
            "amount_cents": int(row["amount_cents"]),
            "amount": from_cents(int(row["amount_cents"])),
            "fee_cents": int(row["fee_cents"]),
            "fee_amount": from_cents(int(row["fee_cents"])),
            "fee_basis_points": int(row["fee_basis_points"]),
            "fee_rate_percent": int(row["fee_basis_points"]) / 100.0,
            "fee_rounding_rule": row["fee_rounding_rule"],
            "confirmation_channel": row["confirmation_channel"],
            "confirmation_latency": row["confirmation_latency"],
            "attested_by": row["attested_by"],
            "external_reference": row["external_reference"],
            "signal_at": row["signal_at"],
            "recorded_at": row["recorded_at"],
            "balance_before_cents": row["balance_before_cents"],
            "balance_after_cents": row["balance_after_cents"],
            "debtor_state_before": row["debtor_state_before"],
            "debtor_state_after": row["debtor_state_after"],
            "reverses_entry_id": int(row["linked_entry_id"]) if row["linked_entry_id"] else None,
            "reversed_by_entry_id": reversed_by.get(entry_id),
            "payment_event_id": row["payment_event_id"],
            "payment_event": event,
            "billable": row["entry_type"] == "collection" and int(row["fee_cents"]) != 0,
            "note": row["note"],
        })
    collected = [l for l in lines if l["entry_type"] == "collection"]
    reversals = [l for l in lines if l["entry_type"] == "reversal"]
    net_amount = sum(l["amount_cents"] for l in lines)
    net_fee = sum(l["fee_cents"] for l in lines)
    return {
        "client_id": client_id,
        "generated_at": clock.iso(),
        "lines": lines,
        "totals": {
            "line_count": len(lines),
            "collections_count": len(collected),
            "reversals_count": len(reversals),
            "gross_collected_cents": sum(l["amount_cents"] for l in collected),
            "reversed_cents": sum(l["amount_cents"] for l in reversals),
            "net_collected_cents": net_amount,
            "net_collected": from_cents(net_amount),
            "net_fee_cents": net_fee,
            "net_fee_amount": from_cents(net_fee),
            "fee_rate_percent": fee_rate_percent(),
            "basis": "20% of funds actually confirmed collected, nothing else",
            "reversal_net_value": "original + linked reversal",
        },
    }


def ledger_for_debtor(store, debtor_id: str) -> List[Dict[str, Any]]:
    row = store.query_one("SELECT client_id FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
    if not row:
        raise KeyError(f"unknown debtor {debtor_id}")
    return [l for l in ledger_for_client(store, row["client_id"])["lines"] if l["debtor_id"] == debtor_id]
