"""Cross-parcel batching into a field day (spec §7.5).

Turns "Thursday scores well for these nine campaigns" into "fly Thursday, these
seven parcels, in this order, two batteries — and here is what did not fit and
why".

The ordering rule is deliberate: **an operator's own route order wins.** If the
folder's jobs already carry ``sort_order`` — someone dragged them into sequence
in the UI, knowing where the gates and the soft ground are — that is the route.
Greedy nearest-neighbour is only the fallback for an unrouted folder, and it is
the browser UI's own algorithm (ported in
:func:`flightmanager.season.scheduling.greedy_route`) so the two never disagree.

Overflow is reported, never silently trimmed: a day plan that quietly dropped
three parcels would be worse than one that says it is two hours over.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any

from flightmanager.season.config import SeasonConfig
from flightmanager.season.models import CampaignScore, Opportunity
from flightmanager.season.scheduling import greedy_route

log = logging.getLogger(__name__)

#: Priority order when a field day overflows. High-priority campaigns go first
#: (spec §7.5), then whichever window is closing soonest.
_PRIORITY_RANK = {"high": 0, "normal": 1, "opportunistic": 2}

#: Assumed batteries for a job whose manifest carries no estimate. One is the
#: honest floor — it is a lower bound, and the plan says so rather than
#: inventing a number.
_UNKNOWN_BATTERIES = 1


@dataclass
class PlannedJob:
    """One parcel's slot in the day, with the campaigns it serves."""

    job_path: str
    name: str
    route_index: int
    campaigns: list[CampaignScore] = field(default_factory=list)
    flight_time_min: float | None = None
    battery_count: int | None = None
    flight_ready: bool | None = None
    takeoff_4326: list[float] | None = None
    priority: str = "normal"
    days_to_close: int | None = None

    @property
    def best_score(self) -> float:
        return max((c.score for c in self.campaigns), default=0.0)

    @property
    def campaign_labels(self) -> list[str]:
        return [c.label_en for c in self.campaigns]


@dataclass
class FieldDayPlan:
    """A day's worth of flying: what, in what order, and whether it fits."""

    date: _dt.date
    folder: str
    jobs: list[PlannedJob] = field(default_factory=list)
    deferred: list[PlannedJob] = field(default_factory=list)
    launch_sites: list[dict[str, Any]] = field(default_factory=list)
    total_flight_time_min: float = 0.0
    total_battery_count: int = 0
    max_field_day_hours: float = 6.0
    best_hours: list[str] = field(default_factory=list)
    order_source: str = "sort_order"
    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    #: Jobs whose flight time the manifests do not carry.
    unknown_time_jobs: list[str] = field(default_factory=list)

    @property
    def total_flight_time_h(self) -> float:
        return round(self.total_flight_time_min / 60.0, 2)

    @property
    def overflows(self) -> bool:
        return bool(self.deferred)

    @property
    def job_paths(self) -> list[str]:
        return [j.job_path for j in self.jobs]

    def campaign_ids(self) -> list[str]:
        return [c.campaign_id for j in self.jobs for c in j.campaigns]


def _card_index(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["path"]: c for c in cards if c.get("path")}


def plan_field_day(
    opportunity: Opportunity,
    cards: list[dict[str, Any]],
    cfg: SeasonConfig,
    folder: str,
    *,
    cluster: Any = None,
    max_hours: float | None = None,
) -> FieldDayPlan:
    """Build the field-day plan for one scored day.

    *cards* are the host's job cards for the folder; *cluster* is the host's
    launch-site clustering (injected so the engine stays testable without it).
    """
    budget = max_hours if max_hours is not None else cfg.max_field_day_hours
    plan = FieldDayPlan(
        date=opportunity.date,
        folder=folder,
        max_field_day_hours=budget,
        best_hours=list(opportunity.best_hours),
    )
    by_path = _card_index(cards)
    jobs = _collect_jobs(opportunity, by_path, plan)
    if not jobs:
        plan.warnings.append("no campaign on this day resolves to a job with geometry")
        return plan

    ordered = _order_jobs(jobs, by_path, plan)
    plan.jobs, plan.deferred = _apply_budget(ordered, budget, plan)
    _summarise(plan)
    _attach_launch_sites(plan, by_path, cluster)
    return plan


