"""The signature-reference matrix, and the rebind a changed tracking choice forces.

The digest is composed by looping over `onboarding.SIGNATURE_REFERENCE_COMPONENTS` in a
fixed, documented order - `client_id`, `communication_boundaries`,
`tracking_consent_selection`, the RAW `warranty_text_version` string, then the warranty
bytes, the signer's identity and the timestamp. These tests prove each component moves the
digest on its own, that the order is part of the contract, that the digest on the client's
configuration row can be re-derived from that row alone, and that changing the fee-tracking
consent rebinds and REVOKES the sign-off through an append-only trail row.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest

from engine import config, onboarding
from tests.base import EngineTestCase

_SEGMENT_SEP = "\x1f"


def _base_components():
    return {
        "client_id": "CLI-X",
        "communication_boundaries": onboarding.communication_boundaries_token(
            "AUT-1.0", [["email", "30"]]),
        "tracking_consent_selection": "hybrid",
        "warranty_text_version": "WAR-1.0",
        "warranty_text_sha256": "a" * 64,
        "signer_name": "Dana Whitmore",
        "signer_title": "CFO",
        "signed_timestamp": "2026-10-06T14:00:00Z",
    }


class TestTheMatrixComposition(unittest.TestCase):
    def test_the_four_bound_components_lead_the_matrix_in_the_documented_order(self):
        self.assertEqual(onboarding.SIGNATURE_REFERENCE_COMPONENTS[:4],
                         ("client_id", "communication_boundaries",
                          "tracking_consent_selection", "warranty_text_version"))

    def test_every_component_the_digest_loops_over_has_a_label_the_console_can_show(self):
        covers = onboarding.signature_covers()
        self.assertEqual(len(covers), len(onboarding.SIGNATURE_REFERENCE_COMPONENTS))
        for key in onboarding.SIGNATURE_REFERENCE_COMPONENTS:
            self.assertIn(key, onboarding.SIGNATURE_COMPONENT_LABELS)
        self.assertEqual(covers, [onboarding.SIGNATURE_COMPONENT_LABELS[k]
                                  for k in onboarding.SIGNATURE_REFERENCE_COMPONENTS])

    def test_each_bound_component_changes_the_digest_independently(self):
        digest = onboarding.compose_signature_reference(_base_components())
        variations = {
            "client_id": "CLI-Y",
            "communication_boundaries": onboarding.communication_boundaries_token(
                "AUT-1.0", [["email", "30"], ["voice", "60"]]),
            "tracking_consent_selection": "manual_attestation",
            "warranty_text_version": "WAR-1.1",
            "warranty_text_sha256": "b" * 64,
            "signer_name": "Someone Else",
            "signer_title": "Controller",
            "signed_timestamp": "2026-10-06T14:00:01Z",
        }
        self.assertEqual(set(variations), set(onboarding.SIGNATURE_REFERENCE_COMPONENTS))
        for key, value in sorted(variations.items()):
            with self.subTest(component=key):
                changed = dict(_base_components())
                changed[key] = value
                self.assertNotEqual(onboarding.compose_signature_reference(changed), digest,
                                    f"changing {key} must change the digest")
        # the same components in the same order reproduce it byte for byte
        self.assertEqual(onboarding.compose_signature_reference(_base_components()), digest)

    def test_the_order_of_the_matrix_is_part_of_the_contract(self):
        components = _base_components()
        reordered = _SEGMENT_SEP.join(
            f"{key}={components[key]}"
            for key in reversed(onboarding.SIGNATURE_REFERENCE_COMPONENTS))
        self.assertNotEqual(
            "sha256:" + hashlib.sha256(reordered.encode("utf-8")).hexdigest(),
            onboarding.compose_signature_reference(components))

    def test_a_hole_in_a_component_is_refused_rather_than_hashed(self):
        broken = _base_components()
        broken["warranty_text_version"] = ""
        with self.assertRaises(ValueError):
            onboarding.compose_signature_reference(broken)

    def test_the_boundaries_are_in_the_digest_and_are_order_independent(self):
        args = ("CLI-X", "Dana", "CFO", "AUT-1.0", "2026-10-06T14:00:00Z")
        kwargs = {"tracking_mode": "hybrid", "step3_attestation_sha256": "a" * 64}
        one = onboarding.compute_signature_reference(*args, scope_pairs=[["email", "30"]], **kwargs)
        two = onboarding.compute_signature_reference(
            *args, scope_pairs=[["email", "30"], ["email", "60"]], **kwargs)
        same = onboarding.compute_signature_reference(
            *args, scope_pairs=[["email", "30"]], **kwargs)
        self.assertNotEqual(one, two, "one extra authorized boundary changes the digest")
        self.assertEqual(one, same, "the same ticks in the same order reproduce it")


class TestTheReferenceIsPinnedToTheClientRow(EngineTestCase):
    def test_the_settings_row_carries_the_digest_and_it_re_derives_from_that_row_alone(self):
        self.attest_step3()
        signoff = self.sign_off(channels=("email", "voice"), states=("60", "90"))
        row = self.engine.settings("CLI-TEST")
        self.assertEqual(row["signature_reference"], signoff["signature_reference"])
        self.assertEqual(signoff["signature_binding"]["components"],
                         list(onboarding.SIGNATURE_REFERENCE_COMPONENTS))
        stored_pairs = row["authorized_scope_pairs"]
        if isinstance(stored_pairs, str):
            stored_pairs = json.loads(stored_pairs)
        pairs = [tuple(str(p).split(":", 1)) for p in stored_pairs]
        attestation = onboarding.current_attestation(self.engine.store, "CLI-TEST",
                                                     onboarding.ATTESTATION_STEP3)
        rederived = onboarding.compose_signature_reference({
            "client_id": "CLI-TEST",
            "communication_boundaries": onboarding.communication_boundaries_token(
                row["authorization_document_version"], pairs),
            "tracking_consent_selection": str(row["tracking_mode"]),
            "warranty_text_version": str(attestation["document_version"]),
            "warranty_text_sha256": str(attestation["text_sha256"]),
            "signer_name": row["signer_name"],
            "signer_title": row["signer_title"],
            "signed_timestamp": row["signed_timestamp"],
        })
        self.assertEqual(rederived, row["signature_reference"])
        self.assertEqual(signoff["signature_binding"]["warranty_text_version"],
                         str(attestation["document_version"]))
        self.assertEqual(signoff["signature_binding"]["communication_boundaries"],
                         onboarding.communication_boundaries_token(
                             row["authorization_document_version"], pairs))


class TestATrackingChoiceChangeRebindsTheSignoff(EngineTestCase):
    def test_changing_the_choice_rebinds_and_revokes_with_an_immutable_trail_row(self):
        self.add_debtor("D-1")
        self.attest_step3()
        signoff = self.sign_off()
        old_reference = signoff["signature_reference"]
        self.engine.switch_mode("CLI-TEST", "full_auto", "test", "signed")
        self.assertEqual(self.engine.settings("CLI-TEST")["operating_mode"], "full_auto")

        result = self.engine.set_tracking_mode("CLI-TEST", "integration", "signer",
                                               "we connected a ledger")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["changed"])
        self.assertTrue(result["signoff_rebind_required"])
        self.assertEqual(result["rebind_token"], "SIGNOFF_REBIND_REQUIRED")
        self.assertEqual(result["revoked_signature_reference"], old_reference)

        settings = self.engine.settings("CLI-TEST")
        self.assertEqual(settings["tracking_mode"], "integration")
        self.assertEqual(settings["has_valid_signoff"], 0)
        self.assertEqual(settings["operating_mode"], "hitl")
        self.assertEqual(self.engine.send("D-1", "voice")["decision"], "BLOCKED_MODE_HITL")

        trail = self.engine.store.query(
            "SELECT * FROM collections_tracking_choices WHERE client_id = ? ORDER BY choice_id",
            ("CLI-TEST",))
        self.assertEqual(len(trail), 1)
        row = trail[0]
        self.assertEqual((row["old_mode"], row["new_mode"]), ("hybrid", "integration"))
        self.assertEqual(row["bound_signature_reference"], old_reference)
        self.assertEqual(row["signoff_rebind_required"], 1)
        self.assertEqual(row["rebind_token"], "SIGNOFF_REBIND_REQUIRED")
        self.assertEqual(row["choice_version"], 1)
        self.assertTrue(str(row["timestamp_ms"]))
        self.assertTrue(str(row["who"]))

        # the trail is append-only, like every other history row in this engine
        for statement in (
                "UPDATE collections_tracking_choices SET new_mode = 'hybrid' WHERE choice_id = ?",
                "DELETE FROM collections_tracking_choices WHERE choice_id = ?"):
            with self.subTest(statement=statement.split()[0]):
                with self.assertRaises(sqlite3.DatabaseError):
                    with self.engine.store.tx(immediate=True):
                        self.engine.store.execute(statement, (row["choice_id"],))

    def test_a_fresh_signature_under_the_new_choice_is_a_different_digest(self):
        self.attest_step3()
        first = self.sign_off()
        self.engine.set_tracking_mode("CLI-TEST", "manual_attestation", "signer", "no ledger")
        second = self.sign_off()
        self.assertNotEqual(first["signature_reference"], second["signature_reference"])
        self.assertEqual(self.engine.settings("CLI-TEST")["tracking_mode"], "manual_attestation")
        self.assertEqual(second["signature_binding"]["tracking_consent_selection"],
                         "manual_attestation")
        self.assertEqual(config.default_fee_tracking_mode(), "hybrid")


if __name__ == "__main__":
    unittest.main()
