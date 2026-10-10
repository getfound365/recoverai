#!/usr/bin/env bash
# Daily-operations click-through for the RecoverAI console (Phase 3, pass 3).
#
# Drives the REAL page in a real browser, captures each moment the owner would click, and
# prints the counters it read back off the screen. Every number it reports is a value the
# engine sent; the harness computes none of them.
#
# Usage:  tools/console_daily_clickthrough.sh [base_url]
#         KEEP_STATE=1 tools/console_daily_clickthrough.sh   # keep the console store as it is
#
# THREE THINGS THIS SCRIPT KNOWS, each of them a defect it was written to stop repeating:
#
#  1. THE SESSION FLAG IS `--session <name>`, NOT `session=<name>`. The first version called
#     `agent-browser session=browser open ...`, and the CLI answered `Unknown command:
#     session=browser` and did nothing — for every single step, including the voice-run
#     scheduling step. The script still exited 0 and still echoed 13 "shot NN.png" lines, so
#     the run looked green while no PNG existed anywhere. Hence rule 2.
#  2. A SCREENSHOT THAT DID NOT LAND IS A FAILURE, NOT A LOG LINE. `shot()` checks the CLI's
#     exit status AND that a non-empty file exists, and exits 1 if either is wrong. EXIT=0
#     from this script therefore means all 13 PNGs are on disk; the final check re-counts
#     them from the filesystem instead of trusting the counter.
#  3. WAIT FOR HYDRATION, THEN SCROLL BEFORE CLICKING. React attaches handlers at hydration,
#     so a button that is live in the SSR HTML is inert until the client effect has run; and
#     a click on an element below the fold lands on empty space and silently does nothing.
#     Every click here scrolls first, every wait polls for a DOM change only a real state
#     update can produce, and every counter is read back afterwards (tools/console_daily_probe.js).
#     The readiness marker is `BROWSER_HASH_MATCHES_ENGINE` — the page computes the warranty
#     sha256 in the browser and renders that token only when it matches the engine's.
#
# The console never POSTs to a provider: `dispatch=False` is its permanent behaviour, so a
# claimed contact reads `claimed, not sent` and the claim itself is what the screen shows.
# See `docs/schema.md` (the daily-operations read models), `README.md` (the bridge ops table
# and the standing dispatch decision) and `site/README.md` (the daily-operations surfaces).
#
# Fresh state by default: the console store, its run queue and its action log are removed and
# re-seeded through the engine's own preview -> register -> commit path, so the counters are
# the same on every run and the script is safe to re-run.
#
# The run needs an in-window clock: the calling window is 08:00-21:00 debtor-local, and the
# script refuses to start unless at least one imported record is inside its own window right
# now (checked with the engine's own `timezones.window_decision_for`). Between 04:00 and
# 12:00 UTC every US debtor is asleep, so re-run later in the UTC day.
set -u

BASE="${1:-http://127.0.0.1:3000}"
ENGINE="${RECOVERAI_ENGINE_DIR:-/home/team/shared/recoverai}"
EVID="${EVID_DIR:-/home/team/shared/phase3-console-evidence}"
SESSION="${BROWSER_SESSION:-browser}"
PROBE_JS="$ENGINE/tools/console_daily_probe.js"
EXPECTED_SHOTS=13
KEEP_STATE="${KEEP_STATE:-0}"

mkdir -p "$EVID"
LOG="$EVID/clickthrough.log"
COUNTERS="$EVID/counters.txt"
STATE="$EVID/console-state.jsonl"
PROBE_RAW="$EVID/.probe.raw"
PROBE_CLEAN="$EVID/.probe.json"
: > "$LOG"
: > "$COUNTERS"
: > "$STATE"
SHOTS=0
PROBE="{}"

say()     { echo "== $*" | tee -a "$LOG"; }
note()    { echo "   $*" | tee -a "$LOG"; }
counter() { printf 'COUNTER %s=%s\n' "$1" "$2" | tee -a "$LOG" | tee -a "$COUNTERS"; }
fail()    { echo "FAIL: $*" | tee -a "$LOG" >&2; echo "EXIT=1 — evidence incomplete; do not read these PNGs as a pass"; exit 1; }