def _collect_jobs(
    opportunity: Opportunity,
    by_path: dict[str, dict[str, Any]],
    plan: FieldDayPlan,
) -> list[PlannedJob]:
    """One PlannedJob per parcel, carrying every campaign it serves that day.

    A parcel flown once can satisfy several campaigns at the same time only
    when they want the same imagery; they usually do not (different GSD,
    different sensor). They are grouped per parcel regardless, because the
    *drive* is shared even when the flight is not — and the operator decides.
    """
    grouped: dict[str, list[CampaignScore]] = {}
    for score in opportunity.servable():
        if score.job_path:
            grouped.setdefault(score.job_path, []).append(score)

    jobs: list[PlannedJob] = []
    for path, scores in grouped.items():
        card = by_path.get(path)
        if card is None:
            plan.warnings.append(f"{path}: no job card found; left out of the day")
            continue
        jobs.append(
            PlannedJob(
                job_path=path,
                name=card.get("name") or path,
                route_index=0,
                campaigns=sorted(scores, key=lambda s: s.score, reverse=True),
                flight_time_min=card.get("flight_time_min"),
                battery_count=card.get("battery_count"),
                flight_ready=card.get("flight_ready"),
                takeoff_4326=card.get("takeoff_point_4326"),
                # The most urgent campaign on the parcel sets the parcel's own
                # priority: a high-priority emergence count drags the whole
                # drive up the list even when it shares the day with a routine
                # canopy flight.
                priority=min(
                    (s.priority for s in scores),
                    key=lambda p: _PRIORITY_RANK.get(p, 1),
                ),
                days_to_close=min(
                    (s.days_to_close for s in scores if s.days_to_close is not None),
                    default=None,
                ),
            )
        )
    return jobs


def _order_jobs(
    jobs: list[PlannedJob],
    by_path: dict[str, dict[str, Any]],
    plan: FieldDayPlan,
) -> list[PlannedJob]:
    """Route order: the operator's own sequence, else greedy nearest-neighbour."""
    sort_orders = {
        j.job_path: by_path.get(j.job_path, {}).get("sort_order") for j in jobs
    }
    if all(v is not None for v in sort_orders.values()):
        plan.order_source = "sort_order"
        plan.notices.append(
            "route order taken from the folder's existing flight sequence"
        )
        ordered = sorted(jobs, key=lambda j: sort_orders[j.job_path])
    else:
        ordered = _greedy_order(jobs, by_path, plan)

    for index, job in enumerate(ordered, start=1):
        job.route_index = index
    return ordered


def _greedy_order(
    jobs: list[PlannedJob],
    by_path: dict[str, dict[str, Any]],
    plan: FieldDayPlan,
) -> list[PlannedJob]:
    """Fall back to the UI's greedy nearest-neighbour over takeoff points."""
    points: list[tuple[str, float, float]] = []
    for job in jobs:
        position = job.takeoff_4326 or _card_centroid(by_path.get(job.job_path))
        if position:
            points.append((job.job_path, position[1], position[0]))

    if len(points) < len(jobs):
        plan.warnings.append(
            "some jobs have no takeoff point; route order falls back to job name"
        )
    if not points:
        plan.order_source = "name"
        return sorted(jobs, key=lambda j: j.job_path)

    plan.order_source = "greedy_nearest_neighbour"
    plan.notices.append(
        "folder has no saved flight order; route computed by greedy "
        "nearest-neighbour (the same algorithm the map view uses)"
    )
    sequence = greedy_route(points)
    rank = {path: i for i, path in enumerate(sequence)}
    return sorted(jobs, key=lambda j: rank.get(j.job_path, len(rank)))


def _card_centroid(card: dict[str, Any] | None) -> list[float] | None:
    """[lon, lat] of a card's survey polygon, when it has no takeoff point."""
    if not card:
        return None
    geometry = card.get("_geometry") or card.get("geometry")
    if not geometry:
        return None
    try:
        from shapely.geometry import shape

        point = shape(geometry).centroid
        return [point.x, point.y]
    except Exception:
        return None


