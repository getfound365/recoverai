"""The reopen audit invariant.

A bounced or refunded payment reopens the record and un-bills it by a linked counter-entry.
That part is unchanged and stays exactly as it was: the original ledger row is never edited,
nothing is ever deleted from the history.

What these tests pin down is what happens NEXT, which used to be nothing at all:

  * a reversal on a Paid or Partially_Paid record puts the record in exactly
    `Reopened_Awaiting_Audit` - a mandatory lifecycle state, not a cosmetic flag;
  * the queue purged when the payment landed stays purged: no resurrection, no new run,
    no restarted sequence;
  * the reversal writes its own append-only audit entry (millisecond UTC) carrying the
    linked payment event id, the collection it un-billed and the counter-entry that
    un-billed it;
  * before any new outbound entry can be generated, the record re-runs the ingestion path
    from step one - re-normalize the source row, re-resolve contact data, re-evaluate the
    calling window, re-check DNC/STOP, re-check paid status and balance - and the completed
    re-audit is recorded with a timestamp;
  * the claim itself refuses a record awaiting its audit, with the exact token
    REOPENED_AWAITING_AUDIT, before the POST - enforcement is in the claim path, not a label;
  * fail-closed everywhere: no source row, an unparseable source balance, or a missing
    contact detail is never guessed at, and a stale source balance never overwrites the
    ledger-derived one;
  * a reversal that lands on a record already in a hold does not exit that hold.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from engine import config, ingest, payments
from engine.orchestrator import (BLOCKED_DNC, BLOCKED_MODE_HITL, BLOCKED_NO_BALANCE,
                                 BLOCKED_OUTSIDE_WINDOW, CLAIMED)
from tests.base import EngineTestCase

REOPENED = payments.STATE_REOPENED_AWAITING_AUDIT
BLOCKED_REOPENED = "REOPENED_AWAITING_AUDIT"
MS_UTC = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"

#: The raw vendor row every test imports through Agent 1's translator. Messy column names on
#: purpose: the re-audit re-runs THIS row through the same alias mapping.
SOURCE_ROW = {
    "Cust_ID": "D-1",
    "Cust_Name": "Debtor D-1",
    "customer_email": "debtor@example.com",
    "PhoneNumber": "(212) 555-0143",
    "AmountDue": "1000.00",
    "Inv_Date": "2026-08-01",
    "InvoiceNumber": "INV-D-1",
    "Terms": "Net 30",
}


def row_hash(row) -> str:
    """A stable fingerprint of a stored row, so 'untouched' can be asserted, not assumed."""
    return json.dumps(dict(row), sort_keys=True, default=str)


class ReopenAuditTestCase(EngineTestCase):
    """One imported, approved record with a confirmed collection of 400.00 against a
    1000.00 invoice, and that collection bounced."""

    def setUp(self) -> None:
        super().setUp()
        self.import_source(dict(SOURCE_ROW))
        self.engine.approve("D-1", who="reviewer@client")
        self.collection = self.collect(400.0)
        self.original_ledger_row = self.engine.store.query_one(
            "SELECT * FROM collections_ledger WHERE entry_id = ?",
            (self.collection["ledger_entry_id"],))
        self.original_ledger_hash = row_hash(self.original_ledger_row)
        self.reversal = self.engine.record_reversal(
            "D-1", kind="bounced", reverses_entry_id=self.collection["ledger_entry_id"],
            external_reference="QBO-RETURN-D1")

    # --- helpers -----------------------------------------------------------
    def import_source(self, raw, client_id: str = "CLI-TEST",
                      system: str = "QuickBooks Online (simulated)",
                      endpoint: str = "mock://fixture/invoices"):
        result = ingest.normalize([raw], source_system=system, endpoint=endpoint,
                                  client_id_hint=client_id)
        self.assertEqual(result.rejected, [], "the fixture source row must normalize")
        ingest.persist(self.engine.store, result)
        return result.records[0]

    def collect(self, amount: float, debtor_id: str = "D-1"):
        return self.engine.record_payment(
            debtor_id, amount, payments.CHANNEL_ACCOUNTING_WEBHOOK,
            external_reference=f"QB-PAY-{debtor_id}-{amount}")

    def ledger_rows(self, debtor_id: str = "D-1"):
        return self.engine.store.query(
            "SELECT * FROM collections_ledger WHERE debtor_id = ? ORDER BY entry_id", (debtor_id,))

    def rewrite_source(self, **overrides) -> None:
        """Change the stored raw vendor row - what a refreshed export would look like."""
        raw = dict(SOURCE_ROW)
        raw.update(overrides)
        target = overrides.get("_debtor_id") or SOURCE_ROW["Cust_ID"]
        raw.pop("_debtor_id", None)
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                "UPDATE debtor_records SET raw_record = ? WHERE debtor_id = ?",
                (json.dumps(raw, sort_keys=True), target))

    def audit_events(self, debtor_id: str = "D-1"):
        return [r for r in self.engine.reopen_audits() if r["debtor_id"] == debtor_id]

    # --- 1. the mandatory state --------------------------------------------
    def test_a_reversal_puts_the_record_in_reopened_awaiting_audit(self):
        row = self.engine.debtor("D-1")
        self.assertEqual(row["account_state"], REOPENED)
        self.assertEqual(self.reversal["account_state_after"], REOPENED)
        self.assertEqual(self.reversal["account_state_before"], payments.STATE_PARTIALLY_PAID)
        self.assertEqual(row["balance_owed"], 1000.0)

    def test_a_refund_of_a_fully_paid_record_reopens_it_the_same_way(self):
        self.import_source({**SOURCE_ROW, "Cust_ID": "D-4", "AmountDue": "300.00"})
        collection = self.collect(300.0, debtor_id="D-4")
        self.assertEqual(self.engine.debtor("D-4")["account_state"], payments.STATE_PAID)
        reversal = self.engine.record_reversal("D-4", kind="refunded",
                                               reverses_entry_id=collection["ledger_entry_id"])
        self.assertEqual(reversal["account_state_before"], payments.STATE_PAID)
        self.assertEqual(reversal["account_state_after"], REOPENED)
        self.assertEqual(self.engine.debtor("D-4")["balance_owed"], 300.0)
        self.assertEqual(self.engine.debtor("D-4")["account_state"], REOPENED)
        self.assertEqual(self.engine.send("D-4", "voice", dispatch=False)["decision"],
                         BLOCKED_REOPENED)

    def test_the_un_bill_is_unchanged_and_the_original_row_is_never_edited(self):
        rows = self.ledger_rows()
        self.assertEqual([r["entry_type"] for r in rows], ["collection", "reversal"])
        self.assertEqual(rows[0]["amount_cents"], 40000)
        self.assertEqual(rows[0]["fee_cents"], 8000)
        self.assertEqual(rows[1]["amount_cents"], -40000)
        self.assertEqual(rows[1]["fee_cents"], -8000)
        self.assertEqual(rows[1]["linked_entry_id"], rows[0]["entry_id"])
        self.assertEqual(self.engine.ledger("CLI-TEST")["totals"]["net_fee_cents"], 0)
        self.assertEqual(row_hash(rows[0]), self.original_ledger_hash)

    # --- 2/3. the destructive-to-the-schedule part -------------------------
    def test_the_purged_queue_is_never_resurrected_and_no_run_is_created(self):
        self.import_source({**SOURCE_ROW, "Cust_ID": "D-2", "AmountDue": "800.00"})
        run_a = self.engine.schedule("D-2", "voice", reason="follow-up A")
        run_b = self.engine.schedule("D-2", "email", reason="follow-up B")
        collection = self.collect(300.0, debtor_id="D-2")
        self.assertEqual(self.engine.queue.all(), [], "the payment purged the schedule")
        with self.engine.store.tx(immediate=True):
            self.engine.store.insert(
                """INSERT INTO outbound_contact_logs
                     (debtor_id, client_id, attempted_at, contact_type, decision, status)
                   VALUES ('D-2','CLI-TEST','2026-10-06T13:00:00Z','voice','CLAIMED','SENT')""")
        logs_before = [row_hash(r) for r in self.contacts_for("D-2")]

        self.engine.record_reversal("D-2", kind="refunded",
                                    reverses_entry_id=collection["ledger_entry_id"])
        self.assertEqual(self.engine.debtor("D-2")["account_state"], REOPENED)
        # nothing came back: not the runs, not a new one
        self.assertEqual(self.engine.queue.all(), [])
        self.assertIsNone(self.engine.queue.get(run_a["run_id"]))
        self.assertIsNone(self.engine.queue.get(run_b["run_id"]))
        self.assertEqual(self.engine.run_due(), [])
        # the audit re-runs ingestion and still schedules nothing
        self.engine.run_reopen_audit("D-2", performed_by="ops@client", reason="bounce confirmed")
        self.assertEqual(self.engine.queue.all(), [])
        self.assertEqual(self.engine.run_due(), [])
        # the history keeps what was cancelled and when, byte for byte
        event = [e for e in self.engine.payment_events("CLI-TEST") if e["debtor_id"] == "D-2"][0]
        self.assertEqual(event["cancelled_run_count"], 2)
        self.assertEqual(sorted(json.loads(event["cancelled_runs"])[i]["run"]["run_id"]
                                for i in (0, 1)), sorted([run_a["run_id"], run_b["run_id"]]))
        self.assertIsNotNone(event["queue_purged_at"])
        self.assertEqual([row_hash(r) for r in self.contacts_for("D-2")], logs_before)

    def test_the_contact_history_survives_the_reversal_and_the_reopen_audit(self):
        self.import_source({**SOURCE_ROW, "Cust_ID": "D-2", "AmountDue": "800.00"})
        self.engine.approve("D-2", who="reviewer@client")
        self.assertEqual(self.engine.send("D-2", "voice", who="reviewer@client",
                                          dispatch=False)["decision"], CLAIMED)
        before = [row_hash(r) for r in self.contacts_for("D-2")]
        collection = self.collect(300.0, debtor_id="D-2")
        self.engine.record_reversal("D-2", kind="bounced",
                                    reverses_entry_id=collection["ledger_entry_id"])
        self.engine.run_reopen_audit("D-2", performed_by="ops@client", reason="bounced payment")
        self.assertEqual([row_hash(r) for r in self.contacts_for("D-2")], before)

    # --- 3. the reversal's own audit entry ---------------------------------
    def test_the_reversal_writes_its_own_audit_entry_with_both_links(self):
        rows = self.audit_events()
        self.assertEqual(len(rows), 1)
        opened = rows[0]
        self.assertEqual(opened["event_type"], payments.REOPEN_AUDIT_OPENED)
        self.assertEqual(opened["collection_entry_id"], self.collection["ledger_entry_id"])
        self.assertEqual(opened["reversal_entry_id"], self.reversal["counter_entry_id"])
        self.assertEqual(opened["payment_event_id"], self.collection["event_id"])
        self.assertEqual(opened["un_billed_amount_cents"], -40000)
        self.assertEqual(opened["un_billed_fee_cents"], -8000)
        self.assertEqual(opened["balance_before_cents"], 60000)
        self.assertEqual(opened["balance_after_cents"], 100000)
        self.assertEqual(opened["account_state_before"], payments.STATE_PARTIALLY_PAID)
        self.assertEqual(opened["account_state_after"], REOPENED)
        self.assertRegex(opened["recorded_at"], MS_UTC)
        self.assertEqual(opened["client_id"], "CLI-TEST")

    def test_the_audit_log_is_append_only(self):
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.engine.store.execute(
                "UPDATE reopen_audit_events SET reason = 'rewritten' WHERE event_id = 1")
        self.assertIn("append-only", str(ctx.exception))
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.engine.store.execute("DELETE FROM reopen_audit_events")
        self.assertIn("append-only", str(ctx.exception))
        self.assertEqual(len(self.audit_events()), 1)

    # --- 4/5. the claim gate ------------------------------------------------
    def test_the_claim_is_refused_before_the_re_audit_with_the_exact_token(self):
        result = self.engine.send("D-1", "voice", who="orchestrator",
                                  reason="attempt while awaiting the audit")
        self.assertEqual(result["decision"], BLOCKED_REOPENED)
        self.assertFalse(result["claimed"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertNotIn("claim_token", result)
        row = self.contacts_for("D-1")[-1]
        self.assertEqual(row["decision"], BLOCKED_REOPENED)
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIsNone(row["claim_token"])
        self.assertEqual(row["account_state_at_claim"], REOPENED)
        self.assertEqual(row["balance_at_claim_cents"], 100000)
        # a guardrail refusing a send is the product working, never an infraction
        self.assertEqual(self.engine.settings("CLI-TEST")["claim_seq"], 0)
        for decision in self.engine.manual_review_decisions("CLI-TEST"):
            self.assertEqual(decision["client_caused_exception"], 0)

    def test_every_channel_is_refused_not_only_voice(self):
        for channel in ("voice", "email", "sms"):
            with self.subTest(channel=channel):
                outcome = self.engine.send("D-1", channel, dispatch=False)
                self.assertEqual(outcome["decision"], BLOCKED_REOPENED)

    def test_the_claim_follows_the_normal_rules_once_the_audit_is_complete(self):
        audit = self.engine.run_reopen_audit("D-1", performed_by="ops@client",
                                             reason="bounce confirmed by the bank")
        self.assertEqual(audit["status"], "REOPEN_AUDIT_COMPLETED")
        self.assertEqual(self.engine.debtor("D-1")["account_state"], payments.STATE_OPEN)
        # the approval that predates the reversal does not carry over: hitl needs a fresh one
        self.assertEqual(self.engine.debtor("D-1")["approval_status"], "Pending")
        self.assertEqual(self.engine.send("D-1", "voice", dispatch=False)["decision"],
                         BLOCKED_MODE_HITL)
        self.engine.approve("D-1", who="reviewer@client")
        claimed = self.engine.send("D-1", "voice", who="reviewer@client", dispatch=False)
        self.assertEqual(claimed["decision"], CLAIMED)
        self.assertTrue(claimed["claimed"])
        self.assertEqual(claimed["operating_mode_at_claim"], "hitl")

    def test_a_full_auto_client_is_still_governed_by_its_sign_off_after_the_audit(self):
        self.import_source({**SOURCE_ROW, "Cust_ID": "D-2", "AmountDue": "800.00"})
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "signer", "client signed")
        collection = self.collect(300.0, debtor_id="D-2")
        self.engine.record_reversal("D-2", kind="bounced",
                                    reverses_entry_id=collection["ledger_entry_id"])
        self.assertEqual(self.engine.send("D-2", "voice", dispatch=False)["decision"],
                         BLOCKED_REOPENED)
        self.engine.run_reopen_audit("D-2", performed_by="ops@client", reason="bounce confirmed")
        self.assertEqual(self.engine.send("D-2", "voice", dispatch=False)["decision"], CLAIMED)

    def test_approval_is_refused_while_the_record_awaits_its_audit(self):
        before = len(self.engine.manual_review_decisions("CLI-TEST"))
        with self.assertRaises(PermissionError) as ctx:
            self.engine.approve("D-1", who="reviewer@client")
        self.assertIn("reopen audit", str(ctx.exception))
        self.assertEqual(len(self.engine.manual_review_decisions("CLI-TEST")), before)
        self.assertEqual(self.engine.debtor("D-1")["approval_status"], "Approved")

    def test_the_open_audit_blocks_across_connections_too(self):
        other = self.new_engine()
        try:
            self.assertEqual(other.debtor("D-1")["account_state"], REOPENED)
            self.assertEqual(len(other.reopen_audits("D-1")), 1)
            self.assertEqual(other.send("D-1", "voice", dispatch=False)["decision"],
                             BLOCKED_REOPENED)
        finally:
            other.store.close()

    # --- 4. the re-audit itself --------------------------------------------
    def test_the_re_audit_is_recorded_and_timestamped(self):
        audit = self.engine.run_reopen_audit("D-1", performed_by="ops@client",
                                             reason="bounce confirmed by the bank")
        rows = self.audit_events()
        self.assertEqual([r["event_type"] for r in rows],
                         [payments.REOPEN_AUDIT_OPENED, payments.REOPEN_AUDIT_COMPLETED])
        done = rows[-1]
        self.assertEqual(audit["event_id"], done["event_id"])
        self.assertRegex(done["recorded_at"], MS_UTC)
        self.assertEqual(done["performed_by"], "ops@client")
        self.assertEqual(done["reason"], "bounce confirmed by the bank")
        self.assertEqual(done["reversal_entry_id"], self.reversal["counter_entry_id"])
        self.assertEqual(done["collection_entry_id"], self.collection["ledger_entry_id"])
        self.assertEqual(done["payment_event_id"], self.collection["event_id"])
        self.assertEqual(done["renormalized"], 1)
        self.assertEqual(done["source_system"], "QuickBooks Online (simulated)")
        self.assertEqual(done["source_balance_cents"], 100000)
        self.assertEqual(done["ledger_balance_cents"], 100000)
        self.assertEqual(done["source_balance_matches_engine"], 1)
        self.assertEqual(done["contact_email_resolved"], 1)
        self.assertEqual(done["contact_phone_resolved"], 1)
        self.assertEqual(done["window_decision"], "In_Window")
        self.assertEqual(done["dnc_flag"], 0)
        self.assertIsNotNone(done["derived_local_time"])
        self.assertEqual(done["paid_status"], payments.STATE_OPEN)
        self.assertEqual(done["account_state_before"], REOPENED)
        self.assertEqual(done["account_state_after"], payments.STATE_OPEN)
        self.assertEqual(done["approval_status_after"], "Pending")
        self.assertEqual(audit["recorded_at"], done["recorded_at"])
        # the completed audit closes the open one: exactly one open audit, then none
        self.assertIsNone(self.engine.open_reopen_audit("D-1"))
        actions = [e["action"] for e in self.engine.audit.entries]
        self.assertIn("reopen_audit_completed", actions)

    def test_the_re_audit_re_runs_ingestion_and_resolves_contact_data_again(self):
        self.rewrite_source(customer_email="new.contact@example.com",
                            PhoneNumber="(808) 555-0111")
        audit = self.engine.run_reopen_audit("D-1", performed_by="ops@client",
                                             reason="source row refreshed")
        row = self.engine.debtor("D-1")
        self.assertEqual(row["email"], "new.contact@example.com")
        self.assertEqual(row["phone"], "(808) 555-0111")
        self.assertEqual(row["area_code"], "808")
        self.assertEqual(row["tz_name"], "Pacific/Honolulu")
        self.assertEqual(row["window_decision"], "Outside_Calling_Window")
        self.assertEqual(audit["window"]["window_decision"], "Outside_Calling_Window")
        self.assertEqual(audit["window"]["area_code"], "808")
        # and the re-evaluated window is the one the claim applies
        self.engine.approve("D-1", who="reviewer@client")
        self.assertEqual(self.engine.send("D-1", "voice", dispatch=False)["decision"],
                         BLOCKED_OUTSIDE_WINDOW)

    def test_missing_contact_data_is_recorded_and_never_invented(self):
        self.rewrite_source(customer_email="", PhoneNumber="")
        audit = self.engine.run_reopen_audit("D-1", performed_by="ops@client",
                                             reason="source row has no contact details")
        row = self.engine.debtor("D-1")
        self.assertIsNone(row["email"])
        self.assertIsNone(row["phone"])
        self.assertFalse(audit["contact"]["email_resolved"])
        self.assertFalse(audit["contact"]["phone_resolved"])
        self.assertEqual(audit["contact"]["email"], None)
        self.assertEqual(audit["contact"]["phone"], None)
        done = self.audit_events()[-1]
        self.assertEqual(done["contact_email_resolved"], 0)
        self.assertEqual(done["contact_phone_resolved"], 0)
        # no phone -> the window cannot be derived: the voice channel fails closed
        self.engine.approve("D-1", who="reviewer@client")
        self.assertEqual(self.engine.send("D-1", "voice", dispatch=False)["decision"],
                         BLOCKED_OUTSIDE_WINDOW)

    def test_the_re_audit_never_touches_the_ledger(self):
        before = [row_hash(r) for r in self.ledger_rows()]
        self.engine.run_reopen_audit("D-1", performed_by="ops@client", reason="bounce confirmed")
        self.assertEqual([row_hash(r) for r in self.ledger_rows()], before)

    def test_the_ledger_totals_are_unchanged_by_the_audit(self):
        before = self.engine.ledger("CLI-TEST")["totals"]
        self.engine.run_reopen_audit("D-1", performed_by="ops@client", reason="bounce confirmed")
        self.assertEqual(self.engine.ledger("CLI-TEST")["totals"], before)

    # --- fail-closed on the re-audit ---------------------------------------
    def test_a_re_audit_without_a_source_row_is_refused_and_nothing_moves(self):
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                "UPDATE debtor_records SET raw_record = NULL WHERE debtor_id = 'D-1'")
        before = row_hash(self.engine.debtor("D-1"))
        with self.assertRaises(payments.ReopenAuditRefused) as ctx:
            self.engine.run_reopen_audit("D-1", performed_by="ops@client", reason="no source row")
        self.assertIn("source", str(ctx.exception))
        self.assertEqual(row_hash(self.engine.debtor("D-1")), before)
        self.assertEqual(self.engine.debtor("D-1")["account_state"], REOPENED)
        self.assertIsNotNone(self.engine.open_reopen_audit("D-1"))
        refused = self.audit_events()[-1]
        self.assertEqual(refused["event_type"], payments.REOPEN_AUDIT_REFUSED)
        self.assertIn("source", refused["refuse_reason"])
        self.assertEqual(self.engine.send("D-1", "voice", dispatch=False)["decision"],
                         BLOCKED_REOPENED)

    def test_an_unparseable_source_balance_is_refused_rather_than_coerced(self):
        self.rewrite_source(AmountDue="n/a")
        with self.assertRaises(payments.ReopenAuditRefused) as ctx:
            self.engine.run_reopen_audit("D-1", performed_by="ops@client", reason="bad balance")
        self.assertIn("balance", str(ctx.exception))
        self.assertEqual(self.engine.debtor("D-1")["account_state"], REOPENED)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 1000.0)
        self.assertEqual(self.audit_events()[-1]["event_type"], payments.REOPEN_AUDIT_REFUSED)
        self.assertIsNotNone(self.engine.open_reopen_audit("D-1"))

    def test_a_source_row_for_another_debtor_is_refused(self):
        self.rewrite_source(Cust_ID="D-SOMEBODY-ELSE", _debtor_id="D-1")
        with self.assertRaises(payments.ReopenAuditRefused) as ctx:
            self.engine.run_reopen_audit("D-1", performed_by="ops@client", reason="stale source")
        self.assertIn("D-SOMEBODY-ELSE", str(ctx.exception))
        self.assertEqual(self.engine.debtor("D-1")["account_state"], REOPENED)

    def test_the_stale_source_balance_never_overwrites_the_ledger_balance(self):
        self.rewrite_source(AmountDue="250.00")
        audit = self.engine.run_reopen_audit("D-1", performed_by="ops@client",
                                             reason="bounce confirmed; export is stale")
        self.assertEqual(audit["source_balance_cents"], 25000)
        self.assertEqual(audit["balance_cents"], 100000)
        self.assertFalse(audit["source_balance_matches_engine"])
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 1000.0)
        self.assertEqual(self.audit_events()[-1]["source_balance_matches_engine"], 0)
        self.assertEqual(self.engine.debtor("D-1")["account_state"], payments.STATE_OPEN)

    def test_a_re_audit_with_nothing_open_is_refused(self):
        self.add_debtor("D-3")
        with self.assertRaises(payments.ReopenAuditRefused) as ctx:
            self.engine.run_reopen_audit("D-3", performed_by="ops@client", reason="nothing open")
        self.assertIn("no open reopen audit", str(ctx.exception))
        self.assertEqual(self.audit_events("D-3"), [])

    def test_a_re_audit_must_be_attributed(self):
        for who, reason in (("", "bounce"), ("ops@client", "")):
            with self.subTest(performed_by=who, reason=reason):
                with self.assertRaises(payments.PaymentRefused) as ctx:
                    self.engine.run_reopen_audit("D-1", performed_by=who, reason=reason)
                self.assertIn("attributed", str(ctx.exception))
        self.assertEqual(len(self.audit_events()), 1)
        self.assertIsNotNone(self.engine.open_reopen_audit("D-1"))

    def test_a_second_re_audit_is_refused_once_the_first_is_done(self):
        self.engine.run_reopen_audit("D-1", performed_by="ops@client", reason="bounce confirmed")
        with self.assertRaises(payments.ReopenAuditRefused):
            self.engine.run_reopen_audit("D-1", performed_by="ops@client", reason="again")
        self.assertEqual([r["event_type"] for r in self.audit_events()],
                         [payments.REOPEN_AUDIT_OPENED, payments.REOPEN_AUDIT_COMPLETED])

    # --- DNC stays absolute -------------------------------------------------
    def test_a_dnc_record_still_audits_and_dnc_still_beats_the_reopen_token(self):
        self.engine.handle_inbound({"debtor_id": "D-1", "content": "Please STOP"})
        audit = self.engine.run_reopen_audit("D-1", performed_by="ops@client",
                                             reason="bounce confirmed")
        self.assertEqual(audit["dnc_flag"], 1)
        self.assertTrue(audit["dnc_blocked"])
        self.assertEqual(self.audit_events()[-1]["dnc_flag"], 1)
        self.assertEqual(self.engine.send("D-1", "voice", dispatch=False)["decision"], BLOCKED_DNC)

    # --- the other payment paths fail closed while the audit is open --------
    def test_a_new_payment_signal_while_awaiting_the_audit_is_refused(self):
        before_events = len(self.engine.payment_events("CLI-TEST"))
        with self.assertRaises(payments.PaymentRefused) as ctx:
            self.engine.record_payment("D-1", 100.0, payments.CHANNEL_DELTA_SYNC,
                                       external_reference="QBO-PMT-LATE")
        self.assertIn("reopen audit", str(ctx.exception))
        self.assertEqual(len(self.ledger_rows()), 2)
        self.assertEqual(len(self.engine.payment_events("CLI-TEST")), before_events)
        self.assertEqual(self.engine.debtor("D-1")["account_state"], REOPENED)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 1000.0)

    def test_a_balance_edit_while_awaiting_the_audit_is_refused(self):
        with self.assertRaises(payments.PaymentRefused) as ctx:
            self.engine.record_balance_edit("D-1", 200.0, who="ops@client", reason="credit note")
        self.assertIn("reopen audit", str(ctx.exception))
        self.assertEqual(self.engine.balance_edit_events("CLI-TEST"), [])
        self.assertEqual(self.engine.debtor("D-1")["account_state"], REOPENED)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 1000.0)

    # --- precedence: a reversal never exits a hold --------------------------
    def _held_record_with_a_bounce(self):
        """A record that is already in a hold when the reversal lands."""
        self.import_source({**SOURCE_ROW, "Cust_ID": "D-2", "AmountDue": "800.00"})
        collection = self.collect(300.0, debtor_id="D-2")
        self.engine.record_balance_edit("D-2", 450.0, who="ops@client",
                                        reason="a credit note in Xero, no payment signal")
        self.assertEqual(self.engine.debtor("D-2")["account_state"],
                         payments.STATE_PENDING_ADJUSTMENT)
        reversal = self.engine.record_reversal("D-2", kind="bounced",
                                              reverses_entry_id=collection["ledger_entry_id"])
        return collection, reversal

    def test_a_reversal_on_a_held_record_does_not_exit_the_hold(self):
        collection, reversal = self._held_record_with_a_bounce()
        # the un-bill still happens, by counter-entry
        self.assertEqual(reversal["status"], "REVERSED")
        self.assertEqual(reversal["net_statement_fee_cents"], 0)
        self.assertEqual(len(self.ledger_rows("D-2")), 2)
        # ...and the hold is not silently cleared
        self.assertEqual(reversal["account_state_before"], payments.STATE_PENDING_ADJUSTMENT)
        self.assertEqual(reversal["account_state_after"], payments.STATE_PENDING_ADJUSTMENT)
        self.assertEqual(reversal["pre_existing_hold_state"], payments.STATE_PENDING_ADJUSTMENT)
        self.assertEqual(self.engine.debtor("D-2")["account_state"],
                         payments.STATE_PENDING_ADJUSTMENT)
        # the audit entry records both facts
        opened = self.audit_events("D-2")[0]
        self.assertEqual(opened["account_state_before"], payments.STATE_PENDING_ADJUSTMENT)
        self.assertEqual(opened["account_state_after"], payments.STATE_PENDING_ADJUSTMENT)
        self.assertIn("hold", opened["note"])
        self.assertIsNotNone(self.engine.open_reopen_audit("D-2"))

    def test_the_dispute_hold_state_is_in_the_same_never_auto_exited_set(self):
        """Dispute_Hold is not storable yet (no dispute interceptor), but the rule that a
        reversal never exits a hold already names it, so landing the interceptor cannot
        reintroduce the bug."""
        self.assertEqual(payments.HOLD_STATES,
                         (payments.STATE_PENDING_ADJUSTMENT, payments.STATE_DISPUTE_HOLD))
        self.assertTrue(payments.is_hold_state(payments.STATE_DISPUTE_HOLD))
        self.assertFalse(payments.is_hold_state(REOPENED))
        self.assertFalse(payments.is_hold_state(payments.STATE_OPEN))

    def test_a_hold_is_kept_by_the_re_audit_and_the_park_still_blocks(self):
        self._held_record_with_a_bounce()
        audit = self.engine.run_reopen_audit("D-2", performed_by="ops@client",
                                             reason="bounce confirmed")
        self.assertEqual(audit["account_state_after"], payments.STATE_PENDING_ADJUSTMENT)
        self.assertFalse(audit["state_derived_from_ledger"])
        self.assertTrue(audit["hold_state_kept"])
        self.assertEqual(self.engine.debtor("D-2")["account_state"],
                         payments.STATE_PENDING_ADJUSTMENT)
        self.assertEqual(self.engine.send("D-2", "voice", dispatch=False)["decision"],
                         "BLOCKED_PENDING_ADJUSTMENT")
        self.assertIsNone(self.engine.open_reopen_audit("D-2"))

    def test_the_open_audit_keeps_blocking_after_the_hold_is_resolved(self):
        self._held_record_with_a_bounce()
        self.engine.resolve_balance_edit("D-2", "adjustment", who="cfo@client",
                                        reason="a credit note, not a payment")
        self.assertEqual(self.engine.debtor("D-2")["account_state"], payments.STATE_OPEN)
        # resolving the park does not silently satisfy the reopen audit
        self.assertEqual(self.engine.send("D-2", "voice", dispatch=False)["decision"],
                         BLOCKED_REOPENED)
        self.engine.run_reopen_audit("D-2", performed_by="ops@client", reason="bounce confirmed")
        self.engine.approve("D-2", who="reviewer@client")
        self.assertEqual(self.engine.send("D-2", "voice", dispatch=False)["decision"], CLAIMED)

    # --- the balance rule is not relaxed by any of this ---------------------
    def _force_balance(self, amount: float, debtor_id: str = "D-1") -> None:
        """Hand-write a balance, the way a hostile or buggy writer would.

        Nothing in the API can reach this state: a payment that settles to zero is refused
        (BLOCKED_NO_BALANCE_DUE), an ingestion of a zero/credit balance is rejected, and a
        balance edit is refused while the audit is open. That is exactly why it is worth
        asserting on: fail-closed has to hold for the state a defect could produce, not only
        for the states the happy path produces.
        """
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                "UPDATE debtor_records SET balance_owed = ? WHERE debtor_id = ?",
                (amount, debtor_id))

    def test_a_zero_balance_still_refuses_and_the_audit_does_not_relax_the_balance_rule(self):
        before = [row_hash(r) for r in self.ledger_rows()]
        self._force_balance(0)
        # 1. the state gate answers first, and it is still a refusal
        self.assertEqual(self.engine.debtor("D-1")["account_state"], REOPENED)
        self.assertEqual(self.engine.send("D-1", "voice", dispatch=False)["decision"],
                         BLOCKED_REOPENED)
        # 2. the audit runs (it re-reads the source, it does not invent a balance)...
        audit = self.engine.run_reopen_audit("D-1", performed_by="ops@client",
                                            reason="bounce confirmed by the bank")
        self.assertEqual(audit["source_balance_cents"], 100000)
        self.assertEqual(audit["balance_cents"], 0)
        self.assertFalse(audit["source_balance_matches_engine"])
        self.assertEqual(self.audit_events()[-1]["ledger_balance_cents"], 0)
        # ...and derives the state from the LEDGER: nothing owed is Paid, never dunnable
        self.assertEqual(audit["state_derived_from_ledger"], True)
        self.assertEqual(audit["account_state_after"], payments.STATE_PAID)
        self.assertEqual(self.engine.debtor("D-1")["account_state"], payments.STATE_PAID)
        # 3. so the balance rule applies exactly as it does without any reversal
        self.engine.approve("D-1", who="reviewer@client")   # approval cannot change the money
        outcome = self.engine.send("D-1", "voice", dispatch=False)
        self.assertEqual(outcome["decision"], BLOCKED_NO_BALANCE)
        self.assertFalse(outcome["claimed"])
        # 4. and none of it wrote a fee line: the ledger is exactly where the reversal left it
        self.assertEqual([row_hash(r) for r in self.ledger_rows()], before)
        self.assertEqual(self.engine.settings("CLI-TEST")["claim_seq"], 0)
        # 5. a credit balance (an overpayment) is the same refusal, not a dunning opportunity
        self._force_balance(-25.00)
        credit = self.engine.send("D-1", "voice", dispatch=False)
        self.assertEqual(credit["decision"], BLOCKED_NO_BALANCE)
        self.assertFalse(credit["claimed"])

    def test_the_rulebook_documents_this_invariant_and_the_token_matches_the_code(self):
        """A rule the engine enforces but the rulebook does not state is a rule nobody can audit."""
        rulebook = config.load_json(config.config_dir() / "rulebook.json")
        reopen = rulebook["reopen_audit"]
        self.assertEqual(reopen["refusal_token"], BLOCKED_REOPENED)
        self.assertEqual(reopen["state"], REOPENED)
        self.assertTrue(reopen["mandatory"])
        order = rulebook["guardrails"]["evaluation_order_at_claim"]
        self.assertIn("reopened_awaiting_audit", order)
        self.assertLess(order.index("reopened_awaiting_audit"), order.index("balance_due"),
                        "the reopen gate has to be documented before the balance gate")
        self.assertLess(order.index("dnc_flag"), order.index("reopened_awaiting_audit"),
                        "an opt-out beats an open reopen audit, in the code and in the rulebook")
        precedence = reopen["precedence_with_holds"]
        self.assertEqual(tuple(precedence["hold_states"]), payments.HOLD_STATES)
        self.assertIn("never exits that hold", precedence["rule"])
        self.assertIn(payments.STATE_DISPUTE_HOLD, precedence["hold_states"])

    # --- the reader ---------------------------------------------------------
    def test_pending_reopen_audits_lists_exactly_the_open_ones(self):
        self.assertEqual([r["debtor_id"] for r in self.engine.pending_reopen_audits()], ["D-1"])
        self.engine.run_reopen_audit("D-1", performed_by="ops@client", reason="bounce confirmed")
        self.assertEqual(self.engine.pending_reopen_audits(), [])
        # the history is still there in full
        self.assertEqual(len(self.audit_events()), 2)


if __name__ == "__main__":
    unittest.main()
