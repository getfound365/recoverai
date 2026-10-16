#!/usr/bin/env python3
"""Phase 3, pass 2 - the console bridge.

ONE JSON request in on stdin, ONE JSON response out on stdout. It is the only thing the
website's server functions are allowed to call, and every op is a *thin dispatch* onto
the engine facade (`engine.app.RecoverAI`) - the bridge holds no intake logic of its own.
Mapping, validation, the consumer-debt scan, the ingestion-batches record and the commit
path all stay in `engine/ingest.py`; if the engine's answer changes, the console changes
with it, because there is only ever one answer.

Why stdio and not an HTTP server: the site's SSR process is managed by the platform, a
second long-running Python daemon is not - a daemon would quietly stop existing and the
console would have to invent what it renders. A short-lived subprocess per request dies
with its answer and there is nothing to keep alive, restart or go stale.

The store it talks to is its OWN SQLite file (`data/console.db` by default, override with
RECOVERAI_CONSOLE_DB). It is deliberately not `data/recoverai.db`: `run_demo.py` opens
that file with `fresh=True`, which deletes it, so console state must not live there.

Usage:
    echo '{"op":"status"}' | python3 tools/console_bridge.py

Contract: always exits 0 with a JSON object on stdout. `{"bridge_ok": true, "payload":
...}` when the engine answered (including when the engine's answer is a REFUSAL - a
refusal is a payload, not a bridge error). `{"bridge_ok": false, "error": ...,
"detail": ...}` when the bridge itself could not run the op; the console renders that
plainly instead of guessing.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import traceback
from pathlib import Path

# run as `python3 tools/console_bridge.py`: the repo root is one level up from tools/
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

BRIDGE_VERSION = "1.0"

#: A file the console will preview. Bigger than this and the wizard says so, in one
#: token, instead of grinding a browser tab to death.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

#: The sandbox client the console operates as. It is a FIXTURE, and the console says so
#: on screen: no real client exists, and the sign-off bootstrapped for it is a sandbox
#: signature, not a client authorization.
SANDBOX_CLIENT_ID = os.environ.get("RECOVERAI_CONSOLE_CLIENT", "CLI-SANDBOX-AGENCY")
SANDBOX_SIGNER_NAME = "Sandbox Operator"
SANDBOX_SIGNER_TITLE = "Simulated signer - sandbox console fixture, not a real client authorization"
#: The prefix every console FIXTURE client id carries. `_bootstrap` writes a Step 3
#: acknowledgement (an explicit TRUE) and a signature ON a client's behalf, so it must be
#: impossible for it to do that to a client a human is supposed to be signing for:
#: `RECOVERAI_CONSOLE_CLIENT` exists to point the console at a different fixture (a demo
#: store, a rehearsal), never to hand the bridge a real client to attest and sign for.
#: A client id outside this prefix still READS fine - the console shows its real state,
#: including a missing sign-off - but no fixture write is made for it.
SANDBOX_CLIENT_ID_PREFIX = "CLI-SANDBOX"
#: The sample file the console offers. It lives in the engine tree so the frontend never
#: carries a copy of a mock row.
SAMPLE_FILE = _REPO_ROOT / "samples" / "spreadsheet" / "agency_past_due_sample.csv"


def _console_db_path() -> Path:
    env = os.environ.get("RECOVERAI_CONSOLE_DB")
    return Path(env).resolve() if env else (_REPO_ROOT / "data" / "console.db")


def _console_audit_path() -> Path:
    env = os.environ.get("RECOVERAI_CONSOLE_AUDIT")
    return Path(env).resolve() if env else (_REPO_ROOT / "out" / "console_audit.jsonl")


def _console_runqueue_path() -> Path:
    """Where the console's SCHEDULE of record lives.

    The bridge is one short-lived process per request, so an in-memory run queue would be
    empty on every read and the "Switch to Manual" halt would have nothing to halt. The
    engine reads this file on open and writes it on close; it is deliberately not the
    demo's `out/scheduled_runs.json`, which `run_demo.py` rewrites on every run.
    """
    env = os.environ.get("RECOVERAI_CONSOLE_RUNS")
    return Path(env).resolve() if env else (_REPO_ROOT / "out" / "console_runs.json")


# --- engine handle ----------------------------------------------------------
def _engine():
    """The facade over the console store. Imports happen here so a broken engine is
    reported as a bridge error rather than a stack trace on stdout."""
    from engine.app import RecoverAI  # noqa: PLC0415 - deliberate late import
    return RecoverAI(db_path=str(_console_db_path()), backend="sqlite",
                     audit_path=_console_audit_path(),
                     runqueue_path=_console_runqueue_path())


def _bootstrap(engine) -> dict:
    """Make sure the sandbox client exists with a usable sign-off AND a Step 3 attestation
    on file. Idempotent, and honest about what it did: it writes an audit line saying both
    are sandbox fixtures, never a client's.

    Why the attestation belongs here: since the real Step 3 gate landed, the intake refuses
    to register a batch for a client with no recorded attestation
    (`BLOCKED_NO_STEP3_ATTESTATION`) - which is exactly right, and it would leave the
    console's upload wizard permanently unable to commit, because this fixture client has no
    human to tick the box. So the fixture ticks its own, labelled as a fixture, in the order
    the real ladder runs: the warranty first, then the signature that covers it. The engine
    gate is untouched - a real client still has to attest before its files can be
    registered.

    It refuses to write EITHER for a client id that is not a console fixture (see
    `SANDBOX_CLIENT_ID_PREFIX`): a Step 3 acknowledgement is a human's explicit TRUE and a
    sign-off is a human's signature, so a bootstrap that filled both in for a real client
    would manufacture the authorization the whole ladder exists to obtain. Reads are not
    affected - the console still shows such a client's real state, including the sign-off
    it does not have.
    """
    from engine import config, onboarding  # noqa: PLC0415
    if not SANDBOX_CLIENT_ID.startswith(SANDBOX_CLIENT_ID_PREFIX):
        detail = (f"client id {SANDBOX_CLIENT_ID!r} is not a console fixture "
                  f"(a fixture id starts with {SANDBOX_CLIENT_ID_PREFIX!r}): the bridge "
                  f"writes no Step 3 attestation and no signature for it. The Step 3 "
                  f"warranty must be ticked by a human and the sign-off typed by a human.")
        engine.audit.record("console_fixture_write_refused", client_id=SANDBOX_CLIENT_ID,
                            refusal_token="BLOCKED_FIXTURE_WRITE_FOR_NON_SANDBOX_CLIENT",
                            detail=detail, bootstrapped_by="tools/console_bridge.py")
        return {"client_id": SANDBOX_CLIENT_ID, "created_client": False, "created_signoff": False,
                "created_step3_attestation": False, "sandbox_fixture": True,
                "fixture_writes_refused": True,
                "refusal_token": "BLOCKED_FIXTURE_WRITE_FOR_NON_SANDBOX_CLIENT",
                "detail": detail}
    row = onboarding.fetch_settings(engine.store, SANDBOX_CLIENT_ID)
    created_signoff = False
    created_client = row is None
    created_attestation = False
    if onboarding.current_attestation(engine.store, SANDBOX_CLIENT_ID,
                                      onboarding.ATTESTATION_STEP3) is None:
        result = engine.record_step3_attestation(
            SANDBOX_CLIENT_ID, True, "console-bridge-bootstrap",
            source="console-bridge-bootstrap",
            note=("sandbox console fixture: a simulated Step 3 attestation so the upload "
                  "wizard has a warranty to bind a batch to. No real client, no real "
                  "contact data and no real warranty exists."))
        created_attestation = bool(result.get("ok"))
        if not created_attestation:
            raise RuntimeError(f"the sandbox Step 3 fixture attestation was refused: {result}")
        engine.audit.record("onboarding_attestation_recorded", client_id=SANDBOX_CLIENT_ID,
                            attestation_type=onboarding.ATTESTATION_STEP3,
                            reason=("sandbox console fixture: a simulated Step 3 warranty "
                                    "attestation. No real client exists."),
                            bootstrapped_by="tools/console_bridge.py")
    # The sign-off must cover the attestation that is on file: a fixture signature given
    # before the warranty existed does not, so re-take it rather than let the console show
    # a signature whose binding the ladder would never have produced.
    attestation = onboarding.current_attestation(engine.store, SANDBOX_CLIENT_ID,
                                                 onboarding.ATTESTATION_STEP3)
    if (not row or not onboarding.signoff_state(row)["valid"]
            or str(row.get("step3_attestation_id") or "") != str(attestation["attestation_id"])):
        scope_pairs = [[o["channel"], str(o["aging_state"])] for o in config.scope_options()]
        engine.capture_signoff(SANDBOX_CLIENT_ID, SANDBOX_SIGNER_NAME, SANDBOX_SIGNER_TITLE,
                               who="console-bridge-bootstrap", scope_pairs=scope_pairs)
        created_signoff = True
        engine.audit.record("signoff_created", client_id=SANDBOX_CLIENT_ID,
                            signer=SANDBOX_SIGNER_NAME,
                            reason="sandbox console fixture: a simulated signature so the "
                                   "upload wizard has a warranty to bind to. No real client "
                                   "and no real authorization exists.",
                            bootstrapped_by="tools/console_bridge.py")
    return {"client_id": SANDBOX_CLIENT_ID, "created_client": created_client,
            "created_signoff": created_signoff,
            "created_step3_attestation": created_attestation, "sandbox_fixture": True}


def _status(engine) -> dict:
    from engine import ingest, onboarding  # noqa: PLC0415
    client_id = SANDBOX_CLIENT_ID
    settings = onboarding.fetch_settings(engine.store, client_id)
    state = onboarding.signoff_state(settings) if settings else {"valid": False,
                                                                 "reason": "no settings row"}
    batches = engine.ingestion_batches(client_id)
    recent = [{"batch_id": b["batch_id"], "file_name": b["file_name"],
               "rows_seen": b["rows_seen"], "rows_accepted": b["rows_accepted"],
               "rows_rejected": b["rows_rejected"], "recorded_at": b["recorded_at"],
               "recorded_at_ms": b["recorded_at_ms"], "uploader_id": b["uploader_id"],
               "chain_hash": b["chain_hash"], "consumer_debt_marker": b["consumer_debt_marker"]}
              for b in batches[-5:]]
    queue_rows = engine.store.query(
        """SELECT debtor_id, name, balance_owed, aging_state, approval_status, dnc_flag,
                  source_ref, imported_at
             FROM debtor_records WHERE client_id = ? ORDER BY debtor_id LIMIT 50""",
        (client_id,))
    # Net collected, straight off the append-only ledger: collections minus reversals.
    # It is $0 because this store has never seen a payment - which is exactly what the
    # console prints.
    ledger = engine.store.query(
        """SELECT COALESCE(SUM(CASE WHEN entry_type = 'collection' THEN amount_cents
                                   ELSE -ABS(amount_cents) END), 0) AS net_cents,
                  COUNT(*) AS entries
             FROM collections_ledger WHERE client_id = ?""", (client_id,))
    net_cents = int(ledger[0]["net_cents"] or 0) if ledger else 0
    return {
        "client_id": client_id,
        "client_exists": settings is not None,
        "sandbox_fixture": True,
        "signoff": state,
        "signer_name": (settings or {}).get("signer_name"),
        "signer_title": (settings or {}).get("signer_title"),
        "signature_reference": (settings or {}).get("signature_reference"),
        "operating_mode": (settings or {}).get("operating_mode"),
        "lifecycle_status": (settings or {}).get("lifecycle_status"),
        "batches_count": len(batches),
        "recent_batches": recent,
        "queue": queue_rows,
        "queue_count": len(queue_rows),
        "money_collected_to_date_usd": round(net_cents / 100.0, 2),
        "ledger_entries": int(ledger[0]["entries"] or 0) if ledger else 0,
        "chain": ingest.verify_ingestion_batches(engine.store),
        "engine_db_path": str(_console_db_path()),
        "bridge_version": BRIDGE_VERSION,
        "sample_file": _sample_info(),
    }


def _sample_info() -> dict:
    if not SAMPLE_FILE.exists():
        return {"available": False, "file_name": SAMPLE_FILE.name}
    return {"available": True, "file_name": SAMPLE_FILE.name,
            "path": str(SAMPLE_FILE), "sha256_prefix": None}


def _sample_payload() -> dict:
    """The engine's own sample CSV, handed to the wizard as bytes. The frontend gets the
    file; it never gets the rows inlined as fixtures."""
    if not SAMPLE_FILE.exists():
        return {"available": False, "file_name": SAMPLE_FILE.name}
    raw = SAMPLE_FILE.read_bytes()
    return {"available": True, "file_name": SAMPLE_FILE.name,
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "size_bytes": len(raw)}


def _decode_content(request: dict) -> bytes:
    if request.get("content_b64") is not None:
        try:
            content = base64.b64decode(str(request["content_b64"]), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"content_b64 is not valid base64: {exc}") from exc
    elif request.get("content_text") is not None:
        # Newlines are preserved exactly: the bridge never rewrites the client's file.
        content = str(request["content_text"]).replace("\r\n", "\n").encode("utf-8")
    else:
        raise ValueError("a file op needs content_b64 or content_text")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"FILE_TOO_LARGE_FOR_CONSOLE_BRIDGE: {len(content)} bytes exceeds the "
            f"{MAX_UPLOAD_BYTES}-byte console limit")
    return content


# --- the op table -----------------------------------------------------------
def op_status(engine, request: dict) -> dict:
    _bootstrap(engine)
    return _status(engine)


def op_bootstrap(engine, request: dict) -> dict:
    info = _bootstrap(engine)
    info["status"] = _status(engine)
    return info


def op_preview(engine, request: dict) -> dict:
    _bootstrap(engine)
    content = _decode_content(request)
    file_name = str(request.get("file_name") or "upload.csv")
    return engine.preview_spreadsheet(
        file_name=file_name, content=content,
        client_id=str(request.get("client_id") or SANDBOX_CLIENT_ID),
        uploader_id=str(request.get("uploader_id") or "console-operator"),
        source_format=request.get("source_format") or None)


def op_register(engine, request: dict) -> dict:
    preview = request.get("preview")
    if not isinstance(preview, dict):
        raise ValueError("register needs the preview payload it is registering")
    return engine.register_ingestion_batch(
        preview,
        uploader_id=str(request.get("uploader_id") or "console-operator"),
        warranty_acknowledged=bool(request.get("warranty_acknowledged")),
        who=str(request.get("who") or "console-operator"),
        reason=str(request.get("reason") or "console spreadsheet upload"))


def op_commit(engine, request: dict) -> dict:
    """Start the import of a registered batch.

    `uploader_id` here is NOT defaulted to a made-up name. It is handed through only when
    the caller actually hands one, and passed as None when it does not - because that is
    exactly the engine's own contract (`commit_spreadsheet` compares `uploader_id` against
    the batch record only `if uploader_id`, and the batch record is the authoritative
    statement of who handed the file over; `register` already required it). Inventing a
    placeholder identity here - which is what a `"console-operator"` fallback did - put a
    name in front of the engine that no human had given, and the honest result was a
    refusal (`BATCH_PREVIEW_STATE_CHANGED`) against the batch's real uploader. The console
    itself always sends the uploader it collected; the drift check stays live and still
    fires for any caller that hands a name that is not the batch's.

    `who` stays the acting-operator label for the audit line, and is the same default the
    preview/register ops use, so a batch's trail reads consistently end to end.
    """
    preview = request.get("preview")
    if not isinstance(preview, dict):
        raise ValueError("commit needs the preview payload the batch was raised from")
    if not str(request.get("batch_id") or "").strip():
        raise ValueError("commit needs the batch_id of the registered batch")
    handed_uploader = str(request.get("uploader_id") or "").strip() or None
    return engine.commit_spreadsheet(
        preview, batch_id=str(request["batch_id"]),
        uploader_id=handed_uploader,
        who=str(request.get("who") or "console-operator"))


def op_batches(engine, request: dict) -> dict:
    client_id = str(request.get("client_id") or SANDBOX_CLIENT_ID)
    rows = engine.ingestion_batches(client_id)
    return {"batches": rows, "chain": engine.verify_ingestion_batches()}


def op_verify(engine, request: dict) -> dict:
    return engine.verify_ingestion_batches()


def op_sample(engine, request: dict) -> dict:
    return _sample_payload()


def op_vocabulary(engine, request: dict) -> dict:
    """The reason-token vocabulary and the per-row evaluation order, read out of the
    rulebook. The console renders its legend from this, so a legend can never drift from
    the tokens the engine actually emits.

    It also carries Step 2's channel utility rate sheet (MODEL 2): internal operational
    metrics, NOT client charges. It is here because both of the console's reference surfaces
    read this op, and the rate block is read from the rulebook at call time - there is no
    rate literal anywhere in the view code."""
    from engine import config  # noqa: PLC0415
    block = config.load_rulebook()["spreadsheet_intake"]
    return {"reason_tokens": block["reason_tokens"],
            "evaluation_order_per_row": block["evaluation_order_per_row"],
            "evaluation_order_note": block["evaluation_order_note"],
            "source_formats": block["source_formats"],
            "channel_utility_rates": config.channel_utility_rates()}


def op_warranty(engine, request: dict) -> dict:
    """The exact Step 3 warranty the engine binds a batch to: its version, its wording,
    and the sha256 of that wording. The console renders these bytes and shows the hash,
    so the checkbox a human ticks is the text the batch hash covers.

    The Step 2 channel utility rate sheet rides along (MODEL 2, internal operational
    metrics and NOT client charges) because this is the op the transparency surface is
    built on: one payload tells a client both what we bind them to and what a unit of each
    channel costs us, straight from the rulebook."""
    from engine import config, ingest  # noqa: PLC0415
    warranty = config.step3_warranty()
    text = str(warranty["text"])
    return {"document_version": warranty["version"], "text": text,
            "text_sha256": ingest.sha256_text(text),
            "acknowledgement_present": bool(warranty.get("acknowledgement")),
            "channel_utility_rates": config.channel_utility_rates()}


# --- daily operations (Phase 3, pass 3) -------------------------------------
# Every one of these is a thin dispatch onto engine/app.py: the bridge decides nothing.
def _client_id(request: dict) -> str:
    return str(request.get("client_id") or SANDBOX_CLIENT_ID)


def _who(request: dict) -> str:
    return str(request.get("who") or "console-operator")


def op_daily_queue(engine, request: dict) -> dict:
    _bootstrap(engine)
    return engine.console_queue(_client_id(request))


def op_mode(engine, request: dict) -> dict:
    _bootstrap(engine)
    return engine.console_mode(_client_id(request))


def op_set_mode(engine, request: dict) -> dict:
    """Switch operating mode, both directions. `full_auto` is refused by the ENGINE when
    no valid signed authorization is on file; that refusal travels up as a token."""
    _bootstrap(engine)
    client_id = _client_id(request)
    new_mode = str(request.get("new_mode") or "").strip()
    if new_mode not in ("hitl", "full_auto"):
        raise ValueError(f"new_mode must be hitl or full_auto, not {new_mode!r}")
    result = engine.switch_mode(client_id, new_mode, _who(request),
                                str(request.get("reason") or "changed from the console"))
    payload = {"result": result, "mode": engine.console_mode(client_id),
               "simulated_switch_note": (
                   "The mode switch is real — it writes client_autonomy_settings and an "
                   "immutable autonomy_mode_history row. Only the contacts themselves are "
                   "simulated.")}
    if result.get("refused"):
        payload.update({"ok": False, "refusal_token": "BLOCKED_NO_SIGNOFF",
                        "detail": result.get("reason_detail")})
    else:
        payload["ok"] = True
    return payload


def op_halt(engine, request: dict) -> dict:
    """The atomic halt behind 'Switch to Manual (HITL)'."""
    _bootstrap(engine)
    client_id = _client_id(request)
    result = engine.halt_pending_for_client(client_id, _who(request),
                                            str(request.get("reason") or ""))
    result["mode"] = engine.console_mode(client_id)
    result["queue"] = engine.console_queue(client_id)
    result["ok"] = True
    return result


def op_reviews(engine, request: dict) -> dict:
    _bootstrap(engine)
    return engine.console_reviews(_client_id(request))


def op_decide(engine, request: dict) -> dict:
    """Approve or reject one queued draft. `ok: false` carries the engine's own token."""
    _bootstrap(engine)
    debtor_id = str(request.get("debtor_id") or "").strip()
    if not debtor_id:
        raise ValueError("decide needs the debtor_id of the draft being reviewed")
    client_id = _client_id(request)
    result = engine.console_decide(client_id, debtor_id, str(request.get("action") or ""),
                                   _who(request), str(request.get("reason") or ""))
    if result.get("ok"):
        result["queue"] = engine.console_queue(client_id)
        result["reviews"] = engine.console_reviews(client_id)
    return result


