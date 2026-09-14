"""Daily mean temperatures for one grid cell: archive + forecast + normals.

The season module needs temperatures the existing forecast plumbing does not
cover: the *past*, back to sowing, and the *far future*, past the 16-day
forecast horizon.  Three sources are stitched into one continuous series:

===============  ==============================  ===================
Span             Source                          Cache TTL
===============  ==============================  ===================
sowing → T-6 d   Open-Meteo historical archive   ``history_ttl_days``
T-6 d → T+14 d   Open-Meteo forecast             forecast TTL (3 h)
beyond           30-year daily climatological     ``normals_ttl_days``
                 normal for the cell
===============  ==============================  ===================

Why the split TTLs: a past day's weather is immutable once the day is over, so
it is cached for months keyed on ``(lat, lon, year)``; the forecast changes
through the day and keeps the host's short TTL.  That is the separate history
cache spec §6.2 asks for, not a widening of the existing forecast cache.

One grid cell per folder by default — Finnish parcels in a folder are usually
within a few kilometres.  :func:`span_km` flags a folder wide enough to need
per-job cells instead.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from flightmanager.season.config import SeasonConfig
from flightmanager.season.phenology import DailyTemp, TemperatureSeries

log = logging.getLogger(__name__)

#: Bump when a cached payload's shape changes so stale files are re-fetched.
_CACHE_VERSION = 1

_ARCHIVE_DAILY = "temperature_2m_mean"
_FORECAST_DAILY = "temperature_2m_mean"

#: Open-Meteo's forecast endpoint also serves a short run of recent past days,
#: which covers the archive's own publication lag.
_FORECAST_PAST_DAYS = 14


class WeatherHistoryError(RuntimeError):
    """Raised when no series can be built and no cache can stand in for one."""


# ---------------------------------------------------------------------------
# Geometry helper
# ---------------------------------------------------------------------------


def span_km(points: list[tuple[float, float]]) -> float:
    """Greatest distance between any two (lat, lon) points, in kilometres.

    Equirectangular approximation — plenty for a "is this folder too wide for
    one weather cell?" check at Finnish latitudes.
    """
    if len(points) < 2:
        return 0.0
    worst = 0.0
    for i, (lat1, lon1) in enumerate(points):
        for lat2, lon2 in points[i + 1 :]:
            mean_lat = math.radians((lat1 + lat2) / 2)
            dx = math.radians(lon2 - lon1) * math.cos(mean_lat)
            dy = math.radians(lat2 - lat1)
            worst = max(worst, math.hypot(dx, dy) * 6371.0)
    return worst


def cell_for(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Representative cell for a set of centroids: the mean, rounded to ~1 km."""
    if not points:
        raise ValueError("no points to derive a weather cell from")
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)
    return (round(lat, 2), round(lon, 2))


# ---------------------------------------------------------------------------
# Cache primitives
# ---------------------------------------------------------------------------


def _cache_path(cache_dir: str | Path, kind: str, name: str) -> Path:
    return Path(cache_dir) / "season" / kind / f"{name}.json"


def _read_cache(path: Path, ttl_days: float | None) -> dict[str, Any] | None:
    """Load a cached payload; ``None`` on miss, bad JSON or version mismatch.

    *ttl_days* of ``None`` means "never expires" — used for a calendar year
    that is entirely in the past, whose weather cannot change again.
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if raw.get("v") != _CACHE_VERSION:
        return None
    if ttl_days is not None:
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days > ttl_days:
            raw["_stale"] = True
    return raw


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Best-effort atomic cache write; a caching failure must not fail a plan."""
    import os
    import tempfile

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"v": _CACHE_VERSION, **payload}
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("Could not write season weather cache %s: %s", path, exc)


def _record_bytes(n: int) -> None:
    """Feed the host's network-usage counters when they are available."""
    try:
        import flightmanager.net_stats as ns

        ns.record_download("weather", n)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Open-Meteo adapters
# ---------------------------------------------------------------------------


