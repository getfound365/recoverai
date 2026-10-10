"""Agent 3 - Webhook Orchestrator: claim-then-fire.

The send path, in order:

  1. render/stage the notice (Agent 2's output) - no side effects on the store
  2. BEGIN IMMEDIATE (SQLite) / BEGIN + SELECT ... FOR UPDATE (Postgres) on the
     client_autonomy_settings row - the single row every mode flip also writes
  3. inside that one transaction: re-read the client's operating mode, re-validate the
     sign-off, re-check dnc_flag, re-derive the calling window and re-check the
     authorization scope
  4. if everything passes, write the send-claim row AND bump claim_seq on that same
     client_autonomy_settings row, then COMMIT
  5. only a committed claim permits the outbound POST to the (simulated) Vapi endpoint

What that guarantees, and what it does not, is written out in docs/vapi-handshake.md.
The short version: nothing is ever claimed after a mode flip to hitl has committed, and
a mode flip can never slip into the gap between the check and the send. It cannot recall
a request that has already gone out over the wire.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import clock, config, db, draft as draftsman, gate, onboarding, payments, timezones
from .audit import AuditLog
from .runqueue import RunQueue

#: Hard ceiling on the outbound provider POST. Nothing in the send path may block
#: indefinitely - a timeout is surfaced as a loud FAILED dispatch on a committed claim.
DISPATCH_TIMEOUT = 10.0

CLAIMED = "CLAIMED"
BLOCKED_DNC = "BLOCKED_DNC"
BLOCKED_OUTSIDE_WINDOW = "BLOCKED_OUTSIDE_WINDOW"
BLOCKED_MODE_HITL = "BLOCKED_MODE_HITL"
BLOCKED_NO_SIGNOFF = "BLOCKED_NO_SIGNOFF"
BLOCKED_SIGNOFF_INVALID = "BLOCKED_SIGNOFF_INVALID"
BLOCKED_OUT_OF_SCOPE = "BLOCKED_OUT_OF_SCOPE"
BLOCKED_DUPLICATE = "BLOCKED_DUPLICATE_CLAIM"
BLOCKED_NO_BALANCE = "BLOCKED_NO_BALANCE_DUE"
#: The balance on this record moved with no payment signal behind it: it is parked in
#: Pending_Adjustment_Confirmation until the client attributes it. No mode can send
#: past this - it is checked inside the claim transaction, before the POST.
BLOCKED_PENDING_ADJUSTMENT = "BLOCKED_PENDING_ADJUSTMENT"
#: The payment on this record was reversed. A reversal is not a resume: the record is in
#: Reopened_Awaiting_Audit (or the reopen audit a reversal opened on it is still open, e.g.
#: after another hold on the record was resolved) and cannot be contacted until it has re-run
#: the ingestion path from step one and that completion is recorded. Checked inside the claim
#: transaction, before the POST, for every channel and in every operating mode.
BLOCKED_REOPENED_AWAITING_AUDIT = "REOPENED_AWAITING_AUDIT"

TEMPLATE_REFERENCE = "past_due_notice/{channel}/v1"

#: A documented, TEST-ONLY interleaving seam. It is None everywhere outside the race
#: tests: when it is None nothing is called, no guard consults it and the send path is
#: byte-for-byte the same. A test may install one to control the interleaving instead of
#: hoping for a race, and it is called at exactly two stages of the claim path:
#:
#:   {"stage": "before_claim_transaction", ...}  after the debtor read, before BEGIN IMMEDIATE
#:   {"stage": "inside_claim_transaction", ...}  inside the claim transaction, after the
#:                                               last-millisecond re-read and before the
#:                                               claim row is written
#:
#: It can only observe and delay. It is handed a copy of the snapshot, it is called after
#: the re-read has already happened when it runs inside the transaction, and nothing it
#: returns is read - it cannot disable a guard, write a claim, or be set by the engine.
CLAIM_INTERLEAVE_HOOK: Optional[Callable[[Dict[str, Any]], None]] = None


def _fetch_debtor(store, debtor_id: str) -> Optional[Dict[str, Any]]:
    return store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))


class Orchestrator:
    def __init__(self, store: db.Store, audit: AuditLog, runqueue: Optional[RunQueue] = None,
                 vendor_base_url: str = "http://127.0.0.1:8801", paths: Optional[Dict[str, Path]] = None):
        self.store = store
        self.audit = audit
        self.queue = runqueue or RunQueue()
        self.vendor_base_url = vendor_base_url
        paths = paths or db.ensure_dirs()
        self.drafts_dir = Path(paths["drafts"])
        self.payloads_dir = Path(paths["payloads"])
        self.email_dir = Path(paths["email"])
        # A payload written before the POST is evidence: make sure the three artifact
        # directories exist whatever paths the caller supplied, rather than failing with a
        # FileNotFoundError at the moment a contact is about to be claimed.
        for directory in (self.drafts_dir, self.payloads_dir, self.email_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # --- drafting (Agent 2 + the human gate) -------------------------------
    def queue_draft(self, debtor_id: str, channel: str = "email", approved_by: Optional[str] = None) -> Dict[str, Any]:
        record = _fetch_debtor(self.store, debtor_id)
        if not record:
            raise KeyError(f"unknown debtor {debtor_id}")
        artifact = draftsman.render(record, channel=channel, approved_by=approved_by)
        path = self.drafts_dir / draftsman.draft_filename(artifact)
        path.write_text(draftsman.draft_text(artifact), encoding="utf-8")
        artifact["path"] = str(path)
        artifact["approval_status"] = record["approval_status"]
        self.audit.record("draft_queued", debtor_id=debtor_id, client_id=record["client_id"],
                          channel=channel, path=str(path),
                          disclaimer_occurrences=artifact["disclaimer_occurrences"],
                          approval_status=record["approval_status"])
        return artifact

    def approve(self, debtor_id: str, who: str, channel: Optional[str] = None) -> Dict[str, Any]:
        """A human approves a queued draft. The narrow case the freeze cares about:

        a record frozen as Outside_Calling_Window can only be approved once the debtor's
        local window is open again - otherwise the human click would unfreeze a run that
        the window guardrail is still holding.
        """
        record = _fetch_debtor(self.store, debtor_id)
        if not record:
            raise KeyError(f"unknown debtor {debtor_id}")
        if record["dnc_flag"]:
            raise PermissionError(f"debtor {debtor_id} is on the do-not-contact list; approval refused")
        # An approval is what authorizes a contact in hitl, so approving a record that cannot
        # be claimed yet would only manufacture a review that can never fire. The audit comes
        # first, and it re-reads the record the human is approving.
        if (record["account_state"] == payments.STATE_REOPENED_AWAITING_AUDIT
                or payments.open_reopen_audit(self.store, debtor_id) is not None):
            raise PermissionError(
                f"debtor {debtor_id} is awaiting its reopen audit "
                f"(account_state = {record['account_state']}): a reversed payment must re-run the "
                "ingestion path from step one before anything is approved, so approval is refused")
        if record["approval_status"] == "Outside_Calling_Window":
            window = timezones.window_decision_for(record["phone"])
            if not window["in_window"]:
                self.audit.record("draft_approved", debtor_id=debtor_id, who=who, outcome="blocked_frozen",
                                  window_decision=window["window_decision"],
                                  derived_local_time=window.get("derived_local_time"))
                return {"debtor_id": debtor_id, "approved": False, "window_decision": window["window_decision"],
                        "reason": "record is frozen as Outside_Calling_Window and the debtor's local "
                                  "time is still outside the calling window"}
        with self.store.tx(immediate=True):
            self.store.execute(
                "UPDATE debtor_records SET approval_status = 'Approved', approved_by = ?, approved_at = ? "
                "WHERE debtor_id = ?", (who, clock.iso_ms(), debtor_id))
        # One row of evidence behind manual_review_threshold_passed: a human reviewed this
        # draft in a known operating mode. In full_auto the row is still written, but the
        # milestone only counts hitl reviews.
        mode_at_decision = onboarding.ensure_client(self.store, record["client_id"])["operating_mode"]
        gate.record_manual_review_decision(
            self.store, record["client_id"], debtor_id, "approved", who, mode_at_decision,
            reason=channel or "")
        self.audit.record("draft_approved", debtor_id=debtor_id, who=who, channel=channel,
                          client_id=record["client_id"])
        return {"debtor_id": debtor_id, "approved": True, "who": who, "approval_status": "Approved"}

    def reject(self, debtor_id: str, who: str, reason: str) -> Dict[str, Any]:
        record = _fetch_debtor(self.store, debtor_id)
        if not record:
            raise KeyError(f"unknown debtor {debtor_id}")
        with self.store.tx(immediate=True):
            self.store.execute(
                "UPDATE debtor_records SET approval_status = 'Pending', approved_by = NULL, approved_at = NULL "
                "WHERE debtor_id = ? AND approval_status <> 'Outside_Calling_Window'", (debtor_id,))
        mode_at_decision = onboarding.ensure_client(self.store, record["client_id"])["operating_mode"]
        gate.record_manual_review_decision(
            self.store, record["client_id"], debtor_id, "rejected", who, mode_at_decision,
            reason=reason or "")
        self.audit.record("draft_rejected", debtor_id=debtor_id, who=who, reason=reason)
        return {"debtor_id": debtor_id, "approval_status": "Pending", "who": who, "reason": reason}

    # --- the send path -----------------------------------------------------
    def _windowed(self, channel: str) -> bool:
        return channel in config.calling_window().get("applies_to_channels", [])

    def _settings_locked(self, client_id: str) -> Optional[Dict[str, Any]]:
        """The contended row. Postgres takes a row lock here; SQLite already holds the
        write lock because the transaction began with BEGIN IMMEDIATE."""
        sql = "SELECT * FROM client_autonomy_settings WHERE client_id = ?"
        if self.store.backend != "sqlite":
            sql += " FOR UPDATE"
        return self.store.query_one(sql, (client_id,))

    def build_payload(self, artifact: Dict[str, Any], record: Dict[str, Any], settings_row: Dict[str, Any],
                      claim: Dict[str, Any], window: Dict[str, Any], channel: str) -> Dict[str, Any]:
        channel_cfg = config.channel_config(channel)
        destination = record.get("email") if channel == "email" else record.get("phone")
        return {
            "provider": {
                "name": channel_cfg["provider"],
                "simulated": bool(channel_cfg.get("simulated", True)),
                "endpoint": channel_cfg["endpoint"],
                "phone_number_id": f"vapi-number-{record['client_id'].lower()}-01",
                "assistant_id": "assistant-recoverai-pastdue-v1",
            },
            "contact_type": channel,
            "destination": destination,
            "destination_phone": record.get("phone"),
            "debtor_id": record["debtor_id"],
            "client_id": record["client_id"],
            "template_reference": TEMPLATE_REFERENCE.format(channel=channel),
            "subject": artifact["subject"],
            "message": artifact["body"],
            "disclaimer": {"text": artifact["disclaimer"], "position": "absolute_bottom",
                           "occurrences": artifact["disclaimer_occurrences"]},
            "authorization": {
                "signature_reference": claim.get("authorization_reference"),
                # What authorized this contact, decided by the OPERATING MODE in force for
                # this claim - recorded inside the claim transaction - and never by whether
                # a sign-off merely exists on the client row. See the same fix in send().
                "source": claim.get("authorization_source"),
                "authorized_by": claim.get("authorized_by"),
                "authorized_at": claim.get("authorized_at"),
                "signer_name": settings_row.get("signer_name"),
                "signer_title": settings_row.get("signer_title"),
                "document_version": settings_row.get("authorization_document_version"),
                "signed_timestamp": settings_row.get("signed_timestamp"),
            },
            "claim": {
                "claim_token": claim["claim_token"],
                "contact_log_id": claim["contact_log_id"],
                "claimed_at": claim["claimed_at"],
                "operating_mode_at_claim": claim["operating_mode_at_claim"],
                "mode_version_at_claim": claim["mode_version_at_claim"],
            },
            "guardrails": {
                "dnc_flag_at_claim": bool(record.get("dnc_flag")),
                "account_state_at_claim": claim.get("snapshot_at_claim", {}).get("account_state"),
                "balance_at_claim_cents": claim.get("snapshot_at_claim", {}).get("balance_cents"),
                "window_decision": window.get("window_decision"),
                "window": f"{config.calling_window()['start_local']}-{config.calling_window()['end_local']} local",
                "derived_local_time": window.get("derived_local_time"),
                "area_code": window.get("area_code"),
                "tz_name": window.get("tz_name"),
            },
            "rulebook_version": config.load_rulebook()["rulebook_version"],
            "metadata": {
                "debtor_id": record["debtor_id"],
                "client_id": record["client_id"],
                "invoice_number": record.get("invoice_number"),
                "aging_state": record.get("aging_state"),
                "balance_owed": record.get("balance_owed"),
            },
        }

    def send(self, debtor_id: str, channel: str, who: str = "orchestrator", reason: str = "",
             artifact: Optional[Dict[str, Any]] = None, dispatch: bool = True) -> Dict[str, Any]:
        """Claim-then-fire one contact. Returns the full decision record."""
        record = _fetch_debtor(self.store, debtor_id)
        if not record:
            raise KeyError(f"unknown debtor {debtor_id}")
        client_id = record["client_id"]
        onboarding.ensure_client(self.store, client_id)
        if artifact is None:
            artifact = draftsman.render(record, channel=channel)

        window = timezones.window_decision_for(record.get("phone"))
        decision: Optional[str] = None
        reason_detail = ""
        claim: Optional[Dict[str, Any]] = None
        settings_row: Optional[Dict[str, Any]] = None
        mode_version_at_claim: Optional[int] = None
        operating_mode_at_claim: Optional[str] = None
        snapshot_at_claim: Dict[str, Any] = {}

        if CLAIM_INTERLEAVE_HOOK is not None:
            CLAIM_INTERLEAVE_HOOK({"stage": "before_claim_transaction", "debtor_id": debtor_id,
                                   "client_id": client_id})

        # ---- ONE transaction: re-check everything, then write the claim ----
        with self.store.tx(immediate=True):
            settings_row = self._settings_locked(client_id)  # contended row
            live = self.store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
            mode_version_at_claim = int(settings_row["mode_version"])
            operating_mode_at_claim = settings_row["operating_mode"]
            window = timezones.window_decision_for(live.get("phone"))  # re-derived inside the claim
            signoff = onboarding.signoff_state(settings_row)
            # The last-millisecond snapshot re-read INSIDE the transaction that writes the
            # claim: mode, sign-off, DNC, paid status, the balance and the calling window
            # as they are right now - and that snapshot is what the contact log carries.
            # A payment that landed between approval and this instant is visible here and
            # refuses the claim, so nothing is ever fired and then logged as a mistake.
            snapshot_at_claim = {
                "operating_mode": operating_mode_at_claim,
                "mode_version": mode_version_at_claim,
                "signoff_valid": bool(signoff.get("valid")),
                "dnc_flag": int(live.get("dnc_flag") or 0),
                "account_state": live.get("account_state"),
                "balance_cents": (payments.to_cents(live.get("balance_owed"))
                                  if live.get("balance_owed") is not None else None),
                "approval_status": live.get("approval_status"),
                "window_decision": window.get("window_decision"),
                "derived_local_time": window.get("derived_local_time"),
            }
            if CLAIM_INTERLEAVE_HOOK is not None:
                # The re-read above has happened; the claim row has not been written. A
                # hook here is a test holding the transaction open, nothing more.
                CLAIM_INTERLEAVE_HOOK({"stage": "inside_claim_transaction",
                                       "debtor_id": debtor_id, "client_id": client_id,
                                       "snapshot": dict(snapshot_at_claim)})

            if int(live.get("dnc_flag") or 0) == 1:
                decision = BLOCKED_DNC
                reason_detail = "debtor is on the global do-not-contact list (dnc_flag = TRUE)"
            elif live.get("account_state") == payments.STATE_PENDING_ADJUSTMENT:
                decision = BLOCKED_PENDING_ADJUSTMENT
                reason_detail = (
                    "the balance on this record moved with no payment signal behind it: it is "
                    "parked in Pending_Adjustment_Confirmation, so there is no dunning, no fee "
                    "and no send until the client attributes the change")
            elif (live.get("account_state") == payments.STATE_REOPENED_AWAITING_AUDIT
                  or payments.open_reopen_audit(self.store, debtor_id) is not None):
                # The record's open reopen audit: the state names it, and the audit log names it
                # even after another hold on the same record was resolved by a human. Both are
                # read here, in the transaction that writes the claim, so no mode and no later
                # state change can slip a contact past a reversal that has not been audited.
                decision = BLOCKED_REOPENED_AWAITING_AUDIT
                reason_detail = (
                    "the payment on this record was reversed, so it is in Reopened_Awaiting_Audit: "
                    "it can only be contacted after it has re-run the ingestion path from step one "
                    "(re-normalize the source row, re-resolve contact data, re-evaluate the calling "
                    "window, re-check DNC/STOP and the balance) and that re-audit is recorded")
            elif live.get("balance_owed") is None or float(live.get("balance_owed")) <= 0:
                decision = BLOCKED_NO_BALANCE
                reason_detail = (f"nothing to collect: balance_owed = {live.get('balance_owed')} "
                                 f"(account_state = {live.get('account_state')}; "
                                 "never demand payment that is not owed)")
            elif self._windowed(channel) and not window["in_window"]:
                decision = BLOCKED_OUTSIDE_WINDOW
                reason_detail = f"{channel} contact refused: {window['reason']}"
            elif operating_mode_at_claim == "full_auto":
                if not signoff["valid"]:
                    decision = BLOCKED_NO_SIGNOFF if not settings_row.get("signature_reference") \
                        else BLOCKED_SIGNOFF_INVALID
                    reason_detail = f"full_auto requires a valid sign-off: {signoff['reason']}"
                elif not onboarding.scope_allows(settings_row, channel, live.get("aging_state")):
                    decision = BLOCKED_OUT_OF_SCOPE
                    reason_detail = (
                        f"sign-off scope does not authorize {channel} contacts for aging state "
                        f"{live.get('aging_state')} "
                        f"(authorized channels={settings_row.get('authorized_channels')}, "
                        f"aging states={settings_row.get('authorized_aging_states')})"
                    )
            elif operating_mode_at_claim == "hitl":
                if live.get("approval_status") != "Approved":
                    decision = BLOCKED_MODE_HITL
                    reason_detail = ("hitl mode: record is not human-approved "
                                     f"(approval_status = {live.get('approval_status')})")
                elif settings_row.get("signature_reference") and not onboarding.scope_allows(
                        settings_row, channel, live.get("aging_state")):
                    decision = BLOCKED_OUT_OF_SCOPE
                    reason_detail = (f"the client's sign-off scope does not cover {channel} contacts for aging "
                                     f"state {live.get('aging_state')}")
            else:
                decision = BLOCKED_MODE_HITL
                reason_detail = f"unknown operating mode {operating_mode_at_claim!r} - fail closed"

            if decision is None:
                # everything passed: write the claim AND touch the contended row
                claim_token = f"claim-{uuid.uuid4().hex}"
                claimed_at = clock.iso_ms()
                # What authorized THIS contact is the operating mode actually in force for
                # this claim - read inside this transaction - never "does a sign-off exist
                # somewhere on the client row". A signed client running hitl fires on a
                # named human's approval, and that human belongs in the log: the reference
                # is human-approval:<who>@<when>, and approved_by/approved_at carry the
                # same fact in millisecond UTC. In full_auto the authorization is the
                # client's sign-off and there is no human approver to name.
                if operating_mode_at_claim == "full_auto":
                    authorization_source = "automation_signoff"
                    authorized_by = None
                    authorized_at = None
                    auth_reference = settings_row.get("signature_reference") or (
                        f"automation-signoff-missing@{claimed_at}")
                else:
                    authorization_source = "human_approval_in_hitl"
                    authorized_by = live.get("approved_by")
                    authorized_at = live.get("approved_at")
                    auth_reference = "human-approval:{}@{}".format(
                        authorized_by or "unattributed-approver", authorized_at or claimed_at)
                self.store.execute(
                    "UPDATE client_autonomy_settings SET claim_seq = claim_seq + 1, last_claim_at = ? "
                    "WHERE client_id = ?", (claimed_at, client_id))
                log_id = self.store.insert(
                    """INSERT INTO outbound_contact_logs
                         (debtor_id, client_id, attempted_at, contact_type, destination, decision, status,
                          reason_detail, operating_mode_at_claim, mode_version_at_claim,
                          approval_status_at_claim, dnc_flag_at_claim, account_state_at_claim,
                          balance_at_claim_cents, window_decision, derived_local_time,
                          area_code, signature_reference, approved_by, approved_at,
                          claim_token, claimed_at, message_preview)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (debtor_id, client_id, claimed_at, channel,
                     live.get("email") if channel == "email" else live.get("phone"),
                     CLAIMED, "CLAIMED", reason or f"claim written for {channel} contact",
                     operating_mode_at_claim, mode_version_at_claim, live.get("approval_status"),
                     int(live.get("dnc_flag") or 0), snapshot_at_claim.get("account_state"),
                     snapshot_at_claim.get("balance_cents"), window["window_decision"],
                     window.get("derived_local_time"), window.get("area_code"),
                     auth_reference, authorized_by, authorized_at, claim_token, claimed_at,
                     artifact["body"][:280]),
                )
                claim = {"claim_token": claim_token, "contact_log_id": log_id, "claimed_at": claimed_at,
                         "operating_mode_at_claim": operating_mode_at_claim,
                         "mode_version_at_claim": mode_version_at_claim,
                         "authorization_reference": auth_reference,
                         "authorization_source": authorization_source,
                         "authorized_by": authorized_by,
                         "authorized_at": authorized_at,
                         "snapshot_at_claim": snapshot_at_claim}
            # else: transaction commits with no claim written. Nothing to undo.
        # ---- end of the claim transaction; the POST happens only after COMMIT ----

        if claim is None:
            return self._log_blocked(debtor_id, client_id, channel, decision or BLOCKED_MODE_HITL,
                                     reason_detail, settings_row, mode_version_at_claim,
                                     operating_mode_at_claim, window, who, live=live,
                                     snapshot_at_claim=snapshot_at_claim)

        payload = self.build_payload(artifact, record, settings_row or {}, claim, window, channel)
        payload_path = self.payloads_dir / f"{claim['contact_log_id']}__{debtor_id}__{channel}.json"
        payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with self.store.tx(immediate=True):
            self.store.execute("UPDATE outbound_contact_logs SET payload_path = ? WHERE contact_log_id = ?",
                               (str(payload_path), claim["contact_log_id"]))
        self.audit.record("claim_allowed", debtor_id=debtor_id, client_id=client_id, channel=channel,
                          contact_log_id=claim["contact_log_id"], claim_token=claim["claim_token"],
                          operating_mode_at_claim=operating_mode_at_claim,
                          mode_version_at_claim=mode_version_at_claim,
                          window_decision=window["window_decision"], who=who, reason=reason)
        result: Dict[str, Any] = {
            "debtor_id": debtor_id, "client_id": client_id, "channel": channel,
            "decision": CLAIMED, "claimed": True, "contact_log_id": claim["contact_log_id"],
            "claim_token": claim["claim_token"],
            "operating_mode_at_claim": operating_mode_at_claim,
            "mode_version_at_claim": mode_version_at_claim,
            "window_decision": window["window_decision"],
            "derived_local_time": window.get("derived_local_time"),
            "payload": payload, "payload_path": str(payload_path),
            "status": "CLAIMED",
        }
        if not dispatch:
            return result

        response, ok, error = self._dispatch(channel, payload)
        with self.store.tx(immediate=True):
            self.store.execute(
                "UPDATE outbound_contact_logs SET status = ?, provider_response = ?, reason_detail = ? "
                "WHERE contact_log_id = ?",
                ("SENT" if ok else "FAILED", json.dumps(response, default=str), reason or
                 f"{channel} contact dispatched through the simulated provider", claim["contact_log_id"]))
            if ok:
                self.store.execute(
                    "UPDATE debtor_records SET approval_status = 'Sent' WHERE debtor_id = ? AND client_id = ?",
                    (debtor_id, client_id))
        result["status"] = "SENT" if ok else "FAILED"
        result["provider_response"] = response
        result["dispatch_error"] = error
        self.audit.record("post_dispatched" if ok else "post_failed", debtor_id=debtor_id,
                          client_id=client_id, channel=channel, contact_log_id=claim["contact_log_id"],
                          provider_response=response, error=error)
        return result

    def _log_blocked(self, debtor_id: str, client_id: str, channel: str, decision: str, reason_detail: str,
                     settings_row: Optional[Dict[str, Any]], mode_version: Optional[int],
                     operating_mode: Optional[str], window: Dict[str, Any], who: str,
                     live: Optional[Dict[str, Any]] = None,
                     snapshot_at_claim: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A refused attempt is audit evidence: it is logged, but it writes no claim."""
        now = clock.iso()
        halt_at = clock.iso_ms()   # the opt-out instant, in the audit resolution
        snapshot = snapshot_at_claim or {}
        with self.store.tx(immediate=True):
            log_id = self.store.insert(
                """INSERT INTO outbound_contact_logs
                     (debtor_id, client_id, attempted_at, contact_type, destination, decision, status,
                      reason_detail, operating_mode_at_claim, mode_version_at_claim,
                      approval_status_at_claim, dnc_flag_at_claim, account_state_at_claim,
                      balance_at_claim_cents, window_decision, derived_local_time,
                      area_code, signature_reference, approved_by, approved_at,
                      claim_token, claimed_at, message_preview)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (debtor_id, client_id, now, channel, None, decision, "BLOCKED", reason_detail,
                 operating_mode, mode_version, None, None,
                 snapshot.get("account_state"), snapshot.get("balance_cents"),
                 window.get("window_decision"),
                 window.get("derived_local_time"), window.get("area_code"),
                 (settings_row or {}).get("signature_reference"), None, None, None, None, None),
            )
            # A guardrail correctly blocking a send is the product working, not the client
            # failing: it is recorded with an exception count of zero, so it can never be
            # turned into an infraction against the client or counted as a review.
            gate.record_guardrail_block(self.store, client_id=client_id, debtor_id=debtor_id,
                                        who=who, operating_mode=operating_mode or "unknown",
                                        reason=decision, manage_tx=False)
            if decision == BLOCKED_OUTSIDE_WINDOW:
                self.store.execute(
                    """UPDATE debtor_records SET approval_status = 'Outside_Calling_Window',
                         window_decision = ?, derived_local_time = ?, window_decision_at = ?,
                         area_code = ?, tz_name = ? WHERE debtor_id = ?""",
                    (window.get("window_decision"), window.get("derived_local_time"), now,
                     window.get("area_code"), window.get("tz_name"), debtor_id))
            if decision == BLOCKED_DNC:
                self.store.execute(
                    "UPDATE debtor_records SET dnc_flag = 1, dnc_set_at = ? WHERE debtor_id = ?",
                    (halt_at, debtor_id))
        action = "guardrail_freeze" if decision == BLOCKED_OUTSIDE_WINDOW else "claim_blocked"
        self.audit.record(action, debtor_id=debtor_id, client_id=client_id, channel=channel,
                          decision=decision, reason_detail=reason_detail, contact_log_id=log_id,
                          who=who, window_decision=window.get("window_decision"),
                          derived_local_time=window.get("derived_local_time"),
                          area_code=window.get("area_code"))
        return {
            "debtor_id": debtor_id, "client_id": client_id, "channel": channel,
            "decision": decision, "claimed": False, "contact_log_id": log_id,
            "reason_detail": reason_detail, "window_decision": window.get("window_decision"),
            "derived_local_time": window.get("derived_local_time"),
            "operating_mode_at_claim": operating_mode, "mode_version_at_claim": mode_version,
            "status": "BLOCKED",
        }

    # --- provider dispatch (simulated) -------------------------------------
    def _dispatch(self, channel: str, payload: Dict[str, Any]):
        """POST the claimed payload to the (simulated) provider.

        Every network wait is bounded: no code path here can block indefinitely. A
        timeout is reported loudly as a dispatch failure on an already-committed claim
        (status = FAILED), never swallowed and never retried silently.
        """
        channel_cfg = config.channel_config(channel)
        url = self.vendor_base_url.rstrip("/") + channel_cfg["endpoint"]
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "RecoverAI-Agent3/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=DISPATCH_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8")), True, None
        except (TimeoutError, socket.timeout) as exc:
            return ({"error": f"{channel} provider did not answer within {DISPATCH_TIMEOUT:.0f}s"},
                    False, f"provider dispatch timed out after {DISPATCH_TIMEOUT:.0f}s: {exc}")
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            return {"error": str(exc)}, False, str(exc)

    # --- scheduled work ----------------------------------------------------
    def schedule(self, debtor_id: str, channel: str, due_at: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        record = _fetch_debtor(self.store, debtor_id)
        if not record:
            raise KeyError(f"unknown debtor {debtor_id}")
        run = self.queue.schedule(debtor_id, record["client_id"], channel, due_at, reason or "scheduled contact")
        self.audit.record("run_scheduled", run_id=run.run_id, debtor_id=debtor_id,
                          client_id=record["client_id"], channel=channel, due_at=run.due_at, reason=reason)
        return run.as_dict()

    def run_due(self, who: str = "scheduler") -> List[Dict[str, Any]]:
        """Execute scheduled runs that are due. A halted/cancelled run is never fired."""
        results = []
        for run in self.queue.due():
            live = _fetch_debtor(self.store, run.debtor_id)
            if not live:
                continue
            artifact = draftsman.render(live, channel=run.channel)
            outcome = self.send(run.debtor_id, run.channel, who=who,
                                reason=f"scheduled run {run.run_id}", artifact=artifact)
            self.queue.mark(run.run_id, "dispatched", outcome["decision"])
            results.append({"run": run.as_dict(), "outcome": outcome})
        return results
