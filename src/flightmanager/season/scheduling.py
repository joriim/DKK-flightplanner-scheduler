"""Opportunity scoring and cross-parcel field days (spec §7).

Phase 1 answered "which week". This answers "which day, and can I fit them all
into one field day".

Two rules shape the whole module:

**Hard gates zero the score outright.** Outside the window, wind above the
drone's limit, precipitation above threshold, no hour meeting the solar
elevation floor, or ``flight_ready: false`` on the underlying job — any one of
these and the day is not a candidate, no matter how good everything else looks.
A weighted sum alone would let a strong cloud score paper over unflyable wind.

**Components are always reported, never just the total.** Operators need to see
*why* Thursday beat Tuesday (§7.4). A bare 0.82 invites either blind trust or
blind dismissal, and both are wrong — particularly while the windows those
scores sit inside rest on uncalibrated stage thresholds.

What is reused rather than rebuilt (§7.1): satellite overpasses, their
clear-sky qualification and the "golden day" concept all come from the host's
``build_forecast``; launch-site clustering comes from the host's
``cluster_jobs``; the greedy route order is the same algorithm the browser UI
uses, ported so the two agree.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from flightmanager.season import solar
from flightmanager.season.config import SeasonConfig, SeasonTables
from flightmanager.season.models import (
    Campaign,
    CampaignScore,
    CampaignType,
    Opportunity,
    SeasonPlan,
    Window,
)
from flightmanager.season.weather_history import HourlyWeather, HourSample

log = logging.getLogger(__name__)

#: Campaign states that can still be flown. ``flown``/``skipped``/``cancelled``
#: are decisions already taken; ``planned`` has no window to score against.
SCHEDULABLE_STATES = ("open", "scheduled")

#: Gate names, so callers can test for one without matching on prose.
GATE_OUTSIDE_WINDOW = "outside_window"
GATE_WIND = "wind_above_limit"
GATE_PRECIP = "precip_above_threshold"
GATE_SOLAR = "no_qualifying_solar_hours"
GATE_NOT_FLIGHT_READY = "job_not_flight_ready"
GATE_NO_COINCIDENT_PASS = "no_coincident_pass"

ALL_GATES = (
    GATE_OUTSIDE_WINDOW,
    GATE_WIND,
    GATE_PRECIP,
    GATE_SOLAR,
    GATE_NOT_FLIGHT_READY,
    GATE_NO_COINCIDENT_PASS,
)

#: Fallback wind ceiling when the host has ``drone_wind_limit_ms`` unset. The
#: host treats ``None`` as "no golden-day highlighting"; for a hard flight gate
#: that is not a safe reading, so a conservative default stands in and the
#: opportunity is flagged so nobody mistakes it for a configured limit.
DEFAULT_WIND_LIMIT_MS = 10.0

#: A gust this much above the mean is worth flagging even when the mean passes.
_GUST_FLAG_RATIO = 1.4


# ---------------------------------------------------------------------------
# Day conditions
# ---------------------------------------------------------------------------


@dataclass
class DayConditions:
    """Everything known about one candidate day at one folder's grid cell."""

    date: _dt.date
    hours: dict[int, HourSample] = field(default_factory=dict)
    #: Satellite passes on this day, from the host's forecast day slots.
    passes: list[dict[str, Any]] = field(default_factory=list)
    golden: bool = False
    stale: bool = False
    #: Daily aggregates from the host's forecast, used when hourly is missing.
    daily_wind_ms: float | None = None
    daily_cloud_pct: float | None = None
    daily_precip_mm: float | None = None

    def window_hours(self, start_h: int, end_h: int) -> dict[int, HourSample]:
        return {h: s for h, s in self.hours.items() if start_h <= h < end_h}

    def mean_wind(self, start_h: int, end_h: int) -> float | None:
        # Explicit None check, not `or`: a dead-calm 0.0 m/s is a real and very
        # good value, and `0.0 or fallback` would throw it away and score the
        # best possible flying conditions as "unknown".
        value = _mean(s.wind_ms for s in self.window_hours(start_h, end_h).values())
        return value if value is not None else self.daily_wind_ms

    def max_gust(self, start_h: int, end_h: int) -> float | None:
        gusts = [
            s.gust_ms
            for s in self.window_hours(start_h, end_h).values()
            if s.gust_ms is not None
        ]
        return max(gusts) if gusts else None

    def mean_cloud(self, start_h: int, end_h: int) -> float | None:
        value = _mean(s.cloud_pct for s in self.window_hours(start_h, end_h).values())
        return value if value is not None else self.daily_cloud_pct

    def total_precip(self, start_h: int, end_h: int) -> float | None:
        values = [
            s.precip_mm
            for s in self.window_hours(start_h, end_h).values()
            if s.precip_mm is not None
        ]
        if values:
            return round(sum(values), 2)
        return self.daily_precip_mm

    def clear_sky_passes(self) -> list[dict[str, Any]]:
        return [p for p in self.passes if p.get("clear_window")]

    @property
    def has_hourly(self) -> bool:
        return bool(self.hours)


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 2) if present else None