def op_schedule_run(engine, request: dict) -> dict:
    """Put one contact on the schedule so the halted-run half of the toggle is visible."""
    _bootstrap(engine)
    debtor_id = str(request.get("debtor_id") or "").strip()
    if not debtor_id:
        raise ValueError("schedule_run needs a debtor_id")
    run = engine.console_schedule(debtor_id, str(request.get("channel") or "email"),
                                  request.get("due_at") or None,
                                  str(request.get("reason") or ""))
    return {"ok": True, "run": run, "mode": engine.console_mode(_client_id(request))}


def op_interactions(engine, request: dict) -> dict:
    _bootstrap(engine)
    debtor_id = str(request.get("debtor_id") or "").strip()
    if not debtor_id:
        raise ValueError("interactions needs a debtor_id")
    return engine.console_interactions(debtor_id)


def op_alerts(engine, request: dict) -> dict:
    limit = int(request.get("limit") or 60)
    return engine.console_alerts(request.get("client_id") or None, limit=limit)


def op_money(engine, request: dict) -> dict:
    _bootstrap(engine)
    return engine.console_money(_client_id(request))


def op_dispatch_approved(engine, request: dict) -> dict:
    """Agent 3's turn: fire the drafts a human has approved.

    One `engine.send` per approved record — the real claim-then-fire path, so a refused
    contact comes back with the engine's own token (BLOCKED_DNC, BLOCKED_OUTSIDE_WINDOW,
    BLOCKED_NO_BALANCE_DUE, REOPENED_AWAITING_AUDIT, ...) and a dispatched one writes a
    committed claim, a payload file and a contact-log row.
    """
    _bootstrap(engine)
    client_id = _client_id(request)
    channel = str(request.get("channel") or "email")
    who = _who(request)
    # `dispatch` defaults to FALSE and the console says so on screen. The claim is the
    # moment worth showing - it is the atomic transaction - and it is committed either
    # way. Whether the POST after it succeeds depends on a simulated vendor endpoint
    # being reachable from whatever host is serving this page, which is not something the
    # owner's click-through should hinge on. With dispatch=False the engine commits the
    # claim, writes the payload the contact would have carried, and stops; the contact log
    # row keeps status CLAIMED rather than being written up as a send that never happened.
    dispatch = bool(request.get("dispatch"))
    rows = engine.store.query(
        "SELECT debtor_id, name, aging_state FROM debtor_records "
        "WHERE client_id = ? AND approval_status = 'Approved' ORDER BY debtor_id",
        (client_id,))
    results = []
    for row in rows:
        results.append(engine.send(
            row["debtor_id"], channel, who=who, dispatch=dispatch,
            reason=str(request.get("reason") or "console: dispatch the approved draft")))
    return {
        "ok": True,
        "client_id": client_id,
        "channel": channel,
        "dispatch_attempted": dispatch,
        "attempted": len(results),
        "claimed": sum(1 for r in results if r.get("claimed")),
        "refused": sum(1 for r in results if not r.get("claimed")),
        "results": results,
        "queue": engine.console_queue(client_id),
        "reviews": engine.console_reviews(client_id),
        "money": engine.console_money(client_id),
        "note": ("Each attempt ran the atomic claim: mode, sign-off, scope, DNC, paid "
                 "status, balance and the calling window were re-read inside the "
                 "transaction immediately before the claim row was written."),
        # STANDING DECISION (team lead, 2026-10-09): the console claims and writes the
        # payload, and NEVER POSTs to a provider. The screen says so in those words, and it
        # says so because it is true, not because the vendor happens to be unreachable: the
        # simulated vendors are not reachable from every host that serves this page, and a
        # claim written up as a send that did not happen would be a lie. The claim-then-fire
        # send moment belongs to the future /demo tour, where the mock vendor is local to the
        # engine. `dispatch` stays a parameter (the engine's own send path honours it) but
        # the console never passes it as True.
        "dispatch_note": ("claimed, not sent. The claim is committed and the payload it "
                          "would have carried is on disk, but no provider POST is attempted "
                          "from the console — that is its standing behaviour, not a fallback, "
                          "so nothing here is reported as sent. Dispatch has not happened.")
                         if not dispatch else
                         ("Provider POST attempted against the simulated vendor endpoint; "
                          "the contact log carries its outcome, including a failure."),
    }


