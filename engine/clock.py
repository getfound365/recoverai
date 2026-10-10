"""Clock. Every timestamp the engine writes comes from here.

Determinism matters for a compliance demo that has to show an in-window contact and
an out-of-window freeze in the same run, so the clock can be pinned:

    RECOVERAI_FAKE_NOW=2026-10-06T14:00:00Z python3 run_demo.py

Faking the clock never changes which rules are applied - it only changes "now".
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

_fake_now: Optional[datetime] = None


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, accepting a trailing Z."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_utc() -> datetime:
    if _fake_now is not None:
        return _fake_now
    env = os.environ.get("RECOVERAI_FAKE_NOW")
    if env:
        return parse_ts(env)
    return datetime.now(timezone.utc)


def set_fake_now(value) -> None:
    global _fake_now
    if value is None:
        _fake_now = None
    elif isinstance(value, datetime):
        _fake_now = value.astimezone(timezone.utc)
    else:
        _fake_now = parse_ts(str(value))


def iso(dt: Optional[datetime] = None) -> str:
    dt = dt or now_utc()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(dt: Optional[datetime] = None) -> str:
    """The same instant, to the millisecond, in UTC with a trailing Z.

    The second-precision `iso()` is what the rest of the engine has always written, so it
    is unchanged. This one exists for the records where the instant two events landed in
    the same second is exactly what has to be provable: the lifecycle transition log and
    the authorized-by/at columns on the contact log.
    """
    dt = dt or now_utc()
    stamp = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return stamp[:-3] + "Z"


def plus(seconds: float = 0, minutes: float = 0, days: float = 0, base: Optional[datetime] = None) -> datetime:
    return (base or now_utc()) + timedelta(seconds=seconds, minutes=minutes, days=days)
