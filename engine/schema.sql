-- RecoverAI store schema - the operating tables, plus three append-only records:
-- the mode timeline, the collections ledger, the payment-event intake log and the
-- balance-edit log. Nothing in an append-only table is ever updated or deleted,
-- in our code or by hand: triggers reject both.
--
-- Dialect: SQLite (stdlib sqlite3, zero setup). The same shape runs on Postgres;
-- blocks between  -- :sqlite-only  and  -- :end  are SQLite-specific (RAISE triggers,
-- partial unique index) and engine/db.py translates them for Postgres.
-- See docs/schema.md for the column-by-column description.
--
-- Conventions kept identical across both engines so one code path serves both:
--   * every timestamp is TEXT, ISO-8601 UTC with a trailing Z
--   * every boolean is INTEGER 0/1
--   * every list-valued column (channels, aging states) is a JSON array in TEXT
--   * money is REAL/NUMERIC in the invoice currency, never moved anywhere

-- ---------------------------------------------------------------------------
-- client_autonomy_settings
-- The single row every mode flip and every send claim contends for.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_autonomy_settings (
    client_id                       TEXT PRIMARY KEY,
    operating_mode                  TEXT    NOT NULL DEFAULT 'hitl'
                                            CHECK (operating_mode IN ('hitl', 'full_auto')),
    has_valid_signoff               INTEGER NOT NULL DEFAULT 0 CHECK (has_valid_signoff IN (0, 1)),
    signer_name                     TEXT,
    signer_title                    TEXT,
    signature_reference             TEXT,
    authorized_channels             TEXT,
    authorized_aging_states         TEXT,
    signed_timestamp                TEXT,
    -- extra audit columns (documented in docs/schema.md)
    authorized_scope_pairs          TEXT,
    authorization_document_version  TEXT,
    signature_revoked               INTEGER NOT NULL DEFAULT 0,
    signature_revoked_at            TEXT,
    signoff_expires_at              TEXT,
    mode_version                    INTEGER NOT NULL DEFAULT 0,
    claim_seq                       INTEGER NOT NULL DEFAULT 0,
    last_claim_at                   TEXT,
    updated_at                      TEXT,
    -- ------------------------------------------------------------------
    -- Phase 2: the beta -> full-active lifecycle and the three milestone
    -- flags. See docs/gate.md. Every new profile starts as a beta tester.
    -- The three milestone columns are MIRRORS for reporting only: the
    -- milestone itself is EVIDENCE-derived (manual_review_decisions,
    -- billing_route_verifications + the bound receipt,
    -- commercial_msa_records) and the triggers below refuse
    -- STATUS_FULL_ACTIVE unless that evidence EXISTS for this client, so
    -- writing a flag by hand - alone or in the same statement as the
    -- status - buys nothing.
    -- ------------------------------------------------------------------
    lifecycle_status                TEXT NOT NULL DEFAULT 'STATUS_BETA_TESTER'
                                    CHECK (lifecycle_status IN ('STATUS_BETA_TESTER',
                                                                'STATUS_FULL_ACTIVE')),
    manual_review_threshold_passed  INTEGER NOT NULL DEFAULT 0
                                    CHECK (manual_review_threshold_passed IN (0, 1)),
    live_billing_verified           INTEGER NOT NULL DEFAULT 0
                                    CHECK (live_billing_verified IN (0, 1)),
    commercial_msa_executed         INTEGER NOT NULL DEFAULT 0
                                    CHECK (commercial_msa_executed IN (0, 1)),
    -- the honest sibling flag: a sandbox-pin receipt may set THIS and never the above
    billing_route_demonstrated_in_sandbox INTEGER NOT NULL DEFAULT 0
                                    CHECK (billing_route_demonstrated_in_sandbox IN (0, 1)),
    -- the bound entry for the successful live-billing receipt: canonical JSON
    -- (sorted keys, compact separators) carrying the receipt and its sha256.
    -- Column-scoped trigger below: one NULL -> value write is allowed, every later
    -- change - including clearing it - is refused, while a mode toggle still works.
    live_billing_receipt_metadata   TEXT,
    -- ------------------------------------------------------------------
    -- Onboarding (Phase 4, step 1 + step 3).
    --
    --   tracking_mode    how the client asked collections to be tracked.
    --                    WHERE we look, never WHETHER we look. It is BOUND
    --                    INTO THE SIGNATURE HASH (engine/onboarding.py::
    --                    compute_signature_reference), so it is not a
    --                    free-floating preference: changing it later revokes
    --                    the sign-off it was signed under and forces a fresh
    --                    signature. Every change is an append-only row in
    --                    collections_tracking_choices.
    --   step3_attestation_id  the onboarding_attestations row (type
    --                    'step3_data_warranty') that the standing sign-off is
    --                    bound to. NULL until a Step 3 attestation exists when
    --                    the client signs.
    -- ------------------------------------------------------------------
    tracking_mode        TEXT NOT NULL DEFAULT 'hybrid'
                         CHECK (tracking_mode IN ('integration', 'manual_attestation', 'hybrid')),
    tracking_mode_version INTEGER NOT NULL DEFAULT 0,
    step3_attestation_id TEXT
);

-- ---------------------------------------------------------------------------
-- debtor_records
-- One normalized past-due invoice per row (Agent 1's output).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS debtor_records (
    debtor_id          TEXT PRIMARY KEY,
    client_id          TEXT NOT NULL,
    name               TEXT,
    email              TEXT,
    phone              TEXT,
    balance_owed       REAL,
    invoice_date       TEXT,
    aging_state        TEXT,
    approval_status    TEXT NOT NULL DEFAULT 'Pending'
                              CHECK (approval_status IN ('Pending', 'Approved', 'Sent', 'Outside_Calling_Window')),
    -- account_state is the PAYMENT lifecycle, a separate axis from approval_status:
    --   Open                          - nothing confirmed collected against this invoice
    --   Partially_Paid                - a confirmed payment arrived, a remainder is still owed
    --   Paid                          - the balance reached zero: never contacted again
    --   Pending_Adjustment_Confirmation - a balance edit with no payment signal behind it is
    --                                   parked: no dunning, no fee, until a human says why
    --   Reopened_Awaiting_Audit       - a confirmed payment was reversed (bounce/refund). The
    --                                   fee is un-billed by a linked counter-entry and the
    --                                   purged queue stays purged; the record cannot be
    --                                   claimed until it has re-run the ingestion path from
    --                                   step one and the completed re-audit is recorded in
    --                                   reopen_audit_events. Mandatory state, not a flag.
    -- approval_status stays the human-approval gate; the claim transaction re-reads both.
    account_state      TEXT NOT NULL DEFAULT 'Open'
                              CHECK (account_state IN ('Open', 'Partially_Paid', 'Paid',
                                                       'Pending_Adjustment_Confirmation',
                                                       'Reopened_Awaiting_Audit')),
    balance_updated_at TEXT,
    dnc_flag           INTEGER NOT NULL DEFAULT 0 CHECK (dnc_flag IN (0, 1)),
    -- extra audit/ingestion columns (documented in docs/schema.md)
    invoice_number     TEXT,
    source_system      TEXT,
    source_ref         TEXT,
    source_columns     TEXT,
    raw_record         TEXT,
    aging_note         TEXT,
    area_code          TEXT,
    tz_name            TEXT,
    derived_local_time TEXT,
    window_decision    TEXT,
    window_decision_at TEXT,
    approved_by        TEXT,
    approved_at        TEXT,
    dnc_set_at         TEXT,
    imported_at        TEXT
);

