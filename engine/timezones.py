"""Area code -> local time derivation, and the calling-window decision.

Rule: an outbound voice/SMS contact may fire only when the DEBTOR'S local time,
derived from the phone area code, is between the rulebook's window start and end
inclusive (08:00-21:00 by default). Everything here is fail-closed: if we cannot
resolve an area code we freeze the run rather than guess.

Known weak point (stated in the business plan's limitations): an area code is not a
reliable proxy for where a mobile debtor currently is. The code stores what it
derived and why, so the decision is auditable; a real deployment should source the
timezone from billing address or carrier data.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from . import config, clock

#: NANP area codes -> IANA timezone. The demo set plus the largest US/CA codes.
AREA_CODE_TIMEZONES: Dict[str, str] = {
    # Eastern
    "202": "America/New_York", "203": "America/New_York", "212": "America/New_York",
    "215": "America/New_York", "216": "America/New_York", "301": "America/New_York",
    "305": "America/New_York", "312": "America/Chicago", "313": "America/Detroit",
    "401": "America/New_York", "404": "America/New_York", "407": "America/New_York",
    "410": "America/New_York", "412": "America/New_York", "413": "America/New_York",
    "502": "America/New_York", "504": "America/Chicago", "513": "America/New_York",
    "516": "America/New_York", "517": "America/Detroit", "518": "America/New_York",
    "561": "America/New_York", "585": "America/New_York", "603": "America/New_York",
    "607": "America/New_York", "609": "America/New_York", "614": "America/New_York",
    "615": "America/Chicago", "616": "America/Detroit", "617": "America/New_York",
    "631": "America/New_York", "646": "America/New_York", "703": "America/New_York",
    "704": "America/New_York", "716": "America/New_York", "718": "America/New_York",
    "724": "America/New_York", "727": "America/New_York", "773": "America/Chicago",
    "781": "America/New_York", "786": "America/New_York", "804": "America/New_York",
    "813": "America/New_York", "814": "America/New_York", "845": "America/New_York",
    "857": "America/New_York", "860": "America/New_York", "904": "America/New_York",
    "908": "America/New_York", "914": "America/New_York", "917": "America/New_York",
    "919": "America/New_York",
    # Central
    "205": "America/Chicago", "210": "America/Chicago", "214": "America/Chicago",
    "217": "America/Chicago", "225": "America/Chicago", "256": "America/Chicago",
    "281": "America/Chicago", "309": "America/Chicago", "314": "America/Chicago",
    "316": "America/Chicago", "317": "America/Indiana/Indianapolis", "318": "America/Chicago",
    "319": "America/Chicago", "320": "America/Chicago", "402": "America/Chicago",
    "405": "America/Chicago", "409": "America/Chicago", "414": "America/Chicago",
    "417": "America/Chicago", "469": "America/Chicago", "479": "America/Chicago",
    "501": "America/Chicago", "512": "America/Chicago", "515": "America/Chicago",
    "563": "America/Chicago", "571": "America/New_York", "601": "America/Chicago",
    "612": "America/Chicago", "618": "America/Chicago", "630": "America/Chicago",
    "651": "America/Chicago", "713": "America/Chicago", "715": "America/Chicago",
    "731": "America/Chicago", "757": "America/New_York", "763": "America/Chicago",
    "785": "America/Chicago", "816": "America/Chicago", "817": "America/Chicago",
    "832": "America/Chicago", "847": "America/Chicago", "901": "America/Chicago",
    "913": "America/Chicago", "918": "America/Chicago", "920": "America/Chicago",
    "931": "America/Chicago", "936": "America/Chicago", "940": "America/Chicago",
    "952": "America/Chicago", "972": "America/Chicago",
    # Mountain
    "303": "America/Denver", "307": "America/Denver", "385": "America/Denver",
    "406": "America/Denver", "435": "America/Denver", "480": "America/Phoenix",
    "505": "America/Denver", "520": "America/Phoenix", "602": "America/Phoenix",
    "623": "America/Phoenix", "719": "America/Denver", "720": "America/Denver",
    "801": "America/Denver", "928": "America/Phoenix", "970": "America/Denver",
    # Pacific / Mountain-west
    "206": "America/Los_Angeles", "208": "America/Boise", "213": "America/Los_Angeles",
    "253": "America/Los_Angeles", "310": "America/Los_Angeles", "323": "America/Los_Angeles",
    "360": "America/Los_Angeles", "408": "America/Los_Angeles", "415": "America/Los_Angeles",
    "425": "America/Los_Angeles", "442": "America/Los_Angeles", "503": "America/Los_Angeles",
    "509": "America/Los_Angeles", "510": "America/Los_Angeles", "530": "America/Los_Angeles",
    "541": "America/Los_Angeles", "559": "America/Los_Angeles", "562": "America/Los_Angeles",
    "619": "America/Los_Angeles", "626": "America/Los_Angeles", "650": "America/Los_Angeles",
    "657": "America/Los_Angeles", "661": "America/Los_Angeles", "702": "America/Los_Angeles",
    "707": "America/Los_Angeles", "714": "America/Los_Angeles", "760": "America/Los_Angeles",
    "775": "America/Los_Angeles", "805": "America/Los_Angeles", "818": "America/Los_Angeles",
    "831": "America/Los_Angeles", "858": "America/Los_Angeles", "909": "America/Los_Angeles",
    "916": "America/Los_Angeles", "925": "America/Los_Angeles", "949": "America/Los_Angeles",
    "951": "America/Los_Angeles",
    # Alaska / Hawaii
    "907": "America/Anchorage", "808": "Pacific/Honolulu",
    # Canada (kept because clients do have Canadian debtors)
    "416": "America/Toronto", "604": "America/Vancouver", "647": "America/Toronto",
    "780": "America/Edmonton", "905": "America/Toronto",
}

_NANP_DIGITS = re.compile(r"(\d{3})\D*(\d{3})\D*(\d{4})\s*$")


def area_code_from_phone(phone: Optional[str]) -> Optional[str]:
    """Return the NANP area code of a phone number, or None if not resolvable."""
    if not phone:
        return None
    digits = re.sub(r"[^\d+]", "", str(phone))
    if digits.startswith("+"):
        if not digits.startswith("+1"):
            return None  # non-NANP: area codes do not describe these
        digits = digits[1:]
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return digits[:3]


def window_decision_for(phone: Optional[str], at: Optional[datetime] = None) -> Dict[str, object]:
    """Derive the debtor's local time and the calling-window decision.

    Returns a dict that is stored verbatim on the debtor record so the decision is
    auditable after the fact.
    """
    now = at or clock.now_utc()
    start_min, end_min, inclusive = config.window_bounds_minutes()
    area = area_code_from_phone(phone)
    decision: Dict[str, object] = {
        "utc_now": clock.iso(now),
        "area_code": area,
        "phone": phone,
        "window_utc_hours": f"{start_min // 60:02d}:{start_min % 60:02d}-{end_min // 60:02d}:{end_min % 60:02d}",
        "window_source": config.calling_window()["time_source"],
    }
    if area is None or area not in AREA_CODE_TIMEZONES:
        decision.update(
            {
                "tz_name": None,
                "derived_local_time": None,
                "local_clock": None,
                "in_window": False,
                "window_decision": "Unknown_Area_Code_Freeze",
                "reason": "area code not resolvable from phone number - fail closed",
            }
        )
        return decision

    tz = ZoneInfo(AREA_CODE_TIMEZONES[area])
    local = now.astimezone(tz)
    local_minutes = local.hour * 60 + local.minute
    if inclusive:
        in_window = start_min <= local_minutes <= end_min
    else:
        in_window = start_min <= local_minutes < end_min
    decision.update(
        {
            "tz_name": AREA_CODE_TIMEZONES[area],
            "derived_local_time": local.isoformat(),
            "local_clock": local.strftime("%H:%M %Z"),
            "local_minutes": local_minutes,
            "in_window": in_window,
            "window_decision": "In_Window" if in_window else "Outside_Calling_Window",
            "reason": (
                f"debtor local time {local.strftime('%H:%M %Z')} "
                f"({'inside' if in_window else 'outside'} {decision['window_utc_hours']} local window)"
            ),
        }
    )
    return decision