def op_board(engine, request: dict) -> dict:
    """Everything the daily console needs, from ONE engine process.

    The site runs each bridge call as its own short-lived python3 process, so composing
    the board in the engine is a real saving (~a second per page load) and it also means
    every panel on the screen was read at the same instant. The op is a plain composition
    of the same facade methods the individual ops call - it holds no logic of its own.
    """
    _bootstrap(engine)
    client_id = _client_id(request)
    return {
        "client_id": client_id,
        "queue": engine.console_queue(client_id),
        "mode": engine.console_mode(client_id),
        "reviews": engine.console_reviews(client_id),
        "money": engine.console_money(client_id),
        "alerts": engine.console_alerts(None, limit=int(request.get("limit") or 60)),
    }


def op_simulate_payment(engine, request: dict) -> dict:
    """Run the engine's real payment path against a simulated webhook signal."""
    _bootstrap(engine)
    result = engine.console_simulate_payment()
    client_id = result.get("client_id") or _client_id(request)
    result["money"] = engine.console_money(client_id)
    result["queue"] = engine.console_queue(client_id)
    # The payment path's own result carries `status` ("COMMITTED"), not `ok` - and the
    # console's close-the-loop panel is rendered on `simulation.ok`. Without this line the
    # panel would never appear even though the payment really ran, which is exactly what the
    # daily click-through found (2026-10-09): the ledger line and the DRAFT statement moved,
    # and the panel that explains them stayed hidden. Say plainly whether the op ran.
    if "ok" not in result:
        result["ok"] = "refusal_token" not in result
    return result


