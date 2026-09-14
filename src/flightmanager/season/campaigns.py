"""Campaign instantiation and window derivation.

``CampaignType`` (agronomy, config) + ``Timeline`` (phenology, derived) →
``Campaign`` (bookkeeping, persisted) with a ``Window`` hanging off it.

Two invariants this module exists to protect:

**Windows are derived, never stored as truth.**  Every ``season plan`` run
recomputes them from the current temperature series.  The one exception is a
``manual_window`` the operator pinned by hand, which is copied forward
untouched — the agronomist on the ground beats the model (spec §6.4).

**Re-planning is idempotent and non-destructive.**  Campaign ids are stable, so
a second run lands on the same campaigns and refreshes their windows without
touching state, logged outcomes, notes or manual overrides.  Anything an
operator typed survives a re-plan; anything the model derived does not.
"""

from __future__ import annotations

import datetime as _dt

from flightmanager.season.config import SeasonConfig, SeasonTables
from flightmanager.season.models import (
    Campaign,
    CampaignType,
    Confidence,
    CropProfile,
    JobAssignment,
    Window,
    make_campaign_id,
)
from flightmanager.season.phenology import Timeline, thermal_repeat_dates

_CONF_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

#: How much a date's data basis alone lets you trust it, before the crop
#: table's own confidence is folded in.
_BASIS_CONFIDENCE: dict[str, Confidence] = {
    "observed": "high",
    "forecast": "medium",
    "normal": "low",
    "manual": "high",
}


def _weaker(a: str, b: str) -> Confidence:
    """The lower of two confidence levels — uncertainty does not average out."""
    return a if _CONF_RANK[a] <= _CONF_RANK[b] else b  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Window derivation
# ---------------------------------------------------------------------------


def compute_window(
    ct: CampaignType,
    timeline: Timeline,
    crop: CropProfile,
    cfg: SeasonConfig,
    *,
    target_override: _dt.date | None = None,
    uncertainty_override: float | None = None,
    basis_override: str | None = None,
    gdd_override: float | None = None,
) -> tuple[Window | None, list[str], list[str]]:
    """Derive one campaign's window.  Returns ``(window, flags, reasons)``.

    The overrides are how a repeat instance places its own target without
    re-deriving the trigger: instance *k* of a cadence campaign shares the
    trigger stage but sits *k* intervals later.
    """
    flags: list[str] = []
    reasons: list[str] = []

    if ct.trigger_type == "weather_event":
        return (
            None,
            ["weather_event_trigger"],
            [
                f"{ct.label_en} fires on an observed gust above "
                f"{ct.event_gust_ms:.0f} m/s in the previous "
                f"{ct.event_lookback_hours} h, not on a growth stage. Event "
                f"triggers are not evaluated in this phase — the campaign is "
                f"listed so it is not forgotten, with no window."
            ],
        )

    if target_override is not None:
        target = target_override
        basis = basis_override or "normal"
        uncertainty = uncertainty_override if uncertainty_override is not None else 0.0
        gdd_at = gdd_override
        trigger_stage = ct.trigger_stage
    else:
        stage = timeline.stage(ct.trigger_stage or "")
        if stage is None:
            return (
                None,
                ["no_stage_threshold"],
                [
                    f"crop {crop.id!r} defines no {ct.trigger_stage!r} stage, so "
                    f"{ct.label_en} cannot be timed against it"
                ],
            )
        if stage.date is None:
            return (
                None,
                ["trigger_not_reached"],
                [stage.reason or f"stage {ct.trigger_stage!r} is not reached"],
            )
        target = stage.date + _dt.timedelta(days=ct.offset_days)
        basis = stage.basis
        uncertainty = stage.uncertainty_days
        gdd_at = stage.gdd_at
        trigger_stage = ct.trigger_stage

    earliest = target - _dt.timedelta(days=ct.window_before_days)
    latest = target + _dt.timedelta(days=ct.window_after_days)

    deadline_stage = None
    if ct.hard_deadline_stage:
        dl = timeline.stage(ct.hard_deadline_stage)
        if dl is not None and dl.date is not None and dl.date < latest:
            deadline_stage = ct.hard_deadline_stage
            if dl.date < target:
                # The campaign's own target sits past its hard deadline. That is
                # a configuration or calibration problem, not a scheduling one —
                # say so instead of quietly emitting a one-day window.
                flags.append("window_past_deadline")
                reasons.append(
                    f"{ct.label_en} targets {target.isoformat()}, but its hard "
                    f"deadline stage {ct.hard_deadline_stage!r} is reached on "
                    f"{dl.date.isoformat()}. Check offset_days against the crop "
                    f"table — the window has been collapsed to the target day."
                )
                latest = target
            else:
                latest = dl.date
                reasons.append(
                    f"window closes early at {ct.hard_deadline_stage!r} "
                    f"({dl.date.isoformat()}) regardless of the date offset"
                )

    confidence = _weaker(crop.provenance.confidence, _BASIS_CONFIDENCE[basis])
    notes: list[str] = []
    if not crop.is_calibrated:
        notes.append(
            f"crop table {crop.id!r} is uncalibrated ({crop.provenance.source}); "
            f"treat these dates as ±{uncertainty:.0f} d, not as precise"
        )
    if basis == "normal":
        notes.append(
            "beyond the forecast horizon — dates rest on a climatological "
            "normal and will move as the season's actual temperature sum diverges"
        )
    notes.extend(reasons)

    window = Window(
        computed_at=_now_iso(),
        earliest=earliest,
        target=target,
        latest=latest,
        basis=basis,  # type: ignore[arg-type]
        uncertainty_days=uncertainty,
        confidence=confidence,
        trigger_stage=trigger_stage,
        gdd_at_target=gdd_at,
        deadline_stage=deadline_stage,
        notes=notes,
    )
    return window, flags, reasons


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Repeat expansion
# ---------------------------------------------------------------------------


