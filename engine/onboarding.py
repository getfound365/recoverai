"""Onboarding back end (no UI in this task).

Four jobs:

1. Capture a digital sign-off: the authorization document version plus the typed
   signer name and corporate title, hashed into an encrypted-looking signature
   reference (sha256 over client_id + signer_name + signer_title + document version
   + the fee-tracking choice + the Step 3 attestation hash + timestamp). The client is
   left in `hitl` after signing - signing is not a licence to fire automatically, it is
   only the precondition for the owner choosing to.

2. Switch operating mode. `full_auto` is refused unless a valid sign-off exists for
   that client_id, every attempt is appended to the immutable timeline, and revoking
   or expiring a sign-off drops the client back to `hitl`.

3. Step 1 - the fee-tracking choice (`integration` / `manual_attestation` / `hybrid`).
   The client chooses WHERE we look, never WHETHER we look. The choice is bound into the
   signature hash, so changing it later is a change to the arrangement: the standing
   sign-off is revoked, the client drops to `hitl`, and a fresh signature is required.
   Every change is an append-only row in `collections_tracking_choices`.

4. Steps 2 and 3 - the transparent acknowledgement and the data warranty. Both are
   UNSELECTED by default and the backend REFUSES to record either until the flag is
   explicitly TRUE. The exact checkbox text is stored with the sha256 of that text and
   its version, in the append-only `onboarding_attestations` table. The Step 3 row is
   the real gate the spreadsheet intake binds a batch to.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Sequence

from . import clock, config
from .audit import AuditLog


# --- sign-off ---------------------------------------------------------------
def compute_signature_reference(client_id: str, signer_name: str, signer_title: str,
                                document_version: str, timestamp: str,
                                tracking_mode: Optional[str] = None,
                                step3_attestation_sha256: Optional[str] = None) -> str:
    """The signature reference: sha256 over the bytes the client actually agreed to.

    The last two segments are optional so a reference computed without them stays exactly
    what it always was. When they are present they are part of the hash - which is the
    point: the fee-tracking choice and the Step 3 warranty text are covered by the
    signature, not merely recorded beside it.
    """
    parts = [client_id, signer_name, signer_title, document_version]
    if tracking_mode is not None:
        parts.append(f"tracking_mode={tracking_mode}")
    if step3_attestation_sha256 is not None:
        parts.append(f"step3_attestation_sha256={step3_attestation_sha256}")
    parts.append(timestamp)
    return "sha256:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def signature_covers() -> List[str]:
    """The fields the signature reference covers, for the console to render verbatim."""
    return ["client_id", "signer_name", "signer_title", "authorization_document_version",
            "fee_tracking_mode (step 1)", "step3_attestation_text_sha256 (step 3)",
            "signed_timestamp"]


def valid_scope_pairs() -> set[tuple[str, str]]:
    return {(o["channel"], str(o["aging_state"])) for o in config.scope_options()}


def normalize_scope(channels: Optional[Sequence[str]] = None,
                    aging_states: Optional[Sequence[str]] = None,
                    scope_pairs: Optional[Sequence[Sequence[str]]] = None) -> Dict[str, Any]:
    """Resolve the sign-off scope into (channels, aging states, precise pairs).

    The onboarding wizard asks one plain-English question per channel x aging-state option,
    so the sign-off stores the exact pair set the client ticked. Passing channels and aging
    states without pairs means the cross product (the wizard's matrix behaviour).
    """
    if scope_pairs:
        pairs = [(str(c), str(a)) for c, a in scope_pairs]
    else:
        pairs = [(str(c), str(a)) for c in (channels or []) for a in (aging_states or [])]
    if not pairs:
        raise ValueError("a sign-off must authorize at least one channel x aging-state option")
    unknown = [p for p in pairs if p not in valid_scope_pairs()]
    if unknown:
        raise ValueError(f"scope entries are not in the rulebook: {unknown}")
    pairs = sorted(set(pairs), key=lambda p: (p[1], p[0]))
    return {
        "pairs": pairs,
        "channels": sorted({p[0] for p in pairs}),
        "aging_states": sorted({p[1] for p in pairs}, key=lambda s: (len(s), s)),
    }


def plain_english_scope(scope_pairs: Sequence[Sequence[str]]) -> List[str]:
    wanted = {(str(c), str(a)) for c, a in scope_pairs}
    out = []
    for option in config.scope_options():
        if (option["channel"], str(option["aging_state"])) in wanted:
            out.append(option["plain_english"])
    return out


def signoff_state(row: Dict[str, Any], now=None) -> Dict[str, Any]:
    """Is this sign-off valid right now? Fail closed on anything unreadable."""
    if not row:
        return {"valid": False, "reason": "no autonomy settings row for this client_id"}
    if not row.get("signature_reference"):
        return {"valid": False, "reason": "no sign-off on file"}
    if int(row.get("signature_revoked") or 0) == 1:
        return {"valid": False, "reason": f"sign-off revoked at {row.get('signature_revoked_at')}"}
    if int(row.get("has_valid_signoff") or 0) != 1:
        return {"valid": False, "reason": "sign-off flag is not set"}
    expires = row.get("signoff_expires_at")
    if expires:
        if clock.parse_ts(str(expires)) < (now or clock.now_utc()):
            return {"valid": False, "reason": f"sign-off expired at {expires}"}
    return {
        "valid": True,
        "reason": f"signed by {row.get('signer_name')} ({row.get('signer_title')}) "
                  f"under document {row.get('authorization_document_version')}",
    }


def fetch_settings(store, client_id: str) -> Optional[Dict[str, Any]]:
    return store.query_one("SELECT * FROM client_autonomy_settings WHERE client_id = ?", (client_id,))


def ensure_client(store, client_id: str) -> Dict[str, Any]:
    """Insert the client in the default mode if we have never seen it."""
    row = fetch_settings(store, client_id)
    if row:
        return row
    with store.tx(immediate=True):
        store.execute(
            """INSERT INTO client_autonomy_settings
                 (client_id, operating_mode, has_valid_signoff, authorized_channels,
                  authorized_aging_states, mode_version, claim_seq, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(client_id) DO NOTHING""",
            (client_id, config.default_mode(), 0, json.dumps([]), json.dumps([]), 0, 0, clock.iso()),
        )
    return fetch_settings(store, client_id)  # type: ignore[return-value]


def capture_signoff(store, client_id: str, signer_name: str, signer_title: str,
                    channels: Optional[Sequence[str]] = None, aging_states: Optional[Sequence[str]] = None,
                    who: str = "unknown", audit: Optional[AuditLog] = None, now=None,
                    validity_days: Optional[int] = None,
                    scope_pairs: Optional[Sequence[Sequence[str]]] = None,
                    tracking_mode: Optional[str] = None) -> Dict[str, Any]:
    name = (signer_name or "").strip()
    title = (signer_title or "").strip()
    if not name or not title:
        raise ValueError("sign-off requires a typed signer name and corporate title")
    scope = normalize_scope(channels, aging_states, scope_pairs)

    ensure_client(store, client_id)
    document_version = config.authorization_document()["version"]
    timestamp = clock.iso(now)
    # Step 1's choice and step 3's warranty text are part of the hash, not recorded
    # beside it: a signature that does not describe the arrangement is not a signature
    # for that arrangement. Both default to what is on file, so a caller that has not
    # walked the ladder still signs under the default choice and no Step 3 binding.
    settings = fetch_settings(store, client_id) or {}
    chosen_tracking = str(tracking_mode or settings.get("tracking_mode")
                          or config.default_fee_tracking_mode())
    if chosen_tracking not in config.fee_tracking_mode_ids():
        raise ValueError(f"unknown fee-tracking mode {chosen_tracking!r}; the rulebook knows "
                         f"{config.fee_tracking_mode_ids()}")
    step3 = current_attestation(store, client_id, ATTESTATION_STEP3)
    signature_reference = compute_signature_reference(
        client_id, name, title, document_version, timestamp,
        tracking_mode=chosen_tracking,
        step3_attestation_sha256=(str(step3["text_sha256"]) if step3 else None))
    days = validity_days if validity_days is not None else int(config.authorization_document()["validity_days"])
    expires_at = clock.iso(clock.plus(days=days, base=now or clock.now_utc()))

    with store.tx(immediate=True):
        store.execute(
            """UPDATE client_autonomy_settings SET
                 has_valid_signoff = 1,
                 signer_name = ?, signer_title = ?, signature_reference = ?,
                 authorized_channels = ?, authorized_aging_states = ?, authorized_scope_pairs = ?,
                 signed_timestamp = ?, authorization_document_version = ?,
                 signature_revoked = 0, signature_revoked_at = NULL, signoff_expires_at = ?,
                 tracking_mode = ?, step3_attestation_id = ?, updated_at = ?
               WHERE client_id = ?""",
            (name, title, signature_reference, json.dumps(scope["channels"]),
             json.dumps(scope["aging_states"]), json.dumps([f"{c}:{a}" for c, a in scope["pairs"]]),
             timestamp, document_version, expires_at,
             chosen_tracking, (str(step3["attestation_id"]) if step3 else None), timestamp, client_id),
        )

    result = {
        "client_id": client_id,
        "signer_name": name,
        "signer_title": title,
        "authorization_document_version": document_version,
        "authorization_text": config.authorization_document()["text"],
        "signature_reference": signature_reference,
        "authorized_channels": scope["channels"],
        "authorized_aging_states": scope["aging_states"],
        "authorized_scope_pairs": [f"{c}:{a}" for c, a in scope["pairs"]],
        "plain_english_scope": plain_english_scope(scope["pairs"]),
        "signed_timestamp": timestamp,
        "signoff_expires_at": expires_at,
        "operating_mode_after_signoff": fetch_settings(store, client_id)["operating_mode"],
        # the hash binding, stated as what it is: which bytes the reference covers
        "signature_binding": {
            "tracking_mode": chosen_tracking,
            "step3_attestation_id": (str(step3["attestation_id"]) if step3 else None),
            "step3_attestation_text_sha256": (str(step3["text_sha256"]) if step3 else None),
            "step3_attestation_document_version": (str(step3["document_version"]) if step3 else None),
            "covers": signature_covers(),
            "note": ("Changing the fee-tracking choice after this point revokes this "
                     "sign-off: the reference above covers the choice it was signed under."),
        },
        "step3_attestation_bound": bool(step3),
    }
    if audit:
        audit.record("signoff_created", **{k: v for k, v in result.items() if k != "authorization_text"},
                     plain_english=result["plain_english_scope"])
    return result


def scope_allows(row: Dict[str, Any], channel: str, aging_state: Optional[str]) -> bool:
    """Does this sign-off authorize a contact on this channel for this aging state?

    The precise channel x aging-state pairs are what count; the channel and aging-state
    lists are the human-readable summary. For a row written before pairs existed we fall
    back to the cross product, which is what the wizard's matrix means.
    """
    if not row:
        return False
    try:
        pairs = json.loads(row.get("authorized_scope_pairs") or "[]")
    except (TypeError, ValueError):
        pairs = []
    if pairs:
        return f"{channel}:{aging_state}" in pairs
    try:
        channels = json.loads(row.get("authorized_channels") or "[]")
        states = [str(s) for s in json.loads(row.get("authorized_aging_states") or "[]")]
    except (TypeError, ValueError):
        return False
    return channel in channels and str(aging_state) in states


# --- mode switching ---------------------------------------------------------
def _append_history(store, client_id: str, who: str, old_mode: str, new_mode: str,
                    reason: str, mode_version: int, timestamp: Optional[str] = None) -> int:
    # Millisecond UTC: the mode timeline is the record of who changed what and when, and
    # a flip that lands in the same second as the claim it must beat is exactly the pair
    # whose order has to be provable. (Second-precision rows written before this change
    # are left exactly as they were - this table is append-only.)
    return store.insert(
        """INSERT INTO autonomy_mode_history
             (who, old_mode, new_mode, timestamp, reason, mode_version, client_id)
           VALUES (?,?,?,?,?,?,?)""",
        (who, old_mode, new_mode, timestamp or clock.iso_ms(), reason, mode_version, client_id),
    )


def switch_mode(store, client_id: str, new_mode: str, who: str, reason: str,
                audit: Optional[AuditLog] = None, runqueue=None) -> Dict[str, Any]:
    """Set the client's operating mode. Both directions, logged, full_auto gated."""
    available = config.operating_modes_cfg()["available"]
    if new_mode not in available:
        raise ValueError(f"unknown operating mode {new_mode!r}; rulebook allows {available}")
    ensure_client(store, client_id)
    now = clock.now_utc()
    result: Dict[str, Any] = {"client_id": client_id, "requested_mode": new_mode, "who": who, "reason": reason}

    with store.tx(immediate=True):
        row = store.query_one("SELECT * FROM client_autonomy_settings WHERE client_id = ?", (client_id,))
        old_mode = row["operating_mode"]
        mode_version = int(row["mode_version"])
        state = signoff_state(row, now)

        if new_mode == "full_auto" and not state["valid"]:
            _append_history(store, client_id, who, old_mode, old_mode,
                            f"REFUSED attempt to switch to full_auto: {state['reason']}",
                            mode_version)
            result.update({"changed": False, "refused": True, "old_mode": old_mode,
                           "new_mode": old_mode, "reason_detail": state["reason"],
                           "mode_version": mode_version})
        elif new_mode == old_mode:
            result.update({"changed": False, "refused": False, "old_mode": old_mode,
                           "new_mode": new_mode, "reason_detail": "already in this mode",
                           "mode_version": mode_version})
        else:
            new_version = mode_version + 1
            store.execute(
                "UPDATE client_autonomy_settings SET operating_mode = ?, mode_version = ?, updated_at = ? "
                "WHERE client_id = ?",
                (new_mode, new_version, clock.iso(now), client_id),
            )
            history_id = _append_history(store, client_id, who, old_mode, new_mode, reason, new_version)
            result.update({"changed": True, "refused": False, "old_mode": old_mode, "new_mode": new_mode,
                           "mode_version": new_version, "history_id": history_id})

    if result.get("changed") and new_mode == "hitl" and runqueue is not None:
        halted = runqueue.halt_pending_for_client(client_id, "mode flipped to hitl")
        result["halted_runs"] = [r.run_id for r in halted]
        if audit:
            for run in halted:
                audit.record("run_halted", run_id=run.run_id, debtor_id=run.debtor_id,
                             client_id=client_id, channel=run.channel, reason="mode flipped to hitl")

    if audit:
        audit.record("mode_change" if result.get("changed") else
                     ("mode_change_refused" if result.get("refused") else "mode_change_noop"), **result)
    return result


def revoke_signoff(store, client_id: str, who: str, reason: str,
                   audit: Optional[AuditLog] = None, runqueue=None) -> Dict[str, Any]:
    """Revoke the sign-off; a client in full_auto drops back to hitl immediately."""
    ensure_client(store, client_id)
    now = clock.now_utc()
    with store.tx(immediate=True):
        row = store.query_one("SELECT * FROM client_autonomy_settings WHERE client_id = ?", (client_id,))
        old_mode = row["operating_mode"]
        mode_version = int(row["mode_version"])
        store.execute(
            """UPDATE client_autonomy_settings SET has_valid_signoff = 0, signature_revoked = 1,
                 signature_revoked_at = ?, updated_at = ? WHERE client_id = ?""",
            (clock.iso(now), clock.iso(now), client_id),
        )
        result: Dict[str, Any] = {"client_id": client_id, "who": who, "reason": reason,
                                  "old_mode": old_mode, "revoked_at": clock.iso(now)}
        if old_mode == "full_auto":
            new_version = mode_version + 1
            store.execute(
                "UPDATE client_autonomy_settings SET operating_mode = 'hitl', mode_version = ?, updated_at = ? "
                "WHERE client_id = ?", (new_version, clock.iso(now), client_id))
            history_id = _append_history(store, client_id, who, old_mode, "hitl",
                                         f"sign-off revoked: {reason}", new_version)
            result.update({"new_mode": "hitl", "mode_version": new_version, "history_id": history_id,
                           "dropped_to_hitl": True})
        else:
            result.update({"new_mode": old_mode, "mode_version": mode_version, "dropped_to_hitl": False})
    if runqueue is not None and result.get("dropped_to_hitl"):
        halted = runqueue.halt_pending_for_client(client_id, "sign-off revoked")
        result["halted_runs"] = [r.run_id for r in halted]
    if audit:
        audit.record("signoff_revoked", **result)
        if result.get("dropped_to_hitl"):
            audit.record("mode_change", client_id=client_id, who=who, old_mode=result["old_mode"],
                         new_mode="hitl", reason="sign-off revoked", changed=True,
                         mode_version=result["mode_version"])
    return result


def sweep_expired_signoffs(store, audit: Optional[AuditLog] = None, runqueue=None) -> List[Dict[str, Any]]:
    """Any client whose sign-off has expired falls back to hitl. Call before a batch."""
    downgraded = []
    rows = store.query("SELECT * FROM client_autonomy_settings WHERE has_valid_signoff = 1")
    for row in rows:
        state = signoff_state(row)
        if state["valid"]:
            continue
        if "expired" not in state["reason"] and "revoked" not in state["reason"]:
            continue
        with store.tx(immediate=True):
            store.execute(
                "UPDATE client_autonomy_settings SET has_valid_signoff = 0, updated_at = ? WHERE client_id = ?",
                (clock.iso(), row["client_id"]),
            )
            old_mode = row["operating_mode"]
            mode_version = int(row["mode_version"])
            if old_mode == "full_auto":
                mode_version += 1
                store.execute(
                    "UPDATE client_autonomy_settings SET operating_mode = 'hitl', mode_version = ? WHERE client_id = ?",
                    (mode_version, row["client_id"]))
                _append_history(store, row["client_id"], "system", old_mode, "hitl",
                                f"sign-off no longer valid: {state['reason']}", mode_version)
        entry = {"client_id": row["client_id"], "old_mode": old_mode, "new_mode": "hitl" if old_mode == "full_auto" else old_mode,
                 "reason": state["reason"]}
        downgraded.append(entry)
        if audit:
            audit.record("signoff_expired", **entry)
    return downgraded


# --- Phase 4: step 1 - how collections are tracked ---------------------------
#: The three rulebook modes, in the order the wizard offers them. The choice is bound
#: into the signature hash (see compute_signature_reference), so this is an agreement
#: term, not a preference.
TRACKING_MODES = ("integration", "manual_attestation", "hybrid")

#: One token per refusal. A refusal is a payload, never an exception the console has to
#: invent an explanation for.
REFUSAL_TRACKING_MODE_UNKNOWN = "BLOCKED_UNKNOWN_TRACKING_MODE"
REFUSAL_STEP2_NOT_TICKED = "BLOCKED_STEP2_ACKNOWLEDGEMENT_NOT_TICKED"
REFUSAL_STEP3_NOT_TICKED = "BLOCKED_STEP3_WARRANTY_NOT_TICKED"
REFUSAL_UNKNOWN_ATTESTATION = "BLOCKED_UNKNOWN_ATTESTATION_TYPE"

ATTESTATION_STEP2 = "step2_fee_tracking_ack"
ATTESTATION_STEP3 = "step3_data_warranty"
ATTESTATION_TYPES = (ATTESTATION_STEP2, ATTESTATION_STEP3)

ATTESTATION_REFUSAL = {ATTESTATION_STEP2: REFUSAL_STEP2_NOT_TICKED,
                       ATTESTATION_STEP3: REFUSAL_STEP3_NOT_TICKED}


def tracking_modes() -> List[Dict[str, Any]]:
    """The rulebook's fee-tracking options, with the engine's default marked."""
    default = config.default_fee_tracking_mode()
    out = []
    for mode in config.fee_tracking_modes():
        entry = dict(mode)
        entry["is_default"] = str(mode["id"]) == default
        entry["is_current"] = False
        out.append(entry)
    return out


def tracking_state(store, client_id: str) -> Dict[str, Any]:
    """Step 1's choice as it stands: the mode, its version, the options and the trail."""
    settings = ensure_client(store, client_id)
    current = str(settings.get("tracking_mode") or config.default_fee_tracking_mode())
    state = signoff_state(settings)
    history = store.query(
        "SELECT * FROM collections_tracking_choices WHERE client_id = ? ORDER BY choice_id",
        (client_id,))
    return {
        "client_id": client_id,
        "current_mode": current,
        "current_mode_detail": config.fee_tracking_mode(current) if current in config.fee_tracking_mode_ids() else None,
        "choice_version": int(settings.get("tracking_mode_version") or 0),
        "default_mode": config.default_fee_tracking_mode(),
        "options": tracking_modes(),
        "history": history,
        "history_count": len(history),
        "rule": config.fee_tracking_cfg()["rule"],
        "change_rule": config.fee_tracking_cfg()["change_rule"],
        "signoff": state,
        "signoff_binding_current": bool(
            state["valid"] and str(settings.get("tracking_mode") or "") == current),
        "honest_note": (
            "The choice records where we look, never whether we look. It is inside the "
            "signature hash: changing it revokes the sign-off on file and requires a fresh "
            "signature under the new choice, because a signature that no longer describes "
            "the arrangement is not a signature for that arrangement."),
    }


def set_tracking_mode(store, client_id: str, new_mode: str, who: str, reason: str = "",
                      audit: Optional[AuditLog] = None, runqueue=None,
                      note: Optional[str] = None) -> Dict[str, Any]:
    """Step 1, changed later. Append-only trail, and a real rebind of the sign-off.

    Writing a row is not enough on its own: the standing sign-off was hashed over the OLD
    choice, so it stops describing the arrangement. The engine therefore revokes it -
    which drops a `full_auto` client back to `hitl` and halts its schedule - and the
    console shows that a fresh signature is required.
    """
    if new_mode not in config.fee_tracking_mode_ids():
        return {"ok": False, "refusal_token": REFUSAL_TRACKING_MODE_UNKNOWN,
                "client_id": client_id, "new_mode": new_mode,
                "detail": f"unknown fee-tracking mode {new_mode!r}; the rulebook knows "
                          f"{config.fee_tracking_mode_ids()}"}
    settings = ensure_client(store, client_id)
    old_mode = str(settings.get("tracking_mode") or config.default_fee_tracking_mode())
    current_reference = settings.get("signature_reference")
    state = signoff_state(settings)
    rebind_required = bool(state["valid"])
    rebind_token = "SIGNOFF_REBIND_REQUIRED" if rebind_required else "NO_SIGNOFF_ON_FILE"
    if new_mode == old_mode:
        result = {"ok": True, "changed": False, "client_id": client_id, "who": who,
                  "old_mode": old_mode, "new_mode": new_mode, "reason": reason,
                  "choice_version": int(settings.get("tracking_mode_version") or 0),
                  "signoff_rebind_required": False, "rebind_token": "NO_CHANGE",
                  "detail": "already the client's fee-tracking mode; nothing was written "
                            "to the append-only trail"}
        if audit:
            audit.record("tracking_mode_unchanged", **result)
        return result

    version = int(settings.get("tracking_mode_version") or 0) + 1
    now, now_ms = clock.iso(), clock.iso_ms()
    with store.tx(immediate=True):
        store.execute(
            "UPDATE client_autonomy_settings SET tracking_mode = ?, tracking_mode_version = ?, "
            "updated_at = ? WHERE client_id = ?", (new_mode, version, now, client_id))
        choice_id = store.insert(
            """INSERT INTO collections_tracking_choices
                 (client_id, who, old_mode, new_mode, timestamp, timestamp_ms, reason,
                  choice_version, bound_signature_reference, signoff_rebind_required,
                  rebind_token, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (client_id, who, old_mode, new_mode, now, now_ms, reason, version,
             current_reference, 1 if rebind_required else 0, rebind_token,
             "" if note is None else str(note)),
        )
    revocation = None
    if rebind_required:
        revocation = revoke_signoff(
            store, client_id, who,
            f"fee-tracking mode changed {old_mode} -> {new_mode}: the sign-off on file was "
            f"hashed over the old choice, so a fresh signature is required",
            audit, runqueue)
    result = {
        "ok": True, "changed": True, "client_id": client_id, "who": who,
        "old_mode": old_mode, "new_mode": new_mode, "reason": reason,
        "choice_id": choice_id, "choice_version": version, "changed_at_ms": now_ms,
        "signoff_rebind_required": rebind_required, "rebind_token": rebind_token,
        "revoked_signature_reference": current_reference if rebind_required else None,
        "revocation": revocation,
        "plain_english": (
            f"Fee tracking is now '{new_mode}' (was '{old_mode}'), recorded at {now_ms}. "
            + ("The sign-off on file was revoked because the choice it was signed under has "
               "changed; the client must sign again before any mode above hitl is available."
               if rebind_required else
               "No sign-off was on file, so nothing had to be re-bound.")),
        "honest_note": ("Nothing about this choice changes what we claim to be able to read "
                        "today: no live accounting integration exists, so an 'integration' "
                        "choice records an intent, not a connection."),
    }
    if audit:
        audit.record("tracking_mode_changed", **{k: v for k, v in result.items() if k != "revocation"})
    return result


# --- Phase 4: steps 2 and 3 - the recorded attestations ----------------------
#: The fields attestation_sha256 covers. The exact checkbox text is inside the hash, so
#: the wording an acknowledgement was given under is recoverable from the row itself.
ATTESTATION_HASH_FIELDS = (
    "client_id", "attestation_type", "document_version", "rulebook_version",
    "attestation_text", "text_sha256", "acknowledged", "acknowledged_by",
    "acknowledged_at_ms", "source", "tracking_mode_at_attestation",
)


def _sha256_text(text: Any) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _sha256_canonical(obj: Any) -> str:
    return _sha256_text(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str))


def attestation_body(attestation_type: str) -> Dict[str, str]:
    """The document version and the EXACT checkbox text for one acknowledgement."""
    if attestation_type == ATTESTATION_STEP3:
        block = config.step3_warranty()
    elif attestation_type == ATTESTATION_STEP2:
        block = config.step2_acknowledgement()
    else:
        raise KeyError(f"unknown attestation type {attestation_type!r}; the engine knows "
                       f"{list(ATTESTATION_TYPES)}")
    return {"document_version": str(block["version"]), "text": str(block["text"])}


def attestation_content(row: Dict[str, Any]) -> Dict[str, Any]:
    """One canonical shape for the hashed fields, from a fresh dict or a row read back."""
    content: Dict[str, Any] = {}
    for field_name in ATTESTATION_HASH_FIELDS:
        value = row.get(field_name)
        if field_name == "acknowledged":
            content[field_name] = int(value or 0)
        else:
            content[field_name] = "" if value is None else str(value)
    return content


def attestation_sha256(row: Dict[str, Any]) -> str:
    return _sha256_canonical(attestation_content(row))


def text_sha256(attestation_type: str) -> str:
    return _sha256_text(attestation_body(attestation_type)["text"])


def record_attestation(store, client_id: str, attestation_type: str, acknowledged: Any,
                       who: str = "unknown", source: str = "engine",
                       note: Optional[str] = None, audit: Optional[AuditLog] = None) -> Dict[str, Any]:
    """Append one onboarding acknowledgement. UNSELECTED by default: refused until TRUE.

    The flag must be TRUE - not truthy-by-accident, not a default. `acknowledged` is
    compared against True (and 1) explicitly, so a missing field, a string, or a
    default-constructed form object refuses rather than silently attesting.
    """
    if attestation_type not in ATTESTATION_TYPES:
        return {"ok": False, "refusal_token": REFUSAL_UNKNOWN_ATTESTATION,
                "client_id": client_id, "attestation_type": attestation_type,
                "detail": f"unknown attestation type {attestation_type!r}; the engine knows "
                          f"{list(ATTESTATION_TYPES)}"}
    body = attestation_body(attestation_type)
    if acknowledged is not True and int(acknowledged or 0) != 1:
        return {"ok": False, "refusal_token": ATTESTATION_REFUSAL[attestation_type],
                "client_id": client_id, "attestation_type": attestation_type,
                "acknowledged": False,
                "document_version": body["document_version"],
                "checkbox_default": False,
                "text": body["text"],
                "text_sha256": _sha256_text(body["text"]),
                "detail": ("the checkbox is unselected by default and must be explicitly "
                           "TRUE before this acknowledgement can be recorded; nothing was "
                           "written")}
    settings = ensure_client(store, client_id)
    tracking_mode = str(settings.get("tracking_mode") or config.default_fee_tracking_mode())
    previous = current_attestation(store, client_id, attestation_type)
    now, now_ms = clock.iso(), clock.iso_ms()
    content = {
        "client_id": client_id,
        "attestation_type": attestation_type,
        "document_version": body["document_version"],
        "rulebook_version": config.rulebook_version(),
        "attestation_text": body["text"],
        "text_sha256": _sha256_text(body["text"]),
        "acknowledged": 1,
        "acknowledged_by": who,
        "acknowledged_at_ms": now_ms,
        "source": source,
        "tracking_mode_at_attestation": tracking_mode,
    }
    attestation_id = uuid.uuid4().hex
    digest = attestation_sha256(content)
    with store.tx(immediate=True):
        store.execute(
            """INSERT INTO onboarding_attestations
                 (attestation_id, client_id, attestation_type, document_version,
                  rulebook_version, attestation_text, text_sha256, acknowledged,
                  acknowledged_by, acknowledged_at_ms, acknowledged_at, source,
                  tracking_mode_at_attestation, attestation_sha256, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (attestation_id, client_id, attestation_type, content["document_version"],
             content["rulebook_version"], content["attestation_text"], content["text_sha256"],
             content["acknowledged"], content["acknowledged_by"], content["acknowledged_at_ms"],
             now, content["source"], content["tracking_mode_at_attestation"], digest,
             "" if note is None else str(note)),
        )
    record = store.query_one("SELECT * FROM onboarding_attestations WHERE attestation_id = ?",
                             (attestation_id,))
    result = {
        "ok": True,
        "client_id": client_id,
        "attestation": record,
        "attestation_id": attestation_id,
        "attestation_type": attestation_type,
        "document_version": content["document_version"],
        "rulebook_version": content["rulebook_version"],
        "text_sha256": content["text_sha256"],
        "attestation_sha256": digest,
        "tracking_mode_at_attestation": tracking_mode,
        "supersedes": (previous["attestation_id"] if previous else None),
        "acknowledged_at_ms": now_ms,
        "plain_english": (f"{attestation_type} recorded for {client_id} at {now_ms} against "
                          f"document {content['document_version']} "
                          f"(text sha256 {content['text_sha256'][:12]}...)"),
        "honest_note": ("A hash proves this row has not changed since it was written, and "
                        "where the wording came from. It does not prove who ticked it: "
                        "acknowledged_by records the identity the operator typed, not a "
                        "verified person."),
    }
    if audit:
        audit.record("onboarding_attestation_recorded", **{k: v for k, v in result.items()
                                                           if k != "attestation"})
    return result


def record_step2_acknowledgement(store, client_id: str, acknowledged: Any, who: str = "unknown",
                                 source: str = "engine", note: Optional[str] = None,
                                 audit: Optional[AuditLog] = None) -> Dict[str, Any]:
    """Step 2: the mandatory fee-and-recovery-tracking transparency acknowledgement."""
    return record_attestation(store, client_id, ATTESTATION_STEP2, acknowledged, who, source,
                              note, audit)


def record_step3_attestation(store, client_id: str, acknowledged: Any, who: str = "unknown",
                             source: str = "engine", note: Optional[str] = None,
                             audit: Optional[AuditLog] = None) -> Dict[str, Any]:
    """Step 3: THE REAL GATE. Contact data accurate/active/consented, AS-IS beta, indemnity."""
    return record_attestation(store, client_id, ATTESTATION_STEP3, acknowledged, who, source,
                              note, audit)


def attestations(store, client_id: str,
                 attestation_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """The append-only trail: every acknowledgement ever recorded for this client."""
    if attestation_type:
        return store.query(
            "SELECT * FROM onboarding_attestations WHERE client_id = ? AND attestation_type = ? "
            "ORDER BY seq", (client_id, attestation_type))
    return store.query(
        "SELECT * FROM onboarding_attestations WHERE client_id = ? ORDER BY seq", (client_id,))


def current_attestation(store, client_id: str,
                        attestation_type: str) -> Optional[Dict[str, Any]]:
    """The acknowledgement in force: the latest row of that type for this client."""
    return store.query_one(
        "SELECT * FROM onboarding_attestations WHERE client_id = ? AND attestation_type = ? "
        "ORDER BY seq DESC LIMIT 1", (client_id, attestation_type))


def step3_gate_state(store, client_id: str) -> Dict[str, Any]:
    """Can this client's files be registered for import? The Step 3 gate, stated plainly."""
    row = current_attestation(store, client_id, ATTESTATION_STEP3)
    body = attestation_body(ATTESTATION_STEP3)
    return {
        "client_id": client_id,
        "attested": row is not None,
        "gate_open": row is not None,
        "attestation": row,
        "attestation_id": (row["attestation_id"] if row else None),
        "text_sha256": (row["text_sha256"] if row else _sha256_text(body["text"])),
        "document_version": body["document_version"],
        "refusal_token": None if row is not None else "BLOCKED_NO_STEP3_ATTESTATION",
        "detail": ("a Step 3 attestation is on file for this client"
                   if row is not None else
                   "no Step 3 attestation is on file: the backend refuses to register an "
                   "ingestion batch, because there is no warranty for the batch to bind to"),
    }


def step3_gate(store, client_id: str) -> Optional[Dict[str, Any]]:
    """The attestation a batch binds to, or None. The intake's single source of truth."""
    return current_attestation(store, client_id, ATTESTATION_STEP3)


def onboarding_state(store, client_id: str) -> Dict[str, Any]:
    """The whole ladder, as the console renders it. Reads only; decides nothing."""
    settings = ensure_client(store, client_id)
    step2 = current_attestation(store, client_id, ATTESTATION_STEP2)
    step3 = current_attestation(store, client_id, ATTESTATION_STEP3)
    step2_body = config.step2_acknowledgement()
    step3_body = config.step3_warranty()
    signoff = signoff_state(settings)
    return {
        "client_id": client_id,
        "as_of": clock.iso_ms(),
        "step_order": config.onboarding_step_order(),
        "tracking": tracking_state(store, client_id),
        "step2": {
            "key": "fee_recovery_transparency",
            "title": "Fee and recovery tracking transparency",
            "document_version": str(step2_body["version"]),
            "text": str(step2_body["text"]),
            "text_sha256": _sha256_text(str(step2_body["text"])),
            "tooltip": str(step2_body.get("tooltip") or ""),
            "invoice_modification_warning": str(step2_body.get("invoice_modification_warning") or ""),
            "rule": str(step2_body.get("acknowledgement_rule") or ""),
            "checkbox_default": False,
            "acknowledged": step2 is not None,
            "acknowledged_at_ms": (step2["acknowledged_at_ms"] if step2 else None),
            "acknowledged_by": (step2["acknowledged_by"] if step2 else None),
            "record": step2,
        },
        "step3": {
            "key": "step3_warranty",
            "title": "Step 3: the data warranty",
            "document_version": str(step3_body["version"]),
            "text": str(step3_body["text"]),
            "text_sha256": _sha256_text(str(step3_body["text"])),
            "checkbox_default": False,
            "attested": step3 is not None,
            "acknowledged_at_ms": (step3["acknowledged_at_ms"] if step3 else None),
            "acknowledged_by": (step3["acknowledged_by"] if step3 else None),
            "record": step3,
            "gate": step3_gate_state(store, client_id),
        },
        "signoff": {
            "state": signoff,
            "valid": bool(signoff["valid"]),
            "signature_reference": settings.get("signature_reference"),
            "signer_name": settings.get("signer_name"),
            "signer_title": settings.get("signer_title"),
            "signed_timestamp": settings.get("signed_timestamp"),
            "signoff_expires_at": settings.get("signoff_expires_at"),
            "step3_attestation_id_on_file": settings.get("step3_attestation_id"),
            "step3_binding_current": bool(
                step3 and settings.get("step3_attestation_id") == step3["attestation_id"]),
            "covers": signature_covers(),
        },
        "operating_mode": settings.get("operating_mode"),
        "attestations": attestations(store, client_id),
        "honest_note": (
            "Nothing on this ladder has been signed by a real client: the sandbox console "
            "operates one fixture client, and its recorded sign-off is a simulated "
            "signature, not an authorization."),
    }