# --- onboarding, reports and the Integrations Hub (Phase 4) ------------------
# Same rule as every op above: a thin dispatch onto the engine facade. The Step 2 and
# Step 3 gates, the fee-tracking rebind, the report counts and the sample-file mapping
# all live in engine/, so the console cannot show a gate as open that the engine refuses,
# or a count the engine did not produce.
def op_onboarding(engine, request: dict) -> dict:
    _bootstrap(engine)
    client_id = _client_id(request)
    payload = engine.console_onboarding(client_id)
    payload["mode"] = engine.console_mode(client_id)
    return payload


def op_tracking_mode(engine, request: dict) -> dict:
    """Step 1, changed later. The engine writes the append-only trail row and rebinds the
    sign-off; a change to a choice the sign-off covered REVOKES that sign-off."""
    _bootstrap(engine)
    client_id = _client_id(request)
    new_mode = str(request.get("new_mode") or "").strip()
    if not new_mode:
        raise ValueError("tracking_mode needs a new_mode")
    result = engine.set_tracking_mode(client_id, new_mode, _who(request),
                                      str(request.get("reason") or "changed from the console"))
    payload = {"result": result, "onboarding": engine.console_onboarding(client_id)}
    payload["ok"] = bool(result.get("ok"))
    if not result.get("ok"):
        payload["refusal_token"] = result.get("refusal_token")
    return payload


