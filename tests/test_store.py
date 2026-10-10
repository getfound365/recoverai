"""The store contract: exactly the expected tables, the specified columns, and a timeline
that cannot be rewritten even by our own code.
"""
from __future__ import annotations

import sqlite3
import unittest

from engine import config, db, gate
from tests.base import EngineTestCase

REQUIRED_COLUMNS = {
    "debtor_records": {"debtor_id", "client_id", "name", "email", "phone", "balance_owed",
                       "aging_state", "approval_status", "dnc_flag", "account_state",
                       "balance_updated_at"},
    "client_autonomy_settings": {"client_id", "operating_mode", "has_valid_signoff", "signer_name",
                                 "signer_title", "signature_reference", "authorized_channels",
                                 "authorized_aging_states", "signed_timestamp",
                                 # Phase 2: the lifecycle state and the milestone flags
                                 "lifecycle_status", "manual_review_threshold_passed",
                                 "live_billing_verified", "commercial_msa_executed",
                                 "billing_route_demonstrated_in_sandbox",
                                 "live_billing_receipt_metadata"},
    "autonomy_mode_history": {"who", "old_mode", "new_mode", "timestamp", "reason"},
    "outbound_contact_logs": {"debtor_id", "client_id", "contact_type", "signature_reference",
                              "operating_mode_at_claim", "window_decision",
                              "account_state_at_claim", "balance_at_claim_cents",
                              # who authorized this contact, to the millisecond
                              "approved_by", "approved_at"},
    # the payment path: the intake log, the fee ledger and the balance-edit log
    "payment_events": {"event_id", "client_id", "debtor_id", "confirmation_channel", "amount_cents",
                       "attested_by", "external_reference", "signal_at", "confirmation_latency",
                       "recorded_at", "balance_before_cents", "balance_after_cents",
                       "overpayment_cents", "account_state_after", "intake_reference",
                       "queue_purged_at", "cancelled_runs", "cancelled_run_count",
                       "outbound_logs_untouched"},
    "collections_ledger": {"entry_id", "client_id", "debtor_id", "entry_type", "amount_cents",
                           "fee_cents", "fee_basis_points", "fee_rounding_rule",
                           "confirmation_channel", "confirmation_latency", "attested_by",
                           "external_reference", "signal_at", "recorded_at",
                           "balance_before_cents", "balance_after_cents", "debtor_state_before",
                           "debtor_state_after", "linked_entry_id", "payment_event_id",
                           "intake_reference"},
    "balance_edit_events": {"event_id", "client_id", "debtor_id", "event_type",
                            "balance_before_cents", "balance_after_cents", "delta_cents", "who",
                            "reason", "recorded_at", "billable", "resolution_channel",
                            "ledger_entry_id", "linked_event_id"},
    # the reopen audit invariant: what a reversal opened, what a re-audit found, what was refused
    "reopen_audit_events": {"event_id", "client_id", "debtor_id", "event_type",
                            "collection_entry_id", "reversal_entry_id", "payment_event_id",
                            "un_billed_amount_cents", "un_billed_fee_cents",
                            "balance_before_cents", "balance_after_cents",
                            "account_state_before", "account_state_after", "source_system",
                            "source_ref", "renormalized", "source_balance_cents",
                            "source_balance_matches_engine", "contact_email_resolved",
                            "contact_phone_resolved", "window_decision", "derived_local_time",
                            "area_code", "tz_name", "dnc_flag", "paid_status",
                            "ledger_balance_cents", "approval_status_after", "hold_state_kept",
                            "refuse_reason", "performed_by", "reason", "recorded_at", "note"},
    # Phase 2: the gate's chain of record, its transition log and the review evidence
    "billing_route_verifications": {"verification_id", "account_id", "livemode",
                                    "charges_enabled", "payouts_enabled", "verified_at",
                                    "verification_source", "receipt_json", "receipt_sha256",
                                    "prev_chain_hash", "chain_hash", "recorded_at", "recorded_by"},
    "lifecycle_transition_log": {"recorded_at", "event", "client_id", "field", "old_value",
                                 "new_value", "validating_parameters",
                                 "executing_administrator", "reason"},
    "manual_review_decisions": {"client_id", "debtor_id", "decision", "decided_by", "decided_at",
                                "operating_mode_at_decision", "client_caused_exception"},
    # Phase 2: the EVIDENCE behind the commercial milestone (the milestone itself, not the
    # mirror column on client_autonomy_settings - see engine/gate.py)
    "commercial_msa_records": {"msa_id", "client_id", "counterparty_legal_name",
                               "signatory_name", "signatory_title", "agreement_reference",
                               "document_version", "executed_at", "document_sha256",
                               "recorded_at", "recorded_by", "note"},
    # Phase 3: the immutable record a spreadsheet upload must have before it can commit
    "ingestion_batches": {"seq", "batch_id", "client_id", "uploader_id", "file_name",
                          "file_sha256", "source_format", "rows_seen", "rows_accepted",
                          "rows_rejected", "preview_fingerprint", "mapping_json",
                          "rejected_json", "consumer_debt_marker", "consumer_debt_signals",
                          "recorded_at_ms", "recorded_at", "warranty_acknowledged",
                          "warranty_signature_reference", "warranty_document_version",
                          "warranty_text_sha256", "warranty_binding_sha256", "batch_sha256",
                          "prev_chain_hash", "chain_hash", "recorded_by", "reason", "note"},
}