def repeat_targets(
    ct: CampaignType, timeline: Timeline, crop: CropProfile
) -> list[tuple[int, _dt.date | None, str, float, float | None]]:
    """Targets for a repeatable campaign type.

    Returns ``(index, target_date, basis, uncertainty_days, gdd)`` per instance,
    index 0 being the anchor flight.  ``target_date`` is ``None`` for an
    instance the season never reaches, which is kept in the list so the plan can
    say "the fourth pass did not happen" rather than silently emitting three.
    """
    stage = timeline.stage(ct.trigger_stage or "")
    if ct.repeat is None or stage is None or stage.date is None:
        return []

    anchor = stage.date + _dt.timedelta(days=ct.offset_days)
    out: list[tuple[int, _dt.date | None, str, float, float | None]] = [
        (0, anchor, stage.basis, stage.uncertainty_days, stage.gdd_at)
    ]

    if ct.repeat.mode == "cadence":
        step = int(ct.repeat.every_days or 0)
        for i in range(1, ct.repeat.max_count):
            day = anchor + _dt.timedelta(days=i * step)
            source = _source_for(timeline, day)
            out.append(
                (
                    i,
                    day,
                    source,
                    _projected_uncertainty(stage.uncertainty_days, i, source),
                    timeline.gdd_on(day),
                )
            )
        return out

    # Adaptive thermal cadence: fly again once the crop has moved on by
    # every_gdd, which tracks a cold season instead of marching down the
    # calendar past a crop that has not changed.
    anchor_gdd = stage.gdd_at if stage.gdd_at is not None else stage.threshold
    for i, (day, raw_source, gdd) in enumerate(
        thermal_repeat_dates(
            timeline, anchor_gdd, ct.repeat.every_gdd or 0.0, ct.repeat.max_count
        ),
        start=1,
    ):
        basis = _BASIS_FOR_TEMP_SOURCE[raw_source]
        out.append(
            (
                i,
                day,
                basis,
                _projected_uncertainty(stage.uncertainty_days, i, basis),
                gdd,
            )
        )
    return out


