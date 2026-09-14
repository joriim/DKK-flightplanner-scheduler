"""Orchestration: ``season init``, ``season plan``, ``season status``.

This is the only module that knows about all the others.  It resolves a
folder's jobs through the host, fetches one temperature series for the folder's
grid cell, derives a timeline per job, instantiates campaigns, merges them onto
whatever is already stored, and writes the plan back.

``recompute`` is **idempotent and side-effect-free apart from the plan file**
(spec §10.1).  Running it twice in a row produces the same plan; running it
daily through a season moves windows as the actual temperature sum diverges
from normal, which is the whole point of re-planning.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from flightmanager.season import campaigns as camp
from flightmanager.season import gsd as gsd_mod
from flightmanager.season import store
from flightmanager.season import weather_history as wx
from flightmanager.season.config import SeasonConfig, SeasonTables
from flightmanager.season.integration import HostContext, JobRef
from flightmanager.season.models import (
    Campaign,
    JobAssignment,
    SeasonPlan,
    Window,
)
from flightmanager.season.phenology import Timeline, build_timeline

log = logging.getLogger(__name__)

#: How far past the last sowing date the temperature series is built.  Long
#: enough to carry a late-sown crop to ``harvest_ready`` in a cool year.
_SEASON_TAIL_DAYS = 220

#: A window whose ``latest`` falls within this many days is "closing".
DEFAULT_CLOSING_DAYS = 5


class PlanError(Exception):
    """Raised when a plan cannot be computed at all."""


@dataclass
class PlanResult:
    """Outcome of one :func:`recompute`, for the surfaces to report."""

    plan: SeasonPlan
    path: Path | None = None
    campaigns_total: int = 0
    campaigns_with_window: int = 0
    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    weather_sources: dict[str, int] = field(default_factory=dict)
    weather_stale: bool = False
    horizon: _dt.date | None = None


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def init_plan(
    host: HostContext,
    folder: str,
    season: int,
    *,
    crop_id: str | None = None,
    sowing_date: _dt.date | None = None,
    sowing_source: str = "farmer_reported",
    csv_text: str | None = None,
    tables: SeasonTables | None = None,
) -> SeasonPlan:
    """Create or update the crop and sowing assignments for a folder.

    Two ways in, matching the CLI: one crop and sowing date applied to every job
    in the folder, or a per-parcel CSV.  Existing assignments are updated in
    place rather than replaced, so adding a parcel later does not wipe the
    sowing dates already recorded for the rest.
    """
    folder_dir = host.folder_dir(folder)
    jobs = host.jobs_in_folder(folder)
    if not jobs:
        raise PlanError(
            f"folder {folder!r} holds no jobs — create the parcel jobs first, "
            f"then plan the season over them"
        )

    if crop_id and tables and tables.crop(crop_id) is None:
        raise PlanError(
            f"unknown crop {crop_id!r}. Configured: {', '.join(sorted(tables.crops))}"
        )

    with store.plan_lock(folder_dir, season):
        plan = store.load_or_create(folder_dir, folder, season)
        known = {j.path for j in jobs}

        if csv_text is not None:
            rows = parse_sowing_csv(csv_text, known)
            for row in rows:
                upsert_assignment(plan, row)
            plan.log(
                "init",
                detail=f"{len(rows)} assignment(s) from CSV",
                campaigns_changed=0,
            )
        else:
            for job in jobs:
                upsert_assignment(
                    plan,
                    JobAssignment(
                        job_path=job.path,
                        crop_id=crop_id,
                        sowing_date=sowing_date,
                        sowing_date_source=sowing_source if sowing_date else None,
                    ),
                )
            plan.log(
                "init",
                detail=(
                    f"{len(jobs)} job(s) set to crop={crop_id!r} "
                    f"sowing={sowing_date.isoformat() if sowing_date else None}"
                ),
            )

        # Jobs that have since disappeared keep their assignment only if it
        # carries information someone typed; otherwise it is just stale.
        plan.assignments = [
            a
            for a in plan.assignments
            if a.job_path in known or a.sowing_date or a.notes
        ]
        store.save_plan(folder_dir, plan)
    return plan


def upsert_assignment(plan: SeasonPlan, new: JobAssignment) -> None:
    """Merge *new* onto the stored assignment, keeping values it leaves unset."""
    existing = plan.assignment_for(new.job_path)
    if existing is None:
        plan.assignments.append(new)
        return
    for name in (
        "crop_id",
        "sowing_date",
        "sowing_date_source",
        "accumulation_start",
    ):
        value = getattr(new, name)
        if value is not None:
            setattr(existing, name, value)
    if new.notes:
        existing.notes = new.notes


#: Accepted CSV header spellings → ``JobAssignment`` field.
_CSV_ALIASES: dict[str, str] = {
    "job": "job_path",
    "job_path": "job_path",
    "path": "job_path",
    "parcel": "job_path",
    "lohko": "job_path",
    "crop": "crop_id",
    "crop_id": "crop_id",
    "kasvi": "crop_id",
    "sowing": "sowing_date",
    "sowing_date": "sowing_date",
    "kylvo": "sowing_date",
    "kylvö": "sowing_date",
    "kylvopaiva": "sowing_date",
    "kylvöpäivä": "sowing_date",
    "accumulation_start": "accumulation_start",
    "establishment": "accumulation_start",
    "source": "sowing_date_source",
    "notes": "notes",
}


def parse_sowing_csv(text: str, known_paths: Iterable[str]) -> list[JobAssignment]:
    """Parse a per-parcel crop/sowing CSV into assignments.

    Tolerant about header spelling (English and Finnish) and about a bare parcel
    name where a full ``folder/name`` path is expected, because the sowing list
    is something a farm hands over as a spreadsheet, not something generated.
    Rows naming a job the folder does not hold are an error, not a silent skip —
    a typo'd parcel id would otherwise vanish without a word.
    """
    known = set(known_paths)
    by_leaf = {p.rstrip("/").split("/")[-1]: p for p in known}
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise PlanError("sowing CSV has no header row")

    mapping = {
        name: _CSV_ALIASES[(name or "").strip().lower().lstrip("﻿")]
        for name in reader.fieldnames
        if (name or "").strip().lower().lstrip("﻿") in _CSV_ALIASES
    }
    if "job_path" not in mapping.values():
        raise PlanError(
            "sowing CSV needs a job/parcel column; got: " + ", ".join(reader.fieldnames)
        )

    out: list[JobAssignment] = []
    for lineno, row in enumerate(reader, start=2):
        values: dict[str, Any] = {}
        for column, field_name in mapping.items():
            raw = (row.get(column) or "").strip()
            if raw:
                values[field_name] = raw

        raw_path = values.get("job_path")
        if not raw_path:
            continue  # a blank line in a spreadsheet export
        path = raw_path if raw_path in known else by_leaf.get(raw_path)
        if path is None:
            raise PlanError(f"line {lineno}: no job named {raw_path!r} in this folder")
        values["job_path"] = path

        for date_field in ("sowing_date", "accumulation_start"):
            if date_field in values:
                values[date_field] = _parse_date(values[date_field], lineno)
        values.setdefault("sowing_date_source", "farmer_reported")
        out.append(JobAssignment.model_validate(values))
    return out


def _parse_date(text: str, lineno: int) -> _dt.date:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise PlanError(
        f"line {lineno}: {text!r} is not a date (use YYYY-MM-DD or DD.MM.YYYY)"
    )


# ---------------------------------------------------------------------------
# plan / recompute
# ---------------------------------------------------------------------------


def recompute(
    host: HostContext,
    tables: SeasonTables,
    cfg: SeasonConfig,
    folder: str,
    season: int,
    *,
    today: _dt.date | None = None,
    audience: str | None = None,
    only_types: list[str] | None = None,
    session: Any = None,
) -> PlanResult:
    """Recompute every window in a folder's plan and persist it."""
    today = today or _dt.date.today()
    folder_dir = host.folder_dir(folder)

    with store.plan_lock(folder_dir, season):
        plan = store.load_or_create(folder_dir, folder, season)
        jobs = {j.path: j for j in host.jobs_in_folder(folder)}
        result = PlanResult(plan=plan)
        result.warnings.extend(tables.source.warnings)

        if not plan.assignments:
            raise PlanError(
                f"folder {folder!r} has no season assignments yet — run "
                f"`flightmanager season init --folder {folder} --crop <crop> "
                f"--sowing <date>` first"
            )

        series = _folder_series(host, cfg, plan, jobs, today, season, result, session)
        timelines = _timelines(plan, tables, cfg, series, today)
        _apply_campaigns(
            plan, tables, cfg, timelines, season, today, audience, only_types, result
        )
        _attach_gsd_flags(host, tables, cfg, plan, result)
        _collect_notices(tables, plan, result)

        plan.log(
            "plan",
            detail=f"{result.campaigns_total} campaign(s) across "
            f"{len(plan.assignments)} job(s)",
            campaigns_changed=result.campaigns_total,
            weather_basis=", ".join(
                f"{k}:{v}" for k, v in sorted(result.weather_sources.items())
            ),
        )
        result.path = store.save_plan(folder_dir, plan)
    return result


