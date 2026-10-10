"""The console's daily-operations read models and the paths behind them.

What is under test here:
  * every queue row carries the RIGHT status label for its state and a reason sentence that
    says why — including the precedence rule (a payment state is never hidden behind a DNC
    badge) and the fact that the raw flags travel on the row alongside the label;
  * the mode view's `full_auto_selectable` IS the engine's own sign-off verdict
    (`onboarding.signoff_state`), so the button can never be disabled for a reason the
    engine would not also refuse on, nor enabled for one it would not honour;
  * the money trail's arithmetic: the fee is exactly 20% of funds confirmed collected,
    computed in integer cents, and the statement stays DRAFT with the ledger's own contents
    as the stated reason;
  * alert sentences print a cent value as dollars with two decimals and never leak the raw
    cent count;
  * `halt_pending_for_client` still halts the schedule when the client is ALREADY in hitl —
    and audits that as `halt_requested` rather than recording a mode change that did not
    happen; a halt is not a delete;
  * the console bridge keeps its schedule across processes, which is the regression guard
    for the `engine.close()` bug: a bridge that closed the STORE instead of the FACADE left
    the run queue in memory and every later process saw an empty schedule.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from engine import daily_ops, onboarding, payments
from engine.app import RecoverAI
from tests.base import EngineTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE = REPO_ROOT / "tools" / "console_bridge.py"


class QueueStatusTests(EngineTestCase):
    """`daily_ops._status_for` is the single place a row's label is chosen."""

    #: (row overrides, last-attempt, expected label, a phrase the reason must carry)
    CASES = (
        ({"account_state": "Paid"}, None, "Paid", "terminal"),
        ({"account_state": payments.STATE_REOPENED_AWAITING_AUDIT}, None,
         "Reopened_Awaiting_Audit", "reversed"),
        ({"account_state": payments.STATE_PENDING_ADJUSTMENT}, None,
         "Pending_Adjustment_Confirmation", "no payment signal"),
        ({"account_state": "Partially_Paid"}, None, "Partially_Paid", "remainder"),
        ({"dnc_flag": 1, "dnc_set_at": "2026-10-06T14:00:00Z"}, None, "DNC", "dnc_flag = TRUE"),
        ({"approval_status": "Outside_Calling_Window",
          "window_decision": "BLOCKED_OUTSIDE_WINDOW",
          "derived_local_time": "2026-10-06 04:00"}, None,
         "Outside_Calling_Window", "calling-window guardrail"),
        ({}, {"status": "BLOCKED", "decision": "BLOCKED_DNC",
              "reason_detail": "the debtor is on the do-not-contact list"},
         "BLOCKED_DNC", "do-not-contact list"),
        ({}, {"claim_token": "clm_abc", "contact_log_id": 7, "status": "CLAIMED"},
         "CLAIMED", "committed a claim"),
        ({}, None, "Pending", "queued"),
    )

    def test_every_state_renders_its_own_status_and_reason(self) -> None:
        for overrides, attempt, status, phrase in self.CASES:
            with self.subTest(status=status):
                row = {"debtor_id": "D-1", "account_state": "Open", "approval_status": "Pending",
                       "dnc_flag": 0}
                row.update(overrides)
                label = daily_ops._status_for(row, attempt)
                self.assertEqual(label["status"], status)
                self.assertIn(phrase, label["status_reason"])

    def test_a_paid_record_outranks_its_own_dnc_flag(self) -> None:
        """Precedence, stated: the payment state wins the label, but the flag still travels."""
        label = daily_ops._status_for(
            {"account_state": "Paid", "dnc_flag": 1, "dnc_set_at": "2026-10-06T14:00:00Z"}, None)
        self.assertEqual(label["status"], "Paid")
        self.assertNotIn("dnc_flag = TRUE", label["status_reason"])

    def test_queue_rows_label_the_states_real_payments_produce(self) -> None:
        self.add_debtor("D-PAID", balance=500.0)
        self.add_debtor("D-PART", balance=500.0)
        self.engine.record_payment("D-PAID", 500.0, "accounting_webhook",
                                   attested_by="test", external_reference="e-paid")
        self.engine.record_payment("D-PART", 200.0, "accounting_webhook",
                                   attested_by="test", external_reference="e-part")
        queue = self.engine.console_queue("CLI-TEST")
        rows = {row["debtor_id"]: row for row in queue["rows"]}
        self.assertEqual(rows["D-PAID"]["status"], "Paid")
        self.assertIn("terminal", rows["D-PAID"]["status_reason"])
        self.assertEqual(rows["D-PAID"]["balance_cents"], 0)
        self.assertEqual(rows["D-PART"]["status"], "Partially_Paid")
        self.assertIn("remainder", rows["D-PART"]["status_reason"])
        self.assertEqual(rows["D-PART"]["balance_cents"], 30000)
        self.assertEqual(queue["counts"]["by_status"]["Paid"], 1)
        self.assertEqual(queue["counts"]["by_status"]["Partially_Paid"], 1)

    def test_a_dnc_row_shows_the_label_and_still_carries_the_raw_flag(self) -> None:
        self.add_debtor("D-DNC")
        self.engine.store.execute(
            "UPDATE debtor_records SET dnc_flag = 1, dnc_set_at = ? WHERE debtor_id = ?",
            ("2026-10-06T14:00:00Z", "D-DNC"))
        row = next(r for r in self.engine.console_queue("CLI-TEST")["rows"]
                   if r["debtor_id"] == "D-DNC")
        self.assertEqual(row["status"], "DNC")
        self.assertIn("dnc_flag = TRUE", row["status_reason"])
        self.assertEqual(row["dnc_flag"], 1)
        self.assertEqual(row["dnc_set_at"], "2026-10-06T14:00:00Z")

    def test_counters_and_portfolio_come_from_the_rendered_rows(self) -> None:
        self.add_debtor("D-1", balance=100.25)
        self.add_debtor("D-2", balance=0.75)
        queue = self.engine.console_queue("CLI-TEST")
        self.assertEqual(queue["counts"]["debtors"], 2)
        self.assertEqual(queue["counts"]["pending_reviews"], 2)
        self.assertEqual(queue["portfolio"]["balance_total_cents"], 10100)
        self.assertEqual(queue["portfolio"]["balance_total"], "101.00")


