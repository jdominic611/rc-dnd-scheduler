"""
Decide whether a given extension should be in Do-Not-Disturb right now,
based on its RingCentral Work Hours schedule and its own timezone.

This targets the NEW call-handling backend (NewCallHandlingAndForwarding).
The schedule comes from the v2 'work-hours' state rule:

    {
      "id": "work-hours",
      "state": {
        "conditions": [
          {
            "type": "Schedule",
            "schedule": {
              "triggers": [
                {
                  "triggerType": "Weekly",
                  "ranges": {
                    "monday":  [{"startTime": "10:00:00", "endTime": "19:30:00"}],
                    "tuesday": [{"startTime": "10:00:00", "endTime": "19:30:00"}],
                    ...
                  }
                }
              ]
            }
          }
        ]
      }
    }

Notes / edge cases handled:
  * No Weekly trigger / empty ranges / empty rule -> treated as "always open"
    (24/7 schedules carry no weekly ranges).
  * A day missing from ranges -> closed all day (DND on).
  * Multiple ranges per day are supported (e.g. split shift).
  * An endTime of "00:00:00" or <= startTime is treated as "until end of day".
  * Times are HH:MM:SS; seconds are honoured.
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


def _parse_time(value: str) -> Optional[dtime]:
    """Parse 'HH:MM:SS' or 'HH:MM' into a time object."""
    try:
        parts = value.strip().split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        ss = int(parts[2]) if len(parts) > 2 else 0
        return dtime(hh, mm, ss)
    except (ValueError, AttributeError, IndexError):
        return None


def _extract_weekly_ranges(work_hours_rule: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Pull the weekly `ranges` dict out of the v2 work-hours rule, or None if the
    rule has no weekly schedule (i.e. 24/7 / always open).
    """
    state = work_hours_rule.get("state") or {}
    for condition in state.get("conditions") or []:
        if condition.get("type") != "Schedule":
            continue
        schedule = condition.get("schedule") or {}
        for trigger in schedule.get("triggers") or []:
            if trigger.get("triggerType") == "Weekly":
                ranges = trigger.get("ranges")
                if ranges:
                    return ranges
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


def is_within_work_hours(
    work_hours_rule: Dict[str, Any],
    now: datetime,
) -> Tuple[bool, str]:
    """
    Return (within_hours, human_reason).

    `now` must already be timezone-aware in the extension's local timezone.
    Reads the v2 work-hours rule structure.
    """
    weekly = _extract_weekly_ranges(work_hours_rule)

    # No weekly schedule -> 24/7 / always open.
    if not weekly:
        return True, "no work-hours schedule (treated as always open)"

    ranges = _ranges_for_day(weekly, now.weekday())
    if not ranges:
        return False, f"{_WEEKDAY_KEYS[now.weekday()]} has no working hours"

    current = now.time()
    for r in ranges:
        start = _parse_time(r.get("startTime", ""))
        end = _parse_time(r.get("endTime", ""))
        if start is None:
            continue
        # endTime of 00:00:00 or not-after start means "through end of day".
        if end is None or end <= start:
            if current >= start:
                return True, f"within {r.get('startTime')}–end of day"
        else:
            if start <= current < end:
                return True, f"within {r.get('startTime')}–{r.get('endTime')}"

    return False, "outside all working ranges for today"