def op_attest(engine, request: dict) -> dict:
    """Record a Step 2 acknowledgement or the Step 3 warranty.

    `acknowledged` is passed through EXACTLY as sent: the engine refuses anything that is
    not explicitly true, so the console cannot tick this on the operator's behalf and the
    unticked state is a real refusal, not a disabled button.
    """
    _bootstrap(engine)
    client_id = _client_id(request)
    step = str(request.get("step") or "").strip()
    if step not in ("2", "3"):
        raise ValueError("attest needs step '2' or '3'")
    acknowledged = request.get("acknowledged")
    note = str(request.get("note") or "") or None
    if step == "2":
        result = engine.record_step2_acknowledgement(client_id, acknowledged, _who(request),
                                                     source="console", note=note)
    else:
        result = engine.record_step3_attestation(client_id, acknowledged, _who(request),
                                                 source="console", note=note)
    payload = dict(result)
    payload["onboarding"] = engine.console_onboarding(client_id)
    return payload


def op_signoff(engine, request: dict) -> dict:
    """Step 4: the signature. Everything the wizard collected is passed to the real
    `capture_signoff`, which hashes the fee-tracking choice and the Step 3 attestation
    text into the reference."""
    _bootstrap(engine)
    from engine import config  # noqa: PLC0415
    client_id = _client_id(request)
    signer_name = str(request.get("signer_name") or "").strip()
    signer_title = str(request.get("signer_title") or "").strip()
    if not signer_name or not signer_title:
        result = {"ok": False, "refusal_token": "BLOCKED_SIGNOFF_INCOMPLETE",
                  "detail": "a sign-off needs a typed signer name and corporate title"}
        result["onboarding"] = engine.console_onboarding(client_id)
        return result
    scope_pairs = request.get("scope_pairs")
    if not scope_pairs:
        scope_pairs = [[o["channel"], str(o["aging_state"])] for o in config.scope_options()]
    result = engine.capture_signoff(client_id, signer_name, signer_title,
                                    who=_who(request), scope_pairs=scope_pairs,
                                    tracking_mode=request.get("tracking_mode") or None)
    result["ok"] = True
    result["onboarding"] = engine.console_onboarding(client_id)
    return result