def _folder_series(
    host: HostContext,
    cfg: SeasonConfig,
    plan: SeasonPlan,
    jobs: dict[str, JobRef],
    today: _dt.date,
    season: int,
    result: PlanResult,
    session: Any,
):
    """One temperature series for the folder's representative grid cell.

    Finnish parcels in a folder are usually within a few kilometres, so one cell
    is right and cheap (spec §6.2).  A folder wider than
    ``max_folder_span_km`` gets a warning rather than silently averaging two
    different microclimates.
    """
    points = [(j.lat, j.lon) for j in jobs.values() if j.has_position]
    if plan.centroid_lat is not None and plan.centroid_lon is not None:
        lat, lon = plan.centroid_lat, plan.centroid_lon
    elif points:
        lat, lon = wx.cell_for(points)  # type: ignore[arg-type]
        plan.centroid_lat, plan.centroid_lon = lat, lon
    else:
        raise PlanError(
            f"no job in folder {plan.folder!r} has geometry, so there is no "
            f"point to fetch temperatures for"
        )

    if points:
        span = wx.span_km(points)  # type: ignore[arg-type]
        if span > cfg.max_folder_span_km:
            result.warnings.append(
                f"folder spans {span:.0f} km, wider than the "
                f"{cfg.max_folder_span_km:.0f} km one-cell limit — thermal time "
                f"is computed at a single point and may be off at the edges"
            )

    starts = [a.start_date() for a in plan.assignments if a.start_date()]
    if not starts:
        raise PlanError(
            f"no job in folder {plan.folder!r} has a sowing date — set one with "
            f"`season init --sowing`, or per parcel with `--from-csv`. Sowing "
            f"dates are not guessed."
        )
    start = min(starts)  # type: ignore[type-var]
    end = max(
        max(starts) + _dt.timedelta(days=_SEASON_TAIL_DAYS),  # type: ignore[type-var]
        today + _dt.timedelta(days=cfg.forecast_horizon_days),
        _dt.date(season, 12, 31),
    )

    series = wx.build_series(
        lat,
        lon,
        start,
        end,
        cfg,
        host.cache_dir,
        today=today,
        forecast_url=host.forecast_url,
        forecast_ttl_hours=host.forecast_ttl_hours,
        session=session,
    )
    result.weather_sources = series.counts()
    result.weather_stale = series.stale
    result.horizon = series.horizon
    result.warnings.extend(series.notes)
    return series