-- ---------------------------------------------------------------------------
-- autonomy_mode_history - APPEND ONLY.
-- Triggers below reject every UPDATE and every DELETE, including from our own code.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS autonomy_mode_history (
    history_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    who           TEXT NOT NULL,
    old_mode      TEXT NOT NULL,
    new_mode      TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    reason        TEXT,
    mode_version  INTEGER,
    client_id     TEXT
);

-- :sqlite-only
CREATE TRIGGER IF NOT EXISTS autonomy_mode_history_no_update
BEFORE UPDATE ON autonomy_mode_history
BEGIN
    SELECT RAISE(ABORT, 'autonomy_mode_history is append-only: UPDATE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS autonomy_mode_history_no_delete
BEFORE DELETE ON autonomy_mode_history
BEGIN
    SELECT RAISE(ABORT, 'autonomy_mode_history is append-only: DELETE is rejected');
END;
-- :end

-- ---------------------------------------------------------------------------
-- outbound_contact_logs
-- Every contact attempt (claimed, sent, failed or blocked) plus the mode and
-- window decision in force at claim time, linked to the signature reference.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outbound_contact_logs (
    contact_log_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    debtor_id              TEXT NOT NULL,
    client_id              TEXT NOT NULL,
    attempted_at           TEXT NOT NULL,
    contact_type           TEXT NOT NULL,
    destination            TEXT,
    decision               TEXT NOT NULL,
    status                 TEXT NOT NULL,
    reason_detail          TEXT,
    operating_mode_at_claim TEXT,
    mode_version_at_claim  INTEGER,
    approval_status_at_claim TEXT,
    dnc_flag_at_claim      INTEGER,
    -- the last-millisecond snapshot: what the payment state actually was inside the
    -- transaction that wrote the claim, including the balance in integer cents
    account_state_at_claim TEXT,
    balance_at_claim_cents INTEGER,
    window_decision        TEXT,
    derived_local_time     TEXT,
    area_code              TEXT,
    signature_reference    TEXT,
    -- who authorized THIS contact and when, in millisecond UTC. In hitl the
    -- approver's name and approval instant are recorded here (and the reference
    -- above is human-approval:<who>@<when>); in full_auto the authorization is the
    -- client's sign-off, carried in signature_reference, and these stay NULL.
    approved_by            TEXT,
    approved_at            TEXT,
    claim_token            TEXT,
    claimed_at             TEXT,
    payload_path           TEXT,
    message_preview        TEXT,
    provider_response      TEXT
);

-- One committed claim per token, and at most one live claim per debtor+token pair.
-- :sqlite-only
CREATE UNIQUE INDEX IF NOT EXISTS ux_contact_log_claim_token
    ON outbound_contact_logs (claim_token) WHERE claim_token IS NOT NULL;
-- :end
CREATE UNIQUE INDEX IF NOT EXISTS ux_contact_log_debtor_claim
    ON outbound_contact_logs (debtor_id, claim_token);

-- ---------------------------------------------------------------------------
-- payment_events - APPEND ONLY. One row per inbound payment signal, whichever
-- channel carried it. This is the intake half of "one payment event, three
-- consequences": it records the signal, the state it produced, the fee ledger row
-- it appended and exactly which scheduled runs it cancelled, at what instant.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           TEXT NOT NULL,
    debtor_id           TEXT NOT NULL,
    confirmation_channel TEXT NOT NULL
                        CHECK (confirmation_channel IN ('accounting_webhook', 'delta_sync',
                                                        'manual_attestation')),
    amount_cents        INTEGER NOT NULL CHECK (amount_cents > 0),
    currency            TEXT NOT NULL DEFAULT 'USD',
    attested_by         TEXT,
    external_reference  TEXT,
    signal_at           TEXT,
    confirmation_latency TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    balance_before_cents INTEGER,
    balance_after_cents INTEGER,
    overpayment_cents   INTEGER NOT NULL DEFAULT 0,
    account_state_before TEXT,
    account_state_after TEXT NOT NULL,
    -- the shared correlation handle: the intake event and the ledger line it produced
    -- carry the same value, because both rows are append-only and neither may be
    -- updated to point at the other
    intake_reference    TEXT NOT NULL,
    queue_purged_at     TEXT,
    cancelled_runs      TEXT,
    cancelled_run_count INTEGER NOT NULL DEFAULT 0,
    outbound_logs_untouched INTEGER,
    status              TEXT NOT NULL DEFAULT 'COMMITTED',
    note                TEXT
);

-- ---------------------------------------------------------------------------
-- collections_ledger - APPEND ONLY. Nothing is ever edited here: a reversal is a
-- linked counter-entry (entry_type = 'reversal', linked_entry_id -> the collection
-- it reverses), so the original row stays byte-identical forever.
--
-- Money is integer cents (amount_cents, fee_cents) and the fee is enforced by the
-- engine AND by the CHECK below: fee = rate x collected, half up to the cent, with
-- the rate carried on the row itself so the arithmetic is auditable per line.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collections_ledger (
    entry_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           TEXT NOT NULL,
    debtor_id           TEXT NOT NULL,
    entry_type          TEXT NOT NULL CHECK (entry_type IN ('collection', 'reversal')),
    amount_cents        INTEGER NOT NULL CHECK (amount_cents <> 0),
    fee_cents           INTEGER NOT NULL,
    fee_basis_points    INTEGER NOT NULL CHECK (fee_basis_points > 0),
    fee_rounding_rule   TEXT NOT NULL,
    -- money is only ever billable with a named confirmation source on the line
    confirmation_channel TEXT NOT NULL
                        CHECK (confirmation_channel IN ('accounting_webhook', 'delta_sync',
                                                        'manual_attestation')),
    confirmation_latency TEXT NOT NULL,
    attested_by         TEXT,
    external_reference  TEXT,
    signal_at           TEXT,
    recorded_at         TEXT NOT NULL,
    balance_before_cents INTEGER,
    balance_after_cents INTEGER,
    debtor_state_before TEXT,
    debtor_state_after  TEXT NOT NULL,
    linked_entry_id     INTEGER,
    payment_event_id    INTEGER,
    intake_reference    TEXT,
    note                TEXT,
    -- a collection is a positive amount with a positive fee; a reversal is the exact
    -- negation of the collection it points at, fee included ("reopen and un-bill")
    CHECK ((entry_type = 'collection' AND amount_cents > 0)
           OR (entry_type = 'reversal' AND amount_cents < 0)),
    CHECK ((entry_type = 'collection'
            AND fee_cents = (abs(amount_cents) * fee_basis_points + 5000) / 10000)
           OR (entry_type = 'reversal'
               AND fee_cents = -((abs(amount_cents) * fee_basis_points + 5000) / 10000))),
    -- a reversal must say which collection it reverses; a collection reverses nothing
    CHECK ((entry_type = 'reversal' AND linked_entry_id IS NOT NULL)
           OR (entry_type = 'collection' AND linked_entry_id IS NULL))
);

