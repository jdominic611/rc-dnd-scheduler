"""
Decide whether a given extension should be in Do-Not-Disturb right now,
based on its RingCentral business-hours schedule and its own timezone.

RingCentral business-hours payload shape (the bit we care about):

    {
      "schedule": {
        # Either a 24/7 marker ...
        "weeklyRanges": {
          "monday":    [{"from": "09:00", "to": "17:00"}],
          "tuesday":   [{"from": "09:00", "to": "17:00"}],
          ...
        }
      }
    }

Notes / edge cases handled:
  * A day missing from weeklyRanges  -> closed all day (DND on).
  * Empty `schedule` / empty `weeklyRanges` -> treated as "always open"
    (RingCentral represents 24/7 as an empty schedule).
  * Multiple ranges per day are supported (e.g. split shift).
  * A range whose `to` is "00:00" or <= `from` is treated as "until end of day".
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

_WEEKDAY_KEYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def _parse_hhmm(value: str) -> Optional[dtime]:
    try:
        hh, mm = value.strip().split(":")
        return dtime(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def resolve_timezone(timezone_obj: Optional[Dict[str, Any]], default_tz: str):
    """
    Resolve an extension's timezone from its regionalSettings.timezone object.

    RingCentral returns timezone as {"id", "name", "bias", "description"}, e.g.
        {"id": "58", "name": "US/Eastern",
         "description": "Eastern Time (US & Canada)", "bias": "-300"}

    Resolution order (most correct first):
      1. `name` parsed as an IANA zone  -> handles DST automatically.
      2. `bias` (offset minutes from UTC) -> fixed offset, NO DST. Last resort,
         because it will be an hour off during the other half of the year.
      3. DEFAULT_TIMEZONE.

    Returns a tzinfo and a short human label describing how it was resolved.
    """
    timezone_obj = timezone_obj or {}

    name = timezone_obj.get("name")
    if name:
        try:
            return ZoneInfo(name), f"zone {name}"
        except Exception:  # noqa: BLE001 - not an IANA name; try next strategy
            pass

    bias = timezone_obj.get("bias")
    if bias not in (None, ""):
        try:
            offset = timedelta(minutes=int(bias))
            label = timezone_obj.get("description") or f"bias {bias}m"
            return dt_timezone(offset), f"fixed offset ({label}, no DST)"
        except (TypeError, ValueError):
            pass

    return ZoneInfo(default_tz), f"default {default_tz}"


def _ranges_for_day(weekly: Dict[str, Any], weekday_index: int) -> List[Dict[str, str]]:
    key = _WEEKDAY_KEYS[weekday_index]
    value = weekly.get(key)
    if not value:
        return []
    if isinstance(value, dict):  # single range expressed as an object
        return [value]
    return list(value)


def is_within_business_hours(
    business_hours: Dict[str, Any],
    now: datetime,
) -> Tuple[bool, str]:
    """
    Return (within_hours, human_reason).

    `now` must already be timezone-aware in the extension's local timezone.
    """
    schedule = business_hours.get("schedule") or {}
    weekly = schedule.get("weeklyRanges")

    # RingCentral encodes 24/7 availability as an empty / absent schedule.
    if not weekly:
        return True, "no weekly schedule set (treated as always open)"

    ranges = _ranges_for_day(weekly, now.weekday())
    if not ranges:
        return False, f"{_WEEKDAY_KEYS[now.weekday()]} has no working hours"

    current = now.time()
    for r in ranges:
        start = _parse_hhmm(r.get("from", ""))
        end = _parse_hhmm(r.get("to", ""))
        if start is None:
            continue
        # "to" of 00:00 or not-after start means "through end of day".
        if end is None or end <= start:
            if current >= start:
                return True, f"within {r.get('from')}–end of day"
        else:
            if start <= current < end:
                return True, f"within {r.get('from')}–{r.get('to')}"

    return False, "outside all working ranges for today"
