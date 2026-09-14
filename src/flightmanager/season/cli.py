"""``flightmanager season …`` — the Typer sub-app.

Wired into the host CLI with two lines in ``cli.py``::

    from flightmanager.season.cli import season_app
    app.add_typer(season_app, name="season")

Phase 1 ships ``init``, ``plan`` and ``status``.  The remaining verbs from spec
§10.1 (``next``, ``day``, ``apply``, ``log``, ``calibrate``, ``export``) belong
to later phases and are absent rather than stubbed, so ``--help`` never advertises
something that does not work.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

import typer

from flightmanager.season import store
from flightmanager.season.config import load_season_config, load_season_tables
from flightmanager.season.integration import HostUnavailable, load_host
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