agent()  { agent-browser --session "$SESSION" "$@" >>"$LOG" 2>&1; }
evaljs() { agent-browser --session "$SESSION" eval "$1" 2>/dev/null; }

# jget FIELD — a dotted path out of the last probe's JSON.
jget() {
  python3 -c '
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    if value is None:
        break
    value = value[int(key)] if key.isdigit() else (value.get(key) if isinstance(value, dict) else None)
print("" if value is None else (json.dumps(value) if isinstance(value, (dict, list)) else value))
' "$PROBE_CLEAN" "$1"
}

# rx TEXT REGEX -> capture group 1, or empty.
rx() {
  python3 -c '
import re, sys
match = re.search(sys.argv[2], sys.argv[1])
print(match.group(1) if match else "")
' "$1" "$2"
}

# probe STEP — read every counter off the screen in one round and archive it as evidence.
probe() {
  local step="$1"
  evaljs "$(cat "$PROBE_JS")" > "$PROBE_RAW"
  PROBE=$(python3 - "$PROBE_RAW" "$PROBE_CLEAN" <<'PY'
import json, sys
raw = open(sys.argv[1]).read()
start, end = raw.find("{"), raw.rfind("}")
if start < 0 or end <= start:
    print(json.dumps({"probe_error": "no JSON object in the eval output", "raw": raw[-300:]}))
    sys.exit(0)
try:
    payload = json.loads(raw[start:end + 1])
except json.JSONDecodeError as exc:
    print(json.dumps({"probe_error": str(exc), "raw": raw[-300:]}))
    sys.exit(0)
json.dump(payload, open(sys.argv[2], "w"), indent=2, sort_keys=True)
print(json.dumps(payload))
PY
)
  echo "### $step" >> "$STATE"
  echo "$PROBE" >> "$STATE"
  if [ -n "$(jget probe_error)" ]; then
    fail "the DOM probe could not read the page at step '$step': $(jget probe_error) / $(jget raw)"
  fi
}

# wait_true JS_EXPR LABEL [TRIES] — poll until the expression is exactly true.
wait_true() {
  local expr="$1" label="$2" tries="${3:-60}" i=0 result=""
  while [ "$i" -lt "$tries" ]; do
    result=$(evaljs "$expr" | tail -1)
    if [ "$result" = "true" ]; then
      note "ready: $label (after $i poll(s))"
      return 0
    fi
    i=$((i + 1))
    sleep 0.5
  done
  fail "timed out waiting for $label — the last eval returned: ${result:-<nothing>}"
}

# wait_probe FIELD WANT LABEL [TRIES] — poll the DOM probe until a counter matches.
wait_probe() {
  local field="$1" want="$2" label="$3" tries="${4:-60}" i=0 got=""
  while [ "$i" -lt "$tries" ]; do
    probe "wait:$label"
    got="$(jget "$field")"
    if [ "$got" = "$want" ]; then
      note "ready: $label ($field=$got after $i poll(s))"
      return 0
    fi
    i=$((i + 1))
    sleep 0.5
  done
  fail "timed out waiting for $label — $field=$got, wanted $want"
}

# click SEL LABEL — scroll first (a below-the-fold click silently does nothing).
click() {
  local sel="$1" label="$2"
  note "click $label  [$sel]"
  agent scrollintoview "$sel" || fail "scrollintoview $sel failed"
  agent click "$sel" || fail "click $sel failed"
  sleep 1
}

# scroll_to SEL LABEL — bring a panel into frame for a screenshot.
scroll_to() {
  note "show $2  [$1]"
  agent scrollintoview "$1" || fail "scrollintoview $1 failed"
  sleep 1
}

