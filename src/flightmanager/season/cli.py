"""``flightmanager season …`` — the Typer sub-app.

Wired into the host CLI with two lines in ``cli.py``::

    from flightmanager.season.cli import season_app
    app.add_typer(season_app, name="season")

Phases 1 and 2 ship ``init``, ``plan``, ``status``, ``next``, ``day`` and
``export``.  The remaining verbs from spec §10.1 (``apply``, ``log``,
``calibrate``) belong to Phases 3 and 4 and are absent rather than stubbed, so
``--help`` never advertises something that does not work.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

import typer

from flightmanager.season import ics, opportunities, store
from flightmanager.season.config import load_season_config, load_season_tables
from flightmanager.season.integration import HostUnavailable, load_host
from flightmanager.season.models import Opportunity
from flightmanager.season.planner import (
    DEFAULT_CLOSING_DAYS,
    PlanError,
    StatusReport,
    init_plan,
    recompute,
    status,
    window_summary,
)

season_app = typer.Typer(
    help="Plan when in the growing season to fly each parcel, and what for.",
    no_args_is_help=True,
)

_CONFIG_OPT = typer.Option("config.toml", "--config", "-c", help="Path to config.toml.")
_FOLDER_OPT = typer.Option(
    ..., "--folder", "-f", help="Output subfolder holding the parcels."
)
_SEASON_OPT = typer.Option(
    None, "--season", help="Season year (default: the current year)."
)


def _context(config_path: str):
    """Load the host config plus the season config and agronomy tables."""
    try:
        host = load_host(config_path)
    except HostUnavailable as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    raw = host.raw_config()
    return host, load_season_config(raw), load_season_tables(raw)


def _year(season: int | None) -> int:
    return season if season is not None else _dt.date.today().year


def _season_for_sowing(season: int | None, sowing: _dt.date | None) -> int:
    """Pick the season year for ``init``.

    An explicit ``--season`` always wins.  Otherwise the sowing date decides:
    planning next season in the winter is normal, and silently filing a
    2027-05-14 sowing under season 2026 produces a plan that ``season status``
    then cannot find.
    """
    if season is not None:
        return season
    return sowing.year if sowing is not None else _dt.date.today().year


def _no_plan_message(folder_dir, folder: str, year: int) -> str:
    """Error text for a missing plan that names the seasons that do exist."""
    available = store.list_seasons(folder_dir)
    if available:
        return (
            f"No season plan for {folder} / {year}. "
            f"This folder has plans for: {', '.join(str(y) for y in available)} "
            f"— add --season <year>."
        )
    return (
        f"No season plan for {folder} / {year}. "
        f"Run `flightmanager season init --folder {folder} …` first."
    )


def _fail(exc: Exception) -> None:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(1)


def _print_lines(lines: list[str], prefix: str) -> None:
    for line in lines:
        typer.echo(f"{prefix} {line}")


@season_app.command("init")
def init_cmd(
    folder: str = _FOLDER_OPT,
    crop: Optional[str] = typer.Option(
        None, "--crop", help="Crop id applied to every job in the folder."
    ),
    sowing: Optional[str] = typer.Option(
        None, "--sowing", help="Sowing date (YYYY-MM-DD) for every job."
    ),
    from_csv: Optional[Path] = typer.Option(
        None, "--from-csv", help="Per-parcel crop/sowing CSV."
    ),
    source: str = typer.Option(
        "farmer_reported", "--source", help="Where the sowing date came from."
    ),
    season: Optional[int] = _SEASON_OPT,
    config_path: str = _CONFIG_OPT,
) -> None:
    """Assign crop and sowing date to a folder's parcels.

    Either one crop and date for the whole folder:

      flightmanager season init --folder hiilisyke-2027 --crop spring_barley --sowing 2027-05-14

    or per parcel from a spreadsheet (columns: job/parcel, crop, sowing):

      flightmanager season init --folder hiilisyke-2027 --from-csv sowing.csv
    """
    host, cfg, tables = _context(config_path)

    if from_csv and (crop or sowing):
        typer.echo(
            "Error: --from-csv cannot be combined with --crop/--sowing.", err=True
        )
        raise typer.Exit(1)
    if not from_csv and not crop and not sowing:
        typer.echo("Error: give --crop and/or --sowing, or --from-csv.", err=True)
        raise typer.Exit(1)

    sowing_date = None
    if sowing:
        try:
            sowing_date = _dt.date.fromisoformat(sowing)
        except ValueError:
            typer.echo(f"Error: --sowing {sowing!r} is not YYYY-MM-DD.", err=True)
            raise typer.Exit(1)

    year = _season_for_sowing(season, sowing_date)

    try:
        plan = init_plan(
            host,
            folder,
            year,
            crop_id=crop,
            sowing_date=sowing_date,
            sowing_source=source,
            csv_text=from_csv.read_text(encoding="utf-8-sig") if from_csv else None,
            tables=tables,
        )
    except (PlanError, ValueError, OSError) as exc:
        _fail(exc)

    typer.echo(
        f"Season {year}, folder {folder}: {len(plan.assignments)} assignment(s)."
    )
    missing = [a.job_path for a in plan.assignments if not a.start_date()]
    if missing:
        typer.echo(
            f"⚠ {len(missing)} job(s) still have no sowing date — their windows "
            f"cannot be computed and will not be guessed:"
        )
        _print_lines(missing[:10], "   ")
    typer.echo(f"Next: flightmanager season plan --folder {folder}")


@season_app.command("plan")
def plan_cmd(
    folder: str = _FOLDER_OPT,
    season: Optional[int] = _SEASON_OPT,
    audience: Optional[str] = typer.Option(
        None, "--audience", help="farmer | researcher | both"
    ),
    types: Optional[str] = typer.Option(
        None, "--types", help="Comma-separated campaign type ids to limit to."
    ),
    config_path: str = _CONFIG_OPT,
) -> None:
    """Compute (or recompute) every window in the folder's season plan.

    Idempotent and side-effect-free apart from the plan file. Re-run it as the
    season progresses: windows move as the actual temperature sum diverges from
    the climatological normal.
    """
    host, cfg, tables = _context(config_path)
    year = _year(season)
    try:
        result = recompute(
            host,
            tables,
            cfg,
            folder,
            year,
            audience=audience,
            only_types=[t.strip() for t in types.split(",")] if types else None,
        )
    except (PlanError, ValueError, OSError) as exc:
        _fail(exc)

    typer.echo(
        f"Season {year}, folder {folder}: {result.campaigns_total} campaign(s), "
        f"{result.campaigns_with_window} with a computed window."
    )
    if result.weather_sources:
        parts = ", ".join(f"{v} {k}" for k, v in sorted(result.weather_sources.items()))
        horizon = result.horizon.isoformat() if result.horizon else "unknown"
        typer.echo(f"Temperatures: {parts} (real data ends {horizon}).")
    typer.echo(f"Tables: {tables.source.summary()}")
    typer.echo(f"Written: {result.path}")
    _print_lines(result.warnings, "⚠")
    for notice in result.notices:
        typer.echo(f"\nℹ {notice}")


@season_app.command("status")
def status_cmd(
    folder: str = _FOLDER_OPT,
    season: Optional[int] = _SEASON_OPT,
    closing_days: int = typer.Option(
        DEFAULT_CLOSING_DAYS, "--closing-days", help="'Closing soon' threshold."
    ),
    audience: Optional[str] = typer.Option(
        None, "--audience", help="farmer | researcher | both"
    ),
    missed: bool = typer.Option(
        False, "--missed", help="Show only the windows that were lost."
    ),
    config_path: str = _CONFIG_OPT,
) -> None:
    """What is open, what is closing, and what was missed.

    `--missed` is how the project reports honestly on what a one-drone operation
    could actually cover:

      flightmanager season status --folder hiilisyke-2026 --season 2026 --missed
    """
    host, cfg, tables = _context(config_path)
    year = _year(season)
    try:
        folder_dir = host.folder_dir(folder)
        plan = store.load_plan(folder_dir, year)
    except (ValueError, store.SeasonStoreError) as exc:
        _fail(exc)

    if plan is None:
        typer.echo(_no_plan_message(folder_dir, folder, year), err=True)
        raise typer.Exit(1)

    report = status(plan, tables, audience=audience)
    if missed:
        _render_group(report, "Missed windows", report.missed(), show_reason=True)
        typer.echo(
            f"\n{len(report.missed())} of {len(report.rows)} campaign(s) missed."
        )
        return
    _render_status(report, closing_days)


def _render_status(report: StatusReport, closing_days: int) -> None:
    typer.echo(
        f"Season {report.season}, folder {report.folder} — {report.today.isoformat()}"
    )
    _render_group(
        report, f"Closing within {closing_days} d", report.closing(closing_days)
    )
    still_open = [r for r in report.open_now() if r not in report.closing(closing_days)]
    _render_group(report, "Open now", still_open)
    _render_group(report, "Upcoming", report.upcoming())
    _render_group(report, "Missed", report.missed())
    _render_group(report, "Blocked", report.blocked(), show_reason=True)

    flown = report.flown()
    if flown:
        typer.echo(f"\nFlown: {len(flown)}")

    notices = {r.ground_sprayer_notice for r in report.rows if r.ground_sprayer_notice}
    for notice in sorted(n for n in notices if n):
        typer.echo(f"\nℹ {notice}")
    if any(r.confidence == "low" for r in report.rows):
        typer.echo(
            "\n⚠ Some window dates rest on uncalibrated stage thresholds. The ± "
            "figure is the honest band — do not treat these dates as precise."
        )


def _render_group(
    report: StatusReport, title: str, rows: list, show_reason: bool = False
) -> None:
    if not rows:
        return
    typer.echo(f"\n{title} ({len(rows)})")
    for row in rows:
        window = (
            f"{row.earliest} → {row.latest}  target {row.target}  ±{row.uncertainty_days:.0f} d"
            if row.target
            else "no window"
        )
        typer.echo(
            f"  {row.job_path:<28} {row.label_en:<26} {window}  "
            f"[{row.state}, {row.priority}, {row.required_gsd_cm:.1f} cm, {row.sensor}]"
        )
        if show_reason:
            _print_lines(row.reasons, "      ·")


@season_app.command("crops")
def crops_cmd(config_path: str = _CONFIG_OPT) -> None:
    """List the configured crop profiles and their calibration status."""
    _, _, tables = _context(config_path)
    typer.echo(tables.source.summary())
    for crop in tables.crops.values():
        mark = "✓" if crop.is_calibrated else "⚠ uncalibrated"
        typer.echo(f"\n{crop.id}  ({crop.label_fi} / {crop.label_en})  {mark}")
        typer.echo(
            f"  model={crop.model}  base={crop.base_temp_c:.0f} °C  "
            f"cutoff={crop.cutoff_temp_c}  source: {crop.provenance.source}"
        )
        for name, value in crop.ordered_stages():
            typer.echo(f"    {name:<16} {value:>6.0f}")
    _print_lines(tables.source.warnings, "⚠")


@season_app.command("campaigns")
def campaigns_cmd(config_path: str = _CONFIG_OPT) -> None:
    """List the configured campaign types and what decision each serves."""
    _, _, tables = _context(config_path)
    typer.echo(tables.source.summary())
    for ct in sorted(tables.campaigns.values(), key=lambda c: c.id):
        typer.echo(f"\n{ct.id}  ({ct.label_fi} / {ct.label_en})")
        typer.echo(
            f"  trigger={ct.trigger_type}:{ct.trigger_stage or '-'}  "
            f"offset={ct.offset_days:+d} d  window=-{ct.window_before_days}/+{ct.window_after_days} d"
        )
        typer.echo(
            f"  {ct.required_gsd_cm:.2f} cm/px  {ct.sensor}  {ct.priority}  "
            f"audience={ct.audience}  satellite={ct.satellite_coincidence}"
        )
        if ct.decision_supported:
            typer.echo(f"  decision: {ct.decision_supported}")
        if (notice := ct.ground_sprayer_notice()) is not None:
            typer.echo(f"  ⚠ {notice}")
    _print_lines(tables.source.warnings, "⚠")


@season_app.command("windows")
def windows_cmd(
    folder: str = _FOLDER_OPT,
    campaign: str = typer.Option(..., "--campaign", help="Campaign id to inspect."),
    season: Optional[int] = _SEASON_OPT,
    config_path: str = _CONFIG_OPT,
) -> None:
    """Show one campaign's window, its basis, and why it says what it says."""
    host, _, tables = _context(config_path)
    year = _year(season)
    try:
        folder_dir = host.folder_dir(folder)
        plan = store.load_plan(folder_dir, year)
    except (ValueError, store.SeasonStoreError) as exc:
        _fail(exc)
    if plan is None:
        _fail(PlanError(_no_plan_message(folder_dir, folder, year)))

    found = plan.campaign(campaign)
    if found is None:
        _fail(PlanError(f"no campaign {campaign!r} in {folder} / {year}"))

    ct = tables.campaign_type(found.type_id)
    typer.echo(f"{found.campaign_id}")
    typer.echo(f"  type      {found.type_id}" + (f" — {ct.label_en}" if ct else ""))
    typer.echo(f"  job(s)    {', '.join(found.job_paths)}")
    typer.echo(
        f"  crop      {found.crop_id}  sowing {found.sowing_date} ({found.sowing_date_source})"
    )
    typer.echo(f"  state     {found.state}")
    typer.echo(f"  window    {window_summary(found.effective_window())}")
    window = found.effective_window()
    if window:
        typer.echo(
            f"  basis     {window.basis}  (trigger stage {window.trigger_stage})"
        )
        if window.gdd_at_target is not None:
            typer.echo(f"  thermal   {window.gdd_at_target:.0f} °C·d at target")
        _print_lines(window.notes, "      ·")
    if found.manual_window:
        typer.echo(
            "  ⚠ manual window override in effect — the model's dates are ignored"
        )
    _print_lines(found.reasons, "  ·")


