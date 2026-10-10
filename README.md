# RecoverAI — accounts-receivable engine (sandbox prototype)

A compliant, client-controlled accounts-receivable pipeline. Three agents run here —
ingestion, compliant drafting, and the atomic send orchestrator — over one shared
rulebook, with the guardrails that hold in **every** operating mode.

This repository is the engine core. The web console, Agent 4's reporting and the system
map are separate, later pieces of work.

## Quickstart (5 commands)

```bash
cd /home/team/shared/recoverai

# 1. the whole test suite (no pytest needed - stdlib unittest)
python3 -m unittest discover -s tests -t . -v

# 2. one full end-to-end cycle against the local mocks, ~8 debtors, 7 scenarios
python3 run_demo.py

# 3. read the transcript it wrote (mapping report, every decision, the DB state)
cat out/run_log.txt

# 4. the exact JSON Agent 3 POSTs to the voice provider
cat out/vapi_payloads/*__voice.json

# 5. run the mock vendor + engine API servers by hand and poke them with curl
python3 -m mocks.server
```

One `run_demo.py` cycle clears `out/`, fetches both simulated exports over HTTP, runs all
seven scenarios and leaves a verified set of artifacts behind: `run_log.txt` (the full
transcript, ending in a closing summary), `guardrail_actions.jsonl`, `scheduled_runs.json`,
`db_state.json` (all four tables) and `summary.json`, plus `drafts/`, `vapi_payloads/`,
`email_outbox/` and `mock_vendor_calls/`. The run ends with an `artifact check` section
that names every one of them and its size, so a truncated or missing file is visible in the
transcript itself rather than having to be noticed by hand. If a scenario cannot run, the
transcript is still written, with a `DEMO FAILED` section quoting the traceback, and the
process exits non-zero.

Nothing to install for the engine, the demo or the tests: Python 3.9+ and the standard
library are enough. The store defaults
to SQLite at `data/recoverai.db` (created on first run). If `DATABASE_URL` is set **and** a
Postgres driver is importable, the same SQL runs against Postgres instead.

**One optional dependency, for one feature:** reading a client's binary `.xlsx` upload needs
`openpyxl` (installed here as 3.1.5, alongside `et-xmlfile`, with
`python3 -m pip install --break-system-packages openpyxl` — the sandbox Python is an
externally-managed environment, so pip needs that flag). It is imported **lazily**, only when
an `.xlsx` file is actually parsed: every other path, including the CSV upload path, runs on
the standard library alone, and a missing `openpyxl` produces a plain refusal naming the
install command rather than an ImportError.

Two environment knobs the demo and the tests use:

```bash
RECOVERAI_FAKE_NOW=2026-10-06T14:00:00Z python3 run_demo.py   # pin the clock
RECOVERAI_DB=/tmp/x.db python3 run_demo.py                    # put the store elsewhere
```

## What it does

| Agent | Module | Job |
| --- | --- | --- |
| 1 — Ledger Ingestion | `engine/ingest.py` | Fetches a client export over HTTP and translates whatever its columns are called (`Cust_Name`, `AmountDue`, `Balance`, `DueDate`, `ContactID`, …) into one normalized schema, reporting which column mapped where. Also takes the client's own CSV/Excel file through the *same* translator, in a two-step preview → commit that writes nothing until an immutable `ingestion_batches` record clears it. |
| 2 — Compliance Draftsman | `engine/draft.py` | Renders a firm, professional past-due notice and appends the required disclaimer verbatim, as the last content, exactly once. |
| 3 — Webhook Orchestrator | `engine/orchestrator.py` | Holds the draft and, at the last moment before the POST, runs one atomic read-and-claim on the client's operating mode plus every guardrail. |
| 4 — Customer Success | *not in this repository* | Onboarding hints, drop-out tracking, the executive monthly summary. |

Supporting pieces: `engine/onboarding.py` (digital sign-off, mode switching), `engine/
webhooks.py` (the inbound DNC opt-out), `engine/runqueue.py` (scheduled runs), `engine/
timezones.py` (area code → local time), `engine/audit.py` (append-only action log).

