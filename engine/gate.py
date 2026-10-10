"""Phase 2 - the beta-to-full-active transition gate, its state machine and Milestone B.

WHAT THIS MODULE PROVES, AND WHAT IT DOES NOT
---------------------------------------------
`billing_route_verifications` is a hash-chained append-only record. It is
TAMPER-EVIDENT, NOT a cryptographic proof of authorship. `chain_hash` proves two things
and only two: that the payload has not changed since it was recorded, and where that
payload sits in the sequence. It does NOT prove who recorded it, and it is not
third-party attestation - anyone with write access to the store could append a row and
recompute the chain. A hash proves content and order; it does not prove who wrote it,
and nothing here should ever be described as though it did.

The gate's job is to stop a client being promoted into a fee we cannot actually collect.
So it fails closed in every direction:

  * the receipt must parse, typecheck and be backed by a read-back we have actually seen
  * `livemode` must be a native boolean True; a native False is a capability deficiency
  * a capability reported False locks that attempt for good, in its own immutable row
  * the account id must byte-equal a pinned platform id we were given; while no id is
    pinned the gate cannot pass and records nothing
  * a receipt validated against the sandbox pin can only ever set
    `billing_route_demonstrated_in_sandbox`, never `live_billing_verified`
  * a timeout is state UNKNOWN: it denies, and changes nothing
  * every milestone is EVIDENCE-DERIVED: the three `*_milestone` columns on
    `client_autonomy_settings` are mirrors kept for reporting, and no decision here reads
    one as truth. `manual_review_threshold_passed` is counted from
    `manual_review_decisions`; `live_billing_verified` needs a chain-verified bound
    receipt; `commercial_msa_executed` needs a recorded executed agreement in the
    append-only `commercial_msa_records` table, which is EMPTY today - so that milestone is
    FALSE for every client and the gate cannot open. The two `full_active_*` store triggers
    recompute the same three things with EXISTS/COUNT, so a one-statement SQL write cannot
    promote anyone either.

There is NO catch-all error handling in this module (no bare `except:` and no
`except Exception:`), there is no `print(` and there are no network calls: the validator
is storage-only. `verify_milestone_b_receipt` takes an OPTIONAL injectable fetch adapter,
and its contract is worth stating plainly because both halves are deliberate:
  * **No adapter configured means no fetch at all.** The call site is guarded by
    `if fetch_adapter is not None` and the default is None, so an ordinary verification
    makes no outbound call of any kind. No passing verdict can be produced by a fetch;
    a fetch can only confirm or refuse.
  * **A supplied adapter's return shape is validated STRICTLY.** It must be a Mapping
    carrying native booleans (`type(value) is bool`, so "true", 1 and bool subclasses
    are all refused) for every key in INDEPENDENT_READ_KEYS - livemode, charges_enabled,
    payouts_enabled. Anything else - a non-mapping, a missing key, a look-alike truthy
    value - is INDEPENDENT_READ_MALFORMED: state MALFORMED, gate locked, NO flag
    written, with the offending field names or the returned type in the refusal row.
    An adapter that raises a timeout is state UNKNOWN: denied, every flag unchanged.
    The rule: an answer we cannot read has told us nothing, and "I could not read the
    platform" must never pass as confirmation.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import clock, config

# --- lifecycle states -------------------------------------------------------
STATUS_BETA_TESTER = "STATUS_BETA_TESTER"
STATUS_FULL_ACTIVE = "STATUS_FULL_ACTIVE"
LIFECYCLE_STATES = (STATUS_BETA_TESTER, STATUS_FULL_ACTIVE)

# --- milestone flags --------------------------------------------------------
MILESTONE_MANUAL_REVIEW = "manual_review_threshold_passed"
MILESTONE_LIVE_BILLING = "live_billing_verified"
MILESTONE_COMMERCIAL_MSA = "commercial_msa_executed"
MILESTONES = (MILESTONE_MANUAL_REVIEW, MILESTONE_LIVE_BILLING, MILESTONE_COMMERCIAL_MSA)

#: The non-milestone flag: an honest statement that a billing route was demonstrated in
#: the sandbox. It is displayed next to the milestone, never in place of it.
FLAG_SANDBOX_DEMONSTRATED = "billing_route_demonstrated_in_sandbox"

#: The milestone flag whose writer is the merchant gate below.
BILLING_FLAGS = (MILESTONE_LIVE_BILLING, FLAG_SANDBOX_DEMONSTRATED)

#: A client needs at least this many manual hitl approvals/rejections, with zero
#: client-caused compliance exceptions, before the review milestone can be TRUE.
MANUAL_REVIEW_THRESHOLD = 20

#: A recorded reason has to be auditable, not merely non-empty. Whitespace-only padding
#: and one-word placeholders ("x", "fixed") are refused: the point of the field is that a
#: later reader can tell WHAT changed. The minimum is on the trimmed length, so padding
#: cannot reach it.
MIN_SUBSTANTIVE_REASON = 20

#: The shape a fetch adapter's answer must have to mean anything. Anything else - None, {},
#: another object, or these keys with non-native-boolean values - is an answer we cannot
#: read, and an unreadable answer is a refusal: it must never pass as confirmation.
INDEPENDENT_READ_KEYS = ("livemode", "charges_enabled", "payouts_enabled")

MILESTONE_PLAIN_ENGLISH = {
    MILESTONE_MANUAL_REVIEW: (
        f"at least {MANUAL_REVIEW_THRESHOLD} manual approvals or rejections recorded in "
        "hitl mode, with zero compliance exceptions caused by the client"
    ),
    MILESTONE_LIVE_BILLING: (
        "our own connected merchant account proven to accept charges and to pay out, "
        "read back from the provider's own record"
    ),
    MILESTONE_COMMERCIAL_MSA: (
        "the pilot agreement upgraded to the commercial Master Services Agreement, executed "
        "and recorded in commercial_msa_records"
    ),
}

# --- the platform pin -------------------------------------------------------
#: No account id is available to us today from any tool, so the pin resolves here and the
#: live gate cannot pass. It is a sentinel, never an invented `acct_...` value.
UNPINNED = "UNPINNED"

#: The explicit SANDBOX pin, used only for the demo and the tests. It is deliberately not
#: a plausible account id: it is a label, and a receipt checked against it can only ever
#: set `billing_route_demonstrated_in_sandbox`.
SANDBOX_PIN = "acct_sandbox_pin_not_our_live_account"

#: The documented genesis constant every verification chain starts from.
GENESIS_CHAIN_HASH = hashlib.sha256(
    b"recoverai:billing_route_verifications:genesis:v1").hexdigest()

# --- the receipt contract ---------------------------------------------------
RECEIPT_KEYS = frozenset(("payment_intent_sample", "livemode", "capabilities",
                          "account_id", "timestamp_utc"))
CAPABILITY_KEYS = frozenset(("charges_enabled", "payouts_enabled"))
EVIDENCE_FILENAME = "verified_stripe_evidence.json"
PLATFORM_CONFIG_FILENAME = "platform.json"
ENV_PLATFORM_ACCOUNT_ID = "RECOVERAI_PLATFORM_ACCOUNT_ID"

#: Decision states. `state` is the human-facing one, `reason_token` the machine one.
DECISION_PASS = "PASS"
DECISION_PASS_SANDBOX = "PASS_SANDBOX_ONLY"
DECISION_MALFORMED = "MALFORMED"
DECISION_CAPABILITY_DISABLED = "CAPABILITY_DISABLED"
DECISION_TIMEOUT_UNKNOWN = "TIMEOUT_UNKNOWN"
DECISION_ACCOUNT_MISMATCH = "PLATFORM_ACCOUNT_MISMATCH"
DECISION_ACCOUNT_NOT_PINNED = "PLATFORM_ACCOUNT_NOT_PINNED"
DECISION_REOPEN_REFUSED = "VERIFICATION_REOPEN_REFUSED"
DECISION_INDEPENDENT_READ_MALFORMED = "INDEPENDENT_READ_MALFORMED"

STATE_SATISFIED = "SATISFIED"
STATE_SANDBOX_DEMONSTRATED = "SANDBOX_DEMONSTRATED"
STATE_MALFORMED = "MALFORMED"
STATE_CAPABILITY_DISABLED = "CAPABILITY_DISABLED"
STATE_UNKNOWN = "UNKNOWN"
STATE_REFUSED = "REFUSED"


# ---------------------------------------------------------------------------
# Exception taxonomy. Every refusal on this path is one of these, and every one
# carries a machine-readable reason_token.
# ---------------------------------------------------------------------------
class GateException(Exception):
    """Base of every refusal on the gate path. Carries a `reason_token`."""

    default_token = "GATE_REFUSED"

    def __init__(self, reason_token: Optional[str] = None, message: str = "", **details: Any):
        self.reason_token = reason_token or self.default_token
        self.details = details
        self.outcome: Optional[Dict[str, Any]] = None
        super().__init__(message or self.reason_token)


class MalformedReceiptException(GateException):
    """The receipt is not the shape or the type the contract requires."""


class CapabilityDisabledException(GateException):
    """A well-formed receipt that reports a capability as explicitly False.

    Also raised for a native `livemode: False`: a correctly-typed False is a capability
    deficiency, not a malformed receipt.
    """


class RecoverAITimeoutException(GateException):
    """The optional fetch adapter did not answer. State UNKNOWN: denies, changes nothing."""


class PlatformCredentialMismatchException(GateException):
    """The receipt names an account that is not the pinned one, or a bound record that
    does not verify. A security refusal, never a warning."""


class GateLockedException(GateException):
    """The absolute code gate: at least one milestone is outstanding."""

    def __init__(self, reason_token: Optional[str] = None, message: str = "",
                 outstanding: Optional[Sequence[str]] = None, **details: Any):
        self.outstanding = list(outstanding or [])
        super().__init__(reason_token, message, outstanding=self.outstanding, **details)


# ---------------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------------
def canonical_json(payload: Any) -> str:
    """Sorted keys, compact separators: one payload, exactly one byte string."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_canonical(payload: Any) -> str:
    return sha256_text(canonical_json(payload))


def _now(now: Optional[datetime.datetime] = None) -> datetime.datetime:
    return now or clock.now_utc()


def substantive_reason(reason: Optional[str]) -> Optional[str]:
    """The recorded reason, or None when it is not good enough to audit.

    A reason is not a formality: it is the only thing that tells a later reader why a
    locked verification was re-opened. Non-blank is not enough - "x" and "   fixed   "
    carry no information, so the check is on the TRIMMED length and whitespace-only
    padding cannot buy its way past it.
    """
    if not isinstance(reason, str):
        return None
    text = reason.strip()
    if len(text) < MIN_SUBSTANTIVE_REASON:
        return None
    return text


# ---------------------------------------------------------------------------
# storage-only reads: the evidence file, the pin
# ---------------------------------------------------------------------------
def evidence_path(path: Optional[Any] = None) -> Path:
    return Path(path) if path else (config.config_dir() / EVIDENCE_FILENAME)


def platform_config_path(path: Optional[Any] = None) -> Path:
    return Path(path) if path else (config.config_dir() / PLATFORM_CONFIG_FILENAME)


