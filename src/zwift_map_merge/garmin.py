"""Thin wrapper around python-garminconnect for what the merge needs."""
from __future__ import annotations

import io
import logging
import threading
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from garminconnect import Garmin

log = logging.getLogger(__name__)


class GarminAuthRequired(RuntimeError):
    pass


class DuplicateActivity(RuntimeError):
    """Garmin refused the file because it already has an activity from it."""


class GarminSession:
    """Holds one logged-in client; tokens live in ``token_dir``."""

    def __init__(self, token_dir: Path):
        self.token_dir = token_dir
        self._client: Garmin | None = None
        self._pending: tuple[Garmin, Any] | None = None
        self._lock = threading.Lock()

    @property
    def token_file(self) -> Path:
        return self.token_dir / "garmin_tokens.json"

    def has_tokens(self) -> bool:
        return self.token_file.exists()

    def client(self) -> Garmin:
        with self._lock:
            if self._client is None:
                if not self.has_tokens():
                    raise GarminAuthRequired("Garmin login required")
                g = Garmin()
                g.login(str(self.token_dir))
                self._client = g
            return self._client

    def start_login(self, email: str, password: str) -> bool:
        """Return True when an MFA code is needed, False when logged in."""
        g = Garmin(email=email, password=password, return_on_mfa=True)
        status, state = g.login()
        if status == "needs_mfa":
            self._pending = (g, state)
            return True
        self._finish(g)
        return False

    def submit_mfa(self, code: str) -> None:
        if not self._pending:
            raise GarminAuthRequired("No login is waiting for a code")
        g, state = self._pending
        g.resume_login(state, code.strip())
        self._pending = None
        self._finish(g)

    def _finish(self, g: Garmin) -> None:
        self.token_dir.mkdir(parents=True, exist_ok=True)
        g.client.dump(str(self.token_dir))
        with self._lock:
            self._client = None  # reload from the token store, profile included
        self.client()

    def logout(self) -> None:
        with self._lock:
            self._client = None
        self.token_file.unlink(missing_ok=True)

    # --- activity helpers -------------------------------------------------

    def activities(self, days: int) -> list[dict[str, Any]]:
        start = (date.today() - timedelta(days=days)).isoformat()
        end = (date.today() + timedelta(days=1)).isoformat()
        return self.client().get_activities_by_date(start, end)

    def activities_between(self, start: date, end: date) -> list[dict[str, Any]]:
        return self.client().get_activities_by_date(start.isoformat(), end.isoformat())

    def activity(self, activity_id: int) -> dict[str, Any]:
        return self.client().get_activity(str(activity_id))

    def original_fit(self, activity_id: int) -> tuple[bytes, bytes]:
        """Return (raw download, extracted FIT bytes)."""
        raw = self.client().download_activity(
            str(activity_id), dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
        if raw[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                fits = [n for n in z.namelist() if n.lower().endswith(".fit")]
                if not fits:
                    raise ValueError(f"no FIT file in the original of {activity_id}")
                return raw, z.read(fits[0])
        return raw, raw

    def gear_uuids(self, activity_id: int) -> list[str]:
        try:
            gear = self.client().get_activity_gear(str(activity_id)) or []
        except Exception as e:  # noqa: BLE001 - gear is best effort
            log.warning("Could not read gear of %s: %s", activity_id, e)
            return []
        return [g["uuid"] for g in gear if isinstance(g, dict) and g.get("uuid")]

    def _around(self, start: datetime) -> list[dict[str, Any]]:
        return self.activities_between(start.date() - timedelta(days=1), start.date() + timedelta(days=1))

    def exists(self, activity_id: int, start: datetime) -> bool:
        """Whether the activity is still on Garmin, checked in the activity list.

        Used after an error: Garmin sometimes answers 5xx or times out even
        though the request went through.
        """
        return any(int(a.get("activityId", 0)) == int(activity_id) for a in self._around(start))

    def find_new(self, start: datetime, exclude: set[int]) -> int | None:
        """Id of an activity starting within 60 s of ``start`` that is not in ``exclude``."""
        for a in self._around(start):
            aid = int(a.get("activityId", 0))
            if aid in exclude:
                continue
            try:
                a_start = datetime.strptime(a["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if abs((a_start - start).total_seconds()) <= 60:
                return aid
        return None

    def delete(self, activity_id: int, start: datetime, checks: int = 6, pause_s: float = 10) -> None:
        """Delete and make sure the activity is gone, whatever Garmin answered."""
        try:
            self.client().delete_activity(str(activity_id))
        except Exception as e:  # noqa: BLE001 - verified below
            log.warning("Delete of %s answered %s; checking whether it went through", activity_id, e)
        for i in range(checks):
            if not self.exists(activity_id, start):
                return
            if i < checks - 1:
                time.sleep(pause_s)
        raise RuntimeError(f"activity {activity_id} is still on Garmin after deleting it")

    def import_fit(self, path: Path, start: datetime, exclude: set[int],
                   wait_s: int = 300, pause_s: float = 10) -> int:
        """Import ``path`` and return the id of the new activity.

        Raises DuplicateActivity when Garmin says it already has this file.
        After any other error, still looks for the activity: Garmin may have
        taken the file despite a 5xx or a timeout.
        """
        error: Exception | None = None
        try:
            result = self.client().import_activity(str(path))
            detailed = (result or {}).get("detailedImportResult") or {}
            for s in detailed.get("successes") or []:
                if s.get("internalId"):
                    return int(s["internalId"])
        except Exception as e:  # noqa: BLE001
            if "duplicate" in str(e).lower():
                raise DuplicateActivity(str(e)) from e
            log.warning("Import of %s answered %s; looking for the activity anyway", path.name, e)
            error = e
        # Processing is asynchronous: look for the activity by start time.
        deadline = time.monotonic() + wait_s
        while True:
            found = self.find_new(start, exclude)
            if found:
                return found
            if time.monotonic() >= deadline:
                break
            time.sleep(pause_s)
        if error:
            raise RuntimeError(f"import failed ({error}) and no new activity appeared")
        raise TimeoutError("imported activity not found after import")

    def restore(self, activity_id: int, *, name: str | None, description: str | None,
                type_dto: dict[str, Any] | None, gear: list[str]) -> list[str]:
        """Re-apply metadata; return the list of things that failed."""
        failed = []
        fields: dict[str, Any] = {}
        if name:
            fields["activityName"] = name
        if description:
            fields["description"] = description
        if type_dto and type_dto.get("typeId"):
            fields["activityTypeDTO"] = {k: type_dto[k] for k in ("typeId", "typeKey", "parentTypeId") if k in type_dto}
        if fields:
            try:
                self.client().update_activity(str(activity_id), fields)
            except Exception as e:  # noqa: BLE001
                log.warning("Metadata not restored on %s: %s", activity_id, e)
                failed.append("name/description/type")
        for uuid in gear:
            try:
                self.client().add_gear_to_activity(uuid, str(activity_id))
            except Exception as e:  # noqa: BLE001
                log.warning("Gear %s not linked to %s: %s", uuid, activity_id, e)
                failed.append(f"gear {uuid}")
        return failed