def _timelines(
    plan: SeasonPlan,
    tables: SeasonTables,
    cfg: SeasonConfig,
    series,
    today: _dt.date,
) -> dict[str, Timeline]:
    """A stage timeline per job that has both a crop and a start date."""
    out: dict[str, Timeline] = {}
    for assignment in plan.assignments:
        crop = tables.crop(assignment.crop_id) if assignment.crop_id else None
        start = assignment.start_date()
        if crop is None or start is None:
            continue
        out[assignment.job_path] = build_timeline(series, crop, start, cfg, today)
    return out


def _apply_campaigns(
    plan: SeasonPlan,
    tables: SeasonTables,
    cfg: SeasonConfig,
    timelines: dict[str, Timeline],
    season: int,
    today: _dt.date,
    audience: str | None,
    only_types: list[str] | None,
    result: PlanResult,
) -> None:
    """Rebuild every campaign and merge it onto what is stored."""
    existing = {c.campaign_id: c for c in plan.campaigns}
    fresh: list[Campaign] = []
    for assignment in plan.assignments:
        fresh.extend(
            camp.build_campaigns(
                tables,
                assignment,
                timelines.get(assignment.job_path),
                cfg,
                season,
                only_types=only_types,
                audience=audience,
            )
        )

    merged: list[Campaign] = []
    for candidate in fresh:
        prior = existing.pop(candidate.campaign_id, None)
        if prior is not None:
            merged.append(camp.merge_campaign(prior, candidate, today))
            continue
        # A brand-new campaign still has to be aged against the calendar: a plan
        # first computed in November must show a June window as missed, not open.
        candidate.state = camp.refresh_state(candidate, today)
        merged.append(candidate)

    # Whatever is left in `existing` no longer has a matching type or job.
    # Keep it only when someone has recorded something against it; otherwise it
    # is stale bookkeeping that would accumulate forever.
    for orphan in existing.values():
        if _carries_operator_data(orphan):
            if "orphaned" not in orphan.flags:
                orphan.flags.append("orphaned")
                orphan.reasons.append(
                    "this campaign's type or job is no longer in the plan; kept "
                    "because it carries a logged outcome or operator notes"
                )
            merged.append(orphan)
            result.warnings.append(
                f"campaign {orphan.campaign_id} kept as orphaned "
                f"(type or job gone, but it has recorded work)"
            )

    merged.sort(
        key=lambda c: (c.job_paths[0] if c.job_paths else "", c.type_id, c.repeat_index)
    )
    plan.campaigns = merged
    result.campaigns_total = len(merged)
    result.campaigns_with_window = sum(
        1 for c in merged if c.effective_window() is not None
    )