def load_verified_evidence(path: Optional[Any] = None) -> Dict[str, Any]:
    """The lead's read-back from our connected account. Read-only, never written here."""
    with open(evidence_path(path), "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise MalformedReceiptException("EVIDENCE_FILE_NOT_AN_OBJECT",
                                        message="config/verified_stripe_evidence.json must "
                                                "hold a JSON object")
    return document


def verified_payment_intent_ids(path: Optional[Any] = None) -> List[str]:
    """Every payment-intent id the evidence file records us having actually read back."""
    document = load_verified_evidence(path)
    entries = document.get("evidence") or []
    if not isinstance(entries, list):
        return []
    found = []
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("kind") == "payment_intent":
            value = entry.get("id")
            if isinstance(value, str) and value:
                found.append(value)
    return sorted(set(found))


def load_platform_config(path: Optional[Any] = None) -> Dict[str, Any]:
    """The local platform pin register. An unreadable file resolves to no pin at all,
    which is the fail-closed direction."""
    target = platform_config_path(path)
    if not target.is_file():
        return {}
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    except OSError:
        return {}
    return document if isinstance(document, dict) else {}


def resolve_platform_account_id(env: Optional[Mapping[str, str]] = None,
                                config_path: Optional[Any] = None) -> str:
    """env RECOVERAI_PLATFORM_ACCOUNT_ID, else config/platform.json, else UNPINNED.

    The value is returned exactly as found: no strip, no case folding, no normalisation.
    A pinned id that differs by one byte from what a receipt carries is a mismatch.
    """
    source = os.environ if env is None else env
    from_env = source.get(ENV_PLATFORM_ACCOUNT_ID)
    if from_env:
        return str(from_env)
    from_config = load_platform_config(config_path).get("platform_account_id")
    if isinstance(from_config, str) and from_config:
        return from_config
    return UNPINNED


def platform_register_facts(config_path: Optional[Any] = None) -> Dict[str, Any]:
    """What the register itself records ABOUT the account it pins, as structured facts.

    Read defensively and never invented: a register that carries only an id - or a
    hand-written test register in a temp directory - yields an empty mapping here, and
    every caller degrades to the bare id. That is the point. A label built from this can
    only ever repeat what the register says, so it cannot turn a pin into a payment.
    """
    document = load_platform_config(config_path)
    facts: Dict[str, Any] = {}
    provenance = document.get("_provenance")
    if isinstance(provenance, Mapping):
        observed = provenance.get("observed")
        if isinstance(observed, Mapping):
            name = observed.get("business_profile_name")
            if isinstance(name, str) and name.strip():
                facts["business_profile_name"] = name.strip()
        verified_by = provenance.get("verified_by")
        if isinstance(verified_by, str) and verified_by.strip():
            facts["verified_by"] = verified_by.strip()
    activity = document.get("_account_activity_when_read")
    if isinstance(activity, Mapping):
        summary = activity.get("summary")
        if isinstance(summary, str) and summary.strip():
            facts["activity_summary"] = summary.strip()
    return facts


# ---------------------------------------------------------------------------
# The receipt contract: shape, strict types, and the evidence check
# ---------------------------------------------------------------------------
def _raise_malformed(token: str, message: str, **details: Any):
    raise MalformedReceiptException(token, message, **details)


def _require_native_boolean(value: Any, field: str) -> bool:
    """`type(value) is bool` - strictly stronger than `isinstance(value, bool)`.

    The owner's spec text read `isinstance(val, bool)`; the literal `is isinstance(...)`
    is a syntax error, and isinstance is the weaker check anyway: it accepts bool
    subclasses and is the reason people reach for string matching on "true"/"false".
    `type(value) is bool` accepts exactly the two native booleans and nothing else, so
    the string "true", the string "True", the ints 1/0, 1.0, None, [] and {} are all
    rejected here rather than being coerced. No string matching of "true"/"false" is
    permitted anywhere on this path.
    """
    if type(value) is not bool:
        _raise_malformed("RECEIPT_TYPE_NOT_BOOLEAN",
                         f"{field} must be a native JSON boolean",
                         field=field, received_type=type(value).__name__)
    return value


def parse_receipt(receipt: Any, evidence_path_override: Optional[Any] = None) -> Dict[str, Any]:
    """Validate the receipt contract. Raises MalformedReceiptException on any deviation.

    Unknown keys are rejected, a missing key is never defaulted, and every boolean is
    checked with `type(value) is bool`.
    """
    if not isinstance(receipt, Mapping):
        _raise_malformed("RECEIPT_NOT_AN_OBJECT",
                         "the receipt must be a JSON object",
                         received_type=type(receipt).__name__)

    keys = set(receipt.keys())
    missing = sorted(str(k) for k in (RECEIPT_KEYS - keys))
    if missing:
        _raise_malformed("RECEIPT_KEY_MISSING",
                         f"the receipt is missing {missing}; a missing key is never "
                         "defaulted", missing=missing)
    unknown = sorted(str(k) for k in (keys - RECEIPT_KEYS))
    if unknown:
        # This is also the test-mode fence: a receipt carrying "test_mode": true, or the
        # wrong-cased "True", is rejected here before anything reads it.
        _raise_malformed("RECEIPT_KEY_UNKNOWN",
                         f"the receipt carries keys the contract does not define: {unknown}",
                         unknown=unknown)

    livemode = _require_native_boolean(receipt["livemode"], "livemode")

    capabilities = receipt["capabilities"]
    if not isinstance(capabilities, Mapping):
        _raise_malformed("RECEIPT_CAPABILITIES_NOT_AN_OBJECT",
                         "capabilities must be a JSON object",
                         received_type=type(capabilities).__name__)
    capability_keys = set(capabilities.keys())
    caps_missing = sorted(str(k) for k in (CAPABILITY_KEYS - capability_keys))
    if caps_missing:
        _raise_malformed("RECEIPT_CAPABILITY_KEY_MISSING",
                         f"capabilities is missing {caps_missing}", missing=caps_missing)
    caps_unknown = sorted(str(k) for k in (capability_keys - CAPABILITY_KEYS))
    if caps_unknown:
        _raise_malformed("RECEIPT_CAPABILITY_KEY_UNKNOWN",
                         f"capabilities carries keys the contract does not define: "
                         f"{caps_unknown}", unknown=caps_unknown)

    charges_enabled = _require_native_boolean(capabilities["charges_enabled"],
                                             "capabilities.charges_enabled")
    payouts_enabled = _require_native_boolean(capabilities["payouts_enabled"],
                                             "capabilities.payouts_enabled")

    account_id = receipt["account_id"]
    if not isinstance(account_id, str) or not account_id:
        _raise_malformed("RECEIPT_ACCOUNT_ID_NOT_A_STRING",
                         "account_id must be a non-empty string",
                         received_type=type(account_id).__name__)
    if not account_id.startswith("acct_"):
        _raise_malformed("RECEIPT_ACCOUNT_ID_NOT_AN_ACCOUNT_ID",
                         "account_id must be an acct_... identifier", received=account_id)

    payment_intent_sample = receipt["payment_intent_sample"]
    if not isinstance(payment_intent_sample, str) or not payment_intent_sample:
        _raise_malformed("RECEIPT_PAYMENT_INTENT_NOT_A_STRING",
                         "payment_intent_sample must be a non-empty string",
                         received_type=type(payment_intent_sample).__name__)
    known = verified_payment_intent_ids(evidence_path_override)
    if payment_intent_sample not in known:
        # The evidence file is the only thing that distinguishes an id we have actually
        # read back from one that was invented. Not present there means not evidence.
        _raise_malformed("RECEIPT_UNVERIFIED_PAYMENT_INTENT",
                         "payment_intent_sample does not appear in "
                         f"config/{EVIDENCE_FILENAME}: it is not an id we have read back, "
                         "so it cannot ground a verification", known_ids=len(known))

    timestamp = _parse_utc_timestamp(receipt["timestamp_utc"])

    return {
        "receipt": dict(receipt),
        "livemode": livemode,
        "charges_enabled": charges_enabled,
        "payouts_enabled": payouts_enabled,
        "account_id": account_id,
        "payment_intent_sample": payment_intent_sample,
        "timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
        "receipt_sha256": sha256_canonical(dict(receipt)),
    }


def _parse_utc_timestamp(value: Any) -> datetime.datetime:
    """Parse it, do not pattern-match it: a real tz-aware UTC instant, or a refusal."""
    if not isinstance(value, str) or not value:
        _raise_malformed("RECEIPT_TIMESTAMP_NOT_A_STRING",
                         "timestamp_utc must be a non-empty ISO-8601 string",
                         received_type=type(value).__name__)
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        _raise_malformed("RECEIPT_TIMESTAMP_UNPARSEABLE",
                         f"timestamp_utc is not parseable as ISO-8601: {exc}",
                         received=value)
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        _raise_malformed("RECEIPT_TIMESTAMP_NOT_UTC",
                         "timestamp_utc must be timezone-aware and exactly UTC "
                         "(a naive or offset timestamp is not evidence of an instant)",
                         received=value)
    return parsed


def check_capabilities(parsed: Mapping[str, Any]) -> None:
    """Raises CapabilityDisabledException for an explicitly-typed False.

    A native `False` is not malformed - it is a deficiency, and a deficiency is handled by
    the decision table, not by the parser.
    """
    if parsed["livemode"] is not True:
        raise CapabilityDisabledException(
            "NOT_LIVEMODE",
            "the receipt reports livemode = false: this is not a live reading, so it "
            "cannot prove a live billing route",
            field="livemode")
    disabled = [name for name in ("charges_enabled", "payouts_enabled")
                if parsed[name] is not True]
    if disabled:
        raise CapabilityDisabledException(
            "CAPABILITY_DISABLED_" + "_".join(sorted(disabled)).upper(),
            f"the receipt reports {disabled} as false: a 20% fee cannot be collected "
            "through a route that will not charge or will not pay out",
            disabled=disabled)