### The four non-negotiable rules, and where they live

1. **Nothing real happens.** QuickBooks, Xero, Vapi and email are local mocks
   (`mocks/server.py`). No call, no email, no money movement, no third-party API.
2. **Calling window 08:00–21:00 local, derived from the phone area code.** Implemented in
   `engine/timezones.py`, re-derived inside the claim transaction in
   `engine/orchestrator.py`. Out of window (or an area code we cannot resolve) the run
   freezes and `approval_status` becomes exactly `Outside_Calling_Window`.
3. **Global DNC opt-out.** `STOP`, `Unsubscribe`, `Stop calling` — case-insensitive
   substring, in `engine/webhooks.py` — sets `dnc_flag`, cancels every scheduled run for
   that debtor, and blocks every future claim.
4. **Claim-then-fire, never check-then-fire.** One transaction re-reads the mode,
   re-validates the sign-off, re-checks DNC and the window, checks the sign-off's scope,
   and writes the send-claim row. Only a committed claim permits the POST. A mode flip and
   a claim write the same `client_autonomy_settings` row, so they can never interleave.

## The spreadsheet intake (Phase 3)
A client's own CSV/Excel export is a first-class way in, and it is deliberately **not** a second
mapping engine. Both steps run Agent 1's existing translator (`config/ingestion_aliases.json`,
matched case-insensitively and ignoring punctuation) and its existing `normalize()` + `persist()`
— the same code a live QuickBooks/Xero export goes through.

```python
preview = engine.preview_spreadsheet(file_name="aged.csv", content=csv_text,
                                     client_id="CLI-CEDAR", uploader_id="dana@cedar.example")
# -> mapping report, per-row status, dropped rows with exact reason tokens, totals. Writes NOTHING.

batch = engine.register_ingestion_batch(preview, uploader_id="dana@cedar.example",
                                        warranty_acknowledged=True, reason="onboarding file")
# -> the immutable ingestion_batches row (file sha256, counts, millisecond-UTC stamp, uploader,
#    the Step 3 warranty bound to the client's signed reference). Refused without the tick.

engine.commit_spreadsheet(preview, batch_id=batch["batch_id"])
# -> rows written through the shared normalizer. Refused if no record exists, if the record
#    does not match the preview byte-for-byte, or if the batch was already committed.
```

* **Nothing is coerced, invented or silently overwritten.** A non-numeric amount
  (`AMOUNT_NOT_NUMERIC`), an unparseable date (`UNPARSEABLE_DATE`), a zero or credit balance
  (`BLOCKED_NO_BALANCE_DUE`), a blank amount (`MISSING_BALANCE`) and a row that does not line up
  with its header (`ROW_SHAPE_MISMATCH`) each drop the row with that one reason, reported with its
  row index. A missing phone or email imports and is **flagged** (`MISSING_PHONE`,
  `MISSING_EMAIL`), never placeholder-filled — and the missing number fails closed at the calling
  window exactly as it does for a live import. An unmapped column is reported, never guessed into
  a field; two columns claiming one target field is a mapping error, never a silent overwrite.
* **The consumer-debt scanner is advisory and is POLICY, not law.** It flags two things —
  individual-person name structures versus entity identifiers, and balances drifting under $100
  (the owner-specified signal) — and carries the warning
  *"Warning: Data footprint resembles consumer debt. RecoverAI operates strictly on B2B
  commercial trade debt."* It never blocks, drops or edits a row. It exists because medical/dental
  and similar balances are usually consumer debt and need counsel before any real campaign.
* **`ingestion_batches` is append-only and hash-chained** like `billing_route_verifications`:
  `chain_hash = sha256(prev_chain_hash | batch_sha256 | batch_id)`. It is **tamper-evident, not
  signature-proof** — a hash proves content and order, not authorship.
  `engine.verify_ingestion_batches()` recomputes the chain from the rows themselves.
* Full detail — every token, every column, the honesty note — is in `docs/schema.md`.