def _carries_operator_data(c: Campaign) -> bool:
    return bool(
        c.outcome
        or c.notes
        or c.manual_window
        or c.flown
        or c.state in ("flown", "skipped", "cancelled", "scheduled")
    )


def _attach_gsd_flags(
    host: HostContext,
    tables: SeasonTables,
    cfg: SeasonConfig,
    plan: SeasonPlan,
    result: PlanResult,
) -> None:
    """Flag campaigns whose GSD the configured drone cannot deliver (spec §5.3).

    Resolved once per campaign type, not per campaign: the answer depends on
    the camera and the altitude limits, neither of which varies between parcels.
    """
    drone = host.active_drone()
    if drone is None:
        return
    profiles = host.drones()
    cache: dict[str, tuple[list[str], list[str]]] = {}

    for campaign in plan.campaigns:
        ct = tables.campaign_type(campaign.type_id)
        if ct is None:
            continue
        if ct.id not in cache:
            res = gsd_mod.resolve(
                ct, drone, cfg, max_height_agl_m=host.max_height_agl_m
            )
            reasons = list(res.reasons)
            if not res.ok:
                alternatives = gsd_mod.suggest_profiles(
                    ct, profiles, cfg, max_height_agl_m=host.max_height_agl_m
                )
                reasons.append(
                    f"profiles that can meet it: {', '.join(alternatives)}"
                    if alternatives
                    else "no configured drone profile can meet this GSD"
                )
                result.warnings.append(
                    f"{ct.label_en}: {ct.required_gsd_cm:.2f} cm/px is out of "
                    f"reach for drone profile {drone.name!r}"
                )
            cache[ct.id] = (res.flags, reasons)

        flags, reasons = cache[ct.id]
        for flag in flags:
            if flag not in campaign.flags:
                campaign.flags.append(flag)
        for reason in reasons:
            if reason not in campaign.reasons:
                campaign.reasons.append(reason)


def _collect_notices(
    tables: SeasonTables, plan: SeasonPlan, result: PlanResult
) -> None:
    """The honesty lines every surface has to print."""
    uncalibrated = sorted(
        {
            a.crop_id
            for a in plan.assignments
            if a.crop_id and a.crop_id in tables.uncalibrated_crops
        }
    )
    if uncalibrated:
        result.notices.append(
            "Window dates rest on UNCALIBRATED stage thresholds for "
            + ", ".join(uncalibrated)
            + ". They are agronomic rules of thumb, not measured values — treat "
            "every date as approximate and record observed BBCH when you fly so "
            "they can be calibrated."
        )
    if result.weather_stale:
        result.notices.append(
            "Some temperatures came from a stale cache; window dates may lag the "
            "real season."
        )
    sprayer = sorted(
        {
            ct.label_en
            for c in plan.campaigns
            if (ct := tables.campaign_type(c.type_id)) and ct.drone_spray_related
        }
    )
    if sprayer:
        result.notices.append(
            "Plant-protection-related campaigns in this plan ("
            + ", ".join(sprayer)
            + ") produce maps for a GROUND SPRAYER. Aerial application of plant "
            "protection products is prohibited (Directive 2009/128/EC Art. 9; "
            "Tukes does not permit drone sprayers in Finland). This module plans "
            "imaging only — it never produces a spray route."
        )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@dataclass
