# The beta-to-full-active gate (Phase 2)

Two states, three milestones, one honest default.

`STATUS_BETA_TESTER` is the default for every new profile and the state of every client
today. `STATUS_FULL_ACTIVE` is reachable only through the gate in `engine/gate.py`. The
commercial reason the gate exists: a client at `STATUS_FULL_ACTIVE` is a client we bill 20%
of collections from, and we must not promote anyone into that fee until we can actually
collect it.

## The three milestones

| Milestone | TRUE only when | Status today |
| --- | --- | --- |
| `manual_review_threshold_passed` | at least **20** manual `hitl` approvals/rejections are recorded and there are **zero** client-caused compliance exceptions | satisfiable: the code counts real rows |
| `live_billing_verified` | a receipt is validated against the pinned platform account and bound to the client, chain-verified on every read | **cannot pass today**: the pin resolves to `acct_1TbxU10xRANq8tnU` (Profit Recovery Partners) and is API-verified, but that account has never taken a live payment (0 charges, 0 payment intents, $0 available, $0 pending), so there is no live `payment_intent_sample` to cite and no receipt can be bound |
| `commercial_msa_executed` | the pilot agreement is upgraded to the commercial Master Services Agreement | **cannot pass today**: it requires an executed agreement recorded in `commercial_msa_records`, and that table is EMPTY. The `commercial_msa_executed` column is a reporting mirror and satisfies nothing (amendment at the end of this file) |

A guardrail *correctly blocking a send* is the product working, not the client failing. It
is recorded with an exception count of zero, it adds no infraction, and it does not reduce
the review count towards the 20. The store's own CHECK constraint makes that structural:

```sql
CHECK (decision <> 'guardrail_block' OR client_caused_exception = 0)
```

## Every state change is one immutable row

`lifecycle_transition_log` records millisecond UTC, the event, the field, the old and new
value, the `validating_parameters` that justified it (receipt hash, verification id, chain
hash, review counts, outstanding milestones), the executing administrator and a reason.
`event` is CHECK-constrained so a `milestone_flag` or `lifecycle_status` row must carry both
values; a refused attempt records the value still in force on both sides, so the log never
claims a change that did not happen.

**The gate function is the only writer.** No console route and no HTTP handler writes a
milestone flag or a lifecycle status. Since 2026-10-07 that is enforced BEHAVIOURALLY rather
than by a source scan: the previous form of that test asserted the literal `STATUS_FULL_ACTIVE`
and several `SET ...` / `INSERT INTO ...` strings were absent from the other modules' source
text, which would still have passed with the runtime guard deleted - and which never covered
the hole that mattered, a direct SQL write. The test now drives every writer this engine
exposes at the client, plus the raw SQL, and asserts the client does not move.

The store refuses it too, and since 2026-10-07 it refuses on the EVIDENCE rather than on the
flags: both `full_active` triggers on `client_autonomy_settings` recompute each milestone with
`EXISTS`/`COUNT` against `manual_review_decisions`, `billing_route_verifications` and
`commercial_msa_records` and raise with `STATUS_FULL_ACTIVE needs evidence, not flags`. A
single statement that sets every flag alongside the status is refused, on INSERT and on UPDATE.
## Milestone B — the receipt

```json
{
  "payment_intent_sample": "pi_...",
  "livemode": true,
  "capabilities": {"charges_enabled": true, "payouts_enabled": true},
  "account_id": "acct_...",
  "timestamp_utc": "2026-10-06T16:23:12Z"
}
```

The schema is **strict**. Unknown keys are rejected, a missing key is never defaulted to a
safe value, and every boolean is checked with `type(value) is bool`.

The owner's spec read `isinstance(val, bool)`. It is implemented as the stricter
`type(value) is bool`, and the reason is in the source: `isinstance` accepts bool
subclasses and is the check people route around with string matching. `type(value) is bool`
accepts exactly the two native booleans, so `"true"`, `"True"`, `1`, `0`, `1.0`, `None`,
`[]` and `{}` are refused rather than coerced. No string matching of `"true"`/`"false"`
appears anywhere on this path, and a test asserts that.

