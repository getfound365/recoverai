"""Append-only guardrail/action log.

Every guardrail decision - a freeze, a DNC kill, a mode change, a claim allowed or
refused - is written here as one JSON line AND kept in memory so a caller (the demo,
a test, the console) can read back what happened. Nothing deletes from it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import clock, config

ACTIONS = (
    "ledger_imported",
    "draft_queued",
    "draft_approved",
    "draft_rejected",
    "signoff_created",
    "signoff_revoked",
    "signoff_expired",
    "mode_change",
    "mode_change_refused",
    "guardrail_freeze",
    "dnc_set",
    "runs_cancelled",
    "run_scheduled",
    "run_halted",
    "halt_requested",
    "claim_allowed",
    "claim_blocked",
    "post_dispatched",
    "post_failed",
    # the payment path: one event, three consequences, plus the guard states around it
    "payment_recorded",
    "payment_duplicate_ignored",
    "queue_purged_for_payment",
    "ledger_entry_appended",
    "reversal_recorded",
    "balance_edit_parked",
    "balance_edit_noop",
    "balance_edit_resolved",
    # Phase 2: the beta-to-active gate and its Agent 4 alerts
    "agent4_alert",
    "lifecycle_transition",
    "billing_route_verified",
    "gate_locked",
    # Phase 3: the spreadsheet intake - registration, commit, and the refusals
    "ingestion_batch_registered",
    "ingestion_batch_refused",
    "spreadsheet_committed",
    "spreadsheet_commit_refused",
)


class AuditLog:
    def __init__(self, path: Optional[Path] = None, echo=None):
        self.path = Path(path) if path else (config.out_dir() / "guardrail_actions.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: List[Dict[str, Any]] = []
        self._echo = echo

    def record(self, action: str, **fields: Any) -> Dict[str, Any]:
        entry = {"timestamp": clock.iso(), "action": action}
        entry.update(fields)
        self.entries.append(entry)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str, sort_keys=True) + "\n")
        if self._echo:
            self._echo(entry)
        return entry

    def since(self, action: str) -> List[Dict[str, Any]]:
        return [e for e in self.entries if e["action"] == action]

    def clear(self) -> None:
        self.entries = []
        if self.path.exists():
            self.path.unlink()
