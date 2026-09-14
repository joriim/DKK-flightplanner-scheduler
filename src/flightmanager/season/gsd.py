"""Campaign GSD requirement → flight altitude and strip geometry (spec §5).

No camera maths is duplicated here.  The host's drone profile already carries
both directions of the GSD ↔ altitude relation:

    ``DroneConfig.height_from_gsd(gsd_cm)``   metres AGL for a required GSD
    ``DroneConfig.gsd_from_height(height_m)`` GSD achieved at an altitude

which is exactly the ``altitude_for_gsd`` the spec asks to add — it already
exists under a different name, so nothing is added to that class.  This module
only applies the resolution *policy*: clamp, detect the two failure modes, and
report them as flags rather than swallowing them.

The two failure modes are asymmetric, and conflating them is the easy mistake:

``gsd_unreachable``
    The altitude floor forces a **coarser** GSD than the campaign requires.
    The campaign cannot be flown as specified with this drone.
``low_altitude_workload``
    The altitude is legal but low: more strips, shorter battery per hectare,
    more obstacle exposure.  Flyable, but the operator should see the cost.

Clamping *down* to the ceiling is neither: a coarse requirement met from a
lower altitude simply yields finer imagery than asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from flightmanager.season.config import SeasonConfig
from flightmanager.season.models import CampaignType

#: EU open-category hard ceiling (AGL).  Mirrors the host's
#: ``EU_OPEN_CATEGORY_MAX_AGL_M``; kept as a local constant so this module has
#: no import-time dependency on the host package.
EU_OPEN_CATEGORY_MAX_AGL_M = 120.0

#: Relative tolerance when comparing achieved against required GSD, so floating
#: point alone never raises ``gsd_unreachable``.
_GSD_TOLERANCE = 1e-6


@runtime_checkable
class DroneLike(Protocol):
    """The slice of the host's ``DroneConfig`` this module needs."""

    name: str
    label: str
    battery_minutes: float

    def height_from_gsd(self, gsd_cm: float) -> float: ...

    def gsd_from_height(self, height_m: float) -> float: ...

    def auto_speed(self, altitude_m: float, overlap_front_pct: int) -> float: ...

    @property
    def sensor_w_mm(self) -> float: ...

    @property
    def sensor_h_mm(self) -> float: ...