# ---------------------------------------------------------------------------
# Phase 2 — scheduling
# ---------------------------------------------------------------------------


def _parse_date(text: str, flag: str) -> _dt.date:
    try:
        return _dt.date.fromisoformat(text)
    except ValueError:
        typer.echo(f"Error: {flag} {text!r} is not YYYY-MM-DD.", err=True)
        raise typer.Exit(1)


def _load_plan_or_fail(host, folder: str, year: int):
    try:
        folder_dir = host.folder_dir(folder)
        plan = store.load_plan(folder_dir, year)
    except (ValueError, store.SeasonStoreError) as exc:
        _fail(exc)
    if plan is None:
        typer.echo(_no_plan_message(folder_dir, folder, year), err=True)
        raise typer.Exit(1)
    return plan


@season_app.command("next")
def next_cmd(
    folder: str = _FOLDER_OPT,
    days: int = typer.Option(
        opportunities.DEFAULT_HORIZON_DAYS, "--days", help="Horizon to score, in days."
    ),
    season: Optional[int] = _SEASON_OPT,
    audience: Optional[str] = typer.Option(
        None, "--audience", help="farmer | researcher | both"
    ),
    limit: int = typer.Option(10, "--limit", help="How many days to print."),
    show_all: bool = typer.Option(
        False, "--all", help="Include days nothing can be flown on."
    ),
    config_path: str = _CONFIG_OPT,
) -> None:
    """Score the coming days and rank them, best first.

    Shows the component breakdown, not just the total: operators need to see
    why Thursday beat Tuesday.

      flightmanager season next --folder hiilisyke-2027 --days 10
    """
    host, cfg, tables = _context(config_path)
    year = _year(season)
    try:
        report = opportunities.find_opportunities(
            host, tables, cfg, folder, year, days=days, audience=audience
        )
    except (PlanError, ValueError, OSError) as exc:
        _fail(exc)

    typer.echo(
        f"Season {year}, folder {folder} — {report.campaigns_considered} open "
        f"campaign(s) scored over {days} day(s)"
    )
    rows = report.opportunities if show_all else report.flyable()
    if not rows:
        typer.echo("\nNo flyable day in this horizon.")
    for opportunity in rows[:limit]:
        _render_opportunity(opportunity)

    _print_lines(report.warnings, "\n⚠")
    for notice in report.notices:
        typer.echo(f"\nℹ {notice}")
    if best := report.best():
        typer.echo(
            f"\nBest: {best.date.isoformat()} "
            f"({best.date.strftime('%A')}) — "
            f"{len(best.campaign_ids)} campaign(s), {' '.join(best.best_hours) or 'no usable hours'}"
        )
        typer.echo(
            f"Next: flightmanager season day --folder {folder} --date {best.date.isoformat()}"
        )


