"""The payment path: one payment event, three consequences, one transaction.

What is under test here:

  * the intake is atomic - a failure in any leg leaves the balance, the payment state and
    the runs queue exactly as they were (nothing commits, including the queue purge);
  * the fee is 20% of funds actually confirmed collected, computed in integer cents,
    half up to the cent, and never on any other basis;
  * the ledger is append-only, enforced by triggers, and a fee line without a
    confirmation channel cannot exist;
  * a Partially_Paid debtor continues only for the remainder, at the reduced balance;
  * zero and credit balances are blocked from contact with BLOCKED_NO_BALANCE_DUE;
  * the last-millisecond re-check refuses a claim for a payment that landed after the
    human approved the draft - the claim is refused, never fired-then-logged;
  * a balance edit with no payment signal behind it parks the record (no dunning, no fee,
    no send) and resolves as a payment (billable) or an adjustment (never billable);
  * a reversal un-bills through a linked counter-entry (the original ledger row stays
    byte-identical) and is NOT a resume: the record goes to `Reopened_Awaiting_Audit`, and
    nothing can be claimed from it until it has re-run the ingestion path from step one and
    that completed re-audit is recorded (the full invariant is in tests/test_reopen_audit.py).
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest import mock

from engine import clock, config, payments
from engine.orchestrator import (BLOCKED_MODE_HITL, BLOCKED_NO_BALANCE,
                                 BLOCKED_PENDING_ADJUSTMENT, BLOCKED_REOPENED_AWAITING_AUDIT,
                                 CLAIMED)
from engine.runqueue import PURGEABLE_STATES
from tests.base import EngineTestCase


class PaymentTestCase(EngineTestCase):
    """Shared fixtures: a debtor with a real balance and a scheduled follow-up."""

    def setUp(self) -> None:
        super().setUp()
        self.add_debtor("D-1", balance=1000.0, aging_state="60", approval_status="Approved")
        self.add_debtor("D-2", balance=4820.50, aging_state="60", approval_status="Approved")

    # --- helpers -----------------------------------------------------------
    def ledger_rows(self, debtor_id: str = "D-1"):
        return self.engine.store.query(
            "SELECT * FROM collections_ledger WHERE debtor_id = ? ORDER BY entry_id", (debtor_id,))

    def event_rows(self, debtor_id: str = "D-1"):
        return self.engine.store.query(
            "SELECT * FROM payment_events WHERE debtor_id = ? ORDER BY event_id", (debtor_id,))

    def queue_fingerprint(self):
        return [(r.run_id, r.debtor_id, r.state, r.due_at) for r in self.engine.queue.all()]

    def payload_files(self):
        return sorted(p.name for p in self.paths["payloads"].glob("*.json"))


# ---------------------------------------------------------------------------
class TestAtomicIntake(PaymentTestCase):
    def test_a_failure_in_the_ledger_leg_rolls_back_all_three_consequences(self):
        """The rollback test: inject a failure in part (c) and assert nothing moved."""
        run = self.engine.schedule("D-1", "voice", reason="follow-up before the payment lands")
        before_queue = self.queue_fingerprint()
        before_debtor = self.engine.debtor("D-1")
        before_logs = self.engine.contacts()

        with mock.patch("engine.payments._insert_ledger_row",
                        side_effect=RuntimeError("injected failure in leg (c)")):
            with self.assertRaises(RuntimeError) as ctx:
                self.engine.record_payment("D-1", 400.0, payments.CHANNEL_ACCOUNTING_WEBHOOK,
                                           external_reference="QB-PAY-ROLLBACK")
        self.assertIn("injected failure in leg (c)", str(ctx.exception))

        # (a) the balance and the state did not move
        after_debtor = self.engine.debtor("D-1")
        self.assertEqual(after_debtor["balance_owed"], before_debtor["balance_owed"])
        self.assertEqual(after_debtor["account_state"], before_debtor["account_state"])
        self.assertEqual(after_debtor["account_state"], payments.STATE_OPEN)
        self.assertIsNone(after_debtor["balance_updated_at"])
        # (b) the queue purge was rolled back with the transaction
        self.assertEqual(self.queue_fingerprint(), before_queue)
        self.assertEqual(self.engine.queue.summary(), {"scheduled": 1})
        self.assertEqual(self.engine.queue.get(run["run_id"]).state, "scheduled")
        # (c) no ledger line, no intake event, no new contact log
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.event_rows(), [])
        self.assertEqual(self.engine.contacts(), before_logs)

    def test_all_three_legs_land_together_on_success(self):
        scheduled = self.engine.schedule("D-1", "voice", reason="follow-up")
        with self.engine.store.tx(immediate=True):
            self.engine.store.insert(
                """INSERT INTO outbound_contact_logs
                     (debtor_id, client_id, attempted_at, contact_type, decision, status)
                   VALUES ('D-1','CLI-TEST',?,'voice','CLAIMED','SENT')""", (clock.iso(),))
        result = self.engine.record_payment("D-1", 400.0, payments.CHANNEL_ACCOUNTING_WEBHOOK,
                                            external_reference="QB-PAY-1")
        self.assertEqual(result["status"], "COMMITTED")
        # (a) balance + state
        self.assertEqual(result["balance_before_cents"], 100000)
        self.assertEqual(result["balance_after_cents"], 60000)
        self.assertEqual(result["account_state_after"], payments.STATE_PARTIALLY_PAID)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 600.0)
        # (b) the scheduled run is GONE from the queue, the contact history is not
        self.assertEqual(self.engine.queue.get(scheduled["run_id"]), None)
        self.assertEqual(self.engine.queue.summary(), {})
        self.assertEqual([r["run"]["run_id"] for r in result["cancelled_runs"]],
                         [scheduled["run_id"]])
        self.assertEqual(result["outbound_logs_untouched"], 1)
        self.assertEqual(len(self.engine.contacts()), 1)
        self.assertIsNotNone(result["queue_purged_at"])
        # the cancellation record is written to the intake row, not only to a log file
        event = self.event_rows()[0]
        self.assertEqual(event["cancelled_run_count"], 1)
        self.assertEqual(json.loads(event["cancelled_runs"])[0]["run"]["run_id"],
                         scheduled["run_id"])
        self.assertEqual(event["queue_purged_at"], result["queue_purged_at"])
        # (c) exactly one ledger line with the channel that confirmed it
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entry_type"], "collection")
        self.assertEqual(rows[0]["amount_cents"], 40000)
        self.assertEqual(rows[0]["confirmation_channel"], payments.CHANNEL_ACCOUNTING_WEBHOOK)
        self.assertEqual(rows[0]["payment_event_id"], event["event_id"])
        self.assertEqual(rows[0]["intake_reference"], event["intake_reference"])

    def test_each_channel_stamps_its_own_confirmation_channel(self):
        cases = [
            ("D-1", payments.CHANNEL_ACCOUNTING_WEBHOOK, None),
            ("D-2", payments.CHANNEL_DELTA_SYNC, None),
            ("D-1", payments.CHANNEL_MANUAL_ATTESTATION, "dana.whitmore@client (CFO)"),
        ]
        for debtor_id, channel, attested_by in cases:
            with self.subTest(channel=channel):
                amount = 100.0 if debtor_id == "D-1" else 20.0
                result = self.engine.record_payment(debtor_id, amount, channel, attested_by=attested_by)
                self.assertEqual(result["confirmation_channel"], channel)
                self.assertEqual(result["attested_by"], attested_by)
                row = self.ledger_rows(debtor_id)[-1]
                self.assertEqual(row["confirmation_channel"], channel)
                self.assertEqual(row["attested_by"], attested_by)
                self.assertEqual(row["confirmation_latency"],
                                 payments.confirmation_latency(channel))
        # the three latencies are different statements, not one smoothed-over number
        latencies = {payments.confirmation_latency(c) for c in payments.CONFIRMATION_CHANNELS}
        self.assertEqual(len(latencies), 3)

    def test_a_manual_attestation_must_name_who_attested(self):
        with self.assertRaises(payments.PaymentRefused) as ctx:
            self.engine.record_payment("D-1", 100.0, payments.CHANNEL_MANUAL_ATTESTATION)
        self.assertIn("must name who attested", str(ctx.exception))
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 1000.0)

    def test_an_unknown_confirmation_channel_is_refused(self):
        with self.assertRaises(payments.PaymentRefused) as ctx:
            self.engine.record_payment("D-1", 100.0, "a_vibe")
        self.assertIn("confirmation channel", str(ctx.exception))
        self.assertEqual(self.ledger_rows(), [])

    def test_a_payment_signal_on_a_paid_record_is_refused_not_billed(self):
        self.engine.record_payment("D-1", 1000.0, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        with self.assertRaises(payments.PaymentRefused) as ctx:
            self.engine.record_payment("D-1", 50.0, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        self.assertIn("owes nothing", str(ctx.exception))
        self.assertEqual(len(self.ledger_rows()), 1)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 0.0)

    def test_a_duplicate_webhook_is_ignored_and_never_billed_twice(self):
        first = self.engine.record_payment("D-1", 200.0, payments.CHANNEL_ACCOUNTING_WEBHOOK,
                                          external_reference="QB-PAY-9")
        second = self.engine.record_payment("D-1", 200.0, payments.CHANNEL_ACCOUNTING_WEBHOOK,
                                            external_reference="QB-PAY-9")
        self.assertEqual(first["status"], "COMMITTED")
        self.assertEqual(second["status"], "DUPLICATE_IGNORED")
        self.assertEqual(len(self.ledger_rows()), 1)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 800.0)

    def test_a_negative_or_zero_amount_is_refused(self):
        for amount in (0, -50.0):
            with self.subTest(amount=amount):
                with self.assertRaises(payments.PaymentRefused):
                    self.engine.record_payment("D-1", amount, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        self.assertEqual(self.ledger_rows(), [])


# ---------------------------------------------------------------------------
class TestFeeArithmetic(PaymentTestCase):
    def test_the_rounding_rule_is_half_up_to_the_cent_and_it_is_stated(self):
        """One rule, in integer cents, applied the same way everywhere."""
        self.assertEqual(payments.FEE_ROUNDING_RULE, "half_up_to_cent")
        self.assertEqual(config.contingency_fee()["rounding_rule"], payments.FEE_ROUNDING_RULE)
        self.assertEqual(payments.fee_basis_points(), 2000)   # 20%
        cases = {
            100000: 20000,     # 1000.00  -> 200.00 exactly
            182053: 36411,     # 1820.53  -> 364.106  -> half up -> 364.11
            299997: 59999,     # 2999.97  -> 599.994  -> half up -> 599.99
            3: 1,              # 0.03     -> 0.006    -> half up -> 0.01
            2: 0,              # 0.02     -> 0.004    -> down     -> 0.00
            5: 1,              # 0.05     -> 0.01 exactly
            1: 0,              # 0.01     -> 0.002    -> down     -> 0.00
        }
        for amount_cents, expected_fee in cases.items():
            with self.subTest(amount_cents=amount_cents):
                self.assertEqual(payments.fee_for(amount_cents), expected_fee)

    def test_the_fee_on_a_ledger_line_is_exactly_twenty_percent_half_up(self):
        result = self.engine.record_payment("D-2", 1820.53, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        self.assertEqual(result["amount_cents"], 182053)
        self.assertEqual(result["fee_cents"], 36411)
        self.assertEqual(result["fee_basis_points"], 2000)
        self.assertEqual(result["fee_rounding_rule"], "half_up_to_cent")
        self.assertEqual(result["fee_amount"], 364.11)
        row = self.ledger_rows("D-2")[0]
        self.assertEqual(row["fee_cents"], payments.fee_for(row["amount_cents"]))
        self.assertEqual(row["fee_basis_points"], 2000)
        self.assertEqual(row["fee_rounding_rule"], "half_up_to_cent")
        # the balance move is exact in cents too: 4820.50 - 1820.53
        self.assertEqual(row["balance_before_cents"], 482050)
        self.assertEqual(row["balance_after_cents"], 299997)

    def test_the_store_refuses_a_fee_that_does_not_match_the_stated_rate_and_rule(self):
        """Belt and braces: the CHECK constraint, not just our arithmetic."""
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    """INSERT INTO collections_ledger
                         (client_id, debtor_id, entry_type, amount_cents, fee_cents,
                          fee_basis_points, fee_rounding_rule, confirmation_channel,
                          confirmation_latency, recorded_at, debtor_state_after)
                       VALUES ('CLI-TEST','D-1','collection',10000,9999,2000,'half_up_to_cent',
                               'accounting_webhook','near-instant',?,'Partially_Paid')""",
                    (clock.iso(),))
        self.assertIn("CHECK", str(ctx.exception))

    def test_a_fee_line_cannot_exist_without_a_confirmation_channel(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    """INSERT INTO collections_ledger
                         (client_id, debtor_id, entry_type, amount_cents, fee_cents,
                          fee_basis_points, fee_rounding_rule, confirmation_channel,
                          confirmation_latency, recorded_at, debtor_state_after)
                       VALUES ('CLI-TEST','D-1','collection',10000,2000,2000,'half_up_to_cent',
                               'trust_me_bro','near-instant',?,'Partially_Paid')""",
                    (clock.iso(),))
        self.assertEqual(self.ledger_rows(), [])

    def test_every_line_cites_its_confirmation_source_and_latency(self):
        self.engine.record_payment("D-1", 100.0, payments.CHANNEL_DELTA_SYNC)
        lines = self.engine.ledger("CLI-TEST")["lines"]
        self.assertTrue(lines)
        for line in lines:
            self.assertIn(line["confirmation_channel"], payments.CONFIRMATION_CHANNELS)
            self.assertTrue(line["confirmation_latency"])
            self.assertTrue(line["recorded_at"])


# ---------------------------------------------------------------------------
class TestPartialPayment(PaymentTestCase):
    def test_a_partial_payment_sets_partially_paid_and_bills_only_the_paid_portion(self):
        result = self.engine.record_payment("D-1", 250.0, payments.CHANNEL_DELTA_SYNC)
        self.assertEqual(result["account_state_after"], payments.STATE_PARTIALLY_PAID)
        self.assertEqual(self.engine.debtor("D-1")["account_state"], payments.STATE_PARTIALLY_PAID)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 750.0)
        self.assertEqual(result["fee_cents"], payments.fee_for(25000))
        self.assertEqual(result["fee_cents"], 5000)
        # the fee is on the paid portion only, never on the invoice or the remainder
        self.assertNotEqual(result["fee_cents"], payments.fee_for(100000))
        self.assertNotEqual(result["fee_cents"], payments.fee_for(75000))
        totals = self.engine.ledger("CLI-TEST")["totals"]
        self.assertEqual(totals["net_collected_cents"], 25000)
        self.assertEqual(totals["net_fee_cents"], 5000)

    def test_the_sequence_continues_for_the_remainder_at_the_reduced_balance(self):
        """The surviving work references the reduced balance, not the original invoice."""
        self.engine.record_payment("D-1", 250.0, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        run = self.engine.schedule("D-1", "voice", due_at=clock.iso(clock.plus(minutes=-1)),
                                   reason="remainder follow-up")
        outcome = self.engine.run_due(who="scheduler")
        self.assertEqual(len(outcome), 1)
        self.assertEqual(outcome[0]["outcome"]["decision"], CLAIMED)
        claim = self.claim_rows("D-1")[-1]
        self.assertEqual(claim["balance_at_claim_cents"], 75000)
        self.assertEqual(claim["account_state_at_claim"], payments.STATE_PARTIALLY_PAID)
        payload = json.loads(self.paths["payloads"].joinpath(
            f"{claim['contact_log_id']}__D-1__voice.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["metadata"]["balance_owed"], 750.0)
        self.assertEqual(payload["guardrails"]["balance_at_claim_cents"], 75000)
        # and the run that fired is no longer pending
        self.assertEqual(self.engine.queue.get(run["run_id"]).state, "dispatched")

    def test_the_purge_removes_scheduled_and_halted_runs_but_keeps_history(self):
        scheduled = self.engine.schedule("D-1", "voice", reason="pending work")
        halted = self.engine.schedule("D-1", "email", reason="work a mode flip stopped")
        self.engine.queue.mark(halted["run_id"], "halted", "mode flipped to hitl")
        dispatched = self.engine.schedule("D-1", "voice", reason="already fired")
        self.engine.queue.mark(dispatched["run_id"], "dispatched")
        self.assertEqual(set(PURGEABLE_STATES), {"scheduled", "halted"})
        result = self.engine.record_payment("D-1", 100.0, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        remaining = {r.run_id: r.state for r in self.engine.queue.all()}
        self.assertNotIn(scheduled["run_id"], remaining)
        self.assertNotIn(halted["run_id"], remaining)
        self.assertEqual(remaining[dispatched["run_id"]], "dispatched")
        self.assertEqual(result["cancelled_run_count"], 2)


# ---------------------------------------------------------------------------
class TestBlockedBalances(PaymentTestCase):
    def test_a_zero_balance_after_payment_is_blocked_with_no_balance_due(self):
        self.engine.record_payment("D-1", 1000.0, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 0.0)
        for attempt in range(2):
            outcome = self.engine.send("D-1", "voice", reason=f"chase a paid debtor, attempt {attempt}")
            self.assertEqual(outcome["decision"], BLOCKED_NO_BALANCE)
            self.assertFalse(outcome["claimed"])
        self.assertEqual(self.claim_rows("D-1"), [])
        self.assertEqual(self.payload_files(), [], "a blocked claim must never write a payload")
        row = self.contacts_for("D-1")[-1]
        self.assertEqual(row["account_state_at_claim"], payments.STATE_PAID)
        self.assertEqual(row["balance_at_claim_cents"], 0)
        self.assertIsNone(row["claim_token"])

    def test_a_credit_balance_is_blocked_with_no_balance_due(self):
        self.add_debtor("C-1", balance=-320.75, approval_status="Approved")
        outcome = self.engine.send("C-1", "email", reason="credit balance")
        self.assertEqual(outcome["decision"], BLOCKED_NO_BALANCE)
        self.assertFalse(outcome["claimed"])
        self.assertEqual(self.claim_rows("C-1"), [])

    def test_an_overpayment_stops_at_zero_and_records_the_overpayment(self):
        result = self.engine.record_payment("D-1", 1250.0, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        self.assertEqual(result["account_state_after"], payments.STATE_PAID)
        self.assertEqual(result["balance_after_cents"], 0)
        self.assertEqual(result["overpayment_cents"], 25000)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 0.0)
        # the fee is on what was actually collected, and the overpayment is recorded, not hidden
        self.assertEqual(result["fee_cents"], payments.fee_for(125000))
        self.assertEqual(self.event_rows()[0]["overpayment_cents"], 25000)


# ---------------------------------------------------------------------------
class TestLastMillisecondRecheck(PaymentTestCase):
    def test_a_payment_landing_between_approval_and_send_refuses_the_claim(self):
        self.engine.queue_draft("D-1", "voice")
        self.engine.approve("D-1", who="reviewer@client (human)")
        self.assertEqual(self.engine.debtor("D-1")["approval_status"], "Approved")
        # the payment lands after the human approved and before the send runs
        payment = self.engine.record_payment("D-1", 1000.0, payments.CHANNEL_ACCOUNTING_WEBHOOK,
                                             external_reference="QB-PAY-RACE")
        self.assertEqual(payment["account_state_after"], payments.STATE_PAID)
        outcome = self.engine.send("D-1", "voice", who="orchestrator", reason="send the approved draft")
        self.assertEqual(outcome["decision"], BLOCKED_NO_BALANCE)
        self.assertFalse(outcome["claimed"])
        self.assertEqual(self.claim_rows("D-1"), [], "the claim must be refused, not fired and logged")
        self.assertEqual(self.payload_files(), [])
        row = self.contacts_for("D-1")[-1]
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIsNone(row["claim_token"])
        self.assertEqual(row["account_state_at_claim"], payments.STATE_PAID)
        self.assertEqual(row["balance_at_claim_cents"], 0)

    def test_a_partial_payment_between_approval_and_send_stamps_the_reduced_balance(self):
        self.engine.queue_draft("D-1", "voice")
        self.engine.approve("D-1", who="reviewer@client (human)")
        self.engine.record_payment("D-1", 400.0, payments.CHANNEL_DELTA_SYNC)
        outcome = self.engine.send("D-1", "voice", who="orchestrator", reason="send the approved draft")
        self.assertEqual(outcome["decision"], CLAIMED)
        claim = self.claim_rows("D-1")[-1]
        self.assertEqual(claim["balance_at_claim_cents"], 60000)
        self.assertEqual(claim["account_state_at_claim"], payments.STATE_PARTIALLY_PAID)
        self.assertEqual(outcome["payload"]["guardrails"]["balance_at_claim_cents"], 60000)

    def test_the_claim_snapshot_is_written_inside_the_claim_transaction(self):
        """Every field of the snapshot comes from a re-read inside the claim transaction."""
        outcome = self.engine.send("D-1", "voice", dispatch=False)
        self.assertEqual(outcome["decision"], CLAIMED)
        claim = self.claim_rows("D-1")[-1]
        self.assertEqual(claim["account_state_at_claim"], payments.STATE_OPEN)
        self.assertEqual(claim["balance_at_claim_cents"], 100000)
        self.assertEqual(claim["dnc_flag_at_claim"], 0)
        self.assertEqual(claim["window_decision"], "In_Window")
        self.assertEqual(claim["operating_mode_at_claim"], "hitl")


# ---------------------------------------------------------------------------
class TestBalanceEditsPause(PaymentTestCase):
    def test_an_edit_with_no_payment_signal_parks_the_record(self):
        self.engine.schedule("D-1", "voice", reason="pending follow-up")
        result = self.engine.record_balance_edit("D-1", 600.0, who="ops@client",
                                                reason="applied a credit note in QuickBooks")
        self.assertEqual(result["status"], "PARKED")
        self.assertEqual(result["account_state_after"], payments.STATE_PENDING_ADJUSTMENT)
        self.assertFalse(result["billable"])
        debtor = self.engine.debtor("D-1")
        self.assertEqual(debtor["account_state"], payments.STATE_PENDING_ADJUSTMENT)
        self.assertEqual(debtor["balance_owed"], 600.0)
        # no fee, no ledger line at all
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.engine.ledger("CLI-TEST")["totals"]["net_fee_cents"], 0)
        # no dunning: the send is refused and writes no claim and no payload
        outcome = self.engine.send("D-1", "voice", reason="chase a parked record")
        self.assertEqual(outcome["decision"], BLOCKED_PENDING_ADJUSTMENT)
        self.assertFalse(outcome["claimed"])
        self.assertEqual(self.claim_rows("D-1"), [])
        self.assertEqual(self.payload_files(), [])
        # the park is attributed and logged
        event = self.engine.balance_edit_events()[0]
        self.assertEqual(event["event_type"], "edit_unconfirmed")
        self.assertEqual(event["who"], "ops@client")
        self.assertEqual(event["reason"], "applied a credit note in QuickBooks")
        self.assertEqual(event["delta_cents"], -40000)
        self.assertEqual(event["billable"], 0)

    def test_a_parked_record_is_not_sendable_even_in_full_auto(self):
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "signer", "signed")
        self.engine.record_balance_edit("D-1", 600.0, who="ops@client", reason="credit note")
        outcome = self.engine.send("D-1", "voice", reason="full_auto attempt on a parked record")
        self.assertEqual(outcome["decision"], BLOCKED_PENDING_ADJUSTMENT)
        self.assertEqual(self.engine.settings("CLI-TEST")["operating_mode"], "full_auto")
        self.assertEqual(self.claim_rows("D-1"), [])

    def test_resolving_as_a_payment_flows_through_the_payment_path_and_is_billable(self):
        self.engine.record_balance_edit("D-1", 600.0, who="ops@client", reason="looks like a payment")
        self.engine.schedule("D-1", "voice", reason="pending follow-up")
        result = self.engine.resolve_balance_edit(
            "D-1", "payment", who="dana@client (CFO)",
            reason="confirmed the client paid 400.00 into our bank account")
        self.assertEqual(result["status"], "RESOLVED_AS_PAYMENT")
        self.assertTrue(result["billable"])
        self.assertEqual(result["amount_cents"], 40000)
        self.assertEqual(result["fee_cents"], 8000)
        self.assertEqual(result["confirmation_channel"], payments.CHANNEL_MANUAL_ATTESTATION)
        self.assertEqual(result["attested_by"], "dana@client (CFO)")
        # the queue was purged by the same path a webhook would use
        self.assertEqual(result["cancelled_run_count"], 1)
        self.assertEqual(self.engine.queue.summary(), {})
        row = self.ledger_rows()[0]
        self.assertEqual(row["amount_cents"], 40000)
        self.assertEqual(row["fee_cents"], 8000)
        self.assertEqual(row["confirmation_channel"], payments.CHANNEL_MANUAL_ATTESTATION)
        self.assertEqual(row["balance_before_cents"], 100000)
        self.assertEqual(row["balance_after_cents"], 60000)
        self.assertEqual(self.engine.debtor("D-1")["account_state"], payments.STATE_PARTIALLY_PAID)
        # the resolution row links back to the edit it resolved
        resolution = self.engine.balance_edit_events()[-1]
        self.assertEqual(resolution["event_type"], "resolved_as_payment")
        self.assertEqual(resolution["billable"], 1)
        self.assertEqual(resolution["ledger_entry_id"], row["entry_id"])
        self.assertEqual(resolution["linked_event_id"], self.engine.balance_edit_events()[0]["event_id"])
        self.assertEqual(self.engine.pending_adjustments(), [])

    def test_resolving_as_an_adjustment_is_closed_and_never_billable(self):
        self.engine.record_balance_edit("D-1", 600.0, who="ops@client", reason="credit note applied")
        result = self.engine.resolve_balance_edit(
            "D-1", "adjustment", who="dana@client (CFO)",
            reason="a credit note, not a payment: nothing was collected")
        self.assertEqual(result["status"], "RESOLVED_AS_ADJUSTMENT")
        self.assertFalse(result["billable"])
        self.assertIsNone(result["ledger_entry_id"])
        self.assertEqual(result["fee_cents"], 0)
        # never billable: there is no ledger line and no fee at all
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.engine.ledger("CLI-TEST")["lines"], [])
        self.assertEqual(self.engine.ledger("CLI-TEST")["totals"]["net_fee_cents"], 0)
        # closed, and dunning reopens on the adjusted balance
        self.assertEqual(self.engine.debtor("D-1")["account_state"], payments.STATE_OPEN)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 600.0)
        self.assertEqual(self.engine.send("D-1", "voice", dispatch=False)["decision"], CLAIMED)
        resolution = self.engine.balance_edit_events()[-1]
        self.assertEqual(resolution["event_type"], "resolved_as_adjustment")
        self.assertEqual(resolution["billable"], 0)
        self.assertEqual(resolution["who"], "dana@client (CFO)")
        self.assertEqual(self.engine.pending_adjustments(), [])

    def test_the_standing_default_is_that_an_unconfirmed_adjustment_is_never_billable(self):
        self.assertFalse(payments.UNCONFIRMED_ADJUSTMENT_BILLABLE,
                         "the team's standing default: an unconfirmed adjustment is never billable")
        self.engine.record_balance_edit("D-1", 500.0, who="ops@client", reason="unexplained drop")
        result = self.engine.resolve_balance_edit("D-1", "adjustment", who="cfo@client",
                                                 reason="no money arrived")
        self.assertFalse(result["billable"])
        self.assertEqual(self.engine.ledger("CLI-TEST")["totals"]["net_fee_cents"], 0)

    def test_an_edit_and_a_resolution_must_both_be_attributed(self):
        with self.assertRaises(payments.PaymentRefused):
            self.engine.record_balance_edit("D-1", 600.0, who="", reason="no attribution")
        with self.assertRaises(payments.PaymentRefused):
            self.engine.record_balance_edit("D-1", 600.0, who="ops@client", reason="")
        self.assertEqual(self.engine.balance_edit_events(), [])
        self.engine.record_balance_edit("D-1", 600.0, who="ops@client", reason="credit note")
        with self.assertRaises(payments.PaymentRefused):
            self.engine.resolve_balance_edit("D-1", "adjustment", who="", reason="because")
        # still parked, still nothing billable
        self.assertEqual(self.engine.debtor("D-1")["account_state"],
                         payments.STATE_PENDING_ADJUSTMENT)
        self.assertEqual(self.ledger_rows(), [])

    def test_resolution_refuses_a_resolution_that_does_not_reconcile(self):
        self.engine.record_balance_edit("D-1", 600.0, who="ops@client", reason="drop of 400")
        with self.assertRaises(payments.PaymentRefused) as ctx:
            self.engine.resolve_balance_edit("D-1", "payment", who="cfo@client",
                                            reason="says 250 arrived", amount=250.0)
        self.assertIn("does not reconcile", str(ctx.exception))
        self.assertEqual(self.ledger_rows(), [])
        self.assertEqual(self.engine.debtor("D-1")["account_state"],
                         payments.STATE_PENDING_ADJUSTMENT)

    def test_a_resolution_with_nothing_parked_is_refused(self):
        with self.assertRaises(payments.PaymentRefused) as ctx:
            self.engine.resolve_balance_edit("D-1", "adjustment", who="cfo@client", reason="nothing")
        self.assertIn("no unconfirmed balance edit parked", str(ctx.exception))

    def test_an_edit_that_changes_nothing_parks_nothing(self):
        result = self.engine.record_balance_edit("D-1", 1000.0, who="ops@client", reason="no-op")
        self.assertEqual(result["status"], "NO_CHANGE")
        self.assertEqual(self.engine.balance_edit_events(), [])
        self.assertEqual(self.engine.debtor("D-1")["account_state"], payments.STATE_OPEN)


# ---------------------------------------------------------------------------
class TestReversal(PaymentTestCase):
    def _collect(self, amount: float = 400.0) -> dict:
        return self.engine.record_payment("D-1", amount, payments.CHANNEL_ACCOUNTING_WEBHOOK,
                                          external_reference=f"QB-PAY-{amount}")

    def test_the_refusal_token_is_exactly_reopened_awaiting_audit(self):
        """The token is part of the contract the console and the reports read."""
        self.assertEqual(BLOCKED_REOPENED_AWAITING_AUDIT, "REOPENED_AWAITING_AUDIT")
        self.assertEqual(payments.STATE_REOPENED_AWAITING_AUDIT, "Reopened_Awaiting_Audit")

    def test_a_reversal_reopens_into_a_mandatory_audit_and_un_bills_through_a_counter_entry(self):
        """A reversal is an un-bill and a mandatory audit - it is not a resume.

        (This replaces the earlier assertion that a reversal simply reopened the sequence.
        The un-bill is unchanged; what changed is that the record cannot be claimed from
        until it has re-run the ingestion path from step one - see
        tests/test_reopen_audit.py for the whole invariant.)

        Four consequences of one reversal, checked here because the payment path owns them:
          * the record goes to exactly `Reopened_Awaiting_Audit` - a mandatory state, not a flag;
          * the un-bill is a linked counter-entry and the original row is byte-identical;
          * the reversal writes its own audit row: millisecond-UTC, the payment event it came
            from, the collection, the counter-entry, and what was un-billed;
          * the purged queue stays purged, and any claim before the completed re-audit is
            refused with the exact reason token REOPENED_AWAITING_AUDIT.
        """
        runs = [self.engine.schedule("D-1", "voice", reason="follow-up A"),
                self.engine.schedule("D-1", "email", reason="follow-up B")]
        collection = self._collect(400.0)
        self.assertEqual(self.engine.queue.all(), [], "the payment purged the schedule")
        original = self.ledger_rows()[0]
        before = json.dumps(original, sort_keys=True, default=str)

        reversal = self.engine.record_reversal("D-1", kind="bounced",
                                              reverses_entry_id=collection["ledger_entry_id"])
        # --- 1. the mandatory state, exactly, and the balance it restored
        self.assertEqual(reversal["status"], "REVERSED")
        self.assertEqual(reversal["account_state_before"], payments.STATE_PARTIALLY_PAID)
        self.assertEqual(reversal["account_state_after"], payments.STATE_REOPENED_AWAITING_AUDIT)
        self.assertEqual(self.engine.debtor("D-1")["account_state"],
                         payments.STATE_REOPENED_AWAITING_AUDIT)
        self.assertEqual(self.engine.debtor("D-1")["balance_owed"], 1000.0)
        self.assertFalse(payments.is_hold_state(payments.STATE_REOPENED_AWAITING_AUDIT))
        self.assertEqual(reversal["claim_blocked_until"], "reopen_audit_completed")
        # --- 2. the un-bill: a linked counter-entry, and the original row is never edited
        self.assertEqual(reversal["amount_cents"], -40000)
        self.assertEqual(reversal["fee_cents"], -8000)
        self.assertEqual(reversal["net_statement_amount_cents"], 0)
        self.assertEqual(reversal["net_statement_fee_cents"], 0)
        rows = self.ledger_rows()
        self.assertEqual([r["entry_type"] for r in rows], ["collection", "reversal"])
        self.assertEqual(rows[1]["linked_entry_id"], rows[0]["entry_id"])
        lines = {l["entry_id"]: l for l in self.engine.ledger("CLI-TEST")["lines"]}
        self.assertEqual(lines[rows[0]["entry_id"]]["reversed_by_entry_id"], rows[1]["entry_id"])
        self.assertEqual(lines[rows[1]["entry_id"]]["reverses_entry_id"], rows[0]["entry_id"])
        latest = self.engine.store.query_one(
            "SELECT * FROM collections_ledger WHERE entry_id = ?", (rows[0]["entry_id"],))
        self.assertEqual(json.dumps(latest, sort_keys=True, default=str), before)
        # --- 3. the reversal's own audit trail
        opened = self.engine.reopen_audits("D-1")
        self.assertEqual(len(opened), 1)
        entry = opened[0]
        self.assertEqual(entry["event_type"], payments.REOPEN_AUDIT_OPENED)
        self.assertEqual(entry["payment_event_id"], collection["event_id"])
        self.assertEqual(entry["collection_entry_id"], rows[0]["entry_id"])
        self.assertEqual(entry["reversal_entry_id"], rows[1]["entry_id"])
        self.assertEqual(entry["un_billed_amount_cents"], -40000)
        self.assertEqual(entry["un_billed_fee_cents"], -8000)
        self.assertEqual(entry["account_state_after"], payments.STATE_REOPENED_AWAITING_AUDIT)
        self.assertEqual(entry["hold_state_kept"], 0)
        self.assertRegex(entry["recorded_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        # --- 4. no claim before the re-audit, and no resurrection of the purged queue
        for channel in ("voice", "email", "sms"):
            with self.subTest(channel=channel):
                blocked = self.engine.send("D-1", channel, dispatch=False)
                self.assertEqual(blocked["decision"], BLOCKED_REOPENED_AWAITING_AUDIT)
                self.assertFalse(blocked["claimed"])
        row = self.contacts_for("D-1")[-1]
        self.assertEqual(row["decision"], BLOCKED_REOPENED_AWAITING_AUDIT)
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIsNone(row["claim_token"])
        self.assertEqual(row["account_state_at_claim"], payments.STATE_REOPENED_AWAITING_AUDIT)
        self.assertEqual(self.engine.settings("CLI-TEST")["claim_seq"], 0, "a refusal is not a claim")
        self.assertEqual(self.engine.queue.all(), [])
        self.assertEqual(self.engine.run_due(), [])
        for run in runs:
            self.assertIsNone(self.engine.queue.get(run["run_id"]))
        # --- and only the completed re-audit returns it to normal flow, under the same mode
        done = self.engine.run_reopen_audit("D-1", performed_by="ops@client",
                                           reason="bounce confirmed by the bank")
        self.assertEqual(done["status"], "REOPEN_AUDIT_COMPLETED")
        self.assertEqual(self.engine.debtor("D-1")["account_state"], payments.STATE_OPEN)
        self.assertEqual(self.engine.queue.all(), [], "the re-audit restores nothing")
        self.assertEqual(self.engine.debtor("D-1")["approval_status"], "Pending",
                         "an approval that predates the reversal does not carry over")
        self.assertEqual(self.engine.send("D-1", "voice", dispatch=False)["decision"],
                         BLOCKED_MODE_HITL)
        self.engine.approve("D-1", who="reviewer@client")
        claimed = self.engine.send("D-1", "voice", who="reviewer@client", dispatch=False)
        self.assertEqual(claimed["decision"], CLAIMED)
        self.assertTrue(claimed["claimed"])
        self.assertEqual(claimed["operating_mode_at_claim"], "hitl")

    def test_net_totals_after_a_full_reversal_are_zero_for_that_collection(self):
        self._collect(250.0)
        self.engine.record_reversal("D-1", kind="refunded")
        totals = self.engine.ledger("CLI-TEST")["totals"]
        self.assertEqual(totals["gross_collected_cents"], 25000)
        self.assertEqual(totals["reversed_cents"], -25000)
        self.assertEqual(totals["net_collected_cents"], 0)
        self.assertEqual(totals["net_fee_cents"], 0)
        self.assertEqual(totals["reversals_count"], 1)

    def test_a_reversal_leaves_other_collections_for_the_same_client_untouched(self):
        self.engine.record_payment("D-2", 1820.53, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        first = self.engine.record_payment("D-2", 1000.0, payments.CHANNEL_DELTA_SYNC)
        self.engine.record_reversal("D-2", kind="bounced",
                                    reverses_entry_id=first["ledger_entry_id"])
        totals = self.engine.ledger("CLI-TEST")["totals"]
        self.assertEqual(totals["net_collected_cents"], 182053)
        self.assertEqual(totals["net_fee_cents"], 36411)
        self.assertEqual(totals["reversals_count"], 1)

    def test_a_reversal_needs_a_collection_behind_it(self):
        with self.assertRaises(payments.PaymentRefused) as ctx:
            self.engine.record_reversal("D-1", kind="bounced")
        self.assertIn("no un-reversed collection", str(ctx.exception))
        self.assertEqual(self.ledger_rows(), [])

    def test_the_same_collection_cannot_be_reversed_twice(self):
        collection = self._collect(100.0)
        self.engine.record_reversal("D-1", kind="bounced",
                                    reverses_entry_id=collection["ledger_entry_id"])
        with self.assertRaises(payments.PaymentRefused):
            self.engine.record_reversal("D-1", kind="refunded",
                                        reverses_entry_id=collection["ledger_entry_id"])
        self.assertEqual(len(self.ledger_rows()), 2)

    def test_a_partial_reversal_amount_is_refused_rather_than_guessed(self):
        collection = self._collect(400.0)
        with self.assertRaises(payments.PaymentRefused) as ctx:
            self.engine.record_reversal("D-1", kind="bounced",
                                        reverses_entry_id=collection["ledger_entry_id"],
                                        amount=100.0)
        self.assertIn("full reversal", str(ctx.exception))
        self.assertEqual(len(self.ledger_rows()), 1)

    def test_a_reversal_of_a_reversal_is_refused(self):
        collection = self._collect(100.0)
        reversal = self.engine.record_reversal("D-1", kind="bounced",
                                              reverses_entry_id=collection["ledger_entry_id"])
        with self.assertRaises(payments.PaymentRefused):
            self.engine.record_reversal("D-1", reverses_entry_id=reversal["counter_entry_id"])
        self.assertEqual(len(self.ledger_rows()), 2)


# ---------------------------------------------------------------------------
class TestLedgerAppendOnly(PaymentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.engine.record_payment("D-1", 400.0, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        self.engine.record_balance_edit("D-2", 4000.0, who="ops@client", reason="credit note")

    def test_update_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.engine.store.execute(
                "UPDATE collections_ledger SET fee_cents = 1 WHERE debtor_id = 'D-1'")
        self.assertIn("append-only", str(ctx.exception))
        self.assertEqual(self.ledger_rows()[0]["fee_cents"], 8000)

    def test_delete_is_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.engine.store.execute("DELETE FROM collections_ledger WHERE debtor_id = 'D-1'")
        self.assertIn("append-only", str(ctx.exception))
        self.assertEqual(len(self.ledger_rows()), 1)

    def test_the_intake_and_the_balance_edit_logs_are_append_only_too(self):
        for table in ("payment_events", "balance_edit_events"):
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.IntegrityError) as ctx:
                    self.engine.store.execute(f"UPDATE {table} SET note = 'edited'")
                self.assertIn("append-only", str(ctx.exception))
                with self.assertRaises(sqlite3.IntegrityError) as ctx:
                    self.engine.store.execute(f"DELETE FROM {table}")
                self.assertIn("append-only", str(ctx.exception))
        self.assertEqual(len(self.event_rows()), 1)
        self.assertEqual(len(self.engine.balance_edit_events()), 1)

    def test_the_ledger_survives_across_connections(self):
        other = self.new_engine()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                other.store.execute("DELETE FROM collections_ledger")
            self.assertEqual(len(other.ledger("CLI-TEST")["lines"]), 1)
        finally:
            other.store.close()


# ---------------------------------------------------------------------------
class TestLedgerReadModel(PaymentTestCase):
    def test_an_empty_ledger_reports_zero_rather_than_a_placeholder_figure(self):
        model = self.engine.ledger("CLI-TEST")
        self.assertEqual(model["lines"], [])
        self.assertEqual(model["totals"]["net_collected_cents"], 0)
        self.assertEqual(model["totals"]["net_fee_cents"], 0)
        self.assertEqual(model["totals"]["line_count"], 0)
        self.assertTrue(json.dumps(model))   # JSON-serialisable as-is

    def test_the_model_returns_real_lines_with_the_net_total_and_is_json_serialisable(self):
        first = self.engine.record_payment("D-1", 400.0, payments.CHANNEL_ACCOUNTING_WEBHOOK,
                                          signal_at="2026-10-06T13:59:59Z")
        second = self.engine.record_payment("D-2", 100.0, payments.CHANNEL_MANUAL_ATTESTATION,
                                            attested_by="cfo@client")
        model = self.engine.ledger("CLI-TEST")
        self.assertEqual(json.loads(json.dumps(model)), model)
        self.assertEqual(model["client_id"], "CLI-TEST")
        self.assertEqual(len(model["lines"]), 2)
        by_entry = {l["entry_id"]: l for l in model["lines"]}
        line = by_entry[first["ledger_entry_id"]]
        self.assertEqual(line["amount"], 400.0)
        self.assertEqual(line["amount_cents"], 40000)
        self.assertEqual(line["fee_cents"], 8000)
        self.assertEqual(line["fee_amount"], 80.0)
        self.assertEqual(line["confirmation_channel"], "accounting_webhook")
        self.assertEqual(line["confirmation_latency"], payments.confirmation_latency("accounting_webhook"))
        self.assertEqual(line["signal_at"], "2026-10-06T13:59:59Z")
        self.assertTrue(line["billable"])
        self.assertIsNone(line["reverses_entry_id"])
        self.assertIsNone(line["reversed_by_entry_id"])
        self.assertEqual(line["payment_event_id"], first["event_id"])
        self.assertEqual(line["payment_event"]["cancelled_run_count"], 0)
        attested = by_entry[second["ledger_entry_id"]]
        self.assertEqual(attested["attested_by"], "cfo@client")
        self.assertEqual(attested["confirmation_latency"],
                         payments.confirmation_latency("manual_attestation"))
        self.assertEqual(model["totals"]["net_collected_cents"], 50000)
        self.assertEqual(model["totals"]["net_fee_cents"], 10000)
        self.assertEqual(model["totals"]["collections_count"], 2)

    def test_the_model_shows_both_sides_of_a_reversal(self):
        collection = self.engine.record_payment("D-1", 400.0, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        reversal = self.engine.record_reversal("D-1", kind="bounced",
                                              reverses_entry_id=collection["ledger_entry_id"])
        model = self.engine.ledger("CLI-TEST")
        by_entry = {l["entry_id"]: l for l in model["lines"]}
        self.assertEqual(by_entry[collection["ledger_entry_id"]]["reversed_by_entry_id"],
                         reversal["counter_entry_id"])
        reversed_line = by_entry[reversal["counter_entry_id"]]
        self.assertEqual(reversed_line["entry_type"], "reversal")
        self.assertEqual(reversed_line["amount_cents"], -40000)
        self.assertEqual(reversed_line["fee_cents"], -8000)
        self.assertFalse(reversed_line["billable"])
        self.assertEqual(model["totals"]["net_fee_cents"], 0)
        self.assertEqual(self.engine.ledger_for_debtor("D-1")[-1]["entry_id"],
                         reversal["counter_entry_id"])

    def test_the_ledger_reads_only_real_state(self):
        self.engine.record_payment("D-1", 400.0, payments.CHANNEL_ACCOUNTING_WEBHOOK)
        model = self.engine.ledger("CLI-TEST")
        stored = self.ledger_rows()[0]
        line = model["lines"][0]
        for column in ("amount_cents", "fee_cents", "fee_basis_points", "confirmation_channel",
                       "confirmation_latency", "recorded_at", "balance_before_cents",
                       "balance_after_cents"):
            with self.subTest(column=column):
                self.assertEqual(line[column], stored[column])


if __name__ == "__main__":
    unittest.main()
