"""Scheduled runs.

RecoverAI schedules contacts ahead of time (a batch, a retry, a follow-up). A DNC
opt-out or a mode flip must be able to kill work that is already scheduled - so the
queue is a first-class object the inbound webhook and the mode switch can reach into.
Runs live in memory and are mirrored to out/scheduled_runs.json for inspection.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import clock, config

RUN_STATES = ("scheduled", "dispatched", "halted", "cancelled")

#: States a confirmed payment may remove from the active queue. A scheduled run is
#: pending work; a halted run is work a mode flip stopped but the queue still holds.
#: `dispatched` and `cancelled` are history and are never removed.
PURGEABLE_STATES = ("scheduled", "halted")


@dataclass
class ScheduledRun:
    run_id: str
    debtor_id: str
    client_id: str
    channel: str
    due_at: str
    state: str = "scheduled"
    created_at: str = field(default_factory=clock.iso)
    state_reason: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RunQueue:
    def __init__(self):
        self._runs: Dict[str, ScheduledRun] = {}
        self._order: List[str] = []

    def schedule(self, debtor_id: str, client_id: str, channel: str, due_at: Optional[str] = None,
                 reason: str = "scheduled contact") -> ScheduledRun:
        run = ScheduledRun(
            run_id=f"run-{uuid.uuid4().hex[:10]}",
            debtor_id=debtor_id,
            client_id=client_id,
            channel=channel,
            due_at=due_at or clock.iso(clock.plus(minutes=1)),
            state_reason=reason,
        )
        self._runs[run.run_id] = run
        self._order.append(run.run_id)
        return run

    def get(self, run_id: str) -> Optional[ScheduledRun]:
        return self._runs.get(run_id)

    def due(self, at_iso: Optional[str] = None) -> List[ScheduledRun]:
        at = at_iso or clock.iso()
        return [r for r in self.all() if r.state == "scheduled" and r.due_at <= at]

    def all(self) -> List[ScheduledRun]:
        return [self._runs[i] for i in self._order]

    def cancel_for_debtor(self, debtor_id: str, reason: str) -> List[ScheduledRun]:
        cancelled = []
        for run in self._runs.values():
            if run.debtor_id == debtor_id and run.state in ("scheduled",):
                run.state = "cancelled"
                run.state_reason = reason
                cancelled.append(run)
        return cancelled

    def purge_for_debtor(self, debtor_id: str, reason: str) -> List[Dict[str, Any]]:
        """Destructively remove a debtor's pending/scheduled attempts. Returns them.

        This is the queue half of the payment path: the entries are REMOVED, not marked,
        because a paid debtor must not be chased again - not now and not by a run that
        somehow survived. Each returned record carries the position and the prior state
        so `restore()` can put the queue back exactly as it was if the surrounding
        transaction rolls back.
        """
        removed: List[Dict[str, Any]] = []
        for index, run_id in enumerate(list(self._order)):
            run = self._runs.get(run_id)
            if run and run.debtor_id == debtor_id and run.state in PURGEABLE_STATES:
                removed.append({
                    "index": index,
                    "run": run.as_dict(),
                    "previous_state": run.state,
                    "previous_state_reason": run.state_reason,
                    "purge_reason": reason,
                })
                del self._runs[run_id]
                self._order.remove(run_id)
        return removed

    def restore(self, removed: List[Dict[str, Any]]) -> List[str]:
        """Put purged runs back exactly where they were, in their previous state."""
        restored: List[str] = []
        for item in sorted(removed, key=lambda i: i["index"]):
            run = ScheduledRun(**dict(item["run"]))
            run.state = item["previous_state"]
            run.state_reason = item["previous_state_reason"]
            self._runs[run.run_id] = run
            self._order.insert(min(item["index"], len(self._order)), run.run_id)
            restored.append(run.run_id)
        return restored

    def halt_pending_for_client(self, client_id: str, reason: str) -> List[ScheduledRun]:
        halted = []
        for run in self._runs.values():
            if run.client_id == client_id and run.state == "scheduled":
                run.state = "halted"
                run.state_reason = reason
                halted.append(run)
        return halted

    def mark(self, run_id: str, state: str, reason: Optional[str] = None) -> Optional[ScheduledRun]:
        run = self._runs.get(run_id)
        if run:
            run.state = state
            if reason:
                run.state_reason = reason
        return run

    def dump(self, path: Optional[Path] = None) -> Path:
        """Mirror the queue to a JSON file.

        The write is atomic (a sibling .tmp file, then os.replace): a reader that opens
        this file at the same moment as a writer must never see half a queue. That
        matters as soon as the file is a queue of RECORD, not just an inspection copy -
        see `load` and `RecoverAI(runqueue_path=...)`.
        """
        target = Path(path) if path else (config.out_dir() / "scheduled_runs.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([r.as_dict() for r in self.all()], indent=2)
        scratch = target.with_name(target.name + ".tmp")
        scratch.write_text(payload, encoding="utf-8")
        os.replace(scratch, target)
        return target

    @classmethod
    def load(cls, path: Path | str) -> "RunQueue":
        """Rebuild a queue from a file `dump` wrote. Missing file = an empty queue.

        A file that exists but cannot be read is a hard error, never a silent empty
        queue: dropping scheduled runs on the floor because a file was malformed is
        exactly the failure a collections queue must not have. The caller decides what
        to do about it (the console bridge reports it as a bridge error).
        """
        target = Path(path)
        queue = cls()
        if not target.exists():
            return queue
        try:
            rows = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"scheduled-run file {target} is unreadable: {exc}") from exc
        if not isinstance(rows, list):
            raise ValueError(f"scheduled-run file {target} is not a JSON list of runs")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"scheduled-run file {target} holds a non-object entry")
            try:
                run = ScheduledRun(**row)
            except TypeError as exc:
                raise ValueError(
                    f"scheduled-run file {target} holds an entry this engine cannot read "
                    f"(unknown or missing field): {exc}") from exc
            queue._runs[run.run_id] = run
            queue._order.append(run.run_id)
        return queue

    def summary(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for run in self.all():
            out[run.state] = out.get(run.state, 0) + 1
        return out