Rules that carry weight:

* **`livemode` must be a native `True`.** A native `False` is not malformed — it is a
  capability deficiency, and a deficiency is handled by the decision table, not by the
  parser. A wrong-cased key containing the *string* `"true"` is rejected as an unknown key.
* **`payment_intent_sample` must appear in `config/verified_stripe_evidence.json`.** That
  file is the only thing that distinguishes an id we have actually read back from one that
  was invented, so an id that is not in it fails validation. It is read-only and is never
  written by this code.
* **`timestamp_utc` is parsed, not pattern-matched** — a real tz-aware instant in UTC, or a
  refusal.
* **`account_id` must byte-equal the pinned platform account id.** No strip, no case
  folding, no normalisation. A leading space is refused as "not an account id"; a
  one-character difference is a `PLATFORM_ACCOUNT_MISMATCH`.

## The decision table

| Outcome | State | What happens |
| --- | --- | --- |
| `PASS` | `SATISFIED` | chain row appended, `live_billing_verified` set, receipt bound **once** |
| `PASS_SANDBOX_ONLY` | `SANDBOX_DEMONSTRATED` | chain row appended, only `billing_route_demonstrated_in_sandbox` set |
| `MALFORMED` | `MALFORMED` | gate locked, **no flag written at all** |
| `CAPABILITY_DISABLED` | `CAPABILITY_DISABLED` | flag stays FALSE, the attempt is locked in its own immutable row |
| `TIMEOUT_UNKNOWN` | `UNKNOWN` | denies; every flag byte-identical, transition denied |
| `PLATFORM_ACCOUNT_MISMATCH` | `REFUSED` | security refusal + high-priority Agent 4 alert; the received id is never echoed — only its sha256 |
| `PLATFORM_ACCOUNT_NOT_PINNED` | `REFUSED` | while no account id is pinned the gate cannot pass **and records nothing** |

### "Permanently locked" means what it says, and what it does not

A `CAPABILITY_DISABLED` attempt is locked in an append-only row that no UPDATE, DELETE or
rewrite will accept. "Permanent" means the lock can never be **silently** cleared — not that
the gate can never open again. Re-opening requires a **new passing verification event
carrying a human-recorded reason that references what changed**; without that reason the
gate refuses with `VERIFICATION_REOPEN_REASON_REQUIRED`. The two behaviours it rules out are
the dishonest ones on either side: a lock that quietly disappears, and a lock that stays
shut even after the provider enabled the capability, which would hide the honest state
behind an unfixable one.

### The optional fetch adapter: a strict return shape, and no adapter means no fetch

`verify_milestone_b_receipt` accepts an optional injectable `fetch_adapter`, which makes the
UNKNOWN path testable. Everything below is a property of the code, not a promise about it.

**With no adapter configured, no fetch happens at all.** The call site is guarded by
`if fetch_adapter is not None`, and the default is `None`, so an ordinary verification makes
no outbound call of any kind and no verdict changes because of one. **No passing verdict
depends on a fetch**: a fetch can only confirm or refuse, never create a pass where the
receipt and the evidence did not already justify one. A test replaces `socket.connect` and
`socket.create_connection` with functions that raise, runs a full verification, and it still
passes. The validator is storage-only: it imports no networking module at all, and a test
parses the module and asserts that.

**When an adapter *is* supplied, its return shape is validated strictly, and a shape we
cannot read is a refusal.** The adapter is called with the receipt's
`payment_intent_sample`, and its answer must be a **mapping** carrying native booleans for
every key in `INDEPENDENT_READ_KEYS` — `livemode`, `charges_enabled`, `payouts_enabled`.
Two things are checked, in this order, and each one is its own recorded outcome:

