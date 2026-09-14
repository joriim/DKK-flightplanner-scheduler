"""Season configuration: the ``[season]`` table plus the crop and campaign libraries.

Everything agronomic is config, not a code constant (spec §2/§9).  The built-in
crop and campaign tables live in ``season_defaults.toml`` next to this file, the
same way drone profiles live in ``drones.toml`` — and they follow the same
replace-not-merge rule:

    **If your config.toml defines any [[crops]] entry, it replaces the entire
    built-in crop list.  Same for [[campaigns]].**

That is the ``[[drones]]`` precedent.  It is surprising the first time, so
:func:`load_season_tables` returns a :class:`TableSource` saying which list was
used and how many entries came from where; every surface prints it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flightmanager.season.models import CampaignType, CropProfile

_DEFAULTS_FILE = Path(__file__).with_name("season_defaults.toml")


class SeasonWeights(BaseModel):
    """Opportunity-scoring weights (spec §9).

    Phase 1 computes no scores, but the weights are configuration and belong in
    the schema now: shipping them later would mean a second settings migration
    for users who have already hand-edited their config.
    """

    model_config = ConfigDict(extra="forbid")

    days_from_target: float = Field(default=0.25, ge=0)
    wind: float = Field(default=0.20, ge=0)
    cloud: float = Field(default=0.20, ge=0)
    sun_elevation: float = Field(default=0.15, ge=0)
    precip: float = Field(default=0.10, ge=0)
    satellite_coincidence: float = Field(default=0.10, ge=0)

    @model_validator(mode="after")
    def _check_nonzero(self) -> "SeasonWeights":
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("season weights must not all be zero")
        return self


class SeasonConfig(BaseModel):
    """The ``[season]`` section of ``config.toml``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    default_crop: str = "spring_barley"

    # ── Phase 2 scheduling knobs (carried now, consumed later) ──────────────
    max_field_day_hours: float = Field(default=6.0, gt=0)
    min_solar_elevation_deg: float = Field(default=30.0, ge=0, le=90)
    min_solar_elevation_rgb_deg: float = Field(default=20.0, ge=0, le=90)
    precip_threshold_mm: float = Field(default=0.5, ge=0)
    weights: SeasonWeights = Field(default_factory=SeasonWeights)

    # ── Phenology / uncertainty ────────────────────────────────────────────
    #: Years of archive averaged into the daily climatological normal.
    projection_normal_years: int = Field(default=30, gt=0, le=60)
    #: The window band widens by this many days for every week the projection
    #: runs past the forecast horizon.  Do not present a single date six weeks
    #: out as if it were known (spec §6.1).
    window_uncertainty_days_per_week_projected: float = Field(default=1.5, ge=0)
    #: Floor on the band even inside the forecast horizon.  The stage thresholds
    #: themselves are uncertain, so a window is never truly ±0 until the stage
    #: has actually been observed.
    base_uncertainty_days: float = Field(default=2.0, ge=0)
    #: Extra band applied while the crop table is still uncalibrated.  This is
    #: what produces the "±7 d, uncalibrated" line in the UI.
    uncalibrated_extra_days: float = Field(default=5.0, ge=0)

    # ── Weather history (spec §6.2) ────────────────────────────────────────
    #: Open-Meteo historical archive endpoint.  Separate from
    #: ``weather.open_meteo_url`` (forecast) because they are different hosts.
    archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
    #: Past weather for a point is immutable once the day is over, so history
    #: gets a long TTL of its own rather than the 3 h forecast TTL.
    history_ttl_days: int = Field(default=120, gt=0)
    normals_ttl_days: int = Field(default=365, gt=0)
    #: The archive lags real time by a few days; days newer than this fall back
    #: to the forecast series.
    archive_lag_days: int = Field(default=6, ge=0, le=14)
    #: How far the forecast is treated as real information.  Open-Meteo serves
    #: 16 days; beyond ``forecast_horizon_days`` the normal takes over and the
    #: uncertainty band starts widening.
    forecast_horizon_days: int = Field(default=14, gt=0, le=16)
    #: Folders wider than this get a warning that one grid cell is too coarse
    #: and per-job cells should be used instead (spec §6.2).
    max_folder_span_km: float = Field(default=30.0, gt=0)
    timeout_s: int = Field(default=30, gt=0)
    #: Serve from cache only; report staleness instead of failing.
    offline: bool = False

    # ── Flight-parameter derivation (spec §5) ──────────────────────────────
    #: Below this AGL a campaign is flagged ``low_altitude_workload``: shorter
    #: battery per hectare, more strips, more obstacle exposure.
    low_altitude_warn_m: float = Field(default=25.0, gt=0)
    min_altitude_m: float = Field(default=10.0, gt=0)