def _render_opportunity(opportunity: Opportunity) -> None:
    """One scored day, with the components that produced the number."""
    weekday = opportunity.date.strftime("%a")
    typer.echo(
        f"\n{opportunity.date.isoformat()} {weekday}  score {opportunity.score:.2f}"
        f"  ({len(opportunity.campaign_ids)} campaign(s))"
    )
    conditions = []
    if opportunity.wind_ms is not None:
        conditions.append(f"wind {opportunity.wind_ms:.0f} m/s")
    if opportunity.gust_ms is not None:
        conditions.append(f"gust {opportunity.gust_ms:.0f}")
    if opportunity.cloud_pct is not None:
        conditions.append(f"cloud {opportunity.cloud_pct:.0f}%")
    if opportunity.precip_mm is not None:
        conditions.append(f"rain {opportunity.precip_mm:.1f} mm")
    if opportunity.max_solar_elevation_deg is not None:
        conditions.append(f"sun ≤{opportunity.max_solar_elevation_deg:.0f}°")
    if conditions:
        typer.echo("  " + "  ".join(conditions))
    if opportunity.best_hours:
        typer.echo(f"  best hours: {', '.join(opportunity.best_hours)}")

    parts = "  ".join(
        f"{name}={value:.2f}" for name, value in sorted(opportunity.components.items())
    )
    if parts:
        typer.echo(f"  components: {parts}")
    for score in opportunity.servable()[:4]:
        typer.echo(
            f"    {score.score:.2f}  {score.label_en:<26} {score.job_path}"
            + (
                f"  (closes in {score.days_to_close} d)"
                if score.days_to_close is not None
                else ""
            )
        )
    _print_lines(opportunity.flags[:4], "    ·")


