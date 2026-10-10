"""Agent 1 - Ledger Ingestion.

Fetches a client export over HTTP (a real request to our local mock vendor servers,
never a local file read), then acts as a translator: whatever the export calls a
column, it maps to the one normalized schema.

Target schema (fixed):
    debtor_id, client_id, name, email, phone, balance_owed, invoice_date, aging_state

Every source column -> target field decision is recorded and returned in a mapping
report, which is what the console renders as "Invoices Successfully Imported".
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import clock, config, onboarding, timezones

TARGET_FIELDS = [
    "debtor_id",
    "client_id",
    "name",
    "email",
    "phone",
    "balance_owed",
    "invoice_date",
    "aging_state",
]
#: fields we are happy to have but that are not part of the normalized contract
OPTIONAL_TARGETS = ["invoice_number"]


def _key(name: str) -> str:
    """Normalize a column name for alias matching: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _alias_index() -> Dict[str, str]:
    aliases = config.load_aliases()["target_fields"]
    index: Dict[str, str] = {}
    for target, names in aliases.items():
        for name in names:
            index[_key(name)] = target
        index.setdefault(_key(target), target)
    return index


# --- value parsing ----------------------------------------------------------
def parse_money(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9\.\-]", "", text)
    if cleaned in ("", "-", "."):
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return -amount if negative else amount


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d %b %Y", "%b %d, %Y", "%d-%b-%Y")


def parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def aging_for(invoice_date: Optional[date], today: Optional[date] = None) -> Tuple[str, Optional[int], str]:
    """(aging_state, age_days, note) - edge cases are stored explicitly, never guessed."""
    today = today or clock.now_utc().date()
    if invoice_date is None:
        return "unknown", None, "invoice_date missing or unparseable; aging state stored as unknown"
    days = (today - invoice_date).days
    if days < 0:
        return "future", days, "invoice_date is after the run date; stored explicitly, not aged"
    return config.aging_state_for_days(days), days, f"{days} days since invoice date {invoice_date.isoformat()}"


# --- fetch ------------------------------------------------------------------
#: Hard ceiling on the ledger export GET. The fetch either returns or raises - it never
#: blocks indefinitely, and a failure is never mistaken for "this client has no debtors".
FETCH_TIMEOUT = 10.0


def fetch_export(base_url: str, path: str, timeout: float = FETCH_TIMEOUT) -> Dict[str, Any]:
    """A real HTTP GET against the mock vendor server. Bounded, and raises on failure."""
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "RecoverAI-Agent1/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(
            f"ledger export fetch timed out after {timeout:.0f}s: GET {url}") from exc
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ledger export fetch failed: GET {url} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ledger export fetch failed: GET {url} -> {exc.reason}") from exc
    return json.loads(payload)


# --- result types -----------------------------------------------------------
@dataclass
class MappingRow:
    source_column: str
    target_field: Optional[str]
    mapped: bool
    sample_value: Any

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_column": self.source_column,
            "target_field": self.target_field,
            "mapped": self.mapped,
            "sample_value": self.sample_value,
        }


@dataclass
class IngestResult:
    source_system: str
    endpoint: str
    client_id_hint: Optional[str]
    records: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    mapping: List[MappingRow] = field(default_factory=list)

    @property
    def normalized_count(self) -> int:
        return len(self.records)

    def unmapped_columns(self) -> List[str]:
        return [m.source_column for m in self.mapping if not m.mapped]

    def report(self) -> Dict[str, Any]:
        """The plain-English summary the console shows after a connection."""
        buckets: Dict[str, int] = {}
        for rec in self.records:
            buckets[rec["aging_state"]] = buckets.get(rec["aging_state"], 0) + 1
        return {
            "source_system": self.source_system,
            "endpoint": self.endpoint,
            "client_id_used": self.client_id_hint,
            "records_fetched": self.normalized_count + len(self.rejected),
            "records_normalized": self.normalized_count,
            "records_rejected": len(self.rejected),
            "rejected_detail": self.rejected,
            "mapping": [m.as_dict() for m in self.mapping],
            "unmapped_columns": self.unmapped_columns(),
            "aging_buckets": buckets,
            "target_fields": TARGET_FIELDS,
            "plain_english": (
                f"{self.normalized_count} invoices imported from {self.source_system} "
                f"({len(self.mapping)} source columns translated into the normalized schema, "
                f"{len(self.unmapped_columns())} unmapped)"
            ),
        }


# --- translation ------------------------------------------------------------
def normalize(raw_records: Iterable[Dict[str, Any]], source_system: str, endpoint: str,
              client_id_hint: Optional[str] = None) -> IngestResult:
    index = _alias_index()
    result = IngestResult(source_system=source_system, endpoint=endpoint, client_id_hint=client_id_hint)
    seen_columns: Dict[str, MappingRow] = {}

    for raw in raw_records:
        normalized: Dict[str, Any] = {}
        per_record_columns: Dict[str, str] = {}
        for src_col, value in raw.items():
            target = index.get(_key(src_col))
            if src_col not in seen_columns:
                seen_columns[src_col] = MappingRow(src_col, target, target is not None, value)
            if target:
                # first non-empty alias wins; never overwrite with a later duplicate
                if target not in normalized or normalized[target] in (None, ""):
                    normalized[target] = value
                    per_record_columns[src_col] = target

        problems: List[str] = []
        debtor_id = str(normalized.get("debtor_id") or "").strip()
        if not debtor_id:
            problems.append("no debtor identifier column mapped (debtor_id is required)")
        client_id = str(normalized.get("client_id") or client_id_hint or "").strip()
        if not client_id:
            problems.append("no client_id column mapped and no client hint supplied")

        if problems:
            result.rejected.append({"raw_record": raw, "reason": "; ".join(problems)})
            continue

        invoice_date = parse_date(normalized.get("invoice_date"))
        aging_state, age_days, note = aging_for(invoice_date)
        phone = normalized.get("phone")
        phone = str(phone).strip() if phone not in (None, "") else None
        window = timezones.window_decision_for(phone)

        record = {
            "debtor_id": debtor_id,
            "client_id": client_id,
            "name": (str(normalized.get("name")).strip() if normalized.get("name") not in (None, "") else None),
            "email": (str(normalized.get("email")).strip() if normalized.get("email") not in (None, "") else None),
            "phone": phone,
            "balance_owed": parse_money(normalized.get("balance_owed")),
            "invoice_date": invoice_date.isoformat() if invoice_date else None,
            "aging_state": aging_state,
            "invoice_number": (str(normalized.get("invoice_number")).strip()
                               if normalized.get("invoice_number") not in (None, "") else None),
            "aging_note": note,
            "age_days": age_days,
            "source_system": source_system,
            "source_ref": endpoint,
            "source_columns": json.dumps(per_record_columns, sort_keys=True),
            "raw_record": json.dumps(raw, default=str, sort_keys=True),
            "area_code": window["area_code"],
            "tz_name": window["tz_name"],
            "derived_local_time": window["derived_local_time"],
            "window_decision": window["window_decision"],
            "window_reason": window["reason"],
        }
        if record["balance_owed"] is None:
            record["aging_note"] = (record["aging_note"] or "") + "; balance_owed missing or unparseable"
        result.records.append(record)

    result.mapping = list(seen_columns.values())
    return result