@dataclass
class TableSource:
    """Where the crop and campaign lists came from, for honest reporting."""

    crops_from: str = "built-in"
    campaigns_from: str = "built-in"
    crop_count: int = 0
    campaign_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.crop_count} crop profile(s) from {self.crops_from}, "
            f"{self.campaign_count} campaign type(s) from {self.campaigns_from}"
        )


@dataclass
class SeasonTables:
    """The loaded agronomy libraries, keyed by id."""

    crops: dict[str, CropProfile]
    campaigns: dict[str, CampaignType]
    source: TableSource

    def crop(self, crop_id: str) -> CropProfile | None:
        return self.crops.get(crop_id)

    def campaign_type(self, type_id: str) -> CampaignType | None:
        return self.campaigns.get(type_id)

    def for_audience(self, audience: str | None) -> list[CampaignType]:
        """Campaign types matching *audience*; ``None`` means every one."""
        if audience in (None, "", "both"):
            return list(self.campaigns.values())
        return [
            c
            for c in self.campaigns.values()
            if c.audience == audience or c.audience == "both"
        ]

    @property
    def uncalibrated_crops(self) -> list[str]:
        return sorted(cid for cid, c in self.crops.items() if not c.is_calibrated)


def load_defaults_raw() -> dict[str, Any]:
    """Parse the bundled ``season_defaults.toml``."""
    with open(_DEFAULTS_FILE, "rb") as fh:
        return tomllib.load(fh)


def load_season_tables(raw_config: dict[str, Any] | None = None) -> SeasonTables:
    """Build the crop and campaign libraries from a parsed ``config.toml`` dict.

    *raw_config* is the whole parsed TOML document, not just the ``[season]``
    table, because ``[[crops]]`` and ``[[campaigns]]`` sit at the top level
    alongside ``[[drones]]``.  Pass ``None`` to get the built-ins alone.
    """
    raw_config = raw_config or {}
    defaults = load_defaults_raw()
    source = TableSource()

    crop_entries = raw_config.get("crops")
    if crop_entries:
        source.crops_from = "config.toml"
        source.warnings.append(
            f"config.toml defines {len(crop_entries)} [[crops]] entr(y/ies); "
            "the built-in crop list is REPLACED, not merged "
            "(same rule as [[drones]])."
        )
    else:
        crop_entries = defaults.get("crops", [])

    campaign_entries = raw_config.get("campaigns")
    if campaign_entries:
        source.campaigns_from = "config.toml"
        source.warnings.append(
            f"config.toml defines {len(campaign_entries)} [[campaigns]] entr(y/ies); "
            "the built-in campaign library is REPLACED, not merged "
            "(same rule as [[drones]])."
        )
    else:
        campaign_entries = defaults.get("campaigns", [])

    crops = _index(CropProfile, crop_entries, "crop")
    campaigns = _index(CampaignType, campaign_entries, "campaign")
    source.crop_count = len(crops)
    source.campaign_count = len(campaigns)

    source.warnings.extend(_cross_check(crops, campaigns))
    return SeasonTables(crops=crops, campaigns=campaigns, source=source)


def _index(model_cls: type, entries: list[dict], label: str) -> dict[str, Any]:
    """Validate *entries* into models keyed by id, rejecting duplicate ids."""
    out: dict[str, Any] = {}
    for entry in entries:
        obj = model_cls.model_validate(entry)
        if obj.id in out:
            raise ValueError(f"duplicate {label} id {obj.id!r}")
        out[obj.id] = obj
    return out


def _cross_check(
    crops: dict[str, CropProfile], campaigns: dict[str, CampaignType]
) -> list[str]:
    """Warn about campaign triggers no crop can ever satisfy.

    This is a warning rather than an error on purpose: a campaign library may
    legitimately carry a catch-crop campaign whose trigger stage exists only in
    the catch-crop profile.  It becomes an error per campaign, at planning time,
    once a specific crop is assigned to a specific job.
    """
    warnings: list[str] = []
    known_stages = {s for c in crops.values() for s in c.stages}
    for camp in campaigns.values():
        for stage in (camp.trigger_stage, camp.hard_deadline_stage):
            if stage and stage not in known_stages:
                warnings.append(
                    f"campaign {camp.id!r} references stage {stage!r}, "
                    "which no configured crop defines"
                )
    return warnings


def load_season_config(raw_config: dict[str, Any] | None = None) -> SeasonConfig:
    """Build a :class:`SeasonConfig` from a parsed ``config.toml`` dict."""
    return SeasonConfig.model_validate((raw_config or {}).get("season", {}))


def load_from_file(path: Path | str) -> tuple[SeasonConfig, SeasonTables]:
    """Read ``config.toml`` (or ``config.example.toml``) and build both halves."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p}")
    with open(p, "rb") as fh:
        raw = tomllib.load(fh)
    return load_season_config(raw), load_season_tables(raw)