@season_app.command("day")
def day_cmd(
    folder: str = _FOLDER_OPT,
    date: str = typer.Option(..., "--date", help="The field day to plan (YYYY-MM-DD)."),
    season: Optional[int] = _SEASON_OPT,
    audience: Optional[str] = typer.Option(
        None, "--audience", help="farmer | researcher | both"
    ),
    max_hours: Optional[float] = typer.Option(
        None, "--max-hours", help="Override max_field_day_hours."
    ),
    config_path: str = _CONFIG_OPT,
) -> None:
    """The field-day plan for one date: parcels, route order, batteries.

    flightmanager season day --folder hiilisyke-2027 --date 2027-06-04
    """
    host, cfg, tables = _context(config_path)
    year = _year(season)
    target = _parse_date(date, "--date")
    try:
        plan, opportunity, report = opportunities.field_day(
            host,
            tables,
            cfg,
            folder,
            year,
            target,
            audience=audience,
            max_hours=max_hours,
        )
    except (PlanError, ValueError, OSError) as exc:
        _fail(exc)

    _render_field_day(plan, opportunity, report, folder, target)


def _render_field_day(plan, opportunity, report, folder: str, target: _dt.date) -> None:
    """Print one field-day plan: route, totals, launch sites, what did not fit."""
    typer.echo(
        f"Field day {target.isoformat()} ({target.strftime('%A')}) — folder {folder}"
    )
    if opportunity is not None:
        hours = ", ".join(opportunity.best_hours)
        typer.echo(
            f"Day score {opportunity.score:.2f}"
            + (f"   best hours {hours}" if hours else "   no usable hours")
        )

    if not plan.jobs:
        typer.echo("\nNothing can be flown on this date.")
        _print_lines(plan.warnings, "⚠")
        _print_lines(report.notices, "\nℹ")
        return

    typer.echo(f"\nRoute ({plan.order_source}):")
    for job in plan.jobs:
        time_text = (
            f"{job.flight_time_min:.0f} min"
            if job.flight_time_min is not None
            else "time unknown"
        )
        typer.echo(
            f"  {job.route_index}. {job.name:<24} {time_text:>14}  "
            f"{job.battery_count or '?'} battery  "
            f"[{', '.join(job.campaign_labels)}]"
        )

    typer.echo(
        f"\nTotal: {plan.total_flight_time_h:.1f} h flying, "
        f"{plan.total_battery_count} batteries, {len(plan.jobs)} parcel(s) "
        f"(budget {plan.max_field_day_hours:.1f} h)"
    )
    _render_launch_sites(plan)
    _render_deferred(plan)
    _print_lines(plan.notices, "·")
    _print_lines(plan.warnings, "⚠")
    _print_lines(report.notices, "\nℹ")


