"""Summarize a FIT file for the preview page: stats, track and series."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import fitfile as ff

SEMI_TO_DEG = 180 / 2**31

MESG_SPORT = 12
SPORTS = {0: "Generic", 1: "Running", 2: "Cycling", 4: "Fitness equipment", 5: "Swimming",
          11: "Walking", 17: "Hiking"}
SUB_SPORTS = {0: "", 1: "Treadmill", 2: "Street", 3: "Trail", 4: "Track", 6: "Indoor cycling",
              7: "Road", 8: "Mountain", 45: "Indoor running", 58: "Virtual activity"}

# record fields
R_LAT, R_LON, R_ALT, R_HR, R_CAD, R_DIST, R_SPEED, R_POWER, R_ENH_SPEED, R_ENH_ALT = (
    0, 1, 2, 3, 4, 5, 6, 7, 73, 78)
# session fields
S_SPORT, S_SUB_SPORT, S_ELAPSED, S_TIMER, S_DIST = 5, 6, 7, 8, 9
S_CALORIES, S_AVG_HR, S_MAX_HR, S_AVG_CAD, S_ASCENT, S_DESCENT = 11, 16, 17, 18, 22, 23
S_AVG_POWER = 20


@dataclass
class Preview:
    start: datetime | None = None
    sport: str = ""
    stats: dict[str, str] = field(default_factory=dict)
    track: list[tuple[float, float]] = field(default_factory=list)  # lat, lon (degrees)
    series: dict[str, list[tuple[float, float]]] = field(default_factory=dict)  # minutes, value
    records: int = 0
    records_with_position: int = 0
    laps: int = 0


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _pace(m_per_s: float) -> str:
    if m_per_s <= 0:
        return "–"
    sec = 1000 / m_per_s
    return f"{int(sec // 60)}:{int(sec % 60):02d} /km"


def _downsample(points: list, limit: int) -> list:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    out = [points[int(i * step)] for i in range(limit)]
    out.append(points[-1])
    return out


def summarize(data: bytes, max_points: int = 1500) -> Preview:
    fit = ff.parse(data)
    p = Preview()
    t0: int | None = None
    hr, alt, speed, cad, power = [], [], [], [], []
    for m in fit.messages(ff.MESG_RECORD):
        if m.timestamp is None:
            continue
        if t0 is None:
            t0 = m.timestamp
        minute = (m.timestamp - t0) / 60
        p.records += 1
        lat, lon = m.get(R_LAT), m.get(R_LON)
        if lat is not None and lon is not None and not (lat == 0 and lon == 0):
            p.records_with_position += 1
            p.track.append((round(lat * SEMI_TO_DEG, 6), round(lon * SEMI_TO_DEG, 6)))
        if (v := m.get(R_HR)) is not None:
            hr.append((minute, v))
        a = m.get(R_ENH_ALT)
        a = a if a is not None else m.get(R_ALT)
        if a is not None:
            alt.append((minute, a / 5 - 500))
        s = m.get(R_ENH_SPEED)
        s = s if s is not None else m.get(R_SPEED)
        if s is not None:
            speed.append((minute, s / 1000))
        if (v := m.get(R_CAD)) is not None:
            cad.append((minute, v))
        if (v := m.get(R_POWER)) is not None:
            power.append((minute, v))
    if t0 is not None:
        p.start = datetime.fromtimestamp(ff.unix_from_fit(t0), timezone.utc)

    p.laps = sum(1 for _ in fit.messages(ff.MESG_LAP))
    session = next(fit.messages(ff.MESG_SESSION), None)
    sport = sub = None
    if session is not None:
        sport, sub = session.get(S_SPORT), session.get(S_SUB_SPORT)
    if sport is None:
        sm = next(fit.messages(MESG_SPORT), None)
        if sm is not None:
            sport, sub = sm.get(0), sm.get(1)
    p.sport = " · ".join(x for x in (SPORTS.get(sport, ""), SUB_SPORTS.get(sub, "")) if x)

    st = p.stats
    if session is not None:
        timer, elapsed, dist = session.get(S_TIMER), session.get(S_ELAPSED), session.get(S_DIST)
        if timer is not None:
            st["Time"] = _fmt_duration(timer / 1000)
        if elapsed is not None:
            st["Elapsed time"] = _fmt_duration(elapsed / 1000)
        if dist:  # indoor files without a speed sensor report 0
            st["Distance"] = f"{dist / 100000:.2f} km"
            if timer and sport == 1:
                st["Avg pace"] = _pace(dist / 100 / (timer / 1000))
            elif timer:
                st["Avg speed"] = f"{dist / 100 / (timer / 1000) * 3.6:.1f} km/h"
        for key, label, unit in ((S_AVG_HR, "Avg heart rate", " bpm"), (S_MAX_HR, "Max heart rate", " bpm"),
                                 (S_AVG_CAD, "Avg cadence", " spm" if sport == 1 else " rpm"),
                                 (S_AVG_POWER, "Avg power", " W"), (S_ASCENT, "Ascent", " m"),
                                 (S_CALORIES, "Calories", " kcal")):
            v = session.get(key)
            if v is not None:
                st[label] = f"{v * 2 if key == S_AVG_CAD and sport == 1 else v}{unit}"
    st["Laps"] = str(p.laps)
    st["GPS points"] = f"{p.records_with_position} / {p.records}"

    p.track = _downsample(p.track, max_points)
    for name, values in (("Heart rate (bpm)", hr), ("Pace (min/km)" if sport == 1 else "Speed (km/h)", speed),
                         ("Elevation (m)", alt), ("Cadence (spm)" if sport == 1 else "Cadence (rpm)", cad),
                         ("Power (W)", power)):
        if not values:
            continue
        if name == "Cadence (spm)":  # FIT stores strides per minute for running
            values = [(t, v * 2) for t, v in values]
        elif name.startswith("Pace"):
            values = [(t, 1000 / v / 60) for t, v in values if v > 0.5]
        elif name.startswith("Speed"):
            values = [(t, v * 3.6) for t, v in values]
        if values:
            p.series[name] = _downsample(values, 600)
    return p
