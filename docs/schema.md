# Store schema

Thirteen tables. `engine/schema.sql` is the file that creates them; this document explains
each column. `engine.app.TABLES` holds the same list in code, and
`tests/test_gate.py::TestVocabulary` fails if the store, that tuple and this document
ever disagree — which is how a table stops being invisible to the demo's exporter. SQLite is the default; the same SQL shape runs on Postgres (see the end).

Every timestamp is TEXT, ISO-8601 UTC with a trailing `Z`. Every boolean is INTEGER
`0`/`1`. List-valued columns hold a JSON array in TEXT. That is what makes one code path
serve both engines.

## `client_autonomy_settings`

One row per client. **This row is the single point of contention**: a mode flip updates
it, a send claim updates `claim_seq` on it, so the two can never interleave.

| Column | Meaning |
| --- | --- |
| `client_id` | Primary key. |
| `operating_mode` | `hitl` (default) or `full_auto`. CHECK-constrained. |
| `has_valid_signoff` | 1 while a signed authorization is on file and neither revoked nor expired. |
| `signer_name` | Typed signer name from the digital sign-off. |
| `signer_title` | Typed corporate title. |
| `signature_reference` | `sha256:` + sha256(`client_id` + `signer_name` + `signer_title` + document version + timestamp). Every automated contact is linked back to this. |
| `authorized_channels` | JSON array, e.g. `["email","voice"]`. |
| `authorized_aging_states` | JSON array, e.g. `["30","60","90"]`. |
| `signed_timestamp` | When the sign-off was captured. |
| `authorization_document_version` | Version of the authorization wording that was signed (e.g. `AUT-1.0`). |
| `signature_revoked`, `signature_revoked_at` | Revocation state. Revoking drops a `full_auto` client to `hitl`. |
| `signoff_expires_at` | `signed_timestamp` + rulebook validity days. Expiry drops a `full_auto` client to `hitl` on the next sweep. |
| `mode_version` | Increments on every real mode change. A claim records the version it read, which is how the race guarantee is checked. |
| `claim_seq`, `last_claim_at` | Incremented/updated by every committed claim: the claim's write to the contended row. |
| `updated_at` | Last settings update. |
| `lifecycle_status` | `STATUS_BETA_TESTER` (the DEFAULT for every new profile) or `STATUS_FULL_ACTIVE`. CHECK-constrained to those two. `STATUS_FULL_ACTIVE` is unreachable except through the gate: DB triggers refuse it unless all three milestone flags are `1` on the same row, and only the gate sets them. |
| `manual_review_threshold_passed` | Milestone 1. `1` only when at least 20 manual `hitl` approvals/rejections are recorded in `manual_review_decisions` **and** zero client-caused compliance exceptions exist. A guardrail correctly blocking a send is never an infraction (see that table). |
| `live_billing_verified` | Milestone 2 — the merchant-processing gate. The column alone satisfies nothing: `engine.gate.verified_live_billing()` re-derives the bound, chain-verified receipt on every read, so a hand-written `UPDATE` or a hardcoded literal cannot open the gate. |
| `commercial_msa_executed` | Milestone 3. A fail-closed stub: `engine.gate.set_milestone_flag` refuses to set it (`MILESTONE_NOT_IMPLEMENTED`), so no code path can satisfy it today. |
| `billing_route_demonstrated_in_sandbox` | The honestly-named sibling of Milestone 2. Set only by a receipt validated against the explicit SANDBOX pin; it never satisfies the live milestone. Displayed beside it, never in place of it. |
| `live_billing_receipt_metadata` | The bound receipt entry (JSON: receipt, its sha256, the `verification_id` and the chain hash). Write-once: a column-scoped trigger allows exactly one NULL→value write and refuses every later change or clear, so a client's bound evidence cannot be replaced under it. |

## `debtor_records`

