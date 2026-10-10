"""Phase 2 - the beta-to-active gate, its state machine and Milestone B.

Everything here is adversarial on purpose: the gate exists to stop a client being
promoted into a 20% fee we cannot actually collect, so each test asks "can this be
talked into passing when it should refuse?" rather than "does the happy path work?".
"""
from __future__ import annotations

import ast
import json
import os
import socket
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from engine import clock, config, db, gate
from engine.orchestrator import BLOCKED_MODE_HITL, BLOCKED_NO_SIGNOFF, CLAIMED
from tests.base import EngineTestCase

GATE_SOURCE = Path(gate.__file__)
EVIDENCE_PI = "pi_3U8DS4DtdnNSJFla0uMRTHFW"      # the only id the evidence file records
ENGINE_DIR = Path(gate.__file__).resolve().parent


_UNSET = object()


def receipt(account_id=_UNSET, livemode=True, charges=True, payouts=True,
            payment_intent=EVIDENCE_PI, timestamp="2026-10-06T16:23:12Z", capabilities=_UNSET):
    """A receipt that passes every check by default, so each test changes one thing."""
    return {
        "payment_intent_sample": payment_intent,
        "livemode": livemode,
        "capabilities": ({"charges_enabled": charges, "payouts_enabled": payouts}
                         if capabilities is _UNSET else capabilities),
        "account_id": gate.SANDBOX_PIN if account_id is _UNSET else account_id,
        "timestamp_utc": timestamp,
    }


def source_text() -> str:
    return GATE_SOURCE.read_text(encoding="utf-8")


def engine_sources():
    for path in sorted(ENGINE_DIR.glob("*.py")):
        yield path, path.read_text(encoding="utf-8")


class GateTestCase(EngineTestCase):
    CLIENT = "CLI-GATE"

    def setUp(self):
        super().setUp()
        self.engine.settings(self.CLIENT)  # ensure the client row exists

    def flags(self, client_id=None):
        return gate._flag_snapshot(self.engine.store, client_id or self.CLIENT)

    def verify(self, rcpt, **kwargs):
        kwargs.setdefault("client_id", self.CLIENT)
        kwargs.setdefault("recorded_by", "gate-tester")
        kwargs.setdefault("verification_source", "pytest/stripe read-back")
        return gate.verify_milestone_b_receipt(self.engine.store, rcpt, **kwargs)

    def chain_rows(self):
        return self.engine.store.query("SELECT * FROM billing_route_verifications ORDER BY seq")

    #: A real pinned account id supplied through the environment, never through the shared
    #: config register, so these tests cannot disturb (or depend on) the shipped pin.
    PIN = "acct_1PinnedByteExact00000001"
    #: The sha256 of the executed agreement document used by the evidence tests.
    MSA_DOC_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def pin_environment(self):
        """The pinned environment the evidence tests run in. No network, no real account."""
        return mock.patch.dict(os.environ, {gate.ENV_PLATFORM_ACCOUNT_ID: self.PIN})

    def record_msa(self, client_id=None, reference="MSA-RECOVERAI-NORTHWIND-2026-10"):
        """Record an EXECUTED agreement - the only thing that satisfies the MSA milestone."""
        return gate.record_commercial_msa(
            self.engine.store, client_id or self.CLIENT, who="owner",
            reason="the commercial Master Services Agreement was executed on 2026-10-06",
            counterparty_legal_name="Northwind Studio LLC", signatory_name="Dana Reyes",
            signatory_title="Chief Financial Officer", agreement_reference=reference,
            document_version="v2.0-commercial", executed_at="2026-10-06T09:30:00Z",
            document_sha256=self.MSA_DOC_SHA)

    def meet_every_milestone(self):
        """Drive all three milestones through their REAL evidence paths.

        Called inside `with self.pin_environment():` - the live milestone is derived from
        the pin at read time, so the patch has to still be in force when it is read.
        """
        self.verify(receipt(account_id=self.PIN))
        for index in range(gate.MANUAL_REVIEW_THRESHOLD):
            gate.record_manual_review_decision(self.engine.store, self.CLIENT, f"D-{index}",
                                               "approved", "reviewer", "hitl")
        gate.refresh_manual_review_milestone(self.engine.store, self.CLIENT, "owner",
                                            "twenty manual hitl reviews are recorded")
        self.record_msa()
        return self.PIN

    def assert_refused_without_flag_write(self, outcome, token=None):
        self.assertIn(outcome["decision"],
                      (gate.DECISION_MALFORMED, gate.DECISION_TIMEOUT_UNKNOWN,
                       gate.DECISION_CAPABILITY_DISABLED, gate.DECISION_ACCOUNT_MISMATCH))
        self.assertTrue(outcome["gate_locked"])
        self.assertFalse(outcome["live_billing_verified"])
        self.assertEqual(gate.flag_value(self.engine.store, self.CLIENT,
                                         gate.MILESTONE_LIVE_BILLING), 0)
        if token:
            self.assertEqual(outcome["reason_token"], token)


