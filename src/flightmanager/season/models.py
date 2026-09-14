"""Pydantic models for the growing-season planning module.

Layering (spec §3), which the types below deliberately keep apart:

``CropProfile`` / ``CampaignType``
    Agronomy.  Stable, shared, config-owned.  Never written back by the planner
    except through the explicit calibration workflow.
``Campaign``
    Bookkeeping.  One per parcel (or folder) per season, mutable, persisted.
``Window``
    Derived.  Recomputed from phenology on every ``season plan``; never the
    source of truth, except for a ``manual_window`` the operator pinned by hand.

Every stage threshold carries :class:`Provenance`.  The seeded values are
placeholders (spec Appendix B) and the code must surface that rather than hide
it — see :meth:`CropProfile.is_calibrated` and :attr:`Window.confidence`, both
of which feed the "window dates ±7 d, uncalibrated" line in every surface.
"""

from __future__ import annotations

import datetime as _dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bump when the on-disk season_<year>.json shape changes in a way that needs a
# migration.  v1: initial Phase 1 layout.
SCHEMA_VERSION = 1

Confidence = Literal["low", "medium", "high"]
Sensor = Literal["rgb", "multispectral", "both", "thermal"]
Priority = Literal["high", "normal", "opportunistic"]
Sensitivity = Literal["low", "normal", "high"]
Audience = Literal["farmer", "researcher", "both"]
TriggerType = Literal["stage", "cadence", "weather_event"]
Coincidence = Literal["required", "preferred", "ignored"]
PhenologyModel = Literal["gdd", "fixed_days", "manual"]

#: Campaign lifecycle.  ``missed`` is a first-class outcome on purpose: half the
#: point of the module is to show, at season end, which windows were lost to
#: weather rather than quietly dropping them (spec §4.3).
CampaignState = Literal[
    "planned",  # no window yet — usually a missing sowing date
    "open",  # window computed; today may or may not be inside it
    "scheduled",  # a day has been picked
    "flown",
    "missed",  # window closed with no flight
    "skipped",  # operator decided not to fly it
    "cancelled",  # no longer applicable (crop changed, parcel dropped)
]

#: Allowed state transitions.  Enforced by :func:`check_transition` so that a
#: typo in an API call cannot walk a campaign backwards out of ``flown``.
_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"planned", "open", "skipped", "cancelled"},
    "open": {"open", "planned", "scheduled", "flown", "missed", "skipped", "cancelled"},
    "scheduled": {"scheduled", "open", "flown", "missed", "skipped", "cancelled"},
    "flown": {"flown"},
    "missed": {"missed", "open", "cancelled"},
    "skipped": {"skipped", "open", "cancelled"},
    "cancelled": {"cancelled"},
}


class InvalidTransition(ValueError):
    """Raised when a campaign is asked to move to a state it cannot reach."""