class ModeControlTests(EngineTestCase):
    def test_full_auto_selectable_is_the_engines_own_signoff_predicate(self) -> None:
        view = daily_ops.mode_control(self.engine.store, "CLI-TEST", runqueue=self.engine.queue)
        settings = onboarding.ensure_client(self.engine.store, "CLI-TEST")
        verdict = onboarding.signoff_state(settings)
        self.assertFalse(verdict["valid"])
        self.assertEqual(view["full_auto_selectable"], bool(verdict["valid"]))
        self.assertEqual(view["full_auto_locked_reason"], verdict["reason"])
        # ... and the engine itself refuses the flip with the same verdict in force.
        refused = self.engine.switch_mode("CLI-TEST", "full_auto", who="test")
        self.assertTrue(refused.get("refused"))

        with self.subTest("with a signed authorization on file"):
            self.sign_off()
            view = daily_ops.mode_control(self.engine.store, "CLI-TEST", runqueue=self.engine.queue)
            settings = onboarding.ensure_client(self.engine.store, "CLI-TEST")
            self.assertTrue(onboarding.signoff_state(settings)["valid"])
            self.assertTrue(view["full_auto_selectable"])
            self.assertIsNone(view["full_auto_locked_reason"])

    def test_full_auto_locks_again_when_the_signoff_expires(self) -> None:
        self.sign_off()
        self.engine.store.execute(
            "UPDATE client_autonomy_settings SET signoff_expires_at = ? WHERE client_id = ?",
            ("2000-01-01T00:00:00Z", "CLI-TEST"))
        view = daily_ops.mode_control(self.engine.store, "CLI-TEST", runqueue=self.engine.queue)
        self.assertFalse(view["full_auto_selectable"])
        self.assertEqual(view["full_auto_locked_reason"],
                         onboarding.signoff_state(
                             onboarding.ensure_client(self.engine.store, "CLI-TEST"))["reason"])
        self.assertTrue(self.engine.switch_mode("CLI-TEST", "full_auto", who="test").get("refused"))

    def test_mode_view_reports_the_clients_live_schedule_and_the_timeline(self) -> None:
        self.add_debtor("D-1")
        self.sign_off()
        self.engine.schedule("D-1", "voice")
        self.engine.switch_mode("CLI-TEST", "full_auto", who="operator", reason="opt in")
        view = daily_ops.mode_control(self.engine.store, "CLI-TEST", runqueue=self.engine.queue)
        self.assertEqual(view["operating_mode"], "full_auto")
        self.assertEqual(len(view["scheduled_runs"]), 1)
        self.assertEqual(view["scheduled_by_state"], {"scheduled": 1})
        self.assertEqual(view["scheduled_runs"][0]["debtor_id"], "D-1")
        self.assertEqual(view["timeline"][-1]["old_mode"], "hitl")
        self.assertEqual(view["timeline"][-1]["new_mode"], "full_auto")
        self.assertFalse(view["timeline"][-1]["refused_attempt"])


