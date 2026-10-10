#!/bin/bash
# Phase 3 pass-2 close-out: real browser click-through of the Spreadsheet Upload Wizard
# against the live engine on :3000. Every counter printed here is read out of the DOM
# after the click, never assumed from the click's own "done".
#
# Two harness rules learned the hard way, both recorded in the pass-2 report:
#  1. WAIT FOR HYDRATION before clicking. The sample button is now enabled in the SSR
#     HTML (that is the fix), so "is the button enabled" no longer proves React is live.
#     The marker used here is BROWSER_HASH_MATCHES_ENGINE, which only ever appears once
#     the client effect has run and computed the warranty sha256 in the page.
#  2. SCROLL BEFORE CLICKING. Every wizard control sits well below the fold; a click
#     dispatched without scrolling first lands on empty space and silently does nothing.
set -u
EVID=/home/team/shared/phase3-pass2-evidence
mkdir -p "$EVID"
OUT=/tmp/clickthrough.out
: > "$OUT"
log() { echo "$@" >> "$OUT"; }

EVAL_PROBE='
JSON.stringify({
  hydrated: document.body.textContent.includes("BROWSER_HASH_MATCHES_ENGINE"),
  sampleDisabled: document.querySelector("[data-testid=load-sample]").disabled,
  sampleLabel: document.querySelector("[data-testid=load-sample]").textContent.trim(),
  previewDisabled: document.querySelector("[data-testid=run-preview]").disabled,
  registerDisabled: document.querySelector("[data-testid=register-batch]").disabled,
  commitDisabled: document.querySelector("[data-testid=commit-import]").disabled,
  gateState: document.querySelector("[data-testid=gate-state]").textContent.trim().slice(0, 60),
  grid: document.querySelectorAll("[data-testid=reconciliation-grid] tbody tr").length,
  dropped: document.querySelectorAll("[data-dropped-row]").length,
  tokens: Array.from(document.querySelectorAll("[data-dropped-row]")).map(function(e){return e.getAttribute("data-dropped-token")}),
  batchId: document.querySelector("[data-testid=batch-record]") ? document.querySelector("[data-testid=batch-record] dd").textContent : null,
  commitRowsWritten: document.querySelector("[data-testid=commit-result]") ? document.querySelector("[data-testid=commit-result] dd").textContent : null,
  committedIds: document.querySelector("[data-testid=commit-result] p.font-mono") ? document.querySelector("[data-testid=commit-result] p.font-mono").textContent.trim() : null,
  consumerMarker: document.body.textContent.includes("Advisory: consumer-shaped data footprint"),
  consumerText: document.querySelector("[data-testid=consumer-debt-warning-text]") ? document.querySelector("[data-testid=consumer-debt-warning-text]").textContent.trim() : null,
  refusalBanners: Array.from(document.querySelectorAll("[data-testid=refusal-banner]")).map(function(e){return e.textContent.trim()}),
  bridgeErrorBanners: Array.from(document.querySelectorAll("[data-testid=bridge-error-banner]")).map(function(e){return e.textContent.trim()})
})
'

probe() { agent-browser eval "$EVAL_PROBE" 2>&1 | tail -1; }

wait_for() { # $1 JS expr, $2 label, $3 tries
  local expr="$1" label="$2" tries="${3:-40}" i=0 r
  while [ "$i" -lt "$tries" ]; do
    r=$(agent-browser eval "$expr" 2>&1 | tail -1)
    if [ "$r" = "true" ]; then log "WAIT   $label -> ready after ${i} polls"; return 0; fi
    i=$((i+1)); sleep 0.5
  done
  log "WAIT   $label -> TIMED OUT after $tries polls"; return 1
}

clickid() { # $1 testid, $2 label
  log "CLICK  $2"
  agent-browser scrollintoview "[data-testid=$1]" >> "$OUT" 2>&1
  agent-browser click "[data-testid=$1]" >> "$OUT" 2>&1
}

log "=== 0. fresh load of /console, wait for hydration ==="
agent-browser close --all >/dev/null 2>&1
sleep 1
agent-browser open "http://127.0.0.1:3000/console" >/dev/null 2>&1
wait_for 'document.body.textContent.includes("BROWSER_HASH_MATCHES_ENGINE")' "page hydrated (browser-computed warranty hash matches)" 60
log "PROBE@load    $(probe)"
agent-browser screenshot "$EVID/p2fix-01-upload.png" >/dev/null 2>&1

log ""
log "=== 1. load sample ==="
clickid load-sample "load-sample"
wait_for 'document.body.textContent.includes("selected file")' "selected-file panel rendered" 20
log "PROBE@loaded  $(probe)"
agent-browser screenshot "$EVID/p2fix-02-sample-loaded.png" >/dev/null 2>&1

log ""
log "=== 2. run preview ==="
clickid run-preview "run-preview"
wait_for 'document.querySelectorAll("[data-testid=reconciliation-grid] tbody tr").length > 0' "reconciliation grid rendered rows" 60
log "PROBE@preview $(probe)"
agent-browser screenshot "$EVID/p2fix-03-preview.png" >/dev/null 2>&1

log ""
log "=== 3. tick the Step 3 warranty checkbox ==="
clickid warranty-checkbox "warranty-checkbox"
wait_for 'document.querySelector("[data-testid=warranty-checkbox]").checked === true' "warranty checkbox checked" 10
wait_for '!document.querySelector("[data-testid=register-batch]").disabled' "register-batch enabled by the tick" 20
log "PROBE@warranty $(probe)"
agent-browser screenshot "$EVID/p2fix-04-warranty-ticked.png" >/dev/null 2>&1

log ""
log "=== 4. register the batch ==="
clickid register-batch "register-batch"
wait_for 'document.querySelector("[data-testid=batch-record]") !== null' "ingestion_batches record panel rendered" 40
wait_for '!document.querySelector("[data-testid=commit-import]").disabled' "commit gate open" 20
log "PROBE@register $(probe)"
agent-browser screenshot "$EVID/p2fix-05-batch-registered.png" >/dev/null 2>&1

log ""
log "=== 5. commit to the queue ==="
clickid commit-import "commit-import"
wait_for 'document.querySelector("[data-testid=commit-result]") !== null' "commit result panel rendered" 60
log "PROBE@commit  $(probe)"
agent-browser screenshot "$EVID/p2fix-06-committed.png" >/dev/null 2>&1

log ""
log "=== 6. commit-again probe (must be refused) ==="
clickid commit-again-probe "commit-again-probe"
wait_for 'document.querySelectorAll("[data-testid=refusal-banner]").length > 0' "refusal banner rendered" 40
log "PROBE@probe   $(probe)"
agent-browser screenshot "$EVID/p2fix-07-commit-again-refused.png" >/dev/null 2>&1

log ""
log "=== 7. reload: is the committed state real engine state, not page state? ==="
agent-browser open "http://127.0.0.1:3000/console" >/dev/null 2>&1
sleep 5
log "PROBE@reload  $(probe)"
agent-browser screenshot "$EVID/p2fix-08-after-reload.png" >/dev/null 2>&1

echo "FLOW COMPLETE" >> "$OUT"