def persist(store, result: IngestResult) -> int:
    """Upsert normalized rows into debtor_records. approval_status starts at Pending."""
    written = 0
    now = clock.iso()
    with store.tx(immediate=True):
        for rec in result.records:
            store.execute(
                """INSERT INTO debtor_records
                     (debtor_id, client_id, name, email, phone, balance_owed, invoice_date, aging_state,
                      approval_status, dnc_flag, invoice_number, source_system, source_ref, source_columns,
                      raw_record, aging_note, area_code, tz_name, derived_local_time, window_decision,
                      window_decision_at, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(debtor_id) DO UPDATE SET
                      client_id=excluded.client_id, name=excluded.name, email=excluded.email,
                      phone=excluded.phone, balance_owed=excluded.balance_owed,
                      invoice_date=excluded.invoice_date, aging_state=excluded.aging_state,
                      invoice_number=excluded.invoice_number, source_system=excluded.source_system,
                      source_ref=excluded.source_ref, source_columns=excluded.source_columns,
                      raw_record=excluded.raw_record, aging_note=excluded.aging_note,
                      area_code=excluded.area_code, tz_name=excluded.tz_name,
                      derived_local_time=excluded.derived_local_time,
                      window_decision=excluded.window_decision,
                      window_decision_at=excluded.window_decision_at, imported_at=excluded.imported_at""",
                (
                    rec["debtor_id"], rec["client_id"], rec["name"], rec["email"], rec["phone"],
                    rec["balance_owed"], rec["invoice_date"], rec["aging_state"], "Pending", 0,
                    rec["invoice_number"], rec["source_system"], rec["source_ref"], rec["source_columns"],
                    rec["raw_record"], rec["aging_note"], rec["area_code"], rec["tz_name"],
                    rec["derived_local_time"], rec["window_decision"], now, now,
                ),
            )
            written += 1
    return written


def import_from_endpoint(store, base_url: str, path: str, source_system: str,
                         client_id_hint: Optional[str] = None) -> IngestResult:
    """Fetch -> translate -> persist. One call, as Agent 1 does it."""
    payload = fetch_export(base_url, path)
    raw_records = payload.get("invoices", payload.get("records", payload if isinstance(payload, list) else []))
    result = normalize(raw_records, source_system=source_system, endpoint=base_url.rstrip("/") + path,
                       client_id_hint=client_id_hint or payload.get("client_id"))
    persist(store, result)
    return result


# ============================================================================
# Phase 3 - the spreadsheet intake (a client's own CSV/Excel file)
#
# Same translator, same normalizer, same persist() as a live vendor export: this
# section adds a way IN, not a second mapping engine. What it adds is the two
# things a file handed over by a human needs and an HTTP export does not:
#
#   PREVIEW  parse -> translate -> validate in memory, and write NOTHING. It
#            reports which column became which field, which rows are dropped and
#            with exactly which reason token, and what the advisory consumer-debt
#            scanner sees.
#   COMMIT   a separate action that only runs against an existing, immutable
#            ingestion_batches record whose stored fingerprint matches the
#            previewed state. No record, or any drift, is a refusal - never a
#            partial import.
#
# Fail-closed rules, all of them: numeric coercion and date guessing are refused
# (`AMOUNT_NOT_NUMERIC`, `UNPARSEABLE_DATE`), a zero or credit balance is refused
# (`BLOCKED_NO_BALANCE_DUE`), a missing phone/email is FLAGGED and never invented,
# an unmapped column is REPORTED and never guessed into a field, and two columns
# claiming one target field is a mapping ERROR rather than a silent overwrite.
# ============================================================================

#: The documented genesis constant the ingestion-batch chain starts from.
GENESIS_BATCH_CHAIN_HASH = hashlib.sha256(
    b"recoverai:ingestion_batches:genesis:v1").hexdigest()

# --- the tokens ------------------------------------------------------------
#: A dropped row carries exactly one of these (rulebook: spreadsheet_intake.
#: reason_tokens.row_dropped, in evaluation_order_per_row).
DROP_MISSING_DEBTOR_ID = "MISSING_DEBTOR_ID"
DROP_AMOUNT_NOT_NUMERIC = "AMOUNT_NOT_NUMERIC"
DROP_MISSING_BALANCE = "MISSING_BALANCE"
DROP_UNPARSEABLE_DATE = "UNPARSEABLE_DATE"
DROP_BLOCKED_NO_BALANCE = "BLOCKED_NO_BALANCE_DUE"
DROP_ROW_SHAPE = "ROW_SHAPE_MISMATCH"
#: An imported row that is missing something we refuse to invent.
FLAG_MISSING_PHONE = "MISSING_PHONE"
FLAG_MISSING_EMAIL = "MISSING_EMAIL"
FLAG_MISSING_INVOICE_DATE = "MISSING_INVOICE_DATE"
#: Mapping vocabulary.
MAPPING_UNMAPPED_COLUMN = "UNMAPPED_COLUMN"
MAPPING_DUPLICATE_TARGET = "DUPLICATE_TARGET_FIELD"
#: Commit / registration refusals.
REFUSAL_NO_BATCH = "BLOCKED_NO_INGESTION_BATCH"
REFUSAL_PREVIEW_STATE_CHANGED = "BATCH_PREVIEW_STATE_CHANGED"
REFUSAL_BATCH_TAMPERED = "BATCH_RECORD_TAMPERED"
REFUSAL_BATCH_ALREADY_COMMITTED = "BATCH_ALREADY_COMMITTED"
REFUSAL_MAPPING_ERROR = "BLOCKED_MAPPING_ERROR"
REFUSAL_WARRANTY_NOT_ACKNOWLEDGED = "BLOCKED_WARRANTY_NOT_ACKNOWLEDGED"
REFUSAL_NO_WARRANTY_SIGNATURE = "BLOCKED_NO_WARRANTY_SIGNATURE"
#: Phase 4: the REAL Step 3 gate. The warranty a batch binds to is no longer just a
#: sha256 of a rulebook string - it is the client's own recorded, versioned attestation
#: row (onboarding_attestations, type 'step3_data_warranty'). A batch raised before its
#: client attested refuses with this token.
REFUSAL_NO_STEP3_ATTESTATION = "BLOCKED_NO_STEP3_ATTESTATION"
REFUSAL_UNSUPPORTED_FILE = "UNSUPPORTED_FILE_TYPE"

