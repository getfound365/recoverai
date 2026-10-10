"""The four non-negotiable rules, tested one at a time.

  1. the disclaimer is appended verbatim, byte-identical, at the absolute bottom, once
  2. the 08:00-21:00 local calling window (area-code derived) freezes, never calls
  3. an inbound STOP / Unsubscribe / Stop calling sets dnc_flag and kills all runs
  4. full_auto is locked without a valid sign-off, in the claim transaction itself
"""
from __future__ import annotations

import unittest

from engine import clock, config, draft as draftsman, timezones
from engine.orchestrator import (BLOCKED_DNC, BLOCKED_MODE_HITL, BLOCKED_NO_SIGNOFF,
                                 BLOCKED_OUT_OF_SCOPE, BLOCKED_OUTSIDE_WINDOW, BLOCKED_SIGNOFF_INVALID, CLAIMED)
from tests.base import EngineTestCase

DISCLAIMER = config.disclaimer()


class TestDisclaimerByteIdentity(EngineTestCase):
    def test_body_ends_with_exact_disclaimer_bytes_and_once(self):
        for channel in ("email", "voice", "sms"):
            record = {"debtor_id": "D-1", "client_id": "CLI-TEST", "name": "Ada Lovelace",
                      "balance_owed": 1234.56, "invoice_date": "2026-06-15", "aging_state": "90",
                      "age_days": 113, "invoice_number": "INV-9"}
            artifact = draftsman.render(record, channel=channel)
            body = artifact["body"]
            self.assertTrue(draftsman.disclaimer_is_last(body), f"{channel}: disclaimer is not last")
            self.assertEqual(
                body.encode("utf-8")[-len(DISCLAIMER.encode("utf-8")):], DISCLAIMER.encode("utf-8"),
                f"{channel}: trailing bytes are not the rulebook disclaimer")
            self.assertEqual(body.count(DISCLAIMER), 1, f"{channel}: disclaimer must appear exactly once")
            self.assertEqual(artifact["disclaimer_occurrences"], 1)

    def test_appending_twice_still_yields_exactly_one(self):
        twice = draftsman._append_disclaimer(f"Hello.\n\n{DISCLAIMER}\n\n{DISCLAIMER}")
        self.assertEqual(twice.count(DISCLAIMER), 1)
        self.assertTrue(twice.endswith(DISCLAIMER))

    def test_disclaimer_text_comes_from_the_rulebook(self):
        rulebook_text = config.load_json(config.config_dir() / "rulebook.json")["disclaimer"]
        self.assertEqual(DISCLAIMER, rulebook_text)
        self.assertEqual(rulebook_text,
                         "This is an attempt to collect a debt. Any information obtained "
                         "will be used for that purpose.")

    def test_queued_draft_file_and_payload_message_keep_disclaimer_last(self):
        self.add_debtor("D-1", balance=500.0, aging_state="30")
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        outcome = self.engine.dispatch_new_draft("D-1", "voice")
        self.assertEqual(outcome["decision"], CLAIMED)
        self.assertTrue(outcome["payload"]["message"].endswith(DISCLAIMER))
        self.assertEqual(outcome["payload"]["message"].count(DISCLAIMER), 1)
        self.assertEqual(outcome["payload"]["disclaimer"]["position"], "absolute_bottom")
        queued = self.engine.queue_draft("D-1", "email")
        with open(queued["path"], encoding="utf-8") as fh:
            file_text = fh.read()
        self.assertIn(DISCLAIMER, file_text)


