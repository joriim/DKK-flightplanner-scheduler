"""Solar elevation and the hours a flight can actually use (spec §7.2).

Pure ephemeris — no network, no dependency.  `pvlib` is heavy and `astral` is
another dependency for something that is fifty lines of Meeus; this is the NOAA
low-precision solar position algorithm, good to about 0.01°, which is far
tighter than any threshold in this module.

**Why this matters at 62.8 °N.**  Solar elevation is not a detail here, it is
the binding constraint on half the season.  Maximum elevation at solar noon is
``90 − latitude + declination``, so at Seinäjoki:

===================  ==================  ================================
Date                 Max elevation       Consequence
===================  ==================  ================================
Summer solstice      50.6°               everything is possible
Equinox              27.2°               **already below the 30° floor
                                         for multispectral work**
Winter solstice      3.8°                nothing radiometric is possible
===================  ==================  ================================

So a multispectral campaign with ``min_solar_elevation_deg = 30`` has no
qualifying hours at all outside roughly late March → mid September, whatever
the weather does.  The spec anticipates this for late-October catch-crop
flights; in fact the wall arrives at the equinox.  :func:`season_window` is
what lets a surface say so explicitly, rather than returning a low score and
leaving the operator to wonder why every autumn day scores badly.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

#: Days from the J2000.0 epoch to the Unix epoch, for the day-number term.
_J2000 = _dt.datetime(2000, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _days_since_j2000(when: _dt.datetime) -> float:
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return (when - _J2000).total_seconds() / 86400.0


def solar_position(lat: float, lon: float, when: _dt.datetime) -> tuple[float, float]:
    """``(elevation_deg, azimuth_deg)`` of the sun, seen from (lat, lon) at *when*.

    *when* is interpreted as UTC when naive.  Elevation is geometric — no
    refraction correction, which at the elevations that matter here (≥ 20°)
    is under 0.05° and far below the precision of any threshold it feeds.
    """
    n = _days_since_j2000(when)

    mean_longitude = math.radians((280.460 + 0.9856474 * n) % 360)
    mean_anomaly = math.radians((357.528 + 0.9856003 * n) % 360)

    # Ecliptic longitude: mean longitude plus the equation of centre.
    ecliptic = mean_longitude + math.radians(
        1.915 * math.sin(mean_anomaly) + 0.020 * math.sin(2 * mean_anomaly)
    )
    obliquity = math.radians(23.439 - 0.0000004 * n)

    right_ascension = math.atan2(
        math.cos(obliquity) * math.sin(ecliptic), math.cos(ecliptic)
    )
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic))

    # Greenwich mean sidereal time, in hours, then the local hour angle.
    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    local_sidereal_deg = (gmst * 15 + lon) % 360
    hour_angle = math.radians(
        (local_sidereal_deg - math.degrees(right_ascension) + 180) % 360 - 180
    )

    phi = math.radians(lat)
    elevation = math.asin(
        math.sin(phi) * math.sin(declination)
        + math.cos(phi) * math.cos(declination) * math.cos(hour_angle)
    )
    azimuth = math.atan2(
        -math.sin(hour_angle),
        math.tan(declination) * math.cos(phi) - math.sin(phi) * math.cos(hour_angle),
    )
    return (math.degrees(elevation), math.degrees(azimuth) % 360)


def elevation_deg(lat: float, lon: float, when: _dt.datetime) -> float:
    """Solar elevation in degrees; negative when the sun is below the horizon."""
    return solar_position(lat, lon, when)[0]


def max_elevation_on(lat: float, lon: float, day: _dt.date) -> float:
    """Highest elevation the sun reaches on *day* — its value at solar noon.

    Sampled every ten minutes rather than solved analytically: the equation of
    time makes the closed form fiddly, and 144 cheap evaluations are exact
    enough for a threshold comparison.
    """
    best = -90.0
    start = _dt.datetime.combine(day, _dt.time(0), tzinfo=_dt.timezone.utc)
    for step in range(0, 24 * 6):
        best = max(
            best, elevation_deg(lat, lon, start + _dt.timedelta(minutes=10 * step))
        )
    return best


# ---------------------------------------------------------------------------
# Usable hours
# ---------------------------------------------------------------------------


@dataclass
class SolarDay:
    """Which local hours of one day clear the elevation floor."""

    date: _dt.date
    threshold_deg: float
    #: Local hour → solar elevation at the middle of that hour.
    elevation_by_hour: dict[int, float] = field(default_factory=dict)
    #: Local hours meeting the floor, ascending.
    qualifying_hours: list[int] = field(default_factory=list)
    max_elevation_deg: float = -90.0

    @property
    def has_qualifying_hours(self) -> bool:
        return bool(self.qualifying_hours)

    def spans(self) -> list[tuple[int, int]]:
        """Qualifying hours collapsed into contiguous ``(start, end)`` runs.

        ``end`` is exclusive, so ``(10, 13)`` means 10:00–13:00 local.
        """
        if not self.qualifying_hours:
            return []
        runs: list[tuple[int, int]] = []
        start = previous = self.qualifying_hours[0]
        for hour in self.qualifying_hours[1:]:
            if hour == previous + 1:
                previous = hour
                continue
            runs.append((start, previous + 1))
            start = previous = hour
        runs.append((start, previous + 1))
        return runs

    def labels(self) -> list[str]:
        """Human spans, e.g. ``["10:00-13:00"]`` — the spec's ``best_hours``."""
        return [f"{a:02d}:00-{b:02d}:00" for a, b in self.spans()]

    def midpoint(self) -> _dt.time | None:
        """Middle of the longest qualifying run, for satellite Δt scoring."""
        runs = self.spans()
        if not runs:
            return None
        start, end = max(runs, key=lambda r: r[1] - r[0])
        minutes = int((start + end) / 2 * 60)
        return _dt.time(hour=min(23, minutes // 60), minute=minutes % 60)

    def shortfall_deg(self) -> float:
        """How far below the floor the best moment of the day falls.

        Zero when the day qualifies.  This is what turns "scores badly" into
        "the sun never gets above 27°, so this cannot be flown for
        multispectral work at all".
        """
        return max(0.0, self.threshold_deg - self.max_elevation_deg)


def solar_day(
    lat: float,
    lon: float,
    day: _dt.date,
    threshold_deg: float,
    *,
    utc_offset_s: int = 0,
    daytime_start_h: int = 6,
    daytime_end_h: int = 18,
) -> SolarDay:
    """Elevation per local hour, and which hours clear *threshold_deg*.

    Hours are **local** (via *utc_offset_s*, which the host's forecast already
    reports) and restricted to the configured daytime window, so the result
    lines up with the rest of the forecast plumbing instead of introducing a
    second notion of "day".
    """
    tz = _dt.timezone(_dt.timedelta(seconds=utc_offset_s))
    result = SolarDay(date=day, threshold_deg=threshold_deg)

    for hour in range(daytime_start_h, daytime_end_h):
        # Sample the middle of the hour: the elevation at 10:00 sharp
        # under-represents an hour that is usable for most of its length.
        local = _dt.datetime.combine(day, _dt.time(hour, 30), tzinfo=tz)
        elev = elevation_deg(lat, lon, local.astimezone(_dt.timezone.utc))
        result.elevation_by_hour[hour] = round(elev, 2)
        result.max_elevation_deg = max(result.max_elevation_deg, elev)
        if elev >= threshold_deg:
            result.qualifying_hours.append(hour)

    result.max_elevation_deg = round(result.max_elevation_deg, 2)
    return result


def threshold_for(sensor: str, cfg) -> float:
    """Elevation floor for a campaign's sensor.

    Multispectral and thermal work is radiometric and needs the sun high;
    RGB-only structural work (stand counts, lodging extent, canopy height)
    tolerates a lower sun, so it gets the looser floor.
    """
    if sensor in ("rgb",):
        return cfg.min_solar_elevation_rgb_deg
    return cfg.min_solar_elevation_deg


def season_window(
    lat: float, lon: float, year: int, threshold_deg: float
) -> tuple[_dt.date | None, _dt.date | None]:
    """First and last date of *year* on which the sun clears *threshold_deg*.

    At Finnish latitudes this is the honest answer to "why does every autumn
    day score zero?" — outside this range, no weather makes the flight
    possible. Returns ``(None, None)`` when the threshold is never met, which
    is the real answer for a 30° floor north of roughly 66 °N in any month.
    """
    days = [
        _dt.date(year, 1, 1) + _dt.timedelta(days=i)
        for i in range(366 if _is_leap(year) else 365)
    ]
    qualifying = [d for d in days if max_elevation_on(lat, lon, d) >= threshold_deg]
    if not qualifying:
        return (None, None)
    return (qualifying[0], qualifying[-1])


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
