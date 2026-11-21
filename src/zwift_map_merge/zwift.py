"""Client for Zwift's unofficial Companion API: login, activity history, FIT download.

Zwift has no public API. This uses the same endpoints as the Zwift Companion
app, so it may stop working if Zwift changes them.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://secure.zwift.com/auth/realms/zwift/protocol/openid-connect/token"
CLIENT_ID = "Zwift_Mobile_Link"
API_URL = "https://us-or-rly101.zwift.com"
USER_AGENT = "Zwift/115 CFNetwork/1335.0.3 Darwin/21.6.0"
PAGE_SIZE = 30
MAX_FIT_BYTES = 50 * 1024 * 1024
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")


class ZwiftError(RuntimeError):
    pass


class ZwiftAuthRequired(ZwiftError):
    pass


@dataclass
class ZwiftActivity:
    id: str
    name: str
    start: datetime | None
    sport: str
    fit_bucket: str | None
    fit_key: str | None


def _parse_date(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    text = str(value).replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)  # +0000 -> +00:00
    try:
        d = datetime.fromisoformat(text)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_activity(a: dict[str, Any]) -> ZwiftActivity | None:
    aid = a.get("id_str") or a.get("id") or a.get("activityId")
    if aid is None:
        return None
    return ZwiftActivity(
        id=str(aid),
        name=str(a.get("name") or a.get("activityName") or ""),
        start=_parse_date(a.get("startDate")),
        sport=str(a.get("sport") or ""),
        fit_bucket=a.get("fitFileBucket"),
        fit_key=a.get("fitFileKey"),
    )


class ZwiftClient:
    """Keeps the OAuth tokens in ``token_file``; the password is never stored."""

    def __init__(self, token_file: Path, session: requests.Session | None = None):
        self.token_file = token_file
        self.session = session or requests.Session()
        self._lock = threading.Lock()
        self._tokens: dict[str, Any] = {}
        self._profile_id: str | None = None
        if token_file.exists():
            try:
                self._tokens = json.loads(token_file.read_text())
            except (OSError, ValueError):
                self._tokens = {}

    # --- auth -------------------------------------------------------------

    def connected(self) -> bool:
        return bool(self._tokens.get("refresh_token"))

    def login(self, email: str, password: str) -> None:
        self._token_request({"grant_type": "password", "username": email, "password": password})
        self._profile_id = None

    def logout(self) -> None:
        self._tokens = {}
        self._profile_id = None
        self.token_file.unlink(missing_ok=True)

    def _token_request(self, grant: dict[str, str]) -> None:
        try:
            resp = self.session.post(
                TOKEN_URL, data={"client_id": CLIENT_ID, **grant},
                headers={"User-Agent": USER_AGENT}, timeout=30, allow_redirects=False)
        except requests.RequestException as e:
            raise ZwiftError(f"cannot reach the Zwift login server: {e}") from e
        if resp.status_code in (400, 401):
            raise ZwiftAuthRequired("Zwift rejected the login; log in again")
        if resp.status_code != 200:
            raise ZwiftError(f"Zwift login failed (HTTP {resp.status_code})")
        body = resp.json()
        if not body.get("access_token"):
            raise ZwiftError("Zwift login returned no access token")
        now = time.time()
        # Zwift sends expires_in in milliseconds, unlike the OAuth standard.
        lifetime = float(body.get("expires_in") or 3600)
        if lifetime > 1e6:
            lifetime /= 1000
        self._tokens = {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or self._tokens.get("refresh_token"),
            "expires_at": now + lifetime,
        }
        self._save()

    def _save(self) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._tokens))
        os.chmod(tmp, 0o600)
        tmp.replace(self.token_file)

    def _access_token(self) -> str:
        with self._lock:
            if not self.connected():
                raise ZwiftAuthRequired("Zwift login required")
            if time.time() > float(self._tokens.get("expires_at") or 0) - 60:
                try:
                    self._token_request({"grant_type": "refresh_token",
                                         "refresh_token": self._tokens["refresh_token"]})
                except ZwiftAuthRequired:
                    self.logout()
                    raise
            return self._tokens["access_token"]

    # --- API --------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._access_token()}",
                   "Accept": "application/json", "User-Agent": USER_AGENT}
        resp = self.session.get(API_URL + path, params=params, headers=headers,
                                timeout=30, allow_redirects=False)
        if resp.status_code == 401:
            self._tokens["expires_at"] = 0  # force a refresh next time
            raise ZwiftError("Zwift refused the token (HTTP 401)")
        if resp.status_code == 429:
            raise ZwiftError("Zwift rate limit reached (HTTP 429); try again later")
        if resp.status_code != 200:
            raise ZwiftError(f"Zwift API {path} failed (HTTP {resp.status_code})")
        return resp.json()

    def profile_id(self) -> str:
        if self._profile_id is None:
            pid = str(self._get("/api/profiles/me").get("id") or "")
            if not pid.isdigit():
                raise ZwiftError("Zwift returned no profile id")
            self._profile_id = pid
        return self._profile_id

    def activities(self, start: int = 0, limit: int = PAGE_SIZE) -> list[dict[str, Any]]:
        data = self._get(f"/api/profiles/{self.profile_id()}/activities",
                         {"start": start, "limit": limit})
        if isinstance(data, dict):  # the endpoint has returned both shapes
            data = data.get("activities") or data.get("data") or []
        return data if isinstance(data, list) else []

    def history(self, since: datetime, max_activities: int = 5000,
                pause_s: float = 0.5) -> Iterator[ZwiftActivity]:
        """Yield activities that started at or after ``since``.

        The feed is newest first, but the order is not documented, so paging
        stops only once a whole page is older than ``since``.
        """
        start = 0
        while start < max_activities:
            page = self.activities(start)
            if not page:
                return
            recent = False
            for raw in page:
                a = parse_activity(raw)
                if a is None or a.start is None or a.start < since:
                    continue
                recent = True
                yield a
            if not recent:
                return
            start += len(page)
            time.sleep(pause_s)

    def download_fit(self, activity: ZwiftActivity) -> bytes:
        bucket, key = activity.fit_bucket, activity.fit_key
        if not bucket or not key:
            detail = self._get(f"/api/activities/{quote(activity.id)}")
            bucket, key = detail.get("fitFileBucket"), detail.get("fitFileKey")
        if not bucket or not key:
            raise ZwiftError(f"no FIT file for Zwift activity {activity.id}")
        if not _BUCKET_RE.fullmatch(bucket):
            raise ZwiftError("Zwift returned an invalid FIT storage bucket")
        url = f"https://{bucket}.s3.amazonaws.com/{quote(key)}"
        with self.session.get(url, headers={"User-Agent": USER_AGENT}, timeout=120,
                              stream=True, allow_redirects=False) as resp:
            if resp.status_code != 200:
                raise ZwiftError(f"FIT download failed (HTTP {resp.status_code})")
            data = bytearray()
            for chunk in resp.iter_content(65536):
                data += chunk
                if len(data) > MAX_FIT_BYTES:
                    raise ZwiftError("FIT file larger than 50 MiB")
        return bytes(data)
