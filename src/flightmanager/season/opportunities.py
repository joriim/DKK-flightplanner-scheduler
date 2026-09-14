"""Orchestration for `season next` and `season day`.

The Phase 1 counterpart of this module is ``planner.py``, which owns windows.
This one owns days: it resolves the folder's grid cell, pulls the hourly
conditions and the host's satellite day slots, scores every schedulable
campaign across the horizon, and builds the field-day plan for a chosen date.

Like everything else in the season module it takes no pipeline lock, and it
caches nothing of its own — an opportunity is only as good as the forecast
behind it, and a stored score would be a stale score.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any

from flightmanager.season import fieldday, scheduling, store
from flightmanager.season import weather_history as wx
from flightmanager.season.config import SeasonConfig, SeasonTables
from flightmanager.season.integration import HostContext
from flightmanager.season.models import Opportunity, SeasonPlan
from flightmanager.season.planner import PlanError

log = logging.getLogger(__name__)

#: Default horizon for ``season next``. The forecast runs to 14-16 days; past
#: that every day would be scored on climatological normals and rank
#: identically, which is noise rather than information.
DEFAULT_HORIZON_DAYS = 10


@dataclass
class OpportunityReport:
    """Scored days for a folder, best first."""

    folder: str
    season: int
    today: _dt.date
    opportunities: list[Opportunity] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    campaigns_considered: int = 0
    weather_stale: bool = False

    def flyable(self) -> list[Opportunity]:
        return [o for o in self.opportunities if o.score > 0]

    def best(self) -> Opportunity | None:
        ranked = self.flyable()
        return ranked[0] if ranked else None

    def by_date(self, day: _dt.date) -> Opportunity | None:
        return next((o for o in self.opportunities if o.date == day), None)


def _load_plan(host: HostContext, folder: str, season: int) -> SeasonPlan:
    folder_dir = host.folder_dir(folder)
    plan = store.load_plan(folder_dir, season)
    if plan is None:
        raise PlanError(
            f"no season plan for {folder} / {season} — run "
            f"`flightmanager season plan --folder {folder}` first"
        )
    return plan


def _cell(plan: SeasonPlan, host: HostContext, folder: str) -> tuple[float, float]:
    """The folder's weather cell, pinned by ``season plan``."""
    if plan.centroid_lat is not None and plan.centroid_lon is not None:
        return (plan.centroid_lat, plan.centroid_lon)
    points = [(j.lat, j.lon) for j in host.jobs_in_folder(folder) if j.has_position]
    if not points:
        raise PlanError(
            f"no job in folder {folder!r} has geometry, so there is no point to "
            f"score the weather at"
        )
    return wx.cell_for(points)  # type: ignore[arg-type]


def _scoring_context(
    host: HostContext,
    cfg: SeasonConfig,
    lat: float,
    lon: float,
    hourly: wx.HourlyWeather,
    cards: list[dict[str, Any]],
) -> scheduling.ScoringContext:
    """Assemble the per-folder scoring context from host config and cards."""
    wind_limit = host.drone_wind_limit_ms
    return scheduling.ScoringContext(
        cfg=cfg,
        lat=lat,
        lon=lon,
        utc_offset_s=hourly.utc_offset_s,
        daytime_start_h=host.daytime_start_h,
        daytime_end_h=host.daytime_end_h,
        wind_limit_ms=(
            wind_limit if wind_limit is not None else scheduling.DEFAULT_WIND_LIMIT_MS
        ),
        wind_limit_is_default=wind_limit is None,
        flight_ready={
            c["path"]: c.get("flight_ready")
            for c in cards
            if c.get("path") and c.get("flight_ready") is not None
        },
    )


def find_opportunities(
    host: HostContext,
    tables: SeasonTables,
    cfg: SeasonConfig,
    folder: str,
    season: int,
    *,
    days: int = DEFAULT_HORIZON_DAYS,
    today: _dt.date | None = None,
    audience: str | None = None,
    session: Any = None,
) -> OpportunityReport:
    """Score the next *days* calendar days for a folder's open campaigns."""
    today = today or _dt.date.today()
    plan = _load_plan(host, folder, season)
    report = OpportunityReport(folder=folder, season=season, today=today)

    pairs = scheduling.schedulable_campaigns(plan, tables, audience=audience)
    report.campaigns_considered = len(pairs)
    if not pairs:
        report.warnings.append(
            "no campaign in this plan has an open window — nothing to schedule. "
            "`season status` shows what is planned, missed or already flown."
        )
        return report

    lat, lon = _cell(plan, host, folder)
    cards = host.job_cards(folder)
    hourly = wx.fetch_hourly(
        lat,
        lon,
        cfg,
        host.cache_dir,
        forecast_url=host.forecast_url,
        ttl_hours=host.forecast_ttl_hours,
        session=session,
    )
    report.weather_stale = hourly.stale
    if hourly.stale:
        report.warnings.append(
            "hourly conditions came from a stale cache; scores may lag reality"
        )

    slots = host.day_slots(folder)
    if not slots:
        report.warnings.append(
            "no satellite/weather day slots available — satellite coincidence "
            "scores zero and daily weather falls back to the hourly fetch"
        )

    horizon = [today + _dt.timedelta(days=i) for i in range(max(1, days))]
    conditions = scheduling.build_day_conditions(horizon, hourly, slots)
    ctx = _scoring_context(host, cfg, lat, lon, hourly, cards)

    report.opportunities = scheduling.rank_opportunities(
        [scheduling.score_day(day, pairs, conditions[day], ctx) for day in horizon]
    )
    _add_notices(report, tables, plan, ctx, horizon, cfg)
    return report