One normalized past-due invoice (Agent 1's output).

| Column | Meaning |
| --- | --- |
| `debtor_id` | Primary key, from the best identifier column the export offered. |
| `client_id` | Owning client. |
| `name`, `email`, `phone` | Debtor contact details as given by the export. |
| `balance_owed` | Amount owed; parenthesised export values parse as negative (a credit). |
| `invoice_date` | ISO date, or NULL when the export was unreadable. |
| `aging_state` | `current` (0-29 days), `30`, `60`, `90`, or `unknown` / `future` for edge cases, derived from `invoice_date` against the run date. |
| `approval_status` | `Pending`, `Approved`, `Sent` or `Outside_Calling_Window`. CHECK-constrained: that is the whole vocabulary. |
| `dnc_flag` | 1 once the debtor opts out. Kills every current and future run. |
| `invoice_number` | Reference used in the notice. |
| `source_system`, `source_ref` | Which export the row came from, and the endpoint it was fetched from. |
| `source_columns` | JSON: the actual source column → target field pairs used for this row. |
| `raw_record` | JSON: the untouched source row, so a mapping decision can always be re-checked. |
| `aging_note` | Why the aging state is what it is — including the explicit edge cases. |
| `area_code`, `tz_name`, `derived_local_time` | The area-code/timezone derivation stored on the record. |
| `window_decision`, `window_decision_at` | The calling-window decision (`In_Window`, `Outside_Calling_Window`, `Unknown_Area_Code_Freeze`) and when it was taken. |
| `approved_by`, `approved_at` | The human who approved the draft in `hitl`. |
| `dnc_set_at` | When the opt-out arrived. |
| `imported_at` | When the row was normalized. |

## `autonomy_mode_history` — APPEND ONLY

| Column | Meaning |
| --- | --- |
| `history_id` | Auto increment. |
| `who` | Who or what made the change (`dana.whitmore@cedar-ridge (CFO)`, `system`). |
| `old_mode`, `new_mode` | The mode before and the mode actually in force after. A refused attempt to switch to `full_auto` is recorded with `new_mode` = the mode still in force and a reason beginning `REFUSED`: the timeline never claims a switch that did not happen. |
| `timestamp` | When. |
| `reason` | The plain-English reason supplied. |
| `mode_version` | The `mode_version` value after the change; used to check the claim/flip race. |
| `client_id` | Which client. |

Append-only is enforced by two SQLite triggers that `RAISE(ABORT)` on UPDATE and on
DELETE, so it holds for the engine's own code and for any future caller:
`tests/test_store.py::TestAppendOnlyTimeline` proves both are rejected.

## `outbound_contact_logs`

Every contact attempt — claimed, sent, failed, or blocked.

| Column | Meaning |
| --- | --- |
| `contact_log_id` | Auto increment. |
| `debtor_id`, `client_id` | Who the attempt was for. |
| `attempted_at` | When. |
| `contact_type` | `email`, `voice` or `sms`. |
| `destination` | The phone number or email address used. |
| `decision` | `CLAIMED`, or a `BLOCKED_*` reason (`BLOCKED_DNC`, `BLOCKED_OUTSIDE_WINDOW`, `BLOCKED_MODE_HITL`, `BLOCKED_NO_SIGNOFF`, `BLOCKED_SIGNOFF_INVALID`, `BLOCKED_OUT_OF_SCOPE`, `BLOCKED_NO_BALANCE_DUE`, `BLOCKED_PENDING_ADJUSTMENT`), or the one token that names a state rather than a mode: `REOPENED_AWAITING_AUDIT` (a reversed payment whose re-audit is not complete — see `reopen_audit_events`). |
| `status` | `CLAIMED`, `SENT`, `FAILED` or `BLOCKED`. |
| `reason_detail` | Plain-English explanation of the decision. |
| `operating_mode_at_claim` | The mode read **inside** the claim transaction. |
| `mode_version_at_claim` | The `mode_version` read in the same read, which is what makes the race guarantee checkable. |
| `approval_status_at_claim`, `dnc_flag_at_claim` | The debtor state read at claim time. |
| `window_decision`, `derived_local_time`, `area_code` | The window decision in force at claim time. |
| `signature_reference` | What authorized the contact: the automation sign-off in `full_auto`, or `human-approval:<who>@<when>` in `hitl`. Decided by **the mode in force for this claim**, read inside the claim transaction — never by "does a sign-off happen to exist on the client row". |
| `approved_by`, `approved_at` | The human approver and the millisecond-UTC instant of their approval (`hitl`); NULL in `full_auto`, where the authorization is the client's sign-off and there is no human approver to name. |
| `claim_token` | The committed claim's token. NULL on every blocked attempt: a blocked attempt writes no claim. |
| `claimed_at` | When the claim committed. |
| `payload_path` | The exact JSON POSTed (kept in `out/vapi_payloads/`). |
| `message_preview` | The first 280 characters of the rendered notice. |
| `provider_response` | The mock provider's response body. |

Two unique indexes back the claim: one on `claim_token` (partial, non-NULL only) and one
on `(debtor_id, claim_token)`.

## `payment_events` — APPEND ONLY

One row per inbound payment signal, whichever channel carried it, written inside the same
transaction as the balance change, the queue purge and the ledger line. This is the intake
half of "one payment event, three consequences", and the durable record of what was
cancelled and when.

| column | meaning |
|---|---|
| `confirmation_channel` | `accounting_webhook` / `delta_sync` / `manual_attestation` |
| `amount_cents` | the funds confirmed collected, integer cents |
| `attested_by` | who attested — required for `manual_attestation` |
| `external_reference` | the accounting system's payment id; used for idempotency |
| `signal_at` / `recorded_at` | when the source says it happened / when we recorded it |
| `confirmation_latency` | how far behind reality this channel can be (per line, never smoothed) |
| `balance_before_cents` / `balance_after_cents` / `overpayment_cents` | the exact balance move |
| `account_state_before` / `account_state_after` | the payment state the signal produced |
| `queue_purged_at`, `cancelled_runs` (JSON), `cancelled_run_count` | the destructive queue purge |
| `outbound_logs_untouched` | how many `outbound_contact_logs` rows existed: history is never purged |
| `intake_reference` | correlation handle shared with the ledger line it produced |

## `collections_ledger` — APPEND ONLY

The fee ledger. Nothing here is ever updated or deleted (triggers reject both, exactly as
for `autonomy_mode_history`). A reversal is a linked counter-entry, so the original row
stays byte-identical and the net statement value is `original + reversal`.

| column | meaning |
|---|---|
| `entry_type` | `collection` (positive amount, positive fee) or `reversal` (the exact negation) |
| `amount_cents`, `fee_cents` | integer cents; `fee_cents` is what the client is billed |
| `fee_basis_points`, `fee_rounding_rule` | the rate and rule that produced the fee (2000 bp = 20%, `half_up_to_cent`) |
| `confirmation_channel`, `confirmation_latency`, `attested_by` | the named confirmation source of the line |
| `linked_entry_id` | on a reversal: the collection it reverses (one reversal per collection, unique index) |
| `payment_event_id`, `intake_reference` | back-link to the intake event |
| `balance_before_cents` / `balance_after_cents`, `debtor_state_before` / `debtor_state_after` | the move it caused |

Enforced in the store, not only in Python: `CHECK ((entry_type = 'collection' AND
fee_cents = (abs(amount_cents) * fee_basis_points + 5000) / 10000) OR (entry_type =
'reversal' AND fee_cents = -((abs(amount_cents) * fee_basis_points + 5000) / 10000)))`
(integer division, half up), `confirmation_channel` NOT NULL and CHECKed, and a reversal
must carry `linked_entry_id`. There is no UPDATE path: links are written one way
(reversal → collection) and the read model resolves `reversed_by_entry_id`.

Read model: `engine.payments.ledger_for_client(store, client_id)` returns JSON-serialisable
lines plus net totals — real rows only, no estimates, no placeholder figures.

## `balance_edit_events` — APPEND ONLY

A balance change with no payment signal behind it parks the record in
`Pending_Adjustment_Confirmation` (no dunning, no fee, no send) and writes an
`edit_unconfirmed` row. The client's resolution is a second row:
`resolved_as_payment` (billable, `ledger_entry_id` set, `billable = 1`) or
`resolved_as_adjustment` (closed, `billable = 0`, no ledger line at all). An unconfirmed
adjustment is never billable — the team's standing default pending the owner's ruling.

## `reopen_audit_events` — APPEND ONLY

The reopen-audit invariant, one log with three event types (`event_type` is CHECK-constrained
to exactly these). A bounced or refunded payment is not a resume: it un-bills by counter-entry
and puts the record in `Reopened_Awaiting_Audit`, and the only way back into normal flow is a
completed re-audit recorded here.

| Column | Meaning |
| --- | --- |
| `event_type` | `reversal_opened_audit` (written by the reversal, in the same transaction that un-bills), `reaudit_completed`, `reaudit_refused`. |
| `collection_entry_id`, `reversal_entry_id`, `payment_event_id` | The links: the collection that was un-billed, the counter-entry that un-billed it, and the intake event behind the original payment. |
| `un_billed_amount_cents`, `un_billed_fee_cents` | What was actually credited back (the negation of the collection). |
| `balance_before_cents`, `balance_after_cents`, `account_state_before`, `account_state_after` | The move the reversal made and the state it produced. |
| `source_system`, `source_ref`, `renormalized`, `source_balance_cents`, `source_balance_matches_engine` | What the re-run of ingestion found over the stored source row. The source figure is **recorded, never applied**: a mismatch is flagged (`source_balance_matches_engine = 0`), because the ledger is the money record. |
| `contact_email_resolved`, `contact_phone_resolved` | Whether the source row resolved contact data. A missing address or number is recorded as missing, never invented. |
| `window_decision`, `derived_local_time`, `area_code`, `tz_name`, `dnc_flag`, `paid_status`, `ledger_balance_cents` | The re-evaluated calling window, the opt-out flag and the paid status/balance the record re-entered with. |
| `approval_status_after` | `Pending`: any approval predates the reversal, so in `hitl` a fresh human review is required against the re-read record. |
| `hold_state_kept` | `1` when the record was already in a hold (`Pending_Adjustment_Confirmation`, `Dispute_Hold`) and the audit left it there. |
| `refuse_reason` | Why a re-audit was refused. On a refusal **nothing moves**: the record stays where it is and stays un-claimable (fail-closed: no source row, an unparseable source, a source that no longer identifies the same debtor, an unparseable amount). |
| `performed_by`, `reason`, `recorded_at`, `note` | Who completed or refused it, why, and when — millisecond UTC. An audit must be attributed or it is refused. |

**An open audit** is a `reversal_opened_audit` row with no `reaudit_completed` row for the same
reversal. That question is asked inside the send-claim transaction (see `engine/payments.py::open_reopen_audit`),
so a record with an open audit cannot be contacted — not in any mode, and not after another hold
on the record was resolved by a human. Append-only via UPDATE/DELETE triggers, exactly as for
`autonomy_mode_history`.

## `debtor_records.account_state`

The payment lifecycle, a separate axis from the human `approval_status` gate:
`Open`, `Partially_Paid`, `Paid`, `Pending_Adjustment_Confirmation`,
`Reopened_Awaiting_Audit` — that CHECK list is the whole vocabulary. The send claim
re-reads it, so a paid debtor is blocked with `BLOCKED_NO_BALANCE_DUE` and a parked one
with `BLOCKED_PENDING_ADJUSTMENT` — in every mode.

`Reopened_Awaiting_Audit` is set by a reversal on a record that was `Paid` or
`Partially_Paid`, and it is **mandatory, not a flag**: the claim refuses every channel with
the token `REOPENED_AWAITING_AUDIT` until a completed re-audit is recorded. The order the
claim applies its checks in is in the rulebook
(`config/rulebook.json` → `guardrails.evaluation_order_at_claim`):

1. `dnc_flag` → `BLOCKED_DNC` (an opt-out beats everything, including an open audit);
2. `Pending_Adjustment_Confirmation` → `BLOCKED_PENDING_ADJUSTMENT`;
3. an open reopen audit (state or log) → `REOPENED_AWAITING_AUDIT`;
4. zero or credit balance → `BLOCKED_NO_BALANCE_DUE`;
5. the calling window, then the mode/sign-off and the authorization scope.

The balance rule is unchanged by all of this, and the audit cannot relax it: the re-audit
derives the state from the ledger (`engine/payments.py::derive_account_state`), so a record
whose balance is zero or a credit comes back as `Paid` and is refused with
`BLOCKED_NO_BALANCE_DUE`. Where both would apply, the state gate answers first and the
balance gate answers second — both are refusals, neither can be configured off.

**A reversal never exits a hold.** If the record is already in a hold when the reversal
lands, the un-bill still happens by counter-entry, the audit is still opened, and the state
is left exactly where the human put it — the audit row records both states and
`hold_state_kept = 1`. `Dispute_Hold` is named in that rule already
(`engine/payments.py::HOLD_STATES`); the state cannot be stored yet because the dispute
interceptor (Agent 4) is not built.

## `billing_route_verifications` — APPEND ONLY

The Milestone B chain of record. One row per validation attempt, whichever way it went:
a pass, a capability deficiency, a sandbox-pin demonstration.

| Column | Meaning |
| --- | --- |
| `verification_id` | `bv-` + uuid. The handle a client's bound receipt points at. |
| `account_id` | The account the receipt named, byte-exact. |
| `livemode`, `charges_enabled`, `payouts_enabled` | The receipt's native booleans, as `0`/`1`. |
| `verified_at` | The receipt's own `timestamp_utc`, parsed as a real tz-aware UTC instant (not string-matched). |
| `verification_source` | The named read-back that produced the receipt. Empty is treated as unverified. |
| `receipt_json` | The canonical JSON bytes (sorted keys, compact separators) the hash is taken over. |
| `receipt_sha256` | `sha256(receipt_json)`. Recomputed on every read: a row whose payload no longer hashes is a security exception (`RECEIPT_PAYLOAD_TAMPERED`), not a warning. |
| `prev_chain_hash` | The previous row's `chain_hash`, or the documented genesis constant for the first row. |
| `chain_hash` | `sha256(prev_chain_hash \| receipt_sha256 \| verification_id)`. |
| `decision` | `PASS`, `PASS_SANDBOX_ONLY`, `CAPABILITY_DISABLED`. CHECK-constrained: `PASS` requires `sandbox_only = 0`, so a sandbox pass cannot be recorded as a live one. |
| `sandbox_only` | `1` when the receipt validated against the sandbox pin register. |
| `reopen_reason` | The human reason supplied when a previously-locked account is re-verified. Required to re-open: without it the attempt is refused. |
| `recorded_by`, `recorded_at` | Who recorded it and when, to the millisecond. |

**What the chain proves, and what it does not.** `chain_hash` proves two things and only
two: that the payload has not changed since it was recorded, and where that payload sits in
the sequence. It does not prove *who* recorded it, and it is not third-party attestation —
anyone with write access to the store could append a row and recompute the chain. A hash
proves content and order; it does not prove authorship, and nothing in this project should
be described as though it did.

Append-only is enforced by UPDATE/DELETE triggers, exactly as for `autonomy_mode_history`.
`tests/test_gate.py::TestImmutability` drops a trigger on purpose, edits a payload and
shows both `verify_chain()` and `read_bound_receipt()` refuse it.

## `lifecycle_transition_log` — APPEND ONLY

Every lifecycle status change and every milestone flag change, in one immutable record.
Written by the gate function and nowhere else: no console route and no HTTP handler
inserts here (`tests/test_gate.py` fails if any other engine module tries).

| Column | Meaning |
| --- | --- |
| `transition_id` | Auto increment. |
| `recorded_at` | Millisecond UTC (`...T16:23:12.928Z`). |
| `event` | `lifecycle_status`, `milestone_flag` or `gate_event`. CHECK-constrained: a flag or status row must carry both its old and new value, so an event can never claim a change it did not make. |
| `client_id`, `field` | Which client, and which status/flag/decision. |
| `old_value`, `new_value` | The before and after. A refused attempt records both as the value still in force — the log never claims a change that did not happen. |
| `validating_parameters` | JSON: the receipt hash, verification id, chain hash, verification source, review counts or outstanding milestones that justified the decision. This is what makes the row re-checkable. |
| `executing_administrator` | Who or what ran it. |
| `reason` | Plain English, required. |

## `manual_review_decisions` — APPEND ONLY

The evidence behind `manual_review_threshold_passed`, one row per human decision or
guardrail refusal. Only `decision IN ('approved','rejected')` with
`operating_mode_at_decision = 'hitl'` count towards the 20.

| Column | Meaning |
| --- | --- |
| `decision` | `approved`, `rejected` or `guardrail_block`. |
| `decided_by`, `decided_at` | Who, and when (millisecond UTC). A `guardrail_block` is the system, not a person. |
| `operating_mode_at_decision` | The mode in force when the decision was taken. |
| `client_caused_exception` | `1` only for an exception the client caused. |
| `reason` | Plain English. |

`CHECK (decision <> 'guardrail_block' OR client_caused_exception = 0)` is the important
line, and it is in the store rather than in Python: **a guardrail correctly blocking a send
can never be recorded as a client-caused compliance exception**, and it neither adds an
infraction nor reduces the review count. That is asserted in
`tests/test_gate.py::TestManualReviewMilestone`.

## Postgres

`engine/db.py` translates the schema for Postgres when `DATABASE_URL` is set and a driver
imports: `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL PRIMARY KEY`, `?` → `%s`, and
the `:sqlite-only` blocks are stripped in favour of the `:postgres-only` block, which
recreates the append-only guarantee with a plpgsql trigger. The claim transaction uses
`SELECT ... FOR UPDATE` on `client_autonomy_settings` where SQLite relies on
`BEGIN IMMEDIATE`.

**Not verified in this sandbox**: no Postgres driver is installed here, so the Postgres
path and its trigger have never been executed. Treat them as a reviewed, untested
migration and run the suite against a real Postgres before trusting them.


### `commercial_msa_records` (added 2026-10-07)

The EVIDENCE behind the commercial milestone, and the reason the beta -> full-active gate cannot
open today. One row per executed commercial Master Services Agreement: `msa_id`, `client_id`,
`counterparty_legal_name`, `signatory_name`, `signatory_title`, `agreement_reference`,
`document_version`, `executed_at`, `document_sha256` (64-char lowercase hex), `recorded_at`,
`recorded_by`, `note`. Append-only, with the same `*_no_update` / `*_no_delete` triggers as the
other evidence tables; every substantive column is `CHECK`-ed non-blank, so a blank row cannot
stand in for a signature.

**It is EMPTY today**, which is the truth: nobody has signed a commercial agreement. Because the
milestone is derived from this table and never from `client_autonomy_settings.
commercial_msa_executed` (a reporting mirror), the milestone is FALSE for every client and the
gate is closed. Written only by `gate.record_commercial_msa()`.

The two `client_autonomy_settings` triggers that gate `STATUS_FULL_ACTIVE` (on `BEFORE INSERT` and
on `BEFORE UPDATE OF lifecycle_status`) recompute all three milestones from the evidence tables -
`EXISTS` in `commercial_msa_records`, a `PASS`/non-sandbox row in `billing_route_verifications`
with a bound receipt, and `COUNT(*) >= 20` in `manual_review_decisions` - so a one-statement
hand-written promotion is refused by the database as well as by the gate API.

### `ingestion_batches` (Phase 3, added 2026-10-07)
APPEND ONLY. One row per uploaded CSV/Excel file that is cleared to import through Agent 1's
spreadsheet intake, and the **precondition for a COMMIT**: `engine/ingest.py::commit_spreadsheet`
refuses with `BLOCKED_NO_INGESTION_BATCH` when no row exists for the batch id it was handed.

| Column | Meaning |
| --- | --- |
| `seq` | Append order. The chain link is read in this order. |
| `batch_id` | UUID4 hex. The id a commit is checked against. |
| `client_id` | The client the file belongs to. The upload supplies it; it is never read out of the file. |
| `uploader_id` | Who handed the file over. An identity, not a boolean — an upload is a data-consent event. |
| `file_name` | The name the client's file arrived under. |
| `file_sha256` | 64-char lowercase hex over the exact bytes received (`.xlsx`) or the decoded text (`.csv`). |
| `source_format` | `csv` or `xlsx`. CHECK-constrained: anything else is refused before a row exists. |
| `rows_seen` / `rows_accepted` / `rows_rejected` | The preview's counts. `CHECK (rows_seen = rows_accepted + rows_rejected)`. |
| `preview_fingerprint` | sha256 over the previewed state this batch was raised from — the no-drift key. |
| `mapping_json` | The compact source-column → target-field report the batch was accepted under. |
| `rejected_json` | Every dropped row, as `[{row_index, reason_token}]`. |
| `consumer_debt_marker` / `consumer_debt_signals` | The advisory scanner's verdict, recorded even when it is `0`. It blocks nothing. |
| `recorded_at_ms` / `recorded_at` | The acceptance instant, millisecond-UTC and second-UTC. |
| `warranty_acknowledged` | `CHECK (warranty_acknowledged = 1)`: an unacknowledged upload cannot be recorded at all. |
| `warranty_signature_reference` | The client's own signed authorization reference (`sha256:…`) the batch binds to. |
| `warranty_document_version` / `warranty_text_sha256` | The Step 3 warranty version and the sha256 of its exact wording, so the text a batch was accepted under is recoverable byte-for-byte. |
| `warranty_binding_sha256` | sha256 over (client_id, the signed reference, the warranty text hash, its version, the uploader, the acknowledgement instant). |
| `batch_sha256` | sha256 over the row's own canonical content — see the honesty note below. |
| `prev_chain_hash` / `chain_hash` | The append-only chain: `chain_hash = sha256(prev_chain_hash \| batch_sha256 \| batch_id)`, from `GENESIS_BATCH_CHAIN_HASH` in `engine/ingest.py`. |
| `recorded_by`, `reason`, `note` | Who recorded it, why, and anything else a human needs. |

**HONESTY.** This is a hash-chained append-only record. It is **tamper-evident, not
signature-proof**: `batch_sha256` proves the row has not changed since it was appended and
`chain_hash` proves where it sits in the sequence. It does **not** prove who uploaded the file,
and it is not third-party attestation. `ingest.verify_ingestion_batches()` recomputes the whole
chain from the rows themselves; a hand-inserted row is reported as a break.

**Read by the console through the bridge.** The client console never opens this table itself:
the site's server functions call `tools/console_bridge.py` (`op: "register"` / `"commit"` /
`"status"` / `"batches"`) and render the rows the engine returns, so `ingestion_batches` has exactly
one writer and one reader of its rules. See the engine README's "The console bridge" section.