class MoneyTrailTests(EngineTestCase):
    def test_the_fee_is_exactly_twenty_percent_in_integer_cents(self) -> None:
        self.add_debtor("D-1", balance=1234.56)
        self.engine.record_payment("D-1", 1234.56, "accounting_webhook",
                                   attested_by="test", external_reference="e-1")
        trail = self.engine.console_money("CLI-TEST")
        totals = trail["ledger"]["totals"]
        self.assertEqual(totals["net_collected_cents"], 123456)
        self.assertEqual(totals["net_fee_cents"], 24691)  # 24691.2 cents, half up
        self.assertEqual(trail["statement"]["net_collected"], "1,234.56")
        self.assertEqual(trail["statement"]["net_fee"], "246.91")
        self.assertEqual(trail["statement"]["fee_rate_percent"], 20.0)
        self.assertEqual(trail["collected_to_date_label"], "$1,234.56 collected to date")

    def test_a_half_cent_fee_rounds_half_up_and_never_on_a_float(self) -> None:
        # 19.99 -> 1999 cents; 20% is 399.8 cents, so the fee line is 400.
        self.assertEqual(payments.fee_for(1999), 400)
        self.add_debtor("D-1", balance=19.99)
        self.engine.record_payment("D-1", 19.99, "accounting_webhook",
                                   attested_by="test", external_reference="e-2")
        trail = self.engine.console_money("CLI-TEST")
        self.assertEqual(trail["ledger"]["totals"]["net_fee_cents"], 400)
        self.assertEqual(trail["statement"]["net_fee"], "4.00")

    def test_the_statement_is_draft_and_the_reason_is_the_ledgers_own_contents(self) -> None:
        self.add_debtor("D-1", balance=250.0)
        empty = self.engine.console_money("CLI-TEST")["statement"]
        self.assertEqual(empty["status"], "DRAFT")
        self.assertEqual(empty["line_count"], 0)
        self.assertIn("No collections have been recorded", empty["status_reason"])

        self.engine.record_payment("D-1", 250.0, "accounting_webhook",
                                   attested_by="test", external_reference="e-3")
        filled = self.engine.console_money("CLI-TEST")["statement"]
        self.assertEqual(filled["status"], "DRAFT")
        self.assertEqual(filled["line_count"], 1)
        self.assertIn("DRAFT", filled["status_reason"])
        self.assertIn("no payment", filled["status_reason"].lower())

    def test_every_ledger_line_cites_the_channel_that_confirmed_it(self) -> None:
        self.add_debtor("D-1", balance=100.0)
        self.engine.record_payment("D-1", 100.0, "accounting_webhook",
                                   attested_by="test", external_reference="e-4")
        trail = self.engine.console_money("CLI-TEST")
        line = trail["ledger"]["lines"][-1]
        self.assertEqual(line["confirmation_channel"], "accounting_webhook")
        self.assertTrue(line["confirmation_latency"])
        self.assertEqual(trail["payment_events"][-1]["confirmation_channel"], "accounting_webhook")

    def test_the_money_trail_counts_the_runs_a_payment_purged(self) -> None:
        self.add_debtor("D-1", balance=100.0)
        self.engine.schedule("D-1", "voice")
        self.engine.schedule("D-1", "email")
        result = self.engine.record_payment("D-1", 100.0, "accounting_webhook",
                                            attested_by="test", external_reference="e-5")
        self.assertEqual(result["cancelled_run_count"], 2)
        trail = self.engine.console_money("CLI-TEST")
        self.assertEqual(trail["payment_events"][-1]["cancelled_run_count"], 2)
        self.assertEqual(self.engine.queue.all(), [])