class StatusRow:
    """One campaign as ``season status`` renders it."""

    campaign_id: str
    type_id: str
    label_fi: str
    label_en: str
    job_path: str
    state: str
    earliest: _dt.date | None
    target: _dt.date | None
    latest: _dt.date | None
    days_to_open: int | None
    days_to_close: int | None
    uncertainty_days: float
    confidence: str
    basis: str
    priority: str
    audience: str
    sensor: str
    required_gsd_cm: float
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    ground_sprayer_notice: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in ("open", "scheduled") and (
            self.days_to_open or 0
        ) <= 0 <= (self.days_to_close if self.days_to_close is not None else -1)


@dataclass
class StatusReport:
    """``season status`` — what is open, what is closing, what was missed."""

    folder: str
    season: int
    today: _dt.date
    rows: list[StatusRow] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def open_now(self) -> list[StatusRow]:
        return [
            r
            for r in self.rows
            if r.earliest
            and r.latest
            and r.earliest <= self.today <= r.latest
            and r.state in ("open", "scheduled")
        ]

    def closing(self, within_days: int = DEFAULT_CLOSING_DAYS) -> list[StatusRow]:
        return [
            r
            for r in self.open_now()
            if r.days_to_close is not None and r.days_to_close <= within_days
        ]

    def upcoming(self) -> list[StatusRow]:
        return [
            r
            for r in self.rows
            if r.earliest and r.earliest > self.today and r.state == "open"
        ]

    def missed(self) -> list[StatusRow]:
        return [r for r in self.rows if r.state == "missed"]

    def flown(self) -> list[StatusRow]:
        return [r for r in self.rows if r.state == "flown"]

    def blocked(self) -> list[StatusRow]:
        return [r for r in self.rows if r.state == "planned" and r.flags]


def status(
    plan: SeasonPlan,
    tables: SeasonTables,
    *,
    today: _dt.date | None = None,
    audience: str | None = None,
) -> StatusReport:
    """Build the status view over a stored plan.  Pure — reads nothing."""
    today = today or _dt.date.today()
    report = StatusReport(folder=plan.folder, season=plan.season, today=today)

    for campaign in plan.campaigns:
        ct = tables.campaign_type(campaign.type_id)
        if ct is None:
            continue
        if audience and audience != "both" and ct.audience not in (audience, "both"):
            continue
        window = campaign.effective_window()
        report.rows.append(
            StatusRow(
                campaign_id=campaign.campaign_id,
                type_id=campaign.type_id,
                label_fi=ct.label_fi,
                label_en=ct.label_en,
                job_path=campaign.job_paths[0] if campaign.job_paths else "",
                state=campaign.state,
                earliest=window.earliest if window else None,
                target=window.target if window else None,
                latest=window.latest if window else None,
                days_to_open=(window.earliest - today).days if window else None,
                days_to_close=(window.latest - today).days if window else None,
                uncertainty_days=window.uncertainty_days if window else 0.0,
                confidence=window.confidence if window else "low",
                basis=window.basis if window else "",
                priority=ct.priority,
                audience=ct.audience,
                sensor=ct.sensor,
                required_gsd_cm=ct.required_gsd_cm,
                flags=list(campaign.flags),
                reasons=list(campaign.reasons),
                ground_sprayer_notice=ct.ground_sprayer_notice(),
            )
        )

    report.rows.sort(key=lambda r: (r.target or _dt.date.max, r.job_path, r.type_id))
    return report


def window_summary(window: Window | None) -> str:
    """One-line window rendering shared by the CLI and the MCP tools."""
    if window is None:
        return "no window"
    return (
        f"{window.earliest.isoformat()} → {window.latest.isoformat()} "
        f"(target {window.target.isoformat()}, {window.uncertainty_label()})"
    )