def build_day_conditions(
    dates: list[_dt.date],
    hourly: HourlyWeather,
    day_slots: list[dict[str, Any]],
) -> dict[_dt.date, DayConditions]:
    """Merge the season module's hourly fetch with the host's day slots.

    The host owns satellite passes and daily weather; this module owns the
    per-hour detail the host does not cache. Neither is recomputed from the
    other.
    """
    slots_by_date = {s.get("date"): s for s in day_slots}
    out: dict[_dt.date, DayConditions] = {}

    for day in dates:
        slot = slots_by_date.get(day.isoformat()) or {}
        weather = slot.get("weather") or {}
        out[day] = DayConditions(
            date=day,
            hours=hourly.day_hours(day),
            passes=list(slot.get("satellites") or []),
            golden=bool(slot.get("golden")),
            stale=hourly.stale,
            daily_wind_ms=weather.get("wind_avg_ms"),
            daily_cloud_pct=weather.get("cloud_pct"),
            daily_precip_mm=weather.get("precip_mm"),
        )
    return out


# ---------------------------------------------------------------------------
# Best hours (spec §7.2)
# ---------------------------------------------------------------------------


@dataclass
class UsableHours:
    """Hours that clear elevation *and* weather, plus why the rest did not."""

    hours: list[int] = field(default_factory=list)
    solar_day: solar.SolarDay | None = None
    blocked_by_wind: list[int] = field(default_factory=list)
    blocked_by_precip: list[int] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.hours)

    def spans(self) -> list[tuple[int, int]]:
        if not self.hours:
            return []
        runs: list[tuple[int, int]] = []
        start = previous = self.hours[0]
        for hour in self.hours[1:]:
            if hour == previous + 1:
                previous = hour
                continue
            runs.append((start, previous + 1))
            start = previous = hour
        runs.append((start, previous + 1))
        return runs

    def labels(self) -> list[str]:
        return [f"{a:02d}:00-{b:02d}:00" for a, b in self.spans()]

    def midpoint_hour(self) -> float | None:
        runs = self.spans()
        if not runs:
            return None
        start, end = max(runs, key=lambda r: r[1] - r[0])
        return (start + end) / 2


def usable_hours(
    conditions: DayConditions,
    solar_day: solar.SolarDay,
    cfg: SeasonConfig,
    wind_limit_ms: float,
) -> UsableHours:
    """Contiguous local hours meeting elevation, wind and precipitation limits.

    Cloud is deliberately *not* a gate here. Overcast is a poor day for
    radiometric work and a perfectly good one for structural RGB (diffuse light
    removes shadows), so it belongs in the weighted score, not in the list of
    hours the drone can physically fly.
    """
    result = UsableHours(solar_day=solar_day)
    for hour in solar_day.qualifying_hours:
        sample = conditions.hours.get(hour)
        if sample is None:
            # No hourly data (beyond the forecast horizon): the solar floor is
            # the only thing that can be checked, so trust it and let the
            # day-level components carry the weather.
            result.hours.append(hour)
            continue
        if sample.wind_ms is not None and sample.wind_ms > wind_limit_ms:
            result.blocked_by_wind.append(hour)
            continue
        if sample.precip_mm is not None and sample.precip_mm > cfg.precip_threshold_mm:
            result.blocked_by_precip.append(hour)
            continue
        result.hours.append(hour)
    return result