class TestSchema(EngineTestCase):
    def test_the_store_holds_exactly_the_expected_tables(self):
        rows = self.engine.store.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        self.assertEqual(sorted(r["name"] for r in rows), sorted(REQUIRED_COLUMNS))

    def test_required_columns_exist(self):
        for table, columns in REQUIRED_COLUMNS.items():
            actual = {r["name"] for r in self.engine.store.query(f"PRAGMA table_info({table})")}
            missing = columns - actual
            self.assertEqual(missing, set(), f"{table} is missing {missing}")

    def test_approval_status_is_constrained(self):
        self.add_debtor("D-1")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE debtor_records SET approval_status = 'Whatever' WHERE debtor_id = 'D-1'")

    def test_the_payment_lifecycle_states_are_constrained(self):
        self.add_debtor("D-1")
        for state in ("Paid", "Partially_Paid", "Pending_Adjustment_Confirmation", "Open",
                      "Reopened_Awaiting_Audit"):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE debtor_records SET account_state = ? WHERE debtor_id = 'D-1'", (state,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE debtor_records SET account_state = 'Whatever' WHERE debtor_id = 'D-1'")

    def test_operating_mode_is_constrained(self):
        self.engine.settings("CLI-TEST")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE client_autonomy_settings SET operating_mode = 'yolo' WHERE client_id = ?",
                    ("CLI-TEST",))

    def test_schema_sql_is_shipped_and_dialect_marked(self):
        sql = (config.repo_root() / "engine" / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS debtor_records", sql)
        self.assertIn(":sqlite-only", sql)
        self.assertIn(":postgres-only", sql)


class TestAppendOnlyTimeline(EngineTestCase):
    def test_update_is_rejected(self):
        self.engine.switch_mode("CLI-TEST", "full_auto", "client", "attempt without sign-off")
        row = self.engine.timeline("CLI-TEST")[0]
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.engine.store.execute("UPDATE autonomy_mode_history SET reason = 'rewritten' WHERE history_id = ?",
                                      (row["history_id"],))
        self.assertIn("append-only", str(ctx.exception))
        self.assertEqual(self.engine.timeline("CLI-TEST")[0]["reason"], row["reason"])

    def test_delete_is_rejected(self):
        self.engine.switch_mode("CLI-TEST", "full_auto", "client", "attempt without sign-off")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.engine.store.execute("DELETE FROM autonomy_mode_history WHERE client_id = ?", ("CLI-TEST",))
        self.assertIn("append-only", str(ctx.exception))
        self.assertEqual(len(self.engine.timeline("CLI-TEST")), 1)

    def test_history_survives_across_connections(self):
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "signer", "signed")
        other = self.new_engine()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                other.store.execute("DELETE FROM autonomy_mode_history")
            self.assertEqual(len(other.timeline("CLI-TEST")), 1)
        finally:
            other.store.close()

    def test_a_real_mode_change_appends_a_row_with_all_five_fields(self):
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "signer@cedar", "sign-off on file")
        row = self.engine.timeline("CLI-TEST")[-1]
        self.assertEqual(row["old_mode"], "hitl")
        self.assertEqual(row["new_mode"], "full_auto")
        self.assertEqual(row["who"], "signer@cedar")
        self.assertEqual(row["reason"], "sign-off on file")
        self.assertTrue(row["timestamp"])


class TestClaimBookkeeping(EngineTestCase):
    def test_blocked_attempts_are_logged_without_a_claim(self):
        self.add_debtor("D-1")
        self.engine.send("D-1", "voice")
        rows = self.contacts_for("D-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "BLOCKED")
        self.assertIsNone(rows[0]["claim_token"])
        self.assertEqual(self.engine.settings("CLI-TEST")["claim_seq"], 0)

    def test_a_committed_claim_bumps_the_contended_row(self):
        self.add_debtor("D-1", approval_status="Approved")
        self.engine.send("D-1", "voice")
        settings = self.engine.settings("CLI-TEST")
        self.assertEqual(settings["claim_seq"], 1)
        self.assertIsNotNone(settings["last_claim_at"])
        self.assertEqual(self.claim_rows("D-1")[0]["operating_mode_at_claim"], "hitl")

    def test_contact_log_links_back_to_the_signature_reference(self):
        self.add_debtor("D-1")
        signoff = self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        self.engine.send("D-1", "voice")
        row = self.claim_rows("D-1")[0]
        self.assertEqual(row["signature_reference"], signoff["signature_reference"])
        self.assertEqual(row["mode_version_at_claim"], self.engine.settings("CLI-TEST")["mode_version"])

    def test_duplicate_claim_tokens_are_impossible(self):
        self.add_debtor("D-1", approval_status="Approved")
        self.engine.send("D-1", "voice")
        token = self.claim_rows("D-1")[0]["claim_token"]
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    """INSERT INTO outbound_contact_logs
                         (debtor_id, client_id, attempted_at, contact_type, decision, status, claim_token)
                       VALUES ('D-1','CLI-TEST','2026-10-06T14:00:00Z','voice','CLAIMED','CLAIMED',?)""",
                    (token,))


class TestBackendSelection(unittest.TestCase):
    def test_sqlite_is_the_default_and_needs_no_setup(self):
        store = db.Store(db_path="/tmp/recoverai-backend-check.db", backend=None)
        try:
            self.assertEqual(store.backend, "sqlite")
            store.init_schema()
            self.assertTrue(store.query("SELECT name FROM sqlite_master WHERE name = 'debtor_records'"))
        finally:
            store.close()

    def test_database_url_without_a_driver_falls_back_to_sqlite(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgres://user:pw@example/db"}):
            self.assertEqual(db.resolve_backend(), "sqlite")

    def test_forced_backend_wins(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"RECOVERAI_BACKEND": "sqlite"}):
            self.assertEqual(db.resolve_backend(), "sqlite")


if __name__ == "__main__":
    unittest.main()