def _render_launch_sites(plan) -> None:
    if not plan.launch_sites:
        return
    typer.echo(f"Launch sites: {len(plan.launch_sites)}")
    for site in plan.launch_sites:
        typer.echo(
            f"  site {site['index']}: {len(site['job_paths'])} parcel(s), "
            f"radius {site['radius_m']:.0f} m"
        )


def _render_deferred(plan) -> None:
    if not plan.deferred:
        return
    typer.echo(f"\nDeferred ({len(plan.deferred)}) — did not fit the day:")
    for job in plan.deferred:
        closing = (
            f", closes in {job.days_to_close} d"
            if job.days_to_close is not None
            else ""
        )
        typer.echo(f"  {job.name:<24} [{job.priority}{closing}]")


@season_app.command("export")
def export_cmd(
    folder: str = _FOLDER_OPT,
    ics_path: Path = typer.Option(
        ..., "--ics", help="Write the calendar to this path."
    ),
    season: Optional[int] = _SEASON_OPT,
    config_path: str = _CONFIG_OPT,
) -> None:
    """Export the season's windows as an iCalendar file.

    Windows become all-day events spanning earliest→latest — the window is the
    truth; the target date and its uncertainty go in the description rather
    than being claimed as an appointment.

      flightmanager season export --folder hiilisyke-2027 --ics season.ics
    """
    host, _, tables = _context(config_path)
    year = _year(season)
    plan = _load_plan_or_fail(host, folder, year)

    calendar = ics.build_calendar(plan, tables)
    try:
        ics_path.write_text(calendar, encoding="utf-8", newline="")
    except OSError as exc:
        _fail(exc)

    typer.echo(
        f"Wrote {ics.event_count(calendar)} window(s) for season {year} to {ics_path}"
    )
    if tables.uncalibrated_crops:
        typer.echo(
            "⚠ Events carry an UNCALIBRATED note in their description — the "
            "dates are approximate and will move as the season progresses. "
            "Re-export after each `season plan`."
        )