class TestCallingWindow(EngineTestCase):
    def test_out_of_window_freezes_and_sets_the_status_token(self):
        self.add_debtor("HI-1", phone="(808) 555-0119")  # Honolulu: 04:00 local at 14:00Z
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        outcome = self.engine.send("HI-1", "voice")
        self.assertEqual(outcome["decision"], BLOCKED_OUTSIDE_WINDOW)
        self.assertFalse(outcome["claimed"])
        self.assertEqual(self.engine.debtor("HI-1")["approval_status"], "Outside_Calling_Window")
        self.assertIsNone(self.contacts_for("HI-1")[-1]["claim_token"])
        self.assertEqual(self.claim_rows("HI-1"), [], "a frozen run must write no claim")

    def test_freezes_in_every_mode_including_hitl(self):
        self.add_debtor("HI-1", phone="(808) 555-0119")
        outcome = self.engine.send("HI-1", "voice")  # default mode, no sign-off, no approval
        self.assertEqual(outcome["decision"], BLOCKED_OUTSIDE_WINDOW)
        self.assertEqual(self.engine.debtor("HI-1")["approval_status"], "Outside_Calling_Window")

    def test_window_edges_are_inclusive(self):
        # 2026-10-06T12:00:00Z -> 08:00 in New York (EDT) : inside
        # 2026-10-07T01:00:00Z -> 21:00 in New York (EDT) : inside
        # 2026-10-07T01:01:00Z -> 21:01 in New York        : outside
        cases = [("2026-10-06T12:00:00Z", True), ("2026-10-07T01:00:00Z", True),
                 ("2026-10-07T01:01:00Z", False), ("2026-10-06T11:59:00Z", False)]
        for instant, expected in cases:
            decision = timezones.window_decision_for("(212) 555-0143", at=clock.parse_ts(instant))
            self.assertEqual(decision["in_window"], expected, f"{instant} -> {decision['local_clock']}")

    def test_email_is_not_windowed_but_voice_is(self):
        self.add_debtor("HI-1", phone="(808) 555-0119", aging_state="60", approval_status="Approved")
        self.sign_off(channels=("email", "voice"), states=("60",))
        email = self.engine.send("HI-1", "email")
        self.assertEqual(email["decision"], CLAIMED, email.get("reason_detail"))
        self.assertEqual(self.engine.send("HI-1", "voice")["decision"], BLOCKED_OUTSIDE_WINDOW)

    def test_a_frozen_record_unfreezes_only_when_the_window_reopens(self):
        self.add_debtor("HI-1", phone="(808) 555-0119", aging_state="60")
        self.assertEqual(self.engine.send("HI-1", "voice")["decision"], BLOCKED_OUTSIDE_WINDOW)
        self.assertEqual(self.engine.debtor("HI-1")["approval_status"], "Outside_Calling_Window")
        # a human click while the debtor's local time is still 04:00 must not unfreeze the run
        refused = self.engine.approve("HI-1", who="human")
        self.assertFalse(refused["approved"])
        self.assertEqual(self.engine.debtor("HI-1")["approval_status"], "Outside_Calling_Window")
        # 20:00 UTC is 10:00 in Honolulu: the window is open, the human can approve, the claim goes through
        clock.set_fake_now("2026-10-06T20:00:00Z")
        self.assertTrue(self.engine.approve("HI-1", who="human")["approved"])
        outcome = self.engine.send("HI-1", "voice")
        self.assertEqual(outcome["decision"], CLAIMED)
        self.assertEqual(self.engine.debtor("HI-1")["approval_status"], "Sent")
        clock.set_fake_now(self.NOW)

    def test_unknown_area_code_fails_closed(self):
        self.add_debtor("UNK-1", phone="555-0100")
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        outcome = self.engine.send("UNK-1", "voice")
        self.assertEqual(outcome["decision"], BLOCKED_OUTSIDE_WINDOW)
        self.assertEqual(outcome["window_decision"], "Unknown_Area_Code_Freeze")
        self.assertEqual(self.engine.debtor("UNK-1")["approval_status"], "Outside_Calling_Window")