def _apply_budget(
    ordered: list[PlannedJob], budget_h: float, plan: FieldDayPlan
) -> tuple[list[PlannedJob], list[PlannedJob]]:
    """Fit what the day can hold; defer the rest, in the spec's priority order.

    Overflow is split by campaign priority first, then by how soon the window
    closes — a parcel whose window shuts tomorrow outranks one with a fortnight
    left, because the second can be flown another day and the first cannot.
    """
    budget_min = budget_h * 60.0
    total = sum(j.flight_time_min or 0.0 for j in ordered)
    if total <= budget_min:
        return (ordered, [])

    by_urgency = sorted(ordered, key=_urgency_key)
    keep: set[str] = set()
    running = 0.0
    for job in by_urgency:
        cost = job.flight_time_min or 0.0
        if running + cost <= budget_min:
            keep.add(job.job_path)
            running += cost

    kept = [j for j in ordered if j.job_path in keep]
    dropped = [j for j in ordered if j.job_path not in keep]
    plan.warnings.append(
        f"the day needs {total / 60:.1f} h of flying against a "
        f"{budget_h:.1f} h budget — {len(dropped)} parcel(s) deferred by "
        f"campaign priority, then by how soon the window closes"
    )
    _warn_about_passed_over_urgency(kept, dropped, plan)
    # Renumber the kept route so the printed sequence is 1..n with no gaps.
    for index, job in enumerate(kept, start=1):
        job.route_index = index
    return (kept, dropped)


def _warn_about_passed_over_urgency(
    kept: list[PlannedJob], dropped: list[PlannedJob], plan: FieldDayPlan
) -> None:
    """Flag a deferred parcel that is more urgent than one being flown.

    The budget is filled first-fit in urgency order, so a large, urgent parcel
    can fail to fit and then be packed around by smaller, less urgent ones. That
    gets more flying done, which is usually right — but it must never happen
    silently, because the parcel passed over is precisely the one that may not
    get another chance. The operator can then split it, extend the day, or
    accept the miss; what they cannot do is notice it on their own.
    """
    for out in dropped:
        more_urgent_than = [k for k in kept if _urgency_key(out) < _urgency_key(k)]
        if not more_urgent_than:
            continue
        names = ", ".join(k.name for k in more_urgent_than)
        closing = (
            f"closes in {out.days_to_close} d"
            if out.days_to_close is not None
            else "priority " + out.priority
        )
        plan.warnings.append(
            f"{out.name} ({closing}) was deferred although it outranks {names}: "
            f"it needs "
            f"{out.flight_time_min:.0f} min and did not fit the remaining budget. "
            f"Consider raising --max-hours, splitting it, or flying it first."
        )


def _urgency_key(job: PlannedJob) -> tuple[int, int, float]:
    return (
        _PRIORITY_RANK.get(job.priority, 1),
        job.days_to_close if job.days_to_close is not None else 999,
        -job.best_score,
    )


def _summarise(plan: FieldDayPlan) -> None:
    """Totals, and an honest note about what the manifests could not tell us."""
    total_min = 0.0
    batteries = 0
    for job in plan.jobs:
        if job.flight_time_min is None:
            plan.unknown_time_jobs.append(job.job_path)
        else:
            total_min += job.flight_time_min
        batteries += (
            job.battery_count if job.battery_count is not None else _UNKNOWN_BATTERIES
        )
    plan.total_flight_time_min = round(total_min, 1)
    plan.total_battery_count = batteries

    if plan.unknown_time_jobs:
        plan.warnings.append(
            f"{len(plan.unknown_time_jobs)} parcel(s) have no flight-time estimate "
            f"(never exported); the day total is a lower bound"
        )
    not_ready = [j.job_path for j in plan.jobs if j.flight_ready is False]
    if not_ready:
        plan.warnings.append(
            f"{len(not_ready)} parcel(s) are not flight-ready and would need a "
            f"re-export before the field day"
        )


def _attach_launch_sites(
    plan: FieldDayPlan, by_path: dict[str, dict[str, Any]], cluster: Any
) -> None:
    """Group the day's parcels into parking spots via the host's clustering.

    Passes cards carrying *this day's* route order, not the folder's, so a
    partial field day clusters on the sequence actually being flown.
    """
    if cluster is None:
        return
    cards: list[dict[str, Any]] = []
    for job in plan.jobs:
        card = dict(by_path.get(job.job_path) or {})
        if not card:
            continue
        card["sort_order"] = job.route_index - 1
        cards.append(card)
    if not cards:
        return
    try:
        sites = cluster(cards)
    except Exception as exc:  # clustering is a convenience, not the deliverable
        log.warning("Launch-site clustering failed for %s: %s", plan.date, exc)
        plan.warnings.append(f"launch-site clustering unavailable: {exc}")
        return

    plan.launch_sites = [
        {
            "index": site.index,
            "job_paths": list(site.job_paths),
            "job_names": list(site.job_names),
            "dot_4326": list(site.dot_4326),
            "circle_center_4326": list(site.circle_center_4326),
            "radius_m": site.radius_m,
            "flight_time_min": site.flight_time_min,
            "max_altitude_m": site.max_altitude_m,
        }
        for site in sites
    ]
