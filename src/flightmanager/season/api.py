"""REST surface for the season module.

Wired into the host FastAPI app with two lines in ``web/server.py``::

    from flightmanager.season.api import router as season_router
    app.include_router(season_router)

These routes are fast — no tiles, no orbit propagation, no export — and they
**must not acquire the pipeline lock** (spec §2/§10.2).  There is nothing to
serialise against: a season request touches only the folder's plan file, under
that file's own lock.  A season call therefore succeeds while a job export is
running, which is the regression this design is built to avoid.

No SSE: every route returns in one response.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from fastapi.responses import Response

from flightmanager.season import ics, opportunities, store
from flightmanager.season.config import load_season_config, load_season_tables
from flightmanager.season.integration import FlightmanagerHost
from flightmanager.season.models import JobAssignment
from flightmanager.season.planner import PlanError, init_plan, recompute, status

router = APIRouter(prefix="/api/season", tags=["season"])


def _host() -> FlightmanagerHost:
    """Build a host context from the running server's config.

    Reads ``_server_state`` rather than reloading config.toml so the API sees
    whatever the Settings UI last saved, exactly like the other routers.
    """
    import flightmanager.web._server_state as st

    if st.config is None:  # pragma: no cover - only before startup completes
        raise HTTPException(503, "server is still starting")
    return FlightmanagerHost(
        config=st.config, config_path=getattr(st, "config_path", None)
    )


def _tables_and_cfg(host: FlightmanagerHost):
    raw = host.raw_config()
    return load_season_config(raw), load_season_tables(raw)


def _year(season: int | None) -> int:
    return season if season is not None else _dt.date.today().year


def _guard(fn, *args, **kwargs):
    """Map the module's own errors onto HTTP status codes."""
    try:
        return fn(*args, **kwargs)
    except PlanError as exc:
        raise HTTPException(409, str(exc)) from exc
    except store.SeasonStoreError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:  # unsafe folder name, bad crop id, bad CSV
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class AssignmentIn(BaseModel):
    job_path: str
    crop_id: str | None = None
    sowing_date: _dt.date | None = None
    sowing_date_source: str | None = "farmer_reported"
    accumulation_start: _dt.date | None = None
    notes: str = ""


class SeasonInitIn(BaseModel):
    """Either a folder-wide crop/sowing, or an explicit per-job list."""

    season: int | None = None
    crop_id: str | None = None
    sowing_date: _dt.date | None = None
    sowing_date_source: str = "farmer_reported"
    assignments: list[AssignmentIn] | None = None


class SeasonPlanIn(BaseModel):
    season: int | None = None
    audience: str | None = Field(default=None, description="farmer | researcher | both")
    types: list[str] | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{folder}")
async def get_season(folder: str, season: int | None = None) -> dict[str, Any]:
    """The stored plan plus its computed windows and status summary."""
    host = _host()
    cfg, tables = _tables_and_cfg(host)
    year = _year(season)
    folder_dir = _guard(host.folder_dir, folder)
    plan = _guard(store.load_plan, folder_dir, year)
    if plan is None:
        raise HTTPException(
            404,
            f"no season plan for {folder} / {year} — POST to this path to create one",
        )

    report = status(plan, tables)
    return {
        "plan": plan.model_dump(mode="json"),
        "seasons_available": store.list_seasons(folder_dir),
        "tables": {
            "summary": tables.source.summary(),
            "warnings": tables.source.warnings,
            "uncalibrated_crops": tables.uncalibrated_crops,
        },
        "status": _status_payload(report),
        "settings": cfg.model_dump(mode="json"),
    }


@router.post("/{folder}")
async def post_season(folder: str, body: SeasonInitIn) -> dict[str, Any]:
    """Create the plan, or update its crop and sowing assignments."""
    host = _host()
    _, tables = _tables_and_cfg(host)
    year = _year(body.season)

    if body.assignments is not None:
        plan = _guard(_apply_assignments, host, folder, year, body.assignments)
    else:
        plan = _guard(
            init_plan,
            host,
            folder,
            year,
            crop_id=body.crop_id,
            sowing_date=body.sowing_date,
            sowing_source=body.sowing_date_source,
            tables=tables,
        )
    return {"plan": plan.model_dump(mode="json")}


