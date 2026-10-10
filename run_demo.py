#!/usr/bin/env python3
"""RecoverAI demo - one command, one full cycle, one readable transcript.

    cd /home/team/shared/recoverai && python3 run_demo.py

What it does, in order:
  1. starts the local mock QuickBooks/Xero/Vapi/email servers and the engine API, and
     does not go any further until both have answered GET /health (readiness is proven,
     never assumed - see mocks/server.py)
  2. Agent 1 fetches both exports over HTTP and normalizes them (mapping report printed)
  3. runs nine scripted scenarios against the real store:
       S0  ledger ingestion: both exports fetched over HTTP and translated
       S1  hitl: draft queues as Pending, a human approves, the contact fires
       S2  out-of-window: the run freezes, approval_status = Outside_Calling_Window
       S3  inbound STOP: dnc_flag = TRUE and every pending run for that debtor is killed
       S4  full_auto refused for a client with no sign-off (mode stays hitl)
       S5  full_auto permitted after a valid sign-off, subject to the sign-off's scope
       S6  a flip to hitl halts pending work; revoking the sign-off keeps it there
       S7  one payment event, three consequences, one transaction (webhook over HTTP)
       S8  a bounced payment un-bills and reopens into a mandatory audit (no resume);
           an unconfirmed balance edit parks the record
       S9  the beta-to-full-active gate, rendered from its own evidence records
  4. writes every artifact to out/ and the whole transcript to out/run_log.txt

Every scenario is named up front in DEMO_DEBTORS and checked against what the imports
actually produced before the first scenario runs, so a mismatch fails immediately and
loudly instead of half-way through the transcript. If a scenario does fail, the transcript
is still saved, with a FAILED section explaining where the run stopped, and the exit code
is non-zero: the log is never left silently truncated.

The clock is pinned (RECOVERAI_FAKE_NOW, default 2026-10-06T14:00:00Z) so the demo shows
an in-window contact and an out-of-window freeze in the same run, every run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import textwrap
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import clock, config, db, ingest, onboarding, orchestrator, payments, timezones  # noqa: E402
from engine.app import RecoverAI  # noqa: E402
from mocks.server import start_engine_server, start_vendor_server, stop_server  # noqa: E402

DEFAULT_NOW = "2026-10-06T14:00:00Z"

#: Every debtor the scenarios below name. Checked against the real import before S0 ends.
DEMO_DEBTORS = ("NW-1001", "NW-1002", "NW-1003", "NW-1004",
                "CE-2001", "CE-2002", "CE-2003", "CE-2004")

#: The partial payment S7 uses, and why this odd number: 1820.53 x 20% is 364.106, so the
#: half-up-to-the-cent rule has to round it up to 364.11. The rounding rule is visible in
#: the run instead of being a claim about the code.
PARTIAL_PAYMENT = 1820.53
PARTIAL_REFERENCE = "QBO-PMT-7731"

#: Artifacts the run must leave behind. Verified at the end and reported per file.
EXPECTED_ARTIFACTS = ("run_log.txt", "guardrail_actions.jsonl", "scheduled_runs.json",
                      "db_state.json", "summary.json",
                      "payments/ledger_CLI-NORTHWIND.json", "payments/payment_events.json",
                      "payments/queue_purge.json", "payments/queue_state_after_purge.json")

#: The marker every artifact this run writes carries, IN THE ARTIFACT ITSELF. A per-file
#: reader - or a prospect shown one file - must never be able to mistake a simulated
#: figure for collected money, so the label travels with the file and not only with the
#: transcript that produced it.
SIMULATION_LABEL = {
    "simulated": True,
    "simulation_notice": (
        "SIMULATED END TO END. This file was written by the RecoverAI sandbox demo from "
        "mock exports and simulated provider calls. No real money moved, no real email, "
        "call or message was sent, and no live accounting or payment integration exists. "
        "RecoverAI has collected $0 to date: every amount in this file is a simulated "
        "figure, not money received."),
    "money_collected_to_date_usd": 0.0,
    "statement_status": "DRAFT",
    "statement_draft_reason": ("a statement stays DRAFT until real, reconciled collections "
                               "exist; there are none"),
}


def labelled(payload):
    """The artifact payload with the simulated-money marker attached, first."""
    marked = dict(payload) if isinstance(payload, dict) else {"payload": payload}
    marked.update(SIMULATION_LABEL)
    return marked


def label_directory(path: Path) -> Path:
    """Put the marker next to artifacts whose bytes are parity-checked elsewhere.

    The stored Vapi payload must stay byte-identical to the payload that was sent, and the
    mock provider's own call log and email outbox are the machine's records of what it was
    asked to do, so those directories get the marker as a file of their own instead of an
    extra key inside every record.
    """
    path.mkdir(parents=True, exist_ok=True)
    marker = path / "_SIMULATED.txt"
    marker.write_text(
        "SIMULATED: every file in this directory was produced by the RecoverAI sandbox "
        "demo. No real money moved, no real email, call or message was sent, and RecoverAI "
        "has collected $0 to date.\n", encoding="utf-8")
    return marker


class DemoFailure(RuntimeError):
    """A scenario could not run. Loud, and never a silently truncated transcript."""



class Transcript:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lines: list[str] = []

    def write(self, text: str = "") -> None:
        self.lines.append(text)
        print(text)
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")

    def rule(self, char: str = "-", width: int = 100) -> None:
        self.write(char * width)

    def section(self, title: str) -> None:
        self.write("")
        self.rule("=")
        self.write(f"== {title}")
        self.rule("=")

    def step(self, title: str) -> None:
        self.write("")
        self.write(f"--- {title} " + "-" * max(0, 96 - len(title)))

    def kv(self, label: str, value) -> None:
        self.write(f"  {label:<34} {value}")

    def json_block(self, label: str, obj) -> None:
        self.write(f"  {label}")
        for line in json.dumps(obj, indent=2, default=str).splitlines():
            self.write(f"    {line}")

    def save(self) -> None:
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def clean_out(out: Path, keep: bool) -> None:
    for sub in ("drafts", "vapi_payloads", "email_outbox", "mock_vendor_calls", "payments"):
        target = out / sub
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
    for name in ("run_log.txt", "guardrail_actions.jsonl", "scheduled_runs.json", "db_state.json",
                 "summary.json"):
        f = out / name
        if f.exists() and not keep:
            f.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the RecoverAI end-to-end demo")
    parser.add_argument("--now", default=os.environ.get("RECOVERAI_FAKE_NOW", DEFAULT_NOW),
                        help="UTC instant the run pretends it is (keeps the demo deterministic)")
    parser.add_argument("--vendor-port", type=int, default=int(os.environ.get("RECOVERAI_VENDOR_PORT", 8801)))
    parser.add_argument("--api-port", type=int, default=int(os.environ.get("RECOVERAI_API_PORT", 8802)))
    parser.add_argument("--keep-out", action="store_true", help="do not clear previous run artifacts")
    args = parser.parse_args()

    out = ROOT / "out"
    out.mkdir(parents=True, exist_ok=True)
    clean_out(out, keep=args.keep_out)
    clock.set_fake_now(args.now)
    db.ensure_dirs()

    vendor_base = f"http://127.0.0.1:{args.vendor_port}"
    engine = RecoverAI(fresh=True, vendor_base_url=vendor_base, audit_path=out / "guardrail_actions.jsonl")

    # Both start functions block until the server has answered GET /health, and raise a
    # loud RuntimeError if it never does. Nothing below can race a half-started server.
    vendor = start_vendor_server(args.vendor_port)
    api = start_engine_server(engine, args.api_port)

    t = Transcript(out / "run_log.txt")
    t.write("RecoverAI - accounts-receivable pipeline demo (sandbox prototype)")
    t.write(f"run started           : {clock.iso()}")
    t.write(f"pinned UTC clock      : {args.now}  (RECOVERAI_FAKE_NOW - the demo is deterministic)")
    t.write(f"store                 : {engine.store.backend} at {engine.store.path}")
    t.write(f"rulebook              : config/rulebook.json v{config.load_rulebook()['rulebook_version']}")
    t.write(f"mock vendor server    : {vendor_base}   (QuickBooks/Xero/Vapi/email - all local)")
    t.write(f"engine API server     : http://127.0.0.1:{args.api_port}   "
            f"(/webhooks/inbound, /onboarding/*, /mode/switch)")
    t.write(f"server readiness      : vendor HTTP 200 on /health after "
            f"{getattr(vendor, 'ready_seconds', 0.0) * 1000:.1f} ms, "
            f"engine HTTP 200 on /health after {getattr(api, 'ready_seconds', 0.0) * 1000:.1f} ms")
    t.write(f"network waits         : every fetch and POST is bounded "
            f"({ingest.FETCH_TIMEOUT:.0f}s GET, {orchestrator.DISPATCH_TIMEOUT:.0f}s POST); "
            f"a timeout fails loudly and never hangs")
    t.write("")
    t.write("NOTHING HERE IS REAL: no call is placed, no email is sent, no money moves, no")
    t.write("third-party API is contacted. Both servers are in this process, on localhost.")

    failure: str | None = None
    api_base = f"http://127.0.0.1:{args.api_port}"
    try:
        scenario_1_import(t, engine, vendor_base)
        preflight(t, engine)
        scenario_2_hitl_approve_and_fire(t, engine)
        scenario_3_out_of_window_freeze(t, engine)
        scenario_4_inbound_stop(t, engine)
        scenario_5_full_auto_refused(t, engine)
        scenario_6_signoff_and_full_auto(t, engine)
        scenario_7_mode_flip_and_revocation(t, engine)
        scenario_8_payment_loop(t, engine, api_base)
        scenario_9_reversal_and_balance_edits(t, engine, api_base)
        scenario_10_gate(t, engine)
        closing_state(t, engine)
    except DemoFailure as exc:
        failure = f"DemoFailure: {exc}"
        _report_failure(t, failure, exc)
    except BaseException as exc:  # never leave a half-written transcript unexplained
        failure = f"{type(exc).__name__}: {exc}"
        _report_failure(t, failure, exc)
    finally:
        engine.close()
        stop_server(vendor)
        stop_server(api)
        t.save()

    if failure:
        print(f"\nDEMO FAILED: {failure}", file=sys.stderr)
        print(f"Partial transcript (with the failure recorded) at {t.path}", file=sys.stderr)
        return 1

    print(f"\nTranscript written to {t.path}")
    return 0


def _report_failure(t: "Transcript", failure: str, exc: BaseException) -> None:
    """Record where the run stopped, in the transcript itself, so it is never a mystery."""
    t.section("DEMO FAILED")
    t.write(f"  the run stopped here : {failure}")
    t.write("  the traceback below is recorded instead of being lost to the terminal:")
    for line in traceback.format_exc().rstrip().splitlines():
        t.write(f"    {line}")


def preflight(t: "Transcript", engine: RecoverAI) -> None:
    """Fail immediately and loudly if an import did not produce a debtor a scenario needs."""
    present = {r["debtor_id"] for r in engine.store.query("SELECT debtor_id FROM debtor_records")}
    missing = [d for d in DEMO_DEBTORS if d not in present]
    t.step("preflight - every debtor the scenarios name exists after the imports")
    t.kv("debtors required by the scenarios", ", ".join(DEMO_DEBTORS))
    t.kv("debtors the imports produced", ", ".join(sorted(present)))
    t.kv("missing", missing or "none")
    if missing:
        raise DemoFailure(
            f"the mock exports did not yield these debtor ids, which the scenarios need: "
            f"{missing}. Produced instead: {sorted(present)}. Check mocks/data/*.json "
            f"against DEMO_DEBTORS in run_demo.py."
        )


# ---------------------------------------------------------------------------
def scenario_1_import(t: Transcript, engine: RecoverAI, vendor_base: str) -> None:
    t.section("S0 - Agent 1: ledger ingestion (real HTTP fetch, messy columns)")
    t.write("Agent 1 does not read a local file: it fetches the client's export over HTTP from")
    t.write("the mock vendor, then translates every source column into the normalized schema.")
    reports = engine.import_ledger(vendor_base, [
        {"path": "/mock/quickbooks/invoices", "source_system": "QuickBooks Online (simulated)",
         "client_id": "CLI-NORTHWIND"},
        {"path": "/mock/xero/invoices", "source_system": "Xero (simulated)", "client_id": "CLI-CEDAR"},
    ])
    for rep in reports:
        t.step(f"{rep['source_system']} <- {rep['endpoint']}")
        t.kv("records fetched / normalized", f"{rep['records_fetched']} / {rep['records_normalized']}")
        t.kv("records rejected", rep["records_rejected"])
        t.kv("client ids seen", rep["client_ids_seen"])
        t.kv("aging buckets", rep["aging_buckets"])
        t.kv("summary line", rep["plain_english"])
        t.write("  column mapping actually used:")
        for row in rep["mapping"]:
            arrow = row["target_field"] or "UNMAPPED (kept verbatim in raw_record)"
            t.write(f"    {row['source_column']:<18} -> {arrow}")
    t.write("")
    t.write("Normalized + persisted records (aging_state derived from invoice_date):")
    t.write(f"  {'debtor_id':<10} {'client':<14} {'aging':<8} {'balance':>10}  {'phone':<18} "
            f"{'local time':<12} window decision")
    for row in engine.store.query("SELECT * FROM debtor_records ORDER BY debtor_id"):
        t.write(f"  {row['debtor_id']:<10} {row['client_id']:<14} {str(row['aging_state']):<8} "
                f"{row['balance_owed'] if row['balance_owed'] is not None else 'n/a':>10}  "
                f"{str(row['phone']):<18} {str(_local_clock(row)):<12} {row['window_decision']}")
    t.write("")
    t.write("Edge cases are stored explicitly, never guessed. Both exports contain one: NW-1004")
    t.write("at Northwind and CE-2004 at Cedar Ridge each have an invoice date that will not")
    t.write("parse (aging_state = unknown) and a phone number with no resolvable NANP area code,")
    t.write("so their window decision is the fail-closed Unknown_Area_Code_Freeze. NW-1003 carries")
    t.write("a credit balance, which is stored as a negative amount and never demanded.")


def _local_clock(row) -> str:
    decision = timezones.window_decision_for(row["phone"])
    return decision.get("local_clock") or "unknown"


def scenario_2_hitl_approve_and_fire(t: Transcript, engine: RecoverAI) -> None:
    t.section("S1 - hitl (default): a draft queues as Pending, a human approves, it fires")
    t.write("CLI-NORTHWIND is in the default mode, hitl. Nothing fires until a human clicks Approve.")
    t.step("queue the draft (Agent 2 renders it; Agent 3 holds it in Pending)")
    draft = engine.dispatch_new_draft("NW-1001", "voice", who="orchestrator")
    t.kv("decision", draft["decision"])
    t.kv("draft file", draft.get("draft_path"))
    t.kv("approval_status", engine.debtor("NW-1001")["approval_status"])

    t.step("try to fire it anyway, with no human approval")
    blocked = engine.send("NW-1001", "voice", who="orchestrator", reason="attempt before approval")
    t.kv("decision", blocked["decision"])
    t.kv("claimed?", blocked["claimed"])
    t.kv("reason", blocked["reason_detail"])

    t.step("human clicks Approve in the review queue, then the contact fires")
    t.kv("approve", engine.approve("NW-1001", who="reviewer@northwind (human)")["approval_status"])
    fired = engine.send("NW-1001", "voice", who="reviewer@northwind (human)", reason="approved in review queue")
    t.kv("decision", fired["decision"])
    t.kv("contact_log_id / claim_token", f"{fired['contact_log_id']} / {fired['claim_token']}")
    t.kv("mode at claim / mode version", f"{fired['operating_mode_at_claim']} / {fired['mode_version_at_claim']}")
    t.kv("window decision / local time", f"{fired['window_decision']} / {fired['derived_local_time']}")
    t.kv("provider response status", fired["provider_response"].get("status"))
    t.kv("payload file", fired["payload_path"])
    t.write("  the rendered notice that was sent (disclaimer is the last content, verbatim):")
    for line in fired["payload"]["message"].splitlines()[-4:]:
        t.write(f"    | {line}")
    t.kv("disclaimer occurrences", fired["payload"]["disclaimer"]["occurrences"])
    t.write("  full Vapi payload (exactly this JSON is POSTed to the mock):")
    t.json_block("POST /vapi/call", fired["payload"])


def scenario_3_out_of_window_freeze(t: Transcript, engine: RecoverAI) -> None:
    t.section("S2 - calling window: the Hawaii debtor is frozen, not called")
    t.write("NW-1002's area code (808) puts the debtor in Pacific/Honolulu. At the pinned instant")
    t.write("of 14:00 UTC that is 04:00 local - outside the 08:00-21:00 window - so the run freezes.")
    frozen = engine.send("NW-1002", "voice", who="orchestrator", reason="in-window check")
    t.kv("decision", frozen["decision"])
    t.kv("claimed?", frozen["claimed"])
    t.kv("derived local time", frozen["derived_local_time"])
    t.kv("window decision", frozen["window_decision"])
    t.kv("reason", frozen["reason_detail"])
    t.kv("debtor approval_status now", engine.debtor("NW-1002")["approval_status"])
    t.write("")
    t.write("The window is re-derived inside the claim transaction, so a stale in-window reading")
    t.write("cannot be exploited: the decision is part of the same row write as the send claim.")
    t.write("A second attempt while out of window:")
    again = engine.send("NW-1002", "voice", who="orchestrator", reason="second attempt")
    t.kv("decision", again["decision"])
    t.kv("claims written so far for NW-1002",
         len([c for c in engine.contacts() if c["debtor_id"] == "NW-1002" and c["claim_token"]]))


def scenario_4_inbound_stop(t: Transcript, engine: RecoverAI) -> None:
    t.section("S3 - global DNC opt-out: an inbound 'STOP' kills everything for that debtor")
    t.write("A scheduled follow-up exists for NW-1002 before the reply arrives:")
    run = engine.schedule("NW-1002", "voice", due_at=clock.iso(clock.plus(days=2)), reason="follow-up")
    t.kv("scheduled run", f"{run['run_id']} due {run['due_at']}")
    t.write("")
    t.write(f"POST http://127.0.0.1:8802/webhooks/inbound")
    payload = {"debtor_id": "NW-1002", "from": "+18085550119", "channel": "sms",
               "content": "Please STOP calling me. Unsubscribe.", "received_at": clock.iso()}
    t.json_block("inbound payload", payload)
    result = engine.handle_inbound(payload)
    t.kv("matched keywords", result["matched_keywords"])
    t.kv("dnc_flag set", result["dnc_set"])
    t.kv("runs cancelled", result["runs_cancelled"])
    t.kv("dnc_set_at", result["dnc_set_at"])
    t.write("")
    t.write("The kill is recorded and the claim path is closed for this debtor. Even a scheduled")
    t.write("run that survived cancellation cannot claim, because dnc_flag is re-read inside the")
    t.write("claim transaction - the DNC check is the first check in that transaction.")
    killed = engine.send("NW-1002", "voice", who="scheduler", reason="post-opt-out attempt")
    t.kv("attempted send decision", killed["decision"])
    t.kv("claimed?", killed["claimed"])
    t.kv("reason", killed["reason_detail"])
    t.kv("debtor dnc_flag", engine.debtor("NW-1002")["dnc_flag"])


def scenario_5_full_auto_refused(t: Transcript, engine: RecoverAI) -> None:
    t.section("S4 - full_auto is refused for a client with no sign-off")
    t.write("CLI-CEDAR has never signed an Automation Authorization Sign-Off Record. The client")
    t.write("tries to opt in anyway.")
    result = engine.switch_mode("CLI-CEDAR", "full_auto", who="ops@cedar-ridge (client)",
                                reason="wants to switch on automatic sending")
    t.kv("requested", "full_auto")
    t.kv("changed?", result["changed"])
    t.kv("refused?", result["refused"])
    t.kv("mode now", result["new_mode"])
    t.kv("reason", result["reason_detail"])
    t.write("")
    t.write("The refusal is appended to the immutable timeline (new_mode is the mode actually in")
    t.write("force after the attempt - the timeline never claims a switch that did not happen):")
    for row in engine.timeline("CLI-CEDAR"):
        t.write(f"  #{row['history_id']} {row['timestamp']} {row['old_mode']} -> {row['new_mode']} "
                f"by {row['who']}: {row['reason']}")


def scenario_6_signoff_and_full_auto(t: Transcript, engine: RecoverAI) -> None:
    t.section("S5 - digital sign-off, then full_auto is permitted (inside its scope)")
    t.write("The onboarding back end captures the sign-off: authorization document version, typed")
    t.write("signer name and corporate title, and an encrypted-hash signature reference.")
    signoff = engine.capture_signoff(
        client_id="CLI-CEDAR",
        signer_name="Dana Whitmore",
        signer_title="Chief Financial Officer",
        scope_pairs=[("email", "30"), ("email", "60"), ("email", "90"), ("voice", "90")],
        who="dana.whitmore@cedar-ridge (CFO)",
    )
    t.kv("signature_reference", signoff["signature_reference"])
    t.kv("hash inputs", "sha256(client_id + signer_name + signer_title + document_version + timestamp)")
    t.kv("document version", signoff["authorization_document_version"])
    t.kv("scope (channels)", signoff["authorized_channels"])
    t.kv("scope (aging states)", signoff["authorized_aging_states"])
    t.kv("plain-English scope", signoff["plain_english_scope"])
    t.kv("mode right after signing", signoff["operating_mode_after_signoff"])
    t.write("")
    t.write("Signing does not arm anything: the client is still in hitl until it chooses full_auto.")
    switch = engine.switch_mode("CLI-CEDAR", "full_auto", who="dana.whitmore@cedar-ridge (CFO)",
                                reason="sign-off on file, enabling automatic sending")
    t.kv("mode switch", f"{switch['old_mode']} -> {switch['new_mode']} (changed={switch['changed']}, "
                        f"mode_version={switch['mode_version']})")

    t.step("CE-2001 (Denver, 90+ days, 08:00 local - the window's inclusive edge) fires on generation")
    outcome = engine.dispatch_new_draft("CE-2001", "voice", who="orchestrator")
    t.kv("decision", outcome["decision"])
    t.kv("derived local time", f"{outcome['derived_local_time']} ({outcome['window_decision']})")
    t.kv("mode at claim / version", f"{outcome['operating_mode_at_claim']} / {outcome['mode_version_at_claim']}")
    t.kv("signature reference on the contact", outcome["payload"]["authorization"]["signature_reference"])
    t.kv("provider response", outcome["provider_response"].get("call_id"))

    t.step("CE-2002 (Austin, 30+ days) - the sign-off scope permits email, not voice")
    scope_ok = engine.dispatch_new_draft("CE-2002", "email", who="orchestrator")
    t.kv("email decision", scope_ok["decision"])
    t.kv("email provider response", scope_ok["provider_response"].get("message_id"))
    scope_blocked = engine.send("CE-2002", "voice", who="orchestrator", reason="voice attempt for a 30-day invoice")
    t.kv("voice decision", scope_blocked["decision"])
    t.kv("voice reason", scope_blocked["reason_detail"])

    t.step("CE-2004 (unparseable date, no resolvable area code) - fail closed even in full_auto")
    edge = engine.dispatch_new_draft("CE-2004", "voice", who="orchestrator")
    t.kv("decision", edge["decision"])
    t.kv("window decision", edge["window_decision"])
    t.kv("reason", edge["reason_detail"])
    t.kv("approval_status", engine.debtor("CE-2004")["approval_status"])

    t.step("NW-1003 (credit balance on the account) - nothing to collect, never contacted")
    credit = engine.send("NW-1003", "email", who="orchestrator", reason="credit balance")
    t.kv("decision", credit["decision"])
    t.kv("reason", credit["reason_detail"])


def scenario_7_mode_flip_and_revocation(t: Transcript, engine: RecoverAI) -> None:
    t.section("S6 - flipping to hitl halts pending work; revoking the sign-off keeps it there")
    t.write("While CLI-CEDAR is in full_auto, a follow-up for CE-2003 is scheduled and a draft is")
    t.write("queued. Then the client flips back to hitl.")
    run = engine.schedule("CE-2003", "voice", due_at=clock.iso(clock.plus(minutes=2)),
                          reason="follow-up in two minutes")
    draft = engine.queue_draft("CE-2003", "voice")
    t.kv("scheduled run", run["run_id"])
    t.kv("queued draft decision", f"PENDING - approval_status = {engine.debtor('CE-2003')['approval_status']}")
    t.kv("queued draft file", draft["path"])

    flip = engine.switch_mode("CLI-CEDAR", "hitl", who="dana.whitmore@cedar-ridge (CFO)",
                              reason="preparing for the owner's review, halting automation now")
    t.kv("mode switch", f"{flip['old_mode']} -> {flip['new_mode']} (mode_version={flip['mode_version']})")
    t.kv("halted runs", flip.get("halted_runs"))
    t.kv("scheduler now sees", engine.queue.summary())
    t.write("")
    t.write("The scheduler runs (nothing is due, and the halted run is never resurrected):")
    t.kv("run_due() results", engine.run_due(who="scheduler"))
    t.write("And an explicit attempt at the pending draft is refused by the claim transaction:")
    halted = engine.send("CE-2003", "voice", who="orchestrator", reason="attempt after halting flip")
    t.kv("decision", halted["decision"])
    t.kv("claimed?", halted["claimed"])
    t.kv("mode at claim", halted["operating_mode_at_claim"])
    t.kv("reason", halted["reason_detail"])

    t.step("the sign-off is revoked - the client stays in hitl and cannot re-arm")
    revoked = engine.revoke_signoff("CLI-CEDAR", who="dana.whitmore@cedar-ridge (CFO)",
                                    reason="ending the pilot")
    t.kv("dropped to hitl?", revoked["dropped_to_hitl"])
    t.kv("mode", f"{revoked['old_mode']} -> {revoked['new_mode']}")
    retry = engine.switch_mode("CLI-CEDAR", "full_auto", who="ops@cedar-ridge (client)",
                               reason="trying to re-arm after revocation")
    t.kv("re-arm refused?", retry["refused"])
    t.kv("reason", retry["reason_detail"])


def _post_json(url: str, payload: dict) -> tuple:
    """POST real JSON over HTTP to a local mock endpoint. Bounded wait, loud failure."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "RecoverAI-Demo/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _get_json(url: str) -> tuple:
    try:
        with urllib.request.urlopen(url, timeout=10.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _row_hash(row) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def scenario_8_payment_loop(t: Transcript, engine: RecoverAI, api_base: str) -> None:
    t.section("S7 - one payment event, three consequences, one transaction")
    t.write("NW-1001 owes Northwind 4820.50 and has been contacted (S1). A scheduled follow-up is")
    t.write("queued. The debtor pays part of the invoice into the client's own bank account - we")
    t.write("never touch the money; the accounting webhook tells us the balance dropped.")
    t.step("the pending work that exists before the payment signal")
    run = engine.schedule("NW-1001", "voice", due_at=clock.iso(clock.plus(days=1)),
                          reason="follow-up in one day")
    t.kv("scheduled run", f"{run['run_id']} due {run['due_at']}")
    t.kv("active queue", engine.queue.summary())
    t.kv("balance_owed / account_state",
         f"{engine.debtor('NW-1001')['balance_owed']} / {engine.debtor('NW-1001')['account_state']}")
    t.kv("outbound_contact_logs rows for this debtor", len([c for c in engine.contacts()
                                                            if c["debtor_id"] == "NW-1001"]))

    t.step("channel 1 of 3: the accounting webhook arrives over HTTP")
    payload = {"debtor_id": "NW-1001", "amount": PARTIAL_PAYMENT,
               "external_reference": PARTIAL_REFERENCE, "signal_at": clock.iso(),
               "note": "client's QuickBooks balance delta"}
    t.write(f"  POST {api_base}/payments/webhook")
    t.json_block("webhook payload", payload)
    # The queue purge is in-memory, so capture it right after the HTTP call returns.
    status, result = _post_json(f"{api_base}/payments/webhook", payload)
    t.kv("HTTP status", status)
    t.kv("status / intake reference", f"{result['status']} / {result['intake_reference']}")
    t.kv("confirmation channel", f"{result['confirmation_channel']} "
                                 f"({result['confirmation_latency']})")
    t.write("")
    t.write("  consequence (a) - the balance moved and the record's state changed:")
    t.kv("balance before -> after",
         f"{result['balance_before_cents']} -> {result['balance_after_cents']} cents "
         f"({result['amount_collected']} collected)")
    t.kv("account_state after", result["account_state_after"])
    t.write("  consequence (b) - the scheduled attempt is gone from the active queue:")
    t.kv("runs cancelled", [r["run"]["run_id"] for r in result["cancelled_runs"]])
    t.kv("queue after the purge", engine.queue.summary() or "{} (empty - nothing left to fire)")
    t.kv("outbound_contact_logs rows (untouched)", result["outbound_logs_untouched"])
    t.kv("cancelled at", result["queue_purged_at"])
    t.write("  consequence (c) - one immutable ledger line, with the fee on the funds collected:")
    t.kv("ledger entry id", result["ledger_entry_id"])
    t.kv("amount / fee (cents)", f"{result['amount_cents']} / {result['fee_cents']}")
    t.kv("fee basis / rounding",
         f"{result['fee_basis_points']} basis points ({result['fee_rate_percent']}%), "
         f"{result['fee_rounding_rule']}")
    t.kv("why 364.11 and not 364.10", f"{result['amount_cents']} x 20% = 36410.6 cents -> half up -> "
                                      f"{result['fee_cents']} cents")
    t.write("")
    t.write("The same webhook delivered twice is ignored, not billed twice:")
    dup_status, duplicate = _post_json(f"{api_base}/payments/webhook", payload)
    t.kv("second delivery", f"HTTP {dup_status} {duplicate['status']} - {duplicate['reason']}")
    t.kv("ledger lines now", len(engine.ledger("CLI-NORTHWIND")["lines"]))

    t.step("channel 3 of 3: the client attests the rest of the money arrived")
    remainder = round(engine.debtor("NW-1001")["balance_owed"], 2)
    t.write(f"  the remaining balance is {remainder}; the client attests it was paid in full.")
    attest = engine.record_payment("NW-1001", remainder, payments.CHANNEL_MANUAL_ATTESTATION,
                                   attested_by="finance@northwind (client)",
                                   note="attested in the client portal")
    t.kv("channel / attested by", f"{attest['confirmation_channel']} / {attest['attested_by']}")
    t.kv("amount / fee (cents)", f"{attest['amount_cents']} / {attest['fee_cents']}")
    t.kv("account_state after", attest["account_state_after"])
    t.kv("balance after", attest["balance_after_cents"])

    t.step("a paid debtor is never chased again")
    after = engine.send("NW-1001", "voice", who="scheduler", reason="scheduled run after payment")
    t.kv("decision", after["decision"])
    t.kv("claimed?", after["claimed"])
    t.kv("reason", after["reason_detail"])
    t.kv("claims written for NW-1001", len([c for c in engine.contacts()
                                            if c["debtor_id"] == "NW-1001" and c["claim_token"]]))

    t.step("artifacts this scenario produces (written by the run, never by hand)")
    pay_dir = ROOT / "out" / "payments"
    pay_dir.mkdir(parents=True, exist_ok=True)
    ledger_artifact = pay_dir / "ledger_CLI-NORTHWIND.json"
    ledger_artifact.write_text(json.dumps(labelled(engine.ledger("CLI-NORTHWIND")), indent=2),
                               encoding="utf-8")
    events_artifact = pay_dir / "payment_events.json"
    events_artifact.write_text(
        json.dumps(labelled({"payment_events": engine.payment_events("CLI-NORTHWIND")}), indent=2),
        encoding="utf-8")
    purge_artifact = pay_dir / "queue_purge.json"
    purge_record = {
        "event_id": result["event_id"],
        "intake_reference": result["intake_reference"],
        "debtor_id": result["debtor_id"],
        "client_id": result["client_id"],
        "confirmation_channel": result["confirmation_channel"],
        "queue_purged_at": result["queue_purged_at"],
        "cancelled_run_count": result["cancelled_run_count"],
        "cancelled_runs": result["cancelled_runs"],
        "outbound_contact_logs_untouched": result["outbound_logs_untouched"],
        "note": "scheduled queue entries are removed; outbound_contact_logs rows are never touched",
    }
    purge_artifact.write_text(json.dumps(labelled(purge_record), indent=2), encoding="utf-8")
    queue_artifact = pay_dir / "queue_state_after_purge.json"
    queue_artifact.write_text(
        json.dumps(labelled({"queue": [r.as_dict() for r in engine.queue.all()]}), indent=2),
        encoding="utf-8")
    for path in (ledger_artifact, events_artifact, purge_artifact, queue_artifact):
        t.kv(path.name, f"{path.stat().st_size} bytes")
    t.write("")
    t.write("The ledger read model (out/payments/ledger_CLI-NORTHWIND.json), from real rows only:")
    totals = engine.ledger("CLI-NORTHWIND")["totals"]
    t.json_block("totals", totals)
    t.write("")
    t.write("  entry  type  debtor   amount_cents  fee_cents  channel               latency")
    for line in engine.ledger("CLI-NORTHWIND")["lines"]:
        t.write(f"  {line['entry_id']:<6} {line['entry_type']:<8} {line['debtor_id']:<8} "
                f"{line['amount_cents']:>12}  {line['fee_cents']:>9}  "
                f"{line['confirmation_channel']:<21} {line['confirmation_latency']}")


def scenario_9_reversal_and_balance_edits(t: Transcript, engine: RecoverAI, api_base: str) -> None:
    t.section("S8 - a bounced payment un-bills and reopens into a mandatory audit; "
              "an unconfirmed edit parks the record")
    first_entry = engine.ledger("CLI-NORTHWIND")["lines"][0]
    before_hash = _row_hash(engine.store.query_one(
        "SELECT * FROM collections_ledger WHERE entry_id = ?", (first_entry["entry_id"],)))
    t.step("the first payment bounces - a credit counter-entry, never an edit")
    t.write(f"  the payment of {first_entry['amount']} (ledger entry {first_entry['entry_id']}) came back "
            f"as a returned payment.")
    t.write(f"  POST {api_base}/payments/reverse")
    payload = {"debtor_id": "NW-1001", "kind": "bounced",
               "reverses_entry_id": first_entry["entry_id"],
               "external_reference": "QBO-RETURN-7731", "signal_at": clock.iso()}
    t.json_block("reversal payload", payload)
    status, reversal = _post_json(f"{api_base}/payments/reverse", payload)
    t.kv("HTTP status", status)
    t.kv("counter-entry id", reversal["counter_entry_id"])
    t.kv("reverses entry", reversal["reverses_entry_id"])
    t.kv("amount / fee (cents)", f"{reversal['amount_cents']} / {reversal['fee_cents']}")
    t.kv("net statement value", f"amount {reversal['net_statement_amount_cents']} cents, "
                                f"fee {reversal['net_statement_fee_cents']} cents (original + reversal)")
    t.kv("balance after reopening", f"{reversal['balance_after']} ({reversal['account_state_after']})")
    after_row = engine.store.query_one("SELECT * FROM collections_ledger WHERE entry_id = ?",
                                       (first_entry["entry_id"],))
    t.kv("original row hash before", before_hash)
    t.kv("original row hash after", _row_hash(after_row))
    t.kv("original row byte-identical", _row_hash(after_row) == before_hash)
    t.write("  A reversal is not a resume. The record is in Reopened_Awaiting_Audit and the runs the")
    t.write("  payment purged stay purged - no resurrection, no new run, no sequence restart:")
    t.kv("account_state after the reversal", reversal["account_state_after"])
    t.kv("claim blocked until", reversal["claim_blocked_until"])
    t.step("no contact can be claimed from a reversed record - not even with a fresh approval")
    blocked = engine.send("NW-1001", "voice", who="orchestrator",
                          reason="attempt while the reopen audit is open", dispatch=False)
    t.kv("claim attempt", f"{blocked['decision']} (claimed={blocked['claimed']})")
    t.kv("runs queued for NW-1001",
         len([r for r in engine.queue.all() if r.debtor_id == "NW-1001"]))
    try:
        engine.approve("NW-1001", who="reviewer@northwind (human)")
        t.kv("approval while the audit is open", "ACCEPTED - this should not happen")
    except PermissionError as exc:
        t.kv("approval while the audit is open", f"REFUSED - {exc}")
    t.write("  So the record re-runs the ingestion path from step one, and only a COMPLETED")
    t.write("  re-audit (recorded, attributed, millisecond UTC) puts it back in normal flow:")
    audit = engine.run_reopen_audit("NW-1001", performed_by="ops@northwind (client)",
                                    reason="the returned payment was confirmed by the bank")
    t.kv("reopen audit status / event", f"{audit['status']} (event {audit['event_id']})")
    t.kv("re-normalized source", f"{audit['source_system']} -> {audit['source_ref']}")
    t.kv("source balance vs ledger balance (cents)",
         f"{audit['source_balance_cents']} vs {audit['balance_cents']} "
         f"(match={audit['source_balance_matches_engine']})")
    if not audit["source_balance_matches_engine"]:
        t.write("  (That gap is expected here, not a bug being papered over: the export row is the")
        t.write("   invoice as it stood when it was fetched, the ledger records the payments made")
        t.write("   against it since. The re-audit RECORDS both figures and applies neither - the")
        t.write("   ledger is the money record, and a mismatch is flagged for a human to look at.)")
    t.kv("contact data re-resolved",
         f"email={audit['contact']['email_resolved']} phone={audit['contact']['phone_resolved']}")
    t.kv("window re-evaluated",
         f"{audit['window']['window_decision']} (area code {audit['window']['area_code']}, "
         f"{audit['window']['derived_local_time']})")
    t.kv("DNC re-checked", f"dnc_flag={audit['dnc_flag']} blocked={audit['dnc_blocked']}")
    t.kv("paid status / balance", f"{audit['paid_status']} / {audit['balance_owed']}")
    t.kv("recorded at (UTC, ms)", audit["recorded_at"])
    t.kv("approval after the audit",
         f"{audit['approval_status_after']} (a fresh human review is required)")
    t.kv("runs restored by the audit", len(audit["queue_runs_for_debtor"]))
    t.write("  The source balance is RECORDED, never applied: the ledger is the money record. And")
    t.write("  the record re-enters under the mode in force (hitl), so it still needs a human:")
    engine.approve("NW-1001", who="reviewer@northwind (human)")
    reopened = engine.send("NW-1001", "voice", who="reviewer@northwind (human)",
                           reason="after the completed reopen audit", dispatch=False)
    t.kv("after approval", f"{reopened['decision']} (claimed={reopened['claimed']})")
    last_claim = [c for c in engine.contacts()
                  if c["debtor_id"] == "NW-1001" and c["claim_token"]][-1]
    t.kv("balance at claim (cents)",
         f"{last_claim['balance_at_claim_cents']} ({last_claim['account_state_at_claim']})")
    t.kv("reopen audit log rows for NW-1001", len(engine.reopen_audits("NW-1001")))

    t.step("a balance edit with no payment signal behind it parks the record")
    t.write("CE-2002's balance changes in the export. No payment signal is correlated with it, so")
    t.write("the engine does not guess: it parks the record instead of dunning or billing.")
    t.kv("balance before", engine.debtor("CE-2002")["balance_owed"])
    parked = engine.record_balance_edit("CE-2002", 400.00, who="ops@cedar-ridge (client)",
                                        reason="a credit note applied in Xero")
    t.kv("status", parked["status"])
    t.kv("account_state after", parked["account_state_after"])
    t.kv("billable", parked["billable"])
    t.kv("ledger lines for CLI-CEDAR", len(engine.ledger("CLI-CEDAR")["lines"]))
    blocked = engine.send("CE-2002", "email", who="orchestrator", reason="attempt while parked")
    t.kv("send while parked", blocked["decision"])
    t.kv("claimed?", blocked["claimed"])
    t.kv("reason", blocked["reason_detail"])
    t.write("")
    t.write("The client resolves it as an ADJUSTMENT - no money arrived, so nothing is billable:")
    resolved = engine.resolve_balance_edit("CE-2002", "adjustment", who="dana@cedar-ridge (CFO)",
                                          reason="a credit note, not a payment")
    t.kv("status / billable", f"{resolved['status']} / {resolved['billable']}")
    t.kv("ledger entry id", resolved["ledger_entry_id"])
    t.kv("state / balance after", f"{resolved['account_state_after']} / "
                                  f"{engine.debtor('CE-2002')['balance_owed']}")
    t.kv("CLI-CEDAR ledger lines", len(engine.ledger("CLI-CEDAR")["lines"]))
    t.kv("CLI-CEDAR net fee (cents)", engine.ledger("CLI-CEDAR")["totals"]["net_fee_cents"])
    t.write("")
    t.write("A second record, resolved as a PAYMENT - it flows through the normal payment path and")
    t.write("becomes billable on the funds actually collected:")
    t.kv("CE-2001 balance before", engine.debtor("CE-2001")["balance_owed"])
    engine.record_balance_edit("CE-2001", 12140.00, who="ops@cedar-ridge (client)",
                               reason="balance dropped in the Xero export")
    paid = engine.resolve_balance_edit("CE-2001", "payment", who="dana@cedar-ridge (CFO)",
                                      reason="confirmed 200.00 arrived in our bank account")
    t.kv("status / billable", f"{paid['status']} / {paid['billable']}")
    t.kv("collected / fee (cents)", f"{paid['amount_cents']} / {paid['fee_cents']}")
    t.kv("channel / attested by", f"{paid['confirmation_channel']} / {paid['attested_by']}")
    t.kv("account_state after", paid["account_state_after"])
    t.kv("ledger entry id", paid["ledger_entry_id"])
    t.write("")
    t.write("The standing default, stated plainly: an unconfirmed adjustment is NEVER billable.")
    t.write(f"payments.UNCONFIRMED_ADJUSTMENT_BILLABLE = {payments.UNCONFIRMED_ADJUSTMENT_BILLABLE} "
            f"- the owner's ruling is still pending and this is the team's default until then.")
    t.step("every billable line, per client, from the ledger read model")
    for client_id in ("CLI-NORTHWIND", "CLI-CEDAR"):
        totals = engine.ledger(client_id)["totals"]
        t.kv(client_id, f"lines={totals['line_count']} net_collected={totals['net_collected']} "
                        f"net_fee={totals['net_fee_amount']} "
                        f"({totals['net_fee_cents']} cents)")


def scenario_10_gate(t: Transcript, engine: RecoverAI) -> None:
    """S10 - the beta-to-full-active gate, rendered from the gate's own records.

    Nothing here is a print statement inside the gate: the gate module is print-free and
    stays that way (asserted by tests/test_gate.py::TestAgent4Reporting). This section reads
    the structured records and renders them.
    """
    from engine import gate  # imported here so the demo's imports stay where they are

    t.section("S9 - the gate: nobody can leave beta today, and here is the evidence")
    for client_id in ("CLI-NORTHWIND", "CLI-CEDAR"):
        tooltip = gate.milestone_tooltip(engine.store, client_id)
        t.step(f"{client_id} - lifecycle_status = {tooltip['lifecycle_status']}")
        t.kv("can go live (all milestones met)", tooltip["can_go_live"])
        t.kv("milestones outstanding", tooltip["outstanding_count"])
        t.kv("sandbox route demonstrated", tooltip["billing_route_demonstrated_in_sandbox"])
        for milestone in tooltip["milestones"]:
            t.kv(f"  {milestone['key']}",
                 f"met={milestone['met']}  (mirror column={milestone['flag_value']})")
            t.write(f"      why: {milestone['detail']}")
        t.write("  the Agent 4 tooltip, verbatim (this is what the console shows):")
        for line in textwrap.wrap(tooltip["text"], 94):
            t.write(f"    {line}")
        for label in tooltip["honest_labels"]:
            t.kv("  honest label", label)

    t.step("the platform pin, and what a pin does NOT prove")
    resolved = gate.resolve_platform_account_id()
    t.kv("resolved platform account id", resolved)
    t.kv("is the register pinned at all?", resolved != gate.UNPINNED)
    t.kv("config/platform.json", "the team's recorded GET /v1/account read-back")
    t.write("  A pinned id is not a payment. It says which account a receipt would have to")
    t.write("  name; it does not show that the account ever took money, and this one has not")
    t.write("  (0 charges, $0 available, $0 pending when it was read). So the live billing")
    t.write("  milestone is still outstanding for every client: no chain-verified receipt is")
    t.write("  bound to any of them.")

    t.step("what the gate does when no account id is pinned at all (an empty register)")
    empty_register = ROOT / "out" / "no_such_platform_register.json"   # missing -> no pin
    unpinned = gate.verify_milestone_b_receipt(
        engine.store, {"payment_intent_sample": "pi_simulated_in_the_demo", "livemode": False,
                       "capabilities": {"charges_enabled": False, "payouts_enabled": False},
                       "account_id": "acct_simulated_in_the_demo",
                       "timestamp_utc": "2026-10-06T14:00:00Z"},
        client_id="CLI-NORTHWIND", recorded_by="demo", verification_source="demo (simulated)",
        pin=gate.UNPINNED, platform_config_file=empty_register)
    t.kv("decision / state", f"{unpinned['decision']} / {unpinned['state']}")
    t.kv("recorded anything?", unpinned["recorded"])
    t.kv("gate locked", unpinned["gate_locked"])
    t.kv("chain rows for this client", len(engine.billing_route_verifications("CLI-NORTHWIND")))
    t.write("  With no pin the gate refuses and records NOTHING at all - not even a refusal")
    t.write("  row. That is the fail-closed direction, and it is the same code path the")
    t.write("  pinned case takes; the pin only decides which account a receipt must name.")

    t.step("the launcher refuses too (its structured refusal record)")
    refusal = gate.gate_lock_record(engine.store, "CLI-NORTHWIND", "voice", "demo-runner",
                                    "the demo asks the live launcher to start")
    t.kv("decision / reason token", f"{refusal['decision']} / {refusal['reason_token']}")
    t.kv("outstanding milestones", ", ".join(refusal["outstanding_milestones"]))
    t.kv("lifecycle_status", refusal["lifecycle_status"])
    t.kv("sandbox send path affected", refusal["sandbox_send_path_affected"])

    t.step("stated in plain words")
    t.write("  * The pin above is the id the owner supplied and the team read back with")
    t.write("    GET /v1/account using that account's own live key. It is the fee account")
    t.write("    and is recorded in config/platform.json. It is an id, not a payment, and")
    t.write("    it is not a verified live billing route: that account has never taken a")
    t.write("    payment (0 charges, 0 payment intents, $0/$0), so no receipt can be bound")
    t.write("    and the milestone stays unmet. If the register were emptied, the gate")
    t.write("    would refuse with PLATFORM_ACCOUNT_NOT_PINNED and write nothing at all.")
    t.write("  * NO CLIENT CAN LEAVE BETA TODAY. The commercial milestone needs an executed")
    t.write("    commercial Master Services Agreement recorded in commercial_msa_records.")
    t.write("    That table is empty because nobody has signed anything, and the milestone")
    t.write("    is derived from the table - never from the mirror column, and never from a")
    t.write("    flag somebody could write by hand.")
    t.write("  * An unconfirmed balance edit is NEVER billed. A balance that moves with no")
    t.write("    payment signal behind it parks the record in")
    t.write("    Pending_Adjustment_Confirmation: no dunning, no fee, no send, until the")
    t.write("    client says which it was. The standing default in this build is")
    t.write(f"    payments.UNCONFIRMED_ADJUSTMENT_BILLABLE = {payments.UNCONFIRMED_ADJUSTMENT_BILLABLE}.")
    t.write("  * $0 HAS BEEN COLLECTED TO DATE. Every dollar figure anywhere in this run is")
    t.write("    simulated, from mock exports and simulated provider calls.")

    t.step("the milestone tooltip, as the console payload it is")
    t.json_block("CLI-NORTHWIND milestone tooltip",
                 gate.milestone_tooltip(engine.store, "CLI-NORTHWIND"))


def closing_state(t: Transcript, engine: RecoverAI) -> None:
    t.section("Closing state - the tables, the three append-only logs and the artifacts")

    t.step("outbound_contact_logs (every attempt; blocked attempts write no claim)")
    t.write(f"  {'id':<4} {'debtor':<10} {'chan':<6} {'decision':<24} {'mode':<10} {'ver':<4} "
            f"{'window':<24} claim_token")
    for row in engine.contacts():
        t.write(f"  {row['contact_log_id']:<4} {row['debtor_id']:<10} {row['contact_type']:<6} "
                f"{row['decision']:<24} {str(row['operating_mode_at_claim']):<10} "
                f"{str(row['mode_version_at_claim']):<4} {str(row['window_decision']):<24} "
                f"{row['claim_token'] or '-'}")
    claimed = [r for r in engine.contacts() if r["claim_token"]]
    t.write("")
    t.kv("attempts logged", len(engine.contacts()))
    t.kv("claims committed (POST permitted)", len(claimed))
    t.kv("blocked attempts (no claim, no POST)", len(engine.contacts()) - len(claimed))

    t.step("debtor_records - final approval status, payment state and DNC state")
    t.write(f"  {'debtor_id':<10} {'client':<14} {'aging':<8} {'approval_status':<24} "
            f"{'account_state':<30} dnc  window_decision")
    for row in engine.store.query("SELECT * FROM debtor_records ORDER BY debtor_id"):
        t.write(f"  {row['debtor_id']:<10} {row['client_id']:<14} {str(row['aging_state']):<8} "
                f"{row['approval_status']:<24} {row['account_state']:<30} {row['dnc_flag']:<4} "
                f"{row['window_decision']}")

    t.step("collections_ledger - append only, one line per confirmed collection (UPDATE and "
           "DELETE are rejected by triggers)")
    t.write(f"  {'id':<4} {'debtor':<10} {'type':<9} {'amount_cents':>13} {'fee_cents':>10} "
            f"{'channel':<21} {'latency':<18} {'reverses':<9} recorded_at")
    for row in engine.store.query("SELECT * FROM collections_ledger ORDER BY entry_id"):
        t.write(f"  {row['entry_id']:<4} {row['debtor_id']:<10} {row['entry_type']:<9} "
                f"{row['amount_cents']:>13} {row['fee_cents']:>10} {row['confirmation_channel']:<21} "
                f"{row['confirmation_latency']:<18} {str(row['linked_entry_id'] or '-'):<9} "
                f"{row['recorded_at']}")
    for client_id in ("CLI-NORTHWIND", "CLI-CEDAR"):
        totals = engine.ledger(client_id)["totals"]
        t.kv(f"{client_id} net", f"{totals['net_collected']} collected, "
                                 f"{totals['net_fee_amount']} fee "
                                 f"({totals['net_fee_cents']} cents, "
                                 f"{totals['collections_count']} collections, "
                                 f"{totals['reversals_count']} reversals)")

    t.step("payment_events - the intake record: signal, state, fee line and the purge")
    for row in engine.payment_events():
        t.write(f"  #{row['event_id']} {row['debtor_id']:<10} {row['confirmation_channel']:<19} "
                f"{row['amount_cents']:>12} cents  state_after={row['account_state_after']:<14} "
                f"cancelled_runs={row['cancelled_run_count']} "
                f"logs_untouched={row['outbound_logs_untouched']} "
                f"by={row['attested_by'] or '-'}")

    t.step("balance_edit_events - edits with no payment signal, and how they were resolved")
    for row in engine.balance_edit_events():
        t.write(f"  #{row['event_id']} {row['debtor_id']:<10} {row['event_type']:<22} "
                f"delta={row['delta_cents']:>9} billable={row['billable']} "
                f"ledger_entry={row['ledger_entry_id'] or '-'} "
                f"linked_event={row['linked_event_id'] or '-'} by {row['who']}: {row['reason']}")

    t.step("pending adjustments (a parked record: no dunning, no fee, no send)")
    parked = engine.pending_adjustments()
    t.kv("records parked right now", len(parked))
    for row in parked:
        t.kv(row["debtor_id"], f"balance={row['balance_owed']} open_edit_event="
                               f"{(row['open_edit_event'] or {}).get('event_id')}")

    t.step("autonomy_mode_history - append only (UPDATE and DELETE are rejected by triggers)")
    for row in engine.timeline():
        t.write(f"  #{row['history_id']} {row['timestamp']} {row['client_id']:<14} "
                f"{row['old_mode']:<10} -> {row['new_mode']:<10} v{row['mode_version']} "
                f"by {row['who']}: {row['reason']}")

    t.step("client_autonomy_settings")
    for row in engine.store.query("SELECT * FROM client_autonomy_settings ORDER BY client_id"):
        t.kv(row["client_id"],
             f"mode={row['operating_mode']} valid_signoff={row['has_valid_signoff']} "
             f"revoked={row['signature_revoked']} mode_version={row['mode_version']} "
             f"claims={row['claim_seq']} sig={(row['signature_reference'] or '-')[:28]}")

    t.step("scheduled runs (killed by DNC or halted by a mode flip)")
    for run in engine.queue.all():
        t.write(f"  {run.run_id} {run.debtor_id:<10} {run.channel:<6} {run.state:<11} "
                f"{run.state_reason}")

    t.step("guardrail / action log (out/guardrail_actions.jsonl)")
    counts: dict[str, int] = {}
    for entry in engine.audit.entries:
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1
    for action, count in sorted(counts.items()):
        t.kv(action, count)

    t.step("artifacts written to out/")
    # Write every final artifact BEFORE listing them, so the listing is the truth about
    # what this run left behind rather than what the previous one did.
    engine.queue.dump()
    db_state = ROOT / "out" / "db_state.json"
    db_state.write_text(json.dumps(labelled(engine.snapshot()), indent=2, default=str),
                        encoding="utf-8")
    summary = {
        "run_at": clock.iso(),
        "store": str(engine.store.path),
        "debtors": len(engine.store.query("SELECT * FROM debtor_records")),
        "claims_committed": len(claimed),
        "attempts_logged": len(engine.contacts()),
        "timeline_entries": len(engine.timeline()),
        "dnc_debtors": [r["debtor_id"] for r in engine.store.query(
            "SELECT debtor_id FROM debtor_records WHERE dnc_flag = 1")],
        "frozen_debtors": [r["debtor_id"] for r in engine.store.query(
            "SELECT debtor_id FROM debtor_records WHERE approval_status = 'Outside_Calling_Window'")],
        "modes": {r["client_id"]: r["operating_mode"] for r in engine.store.query(
            "SELECT client_id, operating_mode FROM client_autonomy_settings")},
        "payment_states": {r["debtor_id"]: r["account_state"] for r in engine.store.query(
            "SELECT debtor_id, account_state FROM debtor_records ORDER BY debtor_id")},
        "ledger_totals": {cid: engine.ledger(cid)["totals"]
                          for cid in ("CLI-NORTHWIND", "CLI-CEDAR")},
        "payment_events": [{"event_id": r["event_id"], "debtor_id": r["debtor_id"],
                            "confirmation_channel": r["confirmation_channel"],
                            "amount_cents": r["amount_cents"],
                            "account_state_after": r["account_state_after"],
                            "cancelled_run_count": r["cancelled_run_count"]}
                           for r in engine.payment_events()],
        "parked_adjustments": [r["debtor_id"] for r in engine.pending_adjustments()],
        "all_rows_are_simulated": True,
    }
    summary.update(SIMULATION_LABEL)
    summary_path = ROOT / "out" / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    listed = 0
    for path in sorted((ROOT / "out").rglob("*")):
        if path.is_file():
            t.write(f"  {path.relative_to(ROOT / 'out')}  ({path.stat().st_size} bytes)")
            listed += 1
    t.write(f"  ({listed} files)")
    t.write("  (run_log.txt is this transcript: it keeps growing as each of these lines is")
    t.write("   written, so its size above is the size as of that line, not the final size.)")

    t.step("artifact check - every file this run promised to leave behind")
    for name in EXPECTED_ARTIFACTS:
        path = ROOT / "out" / name
        ok = path.is_file() and path.stat().st_size > 0
        t.kv(name, f"{'OK' if ok else 'MISSING OR EMPTY'} ({path.stat().st_size if path.exists() else 0} bytes)")
    for pattern, label in (("vapi_payloads/*.json", "vapi_payloads"),
                           ("email_outbox/*.json", "email_outbox"),
                           ("mock_vendor_calls/*.json", "mock_vendor_calls"),
                           ("drafts/*.txt", "drafts")):
        files = sorted((ROOT / "out").glob(pattern))
        t.kv(label, f"{len(files)} file(s)")
        if files:
            t.kv(f"  {label} marker", label_directory(ROOT / "out" / label.partition("/")[0])
                 if label != "drafts" else label_directory(ROOT / "out" / "drafts"))

    t.step("honesty check - does every JSON artifact label its simulated money itself?")
    unlabelled = []
    for path in sorted((ROOT / "out").rglob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            unlabelled.append(f"{path.relative_to(ROOT / 'out')} (unparseable JSON)")
            continue
        carries = (isinstance(document, dict) and document.get("simulated") is True
                   and document.get("money_collected_to_date_usd") == 0.0)
        if isinstance(document, dict):
            for value in document.values():
                if isinstance(value, list) and value and isinstance(value[0], dict) \
                        and value[0].get("simulated") is True:
                    carries = True
        t.kv(f"  {path.relative_to(ROOT / 'out')}",
             "labelled" if carries else "NO SIMULATED-MONEY MARKER")
        if not carries:
            unlabelled.append(str(path.relative_to(ROOT / "out")))
    t.kv("artifacts carrying the marker",
         f"{len(list((ROOT / 'out').rglob('*.json'))) - len(unlabelled)} of "
         f"{len(list((ROOT / 'out').rglob('*.json')))}")
    if unlabelled:
        t.write(f"  {len(unlabelled)} artifact(s) carry no marker: {sorted(unlabelled)}")
        t.write("  (each of these is listed above with NO SIMULATED-MONEY MARKER - the run")
        t.write("   does not hide it.)")

    t.write("")
    t.write("Summary:")
    t.json_block("out/summary.json", summary)
    t.write("")
    t.write("All external APIs in this run were local mocks. No real email, call or payment happened.")


if __name__ == "__main__":
    raise SystemExit(main())
