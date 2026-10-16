"""The Step 3 sign-off path, end to end: console -> bridge -> facade -> `onboarding`.

QA's independent Phase 4 pass (2026-10-10, `onboarding-qa-evidence/QA-PHASE4-VERDICT.md`,
defect D1) proved the console's Sign button could never succeed:

    printf '{"op":"signoff","signer_name":"QA Probe","signer_title":"Tester"}' \\
      | python3 tools/console_bridge.py
    -> {"bridge_ok": false, "error": "ENGINE_ERROR",
        "detail": "TypeError: RecoverAI.capture_signoff() got an unexpected keyword
                   argument 'tracking_mode'"}

`tools/console_bridge.py` passed the wizard's Step 1 choice to
`engine.app.RecoverAI.capture_signoff`, which did not accept it, so every sign-off died
before it reached the one canonical path in `engine/onboarding.py`.

These tests hold that path together, and hold the two ends of it to their contracts:
the bridge op returns a real reference with the choice it was given, the digest still
covers the four owner-mandated components and the row carries it, and the facade's old
signature (no tracking mode) still signs a client.

The second half covers the bootstrap's fixture writes: they are legitimate for the
console's own sandbox client and for nothing else, because a Step 3 acknowledgement is a
human's explicit TRUE and a sign-off is a human's signature.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import config, onboarding
from tests.base import EngineTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE = REPO_ROOT / "tools" / "console_bridge.py"

#: The four owner-mandated components, in the order the digest loops over them.
BOUND_COMPONENTS = ("client_id", "communication_boundaries",
                    "tracking_consent_selection", "warranty_text_version")


class TestTheBridgeSignoffOp(EngineTestCase):
    """The op the console's Sign button calls, on a real engine, exactly as it calls it."""

    def signoff(self, **request) -> dict:
        from tools import console_bridge  # noqa: PLC0415
        request.setdefault("signer_name", "QA Probe")
        request.setdefault("signer_title", "Tester")
        return console_bridge.op_signoff(self.engine, request)

    def test_a_signoff_with_a_tracking_mode_returns_ok_and_a_signature_reference(self):
        result = self.signoff(tracking_mode="manual_attestation")
        self.assertTrue(result["ok"], result)
        reference = str(result["signature_reference"])
        self.assertTrue(reference.startswith("sha256:"), result)
        self.assertEqual(len(reference), len("sha256:") + 64, result)
        self.assertEqual(result["signer_name"], "QA Probe")

        row = self.engine.settings(result["client_id"])
        self.assertEqual(row["signature_reference"], reference)
        self.assertEqual(row["has_valid_signoff"], 1)
        # the choice the wizard sent is the choice the signer signed under, and it is on
        # the client's own row - not merely echoed back in the answer
        self.assertEqual(result["signature_binding"]["tracking_consent_selection"],
                         "manual_attestation")
        self.assertEqual(row["tracking_mode"], "manual_attestation")

    def test_the_digest_covers_the_four_owner_mandated_components_and_the_row_carries_it(self):
        signing = self.signoff(signer_name="Dana Whitmore", signer_title="Managing Partner",
                               tracking_mode="integration")
        binding = signing["signature_binding"]

        self.assertEqual(tuple(binding["components"][:4]), BOUND_COMPONENTS)
        self.assertEqual(tuple(binding["matrix_order"]), tuple(onboarding.SIGNATURE_REFERENCE_COMPONENTS))
        self.assertEqual(binding["tracking_consent_selection"], "integration")

        # the console renders `covers` verbatim: all four have to be in it, by name
        covers = " | ".join(binding["covers"])
        for component in BOUND_COMPONENTS:
            with self.subTest(component=component):
                self.assertIn(component, covers)

        # re-derive the digest from the client's own row plus the attestation row: this is
        # what a third party checking the reference has to be able to do
        client_id = signing["client_id"]
        row = self.engine.settings(client_id)
        stored_pairs = row["authorized_scope_pairs"]
        if isinstance(stored_pairs, str):
            stored_pairs = json.loads(stored_pairs)
        pairs = [tuple(str(p).split(":", 1)) for p in stored_pairs]
        attestation = onboarding.current_attestation(self.engine.store, client_id,
                                                     onboarding.ATTESTATION_STEP3)
        self.assertIsNotNone(attestation, "the bootstrap's fixture warranty is not on file")
        rederived = onboarding.compose_signature_reference({
            "client_id": client_id,
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
        self.assertEqual(rederived, signing["signature_reference"])
        self.assertEqual(binding["step3_attestation_text_sha256"], str(attestation["text_sha256"]))
        self.assertTrue(signing["step3_attestation_bound"])

        # and each of the four moves the digest: same signer, same timestamp, same
        # boundaries - only the consented tracking choice changes
        other_choice = onboarding.compose_signature_reference({
            "client_id": client_id,
            "communication_boundaries": onboarding.communication_boundaries_token(
                row["authorization_document_version"], pairs),
            "tracking_consent_selection": "hybrid",
            "warranty_text_version": str(attestation["document_version"]),
            "warranty_text_sha256": str(attestation["text_sha256"]),
            "signer_name": row["signer_name"],
            "signer_title": row["signer_title"],
            "signed_timestamp": row["signed_timestamp"],
        })
        self.assertNotEqual(other_choice, rederived)

    def test_a_signoff_without_a_tracking_mode_signs_under_the_choice_on_file(self):
        """The op's `request.get("tracking_mode") or None` path: no choice sent, no guess."""
        self.signoff(tracking_mode="integration")
        result = self.signoff(signer_name="Dana Whitmore", signer_title="Managing Partner")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["signature_binding"]["tracking_consent_selection"], "integration")

    def test_an_unknown_tracking_mode_is_refused_by_the_engine_not_silently_dropped(self):
        # `op_signoff` is a thin dispatch: `capture_signoff` accepts only a mode the
        # rulebook knows, and says which ones it knows. The refusal is the engine's, and
        # it happens BEFORE the row is written.
        first = self.signoff(tracking_mode="hybrid")  # runs the bootstrap's own fixture writes
        before = self.engine.settings("CLI-SANDBOX-AGENCY")["signature_reference"]
        self.assertEqual(before, first["signature_reference"])
        with self.assertRaises(ValueError) as caught:
            self.signoff(tracking_mode="telepathy")
        self.assertIn("unknown fee-tracking mode", str(caught.exception))
        self.assertIn(str(config.fee_tracking_mode_ids()), str(caught.exception))
        self.assertEqual(self.engine.settings("CLI-SANDBOX-AGENCY")["signature_reference"], before)


class TestTheFacadeKeepsItsOldSignature(EngineTestCase):
    """Backward compatibility: the positional call every existing caller makes still signs."""

    def test_capture_signoff_without_a_tracking_mode_still_works(self):
        first = self.engine.capture_signoff("CLI-TEST", "Dana Whitmore", "CFO",
                                            ["email"], ["60"], "test")
        self.assertTrue(str(first["signature_reference"]).startswith("sha256:"), first)
        self.assertEqual(first["signature_binding"]["tracking_consent_selection"],
                         config.default_fee_tracking_mode())
        self.assertEqual(self.engine.settings("CLI-TEST")["signature_reference"],
                         first["signature_reference"])

        # once the client has changed its choice, an omitted argument means "the choice on
        # file", never the rulebook default
        self.engine.set_tracking_mode("CLI-TEST", "manual_attestation", "test", "no ledger")
        second = self.engine.capture_signoff("CLI-TEST", "Dana Whitmore", "CFO",
                                             ["email"], ["60"], "test")
        self.assertEqual(second["signature_binding"]["tracking_consent_selection"],
                         "manual_attestation")
        self.assertNotEqual(first["signature_reference"], second["signature_reference"])
        self.assertEqual(self.engine.settings("CLI-TEST")["signature_reference"],
                         second["signature_reference"])

    def test_the_facade_forwards_the_tracking_mode_and_composes_through_onboarding(self):
        """One canonical path: the digest is composed inside `onboarding.capture_signoff`,
        which is the call the facade forwards to - the facade adds no hashing of its own."""
        with_tracking = self.engine.capture_signoff("CLI-TEST", "Dana Whitmore", "CFO",
                                                    ["email"], ["60"], "test",
                                                    tracking_mode="integration")
        self.assertEqual(with_tracking["signature_binding"]["tracking_consent_selection"],
                         "integration")
        self.assertEqual(self.engine.settings("CLI-TEST")["tracking_mode"], "integration")
        direct = self.engine.settings("CLI-TEST")
        self.assertEqual(direct["signature_reference"], with_tracking["signature_reference"])

        on_file = self.engine.tracking_choices("CLI-TEST")
        self.assertEqual(on_file["current_mode"], "integration")
        self.assertTrue(on_file["signoff_binding_current"])


class TestTheQaCommandThroughTheRealBridge(unittest.TestCase):
    """The literal command from the QA verdict, as a subprocess.

    One JSON request in on stdin, one JSON answer out on stdout - the same door the
    console's server functions use, on its own store so the run is reproducible.
    """

    def test_the_signoff_request_on_stdin_returns_ok_and_a_signature_reference(self):
        tmp = Path(tempfile.mkdtemp(prefix="recoverai-bridge-test-"))
        try:
            env = dict(os.environ,
                       RECOVERAI_CONSOLE_DB=str(tmp / "console.db"),
                       RECOVERAI_CONSOLE_RUNS=str(tmp / "console_runs.json"),
                       RECOVERAI_CONSOLE_AUDIT=str(tmp / "console_audit.jsonl"))
            proc = subprocess.run(
                [sys.executable, str(BRIDGE)],
                input='{"op":"signoff","signer_name":"QA Probe","signer_title":"Tester"}',
                capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            answer = json.loads(proc.stdout)
            self.assertTrue(answer["bridge_ok"], answer)
            payload = answer["payload"]
            self.assertTrue(payload["ok"], payload)
            self.assertTrue(str(payload["signature_reference"]).startswith("sha256:"), payload)
            self.assertEqual(tuple(payload["signature_binding"]["components"][:4]),
                             BOUND_COMPONENTS)
            self.assertIn("tracking_consent_selection (step 1: where we look)",
                          payload["signature_binding"]["covers"])
            self.assertEqual(payload["signer_name"], "QA Probe")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestTheBootstrapWritesFixturesOnlyForAFixtureClient(EngineTestCase):
    """`_bootstrap` ticks a Step 3 warranty (TRUE) and signs, on a client's behalf.

    That is legitimate for the console's fixture client, which has no human of its own,
    and for nothing else: for any other client id the bootstrap must write neither, while
    reads keep working and keep telling the truth.
    """

    REAL_CLIENT = "CLI-REAL-AGENCY"

    def call_bootstrap_as(self, client_id: str) -> dict:
        from tools import console_bridge  # noqa: PLC0415
        original = console_bridge.SANDBOX_CLIENT_ID
        console_bridge.SANDBOX_CLIENT_ID = client_id
        try:
            return console_bridge.op_bootstrap(self.engine, {})
        finally:
            console_bridge.SANDBOX_CLIENT_ID = original

    def test_a_client_outside_the_fixture_prefix_gets_no_attestation_and_no_signature(self):
        info = self.call_bootstrap_as(self.REAL_CLIENT)
        self.assertTrue(info["fixture_writes_refused"], info)
        self.assertEqual(info["refusal_token"], "BLOCKED_FIXTURE_WRITE_FOR_NON_SANDBOX_CLIENT")
        self.assertFalse(info["created_step3_attestation"])
        self.assertFalse(info["created_signoff"])
        self.assertFalse(info["created_client"])
        # nothing at all was written for that client
        self.assertIsNone(onboarding.fetch_settings(self.engine.store, self.REAL_CLIENT))
        self.assertIsNone(onboarding.current_attestation(self.engine.store, self.REAL_CLIENT,
                                                         onboarding.ATTESTATION_STEP3))
        self.assertEqual(self.engine.store.query(
            "SELECT COUNT(*) AS n FROM client_autonomy_settings")[0]["n"], 0)
        # and the refusal is on the audit trail, not swallowed
        audit = (self.tmp / "guardrail_actions.jsonl")
        self.assertTrue(audit.exists(), "no audit file was written")
        self.assertIn("console_fixture_write_refused", audit.read_text())

    def test_the_sandbox_fixture_client_is_untouched(self):
        from tools import console_bridge  # noqa: PLC0415
        info = self.call_bootstrap_as(console_bridge.SANDBOX_CLIENT_ID)
        self.assertTrue(info["sandbox_fixture"], info)
        self.assertTrue(info["created_step3_attestation"], info)
        self.assertTrue(info["created_signoff"], info)
        self.assertNotIn("fixture_writes_refused", info)
        settings = self.engine.settings(console_bridge.SANDBOX_CLIENT_ID)
        self.assertEqual(settings["signer_name"], console_bridge.SANDBOX_SIGNER_NAME)
        self.assertEqual(settings["signer_title"], console_bridge.SANDBOX_SIGNER_TITLE)
        attestation = onboarding.current_attestation(self.engine.store,
                                                     console_bridge.SANDBOX_CLIENT_ID,
                                                     onboarding.ATTESTATION_STEP3)
        self.assertEqual(str(settings["step3_attestation_id"]), str(attestation["attestation_id"]))


if __name__ == "__main__":
    unittest.main()
