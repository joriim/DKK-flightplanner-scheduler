"""MCP tools for the season module.

Registered onto the host's ``FastMCP`` instance with one line in
``mcp_server.py``::

    from flightmanager.season.mcp_tools import register
    register(mcp)

Phase 1 exposes the three read tools (spec §10.3): ``season_status``,
``campaign_detail`` and ``crop_profiles``.  ``season_opportunities`` and
``season_day_plan`` need day-level scoring and arrive with Phase 2; the write
tools arrive with Phase 3/4.

These are the queries that should work end to end today::

    When does the emergence window open for folder hiilisyke-2027?
    Which parcels have a window closing in the next 5 days?
    Which windows did we miss last season and why?

Every payload carries its uncertainty and calibration status.  An assistant
reading these tools must be able to say "24-31 May, ±7 days, uncalibrated" and
never "24 May".
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from flightmanager.season import store
from flightmanager.season.config import load_season_config, load_season_tables
from flightmanager.season.integration import FlightmanagerHost, HostUnavailable
from flightmanager.season.planner import StatusRow, status


def _host() -> FlightmanagerHost:
    """Host context from the running server when integrated, else from disk."""
    try:
        import flightmanager.web._server_state as st

        if st.config is not None:
            return FlightmanagerHost(
                config=st.config, config_path=getattr(st, "config_path", None)
            )
    except ImportError:
        pass
    from flightmanager.season.integration import load_host

    return load_host()


def _context():
    host = _host()
    raw = host.raw_config()
    return host, load_season_config(raw), load_season_tables(raw)


def _year(season: int | None) -> int:
    return season if season is not None else _dt.date.today().year


def _err(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _row(r: StatusRow) -> dict[str, Any]:
    """One campaign, phrased so an assistant cannot drop the error bar."""
    window = (
        f"{r.earliest} → {r.latest} (target {r.target}, ±{r.uncertainty_days:.0f} d"
        + (", UNCALIBRATED" if r.confidence == "low" else "")
        + ")"
        if r.target
        else "no window"
    )
    out = {
        "campaign_id": r.campaign_id,
        "type": r.type_id,
        "label_en": r.label_en,
        "label_fi": r.label_fi,
        "job": r.job_path,
        "state": r.state,
        "window": window,
        "earliest": r.earliest,
        "target": r.target,
        "latest": r.latest,
        "uncertainty_days": r.uncertainty_days,
        "confidence": r.confidence,
        "basis": r.basis,
        "days_to_open": r.days_to_open,
        "days_to_close": r.days_to_close,
        "priority": r.priority,
        "audience": r.audience,
        "sensor": r.sensor,
        "required_gsd_cm": r.required_gsd_cm,
    }
    if r.flags:
        out["flags"] = r.flags
    if r.reasons:
        out["reasons"] = r.reasons
    if r.ground_sprayer_notice:
        out["ground_sprayer_notice"] = r.ground_sprayer_notice
    return out


def season_status(
    folder: str,
    season: int | None = None,
    view: str = "summary",
    closing_days: int = 5,
    audience: str | None = None,
) -> str:
    """Growing-season campaign windows for a folder: open, closing, missed.

    Answers "when does the emergence window open?", "which parcels have a
    window closing in the next 5 days?" and "which windows did we miss last
    season, and why?".

    Every window carries an uncertainty band and a confidence level. The
    seeded stage thresholds are UNCALIBRATED agronomic rules of thumb, so a
    window must always be reported with its ± band — never as a single
    precise date.

    Args:
        folder: Output subfolder holding the parcels.
        season: Season year. Defaults to the current year.
        view: "summary" (counts + the urgent groups), "all", "open",
            "closing", "upcoming", "missed", "flown", or "blocked".
        closing_days: Threshold in days for the "closing" view.
        audience: Limit to "farmer" or "researcher" campaigns.

    Returns the matching campaigns, or an error if the folder has no plan.
    """
    try:
        host, _, tables = _context()
        folder_dir = host.folder_dir(folder)
    except (HostUnavailable, ValueError) as exc:
        return _err(str(exc))

    year = _year(season)
    try:
        plan = store.load_plan(folder_dir, year)
    except store.SeasonStoreError as exc:
        return _err(str(exc))
    if plan is None:
        available = store.list_seasons(folder_dir)
        return _err(
            f"No season plan for folder {folder!r} / {year}."
            + (f" Seasons with a plan: {available}." if available else "")
        )

    report = status(plan, tables, audience=audience)
    views = {
        "all": report.rows,
        "open": report.open_now(),
        "closing": report.closing(closing_days),
        "upcoming": report.upcoming(),
        "missed": report.missed(),
        "flown": report.flown(),
        "blocked": report.blocked(),
    }
    payload: dict[str, Any] = {
        "folder": folder,
        "season": year,
        "today": report.today.isoformat(),
        "counts": {name: len(rows) for name, rows in views.items()},
        "uncalibrated_crops": tables.uncalibrated_crops,
    }
    if view == "summary":
        payload["closing"] = [_row(r) for r in views["closing"]]
        payload["open"] = [_row(r) for r in views["open"]]
        payload["blocked"] = [_row(r) for r in views["blocked"]]
    elif view in views:
        payload["campaigns"] = [_row(r) for r in views[view]]
    else:
        return _err(f"unknown view {view!r}; use one of: summary, {', '.join(views)}")

    if tables.uncalibrated_crops:
        payload["caveat"] = (
            "Stage thresholds for these crops are uncalibrated placeholders. "
            "Report every window with its ± band, not as a precise date."
        )
    return _dump(payload)


def campaign_detail(folder: str, campaign_id: str, season: int | None = None) -> str:
    """Everything known about one campaign: window, basis, flags, outcome.

    Use after season_status to explain *why* a window falls where it does —
    which growth stage triggered it, whether the dates rest on measured
    history, a forecast or a climatological normal, and what is blocking it.

    Args:
        folder: Output subfolder holding the parcels.
        campaign_id: Id from season_status, e.g. "2027-emergence_count-5241087453".
        season: Season year. Defaults to the current year.
    """
    try:
        host, _, tables = _context()
        folder_dir = host.folder_dir(folder)
    except (HostUnavailable, ValueError) as exc:
        return _err(str(exc))

    year = _year(season)
    try:
        plan = store.load_plan(folder_dir, year)
    except store.SeasonStoreError as exc:
        return _err(str(exc))
    if plan is None:
        return _err(f"No season plan for folder {folder!r} / {year}.")

    campaign = plan.campaign(campaign_id)
    if campaign is None:
        return _err(
            f"No campaign {campaign_id!r} in {folder}/{year}. "
            f"Use season_status to list them."
        )

    ct = tables.campaign_type(campaign.type_id)
    crop = tables.crop(campaign.crop_id) if campaign.crop_id else None
    window = campaign.effective_window()
    payload: dict[str, Any] = {
        "campaign": campaign.model_dump(mode="json"),
        "window_in_effect": "manual override" if campaign.manual_window else "derived",
        "type": ct.model_dump(mode="json") if ct else None,
        "ground_sprayer_notice": ct.ground_sprayer_notice() if ct else None,
    }
    if crop is not None:
        payload["crop"] = {
            "id": crop.id,
            "label_en": crop.label_en,
            "model": crop.model,
            "base_temp_c": crop.base_temp_c,
            "calibrated": crop.is_calibrated,
            "provenance": crop.provenance.model_dump(mode="json"),
            "trigger_threshold_cd": crop.threshold(window.trigger_stage)
            if window and window.trigger_stage
            else None,
        }
    if window is not None:
        payload["interpretation"] = (
            f"{window.earliest} to {window.latest}, best on {window.target}, "
            f"{window.uncertainty_label()}. Dates rest on {window.basis} "
            f"temperatures."
        )
    return _dump(payload)


def crop_profiles() -> str:
    """The configured crop phenology tables and how much to trust them.

    Returns each crop's stage thresholds in accumulated effective °C·d
    (base +5 °C, the Finnish tehoisa lämpösumma convention) together with
    its provenance and confidence. Crops listed under "uncalibrated" carry
    placeholder thresholds: state that whenever you quote a date derived
    from them.
    """
    try:
        _, _, tables = _context()
    except (HostUnavailable, ValueError) as exc:
        return _err(str(exc))

    return _dump(
        {
            "summary": tables.source.summary(),
            "warnings": tables.source.warnings,
            "uncalibrated": tables.uncalibrated_crops,
            "crops": [c.model_dump(mode="json") for c in tables.crops.values()],
            "campaign_types": [
                {
                    "id": ct.id,
                    "label_en": ct.label_en,
                    "label_fi": ct.label_fi,
                    "trigger_type": ct.trigger_type,
                    "trigger_stage": ct.trigger_stage,
                    "required_gsd_cm": ct.required_gsd_cm,
                    "sensor": ct.sensor,
                    "audience": ct.audience,
                    "decision_supported": ct.decision_supported,
                    "ground_sprayer_notice": ct.ground_sprayer_notice(),
                }
                for ct in tables.campaigns.values()
            ],
            "note": (
                "This module plans imaging only. Campaigns flagged with a "
                "ground_sprayer_notice produce prescription maps for a ground "
                "sprayer — drone application of plant protection products is "
                "prohibited in Finland and never planned here."
            ),
        }
    )


#: The Phase 1 read tools, in the order they are registered.
_TOOLS = (season_status, campaign_detail, crop_profiles)


def register(mcp: Any) -> None:
    """Register the season read tools on the host's FastMCP instance.

    The tool functions live at module scope rather than nested here so that each
    keeps its own signature and docstring — FastMCP derives the tool schema and
    the description an assistant reads from exactly those.
    """
    for tool in _TOOLS:
        mcp.tool()(tool)
