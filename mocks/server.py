"""Every third-party API in this prototype is simulated on localhost.

Two servers, one module:

  * the VENDOR mock (default 127.0.0.1:8801) - QuickBooks-style and Xero-style
    past-due invoice exports over HTTP, a Vapi-shaped POST /vapi/call and a local
    email sink. It talks to nothing but this machine.
  * the ENGINE API (default 127.0.0.1:8802) - /webhooks/inbound (the global DNC
    opt-out), /onboarding/signoff, /mode/switch and the review-queue endpoints. This
    is the surface the console will call in the next task.

Run both: python3 -m mocks.server

Readiness is explicit, never assumed: `start_vendor_server` / `start_engine_server` do not
return until the server has answered `GET /health` with 200, and they raise a loud
RuntimeError if it does not within `READY_TIMEOUT`. That removes the "server started but
not serving yet" race at its source. `stop_server` is bounded too - it never blocks
forever on a server that failed to start.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MOCK_DATA = HERE / "data"
OUT = ROOT / "out"

#: Every network wait in this module is bounded by one of these.
READY_TIMEOUT = 10.0   # seconds to wait for a freshly started mock server to answer /health
READY_POLL = 0.02      # seconds between readiness probes
SHUTDOWN_TIMEOUT = 5.0  # seconds to wait for a server to stop before closing the socket anyway


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Base(BaseHTTPRequestHandler):
    server_version = "RecoverAI-Mock/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet by default
        if getattr(self.server, "verbose", False):
            print(f"[mock] {self.address_string()} {fmt % args}")

    def _send(self, code: int, body: Dict[str, Any]) -> None:
        payload = json.dumps(body, indent=2, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    def _query(self) -> Dict[str, str]:
        raw = self.path.split("?", 1)[1] if "?" in self.path else ""
        return {k: v for k, v in (pair.split("=", 1) for pair in raw.split("&") if "=" in pair)}


# ---------------------------------------------------------------------------
# Vendor mock: QuickBooks, Xero, Vapi, email
# ---------------------------------------------------------------------------
class VendorHandler(_Base):
    def _export(self, filename: str) -> None:
        path = MOCK_DATA / filename
        if not path.exists():
            self._send(404, {"error": f"no mock export at {path}"})
            return
        self._send(200, json.loads(path.read_text(encoding="utf-8")))

    def do_GET(self):  # noqa: N802
        route = self.path.split("?")[0].rstrip("/")
        if route == "/health":
            self._send(200, {"status": "ok", "service": "recoverai-mock-vendor", "simulated": True,
                             "time": _now()})
        elif route == "/mock/quickbooks/invoices":
            self._export("quickbooks_invoices.json")
        elif route == "/mock/xero/invoices":
            self._export("xero_invoices.json")
        else:
            self._send(404, {"error": f"no such mock route {route}"})

    def do_POST(self):  # noqa: N802
        route = self.path.split("?")[0].rstrip("/")
        body = self._read_json()
        if route == "/vapi/call":
            self._vapi_call(body)
        elif route == "/mock/email/send":
            self._email_send(body)
        else:
            self._send(404, {"error": f"no such mock route {route}"})

    # Vapi-shaped voice/SMS handshake (simulated - no real call is placed)
    def _vapi_call(self, body: Dict[str, Any]) -> None:
        required = ["contact_type", "destination_phone", "debtor_id", "client_id", "message",
                    "authorization", "claim"]
        missing = [k for k in required if not body.get(k)]
        if body.get("authorization") and not body["authorization"].get("signature_reference"):
            missing.append("authorization.signature_reference")
        if body.get("claim") and not body["claim"].get("claim_token"):
            missing.append("claim.claim_token")
        if missing:
            self._send(422, {"error": "payload rejected by the Vapi mock", "missing": missing})
            return
        call_id = f"vapi-call-{uuid.uuid4().hex[:12]}"
        record = {
            "call_id": call_id,
            "status": "queued",
            "provider": "vapi-mock",
            "simulated": True,
            "received_at": _now(),
            "echo": {
                "contact_type": body["contact_type"],
                "destination_phone": body["destination_phone"],
                "debtor_id": body["debtor_id"],
                "client_id": body["client_id"],
                "template_reference": body.get("template_reference"),
                "signature_reference": body["authorization"]["signature_reference"],
                "claim_token": body["claim"]["claim_token"],
                "operating_mode_at_claim": body["claim"].get("operating_mode_at_claim"),
                "message_bytes": len(body["message"].encode("utf-8")),
                "disclaimer_occurrences":
                    body.get("disclaimer", {}).get("occurrences"),
            },
            "notes": "Local mock. No call was placed, no phone number was dialled, no message left.",
        }
        log_dir = OUT / "mock_vendor_calls"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{call_id}.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
        self._send(201, record)

    def _email_send(self, body: Dict[str, Any]) -> None:
        required = ["destination", "debtor_id", "client_id", "message", "authorization", "claim"]
        missing = [k for k in required if not body.get(k)]
        if missing:
            self._send(422, {"error": "payload rejected by the email mock", "missing": missing})
            return
        message_id = f"mock-mail-{uuid.uuid4().hex[:12]}"
        (OUT / "email_outbox").mkdir(parents=True, exist_ok=True)
        (OUT / "email_outbox" / f"{message_id}.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
        self._send(202, {
            "message_id": message_id,
            "status": "accepted",
            "provider": "local-email-mock",
            "simulated": True,
            "received_at": _now(),
            "echo": {
                "destination": body["destination"],
                "debtor_id": body["debtor_id"],
                "client_id": body["client_id"],
                "subject": body.get("subject"),
                "signature_reference": body["authorization"]["signature_reference"],
                "claim_token": body["claim"]["claim_token"],
                "disclaimer_occurrences": body.get("disclaimer", {}).get("occurrences"),
            },
            "notes": "Written to out/email_outbox. No mail server was contacted.",
        })


# ---------------------------------------------------------------------------
# Engine API: the endpoints the console will drive
# ---------------------------------------------------------------------------
class EngineHandler(_Base):
    engine = None  # set by start_engine_server

    def do_GET(self):  # noqa: N802
        route = self.path.split("?")[0].rstrip("/")
        if route == "/health":
            self._send(200, {"status": "ok", "service": "recoverai-engine", "time": _now(),
                             "backend": self.engine.store.backend})
        elif route == "/state":
            snap = self.engine.snapshot()
            self._send(200, {t: len(rows) for t, rows in snap.items()})
        elif route == "/timeline":
            self._send(200, {"timeline": self.engine.timeline()})
        elif route == "/contacts":
            self._send(200, {"contacts": self.engine.contacts()})
        elif route == "/ledger":
            client_id = self._query().get("client_id")
            if not client_id:
                self._send(400, {"error": "client_id is required"})
            else:
                self._send(200, self.engine.ledger(client_id))
        elif route == "/payments/events":
            self._send(200, {"payment_events": self.engine.payment_events()})
        elif route == "/adjustments/pending":
            self._send(200, {"pending_adjustments": self.engine.pending_adjustments()})
        else:
            self._send(404, {"error": f"no such route {route}"})

    def do_POST(self):  # noqa: N802
        route = self.path.split("?")[0].rstrip("/")
        body = self._read_json()
        try:
            if route == "/webhooks/inbound":
                self._send(200, self.engine.handle_inbound(body))
            elif route == "/onboarding/signoff":
                self._send(201, self.engine.capture_signoff(
                    client_id=body["client_id"], signer_name=body["signer_name"],
                    signer_title=body["signer_title"],
                    channels=body.get("channels"), aging_states=[str(a) for a in body["aging_states"]]
                    if body.get("aging_states") else None,
                    scope_pairs=body.get("scope_pairs"),
                    who=body.get("who")))
            elif route == "/onboarding/revoke":
                self._send(200, self.engine.revoke_signoff(
                    client_id=body["client_id"], who=body.get("who", "owner"), reason=body.get("reason", "")))
            elif route == "/mode/switch":
                self._send(200, self.engine.switch_mode(
                    client_id=body["client_id"], new_mode=body["new_mode"],
                    who=body.get("who", "client"), reason=body.get("reason", "")))
            elif route == "/review/queue":
                self._send(201, self.engine.queue_draft(body["debtor_id"], body.get("channel", "email")))
            elif route == "/review/approve":
                self._send(200, self.engine.approve(body["debtor_id"], body.get("who", "reviewer"),
                                                    body.get("channel")))
            elif route == "/review/reject":
                self._send(200, self.engine.reject(body["debtor_id"], body.get("who", "reviewer"),
                                                   body.get("reason", "")))
            elif route == "/send":
                self._send(200, self.engine.send(body["debtor_id"], body.get("channel", "voice"),
                                                 who=body.get("who", "orchestrator"),
                                                 reason=body.get("reason", "")))
            # --- the payment path: one event in, three consequences out ---
            elif route == "/payments/webhook":
                self._send(201, self.engine.record_payment(
                    debtor_id=body["debtor_id"], amount=body["amount"],
                    confirmation_channel="accounting_webhook",
                    external_reference=body.get("external_reference"),
                    signal_at=body.get("signal_at"), note=body.get("note")))
            elif route == "/payments/delta-sync":
                self._send(201, self.engine.record_payment(
                    debtor_id=body["debtor_id"], amount=body["amount"],
                    confirmation_channel="delta_sync",
                    external_reference=body.get("external_reference"),
                    signal_at=body.get("signal_at"), note=body.get("note")))
            elif route == "/payments/attest":
                self._send(201, self.engine.record_payment(
                    debtor_id=body["debtor_id"], amount=body["amount"],
                    confirmation_channel="manual_attestation",
                    attested_by=body.get("attested_by"),
                    external_reference=body.get("external_reference"),
                    signal_at=body.get("signal_at"), note=body.get("note")))
            elif route == "/payments/reverse":
                self._send(201, self.engine.record_reversal(
                    debtor_id=body["debtor_id"], kind=body.get("kind", "bounced"),
                    reverses_entry_id=body.get("reverses_entry_id"), amount=body.get("amount"),
                    confirmation_channel=body.get("confirmation_channel", "accounting_webhook"),
                    who=body.get("who"), external_reference=body.get("external_reference"),
                    signal_at=body.get("signal_at"), note=body.get("note")))
            elif route == "/balance/edit":
                self._send(201, self.engine.record_balance_edit(
                    debtor_id=body["debtor_id"], new_balance=body["new_balance"],
                    who=body.get("who", "client"), reason=body.get("reason", ""),
                    note=body.get("note")))
            elif route == "/balance/resolve":
                self._send(200, self.engine.resolve_balance_edit(
                    debtor_id=body["debtor_id"], resolution=body["resolution"],
                    who=body.get("who", "client"), reason=body.get("reason", ""),
                    amount=body.get("amount"),
                    confirmation_channel=body.get("confirmation_channel", "manual_attestation")))
            else:
                self._send(404, {"error": f"no such route {route}"})
        except KeyError as exc:
            self._send(400, {"error": f"missing field {exc}"})
        except (ValueError, PermissionError) as exc:
            self._send(409, {"error": str(exc)})


def base_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}"


def wait_until_ready(server: ThreadingHTTPServer, path: str = "/health",
                     timeout: float = READY_TIMEOUT, interval: float = READY_POLL) -> float:
    """Block until `server` answers 200 on `path`. Returns how long that took.

    Bounded and loud: every attempt has its own socket timeout, the whole loop is capped
    by `timeout`, and exhausting it raises rather than returning a half-trained server.
    """
    url = base_url(server) + path
    started = time.monotonic()
    deadline = started + timeout
    last_error = "no probe was attempted"
    while time.monotonic() < deadline:
        remaining = max(0.05, min(2.0, deadline - time.monotonic()))
        try:
            with urllib.request.urlopen(url, timeout=remaining) as resp:
                if 200 <= int(resp.status) < 300:
                    return time.monotonic() - started
                last_error = f"HTTP {resp.status}"
        except Exception as exc:  # connection refused, timeout, bad body - all retryable here
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(interval)
    raise RuntimeError(
        f"mock server {url} did not become ready within {timeout:.1f}s "
        f"(last probe error: {last_error}). Refusing to start with a server nobody has "
        "confirmed is listening: the first real call would hang or fail confusingly."
    )


def _serve(server: ThreadingHTTPServer, name: str) -> Thread:
    """Start serve_forever in a daemon thread, polling fast so shutdown() is prompt."""
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                    name=name, daemon=True)
    thread.start()
    return thread


def start_vendor_server(port: int = 8801, verbose: bool = False,
                        ready_timeout: float = READY_TIMEOUT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), VendorHandler)
    server.daemon_threads = True
    server.verbose = verbose  # type: ignore[attr-defined]
    server.serving_thread = _serve(server, "mock-vendor")  # type: ignore[attr-defined]
    try:
        server.ready_seconds = wait_until_ready(server, timeout=ready_timeout)  # type: ignore[attr-defined]
    except BaseException:
        stop_server(server)
        raise
    return server


def start_engine_server(engine, port: int = 8802, verbose: bool = False,
                        ready_timeout: float = READY_TIMEOUT) -> ThreadingHTTPServer:
    handler = type("BoundEngineHandler", (EngineHandler,), {"engine": engine})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server.verbose = verbose  # type: ignore[attr-defined]
    server.serving_thread = _serve(server, "engine-api")  # type: ignore[attr-defined]
    try:
        server.ready_seconds = wait_until_ready(server, timeout=ready_timeout)  # type: ignore[attr-defined]
    except BaseException:
        stop_server(server)
        raise
    return server


def stop_server(server: Optional[ThreadingHTTPServer], timeout: float = SHUTDOWN_TIMEOUT) -> None:
    """Stop a mock server without ever blocking forever.

    `shutdown()` returns once serve_forever's loop has exited; because every server here
    is confirmed ready before it is handed back, that loop is provably running. The wait
    is still bounded - if it somehow is not, we close the listening socket anyway and say
    so, rather than hanging the caller.
    """
    if server is None:
        return
    stopped = Event()

    def _shut_down() -> None:
        try:
            server.shutdown()
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[mock] shutdown of {base_url(server)} raised {type(exc).__name__}: {exc}",
                  file=sys.stderr)
        finally:
            stopped.set()

    Thread(target=_shut_down, name="mock-shutdown", daemon=True).start()
    if not stopped.wait(timeout):
        print(f"[mock] {base_url(server)} did not stop within {timeout:.1f}s; "
              "closing the listening socket anyway", file=sys.stderr)
    server.server_close()


def main() -> None:  # pragma: no cover - interactive helper
    from engine.app import RecoverAI

    engine = RecoverAI()
    engine.orchestrator.audit.record("server_started", note="local mocks only")
    vendor = start_vendor_server(8801, verbose=True)
    api = start_engine_server(engine, 8802, verbose=True)
    print("RecoverAI mocks running (nothing leaves this machine):")
    print(f"  vendor mock  {base_url(vendor)}/mock/quickbooks/invoices")
    print(f"  vendor mock  {base_url(vendor)}/mock/xero/invoices")
    print(f"  vapi mock    POST {base_url(vendor)}/vapi/call")
    print(f"  engine api   POST {base_url(api)}/webhooks/inbound")
    print("Both servers confirmed ready via GET /health before this line printed.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_server(vendor)
        stop_server(api)



if __name__ == "__main__":  # pragma: no cover
    main()