def _get_json(
    url: str, params: dict, timeout_s: int, session: requests.Session | None
) -> dict:
    sess = session or requests.Session()
    owns = session is None
    try:
        resp = sess.get(url, params=params, timeout=timeout_s)
        resp.raise_for_status()
        _record_bytes(len(resp.content))
        return resp.json()
    finally:
        if owns:
            sess.close()


def _daily_pairs(data: dict) -> dict[str, float]:
    """``{"YYYY-MM-DD": mean_c}`` from an Open-Meteo daily block."""
    daily = data.get("daily") or {}
    times = daily.get("time") or []
    means = daily.get(_ARCHIVE_DAILY) or []
    out: dict[str, float] = {}
    for i, day in enumerate(times):
        if i < len(means) and means[i] is not None:
            out[day] = float(means[i])
    return out


def fetch_archive(
    lat: float,
    lon: float,
    start: _dt.date,
    end: _dt.date,
    cfg: SeasonConfig,
    cache_dir: str | Path,
    session: requests.Session | None = None,
) -> tuple[dict[str, float], bool]:
    """Measured daily means for ``[start, end]``.  Returns ``(pairs, stale)``.

    Cached per calendar year.  A year wholly in the past never expires — its
    weather is finished — so a multi-season plan re-reads it for free.
    """
    pairs: dict[str, float] = {}
    stale = False
    this_year = _dt.date.today().year

    for year in range(start.year, end.year + 1):
        y_start = max(start, _dt.date(year, 1, 1))
        y_end = min(end, _dt.date(year, 12, 31))
        if y_start > y_end:
            continue
        path = _cache_path(cache_dir, "archive", f"{lat}_{lon}_{year}")
        ttl = None if year < this_year else float(cfg.history_ttl_days)
        cached = _read_cache(path, ttl)

        if cached is not None and _covers(cached.get("days", {}), y_start, y_end):
            if cached.get("_stale"):
                stale = True
            pairs.update(cached["days"])
            continue

        if cfg.offline:
            if cached is not None:
                stale = True
                pairs.update(cached.get("days", {}))
                continue
            raise WeatherHistoryError(
                f"offline and no cached archive for {lat},{lon} in {year}"
            )

        fetched = _fetch_archive_year(lat, lon, y_start, y_end, cfg, session)
        if fetched is None:
            if cached is not None:
                stale = True
                pairs.update(cached.get("days", {}))
                continue
            raise WeatherHistoryError(
                f"could not fetch archive temperatures for {lat},{lon} in {year}"
            )
        merged = {**(cached or {}).get("days", {}), **fetched}
        _write_cache(path, {"days": merged, "lat": lat, "lon": lon, "year": year})
        pairs.update(merged)

    return (
        {d: v for d, v in pairs.items() if start.isoformat() <= d <= end.isoformat()},
        stale,
    )


def _covers(days: dict[str, float], start: _dt.date, end: _dt.date) -> bool:
    """True when *days* holds every date in ``[start, end]``."""
    day = start
    while day <= end:
        if day.isoformat() not in days:
            return False
        day += _dt.timedelta(days=1)
    return True


def _fetch_archive_year(
    lat: float,
    lon: float,
    start: _dt.date,
    end: _dt.date,
    cfg: SeasonConfig,
    session: requests.Session | None,
) -> dict[str, float] | None:
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": _ARCHIVE_DAILY,
        "timezone": "auto",
    }
    try:
        log.info("Fetching Open-Meteo archive %s..%s for %s,%s", start, end, lat, lon)
        return _daily_pairs(_get_json(cfg.archive_url, params, cfg.timeout_s, session))
    except Exception as exc:
        log.error("Open-Meteo archive fetch failed: %s", exc)
        return None