The Step 3 warranty is a CHECK constraint rather than a convention, and the wording it binds
lives in `config/rulebook.json::spreadsheet_intake.step3_warranty` — the team's draft, **not
reviewed by counsel**, like every other legal string in this tree.

### The spreadsheet intake path (Phase 3, added 2026-10-07)
A client's own CSV/Excel file is a first-class way in, and it is deliberately **not** a second
mapping engine: `preview_spreadsheet()` → `commit_spreadsheet()` both run Agent 1's existing
translator (`config/ingestion_aliases.json` via `_alias_index()`) and its existing
`normalize()` + `persist()`. The only thing the upload adds is the two-step, fail-closed
protocol.

* **PREVIEW writes nothing** — no `debtor_records` row, no queue entry, no batch record. It
  returns the mapping report (source column → target field, unmapped columns), per-row status,
  every dropped row with its index and exact reason token, the consumer-debt state and the
  totals. `preview_fingerprint` is the sha256 over exactly that state.
* **COMMIT writes only against an existing batch record** whose `preview_fingerprint`,
  `file_sha256`, client, format, counts and uploader match the preview handed to it. Drift is a
  refusal, never a partial import. Committed rows carry `source_system = "Spreadsheet intake
  (CSV: <file>)"` and `source_ref = "ingestion_batch:<batch_id>"`, so one query shows which
  upload a debtor came from.

