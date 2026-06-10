"""
READ-ONLY diagnostic. Lists all call queues and their members, then prints the
unique set of member extension IDs (the would-be dynamic agent list). Makes
only GET calls.

Run by temporarily setting the Render Start Command to:
    python debug_queues.py
Then revert to:
    python main.py
"""

from __future__ import annotations

import os
import sys

from ringcentral_client import RingCentralClient, RingCentralError


def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        print(f"Missing env var: {name}")
        sys.exit(2)
    return v


def main() -> int:
    client = RingCentralClient(
        os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com"),
        _required("RC_CLIENT_ID"),
        _required("RC_CLIENT_SECRET"),
        _required("RC_JWT"),
    )
    client.authenticate()
    print("Authenticated OK.\n")

    # 1. List all call queues
    try:
        queues = client._request("GET", "/restapi/v1.0/account/~/call-queues")
    except RingCentralError as e:
        print(f"call-queues error: {e}")
        return 1

    records = queues.get("records") or []
    print(f"Found {len(records)} call queue(s).\n")

    agent_ids = {}  # ext_id -> name (collected across all queues)

    for q in records:
        q_id = q.get("id")
        q_name = q.get("name") or q.get("extensionNumber")
        print(f"=== Queue: {q_name} (id={q_id}, ext={q.get('extensionNumber')}) ===")
        try:
            members = client._request(
                "GET",
                f"/restapi/v1.0/account/~/department/{q_id}/members",
                params={"perPage": 100},
            )
        except RingCentralError as e:
            print(f"  members error: {e}")
            continue

        for m in members.get("records") or []:
            mid = str(m.get("id"))
            mnum = m.get("extensionNumber")
            print(f"    member: id={mid}  ext={mnum}")
            agent_ids.setdefault(mid, mnum)
        print()

    print("=========================================================")
    print(f"UNIQUE agent extension IDs across all queues ({len(agent_ids)}):")
    print(",".join(agent_ids.keys()))
    print("\n(For reference, with extension numbers:)")
    for mid, mnum in agent_ids.items():
        print(f"    {mid}  (ext {mnum})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
