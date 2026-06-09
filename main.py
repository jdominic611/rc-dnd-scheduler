"""
Entry point for the RingCentral DND scheduler (run as a Render Cron Job).

On each run it:
  1. authenticates with RingCentral (JWT flow)
  2. lists enabled user extensions
  3. reads each extension's business-hours + current presence
  4. sets dndStatus to the desired value ONLY if it differs

Configuration is via environment variables (see .env.example).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ringcentral_client import RingCentralClient, RingCentralError
from scheduler import resolve_timezone, is_within_work_hours

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("main")


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        log.error("Missing required environment variable: %s", name)
        sys.exit(2)
    return value


def main() -> int:
    server_url = os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com")
    client_id = _required("RC_CLIENT_ID")
    client_secret = _required("RC_CLIENT_SECRET")
    jwt_assertion = _required("RC_JWT")

    # DND value to set when an agent is OUTSIDE their work hours.
    #   DoNotAcceptAnyCalls        -> full DND (blocks everything)
    #   DoNotAcceptDepartmentCalls -> only blocks call-queue/department calls
    dnd_when_closed = os.getenv("DND_WHEN_CLOSED", "DoNotAcceptAnyCalls")

    # SAFETY: by default the job NEVER forces an agent available during work
    # hours. It only ever *adds* DND (off-hours); going available must be an
    # agent-asserted action (their morning login). This prevents a scheduled-
    # but-absent agent from being marked available on an empty seat.
    # Set CLEAR_DND_IN_HOURS=true ONLY if you explicitly want the old behavior
    # of forcing everyone available during their hours.
    clear_dnd_in_hours = os.getenv("CLEAR_DND_IN_HOURS", "false").lower() in (
        "1", "true", "yes",
    )
    dnd_when_open = os.getenv("DND_WHEN_OPEN", "TakeAllCalls")

    # During work hours, if an agent's presence is Offline, set them to DND so
    # RingCentral stops routing calls to an unreachable seat. This is safe (it
    # only closes a seat, never opens one). It does NOT touch agents who are
    # Available, on a call, or already DND. Default off; enable explicitly.
    dnd_offline_in_hours = os.getenv("DND_OFFLINE_IN_HOURS", "false").lower() in (
        "1", "true", "yes",
    )

    default_tz = os.getenv("DEFAULT_TIMEZONE", "America/Los_Angeles")
    dry_run = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
    # Optional: limit to a comma-separated allowlist of extension IDs for testing.
    only_ids = {
        x.strip() for x in os.getenv("ONLY_EXTENSION_IDS", "").split(",") if x.strip()
    }
    pace_seconds = float(os.getenv("PACE_SECONDS", "0.5"))

    try:
        ZoneInfo(default_tz)
    except Exception:  # noqa: BLE001
        log.error("DEFAULT_TIMEZONE %r is not a valid IANA timezone.", default_tz)
        return 2

    client = RingCentralClient(server_url, client_id, client_secret, jwt_assertion)
    try:
        client.authenticate()
    except RingCentralError as exc:
        log.error("Could not authenticate: %s", exc)
        return 1

    checked = changed = errors = skipped = left_alone = 0

    for ext in client.iter_user_extensions():
        ext_id = str(ext.get("id"))
        name = ext.get("name") or ext.get("contact", {}).get("firstName") or ext_id

        if only_ids and ext_id not in only_ids:
            continue

        checked += 1
        try:
            # Timezone comes from the extension's regionalSettings, not from
            # the work-hours rule. Use the list record if it carries it;
            # otherwise fetch the full extension detail.
            tz_obj = (ext.get("regionalSettings") or {}).get("timezone")
            if not tz_obj:
                detail = client.get_extension(ext_id)
                tz_obj = (detail.get("regionalSettings") or {}).get("timezone")
            tz, tz_label = resolve_timezone(tz_obj, default_tz)
            now_local = datetime.now(tz)

            work_hours = client.get_work_hours_rule(ext_id)
            within, reason = is_within_work_hours(work_hours, now_local)

            # DURING work hours: by default do nothing. The job never forces an
            # agent available — going available is the agent's own action
            # (morning login). This keeps a scheduled-but-absent agent closed,
            # because nothing here opens their seat.
            #
            # EXCEPTION (DND_OFFLINE_IN_HOURS): if an agent is Offline during
            # their hours, set DND so RingCentral stops routing calls to an
            # unreachable seat. We never touch agents who are Available, already
            # DND, or on a call.
            if within and not clear_dnd_in_hours:
                if not dnd_offline_in_hours:
                    left_alone += 1
                    log.info(
                        "[%s] %s: within hours, not managed (open, %s; tz=%s)",
                        ext_id, name, reason, tz_label,
                    )
                    continue

                presence = client.get_presence(ext_id)
                presence_status = presence.get("presenceStatus")
                telephony = presence.get("telephonyStatus")
                current = presence.get("dndStatus")

                # Only act on genuinely-offline agents who are not on a call.
                on_a_call = telephony not in (None, "NoCall")
                if presence_status != "Offline" or on_a_call:
                    left_alone += 1
                    log.info(
                        "[%s] %s: within hours, not managed (open, presence=%s, "
                        "telephony=%s; tz=%s)",
                        ext_id, name, presence_status, telephony, tz_label,
                    )
                    continue

                desired = dnd_when_closed
                if current == desired:
                    skipped += 1
                    log.info(
                        "[%s] %s: already %s (offline during hours; tz=%s)",
                        ext_id, name, desired, tz_label,
                    )
                    continue
                if dry_run:
                    log.info(
                        "[%s] %s: WOULD set %s -> %s (OFFLINE during hours; "
                        "tz=%s) [dry-run]",
                        ext_id, name, current, desired, tz_label,
                    )
                else:
                    client.set_dnd_status(ext_id, desired)
                    changed += 1
                    log.info(
                        "[%s] %s: set %s -> %s (OFFLINE during hours; tz=%s)",
                        ext_id, name, current, desired, tz_label,
                    )
                continue

            desired = dnd_when_open if within else dnd_when_closed

            presence = client.get_presence(ext_id)
            current = presence.get("dndStatus")

            if current == desired:
                skipped += 1
                log.info(
                    "[%s] %s: already %s (%s, %s; tz=%s)",
                    ext_id, name, desired, "open" if within else "closed",
                    reason, tz_label,
                )
                continue

            if dry_run:
                log.info(
                    "[%s] %s: WOULD set %s -> %s (%s, %s; tz=%s) [dry-run]",
                    ext_id, name, current, desired,
                    "open" if within else "closed", reason, tz_label,
                )
            else:
                client.set_dnd_status(ext_id, desired)
                changed += 1
                log.info(
                    "[%s] %s: set %s -> %s (%s, %s; tz=%s)",
                    ext_id, name, current, desired,
                    "open" if within else "closed", reason, tz_label,
                )
        except RingCentralError as exc:
            errors += 1
            log.error("[%s] %s: error: %s", ext_id, name, exc)
        finally:
            if pace_seconds:
                time.sleep(pace_seconds)

    log.info(
        "Done. checked=%d changed=%d unchanged=%d in_hours_unmanaged=%d errors=%d%s",
        checked, changed, skipped, left_alone, errors,
        " [dry-run]" if dry_run else "",
    )
    return 1 if errors and changed == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