**Row tokens** (exactly one drop reason per row, first check wins; rulebook
`spreadsheet_intake.reason_tokens` and `evaluation_order_per_row`):

| Token | Kind | Meaning |
| --- | --- | --- |
| `MISSING_DEBTOR_ID` | drop | No debtor identifier could be translated. |
| `AMOUNT_NOT_NUMERIC` | drop | The amount is present but is not a number (`12abc`, `1.234,56`, `N/A`). Never coerced. |
| `MISSING_BALANCE` | drop | No amount at all — refused rather than read as zero. |
| `UNPARSEABLE_DATE` | drop | The date is present but matches no documented format. Never guessed. |
| `BLOCKED_NO_BALANCE_DUE` | drop | Zero or credit balance — the same token the claim path uses. |
| `ROW_SHAPE_MISMATCH` | drop | The row does not line up with its header row. Never padded or truncated. |
| `MISSING_PHONE` / `MISSING_EMAIL` | flag | The row imports; the missing value is flagged per row and stored as missing, never invented. |
| `MISSING_INVOICE_DATE` | flag | Imports with `aging_state = unknown`, exactly as the live path stores an absent date. |
| `UNMAPPED_COLUMN` | report | A source column with no documented alias. Reported, never guessed into a field. |
| `DUPLICATE_TARGET_FIELD` | error | Two source columns claim one target field. Surfaced; the preview is not importable. |

