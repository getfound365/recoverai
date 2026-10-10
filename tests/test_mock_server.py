"""The mock servers must be provably up before the first call, and never block forever.

These are the regression tests for the demo stall: a server handed back before it was
serving, and a shutdown that waited on nothing. Both used to be possible; neither is now.
"""
from __future__ import annotations

import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from engine import clock
from engine.app import RecoverAI
from engine.ingest import FETCH_TIMEOUT
from engine.orchestrator import DISPATCH_TIMEOUT
from mocks.server import (READY_TIMEOUT, SHUTDOWN_TIMEOUT, base_url, start_vendor_server,
                          stop_server, wait_until_ready)
from tests.base import EngineTestCase


def _free_port() -> int:
    """A port number nothing is listening on: bind, read the number, close."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestMockServerReadiness(EngineTestCase):
    def test_start_vendor_server_does_not_return_until_it_answers_health(self):
        # setUp already started one; start another so the handshake itself is under test.
        other = start_vendor_server(0)
        try:
            self.assertTrue(other.serving_thread.is_alive(), "serve_forever must be running")
            self.assertLess(other.ready_seconds, READY_TIMEOUT)
            with urllib.request.urlopen(base_url(other) + "/health", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            stop_server(other)

    def test_the_fixture_server_is_ready_before_any_test_uses_it(self):
        with urllib.request.urlopen(base_url(self.vendor) + "/health", timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_wait_until_ready_raises_loudly_and_boundedly_on_a_dead_port(self):
        server = type("Fake", (), {"server_address": ("127.0.0.1", _free_port())})()
        started = time.monotonic()
        with self.assertRaises(RuntimeError) as caught:
            wait_until_ready(server, timeout=0.6, interval=0.05)
        elapsed = time.monotonic() - started
        message = str(caught.exception)
        self.assertIn("did not become ready", message)
        self.assertIn("/health", message)
        self.assertLess(elapsed, 5.0, "the readiness wait must be bounded, not open-ended")

    def test_a_server_that_cannot_bind_raises_instead_of_hanging(self):
        with self.assertRaises(OSError):
            start_vendor_server(self.vendor.server_address[1])  # port already taken

    def test_stop_server_returns_promptly_and_is_idempotent(self):
        other = start_vendor_server(0)
        started = time.monotonic()
        stop_server(other)
        stop_server(other)  # closing twice must not raise or hang
        self.assertLess(time.monotonic() - started, SHUTDOWN_TIMEOUT)
        with self.assertRaises((urllib.error.URLError, OSError)):
            urllib.request.urlopen(base_url(other) + "/health", timeout=1)


class TestEveryNetworkWaitIsBounded(unittest.TestCase):
    def test_the_two_timeouts_are_finite_and_named(self):
        for value in (FETCH_TIMEOUT, DISPATCH_TIMEOUT):
            self.assertGreater(value, 0)
            self.assertLessEqual(value, 60)

    def test_a_dead_provider_fails_the_dispatch_loudly_instead_of_hanging(self):
        """The whole send path, against a vendor URL where nothing is listening."""
        tmp = Path(tempfile.mkdtemp(prefix="recoverai-dead-provider-"))
        clock.set_fake_now("2026-10-06T14:00:00Z")
        engine = RecoverAI(db_path=str(tmp / "t.db"), backend="sqlite", fresh=True,
                           vendor_base_url=f"http://127.0.0.1:{_free_port()}",
                           audit_path=tmp / "audit.jsonl",
                           paths={"drafts": tmp / "d", "payloads": tmp / "p", "email": tmp / "e"})
        try:
            with engine.store.tx(immediate=True):
                engine.store.execute(
                    """INSERT INTO debtor_records
                         (debtor_id, client_id, name, email, phone, balance_owed, invoice_date,
                          aging_state, approval_status, dnc_flag, invoice_number, source_system,
                          imported_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("D-1", "CLI-TEST", "Debtor", "d@example.com", "(212) 555-0143", 1000.0,
                     "2026-08-01", "60", "Approved", 0, "INV-1", "test", clock.iso()))
            started = time.monotonic()
            outcome = engine.send("D-1", "voice", who="test", reason="dead provider")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 40.0, "a dead provider must not stall the send path")
            # The claim committed and was authenticated before the POST; what failed is the
            # dispatch, and that is recorded as FAILED with the error, never swallowed.
            self.assertTrue(outcome["claimed"])
            self.assertEqual(outcome["status"], "FAILED")
            self.assertIsNotNone(outcome["dispatch_error"])
            rows = engine.store.query("SELECT * FROM outbound_contact_logs")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "FAILED")
            self.assertTrue(rows[0]["claim_token"], "the claim itself still committed")
            self.assertTrue(rows[0]["provider_response"], "the failure is recorded on the claim")
        finally:
            engine.close()
            clock.set_fake_now(None)


if __name__ == "__main__":
    unittest.main()
