# The Agent 3 ↔ Vapi handshake (simulated)

Agent 3 POSTs to the voice provider only after the send claim has committed. In this
prototype the provider is the local mock at `POST http://127.0.0.1:8801/vapi/call`
(`mocks/server.py`). It validates the payload, writes it to `out/mock_vendor_calls/` and
returns a fake `call_id`. **No call is placed, no number is dialled, no message is left.**

Everything quoted on this page is copied out of a real run, not written by hand:

* the request is the byte-for-byte content of `out/vapi_payloads/6__CE-2001__voice.json`
  as produced by `python3 run_demo.py`
* the response is the byte-for-byte body the mock genuinely returned when that exact file
  was POSTed to it

Reproduce both yourself:

```bash
cd /home/team/shared/recoverai
python3 run_demo.py                                  # writes out/vapi_payloads/*.json
cat out/vapi_payloads/6__CE-2001__voice.json         # the request below
```

The one field that differs between two runs of the demo is `claim.claim_token` (a fresh
per-contact nonce). Everything else — the rendered message, the disclaimer, the
signature reference, the window decision, `claimed_at` — is byte-identical, because the
demo pins the clock (`RECOVERAI_FAKE_NOW`) and the signature reference is a hash over
pinned inputs.

## Request

`POST /vapi/call` · `Content-Type: application/json`

Every key below is required by the mock, which answers `422` with a `missing` list if any
of them is absent: `contact_type`, `destination_phone`, `debtor_id`, `client_id`,
`message`, `authorization` (including `authorization.signature_reference`) and `claim`
(including `claim.claim_token`).

Byte-for-byte, from `out/vapi_payloads/6__CE-2001__voice.json` (2176 bytes):

```json
{
  "provider": {
    "name": "Vapi (mock)",
    "simulated": true,
    "endpoint": "/vapi/call",
    "phone_number_id": "vapi-number-cli-cedar-01",
    "assistant_id": "assistant-recoverai-pastdue-v1"
  },
  "contact_type": "voice",
  "destination": "(303) 555-0171",
  "destination_phone": "(303) 555-0171",
  "debtor_id": "CE-2001",
  "client_id": "CLI-CEDAR",
  "template_reference": "past_due_notice/voice/v1",
  "subject": "Past-due balance on invoice XR-5501",
  "message": "Hello, this message is for Theo Lindqvist. This is Accounts Receivable calling from Cedar Ridge Dental Group about invoice XR-5501 for $12,340.00, which our records show is in the 90+ day aging bucket. We would like to arrange payment. Please call us back at (617) 555-0101 or reply to billing@cedar-ridge.example with your expected payment date. If payment has already been sent, thank you - please disregard this message.\n\nThis is an attempt to collect a debt. Any information obtained will be used for that purpose.",
  "disclaimer": {
    "text": "This is an attempt to collect a debt. Any information obtained will be used for that purpose.",
    "position": "absolute_bottom",
    "occurrences": 1
  },
  "authorization": {
    "signature_reference": "sha256:2a8b54985496283a6ae23523f4d995db488adacd0e0e936be32573c1b673e2ae",
    "source": "automation_signoff",
    "signer_name": "Dana Whitmore",
    "signer_title": "Chief Financial Officer",
    "document_version": "AUT-1.0",
    "signed_timestamp": "2026-10-06T14:00:00Z"
  },
  "claim": {
    "claim_token": "claim-e85c480289274e889d631a78238533c8",
    "contact_log_id": 6,
    "claimed_at": "2026-10-06T14:00:00Z",
    "operating_mode_at_claim": "full_auto",
    "mode_version_at_claim": 1
  },
  "guardrails": {
    "dnc_flag_at_claim": false,
    "window_decision": "In_Window",
    "window": "08:00-21:00 local",
    "derived_local_time": "2026-10-06T08:00:00-06:00",
    "area_code": "303",
    "tz_name": "America/Denver"
  },
  "rulebook_version": "1.0.0",
  "metadata": {
    "debtor_id": "CE-2001",
    "client_id": "CLI-CEDAR",
    "invoice_number": "XR-5501",
    "aging_state": "90",
    "balance_owed": 12340.0
  }
}
```

Reading it:

* `message` is the whole rendered notice, with the rulebook disclaimer as the last content
  and `disclaimer.occurrences == 1`. The `message` here is 518 bytes, which the mock echoes
  back as `echo.message_bytes`.
* `template_reference` says which Agent 2 template produced it, so a payload can be traced
  back to the notice that generated it.
* `authorization.source` is `automation_signoff` when a signed authorization permitted the
  contact (full_auto) and `human_approval_in_hitl` when a named human approved the queued
  draft; in that case `authorization.signature_reference` is `human-approval:<who>@<when>`
  so a hitl contact still links back to exactly what authorized it.