@dataclass
class GsdResolution:
    """What a campaign's GSD requirement works out to on a given drone."""

    drone_name: str
    required_gsd_cm: float
    #: Altitude actually planned, after clamping.
    altitude_m: float
    #: Unclamped altitude the requirement implies.
    ideal_altitude_m: float
    #: GSD the clamped altitude actually delivers.
    achieved_gsd_cm: float
    ceiling_m: float
    floor_m: float
    overlap_front_pct: int
    overlap_side_pct: int
    speed_ms: float | None = None
    swath_width_m: float | None = None
    line_spacing_m: float | None = None
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the campaign can be flown as specified."""
        return "gsd_unreachable" not in self.flags

    @property
    def clamped(self) -> bool:
        return abs(self.altitude_m - self.ideal_altitude_m) > 1e-6


def resolve(
    campaign_type: CampaignType,
    drone: DroneLike,
    cfg: SeasonConfig,
    *,
    max_height_agl_m: float = 110.0,
    zone_cap_m: float | None = None,
) -> GsdResolution:
    """Resolve one campaign's acquisition parameters against one drone.

    *max_height_agl_m* is the host's configured ceiling
    (``flight.max_height_agl_m``) and *zone_cap_m* an altitude cap imposed by an
    intersecting UAS zone, when the caller knows of one.  Both are clamped
    against the EU open-category limit, and the tightest wins.
    """
    ceiling = min(
        float(max_height_agl_m),
        EU_OPEN_CATEGORY_MAX_AGL_M,
        float(zone_cap_m) if zone_cap_m is not None else EU_OPEN_CATEGORY_MAX_AGL_M,
    )
    floor = float(cfg.min_altitude_m)

    ideal = drone.height_from_gsd(campaign_type.required_gsd_cm)
    altitude = max(floor, min(ideal, ceiling))
    achieved = drone.gsd_from_height(altitude)

    front_pct = int(round(campaign_type.overlap_front * 100))
    side_pct = int(round(campaign_type.overlap_side * 100))

    res = GsdResolution(
        drone_name=drone.name,
        required_gsd_cm=campaign_type.required_gsd_cm,
        altitude_m=round(altitude, 1),
        ideal_altitude_m=round(ideal, 1),
        achieved_gsd_cm=round(achieved, 3),
        ceiling_m=ceiling,
        floor_m=floor,
        overlap_front_pct=front_pct,
        overlap_side_pct=side_pct,
    )

    if achieved > campaign_type.required_gsd_cm * (1 + _GSD_TOLERANCE):
        res.flags.append("gsd_unreachable")
        res.reasons.append(
            f"{drone.label if hasattr(drone, 'label') else drone.name} cannot reach "
            f"{campaign_type.required_gsd_cm:.2f} cm/px: the {floor:.0f} m altitude "
            f"floor gives {achieved:.2f} cm/px. Pick a drone profile with a longer "
            f"focal length or finer pixel pitch, or drop this campaign — do not "
            f"fly it and call the result a {campaign_type.required_gsd_cm:.2f} cm map."
        )
    elif ideal > ceiling:
        res.reasons.append(
            f"Requirement met from below: clamped from {ideal:.0f} m to the "
            f"{ceiling:.0f} m ceiling, giving {achieved:.2f} cm/px "
            f"(finer than the {campaign_type.required_gsd_cm:.2f} cm asked for)."
        )

    if altitude < cfg.low_altitude_warn_m:
        res.flags.append("low_altitude_workload")
        res.reasons.append(
            f"{altitude:.0f} m AGL is low: strips are narrow, so expect more "
            f"lines, shorter coverage per battery and more obstacle exposure. "
            f"Consider sampling sub-areas rather than the whole parcel."
        )

    _fill_geometry(res, drone, campaign_type, altitude)
    return res


def _fill_geometry(
    res: GsdResolution,
    drone: DroneLike,
    campaign_type: CampaignType,
    altitude_m: float,
) -> None:
    """Strip speed, swath and line spacing — the workload numbers.

    Flight time and battery count come from the host's route estimator once a
    parcel geometry is attached (Phase 3).  Swath and line spacing need only
    the camera, so they are derived here and are what actually explain a
    ``low_altitude_workload`` flag to an operator.
    """
    try:
        res.speed_ms = round(drone.auto_speed(altitude_m, res.overlap_front_pct), 2)
    except Exception:  # a profile without auto_speed is still usable
        res.speed_ms = None
    try:
        swath = altitude_m * drone.sensor_w_mm / _focal_mm(drone)
        res.swath_width_m = round(swath, 1)
        res.line_spacing_m = round(swath * (1 - campaign_type.overlap_side), 1)
    except Exception:
        pass


def _focal_mm(drone: DroneLike) -> float:
    focal = getattr(drone, "focal_length_mm", None)
    if not focal:
        raise AttributeError("drone profile exposes no focal_length_mm")
    return float(focal)


def suggest_profiles(
    campaign_type: CampaignType,
    drones: list[DroneLike],
    cfg: SeasonConfig,
    *,
    max_height_agl_m: float = 110.0,
) -> list[str]:
    """Names of the configured profiles that *can* meet this campaign's GSD.

    Fed to the "pick a different drone" half of the ``gsd_unreachable``
    message; the host's ``flightmanager drones`` already prints GSD at altitude
    for each of them.
    """
    ok: list[str] = []
    for drone in drones:
        res = resolve(campaign_type, drone, cfg, max_height_agl_m=max_height_agl_m)
        if res.ok:
            ok.append(drone.name)
    return ok
