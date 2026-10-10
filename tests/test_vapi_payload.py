"""Agent 3's handshake with the (simulated) Vapi endpoint, and the docs that describe it."""
from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from engine import config, draft as draftsman
from engine.orchestrator import CLAIMED
from tests.base import EngineTestCase

DOC = config.repo_root() / "docs" / "vapi-handshake.md"


def _post(url: str, payload: dict, expect_error: bool = False):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if not expect_error:
            raise
        return exc.code, json.loads(exc.read().decode())


class TestVapiHandshake(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.add_debtor("D-1", phone="(212) 555-0143", aging_state="90", balance=4820.50)
        self.signoff = self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "signer", "signed")
        self.outcome = self.engine.send("D-1", "voice")
        self.payload = self.outcome["payload"]

    def test_payload_carries_everything_the_rulebook_requires(self):
        for key in ("contact_type", "destination_phone", "debtor_id", "client_id", "message",
                    "template_reference", "authorization", "claim", "guardrails", "provider"):
            self.assertIn(key, self.payload, f"payload is missing {key}")
        self.assertEqual(self.payload["contact_type"], "voice")
        self.assertEqual(self.payload["destination_phone"], "(212) 555-0143")
        self.assertEqual(self.payload["debtor_id"], "D-1")
        self.assertEqual(self.payload["client_id"], "CLI-TEST")
        self.assertTrue(self.payload["template_reference"].startswith("past_due_notice/voice"))
        self.assertEqual(self.payload["authorization"]["signature_reference"],
                         self.signoff["signature_reference"])
        self.assertEqual(self.payload["authorization"]["document_version"], "AUT-1.0")
        self.assertEqual(self.payload["claim"]["claim_token"], self.outcome["claim_token"])
        self.assertEqual(self.payload["claim"]["operating_mode_at_claim"], "full_auto")
        self.assertEqual(self.payload["guardrails"]["window_decision"], "In_Window")
        self.assertEqual(self.payload["guardrails"]["dnc_flag_at_claim"], False)
        self.assertEqual(self.payload["provider"]["simulated"], True)

    def test_message_is_the_rendered_notice_with_the_disclaimer_last(self):
        self.assertEqual(self.payload["message"], draftsman.render(
            {**self.engine.debtor("D-1"), "age_days": None}, channel="voice")["body"])
        self.assertTrue(self.payload["message"].endswith(config.disclaimer()))
        self.assertEqual(self.payload["message"].count(config.disclaimer()), 1)

    def test_payload_is_written_to_out_for_evidence(self):
        path = self.outcome["payload_path"]
        self.assertTrue(path.endswith(".json"))
        with open(path, encoding="utf-8") as fh:
            saved = json.loads(fh.read())
        self.assertEqual(saved["claim"]["claim_token"], self.outcome["claim_token"])

    def test_mock_returns_a_call_id_and_echoes_the_authorization(self):
        response = self.outcome["provider_response"]
        self.assertEqual(self.outcome["status"], "SENT")
        self.assertTrue(response["call_id"].startswith("vapi-call-"))
        self.assertEqual(response["status"], "queued")
        self.assertEqual(response["simulated"], True)
        self.assertEqual(response["echo"]["claim_token"], self.outcome["claim_token"])
        self.assertEqual(response["echo"]["signature_reference"], self.signoff["signature_reference"])
        self.assertEqual(response["echo"]["disclaimer_occurrences"], 1)

    def test_claim_row_records_the_provider_response(self):
        row = self.claim_rows("D-1")[0]
        self.assertEqual(row["status"], "SENT")
        self.assertIn("vapi-call-", row["provider_response"])
        self.assertEqual(row["operating_mode_at_claim"], "full_auto")
        self.assertEqual(row["window_decision"], "In_Window")

    def test_mock_rejects_a_payload_without_authorization_or_claim(self):
        url = self.vendor_url + "/vapi/call"
        status, body = _post(url, {"contact_type": "voice", "destination_phone": "+12125550143",
                                   "debtor_id": "D-1", "client_id": "CLI-TEST",
                                   "message": "hello"}, expect_error=True)
        self.assertEqual(status, 422)
        self.assertIn("authorization", body["missing"])
        self.assertIn("claim", body["missing"])

    def test_email_goes_through_the_same_claim_and_the_same_scope(self):
        self.add_debtor("D-2", phone="(808) 555-0119", aging_state="90", balance=99.0)
        # out of window for voice, but email is not a windowed channel
        voice = self.engine.send("D-2", "voice")
        self.assertEqual(voice["decision"], "BLOCKED_OUTSIDE_WINDOW")
        email = self.engine.send("D-2", "email")
        self.assertEqual(email["decision"], CLAIMED)
        self.assertEqual(email["status"], "SENT")
        self.assertTrue(email["provider_response"]["message_id"].startswith("mock-mail-"))
        self.assertEqual(email["payload"]["contact_type"], "email")

    def test_handshake_document_exists_and_documents_the_fields_we_send(self):
        self.assertTrue(DOC.exists(), "docs/vapi-handshake.md must ship with the engine")
        text = DOC.read_text(encoding="utf-8")
        for key in ("contact_type", "destination_phone", "debtor_id", "client_id", "message",
                    "template_reference", "authorization.signature_reference", "claim.claim_token",
                    "window_decision", "vapi-call-"):
            self.assertIn(key, text, f"docs/vapi-handshake.md does not mention {key}")
        self.assertIn("cannot be recalled", text.lower())


if __name__ == "__main__":
    unittest.main()