class TestDncOptOut(EngineTestCase):
    def test_each_keyword_sets_dnc_case_insensitively(self):
        for index, content in enumerate(["please STOP", "UNSUBSCRIBE me", "stop calling me now",
                                         "Stop Calling", "Unsubscribe"]):
            debtor = f"DNC-{index}"
            self.add_debtor(debtor)
            result = self.engine.handle_inbound({"debtor_id": debtor, "content": content})
            self.assertTrue(result["dnc_set"], f"{content!r} did not opt the debtor out")
            self.assertEqual(self.engine.debtor(debtor)["dnc_flag"], 1)
            self.assertTrue(result["matched_keywords"])

    def test_stop_kills_scheduled_runs_and_blocks_every_future_contact(self):
        self.add_debtor("D-2", approval_status="Approved")
        run = self.engine.schedule("D-2", "voice")
        result = self.engine.handle_inbound({"debtor_id": "D-2", "content": "STOP"})
        self.assertEqual(result["runs_cancelled"], 1)
        self.assertEqual(self.engine.queue.get(run["run_id"]).state, "cancelled")
        self.assertEqual(self.engine.run_due(), [], "a cancelled run must never be executed")
        outcome = self.engine.send("D-2", "voice")
        self.assertEqual(outcome["decision"], BLOCKED_DNC)
        self.assertEqual(self.claim_rows("D-2"), [], "DNC must stop the claim, not just the dispatch")

    def test_opt_out_by_phone_number_when_no_debtor_id_is_supplied(self):
        self.add_debtor("D-3", phone="(212) 555-0999")
        result = self.engine.handle_inbound({"from": "+1 (212) 555-0999", "Body": "unsubscribe"})
        self.assertEqual(result["debtor_id"], "D-3")
        self.assertEqual(self.engine.debtor("D-3")["dnc_flag"], 1)

    def test_ordinary_replies_do_not_set_dnc(self):
        self.add_debtor("D-4")
        result = self.engine.handle_inbound({"debtor_id": "D-4", "content": "Cheque is in the post"})
        self.assertFalse(result["dnc_set"])
        self.assertEqual(self.engine.debtor("D-4")["dnc_flag"], 0)


class TestFullAutoLock(EngineTestCase):
    def test_full_auto_is_refused_without_a_signoff(self):
        self.add_debtor("D-1")
        result = self.engine.switch_mode("CLI-TEST", "full_auto", "client", "wants automation")
        self.assertTrue(result["refused"])
        self.assertEqual(result["new_mode"], "hitl")
        self.assertEqual(self.engine.settings("CLI-TEST")["operating_mode"], "hitl")
        timeline = self.engine.timeline("CLI-TEST")
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["new_mode"], "hitl")
        self.assertIn("REFUSED", timeline[0]["reason"])

    def test_claim_refuses_full_auto_even_if_the_row_is_forced(self):
        """Defence in depth: even a hand-edited mode row cannot produce a claim."""
        self.add_debtor("D-1")
        self.force_mode("CLI-TEST", "full_auto")
        outcome = self.engine.send("D-1", "voice")
        self.assertEqual(outcome["decision"], BLOCKED_NO_SIGNOFF)
        self.assertEqual(self.claim_rows("D-1"), [])

    def test_claim_refuses_when_the_signoff_flag_is_cleared_but_mode_stays(self):
        self.add_debtor("D-1")
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                "UPDATE client_autonomy_settings SET has_valid_signoff = 0 WHERE client_id = ?",
                ("CLI-TEST",))
        outcome = self.engine.send("D-1", "voice")
        self.assertEqual(outcome["decision"], BLOCKED_SIGNOFF_INVALID)
        self.assertEqual(self.claim_rows("D-1"), [])

    def test_default_mode_is_hitl_and_needs_human_approval(self):
        self.add_debtor("D-1")
        self.engine.settings("CLI-TEST")
        self.assertEqual(self.engine.settings("CLI-TEST")["operating_mode"], "hitl")
        self.assertEqual(self.engine.send("D-1", "voice")["decision"], BLOCKED_MODE_HITL)
        self.engine.approve("D-1", who="human")
        self.assertEqual(self.engine.send("D-1", "voice")["decision"], CLAIMED)

    def test_signoff_scope_is_honoured_in_full_auto(self):
        self.add_debtor("E-30", aging_state="30")
        self.add_debtor("E-60", aging_state="60")
        self.sign_off(channels=("email",), states=("30",))
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        self.assertEqual(self.engine.send("E-30", "email")["decision"], CLAIMED)
        self.assertEqual(self.engine.send("E-60", "email")["decision"], BLOCKED_OUT_OF_SCOPE)
        self.assertEqual(self.engine.send("E-30", "voice")["decision"], BLOCKED_OUT_OF_SCOPE)
        self.assertEqual(len(self.claim_rows()), 1)

    def test_credit_balance_is_never_collected(self):
        self.add_debtor("C-1", balance=-320.75)
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        outcome = self.engine.send("C-1", "voice")
        self.assertEqual(outcome["decision"], "BLOCKED_NO_BALANCE_DUE")
        self.assertEqual(self.claim_rows("C-1"), [])


if __name__ == "__main__":
    unittest.main()