* **Not a mapping at all** (a list, a string, `None`, a model object from a client library):
  `INDEPENDENT_READ_MALFORMED`, state `MALFORMED`, **gate locked, no flag written**. The
  refusal row records the returned type; the answer is never coerced into a boolean.
* **A mapping whose keys are missing, or whose values are not `bool`** — including the
  strings `"true"`/`"false"`, `1`/`0`, or a `bool` subclass — is the same outcome,
  `INDEPENDENT_READ_MALFORMED`, state `MALFORMED`, **gate locked, no flag written**, with the
  offending field names recorded. The check is `type(value) is bool`, so a truthy look-alike
  is malformed rather than accepted.

The rule behind both: **an answer we cannot read has told us nothing, and "I could not read
the platform" must never pass as confirmation.** A malformed read is refused exactly like a
malformed receipt. Only a well-formed mapping of native booleans gets further, and there a
`False` is a capability deficiency (`CAPABILITY_DISABLED`) or a contradiction
(`INDEPENDENT_READ_NOT_LIVEMODE`), never a pass. An adapter that raises a timeout is state
`UNKNOWN`: the transition is denied and every flag stays byte-identical.

## Two pin registers

* The **platform pin** comes from `RECOVERAI_PLATFORM_ACCOUNT_ID` or
  `config/platform.json`. When neither supplies one it resolves to the sentinel
  `UNPINNED`. That sentinel is deliberately not shaped like an `acct_...` id, because an
  invented account id is worse than an absent one.
* **The shipped register does supply one.** `config/platform.json` pins
  `acct_1TbxU10xRANq8tnU` (Profit Recovery Partners), read back with `GET /v1/account`
  using that account's own live key, which returned exactly that id with
  `charges_enabled` and `payouts_enabled` both true. The file carries the provenance and
  the correction of an earlier, mistaken withdrawal.
* **Pinning is not paying.** That account has never taken a payment (0 charges,
  0 payment intents, $0 available, $0 pending), so the live billing milestone stays
  UNRECORDED and every client stays `STATUS_BETA_TESTER`. This is an owner decision, not
  an omission: the first real payment through that account is what satisfies it.
* The **sandbox pin** (`acct_sandbox_pin_not_our_live_account`) is an explicitly-labelled
  register used by the demo and the tests. It is not a plausible account id, and a receipt
  validated against it can only ever set `billing_route_demonstrated_in_sandbox`.

A sandbox validation satisfies the sandbox flag and nothing else. That is enforced in three
independent places: the `decision` CHECK constraint (a `PASS` row must have
`sandbox_only = 0`), the branch that writes the flags, and `verified_live_billing()`, which
refuses to count a bound receipt whose account id is the sandbox pin. A test forces
`live_billing_verified = 1` by hand *and* binds a sandbox receipt, then shows the launch
condition still refuses.

## What opens the gate, and what still closes it

`verified_live_billing()` is what the launch condition reads — never the column. It
requires: the flag is `1`, a receipt is bound whose payload still recomputes, the chain row
it names is a live `PASS` with a non-empty verification source, and its account id
byte-equals the really pinned id.

Then, even with **every** milestone TRUE, the live launcher refuses:

```
engine.gate.launch_live_outbound(store, client_id, "voice", who, reason)
    -> GateLockedException("LIVE_SENDING_NOT_IMPLEMENTED")
```

Because there is no live sending infrastructure to launch into. The stub's only correct
behaviour today is to refuse — and it refuses **only the live launcher**. The sandbox send
path in `engine/orchestrator.py` is untouched, so the demo and the compliance tests keep
working. Locking that path would have broken the product to celebrate a gate.

## Honest state, stated plainly

* Every client is `STATUS_BETA_TESTER`, and will be until all three milestones are met.
  **No client has ever been promoted.**
