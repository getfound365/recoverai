"""Onboarding back end (no UI in this task).

Two jobs:

1. Capture a digital sign-off: the authorization document version plus the typed
   signer name and corporate title, hashed into an encrypted-looking signature
   reference (sha256 over client_id + signer_name + signer_title + document version
   + timestamp). The client is left in `hitl` after signing - signing is not a licence
   to fire automatically, it is only the precondition for the owner choosing to.

2. Switch operating mode. `full_auto` is refused unless a valid sign-off exists for
   that client_id, every attempt is appended to the immutable timeline, and revoking
   or expiring a sign-off drops the client back to `hitl`.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence

from . import clock, config
from .audit import AuditLog


# --- sign-off ---------------------------------------------------------------
def compute_signature_reference(client_id: str, signer_name: str, signer_title: str,
                                document_version: str, timestamp: str) -> str:
    payload = "|".join([client_id, signer_name, signer_title, document_version, timestamp])
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
                    scope_pairs: Optional[Sequence[Sequence[str]]] = None) -> Dict[str, Any]:
    name = (signer_name or "").strip()
    title = (signer_title or "").strip()
    if not name or not title:
        raise ValueError("sign-off requires a typed signer name and corporate title")
    scope = normalize_scope(channels, aging_states, scope_pairs)

    ensure_client(store, client_id)
    document_version = config.authorization_document()["version"]
    timestamp = clock.iso(now)
    signature_reference = compute_signature_reference(client_id, name, title, document_version, timestamp)
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
                 updated_at = ?
               WHERE client_id = ?""",
            (name, title, signature_reference, json.dumps(scope["channels"]),
             json.dumps(scope["aging_states"]), json.dumps([f"{c}:{a}" for c, a in scope["pairs"]]),
             timestamp, document_version, expires_at, timestamp, client_id),
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