-- One reversal per collection. NULLs do not collide in a unique index in either
-- engine, so this needs no partial predicate.
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_one_reversal_per_collection
    ON collections_ledger (linked_entry_id);
CREATE INDEX IF NOT EXISTS ix_ledger_client ON collections_ledger (client_id, entry_id);
CREATE INDEX IF NOT EXISTS ix_ledger_debtor ON collections_ledger (debtor_id, entry_id);

-- ---------------------------------------------------------------------------
-- balance_edit_events - APPEND ONLY. A balance change with no payment signal behind
-- it parks the record in Pending_Adjustment_Confirmation (no dunning, no fee). The
-- resolution is a second row: resolved_as_payment (billable, through the normal
-- payment path) or resolved_as_adjustment (closed, never billable).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS balance_edit_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           TEXT NOT NULL,
    debtor_id           TEXT NOT NULL,
    event_type          TEXT NOT NULL
                        CHECK (event_type IN ('edit_unconfirmed', 'resolved_as_payment',
                                              'resolved_as_adjustment')),
    balance_before_cents INTEGER NOT NULL,
    balance_after_cents INTEGER NOT NULL,
    delta_cents         INTEGER NOT NULL,
    who                 TEXT NOT NULL,
    reason              TEXT,
    recorded_at         TEXT NOT NULL,
    billable            INTEGER NOT NULL CHECK (billable IN (0, 1)),
    resolution_channel  TEXT,
    ledger_entry_id     INTEGER,
    linked_event_id     INTEGER,
    note                TEXT
);

CREATE INDEX IF NOT EXISTS ix_balance_edit_debtor ON balance_edit_events (debtor_id, event_id);

-- ---------------------------------------------------------------------------
-- reopen_audit_events - APPEND ONLY. The reopen audit invariant, in three event
-- types on one log:
--
--   reversal_opened_audit  written by the reversal itself, inside the same transaction
--                          that un-bills: which collection was reversed, by which counter-
--                          entry, from which payment event, for exactly how much, and the
--                          payment state before and after. Millisecond UTC.
--   reaudit_completed      the record re-ran the ingestion path from step one over its
--                          stored source row and this is what was found: the re-normalized
--                          source balance (recorded, never applied), whether it matches the
--                          ledger-derived balance, whether contact data resolved, the
--                          re-evaluated calling window, the DNC flag, the paid status and
--                          the administrator who ran it. Millisecond UTC.
--   reaudit_refused        a re-audit the engine refused to complete (no source row,
--                          unparseable source, source that no longer identifies the same
--                          debtor). The refusal reason is recorded and NOTHING moves: the
--                          record stays where it is and stays un-claimable.
--
-- An open audit is an `reversal_opened_audit` row with no completion row carrying the same
-- reversal_entry_id. That question is asked inside the send claim transaction, so a record
-- with an open audit cannot be contacted - not by any mode, not after another hold on it
-- was resolved by a human.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reopen_audit_events (
    event_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           TEXT NOT NULL,
    debtor_id           TEXT NOT NULL,
    event_type          TEXT NOT NULL
                        CHECK (event_type IN ('reversal_opened_audit', 'reaudit_completed',
                                              'reaudit_refused')),
    -- the links: the collection that was un-billed, the counter-entry that un-billed it,
    -- and the intake event that recorded the payment in the first place
    collection_entry_id INTEGER,
    reversal_entry_id   INTEGER,
    payment_event_id    INTEGER,
    un_billed_amount_cents INTEGER,
    un_billed_fee_cents    INTEGER,
    balance_before_cents   INTEGER,
    balance_after_cents    INTEGER,
    account_state_before   TEXT,
    account_state_after    TEXT,
    -- what the re-run of the ingestion path found (completion rows)
    source_system          TEXT,
    source_ref             TEXT,
    renormalized           INTEGER NOT NULL DEFAULT 0 CHECK (renormalized IN (0, 1)),
    source_balance_cents   INTEGER,
    -- 1 = the re-normalized source figure agrees with the ledger-derived balance. The source
    -- figure is RECORDED either way and never applied: the ledger is the money record.
    source_balance_matches_engine INTEGER
                        CHECK (source_balance_matches_engine IS NULL
                               OR source_balance_matches_engine IN (0, 1)),
    contact_email_resolved INTEGER NOT NULL DEFAULT 0
                        CHECK (contact_email_resolved IN (0, 1)),
    contact_phone_resolved INTEGER NOT NULL DEFAULT 0
                        CHECK (contact_phone_resolved IN (0, 1)),
    window_decision        TEXT,
    derived_local_time     TEXT,
    area_code              TEXT,
    tz_name                TEXT,
    dnc_flag               INTEGER,
    paid_status            TEXT,
    ledger_balance_cents   INTEGER,
    approval_status_after  TEXT,
    -- 1 = the record was already in a hold (Pending_Adjustment_Confirmation, Dispute_Hold)
    -- and the audit left it there: a reversal and a reopen audit never exit a hold.
    hold_state_kept        INTEGER NOT NULL DEFAULT 0 CHECK (hold_state_kept IN (0, 1)),
    refuse_reason          TEXT,
    performed_by           TEXT NOT NULL,
    reason                 TEXT,
    -- millisecond UTC: this log has to answer "which of these two landed first"
    recorded_at            TEXT NOT NULL,
    note                   TEXT,
    -- a completed or refused audit must be attributable and dated; an opening row must carry
    -- the counter-entry it describes, or it is not a record of anything
    CHECK (event_type <> 'reversal_opened_audit' OR reversal_entry_id IS NOT NULL),
    CHECK (event_type <> 'reaudit_completed' OR renormalized = 1)
);

CREATE INDEX IF NOT EXISTS ix_reopen_audit_debtor
    ON reopen_audit_events (debtor_id, event_id);