## The console bridge (`tools/console_bridge.py`)

The web console is a separate tree (`/home/team/shared/site`), and it reaches this engine
through exactly one door: a short-lived process that reads ONE JSON request on stdin and
writes ONE JSON response on stdout. It holds no intake logic — every op is a thin dispatch
onto `engine.app.RecoverAI`, so mapping, validation, the consumer-debt scan, the
hash-chained batch record and the commit preconditions all stay in `engine/ingest.py` and
there is only ever one answer.

```bash
echo '{"op":"status"}' | python3 tools/console_bridge.py
echo '{"op":"sample"}' | python3 tools/console_bridge.py      # the console's sample CSV, as base64
echo '{"op":"vocabulary"}' | python3 tools/console_bridge.py  # reason tokens, read out of the rulebook
```

| Op | What it answers |
| --- | --- |
| `status` | The sandbox client's sign-off state, mode, lifecycle, batch count, queue rows, net collected off the ledger, and the batch-chain verification. |
| `bootstrap` | `status` plus what the bootstrap had to create (idempotent). |
| `preview` | The full reconciliation payload for an uploaded file — mapping report, per-row status, dropped rows with their tokens. Writes nothing. |
| `register` | The immutable `ingestion_batches` row. Refused without the Step 3 acknowledgement. |
| `commit` | The rows written into the working queue. Refused with no batch, on a preview that has drifted, or on a second commit. |
| `batches` / `verify` | The batch records, and the chain recomputed from the rows themselves. |
| `sample` | The engine's own sample CSV as bytes (`samples/spreadsheet/agency_past_due_sample.csv`), so the frontend never carries a copy of a mock row. |
| `vocabulary` / `warranty` | The reason-token legend and the exact Step 3 warranty text plus its sha256, so the console's legend and the checkbox it renders can never drift from what the engine enforces. |
| `board` | Everything the daily console needs, from ONE engine process in one read: the queue, the mode view, the review queue, the money trail and the alert feed, all read at the same instant. |
| `daily_queue` / `mode` / `reviews` / `money` | The same daily-operations read models one panel at a time (the board composes exactly these). |
| `set_mode` | Switch operating mode, both directions. `full_auto` is refused by the ENGINE, not the UI, when no valid signed authorization is on file — the refusal arrives as `BLOCKED_NO_SIGNOFF`. |
| `halt` | THE atomic halt behind *Switch to Manual (HITL)*: the client's mode to `hitl` **and** every `scheduled` run for that client halted, in one call, audited. It runs even when the client is already in `hitl`, and it is not a delete: halted runs stay on the queue in state `halted` with their reason. |
| `decide` | Approve or reject one queued draft through the real approval path; a refusal carries the engine's own token (`BLOCKED_DNC`, `REOPENED_AWAITING_AUDIT`, `BLOCKED_OUTSIDE_WINDOW`). |
| `schedule_run` | Put one contact on the schedule, so the halt (and a payment) has something to reach. |
| `dispatch_approved` | Agent 3's turn: the atomic claim for every approved record. Guardrails are re-read inside the transaction, so a refusal carries its token. **`dispatch=False`, permanently — see below.** |
| `interactions` | Every contact attempt for one debtor, read back from `outbound_contact_logs` plus the exact payload file the claim wrote before the POST. Read-only. |
| `alerts` | The engine's own append-only action log in plain English, newest first. |
| `simulate_payment` | The engine's real payment path (`RecoverAI.record_payment` → `payments.apply_payment`) against a simulated accounting webhook. Balance debit, queue purge, fee ledger line — and no money moves. |

### The standing decision: the console claims, it never sends

**`dispatch=False` is the console's permanent behaviour** (team decision, 2026-10-09). The
console re-reads every guardrail inside the claim transaction, commits the claim, writes the
payload the contact *would* have carried — and **never POSTs to a provider**. The screen says
`claimed, not sent`, and it says it because it is true, not because a vendor happens to be
unreachable: a claim written up as a send that did not happen would be a lie, and the
simulated vendors are not reachable from every host that serves the page. The contact log
keeps status `CLAIMED`, never `SENT`. `RecoverAI.send(..., dispatch=True)` still exists and
still honours the flag — the claim-then-fire send moment belongs to the future `/demo` tour,
where the mock vendor is local to the engine. The console does not pass it.