* `live_billing_verified` cannot be satisfied at all right now, and the reason is NOT an
  absent pin: the register pins `acct_1TbxU10xRANq8tnU` (Profit Recovery Partners), and
  an API read of that account with its own key confirmed its id
  and both capability booleans. It stays unmet because no chain-verified receipt is bound
  and that account has never taken a live payment (0 charges, 0 payment intents, $0/$0),
  so there is no real `payment_intent_sample` to cite. The capability booleans are read
  from the receipt under test; our own tools do not query them live.
* `commercial_msa_executed` cannot be satisfied at all right now: it is a stub.
* The sandbox flag exists so demo and beta work can advance visibly without pretending the
  commercial route is verified. The console shows both states side by side.
* `billing_route_verifications` is **tamper-evident, not a proof of authorship**. See
  `docs/schema.md`.
* All external APIs are simulated. No real client data, no live calls, no money movement.

## Where the tests are

`tests/test_gate.py` — 85 tests across the lifecycle states, the receipt contract, the
decision table, the timeout path, the pin, the sandbox register, immutability and tamper
detection, the launch gate, Agent 4 reporting, the review milestone and the `hitl`
attribution fix. Run it with:

```
python3 -m unittest tests.test_gate -v
```


---

## Amendment (2026-10-07): the milestones are EVIDENCE-DERIVED, and the columns are mirrors

This section supersedes any earlier sentence in this file that says a milestone is read from
its flag column, that `commercial_msa_executed` is a fail-closed stub satisfied by nothing, or
that `STATUS_FULL_ACTIVE` is reachable once the three flags are TRUE. It was false when the gate
could be opened by one hand-written SQL statement, and it is now enforced twice over.

**Truth for each milestone comes from its own evidence, never from a column.**

| milestone | evidence table | how it is derived |
|---|---|---|
| `manual_review_threshold_passed` | `manual_review_decisions` | `COUNT(*)` of approved/rejected decisions taken in `hitl` >= 20, and `SUM(client_caused_exception) = 0` for that client |
| `live_billing_verified` | `billing_route_verifications` + the bound receipt | a `PASS` row with `sandbox_only = 0`, plus a chain-verified bound receipt in `client_autonomy_settings.live_billing_receipt_metadata`, re-checked against the pinned account id |
| `commercial_msa_executed` | `commercial_msa_records` | an executed commercial Master Services Agreement recorded for that client |

`client_autonomy_settings.manual_review_threshold_passed`, `live_billing_verified` and
`commercial_msa_executed` are **mirror columns written for reporting**. No decision reads them.
`milestone_state()` returns both the evidence-derived `value` and the column's `flag_value`, so
the two can be compared and reported on rather than confused.

**`commercial_msa_records` is EMPTY.** No client has a recorded, executed agreement, so the
commercial milestone is FALSE for every client, `launch_condition()["all_true"]` is False, and the
gate cannot open. That is the honest state, not a bug: nobody has signed anything, and the table
can only be written by `gate.record_commercial_msa()`, which records a counterparty, a signatory
and title, an agreement reference, a document version, the execution instant, the document's
sha256 and a reason before it mirrors the flag.

**The store enforces the same thing, because SQL cannot tell who is calling.** Both
`full_active` triggers (`BEFORE UPDATE OF lifecycle_status` and `BEFORE INSERT` on
`client_autonomy_settings`) recompute the three milestones with `EXISTS`/`COUNT` against the
evidence tables and refuse with `STATUS_FULL_ACTIVE needs evidence, not flags: ...`. A
single statement that sets every flag alongside the status is refused on INSERT and on UPDATE.

**A promotion records WHICH evidence satisfied each milestone.** The immutable transition row
carries an `evidence` object: the review decision ids and count, the chain verification id, the
receipt sha256, the chain hash, the pinned account id, and the recorded agreement's reference,
version and document hash. A later reader sees the basis, not just the date.

### What this does NOT prove

* The pin in `config/platform.json` is the account id the team read back with `GET /v1/account`
  using the account's own key. It is an **API read-back, not an owner attestation and not a
  payment**: that account has 0 charges and $0 available, so no receipt can be bound and the live
  billing milestone remains unmet for every client. A pin is not a verified live route.