-- ---------------------------------------------------------------------------
-- billing_route_verifications - APPEND ONLY. The chain of record for the
-- Milestone B (live billing route) evidence, one row per verification attempt
-- that got as far as carrying a well-formed receipt. See docs/gate.md.
--
-- HONESTY: this is a hash-chained append-only record. It is TAMPER-EVIDENT, not a
-- cryptographic proof: chain_hash proves the payload has not changed since it was
-- recorded and where it sits in the sequence. It does NOT prove who recorded it, and
-- it is not third-party attestation. A hash proves content and order, not authorship.
--
-- chain_hash = sha256(prev_chain_hash | receipt_sha256 | verification_id) over append
-- order (seq), starting from GENESIS_CHAIN_HASH in engine/gate.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing_route_verifications (
    seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id     TEXT NOT NULL UNIQUE,
    client_id           TEXT NOT NULL,
    account_id          TEXT NOT NULL,
    livemode            INTEGER NOT NULL CHECK (livemode IN (0, 1)),
    charges_enabled     INTEGER NOT NULL CHECK (charges_enabled IN (0, 1)),
    payouts_enabled     INTEGER NOT NULL CHECK (payouts_enabled IN (0, 1)),
    verified_at         TEXT NOT NULL,
    verification_source TEXT NOT NULL,
    receipt_json        TEXT NOT NULL,
    receipt_sha256      TEXT NOT NULL,
    prev_chain_hash     TEXT NOT NULL,
    chain_hash          TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    recorded_by         TEXT NOT NULL,
    -- PASS                  the receipt passed and the platform pin matched byte-for-byte
    -- PASS_SANDBOX_ONLY     it passed against the explicit SANDBOX_PIN: demo/tests only
    -- CAPABILITY_DISABLED   a well-formed receipt reporting a capability as explicitly
    --                       False (or livemode False): the attempt is locked for good
    decision            TEXT NOT NULL CHECK (decision IN ('PASS', 'PASS_SANDBOX_ONLY',
                                                         'CAPABILITY_DISABLED')),
    sandbox_only        INTEGER NOT NULL CHECK (sandbox_only IN (0, 1)),
    -- a capability-disabled attempt is never re-opened by editing THIS row: re-opening
    -- means a NEW verification event whose receipt passes, carrying the human reason
    -- that references what changed, recorded on the new row
    reopen_reason       TEXT,
    note                TEXT,
    CHECK ((decision = 'PASS' AND sandbox_only = 0)
           OR (decision <> 'PASS'))
);

CREATE INDEX IF NOT EXISTS ix_billing_route_client
    ON billing_route_verifications (client_id, seq);