def _apply_assignments(
    host: FlightmanagerHost, folder: str, year: int, rows: list[AssignmentIn]
):
    """Write an explicit per-job assignment list into the plan."""
    from flightmanager.season.planner import upsert_assignment

    folder_dir = host.folder_dir(folder)
    known = {j.path for j in host.jobs_in_folder(folder)}
    unknown = [r.job_path for r in rows if r.job_path not in known]
    if unknown:
        raise PlanError(
            f"folder {folder!r} holds no job(s): {', '.join(sorted(unknown)[:5])}"
        )

    with store.plan_lock(folder_dir, year):
        plan = store.load_or_create(folder_dir, folder, year)
        for row in rows:
            upsert_assignment(plan, JobAssignment(**row.model_dump()))
        plan.log("init", detail=f"{len(rows)} assignment(s) via API")
        store.save_plan(folder_dir, plan)
    return plan


@router.post("/{folder}/plan")
async def post_plan(folder: str, body: SeasonPlanIn | None = None) -> dict[str, Any]:
    """Recompute every window in the folder's plan."""
    host = _host()
    cfg, tables = _tables_and_cfg(host)
    body = body or SeasonPlanIn()
    year = _year(body.season)

    result = _guard(
        recompute,
        host,
        tables,
        cfg,
        folder,
        year,
        audience=body.audience,
        only_types=body.types,
    )
    return {
        "plan": result.plan.model_dump(mode="json"),
        "campaigns_total": result.campaigns_total,
        "campaigns_with_window": result.campaigns_with_window,
        "weather_sources": result.weather_sources,
        "weather_stale": result.weather_stale,
        "horizon": result.horizon.isoformat() if result.horizon else None,
        "warnings": result.warnings,
        "notices": result.notices,
    }


@router.get("/{folder}/status")
async def get_status(
    folder: str, season: int | None = None, audience: str | None = None
) -> dict[str, Any]:
    """What is open, what is closing, what was missed."""
    host = _host()
    _, tables = _tables_and_cfg(host)
    year = _year(season)
    plan = _guard(store.load_plan, _guard(host.folder_dir, folder), year)
    if plan is None:
        raise HTTPException(404, f"no season plan for {folder} / {year}")
    return _status_payload(status(plan, tables, audience=audience))


@router.get("/{folder}/opportunities")
async def get_opportunities(
    folder: str,
    days: int = 14,
    season: int | None = None,
    audience: str | None = None,
    include_unflyable: bool = False,
) -> dict[str, Any]:
    """Scored candidate days, best first.

    Fast enough to serve inline (no SSE): the expensive parts — orbit
    propagation and the MGRS grid — are the host forecast's own disk-cached
    work, and the solar ephemeris is arithmetic.
    """
    host = _host()
    cfg, tables = _tables_and_cfg(host)
    report = _guard(
        opportunities.find_opportunities,
        host,
        tables,
        cfg,
        folder,
        _year(season),
        days=max(1, min(days, 30)),
        audience=audience,
    )
    rows = report.opportunities if include_unflyable else report.flyable()
    return {
        "folder": report.folder,
        "season": report.season,
        "today": report.today.isoformat(),
        "campaigns_considered": report.campaigns_considered,
        "weather_stale": report.weather_stale,
        "opportunities": [o.model_dump(mode="json") for o in rows],
        "best": report.best().model_dump(mode="json") if report.best() else None,
        "warnings": report.warnings,
        "notices": report.notices,
    }