* If the register holds no pin, the gate refuses with `PLATFORM_ACCOUNT_NOT_PINNED` **and records
  nothing at all** - no refusal row, no chain entry, no flag. Unpinned is the strictest state, and
  it is exercised in the demo and in tests against an explicit empty register rather than by
  assuming what the shipped register contains.
* Two of the three milestones remain unmet for every client (the review threshold and the
  commercial MSA), so **every client stays `STATUS_BETA_TESTER`** however the billing route reads.
* An unconfirmed balance edit is never billable (`UNCONFIRMED_ADJUSTMENT_BILLABLE = False`).

## Amendment — the reopen-audit gate on the payment path (added 2026-10-07)

This is not a lifecycle milestone and it has nothing to do with the evidence tables above.
It is the other state gate in the engine, and it is documented here because it is the same
kind of thing: **a refusal the claim transaction applies, that no mode can switch off.**

A bounced or refunded payment un-bills the fee by a linked counter-entry (the original
`collections_ledger` row is never edited) and puts the record in `Reopened_Awaiting_Audit`.
**A reversal is not a resume.** The runs the payment purged stay purged — no resurrection, no
new run, no sequence restart — and the only way back into normal flow is a completed re-audit
recorded in `reopen_audit_events`.

| | |
| --- | --- |
| state | `Reopened_Awaiting_Audit` (mandatory, CHECK-constrained in the store) |
| claim refusal | token `REOPENED_AWAITING_AUDIT`, written to `outbound_contact_logs.decision` with status `BLOCKED` and no claim token |
| asked where | inside the send-claim transaction, on every channel, in every operating mode — the state *and* the open audit row are both re-read there |
| release | a completed re-audit only: re-normalize the stored source row, re-resolve contact data, re-evaluate the calling window, re-check DNC/STOP, re-check paid status and balance ("`Reopened_Awaiting_Audit`" is cleared by `engine.payments.run_reopen_audit`, and nothing else) |
| after release | back in normal flow under the client's current mode (`hitl` by default), with a **fresh** human approval in `hitl` (any approval predates the reversal) and every guardrail unchanged |
| fail-closed | no source row, an unparseable source, a source that no longer identifies the same debtor, an unparseable amount, or an unattributed re-audit: refused and recorded as `reaudit_refused`, the record does not move, and it stays un-claimable |

**Order of the refusals** (`config/rulebook.json` → `guardrails.evaluation_order_at_claim`):
`dnc_flag` → `pending_adjustment_confirmation` → `reopened_awaiting_audit` → `balance_due` →
`calling_window` → `operating_mode_and_signoff` → `authorization_scope`. Two consequences worth
stating: an opt-out beats an open audit, and **the balance rule is not relaxed by any of this** —
a record whose balance is zero or a credit cannot be dunned with or without an audit (after the
audit the record derives back to `Paid` and is refused with `BLOCKED_NO_BALANCE_DUE`).

**Precedence with a hold (team decision, 2026-10-07): a reversal never exits a hold.** If the
record is already in `Pending_Adjustment_Confirmation` — or `Dispute_Hold`, once the dispute
interceptor lands; the rule already names it — the reversal still un-bills, still opens the audit,
and **leaves the state exactly where the human put it**. The audit row records both states and
`hold_state_kept = 1`, and the open audit keeps refusing claims until it is completed. Resolving
the hold does not satisfy the audit, and completing the audit does not release the hold: only a
human does that, with a reason.

What the reopen audit does **not** do: it writes no fee line, edits no ledger row, touches none of
the evidence tables above, and creates no schedule. It re-reads and records; it never resurrects.

Coverage: `tests/test_reopen_audit.py` (the whole invariant, including the held-record precedence
and the exact refusal token) and the reversal test in `tests/test_payments.py`.