# shot NAME — fatal unless a non-empty PNG really landed.
shot() {
  local name="$1" path="$EVID/$1"
  rm -f "$path"
  if ! agent-browser --session "$SESSION" screenshot "$path" >>"$LOG" 2>&1; then
    fail "screenshot $name: agent-browser exited non-zero (see $LOG)"
  fi
  if [ ! -s "$path" ]; then
    fail "screenshot $name: no PNG at $path (the CLI reported success but wrote nothing)"
  fi
  SHOTS=$((SHOTS + 1))
  note "shot $name ($(wc -c < "$path") bytes) [$SHOTS/$EXPECTED_SHOTS]"
}

bridge() { printf '%s' "$1" | python3 "$ENGINE/tools/console_bridge.py"; }

# --- fresh state ------------------------------------------------------------
reset_state() {
  say "reset the console store (fresh, deterministic counters)"
  rm -f "$ENGINE/data/console.db" "$ENGINE/data/console.db-journal" \
        "$ENGINE/out/console_runs.json" "$ENGINE/out/console_audit.jsonl"
}

seed_state() {
  say "seed the working queue through the engine's own preview -> register -> commit path"
  local out="$EVID/.seed.out"
  if ! python3 - "$ENGINE" > "$out" 2>&1 <<'PY'
import base64, csv, io, json, pathlib, subprocess, sys

engine = pathlib.Path(sys.argv[1])
bridge = engine / "tools" / "console_bridge.py"
sample = engine / "samples" / "spreadsheet" / "agency_past_due_sample.csv"


def call(request):
    proc = subprocess.run([sys.executable, str(bridge)], input=json.dumps(request),
                          capture_output=True, text=True)
    try:
        answer = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("FAIL: the bridge wrote no JSON: %r %r" % (proc.stdout[:200], proc.stderr[:200]))
        sys.exit(1)
    if not answer.get("bridge_ok"):
        print("FAIL: the bridge refused op %s: %s %s"
              % (request.get("op"), answer.get("error"), answer.get("detail")))
        sys.exit(1)
    return answer["payload"]


call({"op": "bootstrap"})
content = base64.b64encode(sample.read_bytes()).decode("ascii")
preview = call({"op": "preview", "file_name": sample.name, "content_b64": content,
                "uploader_id": "console-daily-clickthrough"})
rows_seen = preview.get("rows_seen")
registered = call({"op": "register", "preview": preview, "warranty_acknowledged": True,
                   "uploader_id": "console-daily-clickthrough", "who": "clickthrough-harness",
                   "reason": "daily-operations click-through: seed the working queue"})
batch_id = registered.get("batch_id") or (registered.get("record") or {}).get("batch_id")
if not batch_id:
    print("FAIL: register returned no batch_id: %s" % json.dumps(registered)[:300])
    sys.exit(1)
committed = call({"op": "commit", "preview": preview, "batch_id": batch_id,
                  "uploader_id": "console-daily-clickthrough", "who": "clickthrough-harness"})
rows_written = committed.get("rows_written")
if not rows_written:
    print("FAIL: commit wrote no rows: %s" % json.dumps(committed)[:300])
    sys.exit(1)

# The engine's OWN calling-window function decides which imported records could actually be
# claimed right now. Nothing is guessed here: `window_decision_for` is the same function the
# claim transaction uses, so the harness picks a dispatch target the engine would not refuse.
sys.path.insert(0, str(engine))
from engine import timezones  # noqa: E402

in_window = []
for row in csv.DictReader(io.StringIO(sample.read_text(encoding="utf-8"))):
    debtor_id = (row.get("Cust_ID") or "").strip()
    if not debtor_id:
        continue
    if timezones.window_decision_for(row.get("Cust_Phone") or "").get("in_window"):
        in_window.append(debtor_id)

print("COUNTER seeded_rows_seen=%s" % rows_seen)
print("COUNTER seeded_rows_written=%s" % rows_written)
print("COUNTER seeded_batch_id=%s" % batch_id)
print("COUNTER in_window_debtors=%s" % ",".join(in_window))
PY
  then
    cat "$out" >> "$LOG"
    fail "seeding the console store failed — $(tail -3 "$out" | tr '\n' ' ')"
  fi
  tee -a "$LOG" < "$out" | tee -a "$COUNTERS"
}