#: The source_system / source_ref stamped on rows a spreadsheet import writes, so a
#: single SQL query shows which upload (which file, which batch) a debtor came from.
SPREADSHEET_SOURCE_LABEL = "Spreadsheet intake"
BATCH_SOURCE_REF_PREFIX = "ingestion_batch:"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    """Sorted keys, compact separators: one payload, exactly one byte string."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_canonical(payload: Any) -> str:
    return sha256_text(canonical_json(payload))


def _refusal(token: str, **fields: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "refusal_token": token}
    out.update(fields)
    return out


# --- value parsing (strict) -------------------------------------------------
#: US-format money only: an optional sign, digits with optional thousands
#: separators, an optional decimal part. Anything else is `AMOUNT_NOT_NUMERIC`.
#: Note what this is NOT: a "strip the junk out and hope" cleaner. `12abc` and
#: `1.234,56` are refused, not silently read as 12 and 1.234.
_STRICT_MONEY = re.compile(r"^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_CURRENCY_NOISE = ("$", "\u00a3", "\u20ac", "\u00a5", " ", "\u00a0")


def parse_money_strict(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """(amount, failure). `failure` is None, "missing" or "not_numeric".

    Never coerces and never guesses: this is the fail-closed sibling of
    `parse_money()`, which the live-export path keeps using unchanged.
    """
    if value is None:
        return None, "missing"
    if isinstance(value, bool):
        return None, "not_numeric"
    if isinstance(value, (int, float)):
        return float(value), None
    text = str(value).strip()
    if not text:
        return None, "missing"
    negative = text.startswith("(") and text.endswith(")") and len(text) > 2
    if negative:
        text = text[1:-1].strip()
    cleaned = text
    for noise in _CURRENCY_NOISE:
        cleaned = cleaned.replace(noise, "")
    if cleaned.endswith("-") and not cleaned[:-1].endswith((".", ",")):
        cleaned = cleaned[:-1]  # `100.00-` is a trailing-minus credit
    if not _STRICT_MONEY.match(cleaned):
        return None, "not_numeric"
    amount = float(cleaned.replace(",", ""))
    return (-amount if negative else amount), None


# --- file parsing -----------------------------------------------------------
@dataclass
class SheetTable:
    """A parsed file: one header row and the raw cell values beneath it."""

    headers: List[str]
    rows: List[List[Any]]
    source_format: str

    def raw_row(self, row: Sequence[Any]) -> Dict[str, Any]:
        """The row as a dict keyed by its ORIGINAL header names - the same shape a
        live vendor export arrives in, which is what `normalize()` consumes."""
        return {header: (row[i] if i < len(row) else "") for i, header in enumerate(self.headers)}


def _clean_cell(value: Any) -> Any:
    """An .xlsx cell as a JSON-safe value. A date cell is its ISO date (exact, not a
    guess), a whole-number float is that integer, `None` is an empty string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def _finish_table(headers: List[str], rows: List[List[Any]], source_format: str) -> SheetTable:
    # Trailing blank columns carry no header and no data; drop them rather than
    # reporting a dozen "" columns as unmapped. The BODY rows are left at their own
    # width on purpose: padding or truncating them here is exactly the silent guess
    # the shape check in `preview_spreadsheet` exists to refuse.
    last_named = -1
    for i, header in enumerate(headers):
        if str(header).strip():
            last_named = i
    headers = headers[: last_named + 1]
    while rows and all(str(c).strip() == "" for c in rows[-1]):
        rows.pop()
    if not headers:
        raise ValueError(f"the {source_format} file has no header row")
    return SheetTable([str(h).strip() for h in headers], rows, source_format)


def row_shape_problem(row: Sequence[Any], header_count: int) -> Optional[str]:
    """A row that does not line up with its header row, described - or None.

    A short row is short. A long row is fine ONLY when every extra cell is empty
    (a spreadsheet app pads to the widest used column); extra cells carrying data
    are a misalignment, and padding or truncating them would be a guess.
    """
    if len(row) < header_count:
        return f"{len(row)} cells for {header_count} columns"
    extra = row[header_count:]
    if any(str(cell).strip() for cell in extra):
        return f"{len(row)} cells for {header_count} columns, with data in the extra cells"
    return None


def read_csv_text(text: str) -> SheetTable:
    """Parse CSV text with the stdlib reader. Csv is not typed, so everything stays a
    string and the strict parsers decide what is a number and what is not."""
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text, newline=""))
    raw = [list(row) for row in reader]
    if not raw:
        raise ValueError("the CSV file is empty")
    return _finish_table([str(h) for h in raw[0]], [list(r) for r in raw[1:]], "csv")