#: Temperature-source name → the basis label a window carries.
_BASIS_FOR_TEMP_SOURCE = {
    "archive": "observed",
    "forecast": "forecast",
    "normal": "normal",
}


def _source_for(timeline: Timeline, day: _dt.date) -> str:
    if timeline.horizon is None:
        return "normal"
    if day <= timeline.horizon:
        return "observed" if day <= _dt.date.today() else "forecast"
    return "normal"


def _projected_uncertainty(base: float, index: int, basis: str) -> float:
    """Uncertainty for repeat instance *index*.

    A repeat that has already happened on measured history is a known date, so
    its band collapses to zero like any other observed stage — the property the
    test plan pins (spec §13). Only a repeat still in the future inherits the
    anchor's error and adds its own drift: a cadence campaign's third pass is no
    better known than its first, and usually worse, because the interval carries
    the anchor's error along with it. Half a day per interval is deliberately
    conservative.
    """
    if basis == "observed":
        return 0.0
    return round(base + 0.5 * index, 1)


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------


def build_campaigns(
    tables: SeasonTables,
    assignment: JobAssignment,
    timeline: Timeline | None,
    cfg: SeasonConfig,
    season: int,
    *,
    only_types: list[str] | None = None,
    audience: str | None = None,
) -> list[Campaign]:
    """Every campaign one job should carry this season.

    A job with no sowing date still gets its campaigns, in state ``planned``
    with no window and a ``sowing_date_required`` flag.  Guessing a sowing date
    would be worse than showing the gap (spec §6.4).
    """
    crop = tables.crop(assignment.crop_id) if assignment.crop_id else None
    types = _selected_types(tables, only_types, audience)
    out: list[Campaign] = []

    for ct in types:
        if crop is None or timeline is None:
            out.append(
                _blank_campaign(
                    ct, assignment, season, crop, _missing_reason(assignment)
                )
            )
            continue
        if ct.repeat is not None:
            out.extend(_repeat_campaigns(ct, assignment, season, crop, timeline, cfg))
        else:
            out.append(_one_campaign(ct, assignment, season, crop, timeline, cfg))
    return out


def _selected_types(
    tables: SeasonTables, only_types: list[str] | None, audience: str | None
) -> list[CampaignType]:
    types = tables.for_audience(audience)
    if only_types:
        wanted = set(only_types)
        unknown = wanted - set(tables.campaigns)
        if unknown:
            raise ValueError(
                f"unknown campaign type(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(sorted(tables.campaigns))}"
            )
        types = [t for t in types if t.id in wanted]
    return sorted(types, key=lambda t: t.id)


def _missing_reason(assignment: JobAssignment) -> tuple[str, str]:
    if assignment.crop_id is None:
        return (
            "crop_required",
            f"job {assignment.job_path} has no crop assigned — set one with "
            f"`season init --crop` or per-parcel via --from-csv",
        )
    return (
        "sowing_date_required",
        f"job {assignment.job_path} has no sowing date — window dates cannot be "
        f"computed and will not be guessed",
    )


def _blank_campaign(
    ct: CampaignType,
    assignment: JobAssignment,
    season: int,
    crop: CropProfile | None,
    missing: tuple[str, str],
) -> Campaign:
    flag, reason = missing
    return Campaign(
        campaign_id=make_campaign_id(season, ct.id, _scope_key(assignment.job_path)),
        type_id=ct.id,
        season=season,
        scope="job",
        job_paths=[assignment.job_path],
        crop_id=assignment.crop_id,
        sowing_date=assignment.sowing_date,
        sowing_date_source=assignment.sowing_date_source,
        state="planned",
        window=None,
        flags=[flag],
        reasons=[reason],
    )