# --- 0. open the page -------------------------------------------------------
if [ "$KEEP_STATE" = "1" ]; then
  say "KEEP_STATE=1: using the console store as it stands"
else
  reset_state
  seed_state
fi

say "open $BASE/console"
agent close >/dev/null 2>&1 || true
sleep 1
agent open "$BASE/console" || fail "could not open $BASE/console — is the dev server up?"
wait_true 'document.body.textContent.includes("BROWSER_HASH_MATCHES_ENGINE")' \
          "page hydrated (browser-computed warranty hash matches the engine's)" 90
probe "01-load"
[ "$(jget bridge_error_banners)" = "0" ] || fail "the console could not read engine state on load"
counter queue_debtors_on_screen "$(jget stat_debtors)"
counter queue_row_count "$(jget queue_row_count)"
counter pending_drafts_before "$(jget stat_pending_drafts)"
counter scheduled_runs_at_start "$(jget stat_scheduled_runs)"
counter ledger_lines_before_payment "$(jget stat_ledger_lines)"
counter collected_before_payment "$(jget stat_collected)"
counter statement_status_before "$(jget stat_statement)"
counter full_auto_selectable "$(jget full_auto_selectable)"
counter mode_in_force "$(jget mode_in_force)"
shot 01-console-daily-ops-onboard.png

# --- the two debtors this run works with -----------------------------------
PAY_DEBTOR=$(bridge '{"op":"board"}' | python3 -c '
import json, sys
rows = [r for r in json.load(sys.stdin)["payload"]["queue"]["rows"]
        if r["account_state"] in ("Open", "Partially_Paid") and (r["balance_cents"] or 0) > 0]
rows.sort(key=lambda r: r["debtor_id"])
print(rows[0]["debtor_id"] if rows else "")')
[ -n "$PAY_DEBTOR" ] || fail "no record with a live balance exists, so the simulated payment has nothing to collect"
say "the simulated payment lands on $PAY_DEBTOR (the engine's own choice: first live balance by debtor_id)"

IN_WINDOW="$(grep '^COUNTER in_window_debtors=' "$COUNTERS" | tail -1 | cut -d= -f2-)"
APPROVE_DEBTOR="$(python3 - "$PROBE_CLEAN" "$PAY_DEBTOR" "$IN_WINDOW" <<'PY'
import json, sys
review_ids = [r["id"] for r in json.load(open(sys.argv[1]))["review_rows"]]
pay, in_window = sys.argv[2], sys.argv[3].split(",")
for candidate in review_ids:
    if candidate and candidate != pay and candidate in in_window:
        print(candidate)
        break
PY
)"
if [ -z "$APPROVE_DEBTOR" ]; then
  fail "every pending draft is either the payment target or outside its own calling window right now \
(in-window: ${IN_WINDOW:-none}). The window is 08:00-21:00 debtor-local, so re-run between 12:00 and 04:00 UTC \
to get an in-window dispatch target."
fi
say "approve + claim $APPROVE_DEBTOR — a DIFFERENT debtor from the payment target, so the purge empties visible work"
counter pay_debtor "$PAY_DEBTOR"
counter approve_debtor "$APPROVE_DEBTOR"

# --- 1. the queue -----------------------------------------------------------
say "the daily queue"
scroll_to '[data-testid="daily-queue-panel"]' "the daily queue panel"
shot 02-queue.png

say "refresh the queue (manual — there is no push channel)"
click '[data-testid="refresh-queue"]' "refresh-queue"
wait_true 'document.querySelector("[data-testid=refresh-queue]").textContent.indexOf("Refreshing") === -1' \
          "the refresh round-trip finished" 60
