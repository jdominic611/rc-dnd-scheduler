"""
Thin RingCentral REST client for the DND scheduler.

Only implements the few endpoints we need:
  - JWT auth flow (server-to-server)
  - list user extensions (paginated)
  - read an extension's business-hours schedule
  - read / update an extension's presence (dndStatus)

Docs:
  Auth (JWT):       https://developers.ringcentral.com/guide/authentication/jwt-flow
  Presence:         https://developers.ringcentral.com/api-reference/Presence/readUserPresenceStatus
  Business hours:   https://developers.ringcentral.com/api-reference/User-Business-Hours/readUserBusinessHours
  Extensions:       https://developers.ringcentral.com/api-reference/Extensions/listExtensions
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterator, Optional

import requests

log = logging.getLogger("rc")


class RingCentralError(RuntimeError):
    pass


class RingCentralClient:
    def __init__(
        self,
        server_url: str,
        client_id: str,
        client_secret: str,
        jwt_assertion: str,
        timeout: int = 30,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._jwt = jwt_assertion
        self._timeout = timeout
        self._session = requests.Session()
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0

    # ----------------------------------------------------------------- auth
    def authenticate(self) -> None:
        """Exchange the JWT assertion for an access token (JWT auth flow)."""
        url = f"{self.server_url}/restapi/oauth/token"
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": self._jwt,
        }
        resp = self._session.post(
            url,
            data=data,
            auth=(self._client_id, self._client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise RingCentralError(
                f"Auth failed ({resp.status_code}): {resp.text[:500]}"
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        # refresh a minute before actual expiry to be safe
        self._token_expiry = time.time() + int(payload.get("expires_in", 3600)) - 60
        log.info("Authenticated with RingCentral.")

    def _ensure_token(self) -> None:
        if not self._access_token or time.time() >= self._token_expiry:
            self.authenticate()

    # -------------------------------------------------------------- requests
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        _retries: int = 3,
    ) -> Dict[str, Any]:
        self._ensure_token()
        url = path if path.startswith("http") else f"{self.server_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        for attempt in range(1, _retries + 1):
            resp = self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=self._timeout,
            )

            # Rate limited: honour Retry-After and back off.
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "5"))
                log.warning("Rate limited; sleeping %ss (attempt %s).", wait, attempt)
                time.sleep(wait)
                continue

            # Token expired mid-run: re-auth once and retry.
            if resp.status_code == 401 and attempt < _retries:
                log.info("Token rejected; re-authenticating.")
                self.authenticate()
                headers["Authorization"] = f"Bearer {self._access_token}"
                continue

            if resp.status_code >= 400:
                raise RingCentralError(
                    f"{method} {url} -> {resp.status_code}: {resp.text[:500]}"
                )

            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()

        raise RingCentralError(f"{method} {url} exhausted retries (rate limited).")

    # ------------------------------------------------------------- endpoints
    def iter_user_extensions(self, per_page: int = 100) -> Iterator[Dict[str, Any]]:
        """Yield enabled User-type extensions across all pages."""
        path = "/restapi/v1.0/account/~/extension"
        params = {"type": "User", "status": "Enabled", "perPage": per_page}
        while True:
            data = self._request("GET", path, params=params)
            for record in data.get("records", []):
                yield record
            navigation = data.get("navigation", {})
            next_page = navigation.get("nextPage", {}).get("uri")
            if not next_page:
                break
            path, params = next_page, None  # nextPage uri already has query string

    def get_work_hours_rule(self, extension_id: str) -> Dict[str, Any]:
        """
        Read the v2 'work-hours' state rule, which holds the weekly schedule on
        accounts upgraded to NewCallHandlingAndForwarding. Returns {} if the
        account/extension has no work-hours rule (treated as always open).
        """
        try:
            return self._request(
                "GET",
                f"/restapi/v2/accounts/~/extensions/{extension_id}"
                "/comm-handling/voice/state-rules/work-hours",
            )
        except RingCentralError as exc:
            # 404 = no such rule (e.g. 24/7); surface empty rather than crash.
            if " 404:" in str(exc):
                return {}
            raise

    def get_extension(self, extension_id: str) -> Dict[str, Any]:
        """Full extension record, including regionalSettings.timezone."""
        return self._request(
            "GET", f"/restapi/v1.0/account/~/extension/{extension_id}"
        )

    def get_presence(self, extension_id: str) -> Dict[str, Any]:
        return self._request(
            "GET", f"/restapi/v1.0/account/~/extension/{extension_id}/presence"
        )

    def set_dnd_status(self, extension_id: str, dnd_status: str) -> Dict[str, Any]:
        return self._request(
            "PUT",
            f"/restapi/v1.0/account/~/extension/{extension_id}/presence",
            json={"dndStatus": dnd_status},
        )