class AlertSentenceTests(EngineTestCase):
    def test_a_payment_sentence_prints_cents_as_dollars_with_two_decimals(self) -> None:
        sentence = daily_ops._alert_sentence({
            "action": "payment_recorded", "debtor_id": "D-1", "amount_cents": 482050,
            "balance_after_cents": 0, "confirmation_channel": "accounting_webhook",
            "cancelled_run_count": 1})
        self.assertIn("$4,820.50", sentence)
        self.assertIn("$0.00", sentence)
        self.assertNotIn("482050", sentence)

    def test_a_fee_sentence_prints_cents_as_dollars_with_two_decimals(self) -> None:
        sentence = daily_ops._alert_sentence({
            "action": "ledger_entry_appended", "debtor_id": "D-1", "fee_cents": 96410,
            "confirmation_channel": "accounting_webhook"})
        self.assertIn("$964.10", sentence)
        self.assertNotIn("96410", sentence)

    def test_an_unknown_action_invents_no_amount(self) -> None:
        sentence = daily_ops._alert_sentence({"action": "something_new", "debtor_id": "D-1"})
        self.assertIn("something_new", sentence)
        self.assertNotIn("$", sentence)

    def test_the_feed_reads_the_append_only_log_and_marks_engine_wide_entries(self) -> None:
        self.add_debtor("D-1", balance=100.0)
        self.engine.record_payment("D-1", 100.0, "accounting_webhook",
                                   attested_by="test", external_reference="e-6")
        self.engine.audit.record("signoff_created", signer="Sandbox Operator")
        feed = self.engine.console_alerts(limit=50)
        actions = [entry["action"] for entry in feed["entries"]]
        self.assertIn("payment_recorded", actions)
        self.assertGreater(feed["high_priority_count"], 0)
        payment = next(e for e in feed["entries"] if e["action"] == "payment_recorded")
        self.assertTrue(payment["high_priority"])
        engine_wide = next(e for e in feed["entries"] if e["action"] == "signoff_created")
        self.assertFalse(engine_wide["client_scoped"])


class HaltTests(EngineTestCase):
    def test_the_halt_stops_the_schedule_and_is_audited(self) -> None:
        self.add_debtor("D-1")
        self.add_debtor("D-2")
        signed = self.engine.schedule("D-1", "voice")
        second = self.engine.schedule("D-2", "email")
        result = self.engine.halt_pending_for_client("CLI-TEST", "operator", "stop everything")
        self.assertEqual(result["halted_count"], 2)
        self.assertEqual(result["new_mode"], "hitl")
        # Already hitl, so no mode change is recorded - but the halt still ran, and says so.
        self.assertFalse(result["mode_changed"])
        self.assertIsNone(result["mode_change"])
        states = {run.run_id: (run.state, run.state_reason) for run in self.engine.queue.all()}
        self.assertEqual(states[signed["run_id"]][0], "halted")
        self.assertEqual(states[signed["run_id"]][1], "stop everything")
        self.assertEqual(states[second["run_id"]][0], "halted")
        # A halt is not a delete: both runs are still on the queue, in state halted.
        self.assertEqual(len(self.engine.queue.all()), 2)

        actions = [entry["action"] for entry in self._audit_entries()]
        self.assertIn("halt_requested", actions)
        self.assertIn("run_halted", actions)
        self.assertNotIn("mode_change", actions)

    def test_a_second_press_still_halts_the_work_scheduled_in_between(self) -> None:
        self.add_debtor("D-1")
        self.engine.schedule("D-1", "voice")
        self.engine.halt_pending_for_client("CLI-TEST", "operator", "first press")
        self.engine.schedule("D-1", "email")
        second = self.engine.halt_pending_for_client("CLI-TEST", "operator", "second press")
        self.assertEqual(second["halted_count"], 1)
        self.assertEqual([run.state for run in self.engine.queue.all()],
                         ["halted", "halted"])

    def _audit_entries(self):
        path = self.tmp / "guardrail_actions.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]