### The run queue is a queue of record (`out/console_runs.json`)

The bridge is one short-lived process per request, so an in-memory queue would be empty on
every read and *Switch to Manual* would have nothing to halt.
`RecoverAI(runqueue_path=...)` loads the queue on open and **`engine.close()`** — the facade,
not `store.close()` — writes it back. `RunQueue.dump` is atomic (sibling `.tmp` then
`os.replace`), and `RunQueue.load` treats an unreadable file as a hard error rather than
silently returning an empty queue: dropping scheduled work on the floor because a file was
malformed is the one failure a collections queue must not have.
`tests/test_daily_ops.py::BridgeAcrossProcessesTests` is the regression guard — two bridge
processes, one schedule.

**Contract:** the bridge always exits 0 with a JSON object on stdout. `{"bridge_ok": true,
"payload": …}` when the engine answered — and an engine REFUSAL arrives inside `payload`,
because a refusal is an answer, not a bridge failure. `{"bridge_ok": false, "error": …,
"detail": …}` when the bridge itself could not run the op; the console renders that plainly
rather than guessing. It works from any working directory (the repo root is inserted into
`sys.path` from `__file__`), which matters because the site's SSR process spawns it with a
`cwd` of its own choosing.

**The sandbox client bootstrap.** The console operates as `CLI-SANDBOX-AGENCY`
(override with `RECOVERAI_CONSOLE_CLIENT`), a **fixture**. Any op that needs a client calls
`_bootstrap()`, which creates the settings row and, if the sign-off is missing or invalid,
captures one signed by "Sandbox Operator" with the title *"Simulated signer - sandbox
console fixture, not a real client authorization"*. It is idempotent, and it writes an audit
line saying exactly that — so the console can demonstrate the full register/commit gate
without anyone mistaking a fixture for a real client or a real authorization.

**Its store is its own file: `data/console.db`, not `data/recoverai.db`.** That separation is
deliberate, not tidiness:

* `run_demo.py` opens `data/recoverai.db` with `fresh=True`, which **deletes** it. Console
  state living there would be wiped by every demo run, and a console that silently forgets
  every batch it registered is worse than no console.
* The demo's export (`db_state.json`) walks a fixed table list and expects demo-shaped data.
  Rows a human clicked in through the wizard would show up in the demo's own summary as if
  the demo had imported them — a false claim about what the run did.
* The console store is a separate SQLite file with the same schema (`engine/schema.sql`), so
  the same code, triggers and chain rules apply. Point it elsewhere with
  `RECOVERAI_CONSOLE_DB`; the audit trail lands in `out/console_audit.jsonl`
  (`RECOVERAI_CONSOLE_AUDIT`). Both are readable in the console's own engine-state strip.

## Deliberate design choices (where the brief was open)

The brief fixes the guardrails but leaves plenty of detail open. These are the calls the
engine makes, so they are decisions to review rather than behaviour to discover:

1. **An unknown or unparseable area code fails closed, and says so in two places.** The run
   freezes with `approval_status = Outside_Calling_Window` — the same token as a genuine
   out-of-window freeze, because that is the token the plan specifies — while
   `window_decision` on the record and in `outbound_contact_logs` stores the precise
   reason, `Unknown_Area_Code_Freeze`. So the coarse status token stays stable for anything
   watching it, and the audit trail still distinguishes "we know it is 04:00 there" from
   "we cannot tell where the debtor is". We never guess a timezone, and we never call.
2. **A zero or credit balance is never contacted.** `balance_owed <= 0` (or unparseable)
   yields `BLOCKED_NO_BALANCE_DUE` before the window, mode or scope checks run. Chasing a
   credit balance would be demanding money that is not owed. `(320.75)` in a QuickBooks
   export is parsed as the credit it is, so it lands here rather than being called.
