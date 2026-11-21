"""Scan, simulate and merge pairs."""
from __future__ import annotations

import gzip
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import fitfile as ff
from .config import Config
from .garmin import DuplicateActivity, GarminSession
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
        return self._pair_source(filename, data, label=f"File {filename}")[1]

    def _pair_source(self, filename: str, data: bytes, *, label: str,
                     garmin_activities: list[dict] | None = None,
                     keep_existing: bool = False) -> tuple[int | None, str]:
        """Pair a Zwift FIT with the overlapping watch activity without a map.

        Returns (watch activity id or None, message). ``garmin_activities``
        avoids one Garmin request per file during a history sync.
        ``keep_existing`` leaves a pair found by the scan untouched (it can
        still delete the Zwift copy on Garmin).
        """
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        try:
            fit = ff.parse(data)
        except ff.FitError as e:
            return None, f"{filename}: unreadable FIT file ({e})"
        man = manufacturer(fit)
        if man not in self.cfg.source_manufacturers:
            return None, f"{filename}: not a Zwift file (manufacturer {man})"
        track = extract_track(fit)
        if not track:
            return None, f"{filename}: no GPS points"
        start = datetime.fromtimestamp(ff.unix_from_fit(track[0].ts), timezone.utc)
        end = datetime.fromtimestamp(ff.unix_from_fit(track[-1].ts), timezone.utc)
        source = Summary(0, filename, start, (end - start).total_seconds(), True, "", "zwift")

        if garmin_activities is None:
            garmin_activities = self.garmin.activities_between(
                start.date() - timedelta(days=1), end.date() + timedelta(days=1))
        handled = self.store.handled_ids()
        candidates = []
        for a in garmin_activities:
            s = parse_summary(a)
            if not s or s.has_polyline or s.id in handled:
                continue
            ratio = overlap_ratio(s, source)
            if ratio >= self.cfg.min_overlap:
                candidates.append((ratio, s))
        if not candidates:
            return None, (f"{filename}: no watch activity without a map matches "
                          f"({start:%Y-%m-%d %H:%M} UTC)")
        ratio, target = max(candidates, key=lambda c: c[0])
        if keep_existing and self.store.get(target.id):
            return target.id, f"{filename}: “{target.name}” is already in the list"

        folder = self.cfg.backup_dir / str(target.id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "zwift_uploaded.fit"
        path.write_bytes(data)
        if not self.store.upsert_detected(target.id, 0, target.name, label,
                                          target.start.isoformat(), ratio, source_file=str(path)):
            return target.id, f"{filename}: activity “{target.name}” is already handled"
        return target.id, f"{filename} → “{target.name}” ({target.start:%Y-%m-%d %H:%M} UTC)"

    # --- Zwift history ------------------------------------------------------

    def sync_zwift(self, zwift, since: datetime, progress=None) -> dict[str, int]:
        """Download Zwift FIT files since ``since`` and pair them with watch activities.

        Activities already looked at are skipped, so running it again only
        fetches new ones. ``progress(counts, message)`` is called after each one.
        """
        counts = {"listed": 0, "seen": 0, "downloaded": 0, "paired": 0, "skipped": 0, "errors": 0}

        def report(message: str) -> None:
            log.info("Zwift sync: %s", message)
            if progress:
                progress(counts, message)

        # 1. List Zwift first: it is quick and tells which period matters.
        report("Listing your Zwift activities…")
        zwift_acts = []
        for act in zwift.history(since):
            zwift_acts.append(act)
            counts["listed"] += 1
            if counts["listed"] % 30 == 0:
                report(f"Listing your Zwift activities… {counts['listed']} found")
        dated = [a.start for a in zwift_acts if a.start]
        if not dated:
            report(f"No Zwift activity found since {since:%Y-%m-%d}.")
            return counts

        # 2. Load Garmin activities only over the period covered by Zwift.
        first, last = min(dated), max(dated)
        report(f"{counts['listed']} Zwift activities found ({first:%Y-%m-%d} to {last:%Y-%m-%d}). "
               "Loading your Garmin activities for that period…")
        garmin_acts = self.garmin.activities_between(first.date() - timedelta(days=1),
                                                     last.date() + timedelta(days=1))
        handled = self.store.handled_ids()
        # Only Zwift activities that overlap a watch activity without a map are downloaded.
        watch = [s for s in (parse_summary(a) for a in garmin_acts)
                 if s and not s.has_polyline and s.id not in handled]
        report(f"{len(garmin_acts)} Garmin activities loaded, {len(watch)} without a map. "
               "Matching and downloading…")

        # 3. Download and pair.
        for act in zwift_acts:
            counts["seen"] += 1
            if self.store.zwift_seen(act.id):
                counts["skipped"] += 1
                continue
            start_iso = act.start.isoformat() if act.start else None
            near = act.start and any(
                w.start <= act.start + timedelta(hours=1) and w.end >= act.start - timedelta(hours=1)
                for w in watch)
            if not near:
                # Recent activities may still be on their way to Garmin: look again next time.
                if act.start and act.start < datetime.now(timezone.utc) - timedelta(days=2):
                    self.store.mark_zwift_seen(act.id, act.name, start_iso, None,
                                               "no watch activity without a map at that time")
                counts["skipped"] += 1
                continue
            try:
                data = zwift.download_fit(act)
                counts["downloaded"] += 1
                target_id, msg = self._pair_source(
                    f"{act.name or 'Zwift'} ({act.start:%Y-%m-%d})", data,
                    label=f"Zwift: {act.name}", garmin_activities=garmin_acts, keep_existing=True)
            except Exception as e:  # noqa: BLE001 - keep going, retried next sync
                log.warning("Zwift activity %s failed: %s", act.id, e)
                counts["errors"] += 1
                report(f"{act.name}: {e}")
                continue
            if target_id:
                counts["paired"] += 1
            self.store.mark_zwift_seen(act.id, act.name, start_iso, target_id, msg)
            report(msg)
        report(f"Done: {counts['listed']} Zwift activities listed, {counts['downloaded']} downloaded, "
               f"{counts['paired']} paired.")
        return counts

    def scan_and_maybe_merge(self) -> None:
        self.scan()
        self.merge_pending()

    def merge_pending(self) -> None:
        """In automatic mode, merge every pair waiting for a click."""
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

    def _from_backup(self, target_id: int) -> Prepared | None:
        """Files of an earlier attempt, for when the watch activity is no longer on Garmin."""
        row = self.store.get(target_id)
        folder = self.cfg.backup_dir / str(target_id)
        watch = folder / f"{target_id}_watch.fit"
        source = Path(row["source_file"]) if row and row["source_file"] else \
            folder / f"{row['source_id'] if row else 0}_zwift.fit"
        if not (row and watch.exists() and source.exists()):
            return None
        # Rebuild the merged file: one written by an older version may carry the
        # original file_id, which Garmin refuses as already uploaded.
        merged_bytes, stats = merge_track(watch.read_bytes(), source.read_bytes(),
                                          tolerance_s=self.cfg.time_tolerance_s,
                                          copy_altitude=self.cfg.copy_altitude)
        merged = folder / f"{target_id}_merged.fit"
        merged.write_bytes(merged_bytes)
        return Prepared(target_id, row["source_id"], merged, watch, stats.ratio,
                        datetime.fromisoformat(row["start_utc"]))

    def _metadata(self, target_id: int, folder: Path, fallback_name: str | None) -> dict:
        """Name, description, type and gear of the watch activity, saved next to the backups."""
        path = folder / "metadata.json"
        try:
            detail = self.garmin.activity(target_id)
            meta = {"name": detail.get("activityName"), "description": detail.get("description"),
                    "type_dto": detail.get("activityTypeDTO"), "gear": self.garmin.gear_uuids(target_id)}
            path.write_text(json.dumps(meta))
            return meta
        except Exception as e:  # noqa: BLE001 - activity may already be gone
            log.warning("Could not read metadata of %s: %s", target_id, e)
        if path.exists():
            return json.loads(path.read_text())
        return {"name": fallback_name, "description": None, "type_dto": None, "gear": []}

    def merge(self, target_id: int) -> int | None:
        """Replace the watch activity with the merged one, without ever losing it.

        The merged file is imported first; the original watch activity and the
        Zwift copy are deleted only once the merged activity exists. If Garmin
        refuses the import as a duplicate of the original, the original is
        deleted first and re-imported if the merged import still fails.
        Every Garmin answer is checked against the activity list, because
        Garmin sometimes reports an error (504) for a request that went through.
        """
        with self._lock:
            row = self.store.get(target_id)
            if not row:
                return None
            if row["status"] == "merged":
                return row["new_id"]
            try:
                prep = self._prepare(target_id)
                target_on_garmin = True
            except Exception as e:  # noqa: BLE001
                prep = None
                try:
                    if not self.garmin.exists(target_id, datetime.fromisoformat(row["start_utc"])):
                        prep = self._from_backup(target_id)
                except Exception:  # noqa: BLE001
                    log.exception("Recovery from backups failed for %s", target_id)
                if prep is None:
                    log.exception("Could not prepare %s", target_id)
                    self.store.set_status(target_id, "failed", f"Preparation: {e}")
                    return None
                # Recovery: an earlier attempt deleted the watch activity but did not import.
                log.info("Activity %s is no longer on Garmin: importing the saved merged file", target_id)
                target_on_garmin = False

            folder = prep.target_backup.parent
            meta = self._metadata(target_id, folder, row["target_name"])
            exclude = {target_id, prep.source_id}
            steps: list[str] = []

            def fail(message: str) -> None:
                self.store.set_status(target_id, "failed",
                                      message + f" Backups: {folder}. Click Merge again to retry.")

            new_id = None
            if target_on_garmin:
                try:
                    new_id = self.garmin.import_fit(prep.merged_path, prep.start, exclude)
                except DuplicateActivity:
                    steps.append("Garmin saw the merged file as a duplicate of the original")
                except Exception as e:  # noqa: BLE001
                    log.exception("Import of the merged file failed for %s", target_id)
                    fail(f"Import of the merged file failed: {e}. Nothing was deleted.")
                    return None
                if new_id is None:
                    # Garmin needs the original gone before it takes the merged file.
                    try:
                        self.garmin.delete(target_id, prep.start)
                    except Exception as e:  # noqa: BLE001
                        fail(f"{steps[0]}, and deleting the original failed: {e}. Nothing changed.")
                        return None
                    target_on_garmin = False

            if new_id is None:
                try:
                    new_id = self.garmin.import_fit(prep.merged_path, prep.start, exclude)
                except Exception as e:  # noqa: BLE001
                    log.exception("Import of the merged file failed for %s", target_id)
                    msg = f"Import of the merged file failed: {e}. "
                    try:
                        restored = self.garmin.import_fit(prep.target_backup, prep.start, exclude)
                        self.garmin.restore(restored, **meta)
                        msg += f"The original watch activity was re-imported (id {restored})."
                    except Exception as e2:  # noqa: BLE001
                        msg += f"Re-importing the original also failed ({e2})."
                    fail(msg)
                    return None

            # The merged activity exists: now remove what it replaces.
            leftovers = []
            to_delete = [target_id] if target_on_garmin else []
            if self.cfg.delete_source and prep.source_id:
                to_delete.append(prep.source_id)
            for aid in to_delete:
                try:
                    self.garmin.delete(aid, prep.start)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not delete %s: %s", aid, e)
                    leftovers.append(str(aid))

            failed = self.garmin.restore(new_id, **meta)
            msg = f"Merged ({prep.ratio:.0%} of points with a position)."
            if failed:
                msg += " Redo by hand: " + ", ".join(failed) + "."
            if leftovers:
                msg += " Delete by hand on Garmin: activity " + ", ".join(leftovers) + "."
            self.store.set_status(target_id, "merged", msg, new_id=new_id,
                                  position_ratio=prep.ratio)
            log.info("Merged %s + %s -> %s", target_id, prep.source_id, new_id)
            return new_id

    def merge_all(self) -> int:
        """Merge every pair waiting in the list, one after the other."""
        done = 0
        for row in self.store.all():
            if row["status"] in ("detected", "simulated", "queued"):
                if self.merge(row["target_id"]):
                    done += 1
        return done

    def ignore(self, target_id: int) -> None:
        self.store.set_status(target_id, "ignored", "Ignored by hand")