def check_transition(current: str, new: str) -> None:
    """Raise :class:`InvalidTransition` unless ``current → new`` is allowed."""
    allowed = _TRANSITIONS.get(current)
    if allowed is None:
        raise InvalidTransition(f"unknown campaign state {current!r}")
    if new not in allowed:
        raise InvalidTransition(
            f"cannot move campaign from {current!r} to {new!r} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class Provenance(BaseModel):
    """Where a set of stage thresholds came from, and how much to trust it.

    Shipped values are ``confidence="low"`` with an empty ``calibrated_from``.
    The calibration loop (spec §6.3, Phase 4) is what upgrades them, and it
    writes ``n`` and ``residual_spread_cd`` so the uncertainty band stops being
    a guess.
    """

    model_config = ConfigDict(extra="allow")

    source: str = "provisional — general Finnish agronomic rules of thumb"
    confidence: Confidence = "low"
    #: Observation ids the fit used.  Empty means nothing has been calibrated.
    calibrated_from: list[str] = Field(default_factory=list)
    #: Number of (accumulated °C·d, BBCH) pairs behind the fit.
    n: int | None = None
    #: Residual spread of the fit, in °C·d.  Converted to days by the phenology
    #: engine using the local rate of thermal-time accumulation.
    residual_spread_cd: float | None = None

    @property
    def is_calibrated(self) -> bool:
        return bool(self.calibrated_from) and self.confidence != "low"


# ---------------------------------------------------------------------------
# Crop profile (spec §4.1)
# ---------------------------------------------------------------------------


class CropProfile(BaseModel):
    """Crop + variety group → phenology parameters.

    ``stages`` maps a stage name to accumulated effective °C·d from
    ``accumulation_start`` for ``model="gdd"``, and to plain days from the same
    date for ``model="fixed_days"``.  ``model="manual"`` carries neither: the
    operator sets every window by hand.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    label_fi: str
    label_en: str
    #: Finnish *tehoisa lämpösumma* convention.
    base_temp_c: float = 5.0
    #: Upper cap applied to the daily mean before the base is subtracted.
    cutoff_temp_c: float | None = 30.0
    model: PhenologyModel = "gdd"
    stages: dict[str, float] = Field(default_factory=dict)
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="after")
    def _check_stages(self) -> "CropProfile":
        if self.model in ("gdd", "fixed_days") and not self.stages:
            raise ValueError(
                f"crop {self.id!r} uses model={self.model!r} but defines no stages"
            )
        if self.cutoff_temp_c is not None and self.cutoff_temp_c <= self.base_temp_c:
            raise ValueError(
                f"crop {self.id!r}: cutoff_temp_c must be above base_temp_c"
            )
        for name, value in self.stages.items():
            if value < 0:
                raise ValueError(f"crop {self.id!r}: stage {name!r} is negative")
        return self

    @property
    def is_calibrated(self) -> bool:
        return self.provenance.is_calibrated

    def ordered_stages(self) -> list[tuple[str, float]]:
        """Stages sorted by threshold — the order the crop actually passes them."""
        return sorted(self.stages.items(), key=lambda kv: kv[1])

    def threshold(self, stage: str) -> float | None:
        return self.stages.get(stage)


# ---------------------------------------------------------------------------
# Campaign type (spec §4.2)
# ---------------------------------------------------------------------------


class RepeatSpec(BaseModel):
    """How a repeatable campaign type spawns its instances.

    ``cadence``
        Fixed calendar interval — ``every_days`` between flights.
    ``thermal``
        Adaptive: fly again once ``every_gdd`` further °C·d have accumulated.
        More defensible agronomically (spec §14.4) because it tracks the crop
        rather than the calendar, and it costs nothing extra — the thermal-time
        series is already computed.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["cadence", "thermal"] = "cadence"
    every_days: int | None = Field(default=None, gt=0)
    every_gdd: float | None = Field(default=None, gt=0)
    max_count: int = Field(default=3, gt=0, le=20)

    @model_validator(mode="after")
    def _check_mode(self) -> "RepeatSpec":
        if self.mode == "cadence" and self.every_days is None:
            raise ValueError("repeat mode 'cadence' needs every_days")
        if self.mode == "thermal" and self.every_gdd is None:
            raise ValueError("repeat mode 'thermal' needs every_gdd")
        return self


class CampaignType(BaseModel):
    """A purpose, a trigger, and the acquisition parameters it demands.

    This is the agronomy half of the model.  It says *why* you would fly and
    *what the imagery has to resolve*; it never names a drone or an altitude —
    those are derived from ``required_gsd_cm`` against whatever profile the job
    is configured for (spec §5).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    label_fi: str
    label_en: str
    purpose: str = ""
    decision_supported: str = ""

    trigger_type: TriggerType = "stage"
    #: Stage name in the crop's ``stages`` table.  Required for trigger_type="stage".
    trigger_stage: str | None = None
    #: Fire this many days after the trigger stage is crossed.
    offset_days: int = 0
    window_before_days: int = Field(default=3, ge=0)
    window_after_days: int = Field(default=7, ge=0)
    #: Window force-closes when this stage is reached, whatever the dates say.
    hard_deadline_stage: str | None = None

    required_gsd_cm: float = Field(gt=0)
    sensor: Sensor = "rgb"
    overlap_front: float = Field(default=0.80, gt=0, lt=1)
    overlap_side: float = Field(default=0.70, gt=0, lt=1)

    priority: Priority = "normal"
    weather_sensitivity: Sensitivity = "normal"
    satellite_coincidence: Coincidence = "ignored"
    #: A researcher's calibration flight and a farmer's decision flight have
    #: different acceptable miss rates (spec §14.3).  One plan, tagged.
    audience: Audience = "both"
    repeat: RepeatSpec | None = None

    #: Gust threshold (m/s) in the trailing window, for trigger_type="weather_event".
    event_gust_ms: float | None = Field(default=None, gt=0)
    event_lookback_hours: int = Field(default=72, gt=0)

    notes_fi: str = ""
    notes_en: str = ""

    #: True only for campaigns whose *purpose* relates to plant protection.
    #: Drone application of PPPs is prohibited in Finland (spec §1), so these
    #: still produce a map for a **ground sprayer** — never a spray route.
    #: Surfaces must say so; see :meth:`ground_sprayer_notice`.
    drone_spray_related: bool = False

    @model_validator(mode="after")
    def _check_trigger(self) -> "CampaignType":
        # A cadence campaign still has to start somewhere, so it needs an anchor
        # stage just as much as a one-shot stage campaign does.
        if self.trigger_type in ("stage", "cadence") and not self.trigger_stage:
            raise ValueError(
                f"campaign {self.id!r}: trigger_type={self.trigger_type!r} needs trigger_stage"
            )
        if self.trigger_type == "weather_event" and self.event_gust_ms is None:
            raise ValueError(
                f"campaign {self.id!r}: trigger_type='weather_event' needs event_gust_ms"
            )
        if self.trigger_type == "cadence" and self.repeat is None:
            raise ValueError(
                f"campaign {self.id!r}: trigger_type='cadence' needs a [repeat] block"
            )
        return self

    def ground_sprayer_notice(self) -> str | None:
        """The mandatory disclaimer for plant-protection-related campaigns.

        Aerial application of plant protection products is prohibited across the
        EU under Article 9 of Directive 2009/128/EC, and Tukes does not permit
        drone sprayers for plant protection in Finland.  This module plans
        **imaging only**; the output of such a campaign is a prescription map a
        ground sprayer consumes.
        """
        if not self.drone_spray_related:
            return None
        return (
            "Tuottaa kartan MAALEVITTIMELLE — ei lennokkiruiskutusreittiä. "
            "Kasvinsuojeluaineiden lentolevitys on kielletty (dir. 2009/128/EY 9 art.). "
            "| Produces a map for a GROUND SPRAYER, never a spray route. "
            "Aerial application of plant protection products is prohibited."
        )


# ---------------------------------------------------------------------------
# Window (spec §4.3, derived)
# ---------------------------------------------------------------------------

#: What the window's dates rest on, worst basis wins for the window as a whole.
WindowBasis = Literal["observed", "forecast", "normal", "manual"]


class Window(BaseModel):
    """``[earliest, target, latest]`` plus an honest error bar.

    ``uncertainty_days`` is the half-width to show around ``target``.  It is
    **not** the same thing as ``latest - target``: the window width is
    agronomy (how long the campaign stays useful), the uncertainty is
    ignorance (how well we know when the stage arrives).  Surfaces must render
    both, which is why they are separate fields.
    """

    model_config = ConfigDict(extra="forbid")

    computed_at: str
    earliest: _dt.date
    target: _dt.date
    latest: _dt.date
    basis: WindowBasis = "normal"
    uncertainty_days: float = 0.0
    #: Combined confidence: the weaker of the crop table's and the basis'.
    confidence: Confidence = "low"
    #: Stage that produced ``target``, for display and for calibration.
    trigger_stage: str | None = None
    #: Accumulated °C·d at ``target`` — the x-axis of the calibration fit.
    gdd_at_target: float | None = None
    #: Set when ``hard_deadline_stage`` pulled ``latest`` in.
    deadline_stage: str | None = None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_order(self) -> "Window":
        if not (self.earliest <= self.target <= self.latest):
            raise ValueError(
                f"window out of order: {self.earliest} / {self.target} / {self.latest}"
            )
        return self

    def contains(self, day: _dt.date) -> bool:
        return self.earliest <= day <= self.latest

    def days_from_target(self, day: _dt.date) -> int:
        return (day - self.target).days

    def uncertainty_label(self) -> str:
        """Short human string, e.g. ``"±7 d, uncalibrated"``."""
        band = f"±{self.uncertainty_days:.0f} d"
        if self.confidence == "low":
            return f"{band}, uncalibrated"
        return f"{band}, {self.confidence} confidence"


# ---------------------------------------------------------------------------
# Campaign (spec §4.3, persisted)
# ---------------------------------------------------------------------------


class CampaignOutcome(BaseModel):
    """What actually happened — the input the calibration loop fits on."""

    model_config = ConfigDict(extra="allow")

    flown_date: _dt.date | None = None
    #: Observed BBCH stage at the time of the flight, recorded by the operator.
    bbch: int | None = Field(default=None, ge=0, le=99)
    #: Accumulated °C·d at ``flown_date``, stamped when the log entry is written
    #: so the fit does not have to re-derive a past season's weather.
    gdd_at_flight: float | None = None
    #: What the flight produced downstream (ortho, index map, …).  Recorded, not
    #: produced — image processing is explicitly out of scope (spec §1).
    products: list[str] = Field(default_factory=list)
    note: str = ""
    observation_id: str | None = None


class Campaign(BaseModel):
    """One CampaignType instantiated for a job (or folder) in one season."""

    model_config = ConfigDict(extra="allow")

    campaign_id: str
    type_id: str
    season: int
    scope: Literal["job", "folder"] = "job"
    job_paths: list[str] = Field(default_factory=list)

    crop_id: str | None = None
    sowing_date: _dt.date | None = None
    sowing_date_source: str | None = None

    state: CampaignState = "planned"
    window: Window | None = None
    #: An operator-pinned window.  Survives ``season plan`` untouched: the
    #: agronomist on the ground beats the model (spec §6.4).
    manual_window: Window | None = None

    flown: _dt.date | None = None
    flight_job_name: str | None = None
    outcome: CampaignOutcome | None = None

    #: Repeat index for a repeatable type — 0 for the first instance.
    repeat_index: int = 0
    #: Machine-readable problems: ``sowing_date_required``, ``gsd_unreachable``,
    #: ``low_altitude_workload``, ``no_stage_threshold``, ``trigger_not_reached``.
    flags: list[str] = Field(default_factory=list)
    #: Human-readable lines explaining the flags, in the operator's language.
    reasons: list[str] = Field(default_factory=list)
    notes: str = ""

    def effective_window(self) -> Window | None:
        """The window surfaces should use: a manual override wins outright."""
        return self.manual_window or self.window

    def is_terminal(self) -> bool:
        return self.state in ("flown", "cancelled")

    def set_state(self, new: CampaignState) -> None:
        """Move to *new*, raising :class:`InvalidTransition` if illegal."""
        check_transition(self.state, new)
        self.state = new


# ---------------------------------------------------------------------------
# Season plan (spec §4.4, persisted)
# ---------------------------------------------------------------------------


class JobAssignment(BaseModel):
    """Crop and sowing information for one job in the folder.

    Kept here rather than in the job's own ``job_params.json``: the season
    module stores no new geometry and does not mutate existing job schemas
    (spec §2).  Jobs are referenced by path only.
    """

    model_config = ConfigDict(extra="allow")

    job_path: str
    crop_id: str | None = None
    sowing_date: _dt.date | None = None
    #: ``farmer_reported`` | ``fms_export`` | ``estimated`` | free text.
    sowing_date_source: str | None = None
    #: For catch crops and autumn-sown cereals, thermal time accumulates from
    #: establishment, not from the main crop's sowing (spec §6.1).
    accumulation_start: _dt.date | None = None
    notes: str = ""

    def start_date(self) -> _dt.date | None:
        return self.accumulation_start or self.sowing_date


class RecomputeEntry(BaseModel):
    """One line of the plan's audit trail."""

    model_config = ConfigDict(extra="allow")

    at: str
    action: str
    detail: str = ""
    campaigns_changed: int = 0
    weather_basis: str = ""


class SeasonPlan(BaseModel):
    """All campaigns for one folder in one season.

    Written atomically to ``<output_root>/<folder>/season_<year>.json``.  Meant
    to be readable and diffable by hand, so dates are plain ISO strings on disk
    and the campaign list keeps a stable order.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = SCHEMA_VERSION
    season: int
    folder: str
    created_at: str
    updated_at: str
    #: Grid cell used for thermal time — one per folder by default (spec §6.2).
    centroid_lat: float | None = None
    centroid_lon: float | None = None
    assignments: list[JobAssignment] = Field(default_factory=list)
    campaigns: list[Campaign] = Field(default_factory=list)
    recompute_log: list[RecomputeEntry] = Field(default_factory=list)

    def assignment_for(self, job_path: str) -> JobAssignment | None:
        return next((a for a in self.assignments if a.job_path == job_path), None)

    def campaign(self, campaign_id: str) -> Campaign | None:
        return next((c for c in self.campaigns if c.campaign_id == campaign_id), None)

    def log(self, action: str, **kwargs) -> None:
        self.recompute_log.append(
            RecomputeEntry(at=_now_iso(), action=action, **kwargs)
        )
        # The log is an audit trail, not an archive — keep the plan file small
        # enough to stay hand-readable.
        if len(self.recompute_log) > 200:
            del self.recompute_log[:-200]


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")


def make_campaign_id(
    season: int, type_id: str, scope_key: str, repeat_index: int = 0
) -> str:
    """Stable id, e.g. ``2027-emergence_count-5241087453`` (spec §4.3).

    Stability matters: ``season plan`` is idempotent, so re-planning must land
    on the same ids or every run would orphan the previous campaigns and lose
    their logged outcomes.
    """
    base = f"{season}-{type_id}-{scope_key}"
    return base if repeat_index == 0 else f"{base}-r{repeat_index}"