3. **A refused `full_auto` attempt is still appended to the timeline, with `new_mode` set to
   the mode actually in force.** The attempt must be auditable, but the timeline must never
   claim a switch that did not happen: the row reads `hitl -> hitl` with a reason prefixed
   `REFUSED attempt to switch to full_auto`. An append-only history that recorded a lie
   about the client's mode would be worse than no history.
4. **Email is not a windowed channel, but it is still scope-limited.** `08:00–21:00` applies
   to voice/SMS only (`channels.*.windowed` in the rulebook), so a debtor at 04:00 local can
   be emailed — but only if the sign-off's `channel × aging_state` scope authorizes it, and
   only through the same atomic claim. Email gets no exemption from DNC, the balance check,
   the sign-off gate or the claim transaction.
5. **The authorizing reference has two shapes, and every contact carries exactly one.**
   In `hitl` it is `human-approval:<who>@<when>` — the named person and the instant they
   approved. In `full_auto` it is the sign-off's `sha256:` reference: a hash over
   `client_id + signer_name + signer_title + document version + timestamp`. Either way the
   contact links back to the specific thing that authorized it, and
   `authorization.source` records which kind it was.
6. **An ambiguous source column is resolved in favour of the invoice, not the debtor.**
   Xero exports call the invoice reference `Reference`; some CRMs use that name for the
   debtor's account. The alias table now reads `Reference` as `invoice_number`, because a
   Xero export always carries `ContactID` for the debtor, and the notice has to quote the
   client's own invoice reference to be useful. `ClientRef`/`Customer_Ref`/`Account_Number`
   remain debtor-id aliases for exports that do use them.

## What is simulated here

Everything external. To be explicit:

* **QuickBooks Online and Xero** — `GET /mock/quickbooks/invoices` and
  `GET /mock/xero/invoices` on `127.0.0.1:8801`, serving the two hand-written export files
  in `mocks/data/`. No OAuth, no tenant, no real ledger.
* **Vapi** — `POST /vapi/call` on `127.0.0.1:8801`. It validates the payload, writes it to
  `out/mock_vendor_calls/` and returns a fake `call_id`. No phone number is dialled and no
  message is left.
* **Email** — `POST /mock/email/send` writes a JSON file to `out/email_outbox/`. No mail
  server is contacted and nothing is delivered.
* **The debtors, clients and invoices** — invented. `config/clients.json` and
  `mocks/data/*.json` contain no real business or person.
* **The authorization wording** — drafted by the team, not reviewed by counsel.
* **Area-code-derived local time** — the specified method, and its known weak point: an
  area code does not reliably track where a mobile debtor is. Every record stores the
  derived local clock and the decision, so the choice is auditable, but a real deployment
  should source the timezone from billing address or carrier data.
* **Postgres** — the schema and the claim transaction are written for it, but no Postgres
  driver is installed in this sandbox, so that path has never been executed. The default
  SQLite path is the one that runs.

## Layout

```
recoverai/
├── README.md
├── config/            rulebook.json (single source of truth), ingestion_aliases.json, clients.json
├── engine/            the pipeline, guardrails, store, schema.sql
├── mocks/             mock vendor + engine API servers, and the messy export data
├── data/              SQLite database (gitignored, created on first run)
├── tests/             compliance, store, onboarding, ingestion, payload, race and mock-server tests
├── docs/              architecture.md, schema.md, vapi-handshake.md
├── out/               run artifacts: run_log.txt, guardrail_actions.jsonl, scheduled_runs.json,
│                      db_state.json, summary.json, drafts/, vapi_payloads/, email_outbox/,
│                      mock_vendor_calls/
└── run_demo.py        the documented end-to-end demo
```

## Docs

* `docs/architecture.md` — the pipeline, a Mermaid diagram of the claim point, and the
  guardrails in detail.
* `docs/schema.md` — every table and column.
* `docs/vapi-handshake.md` — the exact request JSON and the exact response body, plus what
  the claim transaction guarantees and what it does not.