# ---------------------------------------------------------------------------
# Scoring (spec §7.4)
# ---------------------------------------------------------------------------


def _linear_falloff(value: float | None, limit: float) -> float:
    """1.0 at zero, 0.0 at *limit*, clamped. ``None`` is treated as neutral."""
    if value is None:
        return 0.5
    if limit <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - value / limit))


def score_days_from_target(window: Window, day: _dt.date) -> float:
    """1.0 on the target day, falling to 0.0 at whichever window edge is farther."""
    span = max(
        (window.target - window.earliest).days,
        (window.latest - window.target).days,
        1,
    )
    return max(0.0, min(1.0, 1.0 - abs((day - window.target).days) / span))


def score_sun(usable: UsableHours, daytime_span_h: int) -> float:
    """Fraction of the daytime window that is actually usable."""
    return max(0.0, min(1.0, len(usable.hours) / max(1, daytime_span_h)))


def score_coincidence(
    ct: CampaignType, conditions: DayConditions, usable: UsableHours
) -> tuple[float, dict[str, Any] | None]:
    """Bonus scaling with |Δt| between the flight's best hours and an overpass.

    A UAV flight within a day of a clear-sky Sentinel-2 pass is worth far more
    than an arbitrary flight: it lets UAV-resolution truth be aggregated to the
    10/20 m pixel and used to calibrate the satellite-scale model. A pass that
    is clouded over is worth nothing, so only ``clear_window`` passes count.
    """
    if ct.satellite_coincidence == "ignored":
        return (0.0, None)
    passes = conditions.clear_sky_passes()
    if not passes:
        return (0.0, None)

    midpoint = usable.midpoint_hour()
    if midpoint is None:
        midpoint = 12.0

    best_score, best_pass = 0.0, None
    for overpass in passes:
        pass_hour = _local_hour_of(overpass)
        if pass_hour is None:
            continue
        delta_h = abs(pass_hour - midpoint)
        # Same-day coincidence is what matters; a 12 h separation still shares
        # the day's illumination and canopy state, so the falloff is gentle.
        value = max(0.0, 1.0 - delta_h / 12.0)
        if value > best_score:
            best_score, best_pass = value, overpass
    return (round(best_score, 3), best_pass)


def _local_hour_of(overpass: dict[str, Any]) -> float | None:
    stamp = overpass.get("peak_local")
    if not isinstance(stamp, str) or len(stamp) < 16:
        return None
    try:
        return int(stamp[11:13]) + int(stamp[14:16]) / 60.0
    except ValueError:
        return None


@dataclass
class ScoringContext:
    """Everything scoring needs that does not vary between campaigns."""

    cfg: SeasonConfig
    lat: float
    lon: float
    utc_offset_s: int = 0
    #: The daytime window belongs to the host's ``[weather]`` config, not to
    #: ``[season]`` — one notion of "day" across the whole application, so a
    #: season score and the forecast bar never disagree about which hours count.
    daytime_start_h: int = 6
    daytime_end_h: int = 18
    wind_limit_ms: float = DEFAULT_WIND_LIMIT_MS
    wind_limit_is_default: bool = False
    #: Job path → ``flight_ready``. Missing means unknown, which does not gate.
    flight_ready: dict[str, bool] = field(default_factory=dict)
    #: Cached per (date, threshold) so a folder's campaigns share the ephemeris.
    _solar_cache: dict[tuple[_dt.date, float], solar.SolarDay] = field(
        default_factory=dict, repr=False
    )

    def solar_day(self, day: _dt.date, threshold_deg: float) -> solar.SolarDay:
        key = (day, threshold_deg)
        if key not in self._solar_cache:
            self._solar_cache[key] = solar.solar_day(
                self.lat,
                self.lon,
                day,
                threshold_deg,
                utc_offset_s=self.utc_offset_s,
                daytime_start_h=self.daytime_start_h,
                daytime_end_h=self.daytime_end_h,
            )
        return self._solar_cache[key]

    @property
    def daytime_span_h(self) -> int:
        return max(1, self.daytime_end_h - self.daytime_start_h)