class ConsoleSchedulePersistenceTests(EngineTestCase):
    """The run queue is a queue of RECORD for the console: it survives the process."""

    def test_close_persists_the_run_queue_and_the_next_engine_sees_it(self) -> None:
        self.add_debtor("D-1")
        runs_path = self.tmp / "console_runs.json"
        first = RecoverAI(db_path=str(self.db_path), backend="sqlite",
                          vendor_base_url=self.vendor_url, audit_path=self.tmp / "a1.jsonl",
                          paths=self.paths, runqueue_path=runs_path)
        run = first.schedule("D-1", "voice")
        first.close()
        self.assertTrue(runs_path.exists())

        second = RecoverAI(db_path=str(self.db_path), backend="sqlite",
                           vendor_base_url=self.vendor_url, audit_path=self.tmp / "a2.jsonl",
                           paths=self.paths, runqueue_path=runs_path)
        self.assertEqual([r.run_id for r in second.queue.all()], [run["run_id"]])
        second.close()

    def test_a_store_only_close_would_lose_the_schedule(self) -> None:
        """The bug this guards: `store.close()` is not `engine.close()`."""
        self.add_debtor("D-1")
        runs_path = self.tmp / "console_runs.json"
        wrong = RecoverAI(db_path=str(self.db_path), backend="sqlite",
                          vendor_base_url=self.vendor_url, audit_path=self.tmp / "a3.jsonl",
                          paths=self.paths, runqueue_path=runs_path)
        wrong.schedule("D-1", "voice")
        wrong.store.close()  # the wrong half: the queue never reaches disk
        self.assertFalse(runs_path.exists())

        right = RecoverAI(db_path=str(self.db_path), backend="sqlite",
                          vendor_base_url=self.vendor_url, audit_path=self.tmp / "a4.jsonl",
                          paths=self.paths, runqueue_path=runs_path)
        run = right.schedule("D-1", "voice")
        right.close()  # the facade writes the queue, then closes the store
        self.assertTrue(runs_path.exists())
        self.assertEqual([r["run_id"] for r in json.loads(runs_path.read_text())], [run["run_id"]])


