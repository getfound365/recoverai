"""RecoverAI - the engine facade.

One object that ties the store, the guardrail log, the run queue and the three agents
together. The later website task calls exactly these methods from its API routes; the
demo and the tests call them too, so both paths make identical decisions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import db, gate, ingest, onboarding, orchestrator, payments, webhooks
from . import daily_ops
from .audit import AuditLog
from .orchestrator import Orchestrator
from .runqueue import RunQueue

#: Every table the store creates, in one place. The demo's db_state.json exporter and
#: docs/schema.md read the same list, so the three cannot drift apart again.
TABLES = ("debtor_records", "client_autonomy_settings", "autonomy_mode_history",
          "outbound_contact_logs", "payment_events", "collections_ledger",
          "balance_edit_events", "reopen_audit_events", "billing_route_verifications",
          "lifecycle_transition_log", "manual_review_decisions",
          "commercial_msa_records", "ingestion_batches",
          "onboarding_attestations", "collections_tracking_choices")


class RecoverAI:
    def __init__(self, db_path: Optional[str] = None, backend: Optional[str] = None,
                 vendor_base_url: str = "http://127.0.0.1:8801", fresh: bool = False,
                 audit_path: Optional[Path] = None, paths: Optional[Dict[str, Path]] = None,
                 runqueue_path: Optional[Path] = None):
        self.paths = paths or db.ensure_dirs()
        self.store = db.fresh_store(db_path, backend) if fresh else db.Store(db_path, backend)
        if not fresh:
            self.store.init_schema()
        self.audit = AuditLog(audit_path)
        # A caller that gives us a path gets a queue of RECORD, not just an in-memory one:
        # it is read back on open and written on close. The console needs this — every one
        # of its reads runs in a fresh process, so an in-memory queue would be empty every
        # time and the "switch to manual kills the schedule" demo would have nothing to
        # kill. Callers that pass nothing keep the old in-memory behaviour exactly.
        self._runqueue_path = Path(runqueue_path) if runqueue_path else None
        self.queue = (RunQueue.load(self._runqueue_path) if self._runqueue_path
                      else RunQueue())
        self.orchestrator = Orchestrator(self.store, self.audit, self.queue,
                                         vendor_base_url=vendor_base_url, paths=self.paths)

    # --- Agent 1 -----------------------------------------------------------
    def import_ledger(self, base_url: str, sources: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
        """sources: [{"path": "/mock/quickbooks/invoices", "source_system": "QuickBooks Online",
                      "client_id": "CLI-NORTHWIND"}]"""
        reports = []
        for src in sources:
            result = ingest.import_from_endpoint(self.store, base_url, src["path"], src["source_system"],
                                                 src.get("client_id"))
            report = result.report()
            expect = src.get("expected_client_id") or src.get("client_id")
            actual_ids = sorted({r["client_id"] for r in result.records})
            report["client_ids_seen"] = actual_ids
            report["client_id_matches_expectation"] = (expect is None or actual_ids == [expect])
            reports.append(report)
            self.audit.record("ledger_imported", source_system=src["source_system"], endpoint=report["endpoint"],
                              records_normalized=report["records_normalized"],
                              records_rejected=report["records_rejected"],
                              unmapped_columns=report["unmapped_columns"], client_ids=actual_ids)
        return reports

    # --- Phase 3: the spreadsheet intake (a client's own CSV/Excel file) ----
    def preview_spreadsheet(self, *, file_name: str, content: Any, client_id: str,
                            uploader_id: str, source_format: Optional[str] = None) -> Dict[str, Any]:
        """Parse a CSV/Excel file and report what WOULD import. Writes nothing at all."""
        return ingest.preview_spreadsheet(file_name=file_name, content=content, client_id=client_id,
                                          uploader_id=uploader_id, source_format=source_format)

    def register_ingestion_batch(self, preview: Dict[str, Any], *, uploader_id: str,
                                 warranty_acknowledged: bool, who: Optional[str] = None,
                                 reason: str = "", note: Optional[str] = None) -> Dict[str, Any]:
        """Append the immutable ingestion_batches record a COMMIT is checked against.

        Refuses without an explicitly ticked Step 3 warranty, or without a valid signed
        authorization on file to bind that warranty to.
        """
        result = ingest.register_ingestion_batch(
            self.store, preview, uploader_id=uploader_id,
            warranty_acknowledged=warranty_acknowledged, who=who or uploader_id,
            reason=reason, note=note, audit=self.audit)
        if not result.get("ok"):
            self.audit.record("ingestion_batch_refused", **{k: v for k, v in result.items()})
        return result

    def commit_spreadsheet(self, preview: Dict[str, Any], *, batch_id: str,
                           uploader_id: Optional[str] = None,
                           who: Optional[str] = None) -> Dict[str, Any]:
        """Import a previewed file through Agent 1's normalizer. Refuses without a batch."""
        result = ingest.commit_spreadsheet(self.store, preview, batch_id=batch_id,
                                          uploader_id=uploader_id, who=who, audit=self.audit)
        if not result.get("ok"):
            self.audit.record("spreadsheet_commit_refused",
                              **{k: v for k, v in result.items() if k != "accepted_rows"})
        return result

    def ingestion_batches(self, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if client_id:
            return self.store.query(
                "SELECT * FROM ingestion_batches WHERE client_id = ? ORDER BY seq", (client_id,))
        return self.store.query("SELECT * FROM ingestion_batches ORDER BY seq")

    def verify_ingestion_batches(self) -> Dict[str, Any]:
        """Recompute the batch chain of record from its own rows (tamper-evidence)."""
        return ingest.verify_ingestion_batches(self.store)

    # --- onboarding back end ----------------------------------------------
    def capture_signoff(self, client_id: str, signer_name: str, signer_title: str,
                        channels: Optional[Sequence[str]] = None,
                        aging_states: Optional[Sequence[str]] = None, who: Optional[str] = None,
                        validity_days: Optional[int] = None,
                        scope_pairs: Optional[Sequence[Sequence[str]]] = None) -> Dict[str, Any]:
        return onboarding.capture_signoff(self.store, client_id, signer_name, signer_title,
                                          channels, aging_states, who or signer_name, self.audit,
                                          validity_days=validity_days, scope_pairs=scope_pairs)

    def switch_mode(self, client_id: str, new_mode: str, who: str, reason: str = "") -> Dict[str, Any]:
        return onboarding.switch_mode(self.store, client_id, new_mode, who, reason, self.audit, self.queue)

    def revoke_signoff(self, client_id: str, who: str, reason: str) -> Dict[str, Any]:
        return onboarding.revoke_signoff(self.store, client_id, who, reason, self.audit, self.queue)

    def sweep_expired_signoffs(self) -> List[Dict[str, Any]]:
        return onboarding.sweep_expired_signoffs(self.store, self.audit, self.queue)

    # --- Agent 2 + 3 -------------------------------------------------------
    def queue_draft(self, debtor_id: str, channel: str = "email") -> Dict[str, Any]:
        return self.orchestrator.queue_draft(debtor_id, channel)

    def approve(self, debtor_id: str, who: str, channel: Optional[str] = None) -> Dict[str, Any]:
        return self.orchestrator.approve(debtor_id, who, channel)

    def reject(self, debtor_id: str, who: str, reason: str = "") -> Dict[str, Any]:
        return self.orchestrator.reject(debtor_id, who, reason)

    def send(self, debtor_id: str, channel: str, who: str = "orchestrator", reason: str = "",
             artifact: Optional[Dict[str, Any]] = None, dispatch: bool = True) -> Dict[str, Any]:
        return self.orchestrator.send(debtor_id, channel, who=who, reason=reason, artifact=artifact,
                                      dispatch=dispatch)

    def schedule(self, debtor_id: str, channel: str, due_at: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        return self.orchestrator.schedule(debtor_id, channel, due_at, reason)

    def dispatch_new_draft(self, debtor_id: str, channel: str = "email", who: str = "orchestrator",
                           dispatch: bool = True) -> Dict[str, Any]:
        """What Agent 3 does the moment Agent 2 produces a notice.

        In `hitl` the notice queues as Pending and nothing else happens. In `full_auto`
        the orchestrator goes straight to the atomic claim - which still re-checks the
        sign-off, the scope, the calling window and the DNC flag inside its transaction.
        """
        row = self.settings_or_default(debtor_id)
        if row["operating_mode"] == "full_auto":
            artifact = self.queue_draft(debtor_id, channel)
            return self.send(debtor_id, channel, who=who, reason="full_auto: fires on generation",
                             artifact=artifact, dispatch=dispatch)
        artifact = self.queue_draft(debtor_id, channel)
        return {"debtor_id": debtor_id, "client_id": row["client_id"], "channel": channel,
                "decision": "QUEUED_PENDING_HUMAN_APPROVAL", "claimed": False,
                "approval_status": "Pending", "draft_path": artifact["path"]}

    def settings_or_default(self, debtor_id: str) -> Dict[str, Any]:
        record = self.debtor(debtor_id)
        if not record:
            raise KeyError(f"unknown debtor {debtor_id}")
        return onboarding.ensure_client(self.store, record["client_id"])

    def run_due(self, who: str = "scheduler") -> List[Dict[str, Any]]:
        return self.orchestrator.run_due(who)

    # --- inbound -----------------------------------------------------------
    def handle_inbound(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return webhooks.process_inbound(self.store, payload, self.audit, self.queue)

    # --- one payment event, three consequences -----------------------------
    def record_payment(self, debtor_id: str, amount: Any, confirmation_channel: str,
                       attested_by: Optional[str] = None, external_reference: Optional[str] = None,
                       signal_at: Optional[str] = None, note: Optional[str] = None) -> Dict[str, Any]:
        """Accept a payment signal from any of the three channels.

        One transaction: debit the balance and set the state, purge every pending and
        scheduled contact attempt for this debtor, append the fee ledger row. Anything
        failing rolls all three back.
        """
        return payments.apply_payment(self.store, self.queue, self.audit, debtor_id=debtor_id,
                                      amount=amount, confirmation_channel=confirmation_channel,
                                      attested_by=attested_by, external_reference=external_reference,
                                      signal_at=signal_at, note=note)

    def record_reversal(self, debtor_id: str, **kwargs: Any) -> Dict[str, Any]:
        """A bounced or refunded payment: un-bill by counter-entry and open a mandatory audit.

        NOT a resume: the record goes to Reopened_Awaiting_Audit, the purged queue stays purged,
        and the claim refuses with REOPENED_AWAITING_AUDIT until run_reopen_audit() completes.
        """
        return payments.record_reversal(self.store, self.queue, self.audit,
                                        debtor_id=debtor_id, **kwargs)

    def record_balance_edit(self, debtor_id: str, new_balance: Any, who: str, reason: str,
                            note: Optional[str] = None) -> Dict[str, Any]:
        """A balance change with no payment signal behind it: park it, do not guess."""
        return payments.record_balance_edit(self.store, self.queue, self.audit, debtor_id=debtor_id,
                                            new_balance=new_balance, who=who, reason=reason, note=note)

    def resolve_balance_edit(self, debtor_id: str, resolution: str, who: str, reason: str,
                             **kwargs: Any) -> Dict[str, Any]:
        """`payment` (billable, through the normal payment path) or `adjustment` (never billable)."""
        return payments.resolve_balance_edit(self.store, self.queue, self.audit, debtor_id=debtor_id,
                                             resolution=resolution, who=who, reason=reason, **kwargs)

    def ledger(self, client_id: str) -> Dict[str, Any]:
        """The ledger read model for one client: lines + net totals, real state only."""
        return payments.ledger_for_client(self.store, client_id)

    # --- the reopen audit: the only way back from a reversed payment --------
    def run_reopen_audit(self, debtor_id: str, performed_by: str, reason: str) -> Dict[str, Any]:
        """Complete the reopen audit for a reversed record: ingestion, step one, again.

        Re-normalize the stored source row, re-resolve contact data, re-evaluate the calling
        window, re-check DNC/STOP and the paid status/balance, record the completion with a
        timestamp and the administrator - and only then does the record re-enter normal flow,
        still governed by the client's operating mode and every guardrail.
        """
        return payments.run_reopen_audit(self.store, self.queue, self.audit,
                                         debtor_id=debtor_id, performed_by=performed_by,
                                         reason=reason)

    def reopen_audits(self, debtor_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """The append-only reopen-audit log: what opened each audit, what closed it, refusals."""
        return payments.reopen_audit_events(self.store, debtor_id)

    def open_reopen_audit(self, debtor_id: str) -> Optional[Dict[str, Any]]:
        """The reversal whose reopen audit this debtor has not completed yet, if any."""
        return payments.open_reopen_audit(self.store, debtor_id)

    def pending_reopen_audits(self) -> List[Dict[str, Any]]:
        """Every record whose reopen audit is still open: no mode can contact these."""
        return payments.pending_reopen_audits(self.store)

    def ledger_for_debtor(self, debtor_id: str) -> List[Dict[str, Any]]:
        return payments.ledger_for_debtor(self.store, debtor_id)

    def payment_events(self, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if client_id:
            return self.store.query(
                "SELECT * FROM payment_events WHERE client_id = ? ORDER BY event_id", (client_id,))
        return self.store.query("SELECT * FROM payment_events ORDER BY event_id")

    def balance_edit_events(self, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if client_id:
            return self.store.query(
                "SELECT * FROM balance_edit_events WHERE client_id = ? ORDER BY event_id",
                (client_id,))
        return self.store.query("SELECT * FROM balance_edit_events ORDER BY event_id")

    def pending_adjustments(self) -> List[Dict[str, Any]]:
        """Every record parked in Pending_Adjustment_Confirmation, with the edit behind it."""
        parked = self.store.query(
            "SELECT * FROM debtor_records WHERE account_state = ? ORDER BY debtor_id",
            (payments.STATE_PENDING_ADJUSTMENT,))
        for row in parked:
            row["open_edit_event"] = payments._parked_edit(self.store, row["debtor_id"])
        return parked

    # --- Phase 2: the beta-to-active gate ----------------------------------
    def lifecycle_status(self, client_id: str) -> str:
        return gate.lifecycle_status(self.store, client_id)

    def milestone_state(self, client_id: str) -> Dict[str, Any]:
        """Every milestone, its honest value and exactly why it is or is not met."""
        return gate.milestone_state(self.store, client_id)

    def launch_condition(self, client_id: str) -> Dict[str, Any]:
        return gate.launch_condition(self.store, client_id)

    def milestone_tooltip(self, client_id: str) -> Dict[str, Any]:
        """The Agent 4 tooltip payload: which milestones are outstanding, in plain English."""
        return gate.milestone_tooltip(self.store, client_id)

    def verify_milestone_b_receipt(self, receipt: Any, client_id: str, recorded_by: str,
                                   verification_source: str, **kwargs: Any) -> Dict[str, Any]:
        """Validate a billing-route receipt and record the outcome. Never moves money."""
        result = gate.verify_milestone_b_receipt(
            self.store, receipt, client_id=client_id, recorded_by=recorded_by,
            verification_source=verification_source, **kwargs)
        gate.emit_agent4_alert(self.audit, result, client_id=client_id)
        return result

    def set_lifecycle_status(self, client_id: str, new_status: str, who: str,
                             reason: str) -> Dict[str, Any]:
        """The only way a client moves between beta and full active."""
        result = gate.set_lifecycle_status(self.store, client_id, new_status, who, reason)
        self.audit.record("lifecycle_transition", **result)
        return result

    def refresh_manual_review_milestone(self, client_id: str, who: str,
                                        reason: str) -> Dict[str, Any]:
        return gate.refresh_manual_review_milestone(self.store, client_id, who, reason)

    def record_commercial_msa(self, client_id: str, *, who: str, reason: str,
                              **agreement: Any) -> Dict[str, Any]:
        """Record an EXECUTED commercial Master Services Agreement. The only way the
        commercial milestone can be satisfied: it writes evidence, never just a flag."""
        result = gate.record_commercial_msa(self.store, client_id, who=who, reason=reason,
                                            **agreement)
        self.audit.record("commercial_msa_recorded", **result)
        return result

    def commercial_msa_records(self, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return gate.commercial_msa_records(self.store, client_id)

    def commercial_msa_status(self, client_id: str) -> Dict[str, Any]:
        return gate.commercial_msa_milestone_status(self.store, client_id)

    def launch_live_outbound(self, client_id: str, channel: str = "voice",
                             who: str = "orchestrator", reason: str = "") -> Dict[str, Any]:
        """The live collections launcher: always refuses today. The sandbox path is separate."""
        try:
            return gate.launch_live_outbound(self.store, client_id, channel, who, reason)
        except gate.GateLockedException as exc:
            self.audit.record("gate_locked", client_id=client_id, channel=channel, who=who,
                              reason_token=exc.reason_token,
                              outstanding_milestones=exc.outstanding)
            raise

    def billing_route_verifications(self, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if client_id:
            return self.store.query(
                "SELECT * FROM billing_route_verifications WHERE client_id = ? ORDER BY seq",
                (client_id,))
        return self.store.query("SELECT * FROM billing_route_verifications ORDER BY seq")

    def lifecycle_transitions(self, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return gate.transition_log(self.store, client_id)

    def manual_review_decisions(self, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if client_id:
            return self.store.query(
                "SELECT * FROM manual_review_decisions WHERE client_id = ? ORDER BY decision_id",
                (client_id,))
        return self.store.query("SELECT * FROM manual_review_decisions ORDER BY decision_id")

    # --- introspection (used by the demo transcript and the console) --------
    def debtor(self, debtor_id: str) -> Optional[Dict[str, Any]]:
        return self.store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))

    def settings(self, client_id: str) -> Optional[Dict[str, Any]]:
        """Client settings, created in the default hitl mode if we have never seen it."""
        return onboarding.ensure_client(self.store, client_id)

    def timeline(self, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if client_id:
            return self.store.query(
                "SELECT * FROM autonomy_mode_history WHERE client_id = ? ORDER BY history_id", (client_id,))
        return self.store.query("SELECT * FROM autonomy_mode_history ORDER BY history_id")

    def contacts(self) -> List[Dict[str, Any]]:
        return self.store.query("SELECT * FROM outbound_contact_logs ORDER BY contact_log_id")

    def snapshot(self) -> Dict[str, List[Dict[str, Any]]]:
        return {t: self.store.query(f"SELECT * FROM {t}") for t in TABLES}

    def snapshot_json(self) -> str:
        return json.dumps(self.snapshot(), indent=2, default=str)

    def close(self) -> None:
        if self._runqueue_path is not None:
            self.queue.dump(self._runqueue_path)
        else:
            self.queue.dump()
        self.store.close()

    # --- Phase 3, pass 3: the console's daily operations -------------------
    #
    # Read models only, plus three methods that run a mutation through an engine path
    # that already existed. The console calls exactly these (via tools/console_bridge.py)
    # and renders their payloads; it decides nothing itself.
    def console_queue(self, client_id: str) -> Dict[str, Any]:
        """One row per debtor: account status, balance, aging, last attempt, schedule."""
        return daily_ops.queue_rows(self.store, client_id, runqueue=self.queue)

    def console_mode(self, client_id: str) -> Dict[str, Any]:
        """What mode is in force, whether full_auto is selectable and why, plus the
        immutable mode history and the client's live scheduled runs."""
        return daily_ops.mode_control(self.store, client_id, runqueue=self.queue)

    def console_reviews(self, client_id: str) -> Dict[str, Any]:
        """The HITL queue: Pending drafts, what blocks an approval, and the review count
        the manual-review milestone is measured from."""
        return daily_ops.pending_reviews(self.store, client_id)

    def console_decide(self, client_id: str, debtor_id: str, action: str, who: str,
                       reason: str = "") -> Dict[str, Any]:
        """Approve or reject one queued draft through the real engine path."""
        return daily_ops.decide_review(self.store, self.orchestrator, client_id=client_id,
                                       debtor_id=debtor_id, action=action, who=who,
                                       reason=reason)

    def console_interactions(self, debtor_id: str) -> Dict[str, Any]:
        """Every contact attempt for one debtor, with the exact payload each carried."""
        return daily_ops.interactions(self.store, debtor_id,
                                      payloads_dir=self.paths.get("payloads"))

    def console_alerts(self, client_id: Optional[str] = None,
                       limit: int = 60) -> Dict[str, Any]:
        """The engine's own append-only action log, newest first, in plain English."""
        return daily_ops.alerts(self.audit.path, client_id=client_id, limit=limit)

    def console_money(self, client_id: str) -> Dict[str, Any]:
        """The ledger lines, the payment signals behind them, and the statement status."""
        return daily_ops.money_trail(self.store, client_id)

    def halt_pending_for_client(self, client_id: str, who: str,
                                reason: str = "") -> Dict[str, Any]:
        """THE atomic halt behind "Switch to Manual (HITL)".

        Two halves, both applied whatever the current mode is:
          1. the client's mode is put in `hitl` (logged to the immutable
             autonomy_mode_history by `onboarding.switch_mode`, which refuses nothing here
             because hitl never needs a sign-off), and
          2. every `scheduled` run for the client is halted in the queue.

        Step 2 runs even when the client is already in hitl — otherwise a second press
        would be a no-op and scheduled work would survive a halt it was supposed to stop.
        Halting is not deleting: the runs stay in the queue in state `halted`, with the
        reason, so the console can show exactly what was stopped and why.
        """
        settings = onboarding.ensure_client(self.store, client_id)
        old_mode = settings["operating_mode"]
        mode_result: Optional[Dict[str, Any]] = None
        if old_mode != "hitl":
            mode_result = self.switch_mode(client_id, "hitl", who,
                                           reason or "client pressed Switch to Manual (HITL)")
        halted = self.queue.halt_pending_for_client(
            client_id, reason or "client pressed Switch to Manual (HITL)")
        for run in halted:
            self.audit.record("run_halted", run_id=run.run_id, debtor_id=run.debtor_id,
                              client_id=client_id, channel=run.channel,
                              reason=reason or "client pressed Switch to Manual (HITL)")
        if mode_result is None:
            self.audit.record("halt_requested", client_id=client_id, who=who,
                              old_mode=old_mode, new_mode="hitl",
                              reason=reason or "client pressed Switch to Manual (HITL)",
                              note="already in hitl: the mode did not change, but the halt ran")
        return {
            "client_id": client_id,
            "who": who,
            "reason": reason or "client pressed Switch to Manual (HITL)",
            "old_mode": old_mode,
            "new_mode": "hitl",
            "mode_change": mode_result,
            "mode_changed": bool(mode_result and mode_result.get("changed")),
            "halted_runs": [
                {"run_id": r.run_id, "debtor_id": r.debtor_id, "channel": r.channel,
                 "due_at": r.due_at, "state": r.state, "state_reason": r.state_reason}
                for r in halted],
            "halted_count": len(halted),
            "halt_note": ("halted runs stay in the queue in state `halted` with their reason: "
                          "a halt stops work, it does not erase the record of it"),
        }

    def console_schedule(self, debtor_id: str, channel: str = "email",
                         due_at: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        """Put one contact on the schedule, through the engine's own `schedule`."""
        return self.schedule(debtor_id, channel, due_at,
                            reason or "queued from the console")

    def console_simulate_payment(self) -> Dict[str, Any]:
        """Run the engine's real payment path against a simulated webhook signal."""
        return daily_ops.simulate_payment(self)

    # --- Phase 4: the onboarding ladder (steps 1-3) ------------------------
    def console_onboarding(self, client_id: str) -> Dict[str, Any]:
        """Every onboarding surface in one read: the fee-tracking choice and its trail, the
        Step 2 acknowledgement, the Step 3 gate and its attestation, the sign-off and what
        its hash covers, the batches this client's office has handed over, and the parked
        adjustment list. Reads only; the console decides nothing."""
        return daily_ops.onboarding_screen(self.store, client_id)

    def set_tracking_mode(self, client_id: str, new_mode: str, who: str,
                          reason: str = "") -> Dict[str, Any]:
        """Step 1, changed later: append-only trail plus a real rebind of the sign-off."""
        return onboarding.set_tracking_mode(self.store, client_id, new_mode, who, reason,
                                            self.audit, self.queue)

    def record_step2_acknowledgement(self, client_id: str, acknowledged: Any, who: str,
                                     source: str = "console",
                                     note: Optional[str] = None) -> Dict[str, Any]:
        return onboarding.record_step2_acknowledgement(self.store, client_id, acknowledged,
                                                       who, source, note, self.audit)

    def record_step3_attestation(self, client_id: str, acknowledged: Any, who: str,
                                 source: str = "console",
                                 note: Optional[str] = None) -> Dict[str, Any]:
        """The real Step 3 gate. Refuses unless the flag is explicitly TRUE."""
        return onboarding.record_step3_attestation(self.store, client_id, acknowledged,
                                                   who, source, note, self.audit)

    def step3_gate_state(self, client_id: str) -> Dict[str, Any]:
        return onboarding.step3_gate_state(self.store, client_id)

    def onboarding_attestations(self, client_id: str,
                                attestation_type: Optional[str] = None) -> List[Dict[str, Any]]:
        return onboarding.attestations(self.store, client_id, attestation_type)

    def tracking_choices(self, client_id: str) -> Dict[str, Any]:
        return onboarding.tracking_state(self.store, client_id)

    # --- Phase 4: the reports view ----------------------------------------
    def console_reports(self, client_id: str) -> Dict[str, Any]:
        """Weekly and monthly counts from the engine's own logs. Real counts only."""
        return daily_ops.reports(self.store, client_id)

    # --- Phase 4: the Integrations Hub (simulation panel) ------------------
    def integrations_hub(self, *, client_id: str, sample: Optional[str] = None) -> Dict[str, Any]:
        """Run a SAMPLE vendor export through the real mapping logic and show what a live
        sync WOULD map. Nothing here connects anywhere: no request leaves this process, no
        QuickBooks or Xero credential exists, and the panel says so in those words.

        The mapping decision is `ingest.normalize()` - the identical call a live import
        makes - so what the panel shows is the mapper's real verdict on the sample file,
        not a frontend illustration of it.
        """
        return daily_ops.integrations_hub(self.store, client_id=client_id, sample=sample)

    def console_integration_samples(self) -> List[Dict[str, Any]]:
        return daily_ops.integration_samples()