def read_xlsx_bytes(data: bytes) -> SheetTable:
    """Parse a binary .xlsx stream with openpyxl (read-only, values only).

    openpyxl is imported HERE, lazily: the CSV path and every test that does not touch
    a workbook run on the standard library alone. `data_only=True` reads the cached
    value of a formula cell, never the formula.
    """
    try:
        import openpyxl  # noqa: WPS433 - deliberately lazy, see the docstring
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise ValueError(
            "reading .xlsx needs the openpyxl package; install it with "
            "`python3 -m pip install --break-system-packages openpyxl` "
            "(a CSV upload needs nothing extra)"
        ) from exc
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - any malformed workbook fails closed
        raise ValueError(f"the .xlsx file could not be opened: {exc}") from exc
    try:
        sheet = workbook.active
        if sheet is None:
            raise ValueError("the .xlsx file has no active worksheet")
        raw = [[_clean_cell(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    if not raw:
        raise ValueError("the .xlsx worksheet is empty")
    return _finish_table([str(h) for h in raw[0]], [list(r) for r in raw[1:]], "xlsx")


def read_table(file_name: str, content: Any, source_format: Optional[str] = None) -> SheetTable:
    """Dispatch on the declared format or the file extension. A format we do not
    accept fails closed with `UNSUPPORTED_FILE_TYPE` rather than being guessed at."""
    fmt = (source_format or "").strip().lower()
    name = str(file_name or "").strip().lower()
    if not fmt:
        if name.endswith(".csv"):
            fmt = "csv"
        elif name.endswith(".xlsx") or name.endswith(".xlsm"):
            fmt = "xlsx"
    if fmt == "csv":
        text = content.decode("utf-8-sig") if isinstance(content, (bytes, bytearray)) else str(content)
        return read_csv_text(text)
    if fmt == "xlsx":
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return read_xlsx_bytes(data)
    raise ValueError(
        REFUSAL_UNSUPPORTED_FILE + ": only .csv and .xlsx files are accepted "
        f"(got file_name={file_name!r}, source_format={source_format!r})")


def file_sha256(content: Any) -> str:
    """The sha256 of the file we were handed: over the exact bytes for a binary
    workbook, over the UTF-8 encoding of the text for a CSV."""
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return hashlib.sha256(data).hexdigest()


# --- the mapping report -----------------------------------------------------
def build_mapping(headers: Sequence[str], sample_row: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """source column -> target field, using Agent 1's ONE alias index.

    Two rules, both fail-closed:
      * a column with no documented alias is reported as unmapped, never guessed
        into a field;
      * two source columns claiming the same target field is a mapping ERROR
        (`DUPLICATE_TARGET_FIELD`) surfaced to the user, never a silent overwrite.
    """
    index = _alias_index()
    rows: List[MappingRow] = []
    by_target: Dict[str, List[str]] = {}
    for i, header in enumerate(headers):
        name = str(header)
        target = index.get(_key(name))
        sample = None
        if sample_row is not None and i < len(sample_row):
            sample = _clean_cell(sample_row[i])
        rows.append(MappingRow(name, target, target is not None, sample))
        if target:
            by_target.setdefault(target, []).append(name)
    duplicates = [{"target_field": target, "source_columns": columns}
                  for target, columns in sorted(by_target.items()) if len(columns) > 1]
    unmapped = [r.source_column for r in rows if not r.mapped]
    return {
        "rows": [r.as_dict() for r in rows],
        "mapped_count": len(rows) - len(unmapped),
        "unmapped_columns": unmapped,
        "unmapped_token": MAPPING_UNMAPPED_COLUMN if unmapped else None,
        "duplicate_targets": duplicates,
        "mapping_ok": not duplicates,
        "mapping_error_token": MAPPING_DUPLICATE_TARGET if duplicates else None,
        "target_fields": list(TARGET_FIELDS),
    }


# --- the advisory consumer-debt scanner -------------------------------------
def _entity_marker(name: str) -> bool:
    cfg = config.consumer_debt_scan_cfg()
    low = str(name).lower()
    if "&" in low:
        return True
    markers = {str(m).lower() for m in cfg["entity_identifiers"]}
    markers.discard("&")
    return any(token in markers for token in re.split(r"[^a-z0-9]+", low) if token)


def looks_individual(name: Any) -> bool:
    """Does this look like an individual person's name rather than an entity?

    2-4 alphabetic tokens (plus punctuation, hyphens and apostrophes), no digits and
    no entity marker. `Whitfield, Marcus` counts; `Cedar Lane Design Studio`,
    `Northwind Freight LLC` and `Debtor D-1` do not.
    """
    if name in (None, ""):
        return False
    text = str(name).strip()
    if not text or _entity_marker(text):
        return False
    if re.search(r"\d", text):
        return False
    cfg = config.consumer_debt_scan_cfg()
    tokens = [t for t in re.split(r"[^A-Za-z'\-.]+", text) if t.strip(". -")]
    return int(cfg["individual_name_token_min"]) <= len(tokens) <= int(cfg["individual_name_token_max"])


def scan_consumer_debt(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The advisory B2B / consumer-looking footprint scan.

    It NEVER blocks, drops, edits or reorders a row, and it is not a legal test: it
    raises a visible warning for a human to settle with counsel. The thresholds and
    the entity-identifier list are POLICY (owner-specified for the sub-$100 signal),
    not law - see config/rulebook.json::spreadsheet_intake.consumer_debt_scan.
    """
    cfg = config.consumer_debt_scan_cfg()
    threshold_cents = int(cfg["sub_100_balance_threshold_cents"])
    total = len(records)
    individual_rows: List[int] = []
    small_rows: List[int] = []
    both_rows: List[int] = []
    for position, record in enumerate(records, start=1):
        name = record.get("name")
        amount = record.get("balance_owed")
        is_individual = looks_individual(name)
        is_small = False
        if amount is not None:
            is_small = 0 < round(float(amount) * 100) < threshold_cents
        if is_individual:
            individual_rows.append(position)
        if is_small:
            small_rows.append(position)
        if is_individual and is_small:
            both_rows.append(position)
    signals: List[str] = []
    if total:
        if len(individual_rows) / total >= float(cfg["individual_name_share_threshold"]):
            signals.append("individual_name_share")
        if len(small_rows) / total >= float(cfg["sub_100_balance_share_threshold"]):
            signals.append("sub_100_balance_share")
    if both_rows:
        signals.append("individual_name_with_sub_100_balance")
    return {
        "consumer_debt_marker": bool(signals),
        "signals": signals,
        "rows_with_individual_name": individual_rows,
        "rows_under_100": small_rows,
        "rows_individual_and_under_100": both_rows,
        "measured_on_rows": total,
        "warning": cfg["warning_text"] if signals else None,
        "policy_note": cfg["policy_note"],
        "blocks_nothing": True,
    }


# --- per-row validation -----------------------------------------------------
def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def validate_row(raw: Dict[str, Any], row_index: int, index: Dict[str, str]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[str]]:
    """(target values, drop record, flags) for one source row.

    The FIRST failing check names the drop, in the rulebook's documented order:
    MISSING_DEBTOR_ID -> AMOUNT_NOT_NUMERIC -> MISSING_BALANCE -> UNPARSEABLE_DATE ->
    BLOCKED_NO_BALANCE_DUE. A row never carries two reasons and nothing is coerced.
    """
    normalized: Dict[str, Any] = {}
    for source_column, value in raw.items():
        target = index.get(_key(source_column))
        if target and (target not in normalized or normalized[target] in (None, "")):
            normalized[target] = value  # first non-empty alias wins, as everywhere else

    def drop(token: str, column: Optional[str], value: Any) -> Tuple[None, Dict[str, Any], List[str]]:
        return None, {"row_index": row_index, "reason_token": token, "column": column, "value": value}, []

    if not _text(normalized.get("debtor_id")):
        return drop(DROP_MISSING_DEBTOR_ID, "debtor_id", normalized.get("debtor_id"))

    balance_raw = normalized.get("balance_owed")
    amount, why = parse_money_strict(balance_raw)
    if why == "not_numeric":
        return drop(DROP_AMOUNT_NOT_NUMERIC, "balance_owed", balance_raw)
    if why == "missing":
        return drop(DROP_MISSING_BALANCE, "balance_owed", balance_raw)

    invoice_date = None
    if _text(normalized.get("invoice_date")):
        invoice_date = parse_date(normalized["invoice_date"])
        if invoice_date is None:
            return drop(DROP_UNPARSEABLE_DATE, "invoice_date", normalized.get("invoice_date"))

    if amount is None or amount <= 0:
        return drop(DROP_BLOCKED_NO_BALANCE, "balance_owed", balance_raw)

    flags: List[str] = []
    if not _text(normalized.get("phone")):
        flags.append(FLAG_MISSING_PHONE)
    if not _text(normalized.get("email")):
        flags.append(FLAG_MISSING_EMAIL)
    if invoice_date is None:
        flags.append(FLAG_MISSING_INVOICE_DATE)
    return normalized, None, flags


# --- PREVIEW ----------------------------------------------------------------
def _fingerprint_payload(*, file_sha: str, client_id: str, source_format: str, mapping: Any,
                         totals: Dict[str, int], dropped: Sequence[Dict[str, Any]],
                         accepted: Sequence[Dict[str, Any]], consumer_debt: Dict[str, Any]) -> Dict[str, Any]:
    """Exactly the state COMMIT checks a record against. Deliberately excludes the
    clock: a fingerprint that moved with the wall clock could not prove no-drift."""
    mapping_rows = (mapping or {}).get("rows") or []
    return {
        "file_sha256": file_sha,
        "client_id": client_id,
        "source_format": source_format,
        "mapping": [{"source_column": row.get("source_column"), "target_field": row.get("target_field")}
                    for row in mapping_rows],
        "totals": dict(totals or {}),
        "dropped": [{"row_index": d.get("row_index"), "reason_token": d.get("reason_token")}
                    for d in dropped],
        "accepted": [{"row_index": a.get("row_index"), "raw": a.get("raw")} for a in accepted],
        "consumer_debt": {"consumer_debt_marker": bool((consumer_debt or {}).get("consumer_debt_marker")),
                          "signals": list((consumer_debt or {}).get("signals") or [])},
    }


def recompute_preview_fingerprint(preview: Dict[str, Any]) -> str:
    """The fingerprint of the preview payload IN FRONT OF US, recomputed from its own
    contents. COMMIT compares this with the fingerprint the payload claims and with the
    one on the batch record: a payload whose rows were edited after it was previewed
    cannot carry a fingerprint that matches all three."""
    return sha256_canonical(_fingerprint_payload(
        file_sha=str(preview.get("file_sha256") or ""),
        client_id=str(preview.get("client_id") or ""),
        source_format=str(preview.get("source_format") or ""),
        mapping=preview.get("mapping") or {},
        totals=preview.get("totals") or {},
        dropped=preview.get("dropped") or [],
        accepted=preview.get("accepted_rows") or [],
        consumer_debt=preview.get("consumer_debt") or {}))


def _normalized_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """The compact, frontend-ready view of one normalized row."""
    return {key: record.get(key) for key in
            ("debtor_id", "client_id", "name", "email", "phone", "balance_owed",
             "invoice_date", "aging_state", "invoice_number", "age_days", "aging_note",
             "area_code", "tz_name", "derived_local_time", "window_decision")}


def preview_spreadsheet(*, file_name: str, content: Any, client_id: str,
                        uploader_id: str, source_format: Optional[str] = None) -> Dict[str, Any]:
    """Parse -> translate -> validate. WRITES NOTHING: no debtor_records row, no queue
    entry, no ingestion_batches record. Safe to call as often as the console likes."""
    if not str(client_id or "").strip():
        raise ValueError("client_id is required: a file upload belongs to a client")
    if not str(uploader_id or "").strip():
        raise ValueError("uploader_id is required: an upload is a data-consent event")
    file_sha = file_sha256(content)
    try:
        table = read_table(file_name, content, source_format)
    except ValueError as exc:
        token = REFUSAL_UNSUPPORTED_FILE if REFUSAL_UNSUPPORTED_FILE in str(exc) else None
        payload = _refusal(token or "FILE_UNREADABLE", file_name=file_name, file_sha256=file_sha,
                           detail=str(exc))
        payload.update({
            "preview_ok": False,
            "refusal_token": token or "FILE_UNREADABLE",
            "action": "preview",
            "client_id": client_id,
            "uploader_id": uploader_id,
            "source_format": source_format,
            "headers": [],
            "mapping": {"rows": [], "mapped_count": 0, "unmapped_columns": [],
                        "unmapped_token": None, "duplicate_targets": [], "mapping_ok": False,
                        "mapping_error_token": None, "target_fields": list(TARGET_FIELDS)},
            "rows": [], "accepted_rows": [], "dropped": [],
            "totals": {"rows_seen": 0, "rows_accepted": 0, "rows_rejected": 0},
            "consumer_debt": None,
            "preview_fingerprint": None,
            "writes": 0,
            "plain_english": f"{file_name} could not be read: {exc}",
        })
        return payload

    index = _alias_index()
    sample = table.rows[0] if table.rows else None
    mapping = build_mapping(table.headers, sample)

    accepted: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    row_status: List[Dict[str, Any]] = []
    for row_index, row in enumerate(table.rows, start=1):
        shape = row_shape_problem(row, len(table.headers))
        if shape:
            dropped.append({"row_index": row_index, "reason_token": DROP_ROW_SHAPE,
                            "column": None, "value": shape})
            row_status.append({"row_index": row_index, "status": "dropped",
                               "reason_token": DROP_ROW_SHAPE, "flags": []})
            continue
        raw = table.raw_row(row)
        normalized, drop, flags = validate_row(raw, row_index, index)
        if drop:
            dropped.append(drop)
            row_status.append({"row_index": row_index, "status": "dropped",
                               "reason_token": drop["reason_token"], "flags": []})
        else:
            accepted.append({"row_index": row_index, "raw": raw, "flags": flags})
            row_status.append({"row_index": row_index, "status": "accepted",
                               "reason_token": None, "flags": flags})

    source_system = f"{SPREADSHEET_SOURCE_LABEL} ({table.source_format.upper()}: {file_name})"
    result = normalize([a["raw"] for a in accepted], source_system=source_system,
                       endpoint=f"{BATCH_SOURCE_REF_PREFIX}pending", client_id_hint=str(client_id))
    # An internal invariant, not a user-facing refusal: validate_row has already applied
    # the two rules normalize() can reject on (a debtor identifier and a client id), so a
    # row accepted here is a row normalize() accepts. If that ever stops being true the
    # preview must not quietly promise rows the commit would not write.
    if result.rejected:
        raise RuntimeError(
            "internal invariant broken: the spreadsheet validator accepted a row the shared "
            f"normalizer rejected: {result.rejected[:1]}")

    consumer_debt = scan_consumer_debt(result.records)
    normalized_by_row = {a["row_index"]: rec for a, rec in zip(accepted, result.records)}
    rows_view = [dict(status) for status in row_status]
    rows_view.sort(key=lambda item: item["row_index"])
    for item in rows_view:
        if item["status"] == "accepted":
            item["normalized"] = _normalized_view(normalized_by_row[item["row_index"]])

    totals = {"rows_seen": len(table.rows), "rows_accepted": len(accepted), "rows_rejected": len(dropped)}
    fingerprint = sha256_canonical(_fingerprint_payload(
        file_sha=file_sha, client_id=str(client_id), source_format=table.source_format,
        mapping=mapping, totals=totals, dropped=dropped, accepted=accepted, consumer_debt=consumer_debt))

    return {
        "preview_ok": bool(mapping["mapping_ok"]),
        "refusal_token": None if mapping["mapping_ok"] else REFUSAL_MAPPING_ERROR,
        "action": "preview",
        "file_name": file_name,
        "file_sha256": file_sha,
        "source_format": table.source_format,
        "client_id": str(client_id),
        "uploader_id": str(uploader_id),
        "parsed_at_ms": clock.iso_ms(),
        "parsed_at": clock.iso(),
        "headers": list(table.headers),
        "mapping": mapping,
        "rows": rows_view,
        "accepted_rows": [{"row_index": a["row_index"], "raw": a["raw"]} for a in accepted],
        "dropped": dropped,
        "totals": totals,
        "consumer_debt": consumer_debt,
        "preview_fingerprint": fingerprint,
        "writes": 0,
        "plain_english": (
            f"{totals['rows_accepted']} of {totals['rows_seen']} rows are importable from "
            f"{file_name} ({mapping['mapped_count']} source columns translated, "
            f"{len(mapping['unmapped_columns'])} unmapped, {totals['rows_rejected']} rejected)"),
    }


# --- the immutable batch record ---------------------------------------------
#: The fields batch_sha256 covers. Every one is coerced by `_batch_content()`, so the
#: hash recomputes identically from the insert and from a row read back out of the table.
#:
#: TWO field sets, on purpose. `BATCH_HASH_FIELDS_LEGACY` is exactly the set a batch row
#: was hashed over before the real Step 3 gate existed (Phase 3). `BATCH_HASH_FIELDS`
#: adds the Step 3 attestation id. A row whose attestation id is empty is a LEGACY row:
#: it is verified against the legacy set, because this table is append-only history and
#: re-hashing an already-committed batch would be rewriting it. Every NEW row carries a
#: non-empty attestation id and is hashed over the full set, so the gate's own evidence
#: is inside the hash that a later commit re-checks.
BATCH_HASH_FIELDS_LEGACY = (
    "client_id", "uploader_id", "file_name", "file_sha256", "source_format",
    "rows_seen", "rows_accepted", "rows_rejected", "preview_fingerprint",
    "mapping_json", "rejected_json", "consumer_debt_marker", "consumer_debt_signals",
    "warranty_acknowledged", "warranty_signature_reference", "warranty_document_version",
    "warranty_text_sha256", "warranty_binding_sha256", "recorded_at_ms", "recorded_by",
    "reason", "note",
)
BATCH_HASH_FIELDS = BATCH_HASH_FIELDS_LEGACY + ("warranty_step3_attestation_id",)


def batch_hash_fields_for(row: Dict[str, Any]) -> tuple:
    """Which field set this row was hashed over: legacy (no Step 3 binding) or current."""
    if str(row.get("warranty_step3_attestation_id") or ""):
        return BATCH_HASH_FIELDS
    return BATCH_HASH_FIELDS_LEGACY


def _batch_content(row: Dict[str, Any], fields=None) -> Dict[str, Any]:
    """One canonical shape for the hashed fields, whatever the source (a fresh dict or a
    row read back from SQLite, where booleans are integers and NULL is None)."""
    content: Dict[str, Any] = {}
    for field_name in (fields or batch_hash_fields_for(row)):
        value = row.get(field_name)
        if field_name in ("rows_seen", "rows_accepted", "rows_rejected",
                          "consumer_debt_marker", "warranty_acknowledged"):
            content[field_name] = int(value or 0)
        else:
            content[field_name] = "" if value is None else str(value)
    return content


def batch_sha256(row: Dict[str, Any], fields=None) -> str:
    return sha256_canonical(_batch_content(row, fields))


def warranty_binding(*, client_id: str, signature_reference: str, document_version: str,
                     text_sha256: str, uploader_id: str, acknowledged_at_ms: str,
                     step3_attestation_id: str = "",
                     step3_attestation_sha256: str = "") -> str:
    """The cryptographic binding between a batch and the client's Step 3 warranty.

    Six things were always inside this hash: the client, the legal sign-off reference, the
    version of the wording, the sha256 of that exact wording, who acknowledged it and when.
    Phase 4 adds the two facts that make it the REAL gate: the id of the client's recorded
    Step 3 attestation row and the sha256 of the attestation record itself. A batch can no
    longer cite a rulebook string - it cites the row a human ticked.
    """
    payload = {
        "client_id": client_id,
        "warranty_signature_reference": signature_reference,
        "warranty_document_version": document_version,
        "warranty_text_sha256": text_sha256,
        "uploader_id": uploader_id,
        "acknowledged_at_ms": acknowledged_at_ms,
    }
    if step3_attestation_id:
        payload["warranty_step3_attestation_id"] = step3_attestation_id
        payload["warranty_step3_attestation_sha256"] = step3_attestation_sha256
    return sha256_canonical(payload)


def _chain_hash(prev_chain_hash: str, batch_sha: str, batch_id: str) -> str:
    return sha256_text("|".join([prev_chain_hash, batch_sha, batch_id]))


def register_ingestion_batch(store, preview: Dict[str, Any], *, uploader_id: str,
                             warranty_acknowledged: bool, who: Optional[str] = None,
                             reason: str = "", note: Optional[str] = None,
                             audit=None) -> Dict[str, Any]:
    """Append the immutable `ingestion_batches` row a COMMIT will be checked against.

    Refuses, with one token, when: the preview itself is not importable
    (`BLOCKED_MAPPING_ERROR`), the Step 3 warranty checkbox was not explicitly ticked
    (`BLOCKED_WARRANTY_NOT_ACKNOWLEDGED`), the client has no valid signed authorization
    to bind the warranty to (`BLOCKED_NO_WARRANTY_SIGNATURE`), or - Phase 4, the real
    gate - the client has no recorded Step 3 attestation for the warranty to bind to
    (`BLOCKED_NO_STEP3_ATTESTATION`). A batch registered before its client attested is
    refused; batches committed before this gate existed stay valid, because
    ingestion_batches is append-only history.
    """
    if not preview.get("preview_ok"):
        return _refusal(preview.get("refusal_token") or REFUSAL_MAPPING_ERROR,
                        file_name=preview.get("file_name"))
    if not warranty_acknowledged:
        return _refusal(REFUSAL_WARRANTY_NOT_ACKNOWLEDGED,
                        detail="the Step 3 warranty checkbox must be explicitly TRUE before a "
                               "file can be registered for import; it is unselected by default",
                        file_name=preview.get("file_name"))
    if str(preview.get("preview_fingerprint") or "") != recompute_preview_fingerprint(preview):
        return _refusal(REFUSAL_PREVIEW_STATE_CHANGED, file_name=preview.get("file_name"),
                        detail="the preview does not match its own fingerprint: its rows, mapping "
                               "or counts were changed after it was produced, so it cannot be "
                               "registered for import")
    client_id = str(preview.get("client_id") or "").strip()
    settings = onboarding.fetch_settings(store, client_id)
    state = onboarding.signoff_state(settings) if settings else {"valid": False,
                                                                "reason": "no autonomy settings row"}
    if not state["valid"]:
        return _refusal(REFUSAL_NO_WARRANTY_SIGNATURE, client_id=client_id,
                        detail=f"there is no valid signed authorization to bind this batch's "
                               f"warranty to: {state['reason']}")
    # Phase 4: the real Step 3 gate. The warranty a batch binds to is the client's own
    # recorded, versioned attestation row - not a hash of a string in the rulebook. A
    # client that has not attested has nothing for a batch to bind to, so this refuses.
    attestation = onboarding.step3_gate(store, client_id)
    if attestation is None:
        return _refusal(REFUSAL_NO_STEP3_ATTESTATION, client_id=client_id,
                        file_name=preview.get("file_name"),
                        detail="there is no recorded Step 3 warranty attestation for this client: "
                               "the backend refuses to register a batch against an unwarranted "
                               "file. Record the Step 3 attestation first; the exact checkbox "
                               "text and its sha256 are then stored against the sign-off.")
    uploader = str(uploader_id or "").strip()
    if not uploader:
        raise ValueError("uploader_id is required: an ingestion batch names who handed over the file")

    warranty = config.step3_warranty()
    acknowledged_at_ms = clock.iso_ms()
    text_sha = str(attestation["text_sha256"])
    attestation_id = str(attestation["attestation_id"])
    attestation_sha = str(attestation["attestation_sha256"])
    binding = warranty_binding(client_id=client_id,
                               signature_reference=str(settings["signature_reference"]),
                               document_version=str(warranty["version"]),
                               text_sha256=text_sha, uploader_id=uploader,
                               acknowledged_at_ms=acknowledged_at_ms,
                               step3_attestation_id=attestation_id,
                               step3_attestation_sha256=attestation_sha)
    mapping = preview.get("mapping") or {"rows": []}
    content: Dict[str, Any] = {
        "client_id": client_id,
        "uploader_id": uploader,
        "file_name": str(preview.get("file_name") or ""),
        "file_sha256": str(preview.get("file_sha256") or ""),
        "source_format": str(preview.get("source_format") or ""),
        "rows_seen": int(preview["totals"]["rows_seen"]),
        "rows_accepted": int(preview["totals"]["rows_accepted"]),
        "rows_rejected": int(preview["totals"]["rows_rejected"]),
        "preview_fingerprint": str(preview.get("preview_fingerprint") or ""),
        "mapping_json": canonical_json([{"source_column": r["source_column"],
                                         "target_field": r["target_field"]}
                                        for r in mapping["rows"]]),
        "rejected_json": canonical_json([{"row_index": d["row_index"], "reason_token": d["reason_token"]}
                                         for d in preview.get("dropped", [])]),
        "consumer_debt_marker": int(bool((preview.get("consumer_debt") or {}).get("consumer_debt_marker"))),
        "consumer_debt_signals": canonical_json(list((preview.get("consumer_debt") or {}).get("signals") or [])),
        "warranty_acknowledged": 1,
        "warranty_signature_reference": str(settings["signature_reference"]),
        "warranty_document_version": str(warranty["version"]),
        "warranty_text_sha256": text_sha,
        "warranty_binding_sha256": binding,
        # Phase 4: the real Step 3 gate's own evidence, inside the hash above
        "warranty_step3_attestation_id": attestation_id,
        "warranty_step3_attestation_sha256": attestation_sha,
        "recorded_at_ms": acknowledged_at_ms,
        "recorded_at": clock.iso(),
        "recorded_by": str(who or uploader),
        "reason": str(reason or ""),
        "note": "" if note is None else str(note),
    }
    batch_id = uuid.uuid4().hex
    previous = store.query_one("SELECT chain_hash FROM ingestion_batches ORDER BY seq DESC LIMIT 1")
    prev_chain = str(previous["chain_hash"]) if previous else GENESIS_BATCH_CHAIN_HASH
    batch_sha = batch_sha256(content)
    chain = _chain_hash(prev_chain, batch_sha, batch_id)

    with store.tx(immediate=True):
        store.execute(
            """INSERT INTO ingestion_batches
                 (batch_id, client_id, uploader_id, file_name, file_sha256, source_format,
                  rows_seen, rows_accepted, rows_rejected, preview_fingerprint, mapping_json,
                  rejected_json, consumer_debt_marker, consumer_debt_signals, recorded_at_ms,
                  recorded_at, warranty_acknowledged, warranty_signature_reference,
                  warranty_document_version, warranty_text_sha256, warranty_binding_sha256,
                  warranty_step3_attestation_id, warranty_step3_attestation_sha256,
                  batch_sha256, prev_chain_hash, chain_hash, recorded_by, reason, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, content["client_id"], content["uploader_id"], content["file_name"],
             content["file_sha256"], content["source_format"], content["rows_seen"],
             content["rows_accepted"], content["rows_rejected"], content["preview_fingerprint"],
             content["mapping_json"], content["rejected_json"], content["consumer_debt_marker"],
             content["consumer_debt_signals"], content["recorded_at_ms"], content["recorded_at"],
             content["warranty_acknowledged"], content["warranty_signature_reference"],
             content["warranty_document_version"], content["warranty_text_sha256"],
             content["warranty_binding_sha256"], content["warranty_step3_attestation_id"],
             content["warranty_step3_attestation_sha256"],
             batch_sha, prev_chain, chain,
             content["recorded_by"], content["reason"], content["note"]),
        )
    record = store.query_one("SELECT * FROM ingestion_batches WHERE batch_id = ?", (batch_id,))
    if audit:
        audit.record("ingestion_batch_registered", batch_id=batch_id, client_id=client_id,
                     uploader_id=uploader, file_name=content["file_name"],
                     file_sha256=content["file_sha256"], source_format=content["source_format"],
                     rows_seen=content["rows_seen"], rows_accepted=content["rows_accepted"],
                     rows_rejected=content["rows_rejected"],
                     preview_fingerprint=content["preview_fingerprint"],
                     consumer_debt_marker=content["consumer_debt_marker"],
                     consumer_debt_signals=content["consumer_debt_signals"],
                     warranty_document_version=content["warranty_document_version"],
                     warranty_step3_attestation_id=content["warranty_step3_attestation_id"],
                     warranty_step3_attestation_sha256=content["warranty_step3_attestation_sha256"],
                     warranty_binding_sha256=binding, batch_sha256=batch_sha, chain_hash=chain)
    out = {"ok": True, "batch_id": batch_id, "batch_sha256": batch_sha, "prev_chain_hash": prev_chain,
           "chain_hash": chain, "warranty_binding_sha256": binding,
           "warranty_step3_attestation_id": attestation_id,
           "warranty_step3_attestation_sha256": attestation_sha, "record": record}
    return out


def verify_ingestion_batches(store) -> Dict[str, Any]:
    """Recompute the whole chain of record from its own rows: tamper-evidence, in one call.

    A hash proves the content has not changed and where it sits in the sequence. It does
    NOT prove who recorded it, and it is not third-party attestation.
    """
    rows = store.query("SELECT * FROM ingestion_batches ORDER BY seq")
    breaks: List[Dict[str, Any]] = []
    previous = GENESIS_BATCH_CHAIN_HASH
    legacy = 0
    for row in rows:
        # A row appended before the real Step 3 gate existed carries an empty attestation
        # id and is verified over the legacy field set; a row appended since carries the
        # gate's evidence inside its hash. Both are reported, so nobody has to guess which
        # shape a given row is.
        if not str(row.get("warranty_step3_attestation_id") or ""):
            legacy += 1
        recomputed = batch_sha256(row)
        if recomputed != row["batch_sha256"]:
            breaks.append({"seq": row["seq"], "batch_id": row["batch_id"],
                           "problem": "batch_sha256 does not match the row's own content: the "
                                      "record changed after it was appended"})
        if row["prev_chain_hash"] != previous:
            breaks.append({"seq": row["seq"], "batch_id": row["batch_id"],
                           "problem": "prev_chain_hash does not link to the previous record"})
        expected = _chain_hash(row["prev_chain_hash"], row["batch_sha256"], row["batch_id"])
        if expected != row["chain_hash"]:
            breaks.append({"seq": row["seq"], "batch_id": row["batch_id"],
                           "problem": "chain_hash is not sha256(prev_chain_hash | batch_sha256 | batch_id)"})
        previous = row["chain_hash"]
    return {"ok": not breaks, "count": len(rows), "genesis_chain_hash": GENESIS_BATCH_CHAIN_HASH,
            "legacy_rows": legacy, "step3_gated_rows": len(rows) - legacy,
            "hash_field_sets": {
                "legacy": list(BATCH_HASH_FIELDS_LEGACY),
                "current": list(BATCH_HASH_FIELDS)},
            "breaks": breaks}


def _batch_chain_ok(store, record: Dict[str, Any]) -> bool:
    """Is this ONE stored batch row intact and correctly linked to its predecessor?"""
    if batch_sha256(record) != record["batch_sha256"]:
        return False
    expected_chain = _chain_hash(record["prev_chain_hash"], record["batch_sha256"], record["batch_id"])
    if expected_chain != record["chain_hash"]:
        return False
    previous = store.query_one(
        "SELECT chain_hash FROM ingestion_batches WHERE seq < ? ORDER BY seq DESC LIMIT 1",
        (record["seq"],))
    expected_prev = str(previous["chain_hash"]) if previous else GENESIS_BATCH_CHAIN_HASH
    return record["prev_chain_hash"] == expected_prev


# --- COMMIT -----------------------------------------------------------------
def commit_spreadsheet(store, preview: Dict[str, Any], *, batch_id: str,
                       uploader_id: Optional[str] = None, who: Optional[str] = None,
                       audit=None) -> Dict[str, Any]:
    """Import a previewed file. The ONLY path from an upload into the working queue.

    Refusals, one token each, and no partial import in any of them:
      * `BLOCKED_NO_INGESTION_BATCH`     - no record for this batch id (the record is a
        precondition, so a commit cannot happen without one)
      * `BATCH_RECORD_TAMPERED`          - the record exists but its hash or chain link
        does not recompute: the row changed after it was appended
      * `BATCH_PREVIEW_STATE_CHANGED`    - the record does not match the preview in front
        of us: file sha, client, format, counts, fingerprint or uploader (drift)
      * `BATCH_ALREADY_COMMITTED`        - rows from this batch are already in the queue
      * `BLOCKED_MAPPING_ERROR`          - the preview was not importable in the first place

    Committed rows are normalized and written by `normalize()` + `persist()` - the same
    two calls a live vendor export goes through.
    """
    if not preview.get("preview_ok"):
        return _refusal(preview.get("refusal_token") or REFUSAL_MAPPING_ERROR,
                        file_name=preview.get("file_name"))
    record = store.query_one("SELECT * FROM ingestion_batches WHERE batch_id = ?", (str(batch_id),))
    if not record:
        return _refusal(REFUSAL_NO_BATCH, batch_id=str(batch_id),
                        detail="no ingestion_batches record exists for this batch; register the "
                               "previewed file first - a commit is never the thing that creates it")
    if not _batch_chain_ok(store, record):
        return _refusal(REFUSAL_BATCH_TAMPERED, batch_id=str(batch_id),
                        detail="the stored batch record no longer hashes to its recorded "
                               "batch_sha256/chain_hash: it was altered after it was appended")
    totals = preview.get("totals") or {}
    handed_fingerprint = str(preview.get("preview_fingerprint") or "")
    if handed_fingerprint != recompute_preview_fingerprint(preview):
        return _refusal(REFUSAL_PREVIEW_STATE_CHANGED, batch_id=str(batch_id),
                        detail="the preview handed to COMMIT no longer matches its own stored "
                               "fingerprint: its rows, mapping or counts were changed after it "
                               "was previewed")
    if handed_fingerprint != str(record["preview_fingerprint"]):
        return _refusal(REFUSAL_PREVIEW_STATE_CHANGED, batch_id=str(batch_id),
                        detail="the preview handed to COMMIT is not the preview the batch record "
                               "was raised from")
    comparators = (
        ("file_sha256", str(preview.get("file_sha256") or ""), str(record["file_sha256"])),
        ("client_id", str(preview.get("client_id") or ""), str(record["client_id"])),
        ("source_format", str(preview.get("source_format") or ""), str(record["source_format"])),
        ("rows_seen", int(totals.get("rows_seen", -1)), int(record["rows_seen"])),
        ("rows_accepted", int(totals.get("rows_accepted", -1)), int(record["rows_accepted"])),
        ("rows_rejected", int(totals.get("rows_rejected", -1)), int(record["rows_rejected"])),
        ("uploader_id", str(uploader_id), str(record["uploader_id"])) if uploader_id else None,
    )
    for name, handed, stored in [c for c in comparators if c]:
        if handed != stored:
            return _refusal(REFUSAL_PREVIEW_STATE_CHANGED, batch_id=str(batch_id),
                            field=name, handed=handed, stored=stored,
                            detail=f"the {name} in front of us does not match the batch record")

    accepted_rows = preview.get("accepted_rows") or []
    dropped = preview.get("dropped") or []
    if int(totals.get("rows_seen", -1)) != len(accepted_rows) + len(dropped):
        return _refusal(REFUSAL_PREVIEW_STATE_CHANGED, batch_id=str(batch_id),
                        detail="the preview's own counts do not add up (seen != accepted + rejected)")

    existing = store.query_one(
        "SELECT debtor_id FROM debtor_records WHERE source_ref = ? LIMIT 1",
        (f"{BATCH_SOURCE_REF_PREFIX}{batch_id}",))
    if existing:
        return _refusal(REFUSAL_BATCH_ALREADY_COMMITTED, batch_id=str(batch_id),
                        first_debtor=str(existing["debtor_id"]),
                        detail="rows from this batch are already in the working queue; a batch "
                               "commits once")

    source_ref = f"{BATCH_SOURCE_REF_PREFIX}{batch_id}"
    source_system = f"{SPREADSHEET_SOURCE_LABEL} ({str(preview.get('source_format')).upper()}: {preview.get('file_name')})"
    result = normalize([a["raw"] for a in accepted_rows], source_system=source_system,
                       endpoint=source_ref, client_id_hint=str(preview.get("client_id")))
    if result.normalized_count != int(totals.get("rows_accepted", -1)):
        return _refusal(REFUSAL_PREVIEW_STATE_CHANGED, batch_id=str(batch_id),
                        detail=f"re-running the shared normalizer produced {result.normalized_count} "
                               f"rows for {totals.get('rows_accepted')} accepted rows: refusing "
                               f"rather than importing something other than the preview")
    written = persist(store, result)
    committed_ids = [r["debtor_id"] for r in result.records]
    if audit:
        audit.record("spreadsheet_committed", batch_id=str(batch_id),
                     client_id=str(preview.get("client_id")), uploader_id=str(record["uploader_id"]),
                     file_name=str(record["file_name"]), file_sha256=str(record["file_sha256"]),
                     source_format=str(record["source_format"]), source_ref=source_ref,
                     rows_seen=int(record["rows_seen"]), rows_accepted=int(record["rows_accepted"]),
                     rows_rejected=int(record["rows_rejected"]), rows_written=written,
                     debtor_ids=committed_ids, preview_fingerprint=str(record["preview_fingerprint"]),
                     chain_hash=str(record["chain_hash"]))
    return {
        "ok": True,
        "batch_id": str(batch_id),
        "source_ref": source_ref,
        "source_system": source_system,
        "rows_written": written,
        "rows_seen": int(record["rows_seen"]),
        "rows_accepted": int(record["rows_accepted"]),
        "rows_rejected": int(record["rows_rejected"]),
        "committed_debtor_ids": committed_ids,
        "dropped": list(dropped),
        "consumer_debt": scan_consumer_debt(result.records),
        "preview_fingerprint": str(record["preview_fingerprint"]),
        "chain_hash": str(record["chain_hash"]),
        "plain_english": (f"{written} rows imported from {record['file_name']} through batch "
                          f"{str(batch_id)[:12]} ({int(record['rows_rejected'])} rows dropped and "
                          f"reported)"),
    }