@router.get("/{folder}/day/{date}")
async def get_day(
    folder: str,
    date: str,
    season: int | None = None,
    audience: str | None = None,
    max_hours: float | None = None,
) -> dict[str, Any]:
    """The field-day plan for one date: jobs in route order, batteries, sites.

    The payload is shaped to feed the existing route/export flow straight back:
    ``job_paths`` is the flight sequence the reorder endpoint accepts.
    """
    host = _host()
    cfg, tables = _tables_and_cfg(host)
    try:
        target = _dt.date.fromisoformat(date)
    except ValueError as exc:
        raise HTTPException(400, f"{date!r} is not an ISO date") from exc

    plan, opportunity, report = _guard(
        opportunities.field_day,
        host,
        tables,
        cfg,
        folder,
        _year(season),
        target,
        audience=audience,
        max_hours=max_hours,
    )
    return {
        "date": target.isoformat(),
        "folder": folder,
        "order_source": plan.order_source,
        "job_paths": plan.job_paths,
        "jobs": [
            {
                "job_path": j.job_path,
                "name": j.name,
                "route_index": j.route_index,
                "flight_time_min": j.flight_time_min,
                "battery_count": j.battery_count,
                "flight_ready": j.flight_ready,
                "priority": j.priority,
                "days_to_close": j.days_to_close,
                "takeoff_4326": j.takeoff_4326,
                "campaigns": [c.model_dump(mode="json") for c in j.campaigns],
            }
            for j in plan.jobs
        ],
        "deferred": [
            {
                "job_path": j.job_path,
                "name": j.name,
                "priority": j.priority,
                "days_to_close": j.days_to_close,
                "campaigns": [c.model_dump(mode="json") for c in j.campaigns],
            }
            for j in plan.deferred
        ],
        "launch_sites": plan.launch_sites,
        "total_flight_time_min": plan.total_flight_time_min,
        "total_flight_time_h": plan.total_flight_time_h,
        "total_battery_count": plan.total_battery_count,
        "max_field_day_hours": plan.max_field_day_hours,
        "overflows": plan.overflows,
        "best_hours": plan.best_hours,
        "opportunity": opportunity.model_dump(mode="json") if opportunity else None,
        "warnings": plan.warnings,
        "notices": plan.notices + report.notices,
    }


@router.get("/{folder}/ics")
async def get_ics(folder: str, season: int | None = None) -> Response:
    """The season's windows as a downloadable iCalendar file."""
    host = _host()
    _, tables = _tables_and_cfg(host)
    year = _year(season)
    plan = _guard(store.load_plan, _guard(host.folder_dir, folder), year)
    if plan is None:
        raise HTTPException(404, f"no season plan for {folder} / {year}")

    calendar = ics.build_calendar(plan, tables)
    return Response(
        content=calendar,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="season_{folder}_{year}.ics"'
            )
        },
    )


@router.get("/-/crops")
async def get_crops() -> dict[str, Any]:
    """The configured crop profiles, with their provenance and confidence.

    Under ``/-/`` so a folder can never be named ``crops`` and shadow it.
    """
    host = _host()
    _, tables = _tables_and_cfg(host)
    return {
        "crops": [c.model_dump(mode="json") for c in tables.crops.values()],
        "uncalibrated": tables.uncalibrated_crops,
        "summary": tables.source.summary(),
        "warnings": tables.source.warnings,
    }


@router.get("/-/campaign-types")
async def get_campaign_types() -> dict[str, Any]:
    """The configured campaign library."""
    host = _host()
    _, tables = _tables_and_cfg(host)
    return {
        "campaign_types": [
            {
                **ct.model_dump(mode="json"),
                "ground_sprayer_notice": ct.ground_sprayer_notice(),
            }
            for ct in tables.campaigns.values()
        ],
        "summary": tables.source.summary(),
        "warnings": tables.source.warnings,
    }


def _status_payload(report) -> dict[str, Any]:
    """Serialise a :class:`StatusReport` for the UI's timeline and strip."""

    def rows(items) -> list[dict[str, Any]]:
        return [
            {
                "campaign_id": r.campaign_id,
                "type_id": r.type_id,
                "label_fi": r.label_fi,
                "label_en": r.label_en,
                "job_path": r.job_path,
                "state": r.state,
                "earliest": r.earliest.isoformat() if r.earliest else None,
                "target": r.target.isoformat() if r.target else None,
                "latest": r.latest.isoformat() if r.latest else None,
                "days_to_open": r.days_to_open,
                "days_to_close": r.days_to_close,
                "uncertainty_days": r.uncertainty_days,
                "confidence": r.confidence,
                "basis": r.basis,
                "priority": r.priority,
                "audience": r.audience,
                "sensor": r.sensor,
                "required_gsd_cm": r.required_gsd_cm,
                "flags": r.flags,
                "reasons": r.reasons,
                "ground_sprayer_notice": r.ground_sprayer_notice,
            }
            for r in items
        ]

    return {
        "folder": report.folder,
        "season": report.season,
        "today": report.today.isoformat(),
        "all": rows(report.rows),
        "open_now": rows(report.open_now()),
        "closing": rows(report.closing()),
        "upcoming": rows(report.upcoming()),
        "missed": rows(report.missed()),
        "flown": rows(report.flown()),
        "blocked": rows(report.blocked()),
    }
