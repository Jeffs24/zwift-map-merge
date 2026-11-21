"""Configuration from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "oui")


@dataclass
class Config:
    data_dir: Path
    scan_days: int
    scan_interval_minutes: int
    auto_merge: bool
    delete_source: bool
    copy_altitude: bool
    min_overlap: float
    min_position_ratio: float
    time_tolerance_s: int
    source_names: list[str]
    source_manufacturers: list[int]
    app_password: str | None
    garmin_email: str | None
    garmin_password: str | None

    @property
    def token_dir(self) -> Path:
        return self.data_dir / "garminconnect"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "/data")),
            scan_days=int(os.getenv("SCAN_DAYS", "7")),
            scan_interval_minutes=int(os.getenv("SCAN_INTERVAL_MINUTES", "30")),
            auto_merge=_bool("AUTO_MERGE", False),
            delete_source=_bool("DELETE_SOURCE", True),
            copy_altitude=_bool("COPY_ALTITUDE", False),
            min_overlap=float(os.getenv("MIN_OVERLAP", "0.5")),
            min_position_ratio=float(os.getenv("MIN_POSITION_RATIO", "0.5")),
            time_tolerance_s=int(os.getenv("TIME_TOLERANCE_S", "2")),
            source_names=[s.strip() for s in os.getenv("SOURCE_NAMES", "zwift").split(",") if s.strip()],
            # 260 = Zwift in the FIT profile
            source_manufacturers=[int(s) for s in os.getenv("SOURCE_MANUFACTURERS", "260").split(",") if s.strip()],
            app_password=os.getenv("APP_PASSWORD") or None,
            garmin_email=os.getenv("GARMIN_EMAIL") or None,
            garmin_password=os.getenv("GARMIN_PASSWORD") or None,
        )
