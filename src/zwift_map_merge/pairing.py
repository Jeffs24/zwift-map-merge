"""Find (watch activity, Zwift activity) pairs in a Garmin activity list."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


@dataclass
class Summary:
    id: int
    name: str
    start: datetime  # UTC
    duration_s: float
    has_polyline: bool
    type_key: str
    manufacturer: str

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_s)

    def looks_like_source(self, names: Iterable[str]) -> bool:
        """Activity uploaded by a virtual platform (Zwift by default)."""
        names = [n.lower() for n in names]
        return (
            self.has_polyline
            and (self.manufacturer.lower() in names
                 or any(n in self.name.lower() for n in names))
        )


@dataclass
class Pair:
    target: Summary  # watch activity: full data, no map
    source: Summary  # Zwift activity: map
    overlap: float   # shared time / combined time


def parse_summary(a: dict[str, Any]) -> Summary | None:
    try:
        start = datetime.strptime(a["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    return Summary(
        id=int(a["activityId"]),
        name=a.get("activityName") or "",
        start=start,
        duration_s=float(a.get("duration") or a.get("elapsedDuration") or 0),
        has_polyline=bool(a.get("hasPolyline")),
        type_key=((a.get("activityType") or {}).get("typeKey") or ""),
        manufacturer=str(a.get("manufacturer") or ""),
    )


def overlap_ratio(a: Summary, b: Summary) -> float:
    """Shared time over combined time (1.0 = same start and end)."""
    shared = (min(a.end, b.end) - max(a.start, b.start)).total_seconds()
    union = (max(a.end, b.end) - min(a.start, b.start)).total_seconds()
    if shared <= 0 or union <= 0:
        return 0.0
    return shared / union


def find_pairs(activities: Iterable[dict[str, Any]], *, source_names: Iterable[str] = ("zwift",),
               min_overlap: float = 0.5, exclude_ids: set[int] | None = None) -> list[Pair]:
    source_names = list(source_names)
    exclude_ids = exclude_ids or set()
    summaries = [s for s in (parse_summary(a) for a in activities) if s and s.id not in exclude_ids]
    sources = [s for s in summaries if s.looks_like_source(source_names)]
    targets = [s for s in summaries if not s.has_polyline and not s.looks_like_source(source_names)]

    pairs: list[Pair] = []
    used_sources: set[int] = set()
    candidates = sorted(
        ((overlap_ratio(t, s), t, s) for t in targets for s in sources),
        key=lambda c: c[0], reverse=True,
    )
    used_targets: set[int] = set()
    for ratio, t, s in candidates:
        if ratio < min_overlap or t.id in used_targets or s.id in used_sources:
            continue
        used_targets.add(t.id)
        used_sources.add(s.id)
        pairs.append(Pair(t, s, ratio))
    pairs.sort(key=lambda p: p.target.start, reverse=True)
    return pairs
