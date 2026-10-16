"""Step 2's channel utility rate sheet (MODEL 2, owner-confirmed).

Internal operational metrics - what one unit of each channel costs us to run - and NOT
client charges. The three rates and the exact label live in the rulebook; the console's
two reference payloads carry them, read at call time, so no view holds a rate literal.
"""
from __future__ import annotations

import inspect
import unittest

from engine import config
from tests.base import EngineTestCase

#: The label string the console must render VERBATIM, byte for byte.
RATE_LABEL = "Channel Utility Rates (Internal Operational Metrics, Not Client Charges)"

#: The three rates, exactly. email 0.00; sms_segment 0.05; voice_minute 0.20 (USD).
RATES = {"email": 0.00, "sms_segment": 0.05, "voice_minute": 0.20}
RATE_ORDER = ["email", "sms_segment", "voice_minute"]


class RateSheetTestCase(EngineTestCase):
    """The bridge ops, on a real engine, exactly as the console calls them."""

    def payloads(self):
        from tools import console_bridge  # noqa: PLC0415
        return (console_bridge.op_vocabulary(self.engine, {}),
                console_bridge.op_warranty(self.engine, {}))


class TestBothPayloadsCarryTheRateSheet(RateSheetTestCase):
    def test_the_vocabulary_payload_carries_the_three_exact_rates_and_the_exact_label(self):
        vocabulary, _ = self.payloads()
        block = vocabulary["channel_utility_rates"]
        self.assertEqual(block["label"], RATE_LABEL)
        self.assertEqual(block["rates"], RATES)
        self.assertEqual(block["order"], RATE_ORDER)

    def test_the_warranty_payload_carries_the_three_exact_rates_and_the_exact_label(self):
        _, warranty = self.payloads()
        block = warranty["channel_utility_rates"]
        self.assertEqual(block["label"], RATE_LABEL)
        self.assertEqual(block["rates"], RATES)
        self.assertEqual(
            {k: block["rates"][k] for k in RATE_ORDER},
            {"email": 0.00, "sms_segment": 0.05, "voice_minute": 0.20})

    def test_the_warranty_payload_still_carries_the_warranty_it_always_did(self):
        """The rate sheet rides ALONG WITH the warranty, it does not replace it."""
        _, warranty = self.payloads()
        self.assertEqual(warranty["document_version"], config.step3_warranty()["version"])
        self.assertEqual(warranty["text"], config.step3_warranty()["text"])
        self.assertEqual(len(warranty["text_sha256"]), 64)


class TestTheSheetIsSourcedFromConfig(RateSheetTestCase):
    def test_both_payloads_carry_exactly_what_the_config_accessor_returns(self):
        vocabulary, warranty = self.payloads()
        self.assertEqual(vocabulary["channel_utility_rates"], config.channel_utility_rates())
        self.assertEqual(warranty["channel_utility_rates"], config.channel_utility_rates())

    def test_a_config_change_propagates_into_both_payloads(self):
        """No rate is a literal in the view code: change the rulebook, change the payload."""
        book = config.load_rulebook()
        block = book["onboarding"]["step2_acknowledgement"]["channel_utility_rates"]
        original = dict(block["rates"])
        try:
            block["rates"]["email"] = 0.11
            vocabulary, warranty = self.payloads()
            self.assertEqual(vocabulary["channel_utility_rates"]["rates"]["email"], 0.11)
            self.assertEqual(warranty["channel_utility_rates"]["rates"]["email"], 0.11)
        finally:
            block["rates"].clear()
            block["rates"].update(original)
        vocabulary, warranty = self.payloads()
        self.assertEqual(vocabulary["channel_utility_rates"]["rates"]["email"], 0.00)
        self.assertEqual(warranty["channel_utility_rates"]["rates"], RATES)

    def test_the_rates_are_read_one_by_one_and_an_unknown_key_refuses(self):
        for key, value in RATES.items():
            self.assertEqual(config.channel_utility_rate(key), value)
        with self.assertRaises(KeyError):
            config.channel_utility_rate("carrier_pigeon")


class TestTheRateSheetIsNeverAClientCharge(RateSheetTestCase):
    def test_the_sheet_says_in_its_own_words_that_it_is_not_a_client_charge(self):
        block = config.channel_utility_rates()
        self.assertTrue(block["not_client_charges"])
        self.assertIn("MODEL_2", block["model"])
        self.assertIn("not client charges", block["plain_english"])
        self.assertIn("never appear on a client invoice", block["plain_english"])

    def test_the_billing_path_never_reads_the_rate_sheet(self):
        """MODEL 2 enforced structurally: money is only ever the 20% contingency fee."""
        from engine import payments  # noqa: PLC0415
        source = inspect.getsource(payments)
        self.assertNotIn("channel_utility_rate", source)
        self.assertNotIn("channel_utility_rates", source)


if __name__ == "__main__":
    unittest.main()
