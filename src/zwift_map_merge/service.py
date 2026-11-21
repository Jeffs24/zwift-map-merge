"""Scan, simulate and merge pairs."""
from __future__ import annotations

import gzip
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import fitfile as ff
from .config import Config
from .garmin import GarminSession
from .merge import MergeError, extract_track, manufacturer, merge_track
from .pairing import Summary, find_pairs, overlap_ratio, parse_summary
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Prepared:
    target_id: int
    source_id: int
    merged_path: Path
    target_backup: Path
    ratio: float
    start: datetime


class Service:
    def __init__(self, cfg: Config, garmin: GarminSession, store: Store):
        self.cfg = cfg
        self.garmin = garmin
        self.store = store
        self._lock = threading.Lock()

    # --- scan -------------------------------------------------------------

    def scan(self) -> int:
        activities = self.garmin.activities(self.cfg.scan_days)
        pairs = find_pairs(activities, source_names=self.cfg.source_names,
                           min_overlap=self.cfg.min_overlap,
                           exclude_ids=self.store.handled_ids())
        for p in pairs:
            self.store.upsert_detected(p.target.id, p.source.id, p.target.name, p.source.name,
                                       p.target.start.isoformat(), p.overlap)
        log.info("Scan: %d pair(s) found in %d activities", len(pairs), len(activities))
        return len(pairs)

    def add_source_file(self, filename: str, data: bytes) -> str:
        """Pair an uploaded Zwift FIT (zwift.com or Strava export) with a watch activity.

        Returns a short message for the UI.
        """
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        try:
            fit = ff.parse(data)
        except ff.FitError as e:
            return f"{filename}: unreadable FIT file ({e})"
        man = manufacturer(fit)
        if man not in self.cfg.source_manufacturers:
            return f"{filename}: not a Zwift file (manufacturer {man})"
        track = extract_track(fit)
        if not track:
            return f"{filename}: no GPS points"
        start = datetime.fromtimestamp(ff.unix_from_fit(track[0].ts), timezone.utc)
        end = datetime.fromtimestamp(ff.unix_from_fit(track[-1].ts), timezone.utc)
        source = Summary(0, filename, start, (end - start).total_seconds(), True, "", "zwift")

        handled = self.store.handled_ids()
        candidates = []
        for a in self.garmin.activities_between(start.date() - timedelta(days=1),
                                                end.date() + timedelta(days=1)):
            s = parse_summary(a)
            if not s or s.has_polyline or s.id in handled:
                continue
            ratio = overlap_ratio(s, source)
            if ratio >= self.cfg.min_overlap:
                candidates.append((ratio, s))
        if not candidates:
            return (f"{filename}: no watch activity without a map matches "
                    f"({start:%Y-%m-%d %H:%M} UTC)")
        ratio, target = max(candidates, key=lambda c: c[0])

        folder = self.cfg.backup_dir / str(target.id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "zwift_uploaded.fit"
        path.write_bytes(data)
        if not self.store.upsert_detected(target.id, 0, target.name, f"File {filename}",
                                          target.start.isoformat(), ratio, source_file=str(path)):
            return f"{filename}: activity “{target.name}” is already handled"
        return f"{filename} → “{target.name}” ({target.start:%Y-%m-%d %H:%M} UTC)"

    def scan_and_maybe_merge(self) -> None:
        self.scan()
        if not self.cfg.auto_merge:
            return
        for row in self.store.all():
            if row["status"] in ("detected", "simulated"):
                self.merge(row["target_id"])

    # --- simulate / merge ---------------------------------------------------

    def _prepare(self, target_id: int) -> Prepared:
        row = self.store.get(target_id)
        if not row:
            raise MergeError(f"unknown pair: {target_id}")
        source_id = row["source_id"]
        folder = self.cfg.backup_dir / str(target_id)
        folder.mkdir(parents=True, exist_ok=True)

        t_raw, t_fit = self.garmin.original_fit(target_id)
        (folder / f"{target_id}_watch_original.zip").write_bytes(t_raw)
        target_backup = folder / f"{target_id}_watch.fit"
        target_backup.write_bytes(t_fit)
        if row["source_file"]:
            s_fit = Path(row["source_file"]).read_bytes()
        else:
            s_raw, s_fit = self.garmin.original_fit(source_id)
            (folder / f"{source_id}_zwift_original.zip").write_bytes(s_raw)
            (folder / f"{source_id}_zwift.fit").write_bytes(s_fit)

        source = ff.parse(s_fit)
        man = manufacturer(source)
        if man not in self.cfg.source_manufacturers:
            raise MergeError(f"source {source_id or row['source_name']} does not have the expected "
                             f"manufacturer ({man} instead of {self.cfg.source_manufacturers})")
        target = ff.parse(t_fit)
        if extract_track(target):
            raise MergeError(f"activity {target_id} already has GPS points")

        merged, stats = merge_track(t_fit, s_fit, tolerance_s=self.cfg.time_tolerance_s,
                                    copy_altitude=self.cfg.copy_altitude)
        if stats.ratio < self.cfg.min_position_ratio:
            raise MergeError(
                f"only {stats.ratio:.0%} of the watch points got a position "
                f"(minimum {self.cfg.min_position_ratio:.0%}): are the clocks out of sync?")
        check = ff.parse(merged)  # re-read: header, CRC and sizes must be valid
        if not extract_track(check):
            raise MergeError("the merged file has no GPS points")
        merged_path = folder / f"{target_id}_merged.fit"
        merged_path.write_bytes(merged)
        start = datetime.fromisoformat(row["start_utc"])
        return Prepared(target_id, source_id, merged_path, target_backup, stats.ratio, start)

    def simulate(self, target_id: int) -> Prepared:
        with self._lock:
            try:
                prep = self._prepare(target_id)
            except Exception as e:
                self.store.set_status(target_id, "failed", f"Simulation: {e}")
                raise
            self.store.set_status(target_id, "simulated",
                                  f"Merged file ready ({prep.ratio:.0%} of points with a position). "
                                  "Nothing was changed on Garmin.",
                                  position_ratio=prep.ratio)
            return prep

    def merge(self, target_id: int) -> int | None:
        with self._lock:
            row = self.store.get(target_id)
            if row and row["status"] == "merged":
                return row["new_id"]
            try:
                prep = self._prepare(target_id)
            except Exception as e:
                log.exception("Could not prepare %s", target_id)
                self.store.set_status(target_id, "failed", f"Preparation: {e}")
                return None

            detail = self.garmin.activity(target_id)
            name = detail.get("activityName")
            description = detail.get("description")
            type_dto = detail.get("activityTypeDTO")
            gear = self.garmin.gear_uuids(target_id)

            # From here on Garmin is modified. Both original FIT files are on disk.
            try:
                if self.cfg.delete_source and prep.source_id:
                    self.garmin.delete(prep.source_id)
                self.garmin.delete(target_id)
            except Exception as e:  # noqa: BLE001
                log.exception("Delete failed for %s", target_id)
                self.store.set_status(target_id, "failed",
                                      f"Delete on Garmin failed: {e}. Nothing was imported; "
                                      f"backups in {prep.target_backup.parent}")
                return None
            exclude = {target_id, prep.source_id}
            try:
                new_id = self.garmin.import_fit(prep.merged_path, prep.start, exclude)
            except Exception as e:  # noqa: BLE001
                log.exception("Import of the merged file failed for %s", target_id)
                msg = f"Import of the merged file failed: {e}. "
                try:
                    restored = self.garmin.import_fit(prep.target_backup, prep.start, exclude)
                    self.garmin.restore(restored, name=name, description=description,
                                        type_dto=type_dto, gear=gear)
                    msg += f"The original watch activity was re-imported (id {restored})."
                except Exception as e2:  # noqa: BLE001
                    msg += (f"Re-importing the original also failed ({e2}). "
                            f"Backup files: {prep.target_backup.parent}")
                self.store.set_status(target_id, "failed", msg)
                return None

            failed = self.garmin.restore(new_id, name=name, description=description,
                                         type_dto=type_dto, gear=gear)
            msg = f"Merged ({prep.ratio:.0%} of points with a position)."
            if failed:
                msg += " Redo by hand: " + ", ".join(failed) + "."
            self.store.set_status(target_id, "merged", msg, new_id=new_id,
                                  position_ratio=prep.ratio)
            log.info("Merged %s + %s -> %s", target_id, prep.source_id, new_id)
            return new_id

    def ignore(self, target_id: int) -> None:
        self.store.set_status(target_id, "ignored", "Ignored by hand")
