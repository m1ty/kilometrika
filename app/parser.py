"""Parse TCX files into summary + trackpoint series."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone


CATEGORIES = {
    "run":  ("бег", "run", "jog"),
    "bike": ("вел", "bik", "cycl", "ride"),
}


def sport_category(sport: str | None) -> str:
    """Единственное место, где решается 'бег это или велосипед'."""
    s = (sport or "").lower()
    for cat, keys in CATEGORIES.items():
        if any(k in s for k in keys):
            return cat
    return "other"


def _as_utc(dt: datetime | None) -> datetime | None:
    """TCX/GPX времена — UTC; tcxreader теряет tzinfo, восстанавливаем."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

from math import asin, cos, radians, sin, sqrt

from tcxreader.tcxreader import TCXReader


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Distance in meters between two lat/lon points."""
    rlat1, rlon1, rlat2, rlon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    return 6_371_000 * 2 * asin(sqrt(a))


@dataclass
class Trackpoint:
    time: datetime
    lat: float | None
    lon: float | None
    elevation: float | None
    distance: float | None       # cumulative meters
    hr: int | None
    cadence: int | None
    speed: float | None          # m/s (from TPX extension if present)
    watts: float | None


@dataclass
class Activity:
    file_hash: str
    sport: str
    category: str
    start_time: datetime
    duration_s: float
    distance_m: float
    calories: int | None
    hr_avg: float | None
    hr_max: float | None
    ascent_m: float | None
    descent_m: float | None
    cadence_avg: float | None
    laps: list[dict] = field(default_factory=list)
    trackpoints: list[Trackpoint] = field(default_factory=list)

    @property
    def avg_speed_kmh(self) -> float | None:
        if self.duration_s and self.distance_m:
            return round(self.distance_m / self.duration_s * 3.6, 2)
        return None

    @property
    def avg_pace_min_km(self) -> float | None:
        """Minutes per km — the runner's view of the same number."""
        if self.duration_s and self.distance_m:
            return round((self.duration_s / 60) / (self.distance_m / 1000), 2)
        return None


def _fill_missing_from_gps(tps: list[Trackpoint]) -> None:
    """Some exports (e.g. Huawei/Mi 'Health' apps) write only GPS + altitude:
    no per-point DistanceMeters and no Speed. Reconstruct both from
    coordinates so charts and pace still work."""
    if not tps:
        return
    have_distance = sum(1 for t in tps if t.distance is not None)
    if have_distance >= len(tps) * 0.5:      # file already has proper data
        return

    cum = 0.0
    prev = None
    for tp in tps:
        if prev is not None and None not in (tp.lat, tp.lon, prev.lat, prev.lon):
            step = _haversine_m(prev.lat, prev.lon, tp.lat, tp.lon)
            dt = (tp.time - prev.time).total_seconds()
            # ignore GPS jitter jumps (>30 m/s is not a human on foot/bike)
            if dt > 0 and step / dt < 30:
                cum += step
                if tp.speed is None:
                    tp.speed = round(step / dt, 2)
        tp.distance = round(cum, 1)
        prev = tp