def fetch_forecast_means(
    lat: float,
    lon: float,
    cfg: SeasonConfig,
    cache_dir: str | Path,
    *,
    forecast_url: str = "https://api.open-meteo.com/v1/forecast",
    ttl_hours: float = 3.0,
    session: requests.Session | None = None,
) -> tuple[dict[str, float], bool]:
    """Daily means for the recent past and the forecast horizon.

    *forecast_url* and *ttl_hours* default to Open-Meteo's public endpoint and
    the host's 3 h forecast TTL; the caller passes ``weather.open_meteo_url``
    and ``weather.cache_max_age_hours`` so the season module tracks whatever
    the host is configured to use instead of opening its own opinion.
    """
    path = _cache_path(cache_dir, "forecast", f"{lat}_{lon}")
    cached = _read_cache(path, ttl_hours / 24.0)
    if cached is not None and not cached.get("_stale"):
        return (cached.get("days", {}), False)

    if cfg.offline:
        if cached is not None:
            return (cached.get("days", {}), True)
        return ({}, True)

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": _FORECAST_DAILY,
        "past_days": _FORECAST_PAST_DAYS,
        "forecast_days": cfg.forecast_horizon_days,
        "timezone": "auto",
    }
    try:
        log.info("Fetching Open-Meteo daily means for %s,%s", lat, lon)
        pairs = _daily_pairs(_get_json(forecast_url, params, cfg.timeout_s, session))
    except Exception as exc:
        log.error("Open-Meteo forecast fetch failed: %s", exc)
        if cached is not None:
            return (cached.get("days", {}), True)
        return ({}, True)

    if pairs:
        _write_cache(path, {"days": pairs, "lat": lat, "lon": lon})
    return (pairs, False)


# ---------------------------------------------------------------------------
# Hourly conditions — what day-level scoring cannot answer
# ---------------------------------------------------------------------------

#: Hourly fields Open-Meteo is asked for.  The host's forecast client caches
#: only *daily* aggregates plus hourly cloud, so per-hour wind and precipitation
#: have to come from somewhere; this is that somewhere.  The expensive half of
#: the forecast — MGRS tiles and orbit propagation for satellite passes — is
#: still the host's ``build_forecast``, which this module consumes rather than
#: reimplements (spec §7.1).
_HOURLY_FIELDS = (
    "temperature_2m,wind_speed_10m,wind_gusts_10m,cloud_cover,precipitation"
)


@dataclass(frozen=True)
class HourSample:
    """Conditions in one local hour."""

    hour_key: str  # "YYYY-MM-DDTHH", local
    temp_c: float | None = None
    wind_ms: float | None = None
    gust_ms: float | None = None
    cloud_pct: float | None = None
    precip_mm: float | None = None


@dataclass
class HourlyWeather:
    """Hour-resolution conditions for one grid cell, keyed by local hour."""

    by_hour: dict[str, HourSample] = field(default_factory=dict)
    utc_offset_s: int = 0
    stale: bool = False

    def on(self, day: _dt.date, hour: int) -> HourSample | None:
        return self.by_hour.get(f"{day.isoformat()}T{hour:02d}")

    def day_hours(self, day: _dt.date) -> dict[int, HourSample]:
        prefix = f"{day.isoformat()}T"
        return {
            int(k[11:13]): v for k, v in self.by_hour.items() if k.startswith(prefix)
        }

    def covers(self, day: _dt.date) -> bool:
        return bool(self.day_hours(day))


def fetch_hourly(
    lat: float,
    lon: float,
    cfg: SeasonConfig,
    cache_dir: str | Path,
    *,
    forecast_url: str = "https://api.open-meteo.com/v1/forecast",
    ttl_hours: float = 3.0,
    session: requests.Session | None = None,
) -> HourlyWeather:
    """Per-hour wind, gusts, cloud and precipitation over the forecast horizon.

    Shares the host's forecast TTL, because this is the same forecast at a
    finer grain and goes stale at the same rate.  Returns whatever the cache
    holds — flagged ``stale`` — when the network is unavailable, so scoring
    degrades to "here is what we last knew" instead of failing.
    """
    lat, lon = round(lat, 2), round(lon, 2)
    path = _cache_path(cache_dir, "hourly", f"{lat}_{lon}")
    cached = _read_cache(path, ttl_hours / 24.0)
    if cached is not None and not cached.get("_stale"):
        return _hourly_from_cache(cached)

    if cfg.offline:
        return (
            _hourly_from_cache(cached, stale=True)
            if cached is not None
            else HourlyWeather(stale=True)
        )

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": _HOURLY_FIELDS,
        "wind_speed_unit": "ms",
        "timezone": "auto",
        "forecast_days": cfg.forecast_horizon_days,
    }
    try:
        log.info("Fetching Open-Meteo hourly conditions for %s,%s", lat, lon)
        data = _get_json(forecast_url, params, cfg.timeout_s, session)
    except Exception as exc:
        log.error("Open-Meteo hourly fetch failed: %s", exc)
        return (
            _hourly_from_cache(cached, stale=True)
            if cached is not None
            else HourlyWeather(stale=True)
        )

    parsed = _parse_hourly(data)
    if parsed.by_hour:
        _write_cache(
            path,
            {
                "utc_offset_s": parsed.utc_offset_s,
                "hours": {k: _sample_dict(v) for k, v in parsed.by_hour.items()},
            },
        )
    return parsed


