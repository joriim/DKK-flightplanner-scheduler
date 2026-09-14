"""Thermal time → stage timeline, with an honest uncertainty band.

Pure computation: everything here takes a temperature series and returns dates.
Fetching that series (archive, forecast, climatological normal) is
:mod:`flightmanager.season.weather_history`'s job, which keeps this module
network-free and golden-file testable.

The convention is the Finnish *tehoisa lämpösumma*: daily mean temperature,
base +5 °C, optionally capped at an upper cutoff, accumulated from an arbitrary
``accumulation_start`` — sowing for spring crops, establishment for catch crops
and autumn-sown cereals (spec §6.1).

Three things drive the uncertainty band, and they are deliberately additive so
each stays visible:

1. the stage thresholds are uncalibrated placeholders,
2. beyond the forecast horizon the temperatures are a 30-year normal, not a
   forecast, and the band widens with projection distance,
3. once a stage has actually been crossed on archive data, it is *known* and
   the band collapses to zero.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

from flightmanager.season.config import SeasonConfig
from flightmanager.season.models import CropProfile

#: Where one day's temperature came from.  ``archive`` is measured history and
#: is the only source that makes a crossing date certain.
TempSource = Literal["archive", "forecast", "normal"]

#: Basis of a derived date, worst-source-wins.
Basis = Literal["observed", "forecast", "normal"]

_BASIS_FOR_SOURCE: dict[str, Basis] = {
    "archive": "observed",
    "forecast": "forecast",
    "normal": "normal",
}


@dataclass(frozen=True)
class DailyTemp:
    """One day's mean temperature and where it came from."""

    date: _dt.date
    mean_c: float
    source: TempSource = "normal"


@dataclass
class TemperatureSeries:
    """A contiguous daily mean-temperature series for one grid cell."""

    days: list[DailyTemp] = field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    #: True when some part of the series was served from a stale cache because
    #: the network was unavailable — surfaces must report this, not hide it.
    stale: bool = False
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.days.sort(key=lambda d: d.date)

    @property
    def start(self) -> _dt.date | None:
        return self.days[0].date if self.days else None

    @property
    def end(self) -> _dt.date | None:
        return self.days[-1].date if self.days else None

    @property
    def horizon(self) -> _dt.date | None:
        """Last day backed by real information (archive or forecast).

        Everything after this is climatological normal, and the uncertainty
        band starts widening from here.
        """
        real = [d.date for d in self.days if d.source != "normal"]
        return max(real) if real else None

    def source_on(self, day: _dt.date) -> TempSource | None:
        for d in self.days:
            if d.date == day:
                return d.source
        return None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.days:
            out[d.source] = out.get(d.source, 0) + 1
        return out


# ---------------------------------------------------------------------------
# Thermal time
# ---------------------------------------------------------------------------


def effective_gdd(
    mean_c: float, base_temp_c: float, cutoff_temp_c: float | None = None
) -> float:
    """One day's contribution to the effective temperature sum.

    The cutoff caps the *daily mean* before the base is subtracted, which is
    the Finnish convention — it is not the "horizontal cutoff" variant that
    caps the contribution after subtraction.  They give the same answer except
    on hot days, which is exactly where the difference matters.
    """
    capped = min(mean_c, cutoff_temp_c) if cutoff_temp_c is not None else mean_c
    return max(0.0, capped - base_temp_c)


@dataclass(frozen=True)
class CumPoint:
    """Accumulated °C·d at the end of ``date``."""

    date: _dt.date
    cum: float
    source: TempSource


def accumulate(
    series: TemperatureSeries | Iterable[DailyTemp],
    start: _dt.date,
    base_temp_c: float,
    cutoff_temp_c: float | None = None,
) -> list[CumPoint]:
    """Accumulate effective °C·d from *start* (inclusive) forward.

    Days before *start* are ignored rather than treated as zero, so a series
    that begins in January and a sowing date in May give the same answer as a
    series that begins at sowing.
    """
    days = series.days if isinstance(series, TemperatureSeries) else list(series)
    total = 0.0
    out: list[CumPoint] = []
    for d in sorted(days, key=lambda x: x.date):
        if d.date < start:
            continue
        total += effective_gdd(d.mean_c, base_temp_c, cutoff_temp_c)
        out.append(CumPoint(date=d.date, cum=total, source=d.source))
    return out


