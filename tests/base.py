"""Shared test fixture.

Every test gets its own temporary store, its own artifact directories and its own
copy of the local mock vendor server on an ephemeral port - so tests never touch
data/recoverai.db or out/ and can run in any order.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from engine import clock, db
from engine.app import RecoverAI
from mocks.server import start_vendor_server, stop_server

PINNED_NOW = "2026-10-06T14:00:00Z"  # NY 10:00, Denver 08:00, Honolulu 04:00


class EngineTestCase(unittest.TestCase):
    NOW = PINNED_NOW

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="recoverai-test-"))
        self.paths = {name: self.tmp / name for name in ("drafts", "payloads", "email")}
        for path in self.paths.values():
            path.mkdir(parents=True, exist_ok=True)
        clock.set_fake_now(self.NOW)
        self.vendor = start_vendor_server(0)
        self.vendor_url = f"http://127.0.0.1:{self.vendor.server_address[1]}"
        self.db_path = self.tmp / "test.db"
        self.engine = self.new_engine()

    def new_engine(self) -> RecoverAI:
        """A second engine with its own connection to the same database file."""
        return RecoverAI(db_path=str(self.db_path), backend="sqlite", fresh=not self.db_path.exists(),
                         vendor_base_url=self.vendor_url,
                         audit_path=self.tmp / "guardrail_actions.jsonl", paths=self.paths)

    def tearDown(self) -> None:
        clock.set_fake_now(None)
        try:
            self.engine.store.close()
        except Exception:
            pass
        stop_server(self.vendor)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers -----------------------------------------------------------
    def add_debtor(self, debtor_id: str = "D-1", client_id: str = "CLI-TEST", phone: str = "(212) 555-0143",
                   email: str = "debtor@example.com", balance: float = 1000.0, aging_state: str = "60",
                   approval_status: str = "Pending", invoice_date: str = "2026-08-01") -> str:
        # The row carries the raw vendor row it was normalized from, exactly as a real import
        # leaves it: the reopen audit re-runs ingestion over this, so a fixture record without a
        # source would be a record the engine could never re-audit.
        raw = {"Cust_ID": debtor_id, "Cust_Name": f"Debtor {debtor_id}", "customer_email": email,
               "PhoneNumber": phone, "AmountDue": f"{balance:.2f}", "Inv_Date": invoice_date,
               "InvoiceNumber": f"INV-{debtor_id}", "Terms": "Net 30"}
        with self.engine.store.tx(immediate=True):
            self.engine.store.execute(
                """INSERT INTO debtor_records
                     (debtor_id, client_id, name, email, phone, balance_owed, invoice_date, aging_state,
                      approval_status, dnc_flag, invoice_number, source_system, source_ref, source_columns,
                      raw_record, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (debtor_id, client_id, f"Debtor {debtor_id}", email, phone, balance, invoice_date,
                 aging_state, approval_status, 0, f"INV-{debtor_id}", "test-fixture", "fixture://rows",
                 json.dumps({"Cust_ID": "debtor_id", "Cust_Name": "name", "customer_email": "email",
                             "PhoneNumber": "phone", "AmountDue": "balance_owed",
                             "Inv_Date": "invoice_date", "InvoiceNumber": "invoice_number"},
                            sort_keys=True),
                 json.dumps(raw, sort_keys=True), clock.iso()),
            )
        return debtor_id

    def sign_off(self, client_id: str = "CLI-TEST", channels=("email", "voice"), states=("30", "60", "90")):
        return self.engine.capture_signoff(client_id, "Test Signer", "CFO", list(channels), list(states),
                                           who="test")

    def attest_step3(self, client_id: str = "CLI-TEST", who: str = "test"):
        """Record the Step 3 warranty the intake now binds a batch to (Phase 4).

        Deliberately a separate call: a batch registered before its client attested is
        REFUSED, so every test that registers one has to walk this step first.
        """
        from engine import onboarding
        result = self.engine.record_step3_attestation(client_id, True, who, source="test")
        self.assertTrue(result["ok"], result)
        return result

    def force_mode(self, client_id: str, mode: str) -> None:
        """Write a mode straight into the store, bypassing switch_mode, to prove the claim
        transaction refuses a hand-edited mode row. Fails loudly if no row was updated."""
        self.engine.settings(client_id)  # ensure the row exists
        cur = self.engine.store.execute(
            "UPDATE client_autonomy_settings SET operating_mode = ? WHERE client_id = ?", (mode, client_id))
        self.assertEqual(cur.rowcount, 1, f"no client_autonomy_settings row for {client_id}")

    def contacts_for(self, debtor_id: str):
        return [c for c in self.engine.contacts() if c["debtor_id"] == debtor_id]

    def claim_rows(self, debtor_id: Optional[str] = None):
        rows = self.engine.contacts()
        rows = [r for r in rows if r["claim_token"]]
        return [r for r in rows if r["debtor_id"] == debtor_id] if debtor_id else rows

    def last_decision(self, debtor_id: str) -> str:
        rows = self.contacts_for(debtor_id)
        self.assertTrue(rows, f"no contact attempt logged for {debtor_id}")
        return rows[-1]["decision"]
