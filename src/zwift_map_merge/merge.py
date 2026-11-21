"""Inject the GPS track of a source FIT (Zwift) into a target FIT (watch).

Everything the watch recorded (heart rate, footpod, power, laps, developer
fields, device info) is kept byte for byte. Only position fields, and
optionally altitude, are added to the watch's ``record`` messages, matched on
absolute timestamps.
"""
from __future__ import annotations

import bisect
import struct
from dataclasses import dataclass

from . import fitfile as ff
from .fitfile import FieldDef, FitFile, Message

F_LAT, F_LON, F_ALT, F_ENH_ALT = 0, 1, 2, 78
F_START_TIME, F_START_LAT, F_START_LON, F_END_LAT, F_END_LON = 2, 3, 4, 5, 6
F_MANUFACTURER = 1
F_TIME_CREATED = 4


@dataclass
class TrackPoint:
    ts: int  # FIT seconds
    lat: int  # semicircles
    lon: int
    alt_m: float | None


@dataclass
class MergeStats:
    target_records: int = 0
    with_position: int = 0
    already_had_position: int = 0
    with_altitude: int = 0
    source_points: int = 0

    @property
    def ratio(self) -> float:
        return self.with_position / self.target_records if self.target_records else 0.0


class MergeError(ValueError):
    pass


def manufacturer(fit: FitFile) -> int | None:
    for msg in fit.messages(ff.MESG_FILE_ID):
        return msg.get(F_MANUFACTURER)
    return None


def extract_track(fit: FitFile) -> list[TrackPoint]:
    points: list[TrackPoint] = []
    for msg in fit.messages(ff.MESG_RECORD):
        if msg.timestamp is None:
            continue
        lat, lon = msg.get(F_LAT), msg.get(F_LON)
        if lat is None or lon is None or (lat == 0 and lon == 0):
            continue
        alt = msg.get(F_ENH_ALT)
        if alt is not None:
            alt_m = alt / 5 - 500
        else:
            alt = msg.get(F_ALT)
            alt_m = alt / 5 - 500 if alt is not None else None
        points.append(TrackPoint(msg.timestamp, _signed(lat), _signed(lon), alt_m))
    points.sort(key=lambda p: p.ts)
    return points


def _signed(v: int) -> int:
    return v - (1 << 32) if v >= (1 << 31) else v


class _Track:
    def __init__(self, points: list[TrackPoint], tolerance_s: int):
        self.points = points
        self.times = [p.ts for p in points]
        self.tolerance = tolerance_s

    def at(self, ts: int) -> TrackPoint | None:
        # Never extrapolate: before Zwift started or after it stopped, no position.
        if not self.points or ts < self.times[0] or ts > self.times[-1]:
            return None
        i = bisect.bisect_left(self.times, ts)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.points):
                p = self.points[j]
                if abs(p.ts - ts) <= self.tolerance and (best is None or abs(p.ts - ts) < abs(best.ts - ts)):
                    best = p
        return best


def _invalid_bytes(f: FieldDef, endian: str) -> bytes:
    bt = ff.BASE_TYPES.get(f.base_type)
    if bt is None or bt[0] != f.size:
        return b"\xff" * f.size
    return struct.pack(endian + bt[1], bt[2])


def _ensure_fields(fit: FitFile, global_num: int, wanted: list[FieldDef]) -> None:
    """Append missing fields to every definition of ``global_num``.

    Messages using an extended definition get invalid placeholder bytes, so
    the file stays valid even where no value is written.
    """
    # id(definition) -> (byte size of the original fields, padding to insert)
    extended: dict[int, tuple[int, bytes]] = {}
    for item in fit.items:
        if isinstance(item, ff.Definition) and item.global_num == global_num:
            present = {f.num for f in item.fields}
            missing = [FieldDef(w.num, w.size, w.base_type) for w in wanted if w.num not in present]
            if missing:
                cut = sum(f.size for f in item.fields)
                pad = b"".join(_invalid_bytes(f, item.endian) for f in missing)
                extended[id(item)] = (cut, pad)
                item.fields.extend(missing)
        elif isinstance(item, Message) and id(item.definition) in extended:
            cut, pad = extended[id(item.definition)]
            item.raw = item.raw[:cut] + pad + item.raw[cut:]