def _sample_dict(s: HourSample) -> dict[str, Any]:
    return {
        "t": s.temp_c,
        "w": s.wind_ms,
        "g": s.gust_ms,
        "c": s.cloud_pct,
        "p": s.precip_mm,
    }


def _hourly_from_cache(raw: dict[str, Any], stale: bool = False) -> HourlyWeather:
    hours = {
        key: HourSample(
            hour_key=key,
            temp_c=v.get("t"),
            wind_ms=v.get("w"),
            gust_ms=v.get("g"),
            cloud_pct=v.get("c"),
            precip_mm=v.get("p"),
        )
        for key, v in (raw.get("hours") or {}).items()
    }
    return HourlyWeather(
        by_hour=hours,
        utc_offset_s=int(raw.get("utc_offset_s", 0)),
        stale=stale or bool(raw.get("_stale")),
    )


def _parse_hourly(data: dict) -> HourlyWeather:
    """Open-Meteo's column-per-field hourly block → one sample per local hour."""
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []

    def column(name: str) -> list:
        return hourly.get(name) or [None] * len(times)

    series = {
        "t": column("temperature_2m"),
        "w": column("wind_speed_10m"),
        "g": column("wind_gusts_10m"),
        "c": column("cloud_cover"),
        "p": column("precipitation"),
    }
    out: dict[str, HourSample] = {}
    for i, stamp in enumerate(times):
        key = stamp[:13]  # "YYYY-MM-DDTHH", already local (timezone=auto)
        out[key] = HourSample(
            hour_key=key,
            temp_c=_at(series["t"], i),
            wind_ms=_at(series["w"], i),
            gust_ms=_at(series["g"], i),
            cloud_pct=_at(series["c"], i),
            precip_mm=_at(series["p"], i),
        )
    return HourlyWeather(
        by_hour=out, utc_offset_s=int(data.get("utc_offset_seconds", 0))
    )


def _at(column: list, i: int) -> float | None:
    value = column[i] if i < len(column) else None
    return float(value) if value is not None else None


# ---------------------------------------------------------------------------
# Climatological normals
# ---------------------------------------------------------------------------


def fetch_normals(
    lat: float,
    lon: float,
    cfg: SeasonConfig,
    cache_dir: str | Path,
    *,
    today: _dt.date | None = None,
    session: requests.Session | None = None,
) -> tuple[dict[str, float], bool]:
    """Per-day-of-year mean temperature over ``projection_normal_years`` years.

    Keyed ``"MM-DD"``.  Built from the same archive endpoint so there is one
    provider to reason about; the split into its own function is the seam FMI
    would slot into later (spec §14.5) without touching callers.
    """
    today = today or _dt.date.today()
    years = cfg.projection_normal_years
    path = _cache_path(cache_dir, "normals", f"{lat}_{lon}_{years}y")
    cached = _read_cache(path, float(cfg.normals_ttl_days))
    if cached is not None and not cached.get("_stale"):
        return (cached.get("doy", {}), False)

    if cfg.offline:
        if cached is not None:
            return (cached.get("doy", {}), True)
        return ({}, True)

    end = _dt.date(today.year - 1, 12, 31)
    start = _dt.date(today.year - years, 1, 1)
    raw = _fetch_archive_year(lat, lon, start, end, cfg, session)
    if not raw:
        if cached is not None:
            return (cached.get("doy", {}), True)
        return ({}, True)

    doy = _average_by_day_of_year(raw)
    _write_cache(path, {"doy": doy, "lat": lat, "lon": lon, "years": years})
    return (doy, False)