def op_reports(engine, request: dict) -> dict:
    """Weekly and monthly counts from the engine's own logs. Real counts only."""
    _bootstrap(engine)
    return engine.console_reports(_client_id(request))


def op_integrations(engine, request: dict) -> dict:
    """The SIMULATED Integrations Hub: a sample export through the real mapper."""
    _bootstrap(engine)
    return engine.integrations_hub(client_id=_client_id(request),
                                   sample=request.get("sample") or None)


OPS = {
    "status": op_status,
    "bootstrap": op_bootstrap,
    "preview": op_preview,
    "register": op_register,
    "commit": op_commit,
    "batches": op_batches,
    "verify": op_verify,
    "sample": op_sample,
    "vocabulary": op_vocabulary,
    "warranty": op_warranty,
    # --- daily operations (Phase 3, pass 3) --------------------------------
    "daily_queue": op_daily_queue,
    "mode": op_mode,
    "set_mode": op_set_mode,
    "halt": op_halt,
    "reviews": op_reviews,
    "decide": op_decide,
    "schedule_run": op_schedule_run,
    "dispatch_approved": op_dispatch_approved,
    "board": op_board,
    "interactions": op_interactions,
    "alerts": op_alerts,
    "money": op_money,
    "simulate_payment": op_simulate_payment,
    # --- onboarding, reports and the Integrations Hub (Phase 4) ------------
    "onboarding": op_onboarding,
    "tracking_mode": op_tracking_mode,
    "attest": op_attest,
    "signoff": op_signoff,
    "reports": op_reports,
    "integrations": op_integrations,
}