probe "02-queue-after-refresh"
counter queue_rows_after_refresh "$(jget queue_row_count)"
counter queue_status_counts "$(jget queue_rows | python3 -c 'import json,sys,collections; print(json.dumps(collections.Counter(r["status"] for r in json.load(sys.stdin))))')"
counter shift_note "$(jget shift_note)"
shot 03-queue-after-refresh.png

# --- 2. put work on the schedule so the halt has something to stop ----------
say "queue a voice run for $PAY_DEBTOR — the halt and the payment both have to reach it"
click "[data-testid=schedule-$PAY_DEBTOR]" "queue a voice run for $PAY_DEBTOR"
wait_probe stat_scheduled_runs "1" "the engine reports 1 scheduled run" 60
probe "03-run-scheduled"
counter scheduled_before_halt "$(jget stat_scheduled_runs)"
counter scheduled_runs_list_before_halt "$(jget scheduled_runs | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
counter schedule_header_before_halt "$(jget schedule_header)"
shot 04-run-scheduled.png

# --- 3. the human gate: approve one draft (NOT the payment target) ----------
say "approve the draft for $APPROVE_DEBTOR"
scroll_to '[data-testid="review-panel"]' "the review panel"
shot 05-review-queue.png
PENDING_BEFORE="$(jget stat_pending_drafts)"
click "[data-testid=approve-$APPROVE_DEBTOR]" "approve $APPROVE_DEBTOR"
wait_probe review_row_count "$((PENDING_BEFORE - 1))" "the approved draft left the pending list" 60
probe "04-approved"
PENDING_AFTER="$(jget stat_pending_drafts)"
counter pending_drafts_before_approve "$PENDING_BEFORE"
counter pending_drafts_after_approve "$PENDING_AFTER"
counter approval_refusals "$(jget refusal_banners)"
[ "$PENDING_AFTER" -lt "$PENDING_BEFORE" ] || fail "approving $APPROVE_DEBTOR did not reduce the pending count ($PENDING_BEFORE -> $PENDING_AFTER)"
shot 06-draft-approved.png

# --- 4. Agent 3: the atomic claim ------------------------------------------
say "run Agent 3 — the atomic claim-then-stop"
click '[data-testid="dispatch-approved"]' "Run Agent 3 now"
wait_true 'document.querySelector("[data-testid=dispatch-result]") !== null' "the dispatch result panel rendered" 90
probe "05-claim"
DISPATCH="$(jget dispatch_result)"
ATTEMPTED="$(rx "$DISPATCH" 'Agent 3 ran: ([0-9]+) attempted')"
CLAIMED="$(rx "$DISPATCH" '([0-9]+) claim\(s\) committed')"
REFUSED="$(rx "$DISPATCH" '([0-9]+) refused')"
counter claim_attempted "$ATTEMPTED"
counter claim_claimed "$CLAIMED"
counter claim_refused "$REFUSED"
counter claim_lines "$(jget dispatch_lines | python3 -c 'import json,sys; print("; ".join(json.load(sys.stdin)))')"
counter claim_dispatch_note "$DISPATCH"
[ "${CLAIMED:-0}" -ge 1 ] || fail "no claim was committed for $APPROVE_DEBTOR — the screen reads: $DISPATCH"
case "$DISPATCH" in *"claimed, not sent"*) ;; *) fail "the screen does not say 'claimed, not sent': $DISPATCH" ;; esac
shot 07-claim.png

# --- 5. switch to manual: the atomic halt ----------------------------------
say "switch to manual — the atomic halt across the whole schedule"
click '[data-testid="halt-to-manual"]' "Switch to Manual (HITL)"
wait_true 'document.querySelector("[data-testid=halt-result]") !== null' "the halt result panel rendered" 60
probe "06-halt"
HALT="$(jget halt_result)"
counter halted_run_count "$(rx "$HALT" 'Halt applied: ([0-9]+) scheduled run\(s\) stopped')"
counter scheduled_after_halt "$(jget stat_scheduled_runs)"
counter scheduled_runs_list_after_halt "$(jget scheduled_runs | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
counter halt_result "$HALT"
counter mode_in_force_after_halt "$(jget mode_in_force)"
counter mode_timeline_rows "$(jget timeline_rows | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
[ "$(grep '^COUNTER halted_run_count=' "$COUNTERS" | tail -1 | cut -d= -f2)" -ge 1 ] || fail "the halt stopped no run: $HALT"
[ "$(jget stat_scheduled_runs)" = "0" ] || fail "runs are still scheduled after the halt: $(jget stat_scheduled_runs)"
shot 08-halt.png