def score_campaign_on(
    campaign: Campaign,
    ct: CampaignType,
    conditions: DayConditions,
    ctx: ScoringContext,
) -> tuple[CampaignScore, UsableHours]:
    """Score one campaign on one day, with hard gates applied first."""
    cfg = ctx.cfg
    day = conditions.date
    window = campaign.effective_window()

    threshold = solar.threshold_for(ct.sensor, cfg)
    solar_day = ctx.solar_day(day, threshold)
    usable = usable_hours(conditions, solar_day, cfg, ctx.wind_limit_ms)

    start_h, end_h = ctx.daytime_start_h, ctx.daytime_end_h
    wind = conditions.mean_wind(start_h, end_h)
    cloud = conditions.mean_cloud(start_h, end_h)
    precip = conditions.total_precip(start_h, end_h)
    gust = conditions.max_gust(start_h, end_h)

    coincidence, matched_pass = score_coincidence(ct, conditions, usable)
    gates = _failed_gates(
        campaign, ct, window, day, wind, precip, usable, ctx, coincidence
    )

    components = {
        "in_window": 1.0 if window and window.contains(day) else 0.0,
        "days_from_target": round(score_days_from_target(window, day), 3)
        if window
        else 0.0,
        "wind": round(_linear_falloff(wind, ctx.wind_limit_ms), 3),
        "cloud": round(_linear_falloff(cloud, 100.0), 3),
        "precip": round(_linear_falloff(precip, max(cfg.precip_threshold_mm, 0.1)), 3),
        "sun_elevation": round(score_sun(usable, ctx.daytime_span_h), 3),
        "satellite_coincidence": coincidence,
    }
    total = 0.0 if gates else _weighted_total(components, ct, cfg)

    return (
        CampaignScore(
            campaign_id=campaign.campaign_id,
            type_id=campaign.type_id,
            label_fi=ct.label_fi,
            label_en=ct.label_en,
            job_path=campaign.job_paths[0] if campaign.job_paths else "",
            priority=ct.priority,
            score=round(total, 3),
            components=components,
            gates_failed=gates,
            flags=_campaign_flags(ct, usable, solar_day, gust, wind, matched_pass, ctx),
            days_from_target=(day - window.target).days if window else None,
            days_to_close=(window.latest - day).days if window else None,
        ),
        usable,
    )


def _failed_gates(
    campaign: Campaign,
    ct: CampaignType,
    window: Window | None,
    day: _dt.date,
    wind: float | None,
    precip: float | None,
    usable: UsableHours,
    ctx: ScoringContext,
    coincidence: float,
) -> list[str]:
    """Hard gates — any one of these makes the day a non-candidate (§7.4)."""
    gates: list[str] = []
    if window is None or not window.contains(day):
        gates.append(GATE_OUTSIDE_WINDOW)
    if wind is not None and wind > ctx.wind_limit_ms:
        gates.append(GATE_WIND)
    if precip is not None and precip > ctx.cfg.precip_threshold_mm:
        gates.append(GATE_PRECIP)
    if not usable.any:
        gates.append(GATE_SOLAR)
    for path in campaign.job_paths:
        if ctx.flight_ready.get(path) is False:
            gates.append(GATE_NOT_FLIGHT_READY)
            break
    # A calibration flight whose whole point is the satellite pass is not worth
    # flying without one — but only `required` says that; `preferred` merely
    # scores lower.
    if ct.satellite_coincidence == "required" and coincidence <= 0.0:
        gates.append(GATE_NO_COINCIDENT_PASS)
    return gates


