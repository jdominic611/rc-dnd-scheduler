"""
RingCentral DND scheduler — runs as a Render BACKGROUND WORKER.

A single long-running process that:
  1. authenticates with RingCentral (JWT flow)
  2. discovers the managed agent list from call-queue membership
     (once at startup, then refreshed daily at AGENT_REFRESH_HOUR), holding it
     in memory so the 58-queue fetch happens ~once a day, not every cycle
  3. every LOOP_INTERVAL_MINUTES, runs a DND pass over the agent list:
       - outside work hours        -> set DND (if not already)
       - inside hours + Offline     -> set DND (if DND_OFFLINE_IN_HOURS)
       - inside hours otherwise     -> leave alone (never forces available)

Because it's one continuous process, the in-memory agent list persists across
cycles and runs can never overlap.

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


def _csv_ids(name: str) -> set:
    return {x.strip() for x in os.getenv(name, "").split(",") if x.strip()}


class Config:
    def __init__(self) -> None:
        self.server_url = os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com")
        self.client_id = _required("RC_CLIENT_ID")
        self.client_secret = _required("RC_CLIENT_SECRET")
        self.jwt = _required("RC_JWT")

        self.dnd_when_closed = os.getenv("DND_WHEN_CLOSED", "DoNotAcceptAnyCalls")
        self.dnd_when_open = os.getenv("DND_WHEN_OPEN", "TakeAllCalls")
        self.clear_dnd_in_hours = os.getenv("CLEAR_DND_IN_HOURS", "false").lower() in (
            "1", "true", "yes")
        self.dnd_offline_in_hours = os.getenv(
            "DND_OFFLINE_IN_HOURS", "false").lower() in ("1", "true", "yes")

        self.default_tz = os.getenv("DEFAULT_TIMEZONE", "America/New_York")
        self.dry_run = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
        self.pace_seconds = float(os.getenv("PACE_SECONDS", "0.5"))

        # Agent list source.
        #  - DISCOVER_AGENTS_FROM_QUEUES=true: build the list from call-queue
        #    membership (minus EXCLUDE_EXTENSION_IDS), refreshed daily.
        #  - otherwise: use the static ONLY_EXTENSION_IDS allowlist.
        self.discover_from_queues = os.getenv(
            "DISCOVER_AGENTS_FROM_QUEUES", "false").lower() in ("1", "true", "yes")
        self.only_ids = _csv_ids("ONLY_EXTENSION_IDS")
        self.exclude_ids = _csv_ids("EXCLUDE_EXTENSION_IDS")

        # Worker loop timing.
        self.loop_interval = float(os.getenv("LOOP_INTERVAL_MINUTES", "5")) * 60
        self.refresh_hour = int(os.getenv("AGENT_REFRESH_HOUR", "8"))
        # Pacing between the per-queue member calls during the daily refresh.
        self.queue_pace_seconds = float(os.getenv("QUEUE_PACE_SECONDS", "1.0"))

        try:
            ZoneInfo(self.default_tz)
        except Exception:  # noqa: BLE001
            log.error("DEFAULT_TIMEZONE %r is not a valid IANA timezone.",
                      self.default_tz)
            sys.exit(2)


def discover_agents(client: RingCentralClient, cfg: Config) -> set:
    """Build the managed agent set from call-queue membership, minus excludes."""
    agent_ids: dict = {}
    queues = client.list_call_queues()
    log.info("Discovering agents from %d call queues...", len(queues))
    for q in queues:
        q_id = q.get("id")
        try:
            for m in client.get_queue_members(str(q_id)):
                mid = str(m.get("id"))
                if mid not in cfg.exclude_ids:
                    agent_ids.setdefault(mid, m.get("extensionNumber"))
        except RingCentralError as exc:
            log.warning("queue %s members error: %s", q_id, exc)
        if cfg.queue_pace_seconds:
            time.sleep(cfg.queue_pace_seconds)
    log.info("Discovered %d managed agents (excluded %d).",
             len(agent_ids), len(cfg.exclude_ids))
    return set(agent_ids.keys())


def resolve_agent_ids(client: RingCentralClient, cfg: Config) -> set:
    """Return the set of extension IDs to manage, per configuration."""
    if cfg.discover_from_queues:
        return discover_agents(client, cfg)
    return set(cfg.only_ids)


def handle_agent(client: RingCentralClient, cfg: Config, ext: dict) -> str:
    """
    Apply DND policy to one extension. Returns a short outcome tag:
    'changed' | 'unchanged' | 'left_alone'. Raises RingCentralError on API error.
    """
    ext_id = str(ext.get("id"))
    name = ext.get("name") or ext.get("contact", {}).get("firstName") or ext_id

    tz_obj = (ext.get("regionalSettings") or {}).get("timezone")
    if not tz_obj:
        detail = client.get_extension(ext_id)
        tz_obj = (detail.get("regionalSettings") or {}).get("timezone")
    tz, tz_label = resolve_timezone(tz_obj, cfg.default_tz)
    now_local = datetime.now(tz)

    work_hours = client.get_work_hours_rule(ext_id)
    within, reason = is_within_work_hours(work_hours, now_local)

    # DURING work hours
    if within and not cfg.clear_dnd_in_hours:
        if not cfg.dnd_offline_in_hours:
            log.info("[%s] %s: within hours, not managed (open, %s; tz=%s)",
                     ext_id, name, reason, tz_label)
            return "left_alone"

        presence = client.get_presence(ext_id)
        presence_status = presence.get("presenceStatus")
        telephony = presence.get("telephonyStatus")
        current = presence.get("dndStatus")
        on_a_call = telephony not in (None, "NoCall")
        if presence_status != "Offline" or on_a_call:
            log.info("[%s] %s: within hours, not managed (open, presence=%s, "
                     "telephony=%s; tz=%s)", ext_id, name, presence_status,
                     telephony, tz_label)
            return "left_alone"

        desired = cfg.dnd_when_closed
        if current == desired:
            log.info("[%s] %s: already %s (offline during hours; tz=%s)",
                     ext_id, name, desired, tz_label)
            return "unchanged"
        if cfg.dry_run:
            log.info("[%s] %s: WOULD set %s -> %s (OFFLINE during hours; tz=%s) "
                     "[dry-run]", ext_id, name, current, desired, tz_label)
            return "left_alone"
        client.set_dnd_status(ext_id, desired)
        log.info("[%s] %s: set %s -> %s (OFFLINE during hours; tz=%s)",
                 ext_id, name, current, desired, tz_label)
        return "changed"

    # OUTSIDE hours (or CLEAR_DND_IN_HOURS override)
    desired = cfg.dnd_when_open if within else cfg.dnd_when_closed
    presence = client.get_presence(ext_id)
    current = presence.get("dndStatus")

    if current == desired:
        log.info("[%s] %s: already %s (%s, %s; tz=%s)", ext_id, name, desired,
                 "open" if within else "closed", reason, tz_label)
        return "unchanged"

    if cfg.dry_run:
        log.info("[%s] %s: WOULD set %s -> %s (%s, %s; tz=%s) [dry-run]",
                 ext_id, name, current, desired,
                 "open" if within else "closed", reason, tz_label)
        return "left_alone"
    client.set_dnd_status(ext_id, desired)
    log.info("[%s] %s: set %s -> %s (%s, %s; tz=%s)", ext_id, name, current,
             desired, "open" if within else "closed", reason, tz_label)
    return "changed"


def run_pass(client: RingCentralClient, cfg: Config, agent_ids: set) -> None:
    """One DND sweep over the managed agents."""
    changed = unchanged = left_alone = errors = checked = 0
    for ext in client.iter_user_extensions():
        ext_id = str(ext.get("id"))
        if ext_id not in agent_ids:
            continue
        checked += 1
        try:
            outcome = handle_agent(client, cfg, ext)
            if outcome == "changed":
                changed += 1
            elif outcome == "unchanged":
                unchanged += 1
            else:
                left_alone += 1
        except RingCentralError as exc:
            errors += 1
            name = ext.get("name") or ext_id
            log.error("[%s] %s: error: %s", ext_id, name, exc)
        finally:
            if cfg.pace_seconds:
                time.sleep(cfg.pace_seconds)

    log.info("Pass done. checked=%d changed=%d unchanged=%d in_hours_unmanaged=%d "
             "errors=%d%s", checked, changed, unchanged, left_alone, errors,
             " [dry-run]" if cfg.dry_run else "")


def main() -> int:
    cfg = Config()
    client = RingCentralClient(cfg.server_url, cfg.client_id, cfg.client_secret,
                               cfg.jwt)

    # Authenticate, retrying a few times so a transient startup blip doesn't
    # crash the worker.
    for attempt in range(1, 6):
        try:
            client.authenticate()
            break
        except RingCentralError as exc:
            log.error("Auth attempt %d failed: %s", attempt, exc)
            time.sleep(min(30, 5 * attempt))
    else:
        log.error("Could not authenticate after retries; exiting.")
        return 1

    tz = ZoneInfo(cfg.default_tz)
    agent_ids: set = set()
    last_refresh_date = None

    log.info("Worker started. interval=%.0fs refresh_hour=%02d:00 %s discover=%s "
             "dry_run=%s", cfg.loop_interval, cfg.refresh_hour, cfg.default_tz,
             cfg.discover_from_queues, cfg.dry_run)

    while True:
        cycle_start = time.time()
        try:
            now = datetime.now(tz)
            # Refresh the agent list on first loop and once per day at/after the
            # refresh hour.
            need_refresh = (
                not agent_ids
                or (last_refresh_date != now.date() and now.hour >= cfg.refresh_hour)
            )
            if need_refresh:
                new_ids = resolve_agent_ids(client, cfg)
                if new_ids:
                    agent_ids = new_ids
                    last_refresh_date = now.date()
                else:
                    log.warning("Agent list came back empty; keeping previous "
                                "list of %d.", len(agent_ids))

            if agent_ids:
                run_pass(client, cfg, agent_ids)
            else:
                log.warning("No managed agents resolved; nothing to do this cycle.")
        except Exception as exc:  # noqa: BLE001 - never let the loop die
            log.exception("Cycle error (continuing): %s", exc)

        # Sleep the remainder of the interval.
        elapsed = time.time() - cycle_start
        time.sleep(max(5.0, cfg.loop_interval - elapsed))


if __name__ == "__main__":
    sys.exit(main())