# --- 6. the interaction history drawer -------------------------------------
say "interaction history for $APPROVE_DEBTOR (read-only, over the real contact log)"
click "[data-testid=history-$APPROVE_DEBTOR]" "open the history drawer"
wait_true 'document.querySelector("[data-testid=history-drawer]") !== null' "the drawer opened" 40
probe "07-history"
counter history_entries "$(jget history_entries)"
counter history_header "$(jget history_header)"
shot 09-interaction-history.png
click '[data-testid="close-history"]' "close the drawer"
wait_true 'document.querySelector("[data-testid=history-drawer]") === null' "the drawer closed" 30

# --- 7. the alert feed ------------------------------------------------------
say "system alerts, read from the engine's append-only action log"
scroll_to '[data-testid="alerts-panel"]' "the alert feed"
click '[data-testid="refresh-alerts"]' "refresh alerts"
wait_true 'document.querySelectorAll("[data-testid=alert-entry]").length > 0' "the alert feed rendered entries" 60
probe "08-alerts"
counter alerts_entries "$(jget alerts_total)"
counter alerts_high_priority "$(jget alerts_high)"
counter alerts_sample "$(jget alert_sample | python3 -c 'import json,sys; print(" | ".join(json.load(sys.stdin)))')"
shot 10-alerts.png

# --- 8. the money trail + the simulated payment ----------------------------
say "the money trail, before the payment"
scroll_to '[data-testid="money-panel"]' "the money trail"
probe "09-money-before"
LEDGER_BEFORE="$(jget stat_ledger_lines)"
shot 11-money-before.png

say "simulate a payment on $PAY_DEBTOR (the engine's real payment path; no money moves)"
click '[data-testid="simulate-payment"]' "Simulate a payment (webhook)"
wait_true 'document.querySelector("[data-testid=simulation-result]") !== null' "the close-the-loop panel rendered" 90
probe "10-payment"
SIM="$(jget simulation_result)"
PURGED="$(rx "$SIM" 'Queue purged: ([0-9]+) scheduled contact')"
FEE="$(rx "$SIM" '20% fee (\$[0-9.,]+)')"
ENTRY="$(rx "$SIM" 'Ledger line appended: entry ([0-9]+)')"
counter payment_debtor "$PAY_DEBTOR"
counter payment_queue_purged_runs "${PURGED:-0}"
counter payment_ledger_entry "$ENTRY"
counter payment_fee "$FEE"
counter ledger_lines_before_payment "$LEDGER_BEFORE"
counter ledger_lines_after_payment "$(jget stat_ledger_lines)"
counter collected_after_payment "$(jget stat_collected)"
counter statement_status_after_payment "$(jget stat_statement)"
counter scheduled_runs_after_payment "$(jget stat_scheduled_runs)"
counter ledger_rows_on_screen "$(jget ledger_rows | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
counter simulation_result "$SIM"
[ "${PURGED:-0}" -ge 1 ] || fail "the payment purged no scheduled work on $PAY_DEBTOR: $SIM"
shot 12-payment-close-the-loop.png