def _weighted_total(
    components: dict[str, float], ct: CampaignType, cfg: SeasonConfig
) -> float:
    """Weighted sum, normalised so the result is always in [0, 1].

    Satellite coincidence is dropped from *both* numerator and denominator when
    the campaign ignores it. Leaving a guaranteed-zero component in the
    denominator would cap an emergence count at 0.9 for failing to coincide
    with a satellite pass it never wanted.
    """
    weights = cfg.weights.model_dump()
    if ct.satellite_coincidence == "ignored":
        weights.pop("satellite_coincidence", None)

    total = sum(weights.values())
    if total <= 0:
        return 0.0
    score = sum(components.get(name, 0.0) * w for name, w in weights.items())
    return max(0.0, min(1.0, score / total))


def _campaign_flags(
    ct: CampaignType,
    usable: UsableHours,
    solar_day: solar.SolarDay,
    gust: float | None,
    wind: float | None,
    matched_pass: dict[str, Any] | None,
    ctx: ScoringContext,
) -> list[str]:
    """Human-readable notes: the "why" behind a score, in the spec's style."""
    flags: list[str] = []

    if not solar_day.has_qualifying_hours:
        # Say it outright rather than returning a low score (spec §7.2).
        flags.append(
            f"sun never clears {solar_day.threshold_deg:.0f}° "
            f"(peaks at {solar_day.max_elevation_deg:.0f}°) — this campaign's "
            f"{ct.sensor} work is not possible on this date at this latitude"
        )
    elif not usable.any:
        flags.append("every hour with enough sun is blocked by wind or rain")

    if gust is not None and wind is not None and gust > wind * _GUST_FLAG_RATIO:
        flags.append(f"wind gusts {gust:.0f} m/s against a {wind:.0f} m/s mean")
    if usable.blocked_by_wind:
        flags.append(
            f"{len(usable.blocked_by_wind)} sunlit hour(s) lost to wind above "
            f"{ctx.wind_limit_ms:.0f} m/s"
        )
    if usable.blocked_by_precip:
        flags.append(f"{len(usable.blocked_by_precip)} sunlit hour(s) lost to rain")
    if matched_pass is not None:
        flags.append(
            f"clear-sky {matched_pass.get('name', 'satellite')} pass at "
            f"{str(matched_pass.get('peak_local', ''))[11:16]}"
        )
    if ct.satellite_coincidence == "required" and matched_pass is None:
        flags.append(
            "no clear-sky satellite pass on this day — this campaign exists to "
            "coincide with one"
        )
    if ctx.wind_limit_is_default:
        flags.append(
            f"weather.drone_wind_limit_ms is unset; gating on a default "
            f"{ctx.wind_limit_ms:.0f} m/s"
        )
    return flags


# ---------------------------------------------------------------------------
# Opportunities over a folder
# ---------------------------------------------------------------------------


def schedulable_campaigns(
    plan: SeasonPlan, tables: SeasonTables, *, audience: str | None = None
) -> list[tuple[Campaign, CampaignType]]:
    """Campaigns that still have a window worth scoring."""
    out: list[tuple[Campaign, CampaignType]] = []
    for campaign in plan.campaigns:
        if campaign.state not in SCHEDULABLE_STATES:
            continue
        if campaign.effective_window() is None:
            continue
        ct = tables.campaign_type(campaign.type_id)
        if ct is None:
            continue
        if audience and audience != "both" and ct.audience not in (audience, "both"):
            continue
        out.append((campaign, ct))
    return out


