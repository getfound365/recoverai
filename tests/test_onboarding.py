"""Onboarding back end: the sign-off hash, the full_auto gate, revocation and expiry."""
from __future__ import annotations

import unittest

from engine import onboarding
from tests.base import EngineTestCase


class TestSignoff(EngineTestCase):
    def test_signature_reference_is_a_hash_of_the_five_inputs(self):
        a = onboarding.compute_signature_reference("CLI-X", "Dana Whitmore", "CFO", "AUT-1.0",
                                                  "2026-10-06T14:00:00Z")
        b = onboarding.compute_signature_reference("CLI-X", "Dana Whitmore", "CFO", "AUT-1.0",
                                                   "2026-10-06T14:00:00Z")
        c = onboarding.compute_signature_reference("CLI-X", "Dana Whitmore", "CFO", "AUT-1.0",
                                                   "2026-10-06T14:00:01Z")
        self.assertEqual(a, b, "the same inputs must produce the same reference")
        self.assertNotEqual(a, c, "a different timestamp must produce a different reference")
        self.assertTrue(a.startswith("sha256:"))
        self.assertEqual(len(a), len("sha256:") + 64)

    def test_capture_requires_a_typed_name_and_title(self):
        with self.assertRaises(ValueError):
            self.engine.capture_signoff("CLI-TEST", "", "CFO", ["email"], ["30"], who="test")
        with self.assertRaises(ValueError):
            self.engine.capture_signoff("CLI-TEST", "Dana", "   ", ["email"], ["30"], who="test")

    def test_scope_entries_must_exist_in_the_rulebook(self):
        with self.assertRaises(ValueError):
            self.engine.capture_signoff("CLI-TEST", "Dana", "CFO", ["carrier_pigeon"], ["30"], who="test")
        with self.assertRaises(ValueError):
            self.engine.capture_signoff("CLI-TEST", "Dana", "CFO", ["email"], ["365"], who="test")

    def test_signing_leaves_the_client_in_hitl(self):
        self.add_debtor("D-1")
        signoff = self.sign_off()
        settings = self.engine.settings("CLI-TEST")
        self.assertEqual(settings["operating_mode"], "hitl")
        self.assertEqual(settings["has_valid_signoff"], 1)
        self.assertEqual(signoff["operating_mode_after_signoff"], "hitl")
        self.assertEqual(self.engine.send("D-1", "voice")["decision"], "BLOCKED_MODE_HITL")

    def test_settings_row_records_signer_scope_and_timestamps(self):
        signoff = self.sign_off(channels=("email", "voice"), states=("60", "90"))
        row = self.engine.settings("CLI-TEST")
        self.assertEqual(row["signer_name"], "Test Signer")
        self.assertEqual(row["signer_title"], "CFO")
        self.assertEqual(row["signature_reference"], signoff["signature_reference"])
        self.assertEqual(row["authorization_document_version"], "AUT-1.0")
        self.assertIn("Email reminders for invoices 60+ days late", signoff["plain_english_scope"])
        self.assertEqual(row["signed_timestamp"], signoff["signed_timestamp"])
        self.assertIsNotNone(row["signoff_expires_at"])

    def test_revocation_drops_full_auto_back_to_hitl(self):
        self.add_debtor("D-1")
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        result = self.engine.revoke_signoff("CLI-TEST", who="signer", reason="pilot ended")
        self.assertTrue(result["dropped_to_hitl"])
        self.assertEqual(self.engine.settings("CLI-TEST")["operating_mode"], "hitl")
        self.assertEqual(self.engine.settings("CLI-TEST")["has_valid_signoff"], 0)
        # and it cannot be re-armed
        retry = self.engine.switch_mode("CLI-TEST", "full_auto", "test", "trying again")
        self.assertTrue(retry["refused"])
        self.assertEqual(self.engine.settings("CLI-TEST")["operating_mode"], "hitl")
        # the revoked sign-off can no longer authorize a claim either
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                "UPDATE client_autonomy_settings SET operating_mode = 'full_auto' WHERE client_id = ?",
                ("CLI-TEST",))
        self.assertEqual(self.engine.send("D-1", "voice")["decision"], "BLOCKED_SIGNOFF_INVALID")

    def test_expired_signoff_drops_the_client_to_hitl(self):
        self.add_debtor("D-1")
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        self.assertEqual(self.engine.settings("CLI-TEST")["operating_mode"], "full_auto")
        # re-signed with a document that is already past its expiry
        self.engine.capture_signoff("CLI-TEST", "Test Signer", "CFO", ["email", "voice"],
                                    ["30", "60", "90"], who="test", validity_days=-1)
        downgraded = self.engine.sweep_expired_signoffs()
        self.assertEqual([d["client_id"] for d in downgraded], ["CLI-TEST"])
        self.assertEqual(self.engine.settings("CLI-TEST")["operating_mode"], "hitl")
        self.assertEqual(self.engine.settings("CLI-TEST")["has_valid_signoff"], 0)
        self.assertEqual(self.engine.send("D-1", "voice")["decision"], "BLOCKED_MODE_HITL")

    def test_mode_switch_both_directions_is_recorded(self):
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "signer", "on")
        self.engine.switch_mode("CLI-TEST", "hitl", "signer", "off")
        modes = [(r["old_mode"], r["new_mode"]) for r in self.engine.timeline("CLI-TEST")]
        self.assertEqual(modes, [("hitl", "full_auto"), ("full_auto", "hitl")])
        versions = [r["mode_version"] for r in self.engine.timeline("CLI-TEST")]
        self.assertEqual(versions, [1, 2])

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            self.engine.switch_mode("CLI-TEST", "semi_auto", "test", "nope")


if __name__ == "__main__":
    unittest.main()