* `claim.*` is copied out of the committed claim row, not re-derived. `mode_version_at_claim`
  is what makes the mode-flip ordering checkable after the fact.
* `guardrails.*` is the re-derived state *inside* the claim transaction — window decision,
  resolved area code and timezone, and `dnc_flag` as read at that instant.
* `metadata.invoice_number` is the client ledger's own reference for the invoice (Xero calls
  the column `Reference`, QuickBooks `InvoiceNumber`); it is what the notice quotes.

The email channel uses the same envelope against the same claim, with
`contact_type: "email"` and `destination` set to the email address; the mock endpoint is
`POST /mock/email/send` and it answers `202` with a `message_id`.

## Response

`201 Created`, `Content-Type: application/json`, `Content-Length: 710` — the body below is
what the mock genuinely returned for the request above, byte-for-byte:

```json
{
  "call_id": "vapi-call-248977bc0277",
  "status": "queued",
  "provider": "vapi-mock",
  "simulated": true,
  "received_at": "2026-10-06T15:36:28Z",
  "echo": {
    "contact_type": "voice",
    "destination_phone": "(303) 555-0171",
    "debtor_id": "CE-2001",
    "client_id": "CLI-CEDAR",
    "template_reference": "past_due_notice/voice/v1",
    "signature_reference": "sha256:2a8b54985496283a6ae23523f4d995db488adacd0e0e936be32573c1b673e2ae",
    "claim_token": "claim-e85c480289274e889d631a78238533c8",
    "operating_mode_at_claim": "full_auto",
    "message_bytes": 518,
    "disclaimer_occurrences": 1
  },
  "notes": "Local mock. No call was placed, no phone number was dialled, no message left."
}
```

Notes on the response:

* `echo` repeats the fields the mock actually validated, from the request it received, so a
  stored response proves what was sent. `signature_reference` and `claim_token` are echoed
  verbatim: a contact's trace back to its authorization is never reconstructed later.
* `received_at` is stamped with the mock server's real wall clock (`datetime.now(UTC)`), not
  the demo's pinned clock — it records when the server genuinely handled the request, which
  is why it reads a little later than the `claimed_at` in the request.
* `call_id` is minted per call (`vapi-call-<12 hex>`), and the same id names the file in
  `out/mock_vendor_calls/`. `notes` says plainly that nothing was dialled.

The engine stores the whole response in `outbound_contact_logs.provider_response` and the
request in `out/vapi_payloads/<contact_log_id>__<debtor_id>__<channel>.json`.

## What the claim transaction guarantees — and what it does not

Guaranteed, because the claim and a mode flip write the same
`client_autonomy_settings` row inside their own transaction (`BEGIN IMMEDIATE` on SQLite,
`SELECT … FOR UPDATE` on Postgres):

* **No contact is claimed after a flip to `hitl` has committed.** The claim re-reads the
  mode inside its transaction; a claim that starts after the flip sees `hitl`, has no
  human approval, and is refused with `BLOCKED_MODE_HITL`.
* **A mode flip cannot slip between the check and the send.** There is no gap to slip
  into: the checks and the claim write are one transaction, and a flip blocks on the same
  row until that transaction ends.
* **Nothing is claimed unless every guardrail passed at that instant** — `dnc_flag`,
  the balance, the calling window (re-derived, not cached), the mode, the sign-off's
  validity and its scope.
* **A refused attempt writes no claim** (`claim_token` stays NULL); it is still logged
  with its `BLOCKED_*` decision, so a refusal is auditable.
* **A claim that committed first stands**, even if the flip lands a moment later: it was
  authorized under the mode actually in force when it was written. `mode_version_at_claim`
  records which mode version that was, so the ordering is checkable after the fact.

Not guaranteed:

* **A request already dispatched over the network cannot be recalled.** The claim commits,
  then the POST goes out. If a mode flip commits in the milliseconds after the claim but
  before the HTTP request completes, that request is still delivered. The guarantee is
  about what may be *claimed*, not about un-sending what is already on the wire — which is
  why the claim and the POST are as close together as they are, and why the mode switch
  reports which scheduled runs it halted.
* **The window's accuracy** depends on the area-code-to-timezone derivation, which is a
  proxy for where a mobile debtor actually is (see the business plan's known limitations).
* **The provider's behaviour** is not ours: the mock always accepts; a real provider can
  fail, delay or reorder. A dispatch failure is recorded as `status = FAILED` on an
  already-committed claim. The dispatch POST is bounded by
  `engine/orchestrator.py:DISPATCH_TIMEOUT` (10s) — a timeout is recorded as a loud
  failure, never retried silently and never allowed to block the send path.