**Commit refusals:** `BLOCKED_NO_INGESTION_BATCH` (no record), `BATCH_RECORD_TAMPERED` (the
record no longer hashes to its own chain), `BATCH_PREVIEW_STATE_CHANGED` (the record and the
preview disagree — including a preview whose own rows were edited after it was produced),
`BATCH_ALREADY_COMMITTED` (rows from this batch are already queued), `BLOCKED_MAPPING_ERROR`
(the preview was never importable), `BLOCKED_WARRANTY_NOT_ACKNOWLEDGED`,
`BLOCKED_NO_WARRANTY_SIGNATURE`, and `UNSUPPORTED_FILE_TYPE` for a format we do not read.

**The consumer-debt scanner is advisory, and it is POLICY, NOT LAW.** It measures two signals
 individual-person name structures versus entity identifiers (LLC, Inc, Ltd, Corp, Studio,
Agency, Group, `&`, Co, LLP, Designs, Media, …) and the share of balances under $100
(owner-specified) — and when a signal trips it registers `consumer_debt_marker` and carries the
warning text byte-for-byte: *"Warning: Data footprint resembles consumer debt. RecoverAI
operates strictly on B2B commercial trade debt."* It **never blocks, rejects, alters or
reorders a row**. The thresholds and the entity-identifier list are the team's heuristic, in
`config/rulebook.json::spreadsheet_intake.consumer_debt_scan`; the warning is a prompt for a
human to settle with counsel, because medical/dental and similar balances are usually consumer
debt.