def _average_by_day_of_year(pairs: dict[str, float]) -> dict[str, float]:
    """Average daily means into a ``"MM-DD" → °C`` normal."""
    buckets: dict[str, list[float]] = {}
    for day, value in pairs.items():
        buckets.setdefault(day[5:], []).append(value)
    normals = {k: sum(v) / len(v) for k, v in buckets.items() if v}
    # 29 February has ~1/4 the samples of its neighbours; borrow 28 February
    # rather than let a leap year get a noisier value than the rest.
    if "02-28" in normals and len(buckets.get("02-29", [])) < 3:
        normals["02-29"] = normals["02-28"]
    return normals


# ---------------------------------------------------------------------------
# The composed series
# ---------------------------------------------------------------------------


def build_series(
    lat: float,
    lon: float,
    start: _dt.date,
    end: _dt.date,
    cfg: SeasonConfig,
    cache_dir: str | Path,
    *,
    today: _dt.date | None = None,
    forecast_url: str = "https://api.open-meteo.com/v1/forecast",
    forecast_ttl_hours: float = 3.0,
    session: requests.Session | None = None,
) -> TemperatureSeries:
    """Stitch archive, forecast and normals into one continuous daily series.

    Precedence per day is archive > forecast > normal, so a day that the
    archive has already published is never downgraded to a forecast value, and
    the series' ``horizon`` is exactly where real information stops.
    """
    today = today or _dt.date.today()
    lat, lon = round(lat, 2), round(lon, 2)
    notes: list[str] = []
    stale = False

    archive_end = min(end, today - _dt.timedelta(days=cfg.archive_lag_days))
    archive: dict[str, float] = {}
    if archive_end >= start:
        try:
            archive, arch_stale = fetch_archive(
                lat, lon, start, archive_end, cfg, cache_dir, session
            )
            stale = stale or arch_stale
        except WeatherHistoryError as exc:
            notes.append(f"archive unavailable: {exc}")
            stale = True

    forecast, fc_stale = fetch_forecast_means(
        lat,
        lon,
        cfg,
        cache_dir,
        forecast_url=forecast_url,
        ttl_hours=forecast_ttl_hours,
        session=session,
    )
    stale = stale or fc_stale

    normals: dict[str, float] = {}
    horizon_guess = max([_dt.date.fromisoformat(d) for d in forecast] or [today])
    if end > horizon_guess:
        normals, norm_stale = fetch_normals(
            lat, lon, cfg, cache_dir, today=today, session=session
        )
        stale = stale or norm_stale
        if not normals:
            notes.append(
                "no climatological normal available — dates beyond the forecast "
                "horizon are missing rather than estimated"
            )

    days, missing = _assemble_days(start, end, archive, forecast, normals)

    if missing:
        notes.append(f"{missing} day(s) had no temperature from any source")
    if stale:
        notes.append(
            "some temperatures were served from a stale cache — window dates "
            "may lag the real season"
        )

    return TemperatureSeries(days=days, lat=lat, lon=lon, stale=stale, notes=notes)


def _assemble_days(
    start: _dt.date,
    end: _dt.date,
    archive: dict[str, float],
    forecast: dict[str, float],
    normals: dict[str, float],
) -> tuple[list[DailyTemp], int]:
    """Pick each day's temperature by source precedence: archive > forecast > normal.

    Returns the series and a count of days no source could fill — reported by
    the caller rather than quietly interpolated.
    """
    days: list[DailyTemp] = []
    missing = 0
    day = start
    while day <= end:
        key = day.isoformat()
        if key in archive:
            days.append(DailyTemp(day, archive[key], "archive"))
        elif key in forecast:
            days.append(DailyTemp(day, forecast[key], "forecast"))
        elif key[5:] in normals:
            days.append(DailyTemp(day, normals[key[5:]], "normal"))
        else:
            missing += 1
        day += _dt.timedelta(days=1)
    return days, missing