def parse_tcx(path: str) -> Activity:
    with open(path, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()[:16]

    data = TCXReader().read(path)

    laps = []
    for lap in getattr(data, "laps", []) or []:
        laps.append({
            "start_time": lap.start_time.isoformat() if lap.start_time else None,
            "duration_s": lap.duration,
            "distance_m": lap.distance,
            "calories": lap.calories,
            "hr_avg": lap.hr_avg,
            "hr_max": lap.hr_max,
        })

    tps = []
    for tp in data.trackpoints:
        ext = tp.tpx_ext or {}
        tps.append(Trackpoint(
            time=tp.time,
            lat=tp.latitude,
            lon=tp.longitude,
            elevation=tp.elevation,
            distance=tp.distance,
            hr=tp.hr_value,
            cadence=tp.cadence,
            speed=ext.get("Speed"),
            watts=ext.get("Watts"),
        ))

    _fill_missing_from_gps(tps)

    return Activity(
        file_hash=file_hash,
        sport=data.activity_type or "Other",
        category=sport_category(data.activity_type),
        start_time=_as_utc(data.start_time),
        duration_s=data.duration or 0.0,
        distance_m=data.distance or 0.0,
        calories=data.calories,
        hr_avg=data.hr_avg,
        hr_max=data.hr_max,
        ascent_m=data.ascent,
        descent_m=data.descent,
        cadence_avg=data.cadence_avg,
        laps=laps,
        trackpoints=tps,
    )


# ---------------------------------------------------------------- GPX

_GPX_SPORT = {
    # частые значения <type> у Strava/Garmin/OsmAnd
    "running": "Running", "run": "Running", "trail_running": "Running",
    "cycling": "Biking", "biking": "Biking", "ride": "Biking",
    "walking": "Walking", "hiking": "Hiking", "swimming": "Swimming",
}


def _gpx_ext_int(point, *names) -> int | None:
    """Достаёт hr/cad из TrackPointExtension независимо от namespace-префикса."""
    for ext in point.extensions or []:
        for el in ext.iter() if hasattr(ext, "iter") else [ext]:
            tag = el.tag.rsplit("}", 1)[-1].lower()
            if tag in names and el.text and el.text.strip().isdigit():
                return int(el.text.strip())
    return None


def parse_gpx(path: str) -> Activity:
    import gpxpy

    with open(path, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()[:16]

    with open(path, encoding="utf-8-sig") as f:   # -sig: некоторые экспортёры пишут BOM
        gpx = gpxpy.parse(f)

    tps: list[Trackpoint] = []
    sport = "Other"
    for track in gpx.tracks:
        if track.type:
            sport = _GPX_SPORT.get(track.type.strip().lower(), track.type)
        elif track.name and sport == "Other":
            sport = track.name
        for seg in track.segments:
            for p in seg.points:
                tps.append(Trackpoint(
                    time=p.time,
                    lat=p.latitude, lon=p.longitude,
                    elevation=p.elevation,
                    distance=None,             # в GPX нет — восстановим по GPS
                    hr=_gpx_ext_int(p, "hr", "heartrate"),
                    cadence=_gpx_ext_int(p, "cad", "cadence"),
                    speed=None,
                    watts=None,
                ))

    tps = [t for t in tps if t.time is not None]
    if not tps:
        raise ValueError("GPX has no timestamped trackpoints")
    tps.sort(key=lambda t: t.time)
    _fill_missing_from_gps(tps)

    start = _as_utc(tps[0].time) if tps else None
    duration = (tps[-1].time - tps[0].time).total_seconds() if len(tps) > 1 else 0.0
    distance = tps[-1].distance if tps and tps[-1].distance else 0.0
    hrs = [t.hr for t in tps if t.hr]
    up, _down = gpx.get_uphill_downhill()

    return Activity(
        file_hash=file_hash,
        sport=sport,
        category=sport_category(sport),
        start_time=start,
        duration_s=duration,
        distance_m=distance,
        calories=None,
        hr_avg=round(sum(hrs) / len(hrs), 1) if hrs else None,
        hr_max=max(hrs) if hrs else None,
        ascent_m=round(up, 1) if up else None,
        descent_m=round(_down, 1) if _down else None,
        cadence_avg=None,
        laps=[],
        trackpoints=tps,
    )


# ---------------------------------------------------------------- dispatcher

PARSERS = {".tcx": parse_tcx, ".gpx": parse_gpx}
SUPPORTED_EXTENSIONS = tuple(PARSERS)


def parse_activity(path: str) -> Activity:
    from pathlib import Path
    ext = Path(path).suffix.lower()
    if ext not in PARSERS:
        raise ValueError(f"unsupported format: {ext} (supported: {', '.join(PARSERS)})")
    return PARSERS[ext](path)
