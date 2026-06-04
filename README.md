# RingCentral DND Scheduler

A small scheduled job that toggles **Do Not Disturb** for every RingCentral
user extension based on that extension's own **business-hours schedule** —
the schedule already configured in RingCentral. Inside an agent's working
hours they're set to take calls; outside them, they're set to DND.

Designed to run as a **Render Cron Job** from a GitHub repo.

## How it works

On each run the job:

1. Authenticates with RingCentral using the **JWT auth flow** (server-to-server).
2. Lists enabled **User** extensions (paginated).
3. For each extension, reads its **business-hours** schedule and resolves its timezone.
4. Decides whether *now* (in that agent's local time) is inside their working hours.
5. Sets `dndStatus` via the **presence** API — but only when it differs from the
   current value, so re-runs are idempotent and cheap on rate limits.

`dndStatus` values used:

| Situation | Value | Meaning |
|-----------|-------|---------|
| Inside business hours | `TakeAllCalls` | available |
| Outside business hours | `DoNotAcceptAnyCalls` | full DND (default) |

Set `DND_WHEN_CLOSED=DoNotAcceptDepartmentCalls` instead if you only want to
pull agents out of **call queues** after hours rather than block all calls.

## RingCentral setup (one time)

1. Create an app in the [RingCentral Developer Console](https://developers.ringcentral.com/)
   with **Server-only (no UI)** / **JWT** auth.
2. Grant these app scopes:
   - **Read Accounts** (read extensions, regional settings/timezone, and business hours)
   - **Edit Presence** (change `dndStatus`)
3. Note the app's **Client ID** and **Client Secret**.
4. Under your user, create a **JWT** credential (Credentials → JWT). Copy the
   long assertion string — that's `RC_JWT`.
5. The signed-in user/app must have admin rights to read/modify other
   extensions' presence.

> Test against the **sandbox** first: set `RC_SERVER_URL` to
> `https://platform.devtest.ringcentral.com`.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in your values; keep DRY_RUN=true at first
set -a && source .env && set +a
python main.py
```

With `DRY_RUN=true` it logs exactly what it *would* change without touching
any agent. Use `ONLY_EXTENSION_IDS=12345,67890` to scope a test to a couple of
extensions. When the dry-run output looks right, set `DRY_RUN=false`.

## Deploy on Render

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, select the repo. Render reads `render.yaml`
   and creates a Cron Job.
3. In the service's **Environment**, fill in the secret values
   (`RC_CLIENT_ID`, `RC_CLIENT_SECRET`, `RC_JWT`) — they're marked `sync: false`
   so they aren't read from the file.
4. Adjust the `schedule` in `render.yaml` if you want a different cadence.

The default schedule runs every 15 minutes:

```
schedule: "*/15 * * * *"
```

Render interprets cron schedules in **UTC**. The job itself converts time into
each agent's own timezone, so the UTC cron cadence only controls *how often*
it checks — not what "9-to-5" means for any given agent. A 15-minute cadence
means DND flips within ~15 minutes of an agent's hour boundary; use `*/5` for
tighter precision.

## Configuration reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `RC_SERVER_URL` | `https://platform.ringcentral.com` | API base (use devtest for sandbox) |
| `RC_CLIENT_ID` | — | App client ID (**required**) |
| `RC_CLIENT_SECRET` | — | App client secret (**required**) |
| `RC_JWT` | — | JWT assertion string (**required**) |
| `DEFAULT_TIMEZONE` | `America/Los_Angeles` | Fallback IANA tz if an extension's can't be resolved |
| `DND_WHEN_CLOSED` | `DoNotAcceptAnyCalls` | DND value outside hours |
| `DND_WHEN_OPEN` | `TakeAllCalls` | DND value inside hours |
| `DRY_RUN` | `false` | If true, log changes without applying |
| `ONLY_EXTENSION_IDS` | — | Comma-separated allowlist for testing |
| `PACE_SECONDS` | `0.2` | Delay between agents to ease rate limits |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## Notes & caveats

- **Manual overrides get reverted.** Because the job enforces schedule-based DND
  on every run, if an agent manually changes their DND it will be set back on the
  next run if it contradicts their schedule. That's usually the point, but worth
  knowing.
- **Timezone source.** Each agent's "10:00 AM" is interpreted in *their own*
  timezone, read from the extension's **Regional Settings** (the same GMT-04:00
  you see in the admin console). The job resolves the RingCentral timezone
  `name` (e.g. `US/Eastern`) to a full IANA zone so daylight-saving transitions
  are handled automatically; if a name can't be resolved it falls back to the
  fixed UTC offset (`bias`, no DST) and finally to `DEFAULT_TIMEZONE`. The
  `tzdata` package is bundled so the full zone database is present even on a
  minimal container. Confirm timezones resolve as expected during a dry run —
  the log shows `tz=...` for each agent.
- **24/7 schedules.** RingCentral represents always-open as an empty schedule;
  those agents are treated as always within hours (never auto-DND'd).
- **Rate limits.** The client honors `429` `Retry-After` and re-auths on `401`.
  For very large accounts, raise `PACE_SECONDS` or widen the cron interval.