# ---------------------------------------------------------------------------
# Tier 1 - lifecycle states, flags and the transition log
# ---------------------------------------------------------------------------
class TestLifecycleStates(GateTestCase):
    def test_every_new_profile_starts_as_a_beta_tester(self):
        for client_id in ("CLI-FRESH-1", "CLI-FRESH-2"):
            row = self.engine.settings(client_id)
            self.assertEqual(row["lifecycle_status"], gate.STATUS_BETA_TESTER)
            self.assertEqual(row["manual_review_threshold_passed"], 0)
            self.assertEqual(row["live_billing_verified"], 0)
            self.assertEqual(row["commercial_msa_executed"], 0)

    def test_the_database_default_is_the_beta_state_with_no_insert_saying_so(self):
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                "INSERT INTO client_autonomy_settings (client_id, operating_mode) VALUES (?,?)",
                ("CLI-BARE", "hitl"))
        row = self.engine.store.query_one(
            "SELECT * FROM client_autonomy_settings WHERE client_id = 'CLI-BARE'")
        self.assertEqual(row["lifecycle_status"], gate.STATUS_BETA_TESTER)

    def test_an_unknown_lifecycle_status_is_refused_by_the_store(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE client_autonomy_settings SET lifecycle_status = 'STATUS_WHATEVER' "
                    "WHERE client_id = ?", (self.CLIENT,))

    def test_the_gate_refuses_full_active_while_any_single_milestone_is_false(self):
        for flag in gate.MILESTONES:
            with self.subTest(flag=flag):
                with self.assertRaises(gate.GateLockedException) as ctx:
                    gate.set_lifecycle_status(self.engine.store, self.CLIENT,
                                              gate.STATUS_FULL_ACTIVE, "owner", "try anyway")
                self.assertEqual(ctx.exception.reason_token, "LIFECYCLE_LAUNCH_CONDITION_UNMET")
                self.assertIn(flag, ctx.exception.outstanding)
                self.assertEqual(gate.lifecycle_status(self.engine.store, self.CLIENT),
                                 gate.STATUS_BETA_TESTER)

    def test_a_hand_written_update_cannot_reach_full_active_either(self):
        """The store refuses what the gate refuses: the promotion needs all three flags
        TRUE on the same row, and only the gate sets them."""
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE client_autonomy_settings SET lifecycle_status = ? WHERE client_id = ?",
                    (gate.STATUS_FULL_ACTIVE, self.CLIENT))
        self.assertIn("needs evidence, not flags", str(ctx.exception),
                      "the store refuses on the EVIDENCE, not on the flags")
        # ...and it cannot be smuggled in by a fresh INSERT either.
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "INSERT INTO client_autonomy_settings (client_id, lifecycle_status) "
                    "VALUES ('CLI-SNEAK', ?)", (gate.STATUS_FULL_ACTIVE,))

    def test_the_store_refuses_a_flag_only_promotion_and_allows_an_evidence_backed_one(self):
        """Tier 1: the flags are mirrors. One statement that sets them alongside the status
        is refused, because the store recomputes each milestone from its evidence table."""
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    """UPDATE client_autonomy_settings SET lifecycle_status = ?,
                         manual_review_threshold_passed = 1, live_billing_verified = 1,
                         commercial_msa_executed = 1 WHERE client_id = ?""",
                    (gate.STATUS_FULL_ACTIVE, self.CLIENT))
        self.assertIn("needs evidence, not flags", str(ctx.exception))
        self.assertEqual(gate.lifecycle_status(self.engine.store, self.CLIENT),
                         gate.STATUS_BETA_TESTER)
        # ...and with every milestone actually satisfied by its own recorded evidence, both
        # the store and the gate API let the promotion through.
        with self.pin_environment():
            self.meet_every_milestone()
            self.assertTrue(gate.launch_condition(self.engine.store, self.CLIENT)["all_true"])
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE client_autonomy_settings SET lifecycle_status = ? WHERE client_id = ?",
                    (gate.STATUS_FULL_ACTIVE, self.CLIENT))
            self.assertEqual(gate.lifecycle_status(self.engine.store, self.CLIENT),
                             gate.STATUS_FULL_ACTIVE)

    def test_a_single_statement_setting_every_flag_cannot_promote_a_client(self):
        """QA's reproduction, verbatim: this exact statement used to be ALLOWED.

        `UPDATE client_autonomy_settings SET lifecycle_status='STATUS_FULL_ACTIVE',
        manual_review_threshold_passed=1, live_billing_verified=1,
        commercial_msa_executed=1` is now refused by the store, because the trigger
        recomputes the three milestones from their evidence tables with EXISTS/COUNT and
        finds none of them.
        """
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE client_autonomy_settings SET lifecycle_status='STATUS_FULL_ACTIVE', "
                    "manual_review_threshold_passed=1, live_billing_verified=1, "
                    "commercial_msa_executed=1 WHERE client_id=?", (self.CLIENT,))
        self.assertIn("needs evidence, not flags", str(ctx.exception))
        self.assertEqual(gate.lifecycle_status(self.engine.store, self.CLIENT),
                         gate.STATUS_BETA_TESTER)
        # a brand-new row cannot arrive FULL_ACTIVE either, with or without flags
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "INSERT INTO client_autonomy_settings (client_id, lifecycle_status, "
                    "manual_review_threshold_passed, live_billing_verified, "
                    "commercial_msa_executed) VALUES ('CLI-INSTA', 'STATUS_FULL_ACTIVE', 1, 1, 1)")
        self.assertEqual(gate.lifecycle_status(self.engine.store, "CLI-INSTA"),
                         gate.STATUS_BETA_TESTER)
        # the other two milestones cannot be faked by SQL either: with a REAL live receipt
        # and 20 REAL hitl reviews, a hand-written MSA column leaves the milestone unmet,
        # the launch condition false, and the promotion refused through the gate API.
        with self.pin_environment():
            self.verify(receipt(account_id=self.PIN))
            for index in range(gate.MANUAL_REVIEW_THRESHOLD):
                gate.record_manual_review_decision(self.engine.store, self.CLIENT, f"D-{index}",
                                                   "approved", "reviewer", "hitl")
            gate.refresh_manual_review_milestone(self.engine.store, self.CLIENT, "owner",
                                                "twenty manual hitl reviews are recorded")
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE client_autonomy_settings SET commercial_msa_executed = 1 "
                    "WHERE client_id = ?", (self.CLIENT,))
            state = gate.launch_condition(self.engine.store, self.CLIENT)
            self.assertEqual(gate.flag_value(self.engine.store, self.CLIENT,
                                             gate.MILESTONE_COMMERCIAL_MSA), 1,
                             "the mirror column does move: it is a report field")
            self.assertFalse(state["milestones"][gate.MILESTONE_COMMERCIAL_MSA]["value"],
                             "the MILESTONE does not move: it is derived from the evidence table")
            self.assertEqual(state["outstanding_milestones"], [gate.MILESTONE_COMMERCIAL_MSA])
            self.assertFalse(state["all_true"])
            with self.assertRaises(gate.GateLockedException) as ctx2:
                gate.set_lifecycle_status(self.engine.store, self.CLIENT,
                                          gate.STATUS_FULL_ACTIVE, "owner",
                                          "promote with a hand-written MSA column")
            self.assertEqual(ctx2.exception.outstanding, [gate.MILESTONE_COMMERCIAL_MSA])
            self.assertEqual(gate.lifecycle_status(self.engine.store, self.CLIENT),
                             gate.STATUS_BETA_TESTER)
            # and the one-statement write (flags + status together) is still refused
            with self.assertRaises(sqlite3.IntegrityError):
                with self.engine.store.tx(immediate=True):
                    self.engine.store.execute(
                        "UPDATE client_autonomy_settings SET lifecycle_status='STATUS_FULL_ACTIVE', "
                        "manual_review_threshold_passed=1, live_billing_verified=1, "
                        "commercial_msa_executed=1 WHERE client_id=?", (self.CLIENT,))

    def test_the_promotion_records_which_evidence_satisfied_each_milestone(self):
        """A later reader must be able to see the basis of a promotion, not just its date."""
        with self.pin_environment():
            self.meet_every_milestone()
            result = gate.set_lifecycle_status(self.engine.store, self.CLIENT,
                                               gate.STATUS_FULL_ACTIVE, "owner",
                                               "all three milestones are met by their evidence")
        self.assertEqual(result["new_status"], gate.STATUS_FULL_ACTIVE)
        row = gate.transition_log(self.engine.store, self.CLIENT)[-1]
        parameters = json.loads(row["validating_parameters"])
        evidence = parameters["evidence"]
        self.assertEqual(set(evidence), set(gate.MILESTONES))
        reviews = evidence[gate.MILESTONE_MANUAL_REVIEW]
        self.assertEqual(reviews["evidence_source"], "manual_review_decisions")
        self.assertEqual(reviews["review_count"], gate.MANUAL_REVIEW_THRESHOLD)
        self.assertEqual(len(reviews["review_decision_ids"]), gate.MANUAL_REVIEW_THRESHOLD)
        live = evidence[gate.MILESTONE_LIVE_BILLING]
        self.assertTrue(live["bound"])
        self.assertRegex(live["receipt_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(live["chain_hash"], r"^[0-9a-f]{64}$")
        msa = evidence[gate.MILESTONE_COMMERCIAL_MSA]
        self.assertEqual(msa["evidence_source"], "commercial_msa_records")
        self.assertEqual(msa["agreement_reference"], "MSA-RECOVERAI-NORTHWIND-2026-10")
        self.assertEqual(msa["document_sha256"], self.MSA_DOC_SHA)
        # the row itself is immutable, like every other transition
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.store.execute(
                "UPDATE lifecycle_transition_log SET reason = 'rewritten' WHERE transition_id = ?",
                (row["transition_id"],))

    def test_the_gate_still_refuses_a_hand_set_live_flag_with_no_bound_receipt(self):
        """The column is not the milestone: the launch condition re-derives the binding."""
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                """UPDATE client_autonomy_settings SET manual_review_threshold_passed = 1,
                     live_billing_verified = 1, commercial_msa_executed = 1 WHERE client_id = ?""",
                (self.CLIENT,))
        with self.assertRaises(gate.GateLockedException) as ctx:
            gate.set_lifecycle_status(self.engine.store, self.CLIENT, gate.STATUS_FULL_ACTIVE,
                                      "owner", "all three flags are TRUE")
        self.assertIn(gate.MILESTONE_LIVE_BILLING, ctx.exception.outstanding,
                      "a hand-set column is not a verified live billing route")

    def test_a_transition_needs_a_reason_and_a_known_status(self):
        with self.assertRaises(gate.GateException) as ctx:
            gate.set_lifecycle_status(self.engine.store, self.CLIENT, "STATUS_SOMETHING", "o", "x")
        self.assertEqual(ctx.exception.reason_token, "UNKNOWN_LIFECYCLE_STATUS")
        with self.assertRaises(gate.GateException) as ctx:
            gate.set_lifecycle_status(self.engine.store, self.CLIENT, gate.STATUS_BETA_TESTER, "o", "")
        self.assertEqual(ctx.exception.reason_token, "TRANSITION_REASON_REQUIRED")

    def test_no_call_this_engine_exposes_can_move_a_client_to_full_active(self):
        """Behavioural, not a source scan.

        The previous form of this test asserted that the literal `STATUS_FULL_ACTIVE` was
        absent from the other modules' source text, and that certain UPDATE/INSERT strings
        were absent. Both would still have passed with the runtime guard deleted and the
        text left in place, and NEITHER of them covered the hole that mattered: a direct SQL
        write. This drives every writer this engine actually exposes at the client, plus the
        raw SQL, and asserts the client does not move.
        """
        def incomplete_msa():
            return self.engine.record_commercial_msa(
                self.CLIENT, who="owner",
                reason="a bare flag, not an executed agreement",
                counterparty_legal_name="   ", signatory_name="Dana Reyes",
                signatory_title="Chief Financial Officer", agreement_reference="MSA-1",
                document_version="v1", executed_at="2026-10-06T09:30:00Z",
                document_sha256=self.MSA_DOC_SHA)

        # Every writer this engine exposes is driven at the client. Some are refused on the
        # spot, some are legitimate but move nothing: either way the client does not move.
        writers = {
            "gate.set_lifecycle_status": lambda: gate.set_lifecycle_status(
                self.engine.store, self.CLIENT, gate.STATUS_FULL_ACTIVE, "owner", "just promote it"),
            "gate.set_milestone_flag(review)": lambda: gate.set_milestone_flag(
                self.engine.store, self.CLIENT, gate.MILESTONE_MANUAL_REVIEW, True, "owner", "flip"),
            "gate.set_milestone_flag(live)": lambda: gate.set_milestone_flag(
                self.engine.store, self.CLIENT, gate.MILESTONE_LIVE_BILLING, True, "owner", "flip"),
            "gate.set_milestone_flag(msa)": lambda: gate.set_milestone_flag(
                self.engine.store, self.CLIENT, gate.MILESTONE_COMMERCIAL_MSA, True, "owner", "flip"),
            "gate.refresh_manual_review_milestone": lambda: gate.refresh_manual_review_milestone(
                self.engine.store, self.CLIENT, "owner", "recount the reviews"),
            "app.set_lifecycle_status": lambda: self.engine.set_lifecycle_status(
                self.CLIENT, gate.STATUS_FULL_ACTIVE, "owner", "promote from the console"),
            "app.refresh_manual_review_milestone": lambda: self.engine.refresh_manual_review_milestone(
                self.CLIENT, "owner", "recount from the console"),
            "app.launch_live_outbound": lambda: self.engine.launch_live_outbound(
                self.CLIENT, "voice", "owner", "go live"),
            "app.record_commercial_msa(incomplete)": incomplete_msa,
        }
        for name, call in writers.items():
            with self.subTest(writer=name):
                try:
                    call()
                except gate.GateException:
                    pass    # refused, which is one correct answer
                # whatever it answered, the client did not move:
                self.assertEqual(gate.lifecycle_status(self.engine.store, self.CLIENT),
                                 gate.STATUS_BETA_TESTER)
                self.assertFalse(gate.launch_condition(self.engine.store, self.CLIENT)["all_true"])
        with self.subTest(writer="raw SQL"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.engine.store.tx(immediate=True):
                    self.engine.store.execute(
                        "UPDATE client_autonomy_settings SET lifecycle_status = ? WHERE client_id = ?",
                        (gate.STATUS_FULL_ACTIVE, self.CLIENT))
        self.assertEqual(gate.lifecycle_status(self.engine.store, self.CLIENT),
                         gate.STATUS_BETA_TESTER)
        self.assertFalse(gate.launch_condition(self.engine.store, self.CLIENT)["all_true"])

    def test_the_transition_log_is_append_only_and_millisecond_stamped(self):
        gate.set_milestone_flag(self.engine.store, self.CLIENT,
                                gate.MILESTONE_MANUAL_REVIEW, True, "owner", "reviews done")
        rows = gate.transition_log(self.engine.store, self.CLIENT)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["recorded_at"].endswith("Z"))
        self.assertRegex(row["recorded_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual((row["old_value"], row["new_value"]), ("0", "1"))
        self.assertEqual(row["executing_administrator"], "owner")
        self.assertEqual(row["reason"], "reviews done")
        self.assertIsNotNone(row["validating_parameters"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.store.execute(
                "UPDATE lifecycle_transition_log SET reason = 'rewritten' WHERE transition_id = ?",
                (row["transition_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.store.execute("DELETE FROM lifecycle_transition_log")
        self.assertEqual(len(gate.transition_log(self.engine.store, self.CLIENT)), 1)

    def test_the_commercial_milestone_needs_a_recorded_agreement_not_a_column(self):
        """The milestone is the evidence table. It is EMPTY, so the milestone is FALSE and
        the gate cannot open - and the column, which is only a mirror, cannot change that."""
        status = gate.commercial_msa_milestone_status(self.engine.store, self.CLIENT)
        self.assertFalse(status["satisfied"])
        self.assertEqual(status["state"], "EVIDENCE_REQUIRED")
        self.assertEqual(status["record_count"], 0)
        # a flags-only API call is refused, and says why
        attempt = gate.set_milestone_flag(self.engine.store, self.CLIENT,
                                          gate.MILESTONE_COMMERCIAL_MSA, True, "owner", "sign it")
        self.assertTrue(attempt["refused"])
        self.assertEqual(attempt["reason_token"], "MSA_EVIDENCE_REQUIRED")
        # ...and a direct SQL write of the column does not satisfy the milestone either
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                "UPDATE client_autonomy_settings SET commercial_msa_executed = 1 "
                "WHERE client_id = ?", (self.CLIENT,))
        self.assertEqual(gate.flag_value(self.engine.store, self.CLIENT,
                                         gate.MILESTONE_COMMERCIAL_MSA), 1)
        state = gate.milestone_state(self.engine.store, self.CLIENT)
        self.assertFalse(state["milestones"][gate.MILESTONE_COMMERCIAL_MSA]["value"],
                         "the column is a mirror; the milestone reads commercial_msa_records")
        self.assertIn(gate.MILESTONE_COMMERCIAL_MSA, state["outstanding_milestones"])
        self.assertEqual(gate.lifecycle_status(self.engine.store, self.CLIENT),
                         gate.STATUS_BETA_TESTER)

    def test_recording_an_executed_agreement_satisfies_the_milestone_and_is_append_only(self):
        before = gate.milestone_state(self.engine.store, self.CLIENT)
        self.assertFalse(before["milestones"][gate.MILESTONE_COMMERCIAL_MSA]["value"])
        recorded = self.record_msa()
        self.assertTrue(recorded["changed"])
        self.assertEqual(recorded["agreement_reference"], "MSA-RECOVERAI-NORTHWIND-2026-10")
        after = gate.milestone_state(self.engine.store, self.CLIENT)
        self.assertTrue(after["milestones"][gate.MILESTONE_COMMERCIAL_MSA]["value"])
        self.assertNotIn(gate.MILESTONE_COMMERCIAL_MSA, after["outstanding_milestones"])
        rows = gate.commercial_msa_records(self.engine.store, self.CLIENT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["document_sha256"], self.MSA_DOC_SHA)
        # append-only, like every other evidence record
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.store.execute(
                "UPDATE commercial_msa_records SET signatory_name = 'Someone Else' WHERE msa_id = ?",
                (rows[0]["msa_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.store.execute("DELETE FROM commercial_msa_records")
        self.assertEqual(len(gate.commercial_msa_records(self.engine.store, self.CLIENT)), 1)
        # the recording is in the immutable transition log, with the evidence it recorded
        row = gate.transition_log(self.engine.store, self.CLIENT)[-1]
        self.assertEqual(row["field"], gate.MILESTONE_COMMERCIAL_MSA)
        self.assertEqual(row["new_value"], "1")
        self.assertEqual(json.loads(row["validating_parameters"])["document_sha256"],
                         self.MSA_DOC_SHA)

    def test_recording_an_agreement_needs_complete_evidence(self):
        """A blank field, a short document hash or an unusable reason is not evidence."""
        base = dict(reason="the commercial Master Services Agreement was executed",
                    counterparty_legal_name="Northwind Studio LLC", signatory_name="Dana Reyes",
                    signatory_title="Chief Financial Officer",
                    agreement_reference="MSA-1", document_version="v1",
                    executed_at="2026-10-06T09:30:00Z", document_sha256=self.MSA_DOC_SHA)
        with self.assertRaises(gate.GateException) as ctx:
            gate.record_commercial_msa(self.engine.store, self.CLIENT, who="owner",
                                       **{**base, "signatory_name": "   "})
        self.assertEqual(ctx.exception.reason_token, "MSA_RECORD_INCOMPLETE")
        with self.assertRaises(gate.GateException) as ctx:
            gate.record_commercial_msa(self.engine.store, self.CLIENT, who="owner",
                                       **{**base, "document_sha256": "abc123"})
        self.assertEqual(ctx.exception.reason_token, "MSA_DOCUMENT_HASH_INVALID")
        with self.assertRaises(gate.GateException) as ctx:
            gate.record_commercial_msa(self.engine.store, self.CLIENT, who="owner",
                                       **{**base, "executed_at": "some Tuesday"})
        self.assertEqual(ctx.exception.reason_token, "RECEIPT_TIMESTAMP_UNPARSEABLE")
        with self.assertRaises(gate.GateException) as ctx:
            gate.record_commercial_msa(self.engine.store, self.CLIENT, who="owner",
                                       **{**base, "reason": "signed"})
        self.assertEqual(ctx.exception.reason_token, "TRANSITION_REASON_REQUIRED")
        self.assertEqual(gate.commercial_msa_records(self.engine.store, self.CLIENT), [],
                         "nothing incomplete is recorded")


# ---------------------------------------------------------------------------
# Tier 2 - the receipt contract and strict typing
# ---------------------------------------------------------------------------
class TestReceiptStrictTypes(GateTestCase):
    BAD_BOOLEANS = ("true", "True", "false", "False", "1", "0", 1, 0, 1.0, None, [], {})

    def _set(self, **overrides):
        """Same keyword names as receipt(), so a wrong key cannot sneak past the test."""
        return receipt(**overrides)

    def test_every_wrong_type_is_malformed_for_livemode_charges_and_payouts(self):
        for bad in self.BAD_BOOLEANS:
            with self.subTest(field="livemode", value=repr(bad)):
                with self.assertRaises(gate.MalformedReceiptException) as ctx:
                    gate.parse_receipt(self._set(livemode=bad))
                self.assertEqual(ctx.exception.reason_token, "RECEIPT_TYPE_NOT_BOOLEAN")
            with self.subTest(field="charges_enabled", value=repr(bad)):
                with self.assertRaises(gate.MalformedReceiptException):
                    gate.parse_receipt(self._set(
                        capabilities={"charges_enabled": bad, "payouts_enabled": True}))
            with self.subTest(field="payouts_enabled", value=repr(bad)):
                with self.assertRaises(gate.MalformedReceiptException):
                    gate.parse_receipt(self._set(
                        capabilities={"charges_enabled": True, "payouts_enabled": bad}))

    def test_native_booleans_are_accepted_by_the_parser(self):
        parsed = gate.parse_receipt(self._set(livemode=True))
        self.assertIs(parsed["livemode"], True)
        self.assertIs(parsed["charges_enabled"], True)
        self.assertIs(parsed["payouts_enabled"], True)
        # A native False is NOT malformed: it is a capability deficiency (Tier 3).
        parsed_false = gate.parse_receipt(self._set(livemode=False))
        self.assertIs(parsed_false["livemode"], False)
        with self.assertRaises(gate.CapabilityDisabledException) as ctx:
            gate.check_capabilities(parsed_false)
        self.assertEqual(ctx.exception.reason_token, "NOT_LIVEMODE")

    def test_a_wrong_cased_key_never_satisfies_the_schema(self):
        rcpt = self._set()
        rcpt["True"] = rcpt.pop("livemode")          # the wrong-cased key
        with self.assertRaises(gate.MalformedReceiptException) as ctx:
            gate.parse_receipt(rcpt)
        self.assertEqual(ctx.exception.reason_token, "RECEIPT_KEY_MISSING")
        rcpt2 = self._set()
        rcpt2["Livemode"] = True                       # extra key, right value
        with self.assertRaises(gate.MalformedReceiptException) as ctx:
            gate.parse_receipt(rcpt2)
        self.assertEqual(ctx.exception.reason_token, "RECEIPT_KEY_UNKNOWN")

    def test_a_missing_key_is_never_defaulted(self):
        for key in sorted(gate.RECEIPT_KEYS):
            with self.subTest(missing=key):
                rcpt = self._set()
                del rcpt[key]
                with self.assertRaises(gate.MalformedReceiptException) as ctx:
                    gate.parse_receipt(rcpt)
                self.assertEqual(ctx.exception.reason_token, "RECEIPT_KEY_MISSING")
                self.assertIn(key, ctx.exception.details["missing"])
        for key in ("charges_enabled", "payouts_enabled"):
            rcpt = self._set(capabilities={"charges_enabled": True} if key == "payouts_enabled"
                             else {"payouts_enabled": True})
            with self.assertRaises(gate.MalformedReceiptException) as ctx:
                gate.parse_receipt(rcpt)
            self.assertEqual(ctx.exception.reason_token, "RECEIPT_CAPABILITY_KEY_MISSING")
            self.assertIn(key, ctx.exception.details["missing"])

    def test_an_unknown_key_is_refused_so_test_mode_cannot_be_smuggled_in(self):
        for extra_key in ("test_mode", "sandbox", "signature", "amount"):
            with self.subTest(extra=extra_key):
                rcpt = self._set()
                rcpt[extra_key] = True
                with self.assertRaises(gate.MalformedReceiptException) as ctx:
                    gate.parse_receipt(rcpt)
                self.assertEqual(ctx.exception.reason_token, "RECEIPT_KEY_UNKNOWN")
                self.assertIn(extra_key, ctx.exception.details["unknown"])

    def test_a_receipt_that_is_not_an_object_is_malformed(self):
        for bad in ([], "receipt", 7, None, True):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(gate.MalformedReceiptException) as ctx:
                    gate.parse_receipt(bad)
                self.assertEqual(ctx.exception.reason_token, "RECEIPT_NOT_AN_OBJECT")

    def test_a_payment_intent_we_have_never_read_back_is_not_evidence(self):
        with self.assertRaises(gate.MalformedReceiptException) as ctx:
            gate.parse_receipt(self._set(payment_intent="pi_00000000000000000000000000"))
        self.assertEqual(ctx.exception.reason_token, "RECEIPT_UNVERIFIED_PAYMENT_INTENT")
        known = gate.verified_payment_intent_ids()
        self.assertIn(EVIDENCE_PI, known)
        self.assertEqual(len(known), 1, "the evidence file records exactly one payment intent")

    def test_the_account_id_must_be_an_acct_identifier(self):
        for bad in ("not-an-account", "", "acct", 7, None, True):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(gate.MalformedReceiptException):
                    gate.parse_receipt(self._set(account_id=bad))

    def test_the_timestamp_is_parsed_not_pattern_matched(self):
        ok = gate.parse_receipt(self._set(timestamp="2026-10-06T16:23:12Z"))
        self.assertEqual(ok["timestamp_utc"], "2026-10-06T16:23:12Z")
        # an equally valid offset instant, and the explicit +00:00 form
        gate.parse_receipt(self._set(timestamp="2026-10-06T16:23:12+00:00"))
        for bad in ("2026-10-06T16:23:12",          # naive: no zone at all
                    "2026-10-06",                   # date only
                    "2026-10-06T16:23:12+02:00",    # aware, but not UTC
                    "2026-10-06T12:23:12-04:00",    # same instant, still not UTC
                    "2026-10-06T16:23:12Z ",
                    "yesterday", "", 1787637796, None, True):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(gate.MalformedReceiptException) as ctx:
                    gate.parse_receipt(self._set(timestamp=bad))
                self.assertIn(ctx.exception.reason_token,
                              ("RECEIPT_TIMESTAMP_NOT_UTC", "RECEIPT_TIMESTAMP_NOT_A_STRING",
                               "RECEIPT_TIMESTAMP_UNPARSEABLE"))


# ---------------------------------------------------------------------------
# Tier 3 - the fail-closed decision table, one test per outcome
# ---------------------------------------------------------------------------
class TestDecisionTable(GateTestCase):
    def test_malformed_writes_no_flag_and_no_chain_row(self):
        outcome = self.verify(receipt(livemode="true"))
        self.assertEqual(outcome["decision"], gate.DECISION_MALFORMED)
        self.assertEqual(outcome["state"], gate.STATE_MALFORMED)
        self.assert_refused_without_flag_write(outcome, "RECEIPT_TYPE_NOT_BOOLEAN")
        self.assertEqual(self.chain_rows(), [], "a malformed receipt is never recorded as evidence")
        self.assertEqual(outcome["flags_before"], outcome["flags_after"])
        # ...but the refusal itself is in the immutable transition log
        self.assertEqual(gate.transition_log(self.engine.store, self.CLIENT)[-1]["field"],
                         gate.DECISION_MALFORMED)
        with self.assertRaises(gate.MalformedReceiptException):
            gate.verify_milestone_b_receipt_strict(
                self.engine.store, receipt(livemode="true"), client_id=self.CLIENT,
                recorded_by="t", verification_source="s")

    def test_capability_disabled_locks_the_attempt_for_good(self):
        outcome = self.verify(receipt(charges=False, payouts=True), pin=gate.SANDBOX_PIN)
        self.assertEqual(outcome["decision"], gate.DECISION_CAPABILITY_DISABLED)
        self.assertEqual(outcome["reason_token"], "CAPABILITY_DISABLED_CHARGES_ENABLED")
        self.assertFalse(outcome["live_billing_verified"])
        self.assertEqual(gate.flag_value(self.engine.store, self.CLIENT,
                                        gate.MILESTONE_LIVE_BILLING), 0)
        self.assertTrue(outcome["permanently_locked"])
        # its own immutable row
        rows = self.chain_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "CAPABILITY_DISABLED")
        self.assertEqual(rows[0]["charges_enabled"], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.store.execute(
                "UPDATE billing_route_verifications SET decision = 'PASS' WHERE seq = ?",
                (rows[0]["seq"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.store.execute("DELETE FROM billing_route_verifications")
        self.assertEqual(len(self.chain_rows()), 1)

    def test_reopening_a_locked_attempt_needs_a_new_passing_event_and_a_reason(self):
        self.verify(receipt(charges=False), pin=gate.SANDBOX_PIN)
        # the same account, now reporting the capability enabled, but nobody said why
        refused = self.verify(receipt(charges=True), pin=gate.SANDBOX_PIN)
        self.assertEqual(refused["decision"], gate.DECISION_REOPEN_REFUSED)
        self.assertEqual(refused["reason_token"], "VERIFICATION_REOPEN_REASON_REQUIRED")
        self.assertEqual(len(self.chain_rows()), 1)
        # with a human reason that references what changed, it is a NEW verification event
        reopened = self.verify(receipt(charges=True), pin=gate.SANDBOX_PIN,
                               reopen_reason="Stripe enabled charges on this account on "
                                             "2026-10-07; the receipt was re-read after that")
        self.assertEqual(reopened["decision"], gate.DECISION_PASS_SANDBOX)
        rows = self.chain_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["decision"], "CAPABILITY_DISABLED")
        self.assertEqual(rows[1]["decision"], "PASS_SANDBOX_ONLY")
        self.assertIn("Stripe enabled charges", rows[1]["reopen_reason"])
        self.assertEqual(rows[0]["decision"], "CAPABILITY_DISABLED",
                         "the original locked row is never edited")

    def test_a_timeout_is_unknown_it_denies_and_changes_nothing(self):
        self.verify(receipt(), pin=gate.SANDBOX_PIN)   # non-default flags, so "unchanged" means something
        before = self.flags()
        self.assertEqual(before[gate.FLAG_SANDBOX_DEMONSTRATED], 1)

        def dead_adapter(payment_intent):
            raise gate.RecoverAITimeoutException("STUB_TIMEOUT", "the stub never answers")

        outcome = self.verify(receipt(), pin=gate.SANDBOX_PIN, fetch_adapter=dead_adapter)
        self.assertEqual(outcome["decision"], gate.DECISION_TIMEOUT_UNKNOWN)
        self.assertEqual(outcome["state"], gate.STATE_UNKNOWN)
        self.assertEqual(outcome["reason_token"], "RECOVERAI_TIMEOUT")
        self.assertTrue(outcome["flags_unchanged"])
        self.assertEqual(outcome["flags_before"], outcome["flags_after"])
        self.assertEqual(self.flags(), before, "UNKNOWN must not grant or revoke anything")
        self.assertEqual(len(self.chain_rows()), 1, "no new chain row from a timeout")
        with self.assertRaises(gate.RecoverAITimeoutException):
            gate.verify_milestone_b_receipt_strict(
                self.engine.store, receipt(), client_id=self.CLIENT, recorded_by="t",
                verification_source="s", pin=gate.SANDBOX_PIN, fetch_adapter=dead_adapter)

    def test_a_receipt_naming_another_account_is_a_security_refusal(self):
        pinned = "acct_1RealLivePinnedAccount0001"
        outcome = self.verify(receipt(account_id="acct_1SomeoneElseEntirely00002"), pin=pinned)
        self.assertEqual(outcome["decision"], gate.DECISION_ACCOUNT_MISMATCH)
        self.assertEqual(outcome["reason_token"], "PLATFORM_ACCOUNT_MISMATCH")
        self.assertEqual(outcome["exception_class"], "PlatformCredentialMismatchException")
        self.assert_refused_without_flag_write(outcome, "PLATFORM_ACCOUNT_MISMATCH")
        self.assertEqual(self.chain_rows(), [])
        alert = outcome["agent4_alert"]
        self.assertEqual(alert["priority"], "HIGH")
        self.assertEqual(alert["pinned_account_id_prefix"], pinned[:12])
        self.assertEqual(alert["received_account_sha256"],
                         gate.sha256_text("acct_1SomeoneElseEntirely00002"))
        # the received value is never echoed back anywhere in the record
        self.assertNotIn("acct_1SomeoneElseEntirely00002", json.dumps(outcome))
        with self.assertRaises(gate.PlatformCredentialMismatchException):
            gate.verify_milestone_b_receipt_strict(
                self.engine.store, receipt(account_id="acct_Other"), client_id=self.CLIENT,
                recorded_by="t", verification_source="s", pin=pinned)

    def test_while_the_account_is_unpinned_the_gate_refuses_and_records_nothing(self):
        with tempfile_json({}) as empty_config:
            self._assert_unpinned_refusal(empty_config)

    def _assert_unpinned_refusal(self, empty_config):
        """`env={}` plus an EMPTY register: no pin from any source.

        The shipped config/platform.json is whatever the owner's register says - today it
        holds a real pinned id - so the unpinned path is exercised against an explicit
        empty register instead of assuming the shipped file has no pin.
        """
        outcome = self.verify(receipt(), env={}, platform_config_file=empty_config)
        self.assertEqual(outcome["decision"], gate.DECISION_ACCOUNT_NOT_PINNED)
        self.assertEqual(outcome["reason_token"], "PLATFORM_ACCOUNT_NOT_PINNED")
        self.assertFalse(outcome["recorded"])
        self.assertTrue(outcome["gate_locked"])
        self.assertEqual(self.chain_rows(), [])
        self.assertEqual(gate.transition_log(self.engine.store, self.CLIENT), [],
                         "an unpinned refusal writes nothing at all")
        self.assertEqual(self.flags()[gate.MILESTONE_LIVE_BILLING], 0)
        with self.assertRaises(gate.GateLockedException):
            gate.verify_milestone_b_receipt_strict(
                self.engine.store, receipt(), client_id=self.CLIENT, recorded_by="t",
                verification_source="s", env={}, platform_config_file=empty_config)


# ---------------------------------------------------------------------------
# Tier 3 - the timeout path is reachable only through a stub adapter
# ---------------------------------------------------------------------------
class TestTimeoutReachability(GateTestCase):
    def test_the_gate_module_has_no_network_imports_at_all(self):
        """Checked on the parsed code, not on the prose: a docstring mentioning sockets is
        not a socket."""
        forbidden = {"socket", "requests", "urllib", "urllib2", "httpx", "http",
                     "ssl", "asyncio", "aiohttp", "ftplib", "smtplib", "telnetlib"}
        imported = set()
        for node in ast.walk(ast.parse(source_text())):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & forbidden, set(),
                         f"gate.py must stay storage-only: imports {sorted(imported & forbidden)}")

    def test_with_no_adapter_configured_no_network_call_can_happen_at_all(self):
        def explode(*args, **kwargs):
            raise AssertionError("the gate attempted a network call")

        with mock.patch.object(socket.socket, "connect", explode), \
                mock.patch("socket.create_connection", explode):
            outcome = self.verify(receipt(), pin=gate.SANDBOX_PIN)
        self.assertEqual(outcome["decision"], gate.DECISION_PASS_SANDBOX)
        self.assertEqual(outcome["state"], gate.STATE_SANDBOX_DEMONSTRATED)

    def test_no_passing_verdict_depends_on_a_fetch(self):
        """An adapter cannot grant anything: it can only confirm or refuse.

        It must answer with the full contract - native booleans for livemode,
        charges_enabled and payouts_enabled - because an answer we cannot read is treated
        as a refusal, not as agreement.
        """
        called = []

        def agreeing_adapter(payment_intent):
            called.append(payment_intent)
            return {"livemode": True, "charges_enabled": True, "payouts_enabled": True}

        with_adapter = self.verify(receipt(), pin=gate.SANDBOX_PIN,
                                   fetch_adapter=agreeing_adapter)
        self.assertEqual(called, [EVIDENCE_PI])
        self.assertEqual(with_adapter["decision"], gate.DECISION_PASS_SANDBOX)
        # an adapter that contradicts the receipt refuses, rather than granting
        contradicting = self.verify(receipt(), pin=gate.SANDBOX_PIN,
                                    fetch_adapter=lambda pi: {"livemode": False,
                                                              "charges_enabled": True,
                                                              "payouts_enabled": True})
        self.assertEqual(contradicting["decision"], gate.DECISION_CAPABILITY_DISABLED)
        self.assertEqual(contradicting["reason_token"], "INDEPENDENT_READ_NOT_LIVEMODE")

    def test_an_adapter_answer_we_cannot_read_is_a_refusal_not_a_confirmation(self):
        """Tier 3: `I cannot read the platform` must never pass as a good answer."""
        for description, answer in (
                ("None", None),
                ("an empty dict", {}),
                ("an unrelated dict", {"status": "succeeded"}),
                ("a list", []),
                ("a string", "ok"),
                ("a partial dict", {"livemode": True, "charges_enabled": True}),
                ("string booleans", {"livemode": "true", "charges_enabled": "true",
                                     "payouts_enabled": "true"}),
                ("integer booleans", {"livemode": 1, "charges_enabled": 1, "payouts_enabled": 1})):
            with self.subTest(answer=description):
                outcome = self.verify(receipt(), pin=gate.SANDBOX_PIN,
                                      fetch_adapter=lambda pi, a=answer: a)
                self.assertEqual(outcome["decision"], gate.DECISION_INDEPENDENT_READ_MALFORMED)
                self.assertEqual(outcome["reason_token"], "INDEPENDENT_READ_MALFORMED")
                self.assertTrue(outcome["gate_locked"])
                self.assertFalse(outcome["live_billing_verified"])
                self.assertEqual(self.flags()[gate.FLAG_SANDBOX_DEMONSTRATED], 0,
                                 "an unreadable answer must not even demonstrate the sandbox")
                self.assertEqual(self.chain_rows(), [])
        with self.assertRaises(gate.MalformedReceiptException):
            gate.verify_milestone_b_receipt_strict(
                self.engine.store, receipt(), client_id=self.CLIENT, recorded_by="t",
                verification_source="s", pin=gate.SANDBOX_PIN,
                fetch_adapter=lambda pi: None)


# ---------------------------------------------------------------------------
# Tier 3 - the account-id pin
# ---------------------------------------------------------------------------
class TestAccountPin(GateTestCase):
    def test_the_pin_resolves_from_env_then_config_then_the_sentinel(self):
        with mock.patch.dict(os.environ, {gate.ENV_PLATFORM_ACCOUNT_ID: "acct_from_env"}, clear=False):
            self.assertEqual(gate.resolve_platform_account_id(), "acct_from_env")
        # No pin from ANY source resolves to the sentinel. The shipped register is not
        # assumed to be empty (it holds a real pin today), so this is checked against an
        # explicit empty file as well as a missing one.
        with tempfile_json({}) as empty:
            self.assertEqual(gate.resolve_platform_account_id(env={}, config_path=empty),
                             gate.UNPINNED)
        shipped = gate.resolve_platform_account_id(env={})
        self.assertTrue(shipped == gate.UNPINNED or shipped.startswith("acct_"),
                        f"the shipped register resolves to a pin or to the sentinel, not "
                        f"to {shipped!r}")
        with tempfile_json({"platform_account_id": "acct_from_config"}) as path:
            self.assertEqual(gate.resolve_platform_account_id(env={}, config_path=path),
                             "acct_from_config")
        self.assertEqual(gate.resolve_platform_account_id(env={}, config_path="/nonexistent/x.json"),
                         gate.UNPINNED)
        self.assertNotIn("acct_", gate.UNPINNED, "the sentinel is never an invented account id")

    def test_a_pinned_value_is_never_normalised(self):
        with mock.patch.dict(os.environ, {gate.ENV_PLATFORM_ACCOUNT_ID: " acct_padded "}, clear=False):
            self.assertEqual(gate.resolve_platform_account_id(), " acct_padded ")

    def test_one_byte_of_deviation_is_a_mismatch(self):
        pinned = "acct_1PinnedByteExact00000001"
        deviations = {
            "trailing space": pinned + " ",
            "one char swapped": pinned[:-1] + "2",
            "one char dropped": pinned[:-1],
            "one char added": pinned + "0",
            "case change in the body": "acct_1" + pinned[6:].upper(),
            "unicode lookalike": pinned.replace("0", "\u043e"),   # Cyrillic o, not ASCII 0
        }
        for label, received in deviations.items():
            with self.subTest(deviation=label):
                outcome = self.verify(receipt(account_id=received), pin=pinned)
                self.assertEqual(outcome["decision"], gate.DECISION_ACCOUNT_MISMATCH,
                                 f"{label} must be a mismatch")
                self.assertEqual(outcome["received_account_sha256"], gate.sha256_text(received))
                self.assertNotIn(received, json.dumps(outcome))

    def test_a_value_that_is_not_even_an_account_id_is_refused_rather_than_normalised(self):
        pinned = "acct_1PinnedByteExact00000001"
        for label, received in {"leading space": " " + pinned,
                                "whole-string uppercase": pinned.upper(),
                                "trimmed only by stripping": "  " + pinned}.items():
            with self.subTest(deviation=label):
                outcome = self.verify(receipt(account_id=received), pin=pinned)
                self.assertEqual(outcome["decision"], gate.DECISION_MALFORMED)
                self.assertNotEqual(outcome["decision"], gate.DECISION_PASS)
                self.assertEqual(outcome["reason_token"],
                                 "RECEIPT_ACCOUNT_ID_NOT_AN_ACCOUNT_ID")
                self.assertEqual(gate.flag_value(self.engine.store, self.CLIENT,
                                                 gate.MILESTONE_LIVE_BILLING), 0)

    def test_the_exact_pinned_value_passes(self):
        pinned = "acct_1PinnedByteExact00000001"
        outcome = self.verify(receipt(account_id=pinned), pin=pinned)
        self.assertEqual(outcome["decision"], gate.DECISION_PASS)
        self.assertEqual(outcome["receipt_validated_against"], pinned)


# ---------------------------------------------------------------------------
# Tier 3 - two pin registers: the sandbox pin can never satisfy the live milestone
# ---------------------------------------------------------------------------
class TestSandboxPinCannotSatisfyTheLiveMilestone(GateTestCase):
    def test_a_sandbox_pass_sets_only_the_sandbox_flag(self):
        outcome = self.verify(receipt(), pin=gate.SANDBOX_PIN,
                              verification_source="sandbox pin (NOT our live Stripe account)")
        self.assertEqual(outcome["decision"], gate.DECISION_PASS_SANDBOX)
        self.assertTrue(outcome["sandbox_only"])
        self.assertFalse(outcome["live_billing_verified"])
        flags = self.flags()
        self.assertEqual(flags[gate.MILESTONE_LIVE_BILLING], 0)
        self.assertEqual(flags[gate.FLAG_SANDBOX_DEMONSTRATED], 1)
        self.assertFalse(gate.verified_live_billing(self.engine.store, self.CLIENT))
        self.assertEqual(self.chain_rows()[0]["decision"], "PASS_SANDBOX_ONLY")
        self.assertEqual(self.chain_rows()[0]["sandbox_only"], 1)

    def test_even_a_hand_set_flag_backed_by_a_sandbox_receipt_cannot_satisfy_it(self):
        """The launch condition never reads the column: it re-derives the binding."""
        self.verify(receipt(), pin=gate.SANDBOX_PIN)
        row = self.engine.store.query_one(
            "SELECT live_billing_receipt_metadata FROM client_autonomy_settings WHERE client_id = ?",
            (self.CLIENT,))
        self.assertIsNone(row["live_billing_receipt_metadata"],
                          "a sandbox pass binds nothing")
        # an operator with DB access forces the flag AND binds the sandbox receipt by hand
        chain = self.chain_rows()[0]
        document = {"receipt": json.loads(chain["receipt_json"]),
                    "receipt_sha256": chain["receipt_sha256"],
                    "verification_id": chain["verification_id"],
                    "chain_hash": chain["chain_hash"]}
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                "UPDATE client_autonomy_settings SET live_billing_verified = 1, "
                "live_billing_receipt_metadata = ? WHERE client_id = ?",
                (gate.canonical_json(document), self.CLIENT))
        self.assertEqual(gate.flag_value(self.engine.store, self.CLIENT,
                                         gate.MILESTONE_LIVE_BILLING), 1)
        # ...and the launch condition still says no, because the binding is not a live PASS
        self.assertFalse(gate.verified_live_billing(self.engine.store, self.CLIENT))
        state = gate.launch_condition(self.engine.store, self.CLIENT)
        self.assertIn(gate.MILESTONE_LIVE_BILLING, state["outstanding_milestones"])

    def test_a_sandbox_pin_configured_as_the_platform_pin_cannot_satisfy_it_either(self):
        with tempfile_json({"platform_account_id": gate.SANDBOX_PIN}) as path:
            outcome = self.verify(receipt(), env={}, platform_config_file=path)
            self.assertEqual(outcome["decision"], gate.DECISION_PASS_SANDBOX)
            self.assertFalse(gate.verified_live_billing(self.engine.store, self.CLIENT,
                                                        env={}, config_path=path))


# ---------------------------------------------------------------------------
# Tier 3 - immutability: the bound receipt and the chain
# ---------------------------------------------------------------------------
class TestImmutability(GateTestCase):
    def _bind_something(self):
        pinned = "acct_1PinnedByteExact00000001"
        outcome = self.verify(receipt(account_id=pinned), pin=pinned)
        self.assertEqual(outcome["decision"], gate.DECISION_PASS)
        return outcome

    def test_the_first_binding_succeeds_and_is_kept_byte_identical(self):
        outcome = self._bind_something()
        row = self.engine.store.query_one(
            "SELECT * FROM client_autonomy_settings WHERE client_id = ?", (self.CLIENT,))
        self.assertEqual(row["live_billing_verified"], 1)
        self.assertIsNotNone(row["live_billing_receipt_metadata"])
        document = json.loads(row["live_billing_receipt_metadata"])
        self.assertEqual(document["receipt_sha256"], outcome["receipt_sha256"])
        self.assertEqual(gate.sha256_canonical(document["receipt"]), document["receipt_sha256"])
        bound = gate.read_bound_receipt(self.engine.store, self.CLIENT)
        self.assertTrue(bound["bound"])
        self.assertEqual(bound["verification_id"], outcome["verification_id"])

    def test_a_later_change_to_the_bound_receipt_is_refused(self):
        outcome = self._bind_something()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE client_autonomy_settings SET live_billing_receipt_metadata = ? "
                    "WHERE client_id = ?", (gate.canonical_json({"receipt": {"evil": True}}),
                                            self.CLIENT))
        self.assertIn("bound once", str(ctx.exception))
        # rewriting the same value is still a second write, so it is refused too
        row = self.engine.store.query_one(
            "SELECT live_billing_receipt_metadata FROM client_autonomy_settings WHERE client_id = ?",
            (self.CLIENT,))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE client_autonomy_settings SET live_billing_receipt_metadata = ? "
                    "WHERE client_id = ?", (row["live_billing_receipt_metadata"], self.CLIENT))
        self.assertEqual(gate.read_bound_receipt(self.engine.store, self.CLIENT)["receipt_sha256"],
                         outcome["receipt_sha256"])

    def test_clearing_the_bound_receipt_is_refused(self):
        self._bind_something()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    "UPDATE client_autonomy_settings SET live_billing_receipt_metadata = NULL "
                    "WHERE client_id = ?", (self.CLIENT,))
        self.assertIn("bound once", str(ctx.exception))

    def test_a_normal_mode_toggle_still_succeeds_with_the_column_populated(self):
        outcome = self._bind_something()
        before = self.engine.store.query_one(
            "SELECT live_billing_receipt_metadata FROM client_autonomy_settings WHERE client_id = ?",
            (self.CLIENT,))["live_billing_receipt_metadata"]
        self.add_debtor("D-GATE", client_id=self.CLIENT, aging_state="60")
        self.sign_off(client_id=self.CLIENT)
        flipped = self.engine.switch_mode(self.CLIENT, "full_auto", "owner", "signed")
        self.assertTrue(flipped["changed"])
        flipped_back = self.engine.switch_mode(self.CLIENT, "hitl", "owner", "halt")
        self.assertTrue(flipped_back["changed"])
        after = self.engine.store.query_one(
            "SELECT * FROM client_autonomy_settings WHERE client_id = ?", (self.CLIENT,))
        self.assertEqual(after["live_billing_receipt_metadata"], before)
        self.assertEqual(after["live_billing_verified"], 1)
        self.assertEqual(gate.read_bound_receipt(self.engine.store, self.CLIENT)["chain_hash"],
                         outcome["chain_hash"])

    def test_the_chain_verifies_and_detects_an_operator_edit(self):
        self._bind_something()
        self.verify(receipt(), pin=gate.SANDBOX_PIN)
        report = gate.verify_chain(self.engine.store)
        self.assertEqual(report["rows"], 2)
        self.assertTrue(report["chain_verified"])
        # an operator with DB access, with the triggers temporarily dropped
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute("DROP TRIGGER billing_route_verifications_no_update")
            self.engine.store.execute(
                "UPDATE billing_route_verifications SET receipt_json = ? WHERE seq = 1",
                (gate.canonical_json(receipt(charges=True, account_id="acct_Tampered000000000000000")),))
        with self.assertRaises(gate.PlatformCredentialMismatchException) as ctx:
            gate.verify_chain(self.engine.store)
        self.assertEqual(ctx.exception.reason_token, "RECEIPT_PAYLOAD_TAMPERED")
        with self.assertRaises(gate.PlatformCredentialMismatchException):
            gate.read_bound_receipt(self.engine.store, self.CLIENT)
        self.engine.store.init_schema()          # put the trigger back
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.store.execute(
                "UPDATE billing_route_verifications SET chain_hash = 'x' WHERE seq = 1")

    def test_a_recomputed_chain_still_fails_its_own_link_check(self):
        self._bind_something()
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute("DROP TRIGGER billing_route_verifications_no_update")
            self.engine.store.execute(
                "UPDATE billing_route_verifications SET prev_chain_hash = 'not-the-genesis' "
                "WHERE seq = 1")
        with self.assertRaises(gate.PlatformCredentialMismatchException) as ctx:
            gate.verify_chain(self.engine.store)
        self.assertEqual(ctx.exception.reason_token, "CHAIN_LINK_BROKEN")
        self.engine.store.init_schema()


# ---------------------------------------------------------------------------
# Tier 3 - the live milestone is structurally impossible without a real receipt
# ---------------------------------------------------------------------------
class TestLiveMilestoneIsStructurallyBacked(GateTestCase):
    def test_a_hardcoded_literal_is_not_a_receipt(self):
        outcome = self.verify(True)
        self.assertEqual(outcome["decision"], gate.DECISION_MALFORMED)
        self.assert_refused_without_flag_write(outcome)
        self.assertFalse(gate.verified_live_billing(self.engine.store, self.CLIENT))

    def test_an_in_memory_dict_off_the_evidence_file_is_not_evidence(self):
        invented = receipt(payment_intent="pi_invented_in_memory_0000000000")
        outcome = self.verify(invented, pin="acct_1PinnedByteExact00000001")
        self.assertEqual(outcome["decision"], gate.DECISION_MALFORMED)
        self.assertEqual(outcome["reason_token"], "RECEIPT_UNVERIFIED_PAYMENT_INTENT")
        self.assertEqual(self.chain_rows(), [])
        self.assertFalse(gate.verified_live_billing(self.engine.store, self.CLIENT))

    def test_a_sandbox_receipt_can_never_set_the_live_flag(self):
        self.verify(receipt(), pin=gate.SANDBOX_PIN)
        self.assertEqual(gate.flag_value(self.engine.store, self.CLIENT,
                                         gate.MILESTONE_LIVE_BILLING), 0)

    def test_the_flag_alone_never_satisfies_the_launch_condition(self):
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                "UPDATE client_autonomy_settings SET live_billing_verified = 1 WHERE client_id = ?",
                (self.CLIENT,))
        self.assertFalse(gate.verified_live_billing(self.engine.store, self.CLIENT))
        state = gate.launch_condition(self.engine.store, self.CLIENT)
        self.assertIn(gate.MILESTONE_LIVE_BILLING, state["outstanding_milestones"])

    def test_a_real_pin_with_a_really_recorded_receipt_is_the_only_way_through(self):
        """The one honest PASS: pinned id supplied by the environment, the receipt names it
        byte-for-byte, and the verification source names the provider read that produced it."""
        pinned = "acct_1PinnedByteExact00000001"
        with mock.patch.dict(os.environ, {gate.ENV_PLATFORM_ACCOUNT_ID: pinned}):
            outcome = self.verify(receipt(account_id=pinned),
                                  verification_source="GET /v1/accounts/<pinned> (read back)")
            self.assertTrue(gate.verified_live_billing(self.engine.store, self.CLIENT))
            self.assertEqual(outcome["decision"], gate.DECISION_PASS)
            self.assertTrue(outcome["receipt_bound"])
            self.assertEqual(gate.lifecycle_status(self.engine.store, self.CLIENT),
                             gate.STATUS_BETA_TESTER,
                             "one milestone is not enough to leave beta")
            state = gate.launch_condition(self.engine.store, self.CLIENT)
            self.assertEqual(set(state["outstanding_milestones"]),
                             {gate.MILESTONE_MANUAL_REVIEW, gate.MILESTONE_COMMERCIAL_MSA})

    def test_a_second_live_receipt_that_diverges_is_flagged_for_a_human(self):
        pinned = "acct_1PinnedByteExact00000001"
        with mock.patch.dict(os.environ, {gate.ENV_PLATFORM_ACCOUNT_ID: pinned}):
            self.verify(receipt(account_id=pinned))
            second = receipt(account_id=pinned, timestamp="2026-10-07T09:00:00Z")
            outcome = self.verify(second, verification_source="a second read")
            self.assertTrue(outcome["rebind_required"],
                            "the bound entry is write-once, so a divergence is surfaced")
            self.assertFalse(outcome["receipt_bound"])
            self.assertEqual(len(self.chain_rows()), 2)
            self.assertTrue(gate.verified_live_billing(self.engine.store, self.CLIENT))


# ---------------------------------------------------------------------------
# Tier 4 - the absolute code gate
# ---------------------------------------------------------------------------
class TestLaunchGate(GateTestCase):
    def test_the_launcher_refuses_while_any_single_flag_is_false(self):
        for flags in ({}, {gate.MILESTONE_MANUAL_REVIEW},
                      {gate.MILESTONE_MANUAL_REVIEW, gate.MILESTONE_LIVE_BILLING},
                      {gate.MILESTONE_COMMERCIAL_MSA, gate.MILESTONE_LIVE_BILLING},
                      {gate.MILESTONE_MANUAL_REVIEW, gate.MILESTONE_COMMERCIAL_MSA}):
            with self.subTest(flags=sorted(flags)):
                with self.engine.store.tx(immediate=True):
                    self.engine.store.execute(
                        """UPDATE client_autonomy_settings SET manual_review_threshold_passed = ?,
                             live_billing_verified = ?, commercial_msa_executed = ?
                           WHERE client_id = ?""",
                        (1 if gate.MILESTONE_MANUAL_REVIEW in flags else 0,
                         1 if gate.MILESTONE_LIVE_BILLING in flags else 0,
                         1 if gate.MILESTONE_COMMERCIAL_MSA in flags else 0, self.CLIENT))
                with self.assertRaises(gate.GateLockedException) as ctx:
                    gate.launch_live_outbound(self.engine.store, self.CLIENT, "voice", "o", "go")
                self.assertEqual(ctx.exception.reason_token, "GATE_LOCKED_LIVE_OUTBOUND")
                self.assertTrue(ctx.exception.outstanding)
                self.assertTrue(set(ctx.exception.outstanding) <= set(gate.MILESTONES))

    def test_the_launcher_refuses_for_every_client_right_now(self):
        for client_id in ("CLI-GATE", "CLI-NORTHWIND", "CLI-CEDAR", "CLI-BRAND-NEW"):
            with self.subTest(client=client_id):
                with self.assertRaises(gate.GateLockedException):
                    gate.launch_live_outbound(self.engine.store, client_id, "voice", "owner", "go")

    def test_with_every_milestone_true_it_still_refuses_for_want_of_infrastructure(self):
        """The only way to reach this token: a really pinned, really bound live receipt
        plus the other two flags. Even then the launcher refuses, because there is no live
        sending infrastructure to launch into."""
        with self.pin_environment():
            self.meet_every_milestone()
            self.assertEqual(gate.launch_condition(self.engine.store, self.CLIENT)["all_true"],
                             True)
            with self.assertRaises(gate.GateLockedException) as ctx:
                gate.launch_live_outbound(self.engine.store, self.CLIENT, "voice", "owner", "go")
        self.assertEqual(ctx.exception.reason_token, "LIVE_SENDING_NOT_IMPLEMENTED")
        self.assertEqual(ctx.exception.outstanding, [])

    def test_locking_the_live_launcher_does_not_lock_the_sandbox_send_path(self):
        self.add_debtor("D-SANDBOX", client_id=self.CLIENT, aging_state="60",
                        approval_status="Approved")
        with self.assertRaises(gate.GateLockedException):
            self.engine.launch_live_outbound(self.CLIENT, "voice", "owner", "go")
        outcome = self.engine.send("D-SANDBOX", "voice", dispatch=False)
        self.assertEqual(outcome["decision"], CLAIMED, "the sandbox path must keep working")
        self.assertEqual(outcome["status"], "CLAIMED")

    def test_the_refusal_is_recorded_as_a_gate_event_with_the_milestones(self):
        with self.assertRaises(gate.GateLockedException):
            gate.launch_live_outbound(self.engine.store, self.CLIENT, "voice", "owner", "go")
        row = gate.transition_log(self.engine.store, self.CLIENT)[-1]
        self.assertEqual(row["event"], "gate_event")
        self.assertEqual(row["field"], "live_outbound_launch")
        parameters = json.loads(row["validating_parameters"])
        self.assertEqual(set(parameters["outstanding_milestones"]), set(gate.MILESTONES))
        self.assertEqual(row["executing_administrator"], "owner")


# ---------------------------------------------------------------------------
# Tier 4 - Agent 4 reporting: structured records and the plain-English tooltip
# ---------------------------------------------------------------------------
class TestAgent4Reporting(GateTestCase):
    def test_the_gate_module_never_prints_and_has_no_catch_all_handler(self):
        """A structured record is the only output path, and an error path that swallows
        everything is exactly how a refusal becomes a pass. Checked on the AST, so prose
        in a docstring cannot make the test lie either way."""
        tree = ast.parse(source_text())
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "print"]
        self.assertEqual(calls, [], "the gate reports through records, never print()")
        catch_alls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    catch_alls.append("bare except")
                elif isinstance(node.type, ast.Name) and node.type.id in ("Exception",
                                                                          "BaseException"):
                    catch_alls.append(f"except {node.type.id}")
        self.assertEqual(catch_alls, [], "no catch-all handler may exist on this path")

    def test_the_tooltip_names_exactly_which_milestones_are_outstanding(self):
        tooltip = gate.milestone_tooltip(self.engine.store, self.CLIENT)
        self.assertEqual(tooltip["outstanding_count"], 3)
        self.assertEqual(set(tooltip["outstanding_milestones"]), set(gate.MILESTONES))
        self.assertFalse(tooltip["can_go_live"])
        self.assertTrue(tooltip["text"].startswith(f"{self.CLIENT} cannot go live yet"))
        for key in gate.MILESTONES:
            entry = next(m for m in tooltip["milestones"] if m["key"] == key)
            self.assertFalse(entry["met"])
            self.assertIn(entry["plain_english"], tooltip["text"])
        self.assertIn(gate.MILESTONE_PLAIN_ENGLISH[gate.MILESTONE_LIVE_BILLING],
                      tooltip["text"])
        self.assertIn("Master Services Agreement", tooltip["text"])
        # Register-agnostic on purpose: the honest label must read true whichever way the
        # register resolves, so assert the RELATIONSHIP rather than one frozen sentence.
        live_label = next(label for label in tooltip["honest_labels"]
                          if "live billing milestone" in label)
        pinned = gate.resolve_platform_account_id()
        if pinned == gate.UNPINNED:
            self.assertIn("no platform account id is pinned", tooltip["text"])
            self.assertIn("no platform account id is pinned", live_label)
            self.assertNotIn("acct_", live_label)
        else:
            self.assertIn(pinned, live_label)
            self.assertIn("UNRECORDED", live_label)
            self.assertNotIn("no platform account id is pinned", live_label)
        self.assertIn("$0 collected to date", tooltip["honest_labels"])

    def test_a_bare_flag_flip_does_not_satisfy_the_review_milestone(self):
        gate.set_milestone_flag(self.engine.store, self.CLIENT, gate.MILESTONE_MANUAL_REVIEW,
                                True, "owner", "just set it")
        tooltip = gate.milestone_tooltip(self.engine.store, self.CLIENT)
        self.assertIn(gate.MILESTONE_MANUAL_REVIEW, tooltip["outstanding_milestones"],
                      "the milestone is derived from recorded reviews, not from the column")
        self.assertFalse(tooltip["can_go_live"])

    def test_the_tooltip_shrinks_as_milestones_are_met(self):
        for index in range(gate.MANUAL_REVIEW_THRESHOLD):
            gate.record_manual_review_decision(self.engine.store, self.CLIENT, f"D-{index}",
                                               "approved", "reviewer", "hitl")
        gate.refresh_manual_review_milestone(self.engine.store, self.CLIENT, "owner",
                                            "twenty reviews recorded")
        tooltip = gate.milestone_tooltip(self.engine.store, self.CLIENT)
        self.assertEqual(tooltip["outstanding_milestones"],
                         [gate.MILESTONE_LIVE_BILLING, gate.MILESTONE_COMMERCIAL_MSA])
        self.assertNotIn(gate.MILESTONE_MANUAL_REVIEW, tooltip["text"])

    def test_agent_4_alert_for_every_failure_state_and_its_priority(self):
        cases = [
            (self.verify(receipt(livemode=1)), "NORMAL", "GATE_MALFORMED"),
            (self.verify(receipt(payouts=False), pin=gate.SANDBOX_PIN), "HIGH",
             "GATE_CAPABILITY_DISABLED"),
            (self.verify(receipt(livemode=False), pin=gate.SANDBOX_PIN), "HIGH",
             "GATE_CAPABILITY_DISABLED"),
            (self.verify(receipt(account_id="acct_Elsewhere000000000000000"), pin="acct_Pinned000"),
             "HIGH", "GATE_PLATFORM_ACCOUNT_MISMATCH"),
            (self.verify(receipt(), pin=gate.SANDBOX_PIN, fetch_adapter=lambda pi: None),
             "NORMAL", "GATE_INDEPENDENT_READ_MALFORMED"),
        ]
        for outcome, priority, kind in cases:
            with self.subTest(kind=kind):
                alert = gate.agent4_alert(outcome, client_id=self.CLIENT)
                self.assertEqual(alert["priority"], priority)
                self.assertEqual(alert["kind"], kind)
                self.assertTrue(alert["plain_english"])
                self.assertTrue(alert["alert_id"].startswith("alert-"))
                self.assertRegex(alert["raised_at"], r"\.\d{3}Z$")

    def test_an_alert_reaches_the_guardrail_action_log_through_the_facade(self):
        outcome = self.engine.verify_milestone_b_receipt(
            receipt(account_id="acct_Elsewhere000000000000000"), client_id=self.CLIENT,
            recorded_by="owner", verification_source="read-back", pin="acct_Pinned000000")
        self.assertEqual(outcome["decision"], gate.DECISION_ACCOUNT_MISMATCH)
        alerts = [e for e in self.engine.audit.entries if e["action"] == "agent4_alert"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["priority"], "HIGH")
        self.assertEqual(alerts[0]["reason_token"], "PLATFORM_ACCOUNT_MISMATCH")

    def test_the_launcher_refusal_reaches_the_audit_log_and_carries_the_milestones(self):
        with self.assertRaises(gate.GateLockedException):
            self.engine.launch_live_outbound(self.CLIENT, "voice", "owner", "go")
        entries = [e for e in self.engine.audit.entries if e["action"] == "gate_locked"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0]["outstanding_milestones"]), set(gate.MILESTONES))


# ---------------------------------------------------------------------------
# Tier 1 - the manual review milestone
# ---------------------------------------------------------------------------
class TestTheHonestLiveBillingLabel(GateTestCase):
    """The label the public demo prints must read TRUE in every register state.

    Its previous version was a fixed string - "no platform account id is pinned, so the live
    billing milestone cannot be verified" - which the demo kept printing after the register
    was pinned with an API-verified account id, i.e. it printed a false statement in public.
    A hardcoded label goes false the moment the register changes, in either direction, so
    each direction is exercised here directly rather than assumed.
    """

    def label(self, **kwargs) -> str:
        return gate.live_billing_honest_label(self.engine.store, self.CLIENT, **kwargs)

    def test_the_shipped_register_names_the_pin_and_says_the_milestone_is_unrecorded(self):
        pinned = gate.resolve_platform_account_id(env={})
        if pinned == gate.UNPINNED:
            self.skipTest("this checkout ships no pin; the unpinned direction is tested below")
        label = self.label(env={})
        self.assertIn(pinned, label)
        self.assertIn("UNRECORDED", label)
        self.assertNotIn("no platform account id is pinned", label)
        self.assertFalse(gate.verified_live_billing(self.engine.store, self.CLIENT, env={}),
                         "the label says UNRECORDED, so the milestone must really be unmet")

    def test_with_no_register_at_all_it_says_unpinned_and_names_no_account(self):
        label = self.label(env={}, config_path=self.tmp / "no_such_register.json")
        self.assertIn("no platform account id is pinned", label)
        self.assertNotIn("acct_", label)

    def test_the_sandbox_register_is_never_described_as_an_unpinned_one(self):
        path = self.tmp / "sandbox_register.json"
        path.write_text(json.dumps({"platform_account_id": gate.SANDBOX_PIN}),
                        encoding="utf-8")
        label = self.label(env={}, config_path=path)
        self.assertIn("sandbox", label)
        self.assertNotIn("no platform account id is pinned", label)

    def test_an_env_pin_is_named_even_though_it_is_in_no_file(self):
        label = self.label(env={gate.ENV_PLATFORM_ACCOUNT_ID: "acct_put_here_by_the_env"})
        self.assertIn("acct_put_here_by_the_env", label)
        self.assertNotIn("no platform account id is pinned", label)

    def test_a_bare_register_produces_no_invented_figures(self):
        """A register holding only an id has told us nothing about the account, so the label
        must not quote a charge count, a balance or a business name it was never given."""
        path = self.tmp / "bare_register.json"
        path.write_text(json.dumps({"platform_account_id": "acct_bare_pin_only"}),
                        encoding="utf-8")
        label = self.label(env={}, config_path=path)
        self.assertIn("acct_bare_pin_only", label)
        for invented in ("0 charges", "Profit Recovery Partners", "$0 available"):
            self.assertNotIn(invented, label)

    def test_it_quotes_only_what_the_register_records(self):
        facts = gate.platform_register_facts()
        if not facts:
            self.skipTest("this checkout ships a register with no recorded facts")
        label = self.label(env={})
        for value in facts.values():
            self.assertIn(value, label)

    def test_the_tooltip_carries_that_derived_label_rather_than_a_fixed_string(self):
        expected = gate.live_billing_honest_label(self.engine.store, self.CLIENT, env={})
        self.assertIn(expected,
                      gate.milestone_tooltip(self.engine.store, self.CLIENT, env={})["honest_labels"])


class TestManualReviewMilestone(GateTestCase):
    def _review(self, index, decision="approved"):
        debtor_id = f"D-REV-{index}"
        self.add_debtor(debtor_id, client_id=self.CLIENT, aging_state="60")
        if decision == "approved":
            self.engine.approve(debtor_id, f"reviewer-{index}")
        else:
            self.engine.reject(debtor_id, f"reviewer-{index}", "not ready")
        return debtor_id

    def test_nineteen_reviews_are_not_enough_and_twenty_are(self):
        for index in range(19):
            self._review(index)
        counts = gate.manual_review_counts(self.engine.store, self.CLIENT)
        self.assertEqual(counts["manual_hitl_reviews"], 19)
        self.assertFalse(counts["met"])
        self._review(19)
        counts = gate.manual_review_counts(self.engine.store, self.CLIENT)
        self.assertEqual(counts["manual_hitl_reviews"], 20)
        self.assertTrue(counts["met"])
        refreshed = gate.refresh_manual_review_milestone(self.engine.store, self.CLIENT,
                                                        "owner", "twenty reviews recorded")
        self.assertTrue(refreshed["changed"])
        self.assertEqual(gate.flag_value(self.engine.store, self.CLIENT,
                                         gate.MILESTONE_MANUAL_REVIEW), 1)

    def test_a_guardrail_block_neither_adds_an_infraction_nor_reduces_the_count(self):
        for index in range(20):
            self._review(index)
        before = gate.manual_review_counts(self.engine.store, self.CLIENT)
        # a send the calling-window guardrail correctly blocks, in hitl
        self.add_debtor("D-WINDOW", client_id=self.CLIENT, phone="(808) 555-0119",
                        aging_state="60", approval_status="Approved")
        blocked = self.engine.send("D-WINDOW", "voice", dispatch=False)
        self.assertEqual(blocked["decision"], "BLOCKED_OUTSIDE_WINDOW")
        after = gate.manual_review_counts(self.engine.store, self.CLIENT)
        self.assertEqual(after["client_caused_exceptions"], 0)
        self.assertEqual(after["manual_hitl_reviews"], before["manual_hitl_reviews"])
        self.assertEqual(after["guardrail_blocks"], before["guardrail_blocks"] + 1)
        self.assertTrue(after["met"], "a guardrail doing its job must not unmake the milestone")
        rows = [r for r in self.engine.manual_review_decisions(self.CLIENT)
                if r["decision"] == "guardrail_block"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["client_caused_exception"], 0)
        # the store refuses the dishonest version of the same row
        with self.assertRaises(sqlite3.IntegrityError):
            with self.engine.store.tx(immediate=True):
                self.engine.store.execute(
                    """INSERT INTO manual_review_decisions
                         (client_id, debtor_id, decision, decided_by, decided_at,
                          operating_mode_at_decision, client_caused_exception)
                       VALUES (?,?,?,?,?,?,1)""",
                    (self.CLIENT, "D-WINDOW", "guardrail_block", "x", clock.iso_ms(), "hitl"))
        with self.assertRaises(gate.GateException):
            gate.record_manual_review_decision(self.engine.store, self.CLIENT, "D-WINDOW",
                                               "guardrail_block", "x", "hitl",
                                               client_caused_exception=1)

    def test_one_client_caused_exception_unmakes_the_milestone(self):
        for index in range(20):
            self._review(index)
        gate.record_manual_review_decision(self.engine.store, self.CLIENT, "D-REV-0",
                                           "approved", "reviewer-0", "hitl",
                                           reason="approved outside the authorized scope",
                                           client_caused_exception=1)
        counts = gate.manual_review_counts(self.engine.store, self.CLIENT)
        self.assertEqual(counts["client_caused_exceptions"], 1)
        self.assertFalse(counts["met"])
        refreshed = gate.refresh_manual_review_milestone(self.engine.store, self.CLIENT,
                                                        "owner", "recount after an exception")
        self.assertEqual(gate.flag_value(self.engine.store, self.CLIENT,
                                         gate.MILESTONE_MANUAL_REVIEW), 0)
        self.assertFalse(refreshed["counts"]["met"])

    def test_full_auto_decisions_do_not_count_towards_the_milestone(self):
        self.sign_off(client_id=self.CLIENT)
        self.engine.switch_mode(self.CLIENT, "full_auto", "owner", "signed")
        self._review(0)
        counts = gate.manual_review_counts(self.engine.store, self.CLIENT)
        self.assertEqual(counts["manual_hitl_reviews"], 0,
                         "the milestone counts manual hitl reviews, not full_auto activity")


# ---------------------------------------------------------------------------
# Tier 5 defect 1 - hitl attribution
# ---------------------------------------------------------------------------
class TestHitlAttribution(GateTestCase):
    def test_a_signed_client_in_hitl_attributes_the_contact_to_the_human_approver(self):
        self.add_debtor("D-HITL", client_id=self.CLIENT, aging_state="60")
        signoff = self.sign_off(client_id=self.CLIENT)     # a signature EXISTS...
        self.engine.approve("D-HITL", "dana@client.example")  # ...but the mode is hitl
        self.assertEqual(self.engine.settings(self.CLIENT)["operating_mode"], "hitl")
        outcome = self.engine.send("D-HITL", "voice", dispatch=False)
        self.assertEqual(outcome["decision"], CLAIMED)
        row = self.claim_rows("D-HITL")[0]
        self.assertEqual(row["operating_mode_at_claim"], "hitl")
        self.assertEqual(row["approved_by"], "dana@client.example")
        self.assertRegex(row["approved_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual(row["signature_reference"],
                         f"human-approval:dana@client.example@{row['approved_at']}")
        self.assertNotEqual(row["signature_reference"], signoff["signature_reference"],
                            "a human-approved contact must not be stamped as the automation "
                            "sign-off just because a sign-off exists on the row")
        self.assertEqual(outcome["payload"]["authorization"]["source"], "human_approval_in_hitl")
        self.assertEqual(outcome["payload"]["authorization"]["authorized_by"],
                         "dana@client.example")

    def test_full_auto_with_a_valid_signoff_attributes_the_automation_signoff(self):
        self.add_debtor("D-AUTO", client_id=self.CLIENT, aging_state="60")
        signoff = self.sign_off(client_id=self.CLIENT)
        self.engine.switch_mode(self.CLIENT, "full_auto", "owner", "signed")
        outcome = self.engine.send("D-AUTO", "voice", dispatch=False)
        self.assertEqual(outcome["decision"], CLAIMED)
        row = self.claim_rows("D-AUTO")[0]
        self.assertEqual(row["operating_mode_at_claim"], "full_auto")
        self.assertEqual(row["signature_reference"], signoff["signature_reference"])
        self.assertIsNone(row["approved_by"])
        self.assertIsNone(row["approved_at"])
        self.assertEqual(outcome["payload"]["authorization"]["source"], "automation_signoff")

    def test_full_auto_without_a_valid_signoff_still_cannot_fire(self):
        self.add_debtor("D-NOSIGN", client_id=self.CLIENT, aging_state="60")
        self.engine.settings(self.CLIENT)
        self.force_mode(self.CLIENT, "full_auto")          # hand-edited, no sign-off
        self.assertEqual(self.engine.settings(self.CLIENT)["signature_reference"], None)
        outcome = self.engine.send("D-NOSIGN", "voice", dispatch=False)
        self.assertEqual(outcome["decision"], BLOCKED_NO_SIGNOFF)
        self.assertFalse(outcome["claimed"])
        self.assertEqual(self.claim_rows("D-NOSIGN"), [])
        self.assertEqual(self.engine.settings(self.CLIENT)["claim_seq"], 0)

    def test_the_mode_flip_is_what_switches_the_attribution(self):
        """The same client, the same sign-off on file: only the mode in force changes."""
        self.add_debtor("D-SAME", client_id=self.CLIENT, aging_state="60")
        self.sign_off(client_id=self.CLIENT)
        self.engine.approve("D-SAME", "dana@client.example")
        hitl = self.engine.send("D-SAME", "voice", dispatch=False)
        self.assertEqual(hitl["payload"]["authorization"]["source"], "human_approval_in_hitl")
        self.engine.switch_mode(self.CLIENT, "full_auto", "owner", "signed")
        auto = self.engine.send("D-SAME", "voice", dispatch=False)
        self.assertEqual(auto["payload"]["authorization"]["source"], "automation_signoff")
        rows = self.claim_rows("D-SAME")
        self.assertEqual([r["operating_mode_at_claim"] for r in rows], ["hitl", "full_auto"])
        self.assertEqual([r["approved_by"] for r in rows], ["dana@client.example", None])

    def test_a_blocked_attempt_records_no_approver_and_no_claim(self):
        self.add_debtor("D-BLOCK", client_id=self.CLIENT, aging_state="60")
        outcome = self.engine.send("D-BLOCK", "voice", dispatch=False)
        self.assertEqual(outcome["decision"], BLOCKED_MODE_HITL)
        row = self.contacts_for("D-BLOCK")[-1]
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIsNone(row["claim_token"])
        self.assertIsNone(row["approved_by"])
        self.assertIsNone(row["approved_at"])


# ---------------------------------------------------------------------------
# Tier 6 - the vocabulary the surfaces read
# ---------------------------------------------------------------------------
class TestVocabulary(GateTestCase):
    def test_the_table_list_matches_the_store(self):
        from engine.app import TABLES
        rows = self.engine.store.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        self.assertEqual(sorted(r["name"] for r in rows), sorted(TABLES))
        # The canonical list, spelled out (the count is a canary: a table dropped from the
        # schema and from TABLES at the same time would otherwise never be noticed).
        #   Phase 1  debtor_records, client_autonomy_settings, autonomy_mode_history,
        #            outbound_contact_logs, payment_events, collections_ledger,
        #            balance_edit_events, reopen_audit_events
        #   Phase 2  billing_route_verifications, lifecycle_transition_log,
        #            manual_review_decisions, commercial_msa_records
        #   Phase 3  ingestion_batches
        self.assertEqual(len(TABLES), 13)
        self.assertEqual(len(set(TABLES)), len(TABLES), "TABLES has a duplicate entry")

    def test_the_snapshot_exporter_covers_every_table(self):
        from engine.app import TABLES
        snapshot = self.engine.snapshot()
        self.assertEqual(sorted(snapshot), sorted(TABLES))
        for table in snapshot:
            self.assertIsInstance(snapshot[table], list)

    def test_docs_schema_lists_every_table_the_store_has(self):
        from engine.app import TABLES
        text = (config.repo_root() / "docs" / "schema.md").read_text(encoding="utf-8")
        for table in TABLES:
            self.assertIn(table, text, f"docs/schema.md does not name {table}")


class tempfile_json:
    """A temporary JSON file holding a mapping, deleted on exit."""

    def __init__(self, payload):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="recoverai-gate-")
        self.path = Path(self.dir) / "platform.json"
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


if __name__ == "__main__":
    unittest.main()