def date_for_gdd(
    cum: Sequence[CumPoint], target_gdd: float
) -> tuple[_dt.date, TempSource] | None:
    """First day whose accumulated sum reaches *target_gdd*.

    Daily resolution, no interpolation: the crossing is reported on the day the
    sum first meets or exceeds the threshold.  Sub-day precision would be false
    precision given thresholds that are themselves ±100 °C·d placeholders.

    Returns ``None`` when the series ends before the threshold is reached — a
    real outcome in a cold season, not an error (spec §13).
    """
    if target_gdd <= 0:
        return (cum[0].date, cum[0].source) if cum else None
    for point in cum:
        if point.cum >= target_gdd:
            return (point.date, point.source)
    return None


def gdd_on(cum: Sequence[CumPoint], day: _dt.date) -> float | None:
    """Accumulated sum at the end of *day*, or ``None`` if outside the series."""
    best: float | None = None
    for point in cum:
        if point.date <= day:
            best = point.cum
        else:
            break
    return best


# ---------------------------------------------------------------------------
# Stage timeline
# ---------------------------------------------------------------------------


@dataclass
class StageEstimate:
    """When one stage is (or was) reached, and how well that is known."""

    stage: str
    threshold: float
    date: _dt.date | None
    basis: Basis
    reached: bool
    uncertainty_days: float = 0.0
    gdd_at: float | None = None
    reason: str = ""

    @property
    def earliest(self) -> _dt.date | None:
        if self.date is None:
            return None
        return self.date - _dt.timedelta(days=round(self.uncertainty_days))

    @property
    def latest(self) -> _dt.date | None:
        if self.date is None:
            return None
        return self.date + _dt.timedelta(days=round(self.uncertainty_days))


@dataclass
class Timeline:
    """Every stage of one crop on one parcel, plus the series it rests on."""

    crop_id: str
    accumulation_start: _dt.date
    stages: dict[str, StageEstimate] = field(default_factory=dict)
    cum: list[CumPoint] = field(default_factory=list)
    horizon: _dt.date | None = None
    series_end: _dt.date | None = None
    stale: bool = False
    notes: list[str] = field(default_factory=list)

    def stage(self, name: str) -> StageEstimate | None:
        return self.stages.get(name)

    def gdd_on(self, day: _dt.date) -> float | None:
        return gdd_on(self.cum, day)

    @property
    def total_gdd(self) -> float:
        return self.cum[-1].cum if self.cum else 0.0

    def reached(self) -> list[str]:
        return [n for n, s in self.stages.items() if s.reached]


def uncertainty_for(
    day: _dt.date,
    *,
    source: TempSource,
    horizon: _dt.date | None,
    cfg: SeasonConfig,
    calibrated: bool,
) -> float:
    """Half-width of the band around a derived date, in days.

    Monotonically non-decreasing in projection distance, and exactly zero for a
    date already crossed on measured history — the two properties the test plan
    pins (spec §13).
    """
    if source == "archive":
        return 0.0
    band = cfg.base_uncertainty_days
    if not calibrated:
        band += cfg.uncalibrated_extra_days
    if horizon is not None and day > horizon:
        weeks = (day - horizon).days / 7.0
        band += weeks * cfg.window_uncertainty_days_per_week_projected
    return round(band, 1)