-- ---------------------------------------------------------------------------
-- lifecycle_transition_log - APPEND ONLY. Every change (or refused change) of a
-- lifecycle status or a milestone flag, from the one gate function that may write
-- them, with millisecond-UTC time, the old and new value, the parameters that were
-- validated, the administrator and a reason. No console and no HTTP route writes
-- a flag directly; they call the gate, and the gate writes this row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lifecycle_transition_log (
    transition_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at             TEXT NOT NULL,
    event                   TEXT NOT NULL
                            CHECK (event IN ('lifecycle_status', 'milestone_flag',
                                             'gate_event')),
    client_id               TEXT NOT NULL,
    field                   TEXT NOT NULL,
    old_value               TEXT,
    new_value               TEXT,
    validating_parameters   TEXT,
    executing_administrator TEXT NOT NULL,
    reason                  TEXT NOT NULL,
    -- a flag row always says what it moved from and to; a gate_event may not
    CHECK ((event <> 'milestone_flag' AND event <> 'lifecycle_status')
           OR (old_value IS NOT NULL AND new_value IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS ix_lifecycle_log_client
    ON lifecycle_transition_log (client_id, transition_id);

-- ---------------------------------------------------------------------------
-- manual_review_decisions - APPEND ONLY. The evidence behind
-- manual_review_threshold_passed: one row per manual hitl approve/reject, plus one
-- row per attempt a guardrail correctly blocked.
--
-- The CHECK at the bottom is the load-bearing one: a guardrail block can NEVER be
-- recorded as a client-caused compliance exception. A guardrail doing its job is the
-- product working, not the client failing, so it neither adds an infraction nor
-- counts toward (or against) the 20 review milestone.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS manual_review_decisions (
    decision_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id                   TEXT NOT NULL,
    debtor_id                   TEXT NOT NULL,
    decision                    TEXT NOT NULL
                                CHECK (decision IN ('approved', 'rejected', 'guardrail_block')),
    decided_by                  TEXT NOT NULL,
    decided_at                  TEXT NOT NULL,
    operating_mode_at_decision  TEXT NOT NULL,
    client_caused_exception     INTEGER NOT NULL DEFAULT 0
                                CHECK (client_caused_exception IN (0, 1)),
    reason                      TEXT,
    CHECK (decision <> 'guardrail_block' OR client_caused_exception = 0)
);

CREATE INDEX IF NOT EXISTS ix_manual_review_client
    ON manual_review_decisions (client_id, decision_id);

-- ---------------------------------------------------------------------------
-- commercial_msa_records - APPEND ONLY. The EVIDENCE behind the
-- `commercial_msa_executed` milestone: one row per executed commercial Master
-- Services Agreement recorded against a client.
--
-- This table is the milestone. The `commercial_msa_executed` column on
-- client_autonomy_settings is only a MIRROR of it, for reporting: every decision
-- (engine/gate.py::verified_commercial_msa, milestone_state, launch_condition and
-- the two full_active triggers below) reads THIS table with EXISTS, so setting the
-- column by hand - alone or in the same statement as the lifecycle status - cannot
-- satisfy the milestone.
--
-- It is EMPTY in the shipped state, because nobody has signed anything: the
-- commercial milestone is therefore FALSE for every client and the gate cannot
-- open. That is the honest state, not a stub.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commercial_msa_records (
    msa_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id               TEXT NOT NULL,
    -- the executed agreement: who signed, for which legal entity, which document, when
    counterparty_legal_name TEXT NOT NULL,
    signatory_name          TEXT NOT NULL,
    signatory_title         TEXT NOT NULL,
    agreement_reference     TEXT NOT NULL,
    document_version        TEXT NOT NULL,
    executed_at             TEXT NOT NULL,
    document_sha256         TEXT NOT NULL,
    recorded_at             TEXT NOT NULL,
    recorded_by             TEXT NOT NULL,
    note                    TEXT,
    -- an executed agreement has a signature date, a named signatory and a hashed
    -- document; a row with blanks in those is not evidence of anything
    CHECK (length(trim(counterparty_legal_name)) > 0
           AND length(trim(signatory_name)) > 0
           AND length(trim(signatory_title)) > 0
           AND length(trim(agreement_reference)) > 0
           AND length(trim(document_version)) > 0
           AND length(trim(executed_at)) > 0
           AND length(document_sha256) = 64)
);

CREATE INDEX IF NOT EXISTS ix_commercial_msa_client
    ON commercial_msa_records (client_id, msa_id);

-- ---------------------------------------------------------------------------
-- ingestion_batches - APPEND ONLY. Phase 3: one row per uploaded CSV/Excel
-- file that is cleared to import through Agent 1's spreadsheet intake.
--
-- This record is the precondition for a COMMIT. engine/ingest.py::commit_spreadsheet
-- refuses with BLOCKED_NO_INGESTION_BATCH when no row exists for the batch id it was
-- handed, and with BATCH_PREVIEW_STATE_CHANGED when the row it finds does not match the
-- previewed state (drift is a refusal, never a partial import).
--
-- HONESTY: this is a hash-chained append-only record. It is TAMPER-EVIDENT, not
-- signature-proof: batch_sha256 proves the row has not changed since it was recorded and
-- chain_hash proves where it sits in the sequence. It does NOT prove who uploaded the
-- file. A hash proves content and order, not authorship.
--
--   batch_sha256            = sha256 over the canonical JSON of the row's own content
--                             fields, including preview_fingerprint and the warranty binding
--   warranty_binding_sha256 = sha256 over (client_id, the client's legal sign-off
--                             signature_reference, the sha256 of the exact Step 3 warranty
--                             text, its version, the uploader and the acknowledgement instant)
--   chain_hash              = sha256(prev_chain_hash | batch_sha256 | batch_id) in seq order,
--                             starting from GENESIS_BATCH_CHAIN_HASH in engine/ingest.py
--
-- The Step 3 warranty is a CHECK constraint, not a convention: warranty_acknowledged must
-- be 1 for a row to exist at all, so an unacknowledged upload cannot be recorded.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingestion_batches (
    seq                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             TEXT NOT NULL UNIQUE,
    client_id            TEXT NOT NULL,
    -- who handed us the file: an identity, not a boolean
    uploader_id          TEXT NOT NULL,
    file_name            TEXT NOT NULL,
    -- 64-char lowercase hex, over the exact bytes received (xlsx) or the decoded text
    file_sha256          TEXT NOT NULL,
    source_format        TEXT NOT NULL CHECK (source_format IN ('csv', 'xlsx')),
    rows_seen            INTEGER NOT NULL CHECK (rows_seen >= 0),
    rows_accepted        INTEGER NOT NULL CHECK (rows_accepted >= 0),
    rows_rejected        INTEGER NOT NULL CHECK (rows_rejected >= 0),
    -- the sha256 over the previewed state this batch was raised from: the no-drift key
    preview_fingerprint  TEXT NOT NULL,
    mapping_json         TEXT NOT NULL,
    rejected_json        TEXT NOT NULL,
    -- the advisory consumer-debt scanner's verdict; it blocks nothing (see the rulebook)
    consumer_debt_marker  INTEGER NOT NULL CHECK (consumer_debt_marker IN (0, 1)),
    consumer_debt_signals TEXT NOT NULL,
    -- millisecond-UTC instant the file was accepted, plus the second-precision sibling
    recorded_at_ms       TEXT NOT NULL,
    recorded_at          TEXT NOT NULL,
    -- the Step 3 legal warranty binding: the wording a batch was accepted under, recoverable
    -- byte-for-byte from warranty_text_sha256 + the version.
    warranty_acknowledged        INTEGER NOT NULL CHECK (warranty_acknowledged = 1),
    warranty_signature_reference TEXT NOT NULL,
    warranty_document_version    TEXT NOT NULL,
    warranty_text_sha256         TEXT NOT NULL,
    warranty_binding_sha256      TEXT NOT NULL,
    -- Phase 4: the REAL Step 3 gate this batch was accepted under. The attestation id and
    -- the sha256 of the exact checkbox text that attestation recorded, both inside
    -- warranty_binding_sha256. A row with an empty attestation id is a LEGACY row: it was
    -- appended before the Step 3 gate existed, its batch_sha256 covers the legacy field set
    -- (see engine/ingest.py::BATCH_HASH_FIELDS_LEGACY), and it stays valid - this table is
    -- append-only history, and re-hashing old rows would be rewriting them. A NEW row
    -- cannot be written at all without a Step 3 attestation on file for its client.
    warranty_step3_attestation_id    TEXT NOT NULL DEFAULT '',
    warranty_step3_attestation_sha256 TEXT NOT NULL DEFAULT '',
    batch_sha256         TEXT NOT NULL,
    prev_chain_hash      TEXT NOT NULL,
    chain_hash           TEXT NOT NULL,
    recorded_by          TEXT NOT NULL,
    reason               TEXT NOT NULL,
    note                 TEXT,
    -- the counts must add up, and every hash field must be a real sha256
    CHECK (rows_seen = rows_accepted + rows_rejected),
    CHECK (length(file_sha256) = 64 AND length(preview_fingerprint) = 64
           AND length(warranty_text_sha256) = 64 AND length(warranty_binding_sha256) = 64
           AND length(batch_sha256) = 64 AND length(prev_chain_hash) = 64
           AND length(chain_hash) = 64)
);

CREATE INDEX IF NOT EXISTS ix_ingestion_batches_client
    ON ingestion_batches (client_id, seq);

-- ---------------------------------------------------------------------------
-- onboarding_attestations - APPEND ONLY. Phase 4: the onboarding ladder's recorded
-- acknowledgements, one row per explicit tick.
--
--   step2_fee_tracking_ack  the mandatory "here is exactly how we charge and how we
--                           track collections" acknowledgement.
--   step3_data_warranty     THE REAL GATE. Contact data accurate, active and lawfully
--                           consented; AS-IS beta with zero recovery guarantees;
--                           indemnification for data accuracy. engine/ingest.py refuses
--                           to register an ingestion batch without one
--                           (BLOCKED_NO_STEP3_ATTESTATION).
--
-- Both are UNSELECTED by default and the backend refuses to record either one until the
-- flag is explicitly TRUE: the CHECK (acknowledged = 1) makes an unacknowledged row
-- impossible, so "not ticked" can never be written to look like "ticked". The exact
-- checkbox text is stored against the client WITH the sha256 of that text and the
-- document version, so the wording someone agreed to is recoverable byte-for-byte - and
-- that same sha256 is what a batch's warranty binding carries.
--
-- HONESTY, the same shape as ingestion_batches: attestation_sha256 proves the row has not
-- changed since it was written. It proves content, not authorship - a hash is not a
-- signature, and acknowledged_by records who typed the acknowledgement, not who they are.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS onboarding_attestations (
    seq                  INTEGER PRIMARY KEY AUTOINCREMENT,
    attestation_id       TEXT NOT NULL UNIQUE,
    client_id            TEXT NOT NULL,
    attestation_type     TEXT NOT NULL
                         CHECK (attestation_type IN ('step2_fee_tracking_ack',
                                                     'step3_data_warranty')),
    document_version     TEXT NOT NULL,
    rulebook_version     TEXT NOT NULL,
    attestation_text     TEXT NOT NULL,
    text_sha256          TEXT NOT NULL CHECK (length(text_sha256) = 64),
    -- unselected by default, and unrecordable until the flag is explicitly TRUE
    acknowledged         INTEGER NOT NULL CHECK (acknowledged = 1),
    acknowledged_by      TEXT NOT NULL,
    acknowledged_at_ms   TEXT NOT NULL,
    acknowledged_at      TEXT NOT NULL,
    source               TEXT NOT NULL,
    -- the fee-tracking choice in force when this was ticked: a change to that choice
    -- revokes the sign-off, and this is the record of what the client had chosen
    tracking_mode_at_attestation TEXT NOT NULL,
    attestation_sha256   TEXT NOT NULL CHECK (length(attestation_sha256) = 64),
    note                 TEXT
);
CREATE INDEX IF NOT EXISTS ix_onboarding_attestations_client
    ON onboarding_attestations (client_id, attestation_type, seq);
-- :sqlite-only
CREATE TRIGGER IF NOT EXISTS onboarding_attestations_no_update
BEFORE UPDATE ON onboarding_attestations
BEGIN
    SELECT RAISE(ABORT, 'onboarding_attestations is append-only: UPDATE is rejected');
END;
CREATE TRIGGER IF NOT EXISTS onboarding_attestations_no_delete
BEFORE DELETE ON onboarding_attestations
BEGIN
    SELECT RAISE(ABORT, 'onboarding_attestations is append-only: DELETE is rejected');
END;
-- :end

-- ---------------------------------------------------------------------------
-- collections_tracking_choices - APPEND ONLY. Step 1 of onboarding: how the client
-- asked collections to be tracked. Zero upfront does not mean zero evidence: the choice
-- is part of the agreement, so it is BOUND INTO THE SIGNATURE HASH and changing it is a
-- change to the arrangement - the engine revokes the standing sign-off, drops the client
-- to hitl and requires a fresh signature under the new choice. This table is the
-- immutable trail of who changed it, from what, to what, at which millisecond, why, and
-- which signature the change invalidated.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collections_tracking_choices (
    choice_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id        TEXT NOT NULL,
    who              TEXT NOT NULL,
    old_mode         TEXT NOT NULL
                     CHECK (old_mode IN ('integration', 'manual_attestation', 'hybrid')),
    new_mode         TEXT NOT NULL
                     CHECK (new_mode IN ('integration', 'manual_attestation', 'hybrid')),
    timestamp        TEXT NOT NULL,
    timestamp_ms     TEXT NOT NULL,
    reason           TEXT,
    choice_version   INTEGER,
    -- the signature reference in force when the choice changed, and whether the change
    -- pulled it: a sign-off that no longer describes the tracking arrangement is not a
    -- valid sign-off
    bound_signature_reference TEXT,
    signoff_rebind_required   INTEGER NOT NULL DEFAULT 0
                              CHECK (signoff_rebind_required IN (0, 1)),
    rebind_token     TEXT,
    note             TEXT
);
CREATE INDEX IF NOT EXISTS ix_collections_tracking_choices_client
    ON collections_tracking_choices (client_id, choice_id);
-- :sqlite-only
CREATE TRIGGER IF NOT EXISTS collections_tracking_choices_no_update
BEFORE UPDATE ON collections_tracking_choices
BEGIN
    SELECT RAISE(ABORT, 'collections_tracking_choices is append-only: UPDATE is rejected');
END;
CREATE TRIGGER IF NOT EXISTS collections_tracking_choices_no_delete
BEFORE DELETE ON collections_tracking_choices
BEGIN
    SELECT RAISE(ABORT, 'collections_tracking_choices is append-only: DELETE is rejected');
END;
-- :end

-- :sqlite-only
CREATE TRIGGER IF NOT EXISTS collections_ledger_no_update
BEFORE UPDATE ON collections_ledger
BEGIN
    SELECT RAISE(ABORT, 'collections_ledger is append-only: UPDATE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS collections_ledger_no_delete
BEFORE DELETE ON collections_ledger
BEGIN
    SELECT RAISE(ABORT, 'collections_ledger is append-only: DELETE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS payment_events_no_update
BEFORE UPDATE ON payment_events
BEGIN
    SELECT RAISE(ABORT, 'payment_events is append-only: UPDATE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS payment_events_no_delete
BEFORE DELETE ON payment_events
BEGIN
    SELECT RAISE(ABORT, 'payment_events is append-only: DELETE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS balance_edit_events_no_update
BEFORE UPDATE ON balance_edit_events
BEGIN
    SELECT RAISE(ABORT, 'balance_edit_events is append-only: UPDATE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS balance_edit_events_no_delete
BEFORE DELETE ON balance_edit_events
BEGIN
    SELECT RAISE(ABORT, 'balance_edit_events is append-only: DELETE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS reopen_audit_events_no_update
BEFORE UPDATE ON reopen_audit_events
BEGIN
    SELECT RAISE(ABORT, 'reopen_audit_events is append-only: UPDATE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS reopen_audit_events_no_delete
BEFORE DELETE ON reopen_audit_events
BEGIN
    SELECT RAISE(ABORT, 'reopen_audit_events is append-only: DELETE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS billing_route_verifications_no_update
BEFORE UPDATE ON billing_route_verifications
BEGIN
    SELECT RAISE(ABORT, 'billing_route_verifications is append-only: UPDATE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS billing_route_verifications_no_delete
BEFORE DELETE ON billing_route_verifications
BEGIN
    SELECT RAISE(ABORT, 'billing_route_verifications is append-only: DELETE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS lifecycle_transition_log_no_update
BEFORE UPDATE ON lifecycle_transition_log
BEGIN
    SELECT RAISE(ABORT, 'lifecycle_transition_log is append-only: UPDATE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS lifecycle_transition_log_no_delete
BEFORE DELETE ON lifecycle_transition_log
BEGIN
    SELECT RAISE(ABORT, 'lifecycle_transition_log is append-only: DELETE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS manual_review_decisions_no_update
BEFORE UPDATE ON manual_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'manual_review_decisions is append-only: UPDATE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS manual_review_decisions_no_delete
BEFORE DELETE ON manual_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'manual_review_decisions is append-only: DELETE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS commercial_msa_records_no_update
BEFORE UPDATE ON commercial_msa_records
BEGIN
    SELECT RAISE(ABORT, 'commercial_msa_records is append-only: UPDATE is rejected');
END;

CREATE TRIGGER IF NOT EXISTS commercial_msa_records_no_delete
BEFORE DELETE ON commercial_msa_records
BEGIN
    SELECT RAISE(ABORT, 'commercial_msa_records is append-only: DELETE is rejected');
END;
-- ingestion_batches (Phase 3): the same append-only guarantee. A batch record is the
-- precondition for a commit, so being able to edit or delete one would be able to un-say
-- which file was imported, by whom, under which warranty wording.
CREATE TRIGGER IF NOT EXISTS ingestion_batches_no_update
BEFORE UPDATE ON ingestion_batches
BEGIN
    SELECT RAISE(ABORT, 'ingestion_batches is append-only: UPDATE is rejected');
END;
CREATE TRIGGER IF NOT EXISTS ingestion_batches_no_delete
BEFORE DELETE ON ingestion_batches
BEGIN
    SELECT RAISE(ABORT, 'ingestion_batches is append-only: DELETE is rejected');
END;

-- The bound receipt metadata is write-once. The trigger is COLUMN-SCOPED on purpose:
-- client_autonomy_settings is UPDATEd on every mode toggle (operating_mode,
-- mode_version, claim_seq, updated_at), and a trigger on UPDATE of the whole row would
-- either break the mode switch or have to be bypassable. `BEFORE UPDATE OF
-- live_billing_receipt_metadata` fires only when that column is in the SET clause, so a
-- mode toggle never touches it, the single NULL -> value write is allowed, and every
-- later change - including clearing it back to NULL - is refused.
CREATE TRIGGER IF NOT EXISTS client_autonomy_settings_receipt_metadata_bound_once
BEFORE UPDATE OF live_billing_receipt_metadata ON client_autonomy_settings
WHEN NOT (OLD.live_billing_receipt_metadata IS NULL
          AND NEW.live_billing_receipt_metadata IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'live_billing_receipt_metadata is bound once: the receipt entry is write-once and may not be changed or cleared');
END;

-- STATUS_FULL_ACTIVE requires the EVIDENCE, not the flag columns. The previous version of
-- this trigger inspected the milestone flags on the same row being written, so one
-- statement that set the flags alongside the status passed it. These triggers cannot be
-- satisfied by any flag write at all: they recompute each milestone from the table that is
-- its evidence, with EXISTS/COUNT against the client actually being written.
--
--   manual_review_threshold_passed  >= 20 recorded hitl approvals/rejections for this
--                                   client, with zero client-caused compliance exceptions
--   live_billing_verified           a chain-recorded live PASS row for this client that is
--                                   not sandbox-only, plus the bound receipt column set
--   commercial_msa_executed         an executed commercial Master Services Agreement
--                                   recorded in commercial_msa_records for this client
--
-- Because the evidence tables are append-only and EMPTY in the shipped state, no client
-- can reach STATUS_FULL_ACTIVE today, by SQL or otherwise.
DROP TRIGGER IF EXISTS client_autonomy_settings_full_active_requires_milestones;

CREATE TRIGGER client_autonomy_settings_full_active_requires_milestones
BEFORE UPDATE OF lifecycle_status ON client_autonomy_settings
WHEN NEW.lifecycle_status = 'STATUS_FULL_ACTIVE'
     AND NOT (
        (SELECT COUNT(*) FROM manual_review_decisions
          WHERE client_id = NEW.client_id
            AND decision IN ('approved', 'rejected')
            AND operating_mode_at_decision = 'hitl') >= 20
        AND (SELECT COALESCE(SUM(client_caused_exception), 0) FROM manual_review_decisions
              WHERE client_id = NEW.client_id) = 0
        AND EXISTS (SELECT 1 FROM billing_route_verifications
                     WHERE client_id = NEW.client_id
                       AND decision = 'PASS'
                       AND sandbox_only = 0)
        AND NEW.live_billing_receipt_metadata IS NOT NULL
        AND EXISTS (SELECT 1 FROM commercial_msa_records
                     WHERE client_id = NEW.client_id)
     )
BEGIN
    SELECT RAISE(ABORT, 'STATUS_FULL_ACTIVE needs evidence, not flags: this client has no >= 20 recorded hitl reviews with zero client-caused exceptions, or no chain-verified non-sandbox live billing PASS bound to it, or no executed agreement in commercial_msa_records');
END;

DROP TRIGGER IF EXISTS client_autonomy_settings_full_active_on_insert;

CREATE TRIGGER client_autonomy_settings_full_active_on_insert
BEFORE INSERT ON client_autonomy_settings
WHEN NEW.lifecycle_status = 'STATUS_FULL_ACTIVE'
     AND NOT (
        (SELECT COUNT(*) FROM manual_review_decisions
          WHERE client_id = NEW.client_id
            AND decision IN ('approved', 'rejected')
            AND operating_mode_at_decision = 'hitl') >= 20
        AND (SELECT COALESCE(SUM(client_caused_exception), 0) FROM manual_review_decisions
              WHERE client_id = NEW.client_id) = 0
        AND EXISTS (SELECT 1 FROM billing_route_verifications
                     WHERE client_id = NEW.client_id
                       AND decision = 'PASS'
                       AND sandbox_only = 0)
        AND NEW.live_billing_receipt_metadata IS NOT NULL
        AND EXISTS (SELECT 1 FROM commercial_msa_records
                     WHERE client_id = NEW.client_id)
     )
BEGIN
    SELECT RAISE(ABORT, 'STATUS_FULL_ACTIVE needs evidence, not flags: a brand-new client row cannot arrive with evidence for a milestone, so this INSERT is refused');
END;
-- :end

-- :postgres-only
-- Same append-only guarantee as the SQLite RAISE triggers above. NOTE: this block has
-- not been executed anywhere in this sandbox (no Postgres driver is installed); it is
-- the documented migration for a real deployment and must be tested there first.
-- The function now names the table it refused, so one function covers every log.
CREATE OR REPLACE FUNCTION recoverai_append_only() RETURNS trigger AS $recoverai$
BEGIN
    RAISE EXCEPTION '% is append-only: % is rejected', TG_TABLE_NAME, TG_OP;
END;
$recoverai$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS autonomy_mode_history_no_update ON autonomy_mode_history;
CREATE TRIGGER autonomy_mode_history_no_update
    BEFORE UPDATE OR DELETE ON autonomy_mode_history
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

DROP TRIGGER IF EXISTS collections_ledger_no_update ON collections_ledger;
CREATE TRIGGER collections_ledger_no_update
    BEFORE UPDATE OR DELETE ON collections_ledger
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

DROP TRIGGER IF EXISTS payment_events_no_update ON payment_events;
CREATE TRIGGER payment_events_no_update
    BEFORE UPDATE OR DELETE ON payment_events
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

DROP TRIGGER IF EXISTS balance_edit_events_no_update ON balance_edit_events;
CREATE TRIGGER balance_edit_events_no_update
    BEFORE UPDATE OR DELETE ON balance_edit_events
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

DROP TRIGGER IF EXISTS reopen_audit_events_no_update ON reopen_audit_events;
CREATE TRIGGER reopen_audit_events_no_update
    BEFORE UPDATE OR DELETE ON reopen_audit_events
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

DROP TRIGGER IF EXISTS billing_route_verifications_no_update ON billing_route_verifications;
CREATE TRIGGER billing_route_verifications_no_update
    BEFORE UPDATE OR DELETE ON billing_route_verifications
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

DROP TRIGGER IF EXISTS lifecycle_transition_log_no_update ON lifecycle_transition_log;
CREATE TRIGGER lifecycle_transition_log_no_update
    BEFORE UPDATE OR DELETE ON lifecycle_transition_log
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

DROP TRIGGER IF EXISTS manual_review_decisions_no_update ON manual_review_decisions;
CREATE TRIGGER manual_review_decisions_no_update
    BEFORE UPDATE OR DELETE ON manual_review_decisions
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

DROP TRIGGER IF EXISTS commercial_msa_records_no_update ON commercial_msa_records;
CREATE TRIGGER commercial_msa_records_no_update
    BEFORE UPDATE OR DELETE ON commercial_msa_records
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();
DROP TRIGGER IF EXISTS ingestion_batches_no_update ON ingestion_batches;
CREATE TRIGGER ingestion_batches_no_update
    BEFORE UPDATE OR DELETE ON ingestion_batches
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

-- Phase 4: the onboarding ladder's two append-only logs.
DROP TRIGGER IF EXISTS onboarding_attestations_no_update ON onboarding_attestations;
CREATE TRIGGER onboarding_attestations_no_update
    BEFORE UPDATE OR DELETE ON onboarding_attestations
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

DROP TRIGGER IF EXISTS collections_tracking_choices_no_update ON collections_tracking_choices;
CREATE TRIGGER collections_tracking_choices_no_update
    BEFORE UPDATE OR DELETE ON collections_tracking_choices
    FOR EACH ROW EXECUTE FUNCTION recoverai_append_only();

-- The bound receipt entry: one NULL -> value write, then nothing. Column-scoped, so a
-- mode toggle (which never names this column) is unaffected.
CREATE OR REPLACE FUNCTION recoverai_receipt_metadata_bound_once() RETURNS trigger AS $recoverai$
BEGIN
    IF NOT (OLD.live_billing_receipt_metadata IS NULL
            AND NEW.live_billing_receipt_metadata IS NOT NULL) THEN
        RAISE EXCEPTION 'live_billing_receipt_metadata is bound once: % is rejected',
            TG_OP;
    END IF;
    RETURN NEW;
END;
$recoverai$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS client_autonomy_settings_receipt_metadata_bound_once
    ON client_autonomy_settings;
CREATE TRIGGER client_autonomy_settings_receipt_metadata_bound_once
    BEFORE UPDATE OF live_billing_receipt_metadata ON client_autonomy_settings
    FOR EACH ROW EXECUTE FUNCTION recoverai_receipt_metadata_bound_once();

-- STATUS_FULL_ACTIVE requires the EVIDENCE for each milestone, recomputed in the trigger
-- with EXISTS/COUNT against the client being written. The flag columns are mirrors and
-- cannot satisfy it - not alone, and not in the same statement as the status. Unverified
-- here (no Postgres driver in this sandbox); SQLite enforces the identical rule.
CREATE OR REPLACE FUNCTION recoverai_full_active_requires_milestones() RETURNS trigger AS $recoverai$
DECLARE
    reviews integer;
    exceptions integer;
BEGIN
    IF NEW.lifecycle_status = 'STATUS_FULL_ACTIVE' THEN
        SELECT COUNT(*) INTO reviews FROM manual_review_decisions
         WHERE client_id = NEW.client_id
           AND decision IN ('approved', 'rejected')
           AND operating_mode_at_decision = 'hitl';
        SELECT COALESCE(SUM(client_caused_exception), 0) INTO exceptions
          FROM manual_review_decisions WHERE client_id = NEW.client_id;
        IF reviews < 20 OR exceptions <> 0
           OR NOT EXISTS (SELECT 1 FROM billing_route_verifications
                           WHERE client_id = NEW.client_id
                             AND decision = 'PASS' AND sandbox_only = 0)
           OR NEW.live_billing_receipt_metadata IS NULL
           OR NOT EXISTS (SELECT 1 FROM commercial_msa_records
                           WHERE client_id = NEW.client_id) THEN
            RAISE EXCEPTION 'STATUS_FULL_ACTIVE needs evidence, not flags: >= 20 recorded '
                            'hitl reviews with zero client-caused exceptions, a '
                            'chain-verified non-sandbox live billing PASS bound to this '
                            'client, and an executed agreement in commercial_msa_records';
        END IF;
    END IF;
    RETURN NEW;
END;
$recoverai$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS client_autonomy_settings_full_active_requires_milestones
    ON client_autonomy_settings;
CREATE TRIGGER client_autonomy_settings_full_active_requires_milestones
    BEFORE INSERT OR UPDATE OF lifecycle_status ON client_autonomy_settings
    FOR EACH ROW EXECUTE FUNCTION recoverai_full_active_requires_milestones();

-- Deployment migration for a database created before Phase 2 (SQLite gets the same
-- columns through engine/db.py::_ensure_columns). NOT executed in this sandbox.
ALTER TABLE client_autonomy_settings
    ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'STATUS_BETA_TESTER';
ALTER TABLE client_autonomy_settings
    ADD COLUMN IF NOT EXISTS manual_review_threshold_passed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE client_autonomy_settings
    ADD COLUMN IF NOT EXISTS live_billing_verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE client_autonomy_settings
    ADD COLUMN IF NOT EXISTS commercial_msa_executed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE client_autonomy_settings
    ADD COLUMN IF NOT EXISTS billing_route_demonstrated_in_sandbox INTEGER NOT NULL DEFAULT 0;
ALTER TABLE client_autonomy_settings
    ADD COLUMN IF NOT EXISTS live_billing_receipt_metadata TEXT;
ALTER TABLE outbound_contact_logs ADD COLUMN IF NOT EXISTS approved_by TEXT;
ALTER TABLE outbound_contact_logs ADD COLUMN IF NOT EXISTS approved_at TEXT;
-- Phase 4 onboarding: step 1's tracking choice, the Step 3 attestation the standing
-- sign-off is bound to, and the two columns a batch carries for the real Step 3 gate.
-- Defaulted and additive, so a database created before this pass keeps every row and
-- every already-committed batch: a batch row whose attestation id is empty is a legacy
-- row and verifies against the legacy field set (engine/ingest.py::BATCH_HASH_FIELDS_LEGACY).
ALTER TABLE client_autonomy_settings
    ADD COLUMN IF NOT EXISTS tracking_mode TEXT NOT NULL DEFAULT 'hybrid';
ALTER TABLE client_autonomy_settings
    ADD COLUMN IF NOT EXISTS tracking_mode_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE client_autonomy_settings
    ADD COLUMN IF NOT EXISTS step3_attestation_id TEXT;
ALTER TABLE ingestion_batches
    ADD COLUMN IF NOT EXISTS warranty_step3_attestation_id TEXT NOT NULL DEFAULT '';
ALTER TABLE ingestion_batches
    ADD COLUMN IF NOT EXISTS warranty_step3_attestation_sha256 TEXT NOT NULL DEFAULT '';
-- :end