def _set(msg: Message, field_num: int, value: int) -> bool:
    offsets = msg.definition.field_offsets()
    if field_num not in offsets:
        return False
    pos, f = offsets[field_num]
    bt = ff.BASE_TYPES.get(f.base_type)
    if bt is None or bt[0] != f.size:
        return False
    packed = struct.pack(msg.definition.endian + bt[1], value)
    msg.raw = msg.raw[:pos] + packed + msg.raw[pos + f.size:]
    return True


def _encode_alt(alt_m: float, base_type: int) -> int:
    raw = round((alt_m + 500) * 5)
    limit = 0xFFFE if base_type == ff.UINT16 else 0xFFFFFFFE
    return max(0, min(raw, limit))


def merge_track(target_bytes: bytes, source_bytes: bytes, *, tolerance_s: int = 2,
                copy_altitude: bool = False) -> tuple[bytes, MergeStats]:
    target = ff.parse(target_bytes)
    source = ff.parse(source_bytes)
    points = extract_track(source)
    stats = MergeStats(source_points=len(points))
    if not points:
        raise MergeError("the source activity has no GPS points")
    track = _Track(points, tolerance_s)

    wanted = [FieldDef(F_LAT, 4, ff.SINT32), FieldDef(F_LON, 4, ff.SINT32)]
    if copy_altitude:
        has_alt = any(
            isinstance(i, ff.Definition) and i.global_num == ff.MESG_RECORD
            and any(f.num in (F_ALT, F_ENH_ALT) for f in i.fields)
            for i in target.items
        )
        if not has_alt:
            wanted.append(FieldDef(F_ENH_ALT, 4, ff.UINT32))
    _ensure_fields(target, ff.MESG_RECORD, wanted)

    for msg in target.messages(ff.MESG_RECORD):
        if msg.timestamp is None:
            continue
        stats.target_records += 1
        if msg.get(F_LAT) is not None and msg.get(F_LON) is not None:
            stats.already_had_position += 1
            stats.with_position += 1
            continue
        p = track.at(msg.timestamp)
        if p is None:
            continue
        _set(msg, F_LAT, p.lat)
        _set(msg, F_LON, p.lon)
        stats.with_position += 1
        if copy_altitude and p.alt_m is not None:
            offsets = msg.definition.field_offsets()
            wrote = False
            for num in (F_ENH_ALT, F_ALT):
                if num in offsets:
                    wrote |= _set(msg, num, _encode_alt(p.alt_m, offsets[num][1].base_type))
            if wrote:
                stats.with_altitude += 1

    # Start/end positions of laps and session, when the watch defined them.
    for gnum in (ff.MESG_LAP, ff.MESG_SESSION):
        for msg in target.messages(gnum):
            start = msg.get(F_START_TIME)
            if start is not None and msg.get(F_START_LAT) is None:
                p = track.at(start) or _nearest_any(points, start)
                if p:
                    _set(msg, F_START_LAT, p.lat)
                    _set(msg, F_START_LON, p.lon)
            if gnum == ff.MESG_LAP and msg.timestamp is not None and msg.get(F_END_LAT) is None:
                p = track.at(msg.timestamp) or _nearest_any(points, msg.timestamp)
                if p:
                    _set(msg, F_END_LAT, p.lat)
                    _set(msg, F_END_LON, p.lon)

    # Garmin refuses a file whose file_id (device + time created) it has seen before,
    # even right after the activity was deleted. One second later on time_created
    # makes the merged file new to Garmin; the device identity is unchanged.
    for msg in target.messages(ff.MESG_FILE_ID):
        created = msg.get(F_TIME_CREATED)
        if created is not None:
            _set(msg, F_TIME_CREATED, created + 1)
        break

    return ff.encode(target), stats


def _nearest_any(points: list[TrackPoint], ts: int, max_gap: int = 120) -> TrackPoint | None:
    best = min(points, key=lambda p: abs(p.ts - ts))
    return best if abs(best.ts - ts) <= max_gap else None