def _one_campaign(
    ct: CampaignType,
    assignment: JobAssignment,
    season: int,
    crop: CropProfile,
    timeline: Timeline,
    cfg: SeasonConfig,
) -> Campaign:
    window, flags, reasons = compute_window(ct, timeline, crop, cfg)
    return Campaign(
        campaign_id=make_campaign_id(season, ct.id, _scope_key(assignment.job_path)),
        type_id=ct.id,
        season=season,
        scope="job",
        job_paths=[assignment.job_path],
        crop_id=crop.id,
        sowing_date=assignment.sowing_date,
        sowing_date_source=assignment.sowing_date_source,
        state="open" if window else "planned",
        window=window,
        flags=flags,
        reasons=reasons,
    )


def _repeat_campaigns(
    ct: CampaignType,
    assignment: JobAssignment,
    season: int,
    crop: CropProfile,
    timeline: Timeline,
    cfg: SeasonConfig,
) -> list[Campaign]:
    """Expand a repeatable type into its instances (spec §8).

    Modelled explicitly rather than by duplicating config entries, so the
    cadence is one number an agronomist can change instead of N near-identical
    campaign blocks that drift apart.
    """
    targets = repeat_targets(ct, timeline, crop)
    if not targets:
        return [_one_campaign(ct, assignment, season, crop, timeline, cfg)]

    out: list[Campaign] = []
    for index, target, basis, uncertainty, gdd in targets:
        if target is None:
            continue
        window, flags, reasons = compute_window(
            ct,
            timeline,
            crop,
            cfg,
            target_override=target,
            uncertainty_override=uncertainty,
            basis_override=basis,
            gdd_override=gdd,
        )
        out.append(
            Campaign(
                campaign_id=make_campaign_id(
                    season, ct.id, _scope_key(assignment.job_path), index
                ),
                type_id=ct.id,
                season=season,
                scope="job",
                job_paths=[assignment.job_path],
                crop_id=crop.id,
                sowing_date=assignment.sowing_date,
                sowing_date_source=assignment.sowing_date_source,
                state="open" if window else "planned",
                window=window,
                repeat_index=index,
                flags=flags,
                reasons=reasons,
            )
        )
    return out


def _scope_key(job_path: str) -> str:
    """Last path segment — the parcel name — as the campaign id's scope key."""
    return job_path.rstrip("/").split("/")[-1]


# ---------------------------------------------------------------------------
# Merging a fresh computation onto a stored plan
# ---------------------------------------------------------------------------

#: Fields an operator owns.  ``season plan`` must never overwrite them.
_OPERATOR_OWNED = (
    "state",
    "flown",
    "flight_job_name",
    "outcome",
    "notes",
    "manual_window",
)


def merge_campaign(existing: Campaign, fresh: Campaign, today: _dt.date) -> Campaign:
    """Refresh *existing* with *fresh*'s derived window, keeping operator data.

    This is what makes ``season plan`` idempotent and safe to re-run daily:
    the model owns ``window``, ``flags`` and ``reasons``; the operator owns
    everything in :data:`_OPERATOR_OWNED`.
    """
    merged = fresh.model_copy(deep=True)
    for field_name in _OPERATOR_OWNED:
        setattr(merged, field_name, getattr(existing, field_name))
    merged.repeat_index = existing.repeat_index
    merged.state = refresh_state(merged, today)
    return merged


def refresh_state(campaign: Campaign, today: _dt.date) -> str:
    """Advance only the states the calendar is allowed to advance.

    A campaign becomes ``missed`` when its window closes unflown — that is a
    first-class outcome, and the only transition the passage of time makes on
    its own.  ``flown``, ``skipped`` and ``cancelled`` are decisions, and the
    clock does not get to revise them.
    """
    if campaign.state in ("flown", "skipped", "cancelled"):
        return campaign.state

    window = campaign.effective_window()
    if window is None:
        return "planned" if campaign.state != "missed" else "missed"

    if today > window.latest:
        return "missed"
    # A window that moved back into the future — a cold spell pushing the stage
    # later — un-misses the campaign. The window is derived, so it may legitimately
    # move; the operator has still not decided anything.
    if campaign.state == "missed":
        return "open"
    return campaign.state if campaign.state == "scheduled" else "open"