def handle(request: dict) -> dict:
    op = str(request.get("op") or "").strip()
    if op not in OPS:
        return {"bridge_ok": False, "error": "UNKNOWN_OP",
                "detail": f"op {op!r} is not one of {sorted(OPS)}"}
    try:
        engine = _engine()
    except Exception as exc:  # a broken/absent engine is reported, never faked
        return {"bridge_ok": False, "error": "ENGINE_UNAVAILABLE", "detail": f"{exc!r}"}
    try:
        payload = OPS[op](engine, request)
        return {"bridge_ok": True, "op": op, "payload": payload}
    except ValueError as exc:
        return {"bridge_ok": False, "op": op, "error": "BAD_REQUEST", "detail": str(exc)}
    except Exception as exc:
        return {"bridge_ok": False, "op": op, "error": "ENGINE_ERROR",
                "detail": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc().splitlines()[-6:]}
    finally:
        try:
            # engine.close(), not engine.store.close(): the facade also persists the run
            # queue to RECOVERAI_CONSOLE_RUNS, which is what makes "switch to manual halts
            # the schedule" survive from one bridge process to the next.
            engine.close()
        except Exception:
            pass


def main() -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        response = {"bridge_ok": False, "error": "MALFORMED_REQUEST_JSON", "detail": str(exc)}
    else:
        if not isinstance(request, dict):
            response = {"bridge_ok": False, "error": "MALFORMED_REQUEST_JSON",
                        "detail": "the request must be a JSON object"}
        else:
            response = handle(request)
    sys.stdout.write(json.dumps(response))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
