"""Rulebook + alias loading. All tunable behaviour is DATA read from config/*.json.

Nothing in RecoverAI hard-codes the calling window, the opt-out keywords, the aging
bucket edges, the disclaimer text or the authorization scope options: they are read
from config/rulebook.json at call time. Change the JSON, change the behaviour - which
is also why the web console can reuse the exact same file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

_HERE = Path(__file__).resolve().parent


def repo_root() -> Path:
    """Root of the recoverai tree (overridable with RECOVERAI_HOME)."""
    env = os.environ.get("RECOVERAI_HOME")
    if env:
        return Path(env).resolve()
    return _HERE.parent


def config_dir() -> Path:
    return repo_root() / "config"


def data_dir() -> Path:
    return repo_root() / "data"


def out_dir() -> Path:
    return repo_root() / "out"


def load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


_rulebook_cache: Dict[str, Any] | None = None


def load_rulebook(refresh: bool = False) -> Dict[str, Any]:
    global _rulebook_cache
    if _rulebook_cache is None or refresh:
        _rulebook_cache = load_json(config_dir() / "rulebook.json")
    return _rulebook_cache


def load_aliases(refresh: bool = False) -> Dict[str, Any]:
    return load_json(config_dir() / "ingestion_aliases.json")


# --- rulebook accessors -----------------------------------------------------
def disclaimer() -> str:
    return load_rulebook()["disclaimer"]


def disclaimer_versions() -> int:
    return int(load_rulebook().get("disclaimer_occurrences_required", 1))


def calling_window() -> Dict[str, Any]:
    return load_rulebook()["calling_window"]


def window_bounds_minutes() -> tuple[int, int, bool]:
    """(start_minutes, end_minutes, inclusive) derived from the rulebook clock strings."""
    cfg = calling_window()

    def to_minutes(hhmm: str) -> int:
        hh, mm = str(hhmm).split(":")
        return int(hh) * 60 + int(mm)

    return to_minutes(cfg["start_local"]), to_minutes(cfg["end_local"]), bool(
        cfg.get("inclusive_both_ends", True)
    )


def opt_out_keywords() -> list[str]:
    return list(load_rulebook()["opt_out"]["keywords"])


def freeze_token() -> str:
    return load_rulebook()["calling_window"]["freeze_status_token"]


def aging_buckets() -> list[Dict[str, Any]]:
    return list(load_rulebook()["aging_buckets"])


def operating_modes_cfg() -> Dict[str, Any]:
    return load_rulebook()["operating_modes"]


def default_mode() -> str:
    return load_rulebook()["operating_modes"]["default"]


def authorization_document() -> Dict[str, Any]:
    return load_rulebook()["authorization_document"]


def scope_options() -> list[Dict[str, Any]]:
    return list(load_rulebook()["authorization_scope_options"])


def channel_config(channel: str) -> Dict[str, Any]:
    channels = load_rulebook()["channels"]
    if channel not in channels:
        raise KeyError(f"unknown channel {channel!r}; rulebook knows {sorted(channels)}")
    return channels[channel]


def contingency_fee() -> Dict[str, Any]:
    """The fee framework: flat 20% of funds actually collected, in integer cents.

    Read from the rulebook like everything else, so the rate, the rounding rule and the
    confirmation channels the ledger accepts are one edit in one file - and none of it
    is a retainer, a setup fee or a per-call charge.
    """
    return load_rulebook()["contingency_fee"]


def confirmation_channels() -> Dict[str, Any]:
    return load_rulebook()["contingency_fee"]["confirmation_channels"]


def spreadsheet_intake() -> Dict[str, Any]:
    """Phase 3: the upload path's tokens, its advisory consumer-debt heuristic and the
    exact warning text the console renders. Data, like every other rule in here."""
    return load_rulebook()["spreadsheet_intake"]


def step3_warranty() -> Dict[str, Any]:
    """The Step 3 legal warranty wording a batch binds to (version + exact text)."""
    return load_rulebook()["spreadsheet_intake"]["step3_warranty"]


def onboarding() -> Dict[str, Any]:
    """The onboarding step ladder: the fee-tracking choice, the Step 2 transparency
    acknowledgement and the Step 3 warranty. Data, like every other rule in here."""
    return load_rulebook()["onboarding"]


def fee_tracking_cfg() -> Dict[str, Any]:
    return onboarding()["fee_tracking"]


def fee_tracking_modes() -> list[Dict[str, Any]]:
    return list(fee_tracking_cfg()["modes"])


def fee_tracking_mode_ids() -> list[str]:
    return [str(m["id"]) for m in fee_tracking_modes()]


def default_fee_tracking_mode() -> str:
    return str(fee_tracking_cfg()["default"])


def fee_tracking_mode(mode_id: str) -> Dict[str, Any]:
    for mode in fee_tracking_modes():
        if str(mode["id"]) == str(mode_id):
            return mode
    raise KeyError(f"unknown fee-tracking mode {mode_id!r}; the rulebook knows "
                   f"{fee_tracking_mode_ids()}")


def step2_acknowledgement() -> Dict[str, Any]:
    return onboarding()["step2_acknowledgement"]


def channel_utility_rates() -> Dict[str, Any]:
    """Step 2's channel utility rate sheet (MODEL 2, owner-confirmed).

    Internal operational metrics - what one unit of each channel costs us to run - and NOT
    client charges. The three rates and their exact label live in the rulebook, so the
    console renders whatever this file says and a rate is never a literal in view code.
    Nothing in the billing path may read this: the only money a client is charged is the
    flat contingency fee on funds actually collected.
    """
    block = step2_acknowledgement().get("channel_utility_rates")
    if not block:
        raise KeyError("the rulebook has no onboarding.step2_acknowledgement."
                       "channel_utility_rates block: the Step 2 rate sheet is missing")
    return dict(block)


def channel_utility_rate(channel_key: str) -> float:
    """One unit rate by key (email / sms_segment / voice_minute)."""
    rates = channel_utility_rates()["rates"]
    if channel_key not in rates:
        raise KeyError(f"no utility rate for {channel_key!r}; the rate sheet knows "
                       f"{sorted(rates)}")
    return rates[channel_key]


def onboarding_step_order() -> list[Dict[str, Any]]:
    return list(onboarding()["step_order"])


def rulebook_version() -> str:
    return str(load_rulebook().get("rulebook_version", "unknown"))


def consumer_debt_scan_cfg() -> Dict[str, Any]:
    return load_rulebook()["spreadsheet_intake"]["consumer_debt_scan"]


def aging_state_for_days(days: int) -> str:
    """Bucket a non-negative age in days using the rulebook's edges."""
    for bucket in load_rulebook()["aging_buckets"]:
        lo = bucket["min_days"]
        hi = bucket["max_days"]
        if days >= lo and (hi is None or days <= hi):
            return bucket["state"]
    return "unknown"