say "the ledger line and the DRAFT statement"
scroll_to '[data-testid="statement-card"]' "the ledger line and the statement"
probe "11-ledger-statement"
counter statement_card "$(jget statement_card)"
counter ledger_rows_final "$(jget ledger_rows | python3 -c 'import json,sys; print("; ".join(json.load(sys.stdin)))')"
counter alerts_after_payment "$(jget alerts_total)"
counter alerts_high_after_payment "$(jget alerts_high)"
counter queue_status_counts_after_payment "$(jget queue_rows | python3 -c 'import json,sys,collections; print(json.dumps(collections.Counter(r["status"] for r in json.load(sys.stdin))))')"
shot 13-ledger-and-draft-statement.png

# --- final check: the evidence is on disk, and the numbers add up ----------
say "verify the evidence on disk and the arithmetic"
if ! python3 - "$EVID" "$COUNTERS" "$EXPECTED_SHOTS" <<'PY'
import json, pathlib, re, sys

evidence, counters_path, expected = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
counters = {}
for line in counters_path.read_text().splitlines():
    if line.startswith("COUNTER ") and "=" in line:
        key, _, value = line[len("COUNTER "):].partition("=")
        counters[key.strip()] = value.strip()

shots = sorted(evidence.glob("*.png"))
problems = []
if len(shots) != expected:
    problems.append("expected %d PNGs, found %d" % (expected, len(shots)))
for path in shots:
    if path.stat().st_size == 0:
        problems.append("%s is empty" % path.name)


def number(key):
    match = re.search(r"-?\d+", counters.get(key, "").replace(",", ""))
    return int(match.group(0)) if match else None


def money(key):
    match = re.search(r"\(?\$?([0-9][0-9,]*\.\d+)", counters.get(key, ""))
    return float(match.group(1).replace(",", "")) if match else None


pending_before, pending_after = number("pending_drafts_before_approve"), number("pending_drafts_after_approve")
if pending_before is None or pending_after is None or pending_after >= pending_before:
    problems.append("pending drafts did not fall on approval: %s -> %s" % (pending_before, pending_after))
if number("claim_claimed") is None or number("claim_claimed") < 1:
    problems.append("no claim was committed")
if "claimed, not sent" not in counters.get("claim_dispatch_note", ""):
    problems.append("the dispatch note does not say 'claimed, not sent'")
if number("halted_run_count") is None or number("halted_run_count") < 1:
    problems.append("the halt stopped no run")
if counters.get("scheduled_after_halt") != "0":
    problems.append("runs still scheduled after the halt: %s" % counters.get("scheduled_after_halt"))
if number("payment_queue_purged_runs") is None or number("payment_queue_purged_runs") < 1:
    problems.append("the payment purged no scheduled work")
if counters.get("scheduled_runs_after_payment") != "0":
    problems.append("scheduled runs are not 0 after the purge: %s" % counters.get("scheduled_runs_after_payment"))
if counters.get("ledger_lines_before_payment") != "0" or counters.get("ledger_lines_after_payment") != "1":
    problems.append("ledger line count is not 0 -> 1: %s -> %s"
                    % (counters.get("ledger_lines_before_payment"), counters.get("ledger_lines_after_payment")))
fee, collected = money("payment_fee"), money("collected_after_payment")
if fee is None or collected is None or abs(fee - round(collected * 0.20, 2)) > 0.001:
    problems.append("the fee is not 20%% of what was collected: fee=%s collected=%s" % (fee, collected))
if counters.get("statement_status_after_payment") != "DRAFT":
    problems.append("the statement is not DRAFT: %s" % counters.get("statement_status_after_payment"))
if number("alerts_entries") in (None, 0):
    problems.append("the alert feed rendered no entries")
if number("history_entries") is None or number("history_entries") < 1:
    problems.append("the history drawer rendered no contact entry for the approved debtor")

print(json.dumps({"pngs": len(shots), "problems": problems}, indent=2))
if problems:
    sys.exit(1)
print("VERIFIED: %d PNGs on disk in %s and every counter checks out" % (len(shots), evidence))
PY
then
  fail "the final verification did not pass (see the JSON above)"
fi

say "done — $SHOTS/$EXPECTED_SHOTS screenshots in $EVID; counters in $COUNTERS; every probe archived in $STATE"
