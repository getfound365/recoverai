"""Agent 1: the translator. Two messy exports in, one normalized schema out."""
from __future__ import annotations

import json
import unittest

from engine import ingest
from mocks.server import MOCK_DATA
from tests.base import EngineTestCase


class TestNormalization(EngineTestCase):
    def test_quickbooks_export_is_fetched_over_http_and_translated(self):
        report = self.engine.import_ledger(self.vendor_url, [
            {"path": "/mock/quickbooks/invoices", "source_system": "QuickBooks Online (simulated)",
             "client_id": "CLI-NORTHWIND"}])[0]
        self.assertEqual(report["records_normalized"], 4)
        self.assertEqual(report["client_ids_seen"], ["CLI-NORTHWIND"])
        mapping = {m["source_column"]: m["target_field"] for m in report["mapping"]}
        self.assertEqual(mapping["Cust_ID"], "debtor_id")
        self.assertEqual(mapping["Cust_Name"], "name")
        self.assertEqual(mapping["customer_email"], "email")
        self.assertEqual(mapping["PhoneNumber"], "phone")
        self.assertEqual(mapping["AmountDue"], "balance_owed")
        self.assertEqual(mapping["Inv_Date"], "invoice_date")
        self.assertIsNone(mapping["Terms"], "unknown columns are reported as unmapped")
        self.assertIn("Terms", report["unmapped_columns"])
        row = self.engine.debtor("NW-1001")
        self.assertEqual(row["name"], "Marcus Whitfield")
        self.assertEqual(row["email"], "marcus.whitfield@example.com")
        self.assertEqual(row["phone"], "(212) 555-0143")
        self.assertEqual(row["balance_owed"], 4820.50)
        self.assertEqual(row["invoice_date"], "2026-06-15")
        self.assertEqual(row["aging_state"], "90")
        self.assertEqual(row["approval_status"], "Pending")
        source_columns = json.loads(row["source_columns"])
        self.assertEqual(source_columns["Cust_Name"], "name")

    def test_xero_export_uses_a_different_vocabulary_and_a_client_hint(self):
        report = self.engine.import_ledger(self.vendor_url, [
            {"path": "/mock/xero/invoices", "source_system": "Xero (simulated)",
             "client_id": "CLI-CEDAR"}])[0]
        mapping = {m["source_column"]: m["target_field"] for m in report["mapping"]}
        self.assertEqual(mapping["ContactID"], "debtor_id")
        self.assertEqual(mapping["ContactName"], "name")
        self.assertEqual(mapping["EmailAddress"], "email")
        self.assertEqual(mapping["Phone"], "phone")
        self.assertEqual(mapping["Balance"], "balance_owed")
        self.assertEqual(mapping["DueDate"], "invoice_date")
        self.assertIsNone(mapping["CurrencyCode"])
        row = self.engine.debtor("CE-2001")
        self.assertEqual(row["balance_owed"], 12340.00, "thousands separator must be parsed")
        self.assertEqual(row["client_id"], "CLI-CEDAR", "client id comes from the connection")
        self.assertEqual(row["aging_state"], "90")
        self.assertEqual(self.engine.debtor("CE-2002")["aging_state"], "30")
        # Xero's "Reference" is the invoice reference, not the debtor: it must reach the
        # notice (docs/schema.md: "Reference used in the notice"), not be dropped, and it
        # must not displace debtor_id, which ContactID already supplies.
        self.assertEqual(mapping["Reference"], "invoice_number")
        self.assertEqual(row["debtor_id"], "CE-2001")
        self.assertEqual(row["invoice_number"], "XR-5501")

    def test_aging_states_are_derived_from_invoice_date(self):
        self.engine.import_ledger(self.vendor_url, [
            {"path": "/mock/quickbooks/invoices", "source_system": "QuickBooks Online (simulated)",
             "client_id": "CLI-NORTHWIND"}])
        expected = {"NW-1001": "90", "NW-1002": "60", "NW-1003": "60"}
        for debtor_id, state in expected.items():
            self.assertEqual(self.engine.debtor(debtor_id)["aging_state"], state, debtor_id)

    def test_edge_cases_are_stored_explicitly(self):
        self.engine.import_ledger(self.vendor_url, [
            {"path": "/mock/quickbooks/invoices", "source_system": "QuickBooks Online (simulated)",
             "client_id": "CLI-NORTHWIND"}])
        unknown = self.engine.debtor("NW-1004")
        self.assertEqual(unknown["aging_state"], "unknown")
        self.assertIsNone(unknown["invoice_date"])
        self.assertIn("unparseable", unknown["aging_note"])
        self.assertIsNone(unknown["area_code"])
        self.assertEqual(unknown["window_decision"], "Unknown_Area_Code_Freeze")
        credit = self.engine.debtor("NW-1003")
        self.assertEqual(credit["balance_owed"], -320.75, "parenthesised values are credits")

    def test_aging_bucket_edges(self):
        from engine import config
        self.assertEqual(config.aging_state_for_days(0), "current")
        self.assertEqual(config.aging_state_for_days(29), "current")
        self.assertEqual(config.aging_state_for_days(30), "30")
        self.assertEqual(config.aging_state_for_days(59), "30")
        self.assertEqual(config.aging_state_for_days(60), "60")
        self.assertEqual(config.aging_state_for_days(89), "60")
        self.assertEqual(config.aging_state_for_days(90), "90")
        self.assertEqual(config.aging_state_for_days(4000), "90")

    def test_records_without_an_identifier_are_rejected_with_a_reason(self):
        result = ingest.normalize([{"Cust_Name": "No Identifier", "AmountDue": "10.00"}],
                                  source_system="Test", endpoint="/x", client_id_hint=None)
        self.assertEqual(result.records, [])
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("debtor_id", result.rejected[0]["reason"])
        self.assertIn("client_id", result.rejected[0]["reason"])

    def test_failed_fetch_raises_rather_than_silently_importing_nothing(self):
        with self.assertRaises(Exception):
            ingest.fetch_export(self.vendor_url, "/mock/not-a-route")

    def test_mock_export_files_are_the_source_the_server_serves(self):
        for name in ("quickbooks_invoices.json", "xero_invoices.json"):
            self.assertTrue((MOCK_DATA / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
