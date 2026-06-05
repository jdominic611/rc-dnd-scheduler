"""
READ-ONLY diagnostic. Dumps the raw JSON RingCentral returns for a few
extensions so we can see where the working-hours schedule actually lives.

It makes only GET calls — it never changes anyone's presence.

Run it by temporarily setting the Render Start Command to:
    python debug_dump.py
Then revert the Start Command to:
    python main.py

Optional env var:
    DEBUG_EXT_ID  - dump just this one extension id (e.g. Adam's 688620052)
"""

from __future__ import annotations

import json
import os
import sys

from ringcentral_client import RingCentralClient, RingCentralError


def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        print(f"Missing env var: {name}")
        sys.exit(2)
    return v


def dump(label: str, obj) -> None:
    print(f"\n----- {label} -----")
    print(json.dumps(obj, indent=2, default=str))


def main() -> int:
    client = RingCentralClient(
        os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com"),
        _required("RC_CLIENT_ID"),
        _required("RC_CLIENT_SECRET"),
        _required("RC_JWT"),
    )
    client.authenticate()
    print("Authenticated OK.")

    only = os.getenv("DEBUG_EXT_ID", "").strip()

    count = 0
    for ext in client.iter_user_extensions():
        ext_id = str(ext.get("id"))
        name = ext.get("name") or ext_id

        if only and ext_id != only:
            continue

        print("\n=========================================================")
        print(f"EXTENSION {ext_id} — {name}")

        # What the list record itself carries (esp. regionalSettings)
        dump("list record (top-level keys)", sorted(ext.keys()))
        dump("list record regionalSettings", ext.get("regionalSettings"))

        # Full extension detail — has regionalSettings.timezone reliably
        try:
            detail = client.get_extension(ext_id)
            dump("extension detail regionalSettings", detail.get("regionalSettings"))
        except RingCentralError as e:
            print(f"  extension detail error: {e}")

        # The business-hours payload we currently parse
        try:
            bh = client.get_business_hours(ext_id)
            dump("business-hours RAW", bh)
        except RingCentralError as e:
            print(f"  business-hours error: {e}")

        # Answering rules (detailed) — likely where the real schedule lives
        try:
            rules = client._request(
                "GET",
                f"/restapi/v2/accounts/~/extensions/{ext_id}/comm-handling/voice/state-rules/work-hours",
            )
            dump("v2 work-hours state-rule RAW", rules)
        except RingCentralError as e:
            print(f"  v2 work-hours error: {e}")

        # Also list all state rules so we can see dnd / after-hours / agent
        try:
            allrules = client._request(
                "GET",
                f"/restapi/v2/accounts/~/extensions/{ext_id}/comm-handling/voice/state-rules",
            )
            dump("v2 state-rules LIST (names+ids only)", [
                {"id": r.get("id"), "name": r.get("name"),
                 "enabled": r.get("enabled"),
                 "hasSchedule": bool(r.get("schedule"))}
                for r in (allrules.get("records") or [])
            ])
        except RingCentralError as e:
            print(f"  v2 state-rules list error: {e}")

        count += 1
        if not only and count >= 3:
            print("\n(stopping after 3 extensions to stay under rate limits)")
            break

    if only and count == 0:
        print(f"\nExtension id {only} not found among user extensions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