def _add_notices(
    report: OpportunityReport,
    tables: SeasonTables,
    plan: SeasonPlan,
    ctx: scheduling.ScoringContext,
    horizon: list[_dt.date],
    cfg: SeasonConfig,
) -> None:
    """The honesty lines — uncalibrated windows, and the solar wall."""
    uncalibrated = sorted(
        {
            a.crop_id
            for a in plan.assignments
            if a.crop_id and a.crop_id in tables.uncalibrated_crops
        }
    )
    if uncalibrated:
        report.notices.append(
            "These scores rank days inside windows whose stage thresholds are "
            "UNCALIBRATED (" + ", ".join(uncalibrated) + "). The day ranking is "
            "sound; the window it sits in may be off by a week or more."
        )
    if not report.flyable():
        report.notices.append(_why_nothing_flyable(report, ctx, horizon, cfg))


def _why_nothing_flyable(
    report: OpportunityReport,
    ctx: scheduling.ScoringContext,
    horizon: list[_dt.date],
    cfg: SeasonConfig,
) -> str:
    """Say which gate killed the whole horizon, not just "no good days".

    At 62.8 °N the answer is often the sun rather than the weather, and an
    operator refreshing the forecast hoping for a better day deserves to know
    that no forecast will help.
    """
    gates: dict[str, int] = {}
    for opportunity in report.opportunities:
        for score in opportunity.per_campaign:
            for gate in score.gates_failed:
                gates[gate] = gates.get(gate, 0) + 1
    if not gates:
        return "No day in the horizon scores above zero."

    worst = max(gates, key=lambda g: gates[g])
    if worst == scheduling.GATE_SOLAR:
        window = _solar_season_note(ctx, horizon, cfg)
        return (
            "Nothing is flyable in this horizon because the sun never clears the "
            "elevation floor. " + window
        )
    explanations = {
        scheduling.GATE_OUTSIDE_WINDOW: (
            "every campaign's window falls outside this horizon — try a longer "
            "--days, or check `season status` for what is upcoming"
        ),
        scheduling.GATE_WIND: "wind is above the drone limit on every day",
        scheduling.GATE_PRECIP: "rain exceeds the threshold on every day",
        scheduling.GATE_NOT_FLIGHT_READY: (
            "the underlying jobs are not flight-ready; re-export them first"
        ),
        scheduling.GATE_NO_COINCIDENT_PASS: (
            "no clear-sky satellite pass falls in the horizon, and these "
            "campaigns require one"
        ),
    }
    return "Nothing is flyable in this horizon: " + explanations.get(
        worst, f"blocked by {worst}"
    )


def _solar_season_note(
    ctx: scheduling.ScoringContext, horizon: list[_dt.date], cfg: SeasonConfig
) -> str:
    """When the sun is the blocker, name the dates on which it stops being one."""
    from flightmanager.season import solar

    year = horizon[0].year
    first, last = solar.season_window(
        ctx.lat, ctx.lon, year, cfg.min_solar_elevation_deg
    )
    if first is None:
        return (
            f"At {ctx.lat:.1f}°N the sun never reaches "
            f"{cfg.min_solar_elevation_deg:.0f}° in {year} — multispectral work "
            f"is not possible here at all."
        )
    return (
        f"At {ctx.lat:.1f}°N the sun clears {cfg.min_solar_elevation_deg:.0f}° "
        f"only between {first.isoformat()} and {last.isoformat()}; RGB-only work "
        f"at {cfg.min_solar_elevation_rgb_deg:.0f}° has a wider season. This is "
        f"a latitude limit, not a weather one — no forecast will change it."
    )


def field_day(
    host: HostContext,
    tables: SeasonTables,
    cfg: SeasonConfig,
    folder: str,
    season: int,
    day: _dt.date,
    *,
    today: _dt.date | None = None,
    audience: str | None = None,
    max_hours: float | None = None,
    session: Any = None,
) -> tuple[fieldday.FieldDayPlan, Opportunity | None, OpportunityReport]:
    """The field-day plan for one specific date."""
    today = today or _dt.date.today()
    horizon_days = max(1, (day - today).days + 1)
    report = find_opportunities(
        host,
        tables,
        cfg,
        folder,
        season,
        days=horizon_days,
        today=today,
        audience=audience,
        session=session,
    )
    opportunity = report.by_date(day)
    if opportunity is None:
        raise PlanError(
            f"{day.isoformat()} is outside the scored horizon "
            f"({today.isoformat()} onward) — pick a date from `season next`"
        )

    plan = fieldday.plan_field_day(
        opportunity,
        host.job_cards(folder),
        cfg,
        folder,
        cluster=host.cluster_launch_sites,
        max_hours=max_hours,
    )
    return (plan, opportunity, report)