def score_day(
    day: _dt.date,
    pairs: list[tuple[Campaign, CampaignType]],
    conditions: DayConditions,
    ctx: ScoringContext,
) -> Opportunity:
    """Score every campaign on one day and fold them into one Opportunity."""
    scores: list[CampaignScore] = []
    widest: UsableHours | None = None

    for campaign, ct in pairs:
        score, usable = score_campaign_on(campaign, ct, conditions, ctx)
        scores.append(score)
        # The reported best_hours are the most permissive across the campaigns
        # scored — an RGB campaign can fly hours a multispectral one cannot, and
        # the day's summary should not hide that.
        if widest is None or len(usable.hours) > len(widest.hours):
            widest = usable

    servable = [s for s in scores if not s.blocked]
    start_h, end_h = ctx.daytime_start_h, ctx.daytime_end_h

    opportunity = Opportunity(
        date=day,
        campaign_ids=[s.campaign_id for s in servable],
        score=round(max((s.score for s in servable), default=0.0), 3),
        mean_score=round(
            sum(s.score for s in servable) / len(servable) if servable else 0.0, 3
        ),
        per_campaign=scores,
        best_hours=widest.labels() if widest else [],
        wind_ms=conditions.mean_wind(start_h, end_h),
        gust_ms=conditions.max_gust(start_h, end_h),
        cloud_pct=conditions.mean_cloud(start_h, end_h),
        precip_mm=conditions.total_precip(start_h, end_h),
        max_solar_elevation_deg=widest.solar_day.max_elevation_deg
        if widest and widest.solar_day
        else None,
        satellite_passes=conditions.passes,
        weather_stale=conditions.stale,
    )
    opportunity.components = _day_components(scores)
    opportunity.flags = _day_flags(conditions, scores, servable)
    return opportunity


def _day_components(scores: list[CampaignScore]) -> dict[str, float]:
    """The full component breakdown behind the day's headline score.

    Taken from the campaign that *set* the headline — the strongest reason to
    fly that day — rather than averaged. Wind, cloud and precipitation are
    shared by every campaign anyway; ``sun_elevation``, ``days_from_target`` and
    ``satellite_coincidence`` are not, and averaging them would produce a
    breakdown that explains no campaign in particular and does not add up to the
    number printed beside it.
    """
    servable = [s for s in scores if not s.blocked]
    if servable:
        return dict(max(servable, key=lambda s: s.score).components)
    return dict(scores[0].components) if scores else {}


def _day_flags(
    conditions: DayConditions,
    scores: list[CampaignScore],
    servable: list[CampaignScore],
) -> list[str]:
    flags: list[str] = []
    if conditions.stale:
        flags.append("forecast served from a stale cache")
    if conditions.golden:
        flags.append("golden day: flyable weather and a clear-sky satellite pass")
    if not conditions.has_hourly:
        flags.append("beyond the hourly forecast horizon — scored on daily averages")
    if scores and not servable:
        reasons = sorted({g for s in scores for g in s.gates_failed})
        flags.append("no campaign can be flown: " + ", ".join(reasons))
    # Surface each distinct campaign flag once, so a folder of 12 parcels does
    # not print the same wind note twelve times.
    for score in scores:
        for flag in score.flags:
            if flag not in flags:
                flags.append(flag)
    return flags


def rank_opportunities(opportunities: list[Opportunity]) -> list[Opportunity]:
    """Best day first.

    Ranked on the strongest single reason to fly, then on how much else the day
    would carry, then earliest. A day serving one campaign perfectly beats a day
    serving four mediocrely — but between two equally good days, take the one
    that clears more of the backlog.
    """
    return sorted(
        opportunities,
        key=lambda o: (-o.score, -len(o.campaign_ids), o.date),
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres — the browser UI's own formula."""
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def greedy_route(points: list[tuple[str, float, float]]) -> list[str]:
    """Greedy nearest-neighbour visit order over ``(key, lat, lon)`` points.

    A port of ``greedyTSP`` from the browser UI's ``list-math.js``, kept
    identical — same northernmost-then-westernmost start, same nearest-next
    step — so a field day planned here and a route dragged out in the UI agree
    instead of quietly disagreeing.

    The spec calls this "the existing greedy nearest-neighbour route ordering";
    it exists, but only in JavaScript, so this is the port rather than a new
    algorithm.
    """
    if len(points) <= 1:
        return [p[0] for p in points]

    remaining = sorted(points, key=lambda p: (-p[1], p[2]))
    route = [remaining.pop(0)]
    while remaining:
        _, last_lat, last_lon = route[-1]
        best_index = min(
            range(len(remaining)),
            key=lambda i: haversine_m(
                last_lat, last_lon, remaining[i][1], remaining[i][2]
            ),
        )
        route.append(remaining.pop(best_index))
    return [p[0] for p in route]