class BridgeAcrossProcessesTests(EngineTestCase):
    """Two bridge processes, one schedule — through the same door the console uses."""

    SANDBOX_CLIENT = "CLI-SANDBOX-AGENCY"

    def setUp(self) -> None:
        super().setUp()
        self.console_db = self.tmp / "console.db"
        self.console_runs = self.tmp / "console_runs.json"
        self.console_audit = self.tmp / "console_audit.jsonl"
        # The bridge's own store, seeded with one debtor for the bridge's sandbox client.
        seeding = RecoverAI(db_path=str(self.console_db), backend="sqlite", fresh=True,
                            vendor_base_url=self.vendor_url, audit_path=self.console_audit,
                            paths=self.paths, runqueue_path=self.console_runs)
        self._insert_debtor(seeding.store, "CD-1", self.SANDBOX_CLIENT)
        seeding.close()

    @staticmethod
    def _insert_debtor(store, debtor_id: str, client_id: str, balance: float = 1000.0) -> None:
        """The same row shape the ingestion path writes, straight into a store."""
        store.execute(
            """INSERT INTO debtor_records
                 (debtor_id, client_id, name, email, phone, balance_owed, invoice_date, aging_state,
                  approval_status, dnc_flag, invoice_number, source_system, source_ref, source_columns,
                  raw_record, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (debtor_id, client_id, f"Debtor {debtor_id}", "debtor@example.com", "(212) 555-0143",
             balance, "2026-08-01", "60", "Pending", 0, f"INV-{debtor_id}", "test-fixture",
             "fixture://rows", "{}", "{}", "2026-10-06T14:00:00Z"))

    def call(self, request: dict) -> dict:
        env = dict(os.environ,
                   RECOVERAI_CONSOLE_DB=str(self.console_db),
                   RECOVERAI_CONSOLE_RUNS=str(self.console_runs),
                   RECOVERAI_CONSOLE_AUDIT=str(self.console_audit))
        proc = subprocess.run([sys.executable, str(BRIDGE)], input=json.dumps(request),
                              capture_output=True, text=True, cwd=str(REPO_ROOT), env=env)
        return json.loads(proc.stdout)

    def console_state(self) -> dict:
        """A read of the console store after the subprocesses have exited."""
        engine = RecoverAI(db_path=str(self.console_db), backend="sqlite",
                           vendor_base_url=self.vendor_url, audit_path=self.console_audit,
                           paths=self.paths, runqueue_path=self.console_runs)
        state = {
            "contacts": [dict(row) for row in engine.store.query(
                "SELECT debtor_id, status, decision, claim_token FROM outbound_contact_logs "
                "ORDER BY contact_log_id")],
            "queued_runs": [run.as_dict() for run in engine.queue.all()],
        }
        engine.close()
        return state

    def test_the_bridge_keeps_the_schedule_across_processes(self) -> None:
        scheduled = self.call({"op": "schedule_run", "debtor_id": "CD-1", "channel": "voice"})
        self.assertTrue(scheduled["bridge_ok"], scheduled)
        run_id = scheduled["payload"]["run"]["run_id"]

        # A SECOND process, with its own engine and its own in-memory queue.
        board = self.call({"op": "board"})
        self.assertTrue(board["bridge_ok"], board)
        runs = board["payload"]["mode"]["scheduled_runs"]
        self.assertEqual([r["run_id"] for r in runs], [run_id])
        self.assertEqual(board["payload"]["mode"]["scheduled_by_state"], {"scheduled": 1})

        halt = self.call({"op": "halt"})
        self.assertTrue(halt["bridge_ok"], halt)
        self.assertEqual(halt["payload"]["halted_count"], 1)

        # A THIRD process sees the halt as state, not as a memory that died with the last one.
        after = self.call({"op": "board"})
        self.assertEqual(after["payload"]["mode"]["scheduled_by_state"], {"halted": 1})
        self.assertEqual(after["payload"]["mode"]["scheduled_runs"][0]["state"], "halted")
        self.assertEqual(after["payload"]["queue"]["counts"]["scheduled_runs"], 0)

    def test_simulate_payment_targets_the_first_live_balance_and_refuses_when_there_is_none(self) -> None:
        result = self.call({"op": "simulate_payment"})
        self.assertTrue(result["bridge_ok"], result)
        payload = result["payload"]
        self.assertTrue(payload["simulated"])
        # The console renders its close-the-loop panel on `ok`, and the payment path's own
        # result has no such key - so the bridge states it. Without this the panel stayed
        # hidden while the ledger line and statement moved (found by the click-through).
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["debtor_id"], "CD-1")
        self.assertEqual(payload["money"]["ledger"]["totals"]["line_count"], 1)
        self.assertEqual(payload["money"]["statement"]["status"], "DRAFT")

        again = self.call({"op": "simulate_payment"})["payload"]
        self.assertFalse(again["ok"])
        self.assertEqual(again["refusal_token"], "NO_RECORD_WITH_A_BALANCE")

    def test_dispatch_approved_claims_and_never_reports_a_send(self) -> None:
        self.call({"op": "decide", "debtor_id": "CD-1", "action": "approve", "who": "operator"})
        result = self.call({"op": "dispatch_approved", "channel": "email"})["payload"]
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["claimed"], 1)
        self.assertEqual(result["refused"], 0)
        self.assertFalse(result["dispatch_attempted"])
        self.assertIn("claimed, not sent", result["dispatch_note"])
        line = result["results"][0]
        self.assertTrue(line["claimed"])
        self.assertEqual(line["status"], "CLAIMED")
        # The queue row shows the claim, and says the claim is what happened.
        row = next(r for r in result["queue"]["rows"] if r["debtor_id"] == "CD-1")
        self.assertEqual(row["status"], "CLAIMED")
        self.assertIn("committed a claim", row["status_reason"])
        # Nothing was posted anywhere: the contact log says CLAIMED, never SENT, and it is
        # still there when a different process reads the store.
        self.assertEqual([c["status"] for c in self.console_state()["contacts"]], ["CLAIMED"])


if __name__ == "__main__":
    unittest.main()