def build_timeline(
    series: TemperatureSeries,
    crop: CropProfile,
    accumulation_start: _dt.date,
    cfg: SeasonConfig,
    today: _dt.date | None = None,
) -> Timeline:
    """Turn a temperature series into dated stages for one crop.

    Handles all three phenology models:

    ``gdd``
        thresholds are accumulated effective °C·d,
    ``fixed_days``
        thresholds are plain day offsets from ``accumulation_start``, for crops
        with no usable GDD table,
    ``manual``
        no automatic dates at all — the operator sets every window by hand.
    """
    today = today or _dt.date.today()
    # Widening is measured from where real information stops. When the series
    # holds none at all — planning next season in the winter, where every day is
    # a climatological normal — that point is today, not "nowhere". Treating a
    # missing horizon as no-widening would render a date eight months out with
    # the same band as one two weeks out.
    reference = series.horizon or today
    timeline = Timeline(
        crop_id=crop.id,
        accumulation_start=accumulation_start,
        horizon=series.horizon,
        series_end=series.end,
        stale=series.stale,
        notes=list(series.notes),
    )

    if crop.model == "manual":
        timeline.notes.append(
            f"crop {crop.id!r} uses model='manual': windows must be set by hand"
        )
        return timeline

    if crop.model == "fixed_days":
        _fill_fixed_days(timeline, crop, accumulation_start, cfg, series, reference)
        return timeline

    timeline.cum = accumulate(
        series, accumulation_start, crop.base_temp_c, crop.cutoff_temp_c
    )
    if not timeline.cum:
        timeline.notes.append(
            "no temperature data at or after the accumulation start date"
        )
        return timeline

    calibrated = crop.is_calibrated
    for name, threshold in crop.ordered_stages():
        hit = date_for_gdd(timeline.cum, threshold)
        if hit is None:
            timeline.stages[name] = StageEstimate(
                stage=name,
                threshold=threshold,
                date=None,
                basis="normal",
                reached=False,
                reason=(
                    f"season does not reach {threshold:.0f} °C·d "
                    f"(projected total {timeline.total_gdd:.0f} °C·d by "
                    f"{timeline.series_end})"
                ),
            )
            continue
        day, source = hit
        timeline.stages[name] = StageEstimate(
            stage=name,
            threshold=threshold,
            date=day,
            basis=_BASIS_FOR_SOURCE[source],
            reached=source == "archive",
            uncertainty_days=uncertainty_for(
                day,
                source=source,
                horizon=reference,
                cfg=cfg,
                calibrated=calibrated,
            ),
            gdd_at=gdd_on(timeline.cum, day),
        )
    return timeline


def _fill_fixed_days(
    timeline: Timeline,
    crop: CropProfile,
    start: _dt.date,
    cfg: SeasonConfig,
    series: TemperatureSeries,
    reference: _dt.date,
) -> None:
    """Stage dates as plain day offsets — no thermal time involved."""
    for name, days in crop.ordered_stages():
        day = start + _dt.timedelta(days=int(days))
        source: TempSource = series.source_on(day) or (
            "archive" if series.horizon and day <= series.horizon else "normal"
        )
        timeline.stages[name] = StageEstimate(
            stage=name,
            threshold=days,
            date=day,
            basis=_BASIS_FOR_SOURCE[source],
            reached=source == "archive",
            uncertainty_days=uncertainty_for(
                day,
                source=source,
                horizon=reference,
                cfg=cfg,
                calibrated=crop.is_calibrated,
            ),
        )
    timeline.notes.append(
        f"crop {crop.id!r} uses model='fixed_days': dates are day offsets from "
        f"{start.isoformat()}, not thermal time"
    )


def thermal_repeat_dates(
    timeline: Timeline,
    anchor_gdd: float,
    every_gdd: float,
    max_count: int,
) -> list[tuple[_dt.date, TempSource, float]]:
    """Dates on which the crop has moved on by another *every_gdd*.

    This is the adaptive cadence of spec §14.4: more defensible than a fixed
    calendar interval because it tracks the crop rather than the calendar, and
    free here because the accumulated series already exists.

    Returns ``(date, source, gdd)`` triples, shortest-first, stopping at the
    first repeat the season does not reach.
    """
    out: list[tuple[_dt.date, TempSource, float]] = []
    for i in range(1, max_count):
        target = anchor_gdd + i * every_gdd
        hit = date_for_gdd(timeline.cum, target)
        if hit is None:
            break
        out.append((hit[0], hit[1], target))
    return out