# ---------------------------------------------------------------------------
# The one place the transition log is written
# ---------------------------------------------------------------------------
def insert_transition(store, *, client_id: str, event: str, field: str,
                      old_value: Optional[str], new_value: Optional[str],
                      validating_parameters: Optional[Any], executing_administrator: str,
                      reason: str, now: Optional[datetime.datetime] = None) -> int:
    """Append one immutable row. The caller owns the transaction."""
    params = validating_parameters
    if params is not None and not isinstance(params, str):
        params = canonical_json(params)
    return store.insert(
        """INSERT INTO lifecycle_transition_log
             (recorded_at, event, client_id, field, old_value, new_value,
              validating_parameters, executing_administrator, reason)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (clock.iso_ms(_now(now)), event, client_id, field, old_value, new_value,
         params, executing_administrator, reason),
    )


def transition_log(store, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if client_id:
        return store.query(
            "SELECT * FROM lifecycle_transition_log WHERE client_id = ? ORDER BY transition_id",
            (client_id,))
    return store.query("SELECT * FROM lifecycle_transition_log ORDER BY transition_id")


# ---------------------------------------------------------------------------
# Client row + flag helpers
# ---------------------------------------------------------------------------
def _client_row(store, client_id: str) -> Dict[str, Any]:
    from . import onboarding  # local import: keeps the module import graph one-way
    return onboarding.ensure_client(store, client_id)


def flag_value(store, client_id: str, flag: str) -> int:
    if flag not in MILESTONES and flag not in BILLING_FLAGS:
        raise GateException("UNKNOWN_MILESTONE_FLAG", f"unknown flag {flag!r}", flag=flag)
    row = _client_row(store, client_id)
    return int(row.get(flag) or 0)


def lifecycle_status(store, client_id: str) -> str:
    return str(_client_row(store, client_id).get("lifecycle_status") or STATUS_BETA_TESTER)


def _flag_snapshot(store, client_id: str) -> Dict[str, int]:
    row = _client_row(store, client_id)
    snapshot = {name: int(row.get(name) or 0) for name in MILESTONES}
    snapshot[FLAG_SANDBOX_DEMONSTRATED] = int(row.get(FLAG_SANDBOX_DEMONSTRATED) or 0)
    snapshot["lifecycle_status"] = str(row.get("lifecycle_status") or STATUS_BETA_TESTER)
    return snapshot


def set_milestone_flag(store, client_id: str, flag: str, value: bool, who: str, reason: str,
                       validating_parameters: Optional[Any] = None,
                       now: Optional[datetime.datetime] = None,
                       event: str = "milestone_flag") -> Dict[str, Any]:
    """The ONLY writer of a milestone flag. One row in the transition log per change.

    A console route or an HTTP handler must call this, never UPDATE the column: the
    source-level test in tests/test_gate.py fails if any other module writes one.
    """
    if flag not in MILESTONES and flag not in BILLING_FLAGS:
        raise GateException("UNKNOWN_MILESTONE_FLAG", f"unknown flag {flag!r}", flag=flag)
    if flag == MILESTONE_COMMERCIAL_MSA:
        return commercial_msa_refusal(store, client_id, who, reason, now=now)
    if not reason:
        raise GateException("TRANSITION_REASON_REQUIRED",
                           "a lifecycle transition must carry a reason")
    _client_row(store, client_id)
    before = flag_value(store, client_id, flag)
    target = 1 if value else 0
    with store.tx(immediate=True):
        if before != target:
            store.execute(f"UPDATE client_autonomy_settings SET {flag} = ? WHERE client_id = ?",
                          (target, client_id))
        insert_transition(store, client_id=client_id, event=event, field=flag,
                          old_value=str(before), new_value=str(target),
                          validating_parameters=validating_parameters or {},
                          executing_administrator=who, reason=reason, now=now)
    return {"client_id": client_id, "field": flag, "old_value": before, "new_value": target,
            "changed": before != target, "executing_administrator": who, "reason": reason,
            "recorded_at": clock.iso_ms(_now(now))}


def commercial_msa_milestone_status(store=None, client_id: Optional[str] = None) -> Dict[str, Any]:
    """The commercial milestone, derived from its EVIDENCE - never from the column.

    Truth for this milestone is a row in the append-only `commercial_msa_records` table:
    an executed commercial Master Services Agreement for that client, with a named
    counterparty and signatory, an agreement reference, a document version, the execution
    instant and the sha256 of the document. `client_autonomy_settings.commercial_msa_executed`
    is a mirror written by `record_commercial_msa` for reporting; no decision reads it.

    That table is EMPTY in the shipped state - nobody has signed anything - so this
    milestone is FALSE for every client and the gate cannot open. Stated as what it is:
    the milestone needs evidence that does not exist yet, and no flag write, no UPDATE, no
    INSERT and no caller can manufacture it.
    """
    if store is None or client_id is None:
        return {
            "implemented": True,
            "state": "EVIDENCE_REQUIRED",
            "satisfied": False,
            "client_id": client_id,
            "record": None,
            "plain_english": MILESTONE_PLAIN_ENGLISH[MILESTONE_COMMERCIAL_MSA],
            "detail": ("requires an executed commercial Master Services Agreement recorded in "
                       "commercial_msa_records; no client has one, so this milestone is FALSE "
                       "for every client and the column cannot satisfy it"),
        }
    records = commercial_msa_records(store, client_id)
    latest = records[-1] if records else None
    satisfied = bool(records)
    if satisfied:
        detail = (f"recorded agreement {latest['agreement_reference']} "
                  f"(document {latest['document_version']}, sha256 "
                  f"{str(latest['document_sha256'])[:12]}...), executed "
                  f"{latest['executed_at']}, signed by {latest['signatory_name']} "
                  f"({latest['signatory_title']}) for {latest['counterparty_legal_name']}, "
                  f"recorded by {latest['recorded_by']} at {latest['recorded_at']}")
    else:
        detail = ("no executed commercial Master Services Agreement is recorded in "
                  "commercial_msa_records for this client, so this milestone is FALSE; "
                  "the commercial_msa_executed column is a mirror and cannot satisfy it")
    return {
        "implemented": True,
        "state": "RECORDED" if satisfied else "EVIDENCE_REQUIRED",
        "satisfied": satisfied,
        "client_id": client_id,
        "record_count": len(records),
        "record": latest,
        "plain_english": MILESTONE_PLAIN_ENGLISH[MILESTONE_COMMERCIAL_MSA],
        "detail": detail,
    }


def commercial_msa_records(store, client_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every recorded executed agreement, oldest first. Append-only: there is no edit."""
    if client_id:
        return store.query(
            "SELECT * FROM commercial_msa_records WHERE client_id = ? ORDER BY msa_id",
            (client_id,))
    return store.query("SELECT * FROM commercial_msa_records ORDER BY msa_id")


def verified_commercial_msa(store, client_id: str) -> bool:
    """The milestone's real value: is an executed agreement recorded for this client?

    Reads the evidence table, never `client_autonomy_settings.commercial_msa_executed`.
    """
    row = store.query_one(
        "SELECT COUNT(*) AS n FROM commercial_msa_records WHERE client_id = ?", (client_id,))
    return bool(int((row or {}).get("n") or 0))


def insert_commercial_msa_record(store, *, client_id: str, counterparty_legal_name: str,
                                 signatory_name: str, signatory_title: str,
                                 agreement_reference: str, document_version: str,
                                 executed_at: str, document_sha256: str,
                                 recorded_by: str, note: Optional[str] = None,
                                 now: Optional[datetime.datetime] = None) -> int:
    """Append one executed-agreement row. The caller owns the transaction.

    The fields are checked here as well as by the table's CHECK constraints: an executed
    agreement carries a named counterparty and signatory, a reference, a version, an
    execution instant that parses as a real UTC instant, and the sha256 of the document.
    """
    required = {
        "counterparty_legal_name": counterparty_legal_name,
        "signatory_name": signatory_name,
        "signatory_title": signatory_title,
        "agreement_reference": agreement_reference,
        "document_version": document_version,
        "recorded_by": recorded_by,
    }
    for field, value in required.items():
        if not isinstance(value, str) or not value.strip():
            raise GateException("MSA_RECORD_INCOMPLETE",
                                f"an executed agreement needs a non-blank {field}: a blank "
                                "field is not evidence that anyone signed anything",
                                field=field)
    _parse_utc_timestamp(executed_at)
    digest = str(document_sha256 or "")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise GateException("MSA_DOCUMENT_HASH_INVALID",
                            "document_sha256 must be the 64-character lowercase hex sha256 "
                            "of the executed agreement document",
                            received_length=len(digest))
    return store.insert(
        """INSERT INTO commercial_msa_records
             (client_id, counterparty_legal_name, signatory_name, signatory_title,
              agreement_reference, document_version, executed_at, document_sha256,
              recorded_at, recorded_by, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (client_id, counterparty_legal_name.strip(), signatory_name.strip(),
         signatory_title.strip(), agreement_reference.strip(), document_version.strip(),
         executed_at, digest, clock.iso_ms(_now(now)), recorded_by, note),
    )


def record_commercial_msa(store, client_id: str, *, who: str, reason: str,
                          counterparty_legal_name: str, signatory_name: str,
                          signatory_title: str, agreement_reference: str,
                          document_version: str, executed_at: str, document_sha256: str,
                          note: Optional[str] = None,
                          now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """The ONLY writer of the commercial milestone, and it writes evidence.

    One transaction: the executed agreement is appended to `commercial_msa_records`, the
    mirror column is set, and the immutable transition log records both the evidence and
    the reason. There is no path that sets the column without a recorded agreement, which
    is why the milestone cannot be faked by SQL.
    """
    if not substantive_reason(reason):
        raise GateException(
            "TRANSITION_REASON_REQUIRED",
            f"recording an executed Master Services Agreement needs a reason of at least "
            f"{MIN_SUBSTANTIVE_REASON} non-whitespace characters saying what was signed "
            f"and when; {reason!r} is not good enough to audit")
    _client_row(store, client_id)
    before = flag_value(store, client_id, MILESTONE_COMMERCIAL_MSA)
    with store.tx(immediate=True):
        msa_id = insert_commercial_msa_record(
            store, client_id=client_id, counterparty_legal_name=counterparty_legal_name,
            signatory_name=signatory_name, signatory_title=signatory_title,
            agreement_reference=agreement_reference, document_version=document_version,
            executed_at=executed_at, document_sha256=document_sha256, recorded_by=who,
            note=note, now=now)
        store.execute(
            "UPDATE client_autonomy_settings SET commercial_msa_executed = 1 "
            "WHERE client_id = ?", (client_id,))
        row = store.query_one("SELECT * FROM commercial_msa_records WHERE msa_id = ?", (msa_id,))
        insert_transition(
            store, client_id=client_id, event="milestone_flag",
            field=MILESTONE_COMMERCIAL_MSA, old_value=str(before), new_value="1",
            validating_parameters={"msa_id": msa_id,
                                   "agreement_reference": row["agreement_reference"],
                                   "document_version": row["document_version"],
                                   "document_sha256": row["document_sha256"],
                                   "executed_at": row["executed_at"],
                                   "signatory_name": row["signatory_name"],
                                   "signatory_title": row["signatory_title"],
                                   "counterparty_legal_name": row["counterparty_legal_name"],
                                   "recorded_by": who},
            executing_administrator=who, reason=reason, now=now)
    return {"client_id": client_id, "field": MILESTONE_COMMERCIAL_MSA, "msa_id": msa_id,
            "old_value": before, "new_value": 1, "changed": before != 1,
            "executing_administrator": who, "reason": reason,
            "agreement_reference": agreement_reference, "document_version": document_version,
            "document_sha256": document_sha256, "executed_at": executed_at,
            "recorded_at": clock.iso_ms(_now(now))}


def commercial_msa_refusal(store, client_id: str, who: str, reason: str,
                           now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """A bare flag write for the commercial milestone is refused: the flag is a mirror.

    The milestone is satisfied by a recorded, executed agreement - `record_commercial_msa`
    - and by nothing else. Whatever this flags-only API is asked to do, the answer is that
    the evidence is missing and the flag is not the milestone.
    """
    with store.tx(immediate=True):
        insert_transition(store, client_id=client_id, event="gate_event",
                          field=MILESTONE_COMMERCIAL_MSA, old_value="0", new_value="0",
                          validating_parameters=commercial_msa_milestone_status(store, client_id),
                          executing_administrator=who,
                          reason=(reason or "attempted to set the commercial MSA flag by hand"),
                          now=now)
    return {"client_id": client_id, "field": MILESTONE_COMMERCIAL_MSA, "changed": False,
            "refused": True, "reason_token": "MSA_EVIDENCE_REQUIRED",
            "reason": (f"the commercial milestone is satisfied by a recorded executed agreement "
                       f"(record_commercial_msa), not by its mirror column; "
                       f"commercial_msa_records holds no agreement for {client_id}")}


# ---------------------------------------------------------------------------
# manual review milestone: 20 reviews, zero client-caused exceptions
# ---------------------------------------------------------------------------
def insert_manual_review_decision(store, *, client_id: str, debtor_id: str, decision: str,
                                  decided_by: str, operating_mode: str,
                                  client_caused_exception: int = 0, reason: str = "",
                                  now: Optional[datetime.datetime] = None) -> int:
    """Append one review row. The caller owns the transaction.

    A `guardrail_block` may never be recorded as a client-caused exception: the store's
    CHECK constraint refuses it, so a guardrail correctly blocking a send cannot be
    turned into an infraction against the client even by a mistake in this code.
    """
    if client_caused_exception and decision == "guardrail_block":
        raise GateException(
            "GUARDRAIL_BLOCK_IS_NOT_AN_INFRACTION",
            "a guardrail correctly blocking a send is the product working, not a "
            "client-caused compliance exception")
    return store.insert(
        """INSERT INTO manual_review_decisions
             (client_id, debtor_id, decision, decided_by, decided_at,
              operating_mode_at_decision, client_caused_exception, reason)
           VALUES (?,?,?,?,?,?,?,?)""",
        (client_id, debtor_id, decision, decided_by, clock.iso_ms(_now(now)),
         operating_mode, int(client_caused_exception), reason),
    )


def record_manual_review_decision(store, client_id: str, debtor_id: str, decision: str,
                                  decided_by: str, operating_mode: str,
                                  reason: str = "", client_caused_exception: int = 0,
                                  manage_tx: bool = True,
                                  now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    if manage_tx:
        with store.tx(immediate=True):
            decision_id = insert_manual_review_decision(
                store, client_id=client_id, debtor_id=debtor_id, decision=decision,
                decided_by=decided_by, operating_mode=operating_mode,
                client_caused_exception=client_caused_exception, reason=reason, now=now)
    else:
        decision_id = insert_manual_review_decision(
            store, client_id=client_id, debtor_id=debtor_id, decision=decision,
            decided_by=decided_by, operating_mode=operating_mode,
            client_caused_exception=client_caused_exception, reason=reason, now=now)
    return {"decision_id": decision_id, "client_id": client_id, "debtor_id": debtor_id,
            "decision": decision, "decided_by": decided_by,
            "operating_mode_at_decision": operating_mode,
            "client_caused_exception": int(client_caused_exception)}


def record_guardrail_block(store, client_id: str, debtor_id: str, who: str,
                           operating_mode: str, reason: str,
                           manage_tx: bool = True,
                           now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """A guardrail refused a contact. Recorded with an exception count of zero, always."""
    return record_manual_review_decision(store, client_id, debtor_id, "guardrail_block",
                                         who, operating_mode, reason=reason,
                                         client_caused_exception=0, manage_tx=manage_tx,
                                         now=now)


def manual_review_counts(store, client_id: str) -> Dict[str, Any]:
    reviews = store.query_one(
        """SELECT COUNT(*) AS n FROM manual_review_decisions
            WHERE client_id = ? AND decision IN ('approved', 'rejected')
              AND operating_mode_at_decision = 'hitl'""", (client_id,))
    exceptions = store.query_one(
        """SELECT COALESCE(SUM(client_caused_exception), 0) AS n FROM manual_review_decisions
            WHERE client_id = ?""", (client_id,))
    blocks = store.query_one(
        """SELECT COUNT(*) AS n FROM manual_review_decisions
            WHERE client_id = ? AND decision = 'guardrail_block'""", (client_id,))
    review_count = int((reviews or {}).get("n") or 0)
    exception_count = int((exceptions or {}).get("n") or 0)
    block_count = int((blocks or {}).get("n") or 0)
    return {
        "client_id": client_id,
        "manual_hitl_reviews": review_count,
        "required": MANUAL_REVIEW_THRESHOLD,
        "client_caused_exceptions": exception_count,
        "guardrail_blocks": block_count,
        "met": review_count >= MANUAL_REVIEW_THRESHOLD and exception_count == 0,
    }


def refresh_manual_review_milestone(store, client_id: str, who: str, reason: str,
                                    now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Recompute the review milestone from real rows and record the result if it moved."""
    counts = manual_review_counts(store, client_id)
    changed = set_milestone_flag(
        store, client_id, MILESTONE_MANUAL_REVIEW, counts["met"], who, reason,
        validating_parameters=counts, now=now)
    changed["counts"] = counts
    return changed


# ---------------------------------------------------------------------------
# Milestone B: the receipt validator
# ---------------------------------------------------------------------------
def _outcome(decision: str, state: str, reason_token: str, *, client_id: str,
             gate_locked: bool, recorded: bool, before: Mapping[str, Any],
             after: Mapping[str, Any], live_billing_verified: bool,
             sandbox_only: bool, plain_english: str, **extra: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "gate": "milestone_b_live_billing_verification",
        "client_id": client_id,
        "decision": decision,
        "state": state,
        "reason_token": reason_token,
        "gate_locked": gate_locked,
        "recorded": recorded,
        "milestone_satisfied": live_billing_verified,
        "live_billing_verified": live_billing_verified,
        "sandbox_only": sandbox_only,
        "flags_before": dict(before),
        "flags_after": dict(after),
        "plain_english": plain_english,
    }
    record.update(extra)
    return record


def _last_chain_hash(store) -> Optional[str]:
    row = store.query_one(
        "SELECT chain_hash FROM billing_route_verifications ORDER BY seq DESC LIMIT 1")
    return row["chain_hash"] if row else None


def append_billing_route_verification(store, *, client_id: str, parsed: Mapping[str, Any],
                                      verification_source: str, recorded_by: str,
                                      decision: str, sandbox_only: bool,
                                      reopen_reason: Optional[str] = None,
                                      note: Optional[str] = None,
                                      now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Append one immutable row to the chain of record. The caller owns the transaction."""
    verification_id = "bv-" + uuid.uuid4().hex
    receipt_json = canonical_json(parsed["receipt"])
    receipt_sha256 = sha256_canonical(parsed["receipt"])
    prev_chain_hash = _last_chain_hash(store) or GENESIS_CHAIN_HASH
    chain_hash = sha256_text("|".join([prev_chain_hash, receipt_sha256, verification_id]))
    seq = store.insert(
        """INSERT INTO billing_route_verifications
             (verification_id, client_id, account_id, livemode, charges_enabled,
              payouts_enabled, verified_at, verification_source, receipt_json,
              receipt_sha256, prev_chain_hash, chain_hash, recorded_at, recorded_by,
              decision, sandbox_only, reopen_reason, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (verification_id, client_id, parsed["account_id"],
         1 if parsed["livemode"] else 0,
         1 if parsed["charges_enabled"] else 0,
         1 if parsed["payouts_enabled"] else 0,
         parsed["timestamp_utc"], verification_source, receipt_json, receipt_sha256,
         prev_chain_hash, chain_hash, clock.iso_ms(_now(now)), recorded_by, decision,
         1 if sandbox_only else 0, reopen_reason, note),
    )
    return {"seq": seq, "verification_id": verification_id, "receipt_sha256": receipt_sha256,
            "prev_chain_hash": prev_chain_hash, "chain_hash": chain_hash}


def verify_chain(store) -> Dict[str, Any]:
    """Walk the chain from the genesis constant and recompute every link.

    A mismatch is a security exception, never a warning.
    """
    rows = store.query("SELECT * FROM billing_route_verifications ORDER BY seq")
    previous = GENESIS_CHAIN_HASH
    for row in rows:
        recomputed_sha = sha256_text(row["receipt_json"])
        if recomputed_sha != row["receipt_sha256"]:
            raise PlatformCredentialMismatchException(
                "RECEIPT_PAYLOAD_TAMPERED",
                f"billing_route_verifications row {row['seq']} does not hash to its recorded "
                "receipt_sha256: the payload changed after it was recorded",
                seq=row["seq"], verification_id=row["verification_id"])
        if row["prev_chain_hash"] != previous:
            raise PlatformCredentialMismatchException(
                "CHAIN_LINK_BROKEN",
                f"billing_route_verifications row {row['seq']} does not follow the row "
                "before it", seq=row["seq"],
                verification_id=row["verification_id"])
        expected = sha256_text("|".join([previous, row["receipt_sha256"],
                                         row["verification_id"]]))
        if expected != row["chain_hash"]:
            raise PlatformCredentialMismatchException(
                "CHAIN_HASH_MISMATCH",
                f"billing_route_verifications row {row['seq']} does not recompute to its "
                "recorded chain_hash", seq=row["seq"],
                verification_id=row["verification_id"])
        previous = row["chain_hash"]
    return {"rows": len(rows), "chain_verified": True, "head": previous}


def read_bound_receipt(store, client_id: str) -> Dict[str, Any]:
    """Read the bound metadata entry and recompute its hash, then its chain link.

    The binding is checked on every read, not trusted once at write time: a bound entry
    that no longer recomputes is a security refusal.
    """
    row = store.query_one(
        "SELECT live_billing_receipt_metadata FROM client_autonomy_settings WHERE client_id = ?",
        (client_id,))
    if not row or row["live_billing_receipt_metadata"] is None:
        return {"bound": False, "client_id": client_id}
    try:
        document = json.loads(row["live_billing_receipt_metadata"])
    except ValueError as exc:
        raise PlatformCredentialMismatchException(
            "BOUND_RECEIPT_UNPARSEABLE",
            f"the bound receipt metadata for {client_id} is not parseable JSON: {exc}")
    if not isinstance(document, Mapping) or "receipt" not in document:
        raise PlatformCredentialMismatchException(
            "BOUND_RECEIPT_MALFORMED",
            f"the bound receipt metadata for {client_id} does not carry a receipt")
    recomputed = sha256_canonical(document["receipt"])
    if recomputed != document.get("receipt_sha256"):
        raise PlatformCredentialMismatchException(
            "BOUND_RECEIPT_HASH_MISMATCH",
            f"the bound receipt for {client_id} no longer hashes to its recorded sha256: "
            "the bound payload has been altered")
    verification_id = document.get("verification_id")
    chain_row = store.query_one(
        "SELECT * FROM billing_route_verifications WHERE verification_id = ?", (verification_id,))
    if not chain_row:
        raise PlatformCredentialMismatchException(
            "BOUND_RECEIPT_NOT_IN_CHAIN",
            f"the bound receipt for {client_id} names verification {verification_id!r}, "
            "which is not in the chain of record")
    if chain_row["receipt_sha256"] != recomputed:
        raise PlatformCredentialMismatchException(
            "BOUND_RECEIPT_DIVERGES_FROM_CHAIN",
            f"the bound receipt for {client_id} does not match the chain row it names")
    if sha256_text(chain_row["receipt_json"]) != chain_row["receipt_sha256"]:
        # The stored payload no longer hashes to the value it was recorded with: the row
        # was altered after it was written, by something that could reach the store.
        raise PlatformCredentialMismatchException(
            "RECEIPT_PAYLOAD_TAMPERED",
            f"chain row {chain_row['seq']} no longer hashes to its recorded receipt_sha256: "
            "the recorded payload was altered after it was written",
            seq=chain_row["seq"], verification_id=verification_id)
    if chain_row["chain_hash"] != document.get("chain_hash"):
        raise PlatformCredentialMismatchException(
            "BOUND_RECEIPT_CHAIN_HASH_MISMATCH",
            f"the bound receipt for {client_id} does not recompute to the chain hash it "
            "carries")
    expected = sha256_text("|".join([chain_row["prev_chain_hash"], chain_row["receipt_sha256"],
                                     chain_row["verification_id"]]))
    if expected != chain_row["chain_hash"]:
        raise PlatformCredentialMismatchException(
            "CHAIN_HASH_MISMATCH",
            f"chain row {chain_row['seq']} does not recompute to its recorded chain_hash")
    return {
        "bound": True,
        "client_id": client_id,
        "receipt": document["receipt"],
        "receipt_sha256": recomputed,
        "verification_id": verification_id,
        "chain_hash": chain_row["chain_hash"],
        "account_id": chain_row["account_id"],
        "verification_source": chain_row["verification_source"],
        "decision": chain_row["decision"],
        "sandbox_only": int(chain_row["sandbox_only"]),
        "recorded_at": chain_row["recorded_at"],
        "recorded_by": chain_row["recorded_by"],
    }


def verified_live_billing(store, client_id: str, env: Optional[Mapping[str, str]] = None,
                          config_path: Optional[Any] = None) -> bool:
    """Is `live_billing_verified` TRUE *and* backed by a verified binding?

    This is what the launch condition reads. It is not the column: a hand-written
    `UPDATE ... SET live_billing_verified = 1`, a hardcoded literal or an in-memory dict
    cannot satisfy it, because the flag alone is never enough - the client must carry a
    bound entry that recomputes, sits in the chain as a live PASS, was produced by a
    named verification source, and whose account id byte-equals the really pinned id.
    """
    if flag_value(store, client_id, MILESTONE_LIVE_BILLING) != 1:
        return False
    pinned = resolve_platform_account_id(env=env, config_path=config_path)
    if pinned == UNPINNED or pinned == SANDBOX_PIN:
        return False
    bound = read_bound_receipt(store, client_id)
    if not bound.get("bound"):
        return False
    if bound.get("sandbox_only") or bound.get("decision") != DECISION_PASS:
        return False
    if bound.get("account_id") != pinned:
        return False
    return bool(bound.get("verification_source"))


def _record_refusal(store, decision: str, state: str, reason_token: str, client_id: str,
                    before: Mapping[str, Any], who: str, plain_english: str,
                    validating_parameters: Optional[Any] = None, recorded: bool = True,
                    **extra: Any) -> Dict[str, Any]:
    """A refusal: no flag is written, and (unless told otherwise) the immutable
    transition log carries the structured reason."""
    moment = extra.pop("now", None)
    if recorded:
        with store.tx(immediate=True):
            insert_transition(store, client_id=client_id, event="gate_event",
                              field=decision, old_value=None, new_value=None,
                              validating_parameters=validating_parameters,
                              executing_administrator=who,
                              reason=f"{reason_token}: {plain_english}",
                              now=moment)
    after = _flag_snapshot(store, client_id)
    return _outcome(decision, state, reason_token, client_id=client_id, gate_locked=True,
                    recorded=recorded, before=before, after=after,
                    live_billing_verified=False, sandbox_only=False,
                    plain_english=plain_english, **extra)


def _malformed_description(receipt: Any) -> Dict[str, Any]:
    if isinstance(receipt, Mapping):
        return {"received_keys": sorted(str(k) for k in receipt.keys())}
    return {"received_type": type(receipt).__name__}


def verify_milestone_b_receipt(store, receipt: Any, *, client_id: str, recorded_by: str,
                               verification_source: str,
                               pin: Optional[str] = None,
                               reopen_reason: Optional[str] = None,
                               fetch_adapter: Optional[Callable[..., Any]] = None,
                               evidence_path_override: Optional[Any] = None,
                               platform_config_file: Optional[Any] = None,
                               env: Optional[Mapping[str, str]] = None,
                               now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Validate a Milestone B receipt and record the outcome. Never raises for the
    documented outcomes - each one is a returned state. Use `outcome_exception()` (or
    `verify_milestone_b_receipt_strict`) to get the typed exception for a recorded state.

    The outcomes, and what each one does:

      PASS                     chain row appended; live_billing_verified set and the
                               receipt bound once on the client row
      PASS_SANDBOX_ONLY        chain row appended; only
                               billing_route_demonstrated_in_sandbox is set
      MALFORMED                state MALFORMED, gate locked, NO flag written
      CAPABILITY_DISABLED      live_billing_verified stays FALSE and the attempt is
                               permanently locked in its own immutable row
      TIMEOUT_UNKNOWN          state UNKNOWN, every flag byte-identical, transition denied
      INDEPENDENT_READ_MALFORMED
                               a configured fetch adapter returned something that is not a
                               mapping of native booleans for
                               (livemode, charges_enabled, payouts_enabled): state MALFORMED,
                               gate locked, NO flag written. An adapter we cannot read is a
                               refusal - it is never treated as confirmation
      PLATFORM_ACCOUNT_MISMATCH / PLATFORM_ACCOUNT_NOT_PINNED
                               refused; a mismatch raises a high-priority Agent 4 alert and
                               never echoes the received account id; while unpinned nothing
                               at all is recorded
    """
    before = _flag_snapshot(store, client_id)
    resolved_pin = pin if pin is not None else resolve_platform_account_id(
        env=env, config_path=platform_config_file)

    try:
        parsed = parse_receipt(receipt, evidence_path_override)
    except MalformedReceiptException as exc:
        return _record_refusal(
            store, DECISION_MALFORMED, STATE_MALFORMED, exc.reason_token, client_id, before,
            recorded_by,
            f"the receipt is malformed ({exc.reason_token}); the gate stays locked and no "
            "flag was written",
            validating_parameters=_malformed_description(receipt), now=now,
            exception_class="MalformedReceiptException")

    if resolved_pin == UNPINNED:
        # No account id is available to us. The gate cannot pass and records NOTHING:
        # no chain row, no transition row, no flag. Checked immediately after the shape
        # check, so a well-formed receipt cannot get as far as being "verified" against an
        # account we have never been told to trust.
        return _record_refusal(
            store, DECISION_ACCOUNT_NOT_PINNED, STATE_REFUSED, "PLATFORM_ACCOUNT_NOT_PINNED",
            client_id, before, recorded_by,
            "no platform account id is pinned in this register "
            f"({ENV_PLATFORM_ACCOUNT_ID} is unset and config/{PLATFORM_CONFIG_FILENAME} "
            "carries no pin): the live billing milestone cannot be verified, nothing was "
            "recorded",
            recorded=False, now=now)

    if parsed["account_id"] != resolved_pin:
        return _account_mismatch(store, parsed, client_id=client_id, before=before,
                                 recorded_by=recorded_by, pinned=resolved_pin, now=now)

    try:
        check_capabilities(parsed)
    except CapabilityDisabledException as exc:
        return _capability_disabled(store, parsed, exc, client_id=client_id,
                                    recorded_by=recorded_by,
                                    verification_source=verification_source, before=before,
                                    reopen_reason=reopen_reason,
                                    sandbox_only=(resolved_pin == SANDBOX_PIN),
                                    validated_against=resolved_pin, now=now)

    if fetch_adapter is not None:
        try:
            observed = fetch_adapter(parsed["payment_intent_sample"])
        except (RecoverAITimeoutException, TimeoutError) as exc:
            # UNKNOWN never grants and never revokes: every current flag stays
            # byte-identical and the transition is denied.
            record = _record_refusal(
                store, DECISION_TIMEOUT_UNKNOWN, STATE_UNKNOWN, "RECOVERAI_TIMEOUT",
                client_id, before, recorded_by,
                "the independent re-read did not answer in time, so the state of this "
                "verification is UNKNOWN: the transition is denied and no flag changed",
                now=now, exception_class="RecoverAITimeoutException",
                detail=str(exc))
            record["flags_unchanged"] = record["flags_before"] == record["flags_after"]
            return record
        if not isinstance(observed, Mapping):
            # An adapter that answers with something we cannot read has told us nothing.
            # "I could not read the platform" must never pass as confirmation, so an
            # unrecognised shape is a malformed outcome and the gate stays locked.
            return _record_refusal(
                store, DECISION_INDEPENDENT_READ_MALFORMED, STATE_MALFORMED,
                "INDEPENDENT_READ_MALFORMED", client_id, before, recorded_by,
                "the independent re-read returned "
                f"{type(observed).__name__}, which is not a mapping of "
                f"{list(INDEPENDENT_READ_KEYS)}: an answer we cannot read is a refusal, "
                "not a confirmation, so no flag was written", now=now,
                exception_class="MalformedReceiptException",
                received_type=type(observed).__name__)
        malformed = [key for key in INDEPENDENT_READ_KEYS
                     if key not in observed or type(observed[key]) is not bool]
        if malformed:
            return _record_refusal(
                store, DECISION_INDEPENDENT_READ_MALFORMED, STATE_MALFORMED,
                "INDEPENDENT_READ_MALFORMED", client_id, before, recorded_by,
                "the independent re-read did not carry native booleans for "
                f"{malformed} (the contract is {list(INDEPENDENT_READ_KEYS)}): an answer we "
                "cannot read is a refusal, not a confirmation, so no flag was written",
                now=now, exception_class="MalformedReceiptException", malformed_fields=malformed)
        contradicting = [key for key in INDEPENDENT_READ_KEYS if observed[key] is not True]
        if contradicting:
            return _record_refusal(
                store, DECISION_CAPABILITY_DISABLED, STATE_CAPABILITY_DISABLED,
                "INDEPENDENT_READ_NOT_LIVEMODE" if "livemode" in contradicting
                else "INDEPENDENT_READ_CAPABILITY_DISABLED", client_id, before, recorded_by,
                "the independent re-read disagrees with the receipt: it reports "
                f"{contradicting} as false, so no flag was written", now=now,
                exception_class="CapabilityDisabledException",
                contradicting_fields=contradicting)

    if parsed["account_id"] != resolved_pin:
        return _account_mismatch(store, parsed, client_id=client_id, before=before,
                                 recorded_by=recorded_by, pinned=resolved_pin, now=now)

    previous_lock = store.query_one(
        """SELECT verification_id, reopen_reason, recorded_at FROM billing_route_verifications
            WHERE client_id = ? AND account_id = ? AND decision = 'CAPABILITY_DISABLED'
            ORDER BY seq DESC LIMIT 1""", (client_id, resolved_pin))
    if previous_lock and not substantive_reason(reopen_reason):
        return _record_refusal(
            store, DECISION_REOPEN_REFUSED, STATE_CAPABILITY_DISABLED,
            "VERIFICATION_REOPEN_REASON_REQUIRED", client_id, before, recorded_by,
            "a capability-disabled attempt on this account is locked for good; re-opening "
            "it needs a NEW verification event carrying a recorded human reason of at least "
            f"{MIN_SUBSTANTIVE_REASON} non-whitespace characters that says what changed - a "
            "blank, padding-only or placeholder reason is not a reason - and none was given",
            validating_parameters={"previous_locked_verification":
                                   previous_lock["verification_id"],
                                   "required_min_reason_length": MIN_SUBSTANTIVE_REASON,
                                   "received_reason": reopen_reason}, now=now,
            exception_class="GateLockedException")

    sandbox_only = resolved_pin == SANDBOX_PIN
    if sandbox_only:
        with store.tx(immediate=True):
            appended = append_billing_route_verification(
                store, client_id=client_id, parsed=parsed,
                verification_source=verification_source, recorded_by=recorded_by,
                decision=DECISION_PASS_SANDBOX, sandbox_only=True, reopen_reason=reopen_reason,
                note="sandbox pin (NOT our live Stripe account)", now=now)
            insert_transition(
                store, client_id=client_id, event="gate_event",
                field=FLAG_SANDBOX_DEMONSTRATED, old_value="0", new_value="1",
                validating_parameters={"receipt_sha256": appended["receipt_sha256"],
                                       "verification_id": appended["verification_id"],
                                       "sandbox_pin": SANDBOX_PIN,
                                       "account_id": parsed["account_id"]},
                executing_administrator=recorded_by,
                reason=("sandbox-pin validation only: this is not our live Stripe account "
                        "and it does not satisfy the live billing milestone"), now=now)
            store.execute(
                "UPDATE client_autonomy_settings SET "
                "billing_route_demonstrated_in_sandbox = 1 WHERE client_id = ?", (client_id,))
        after = _flag_snapshot(store, client_id)
        return _outcome(
            DECISION_PASS_SANDBOX, STATE_SANDBOX_DEMONSTRATED, "SANDBOX_PIN_VALIDATED",
            client_id=client_id, gate_locked=True, recorded=True, before=before, after=after,
            live_billing_verified=False, sandbox_only=True,
            plain_english=(
                "the receipt validated against the explicit SANDBOX pin. That is a sandbox "
                "pin, not our live Stripe account: it left live_billing_verified FALSE, the "
                "client stays in beta, and the commercial milestone is still unmet"),
            verification_id=appended["verification_id"], chain_hash=appended["chain_hash"],
            receipt_sha256=appended["receipt_sha256"],
            receipt_validated_against=SANDBOX_PIN,
        )

    # The live path. Only reachable with a really pinned, non-sandbox account id that the
    # receipt names byte-for-byte.
    with store.tx(immediate=True):
        existing = store.query_one(
            "SELECT live_billing_receipt_metadata FROM client_autonomy_settings "
            "WHERE client_id = ?", (client_id,))
        already_bound = existing and existing["live_billing_receipt_metadata"]
        appended = append_billing_route_verification(
            store, client_id=client_id, parsed=parsed, verification_source=verification_source,
            recorded_by=recorded_by, decision=DECISION_PASS, sandbox_only=False,
            reopen_reason=reopen_reason, now=now)
        bound_sha = appended["receipt_sha256"]
        rebind_required = False
        if not already_bound:
            document = {"receipt": parsed["receipt"], "receipt_sha256": bound_sha,
                        "verification_id": appended["verification_id"],
                        "chain_hash": appended["chain_hash"]}
            store.execute(
                "UPDATE client_autonomy_settings SET live_billing_verified = 1, "
                "live_billing_receipt_metadata = ? WHERE client_id = ?",
                (canonical_json(document), client_id))
        else:
            held = json.loads(already_bound)
            rebind_required = held.get("receipt_sha256") != bound_sha
            store.execute(
                "UPDATE client_autonomy_settings SET live_billing_verified = 1 "
                "WHERE client_id = ?", (client_id,))
        insert_transition(
            store, client_id=client_id, event="milestone_flag", field=MILESTONE_LIVE_BILLING,
            old_value=str(before[MILESTONE_LIVE_BILLING]), new_value="1",
            validating_parameters={"receipt_sha256": bound_sha,
                                   "verification_id": appended["verification_id"],
                                   "chain_hash": appended["chain_hash"],
                                   "account_id": parsed["account_id"],
                                   "verification_source": verification_source,
                                   "reopen_reason": reopen_reason},
            executing_administrator=recorded_by,
            reason=("billing route verified against the pinned platform account, read back "
                    "through " + verification_source), now=now)
    after = _flag_snapshot(store, client_id)
    return _outcome(
        DECISION_PASS, STATE_SATISFIED, "LIVE_BILLING_ROUTE_VERIFIED",
        client_id=client_id, gate_locked=False, recorded=True, before=before, after=after,
        live_billing_verified=True, sandbox_only=False,
        plain_english=("the live billing route verified: a receipt we have read back, "
                       "checked against the pinned platform account"),
        verification_id=appended["verification_id"], chain_hash=appended["chain_hash"],
        receipt_sha256=bound_sha, receipt_validated_against=resolved_pin,
        receipt_bound=not already_bound, rebind_required=rebind_required,
    )


def _capability_disabled(store, parsed: Mapping[str, Any], exc: CapabilityDisabledException,
                         *, client_id: str, recorded_by: str, verification_source: str,
                         before: Mapping[str, Any], reopen_reason: Optional[str],
                         sandbox_only: bool = False, validated_against: Optional[str] = None,
                         now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """A deficiency is recorded in its own immutable row and locks that attempt for good.

    THE NUANCE, stated plainly: "permanent" does not mean the gate can never open again.
    It means the lock can never be *silently* cleared - this row is append-only and no
    UPDATE, DELETE or rewrite of it is accepted. Re-opening means a NEW verification event
    whose receipt passes, carrying a recorded human reason of at least
    MIN_SUBSTANTIVE_REASON characters that says what changed (see
    VERIFICATION_REOPEN_REASON_REQUIRED above). A gate that could never open even after the
    provider enabled the capability would be dishonest in the opposite direction: it would
    hide the honest state behind an unfixable one.

    `sandbox_only` records WHICH pin validated the receipt behind this refusal: a deficiency
    seen through the SANDBOX pin is a sandbox refusal and is labelled as one, because
    recording it as a live attempt would overstate what we actually tried.
    """
    with store.tx(immediate=True):
        appended = append_billing_route_verification(
            store, client_id=client_id, parsed=parsed, verification_source=verification_source,
            recorded_by=recorded_by, decision=DECISION_CAPABILITY_DISABLED,
            sandbox_only=sandbox_only, reopen_reason=reopen_reason,
            note=(f"capability disabled: {exc.reason_token}; receipt validated against "
                  + ("the sandbox pin (NOT our live account)" if sandbox_only
                     else str(validated_against))), now=now)
        if before[MILESTONE_LIVE_BILLING] == 1:
            # A route that has stopped charging or paying out is not a verified route any
            # more, so the flag is revoked - as its own logged transition, never silently.
            store.execute("UPDATE client_autonomy_settings SET live_billing_verified = 0 "
                          "WHERE client_id = ?", (client_id,))
            insert_transition(
                store, client_id=client_id, event="milestone_flag",
                field=MILESTONE_LIVE_BILLING, old_value="1", new_value="0",
                validating_parameters={"reason_token": exc.reason_token,
                                       "receipt_sha256": appended["receipt_sha256"]},
                executing_administrator=recorded_by,
                reason=(f"revoked: the route stopped reporting {exc.reason_token}, so the "
                        "live billing milestone is no longer satisfied"), now=now)
        insert_transition(
            store, client_id=client_id, event="gate_event",
            field=MILESTONE_LIVE_BILLING, old_value=str(before[MILESTONE_LIVE_BILLING]),
            new_value=str(before[MILESTONE_LIVE_BILLING]),
            validating_parameters={"receipt_sha256": appended["receipt_sha256"],
                                   "verification_id": appended["verification_id"],
                                   "reason_token": exc.reason_token,
                                   "receipt": parsed["receipt"]},
            executing_administrator=recorded_by,
            reason=(f"capability deficiency ({exc.reason_token}): live_billing_verified stays "
                    "FALSE and this attempt is locked in its own immutable row"), now=now)
    after = _flag_snapshot(store, client_id)
    return _outcome(
        DECISION_CAPABILITY_DISABLED, STATE_CAPABILITY_DISABLED, exc.reason_token,
        client_id=client_id, gate_locked=True, recorded=True, before=before, after=after,
        live_billing_verified=False, sandbox_only=sandbox_only,
        receipt_validated_against=validated_against,
        plain_english=(f"the receipt reports a capability deficiency ({exc.reason_token}). "
                       "live_billing_verified is FALSE and this attempt is permanently "
                       "locked: re-opening it needs a new passing verification carrying a "
                       "human reason that says what changed"),
        verification_id=appended["verification_id"], chain_hash=appended["chain_hash"],
        receipt_sha256=appended["receipt_sha256"],
        exception_class="CapabilityDisabledException",
        permanently_locked=True,
        reopen_requires="a NEW verification event whose receipt passes, with a "
                        "human-recorded reason referencing what changed",
    )


def _account_mismatch(store, parsed: Mapping[str, Any], *, client_id: str,
                      before: Mapping[str, Any], recorded_by: str, pinned: str,
                      now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """The security refusal. The alert never echoes the received id."""
    received = parsed["account_id"]
    received_sha256 = sha256_text(received)
    alert = {
        "alert_id": "alert-" + uuid.uuid4().hex,
        "raised_at": clock.iso_ms(_now(now)),
        "priority": "HIGH",
        "kind": "PLATFORM_ACCOUNT_MISMATCH",
        "client_id": client_id,
        "title": "Billing-route receipt names a different account",
        "plain_english": (
            "a Milestone B receipt named an account that is not our pinned platform "
            "account. The gate refused it. The received id is not printed here - only its "
            "hash - so a mismatch alert can never leak the value it rejected."),
        "pinned_account_id_prefix": pinned[:12],
        "received_account_sha256": received_sha256,
        "reason_token": "PLATFORM_ACCOUNT_MISMATCH",
    }
    with store.tx(immediate=True):
        insert_transition(
            store, client_id=client_id, event="gate_event", field=MILESTONE_LIVE_BILLING,
            old_value=str(before[MILESTONE_LIVE_BILLING]),
            new_value=str(before[MILESTONE_LIVE_BILLING]),
            validating_parameters={"pinned_account_id_prefix": pinned[:12],
                                   "received_account_sha256": received_sha256,
                                   "alert_id": alert["alert_id"]},
            executing_administrator=recorded_by,
            reason=("PLATFORM_ACCOUNT_MISMATCH: the receipt's account id does not byte-equal "
                    "the pinned platform account id; gate refused"), now=now)
    record = _outcome(
        DECISION_ACCOUNT_MISMATCH, STATE_REFUSED, "PLATFORM_ACCOUNT_MISMATCH",
        client_id=client_id, gate_locked=True, recorded=True, before=before,
        after=_flag_snapshot(store, client_id), live_billing_verified=False, sandbox_only=False,
        plain_english=("the receipt names an account that is not the pinned platform "
                       "account: refused as a security event"),
        pinned_account_id_prefix=pinned[:12],
        received_account_sha256=received_sha256,
        exception_class="PlatformCredentialMismatchException",
        agent4_alert=alert)
    return record


def outcome_exception(record: Mapping[str, Any]) -> Optional[GateException]:
    """The typed exception for a recorded outcome, or None if the outcome did not refuse."""
    decision = record.get("decision")
    mapping = {
        DECISION_MALFORMED: MalformedReceiptException,
        DECISION_INDEPENDENT_READ_MALFORMED: MalformedReceiptException,
        DECISION_CAPABILITY_DISABLED: CapabilityDisabledException,
        DECISION_TIMEOUT_UNKNOWN: RecoverAITimeoutException,
        DECISION_ACCOUNT_MISMATCH: PlatformCredentialMismatchException,
        DECISION_ACCOUNT_NOT_PINNED: GateLockedException,
        DECISION_REOPEN_REFUSED: GateLockedException,
    }
    if decision not in mapping:
        return None
    exc = mapping[decision](record.get("reason_token"),
                            record.get("plain_english", ""),
                            **({"outstanding": record.get("outstanding_milestones")}
                               if decision in (DECISION_ACCOUNT_NOT_PINNED,
                                               DECISION_REOPEN_REFUSED) else {}))
    exc.outcome = dict(record)
    return exc


def verify_milestone_b_receipt_strict(store, receipt: Any, **kwargs: Any) -> Dict[str, Any]:
    """`verify_milestone_b_receipt`, then raise the taxonomy exception for a refusal."""
    record = verify_milestone_b_receipt(store, receipt, **kwargs)
    exc = outcome_exception(record)
    if exc is not None:
        raise exc
    return record


# ---------------------------------------------------------------------------
# The beta -> full-active state machine
# ---------------------------------------------------------------------------
def milestone_evidence(store, client_id: str, env: Optional[Mapping[str, str]] = None,
                       config_path: Optional[Any] = None) -> Dict[str, Any]:
    """WHICH recorded evidence satisfies each milestone, right now.

    One entry per milestone: the derived value, the evidence tables it was derived from,
    the row ids/hashes that carry it, and the mirror column's own value (reported, never
    trusted). `set_lifecycle_status` stamps this into the immutable transition log, so a
    later reader can see the basis of a promotion instead of having to take it on trust.
    """
    counts = manual_review_counts(store, client_id)
    review_rows = store.query(
        """SELECT decision_id, decision, decided_by, decided_at, operating_mode_at_decision
             FROM manual_review_decisions
            WHERE client_id = ? AND decision IN ('approved', 'rejected')
              AND operating_mode_at_decision = 'hitl'
            ORDER BY decision_id""", (client_id,))
    bound: Dict[str, Any] = {}
    try:
        bound = read_bound_receipt(store, client_id)
    except PlatformCredentialMismatchException as exc:
        bound = {"bound": False, "reason_token": exc.reason_token,
                 "detail": "the bound receipt does not verify; treated as no evidence"}
    msa = commercial_msa_milestone_status(store, client_id)
    flags = _flag_snapshot(store, client_id)
    return {
        MILESTONE_MANUAL_REVIEW: {
            "evidence_source": "manual_review_decisions",
            "value": bool(counts["met"]),
            "mirror_column_value": flags[MILESTONE_MANUAL_REVIEW],
            "review_count": counts["manual_hitl_reviews"],
            "required_reviews": counts["required"],
            "client_caused_exceptions": counts["client_caused_exceptions"],
            "review_decision_ids": [row["decision_id"] for row in review_rows],
        },
        MILESTONE_LIVE_BILLING: {
            "evidence_source": "billing_route_verifications + client_autonomy_settings."
                               "live_billing_receipt_metadata",
            "value": bool(verified_live_billing(store, client_id, env=env,
                                                config_path=config_path)),
            "mirror_column_value": flags[MILESTONE_LIVE_BILLING],
            "pinned_account_id": resolve_platform_account_id(env=env, config_path=config_path),
            "bound": bool(bound.get("bound")),
            "verification_id": bound.get("verification_id"),
            "receipt_sha256": bound.get("receipt_sha256"),
            "chain_hash": bound.get("chain_hash"),
            "account_id": bound.get("account_id"),
            "sandbox_only": bound.get("sandbox_only"),
            "verification_source": bound.get("verification_source"),
        },
        MILESTONE_COMMERCIAL_MSA: {
            "evidence_source": "commercial_msa_records",
            "value": bool(msa["satisfied"]),
            "mirror_column_value": flags[MILESTONE_COMMERCIAL_MSA],
            "msa_id": (msa.get("record") or {}).get("msa_id"),
            "agreement_reference": (msa.get("record") or {}).get("agreement_reference"),
            "document_version": (msa.get("record") or {}).get("document_version"),
            "document_sha256": (msa.get("record") or {}).get("document_sha256"),
            "executed_at": (msa.get("record") or {}).get("executed_at"),
            "record_count": msa.get("record_count", 0),
        },
    }


def milestone_state(store, client_id: str, env: Optional[Mapping[str, str]] = None,
                    config_path: Optional[Any] = None) -> Dict[str, Any]:
    """Each milestone, its honest value, and whether it is met.

    Every `value` here is derived from evidence; `flag_value` is the mirror column, shown
    so the two can be compared and reported, and never used to decide anything.
    """
    counts = manual_review_counts(store, client_id)
    flags = _flag_snapshot(store, client_id)
    live = verified_live_billing(store, client_id, env=env, config_path=config_path)
    msa = commercial_msa_milestone_status(store, client_id)
    milestones = {
        MILESTONE_MANUAL_REVIEW: {
            "flag_value": flags[MILESTONE_MANUAL_REVIEW],
            "value": bool(counts["met"]),
            "plain_english": MILESTONE_PLAIN_ENGLISH[MILESTONE_MANUAL_REVIEW],
            "evidence_source": "manual_review_decisions",
            "detail": (f"{counts['manual_hitl_reviews']} of {counts['required']} manual hitl "
                       f"reviews recorded, {counts['client_caused_exceptions']} client-caused "
                       f"compliance exceptions, {counts['guardrail_blocks']} guardrail blocks "
                       "(a guardrail block is never an infraction)"),
        },
        MILESTONE_LIVE_BILLING: {
            "flag_value": flags[MILESTONE_LIVE_BILLING],
            "value": bool(live),
            "plain_english": MILESTONE_PLAIN_ENGLISH[MILESTONE_LIVE_BILLING],
            "evidence_source": "billing_route_verifications + the bound receipt",
            "detail": _live_billing_detail(store, client_id, env=env, config_path=config_path),
        },
        MILESTONE_COMMERCIAL_MSA: {
            "flag_value": flags[MILESTONE_COMMERCIAL_MSA],
            # Derived from commercial_msa_records, NEVER from the column. The table is
            # empty, so this is FALSE for every client and stays FALSE until somebody
            # actually signs and the agreement is recorded. The column is a mirror.
            "value": bool(msa["satisfied"]),
            "plain_english": MILESTONE_PLAIN_ENGLISH[MILESTONE_COMMERCIAL_MSA],
            "evidence_source": "commercial_msa_records",
            "detail": msa["detail"],
        },
    }
    outstanding = [key for key in MILESTONES if not milestones[key]["value"]]
    return {
        "client_id": client_id,
        "lifecycle_status": flags["lifecycle_status"],
        "milestones": milestones,
        "outstanding_milestones": outstanding,
        "all_true": not outstanding,
        "billing_route_demonstrated_in_sandbox": bool(flags[FLAG_SANDBOX_DEMONSTRATED]),
        "manual_review_counts": counts,
    }


def _live_billing_detail(store, client_id: str, env: Optional[Mapping[str, str]] = None,
                         config_path: Optional[Any] = None) -> str:
    pinned = resolve_platform_account_id(env=env, config_path=config_path)
    if pinned == UNPINNED:
        return ("no platform account id is pinned in the register this client resolves "
                "against, so this cannot be verified: the gate cannot pass while the pin "
                "is UNPINNED")
    if pinned == SANDBOX_PIN:
        return ("the pin resolves to the sandbox register, which can never satisfy the live "
                "milestone")
    row = store.query_one(
        "SELECT live_billing_receipt_metadata FROM client_autonomy_settings WHERE client_id = ?",
        (client_id,))
    if not row or row["live_billing_receipt_metadata"] is None:
        return "no chain-verified receipt is bound to this client"
    try:
        bound = read_bound_receipt(store, client_id)
    except PlatformCredentialMismatchException as exc:
        return f"the bound receipt does not verify ({exc.reason_token}); treated as unmet"
    return (f"bound to verification {bound['verification_id']}, read back through "
            f"{bound['verification_source']}")


def live_billing_honest_label(store, client_id: str,
                              env: Optional[Mapping[str, str]] = None,
                              config_path: Optional[Any] = None) -> str:
    """The one true sentence about Milestone B, for the demo and the console.

    Derived every time from the register that actually resolves AND from this client's own
    milestone state - never a fixed string. That is deliberate: the previous label asserted
    "no platform account id is pinned" unconditionally, which the demo printed in public
    after the register was pinned with an API-verified account id. A label that hardcodes
    either direction goes false the moment the register changes; this one reads correctly
    with no pin, with the sandbox pin, with the live pin and the milestone unmet, and once a
    real receipt is bound.

    What it may say about the account is limited to what the register itself records. With
    no recorded facts it falls back to the id alone, so it cannot turn a pin into a payment.
    """
    pinned = resolve_platform_account_id(env=env, config_path=config_path)
    if pinned == UNPINNED:
        return ("no platform account id is pinned in this register, so the live billing "
                "milestone cannot be verified and nothing is recorded")
    if pinned == SANDBOX_PIN:
        return ("the pin resolves to the sandbox register, which can never satisfy the live "
                "billing milestone: it stays unrecorded")
    facts = platform_register_facts(config_path)
    named = f' ("{facts["business_profile_name"]}")' if "business_profile_name" in facts else ""
    verified = f", verified by {facts['verified_by']}" if "verified_by" in facts else ""
    state = milestone_state(store, client_id, env=env, config_path=config_path)
    if state["milestones"][MILESTONE_LIVE_BILLING]["value"]:
        return (f"the platform account id is pinned to {pinned}{named}{verified}, and a "
                f"chain-verified receipt is bound to {client_id}: the live billing milestone "
                "is recorded")
    detail = _live_billing_detail(store, client_id, env=env, config_path=config_path)
    activity = (f"the register records that account has never taken a live payment "
                f"({facts['activity_summary']})" if "activity_summary" in facts
                else "no live payment on that account is recorded")
    return (f"the platform account id IS pinned to {pinned}{named}{verified}; the live "
            f"billing milestone is nevertheless UNRECORDED for {client_id}, because {detail} "
            f"and {activity}")


def launch_condition(store, client_id: str, env: Optional[Mapping[str, str]] = None,
                     config_path: Optional[Any] = None) -> Dict[str, Any]:
    """The launch condition: rejected unless every milestone is TRUE."""
    state = milestone_state(store, client_id, env=env, config_path=config_path)
    state["launch_allowed"] = bool(state["all_true"])
    state["outstanding_plain_english"] = [state["milestones"][key]["plain_english"]
                                          for key in state["outstanding_milestones"]]
    state["evidence"] = milestone_evidence(store, client_id, env=env, config_path=config_path)
    return state


def set_lifecycle_status(store, client_id: str, new_status: str, who: str, reason: str,
                         env: Optional[Mapping[str, str]] = None,
                         config_path: Optional[Any] = None,
                         now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """THE ONLY writer of lifecycle_status. Statuses never move without a logged reason.

    STATUS_FULL_ACTIVE is refused unless every milestone is satisfied BY ITS EVIDENCE, and
    the store's own triggers recompute the same evidence, so a hand-written UPDATE cannot
    do what this function refuses. The transition row carries `evidence`: which recorded
    rows satisfied each milestone, so a later reader can see the basis of the promotion.
    """
    if new_status not in LIFECYCLE_STATES:
        raise GateException("UNKNOWN_LIFECYCLE_STATUS",
                            f"unknown lifecycle status {new_status!r}; known: "
                            f"{list(LIFECYCLE_STATES)}", requested=new_status)
    if not reason:
        raise GateException("TRANSITION_REASON_REQUIRED",
                            "a lifecycle transition must carry a reason")
    _client_row(store, client_id)
    condition = launch_condition(store, client_id, env=env, config_path=config_path)
    evidence = condition["evidence"]
    old_status = condition["lifecycle_status"]
    if new_status == STATUS_FULL_ACTIVE and not condition["all_true"]:
        with store.tx(immediate=True):
            insert_transition(
                store, client_id=client_id, event="gate_event", field="lifecycle_status",
                old_value=old_status, new_value=old_status,
                validating_parameters={"outstanding_milestones":
                                       condition["outstanding_milestones"],
                                       "milestones": {k: v["value"] for k, v in
                                                      condition["milestones"].items()},
                                       "evidence": evidence},
                executing_administrator=who,
                reason=f"REFUSED promotion to {STATUS_FULL_ACTIVE}: launch condition unmet; "
                       f"{reason}", now=now)
        raise GateLockedException(
            "LIFECYCLE_LAUNCH_CONDITION_UNMET",
            "the launch condition is rejected unless every milestone is TRUE",
            outstanding=condition["outstanding_milestones"],
            lifecycle_status=old_status, requested=new_status, outcome=condition)
    with store.tx(immediate=True):
        if old_status != new_status:
            store.execute(
                "UPDATE client_autonomy_settings SET lifecycle_status = ? WHERE client_id = ?",
                (new_status, client_id))
        insert_transition(
            store, client_id=client_id, event="lifecycle_status", field="lifecycle_status",
            old_value=old_status, new_value=new_status,
            validating_parameters={"outstanding_milestones": condition["outstanding_milestones"],
                                   "milestones": {k: v["value"] for k, v in
                                                  condition["milestones"].items()},
                                   # WHICH recorded evidence satisfied each milestone: the
                                   # review row ids, the chain verification + receipt hash,
                                   # the recorded agreement. A promotion is auditable after
                                   # the fact instead of resting on this function's word.
                                   "evidence": evidence},
            executing_administrator=who, reason=reason, now=now)
    return {"client_id": client_id, "old_status": old_status, "new_status": new_status,
            "changed": old_status != new_status, "executing_administrator": who,
            "reason": reason, "recorded_at": clock.iso_ms(_now(now)),
            "outstanding_milestones": condition["outstanding_milestones"],
            "satisfied_by_evidence": evidence}


# ---------------------------------------------------------------------------
# The absolute code gate: live outbound only
# ---------------------------------------------------------------------------
def gate_lock_record(store, client_id: str, channel: str, who: str, reason: str,
                     env: Optional[Mapping[str, str]] = None,
                     config_path: Optional[Any] = None,
                     now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    condition = launch_condition(store, client_id, env=env, config_path=config_path)
    outstanding = condition["outstanding_milestones"]
    reason_token = ("GATE_LOCKED_LIVE_OUTBOUND" if outstanding
                    else "LIVE_SENDING_NOT_IMPLEMENTED")
    plain_english = (
        "the live collections launcher is refused: " +
        ("the launch condition is unmet, outstanding milestones: "
         + ", ".join(outstanding) if outstanding
         else "every milestone is TRUE, but there is no live sending infrastructure to "
              "launch - the launcher is a stub whose only correct behaviour today is to "
              "refuse"))
    record = {
        "gate": "live_outbound_launch",
        "client_id": client_id,
        "channel": channel,
        "decision": "REFUSED",
        "state": STATE_REFUSED,
        "reason_token": reason_token,
        "outstanding_milestones": outstanding,
        "outstanding_plain_english": condition["outstanding_plain_english"],
        "lifecycle_status": condition["lifecycle_status"],
        "plain_english": plain_english,
        "sandbox_send_path_affected": False,
        "recorded_at": clock.iso_ms(_now(now)),
        "executing_administrator": who,
        "reason": reason,
    }
    with store.tx(immediate=True):
        insert_transition(
            store, client_id=client_id, event="gate_event", field="live_outbound_launch",
            old_value=None, new_value=None,
            validating_parameters={"outstanding_milestones": outstanding,
                                   "channel": channel,
                                   "lifecycle_status": condition["lifecycle_status"]},
            executing_administrator=who, reason=f"{reason_token}: {reason}", now=now)
    return record


def launch_live_outbound(store, client_id: str, channel: str, who: str, reason: str,
                         env: Optional[Mapping[str, str]] = None,
                         config_path: Optional[Any] = None,
                         now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """The gate that matters: the live collections launcher. It REFUSES.

    This gates ONLY the live launcher. The existing sandbox send path
    (engine/orchestrator.py) is untouched by it: locking that would break the demo and
    every compliance test. There is no real live sending infrastructure, so a stub that
    always refuses is the only correct implementation today.
    """
    record = gate_lock_record(store, client_id, channel, who, reason, env=env,
                              config_path=config_path, now=now)
    raise GateLockedException(record["reason_token"],
                             record["plain_english"],
                             outstanding=record["outstanding_milestones"],
                             outcome=record)


# ---------------------------------------------------------------------------
# Agent 4: structured alerts and the plain-English tooltip
# ---------------------------------------------------------------------------
def agent4_alert(record: Mapping[str, Any], *, client_id: str,
                 now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """An Agent 4 System Alert record for a gate state. Rendering is the console's job."""
    decision = record.get("decision")
    priority = "HIGH" if decision in (DECISION_ACCOUNT_MISMATCH, DECISION_CAPABILITY_DISABLED,
                                      DECISION_TIMEOUT_UNKNOWN) else "NORMAL"
    return {
        "alert_id": "alert-" + uuid.uuid4().hex,
        "raised_at": clock.iso_ms(_now(now)),
        "agent": "Agent 4 - Customer Success",
        "priority": priority,
        "kind": f"GATE_{decision}",
        "client_id": client_id,
        "title": {
            DECISION_ACCOUNT_MISMATCH: "Billing-route receipt names a different account",
            DECISION_ACCOUNT_NOT_PINNED: "Billing-route gate cannot pass: no platform "
                                         "account id is pinned in this register",
            DECISION_CAPABILITY_DISABLED: "Billing route cannot charge or pay out",
            DECISION_TIMEOUT_UNKNOWN: "Billing-route verification did not answer in time",
            DECISION_MALFORMED: "Billing-route receipt is malformed",
            DECISION_REOPEN_REFUSED: "A locked billing-route attempt needs a reason to "
                                     "re-open",
            DECISION_PASS: "Billing route verified",
            DECISION_PASS_SANDBOX: "Sandbox billing route demonstrated (not the live one)",
        }.get(decision, "Billing-route gate event"),
        "plain_english": record.get("plain_english", ""),
        "reason_token": record.get("reason_token"),
        "outstanding_milestones": record.get("outstanding_milestones", []),
        "receipt_sha256": record.get("receipt_sha256"),
        "verification_id": record.get("verification_id"),
    }


def emit_agent4_alert(audit, record: Mapping[str, Any], *, client_id: str,
                      now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Write the Agent 4 System Alert for a gate state into the guardrail action log.

    Structured records only: the gate never prints, and the alert text is built here so
    the console (a separate task) has one payload to render.
    """
    alert = record.get("agent4_alert") or agent4_alert(record, client_id=client_id, now=now)
    if audit is not None:
        audit.record("agent4_alert", **alert)
    return alert


def milestone_tooltip(store, client_id: str, env: Optional[Mapping[str, str]] = None,
                      config_path: Optional[Any] = None) -> Dict[str, Any]:
    """Exactly which milestones are outstanding, in plain English. Payload only.

    The console rendering is a separate task: this builds the payload and nothing else.
    """
    state = launch_condition(store, client_id, env=env, config_path=config_path)
    outstanding = state["outstanding_milestones"]
    lines: List[str] = []
    for index, key in enumerate(outstanding, start=1):
        milestone = state["milestones"][key]
        lines.append(f"{index}. {milestone['plain_english']} — {milestone['detail']}")
    if not lines:
        text = (f"{client_id} has met every milestone. It still cannot send live today: "
                "there is no live sending infrastructure, so the live launcher refuses.")
    else:
        plural = "milestone" if len(outstanding) == 1 else "milestones"
        text = (f"{client_id} cannot go live yet. {len(outstanding)} {plural} outstanding: "
                + " ".join(lines))
    return {
        "client_id": client_id,
        "lifecycle_status": state["lifecycle_status"],
        "can_go_live": state["launch_allowed"],
        "outstanding_count": len(outstanding),
        "outstanding_milestones": outstanding,
        "text": text,
        "milestones": [
            {"key": key,
             "met": state["milestones"][key]["value"],
             "flag_value": state["milestones"][key]["flag_value"],
             "plain_english": state["milestones"][key]["plain_english"],
             "detail": state["milestones"][key]["detail"]}
            for key in MILESTONES
        ],
        "billing_route_demonstrated_in_sandbox":
            state["billing_route_demonstrated_in_sandbox"],
        "honest_labels": [
            "every client is a beta tester today",
            live_billing_honest_label(store, client_id, env=env, config_path=config_path),
            "charges_enabled cannot be read with our tools",
            "no executed commercial Master Services Agreement is recorded, so the "
            "commercial milestone cannot be met",
            "$0 collected to date",
        ],
    }