**`aging_state` is derived, not translated.** A source column called `aging_state` is reported
unmapped: the engine buckets aging from `invoice_date` through the rulebook's edges, and a file
with no usable date imports with `aging_state = unknown` rather than our trusting someone
else's bucket.

### The daily-operations read models (Phase 3, added 2026-10-09)

**No new table, and no new column.** The console's daily operations — the live queue, the mode
switch and its halt, the HITL review queue, the per-debtor interaction history, the alert feed
and the money trail — are **read-only views over the tables already documented above**, built in
`engine/daily_ops.py` and served through `tools/console_bridge.py` (`board`, `daily_queue`,
`mode`, `reviews`, `interactions`, `alerts`, `money`). The store is still thirteen tables; if a
reader of this document sees a fourteenth, something went wrong.

| Surface | Read from | Notes |
| --- | --- | --- |
| Queue rows | `debtor_records` + `outbound_contact_logs` (latest attempt per debtor) + the run queue | status label and reason are DERIVED, from the precedence order below; `dnc_flag`, `account_state`, `approval_status`, `window_decision` and `derived_local_time` travel **raw** on every row so no label ever masks them. |
| Mode view | `client_autonomy_settings` + `autonomy_mode_history` + the run queue | `full_auto_selectable` **is** `onboarding.signoff_state(...)["valid"]` — the same predicate `switch_mode` refuses on, so the button can never be disabled for a reason the engine would not also refuse on. `refused_attempt` is derived from a reason starting with `REFUSED`; the stored reason travels too. |
| Review queue | `debtor_records` (`approval_status = 'Pending'`) + `manual_review_decisions` + `gate.manual_review_counts` | the 20-review milestone count is the gate's own function, never a second implementation. `approval_blockers` mirrors the three guards `Orchestrator.approve` itself raises on and says so; everything after approval belongs to the claim transaction. |
| Interaction history | `outbound_contact_logs` + the payload file the claim wrote (`out/…/payloads/*.json`) | read-only. A missing payload file is reported as missing, never reconstructed. `attempted_at` is second-UTC and `claimed_at` millisecond-UTC; both travel and the UI says which is which. |
| Alert feed | `out/console_audit.jsonl` (the engine's append-only action log) | a FILE, deliberately: each console read is a fresh process, so an in-memory log would be empty every time. Entries with no `client_id` are marked `client_scoped: false` rather than attributed to a client. |
| Money trail | `collections_ledger` + `payment_events` + `reopen_audit_events` | the statement is `DRAFT` with the ledger's own contents as the reason. |

**Status precedence** (`engine/daily_ops.STATUS_PRECEDENCE`, first match wins): `Paid` >
`Reopened_Awaiting_Audit` > `Pending_Adjustment_Confirmation` > `Partially_Paid` > `DNC` >
`Outside_Calling_Window` > `CLAIMED` (the last attempt committed a claim) > the token of the
last REFUSED claim (`BLOCKED_*`) > `Pending`. A terminal or held money state is never hidden
behind a DNC badge, and vice versa.

**Where the schedule of record lives.** The console's run queue is
`out/console_runs.json` (`RECOVERAI_CONSOLE_RUNS`), separate from the demo's
`out/scheduled_runs.json`, which `run_demo.py` rewrites on every run. `RecoverAI` takes
`runqueue_path=`, loads the queue on open and writes it back from **`engine.close()`** — the
facade, not `store.close()`. That distinction is the whole point: `store.close()` alone leaves
the queue in memory, and because the bridge is one process per request, the next read would
show an empty schedule and *Switch to Manual* would halt nothing. `RunQueue.dump` writes a
sibling `.tmp` and `os.replace`s it (a reader never sees half a queue); `RunQueue.load` raises
on a file that exists but cannot be read, rather than silently returning an empty queue.
`tests/test_daily_ops.py` covers both halves, including two bridge processes either side of a
halt.

**§ Not a schema change, but recorded here because the console's numbers depend on it:** the
bridge's `simulate_payment` op adds an explicit `ok` to the payment path's own result (which
carries `status: "COMMITTED"` and no `ok` at all). Without it the console's close-the-loop
panel — the one that shows *payment → queue purge → ledger line → statement still DRAFT* —
never rendered, even though the payment had really run: the ledger line and the collected
total moved while the panel that explains them stayed hidden. Found by the daily
click-through on 2026-10-09, fixed in the bridge (not in the UI), and asserted in
`tests/test_daily_ops.py::BridgeAcrossProcessesTests`.
