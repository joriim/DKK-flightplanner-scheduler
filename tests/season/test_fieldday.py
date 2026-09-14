"""Cross-parcel batching: route order, budget, overflow split (spec §7.5, §13)."""

from __future__ import annotations

import datetime as _dt

import pytest

from flightmanager.season import fieldday
from flightmanager.season.config import SeasonConfig
from flightmanager.season.models import CampaignScore, Opportunity

DAY = _dt.date(2026, 6, 17)
FOLDER = "kentta"


def _score(
    job: str,
    score: float = 0.8,
    priority: str = "normal",
    closes: int = 5,
    label="Emergence / stand count",
) -> CampaignScore:
    return CampaignScore(
        campaign_id=f"2026-x-{job.split('/')[-1]}",
        type_id="emergence_count",
        label_en=label,
        label_fi="Orastuminen",
        job_path=job,
        priority=priority,
        score=score,
        days_to_close=closes,
    )


def _opportunity(scores: list[CampaignScore]) -> Opportunity:
    return Opportunity(
        date=DAY,
        campaign_ids=[s.campaign_id for s in scores],
        score=max((s.score for s in scores), default=0.0),
        per_campaign=scores,
        best_hours=["10:00-15:00"],
    )


def _card(
    name: str,
    *,
    sort_order=None,
    flight_time=30.0,
    batteries=1,
    lat=62.79,
    lon=22.84,
    flight_ready=True,
) -> dict:
    return {
        "path": f"{FOLDER}/{name}",
        "name": name,
        "sort_order": sort_order,
        "flight_time_min": flight_time,
        "battery_count": batteries,
        "flight_ready": flight_ready,
        "takeoff_point_4326": [lon, lat],
    }


def _plan(scores, cards, **kwargs):
    return fieldday.plan_field_day(
        _opportunity(scores), cards, SeasonConfig(), FOLDER, **kwargs
    )


class TestRouteOrder:
    def test_the_operators_saved_order_wins(self):
        """A dragged-out route encodes knowledge the algorithm does not have."""
        cards = [
            _card("a", sort_order=2, lat=63.5),
            _card("b", sort_order=0, lat=62.0),
            _card("c", sort_order=1, lat=62.5),
        ]
        plan = _plan([_score(c["path"]) for c in cards], cards)
        assert plan.order_source == "sort_order"
        assert [j.name for j in plan.jobs] == ["b", "c", "a"]

    def test_an_unrouted_folder_falls_back_to_greedy(self):
        cards = [
            _card("south", lat=62.0),
            _card("north", lat=63.0),
            _card("middle", lat=62.5),
        ]
        plan = _plan([_score(c["path"]) for c in cards], cards)
        assert plan.order_source == "greedy_nearest_neighbour"
        # Greedy starts northernmost, then takes nearest.
        assert [j.name for j in plan.jobs] == ["north", "middle", "south"]

    def test_a_partially_routed_folder_uses_greedy(self):
        """Half an order is not an order — mixing the two would be arbitrary."""
        cards = [_card("a", sort_order=0, lat=62.0), _card("b", lat=63.0)]
        plan = _plan([_score(c["path"]) for c in cards], cards)
        assert plan.order_source == "greedy_nearest_neighbour"

    def test_route_indices_are_contiguous_from_one(self):
        cards = [_card(n, lat=62.0 + i * 0.1) for i, n in enumerate("abcd")]
        plan = _plan([_score(c["path"]) for c in cards], cards)
        assert [j.route_index for j in plan.jobs] == [1, 2, 3, 4]

    def test_jobs_without_a_takeoff_point_still_get_an_order(self):
        cards = [_card("a"), _card("b")]
        for card in cards:
            card["takeoff_point_4326"] = None
        plan = _plan([_score(c["path"]) for c in cards], cards)
        assert len(plan.jobs) == 2
        assert plan.order_source == "name"

    def test_the_order_source_is_reported_to_the_operator(self):
        cards = [_card("a", sort_order=0)]
        plan = _plan([_score(cards[0]["path"])], cards)
        assert any("existing flight sequence" in n for n in plan.notices)


class TestGrouping:
    def test_several_campaigns_on_one_parcel_share_the_drive(self):
        cards = [_card("a")]
        scores = [
            _score("kentta/a", score=0.9, label="Emergence / stand count"),
            _score("kentta/a", score=0.7, label="Early weed mapping"),
        ]
        plan = _plan(scores, cards)
        assert len(plan.jobs) == 1
        assert len(plan.jobs[0].campaigns) == 2
        # Sorted best-first inside the parcel.
        assert plan.jobs[0].campaigns[0].score == 0.9

    def test_a_parcel_takes_the_priority_of_its_most_urgent_campaign(self):
        cards = [_card("a")]
        scores = [
            _score("kentta/a", priority="opportunistic"),
            _score("kentta/a", priority="high"),
        ]
        assert _plan(scores, cards).jobs[0].priority == "high"

    def test_a_parcel_takes_the_soonest_closing_window(self):
        cards = [_card("a")]
        scores = [_score("kentta/a", closes=12), _score("kentta/a", closes=2)]
        assert _plan(scores, cards).jobs[0].days_to_close == 2

    def test_blocked_campaigns_never_reach_the_day(self):
        cards = [_card("a")]
        blocked = _score("kentta/a")
        blocked.gates_failed = ["wind_above_limit"]
        plan = _plan([blocked], cards)
        assert plan.jobs == []

    def test_a_campaign_with_no_matching_card_is_reported(self):
        plan = _plan([_score("kentta/ghost")], [_card("a")])
        assert plan.jobs == []
        assert any("ghost" in w for w in plan.warnings)


class TestBudget:
    def test_a_day_that_fits_defers_nothing(self):
        cards = [_card(n, sort_order=i, flight_time=30.0) for i, n in enumerate("abcd")]
        plan = _plan([_score(c["path"]) for c in cards], cards)
        assert plan.deferred == []
        assert plan.overflows is False
        assert plan.total_flight_time_h == pytest.approx(2.0)

    def test_an_overflowing_day_defers_the_excess(self):
        cards = [
            _card(n, sort_order=i, flight_time=120.0) for i, n in enumerate("abcde")
        ]
        plan = _plan([_score(c["path"]) for c in cards], cards, max_hours=4.0)
        assert plan.overflows
        assert len(plan.jobs) == 2
        assert len(plan.deferred) == 3
        assert plan.total_flight_time_h <= 4.0

    def test_overflow_splits_by_priority_first(self):
        """Spec §7.5: split by campaign priority, then by window urgency."""
        cards = [_card(n, sort_order=i, flight_time=120.0) for i, n in enumerate("abc")]
        scores = [
            _score("kentta/a", priority="opportunistic", closes=1),
            _score("kentta/b", priority="high", closes=20),
            _score("kentta/c", priority="normal", closes=20),
        ]
        plan = _plan(scores, cards, max_hours=2.0)
        assert [j.name for j in plan.jobs] == ["b"]
        assert {j.name for j in plan.deferred} == {"a", "c"}

    def test_within_a_priority_the_closing_window_goes_first(self):
        cards = [_card(n, sort_order=i, flight_time=120.0) for i, n in enumerate("abc")]
        scores = [
            _score("kentta/a", priority="high", closes=14),
            _score("kentta/b", priority="high", closes=1),
            _score("kentta/c", priority="high", closes=7),
        ]
        plan = _plan(scores, cards, max_hours=2.0)
        assert [j.name for j in plan.jobs] == ["b"]

    def test_the_kept_route_is_renumbered_without_gaps(self):
        cards = [
            _card(n, sort_order=i, flight_time=120.0) for i, n in enumerate("abcd")
        ]
        scores = [
            _score("kentta/a", priority="opportunistic"),
            _score("kentta/b", priority="high"),
            _score("kentta/c", priority="opportunistic"),
            _score("kentta/d", priority="high"),
        ]
        plan = _plan(scores, cards, max_hours=4.0)
        assert [j.route_index for j in plan.jobs] == list(range(1, len(plan.jobs) + 1))

    def test_the_overflow_warning_names_the_numbers(self):
        cards = [_card(n, sort_order=i, flight_time=300.0) for i, n in enumerate("ab")]
        plan = _plan([_score(c["path"]) for c in cards], cards, max_hours=2.0)
        assert any("against a" in w and "budget" in w for w in plan.warnings)

    def test_deferred_jobs_keep_their_kept_route_positions_out_of_the_total(self):
        cards = [_card(n, sort_order=i, flight_time=120.0) for i, n in enumerate("abc")]
        plan = _plan([_score(c["path"]) for c in cards], cards, max_hours=2.0)
        assert plan.total_flight_time_min == pytest.approx(120.0)


class TestTotals:
    def test_flight_time_and_batteries_sum_over_the_kept_jobs(self):
        cards = [
            _card("a", sort_order=0, flight_time=25.0, batteries=1),
            _card("b", sort_order=1, flight_time=40.0, batteries=2),
        ]
        plan = _plan([_score(c["path"]) for c in cards], cards)
        assert plan.total_flight_time_min == pytest.approx(65.0)
        assert plan.total_battery_count == 3

    def test_a_job_with_no_estimate_makes_the_total_a_lower_bound(self):
        cards = [_card("a", sort_order=0, flight_time=None, batteries=None)]
        plan = _plan([_score(c["path"]) for c in cards], cards)
        assert plan.unknown_time_jobs == ["kentta/a"]
        assert any("lower bound" in w for w in plan.warnings)
        # An unknown battery count still costs at least one.
        assert plan.total_battery_count == 1

    def test_jobs_that_are_not_flight_ready_are_called_out(self):
        cards = [_card("a", sort_order=0, flight_ready=False)]
        plan = _plan([_score(c["path"]) for c in cards], cards)
        assert any("not flight-ready" in w for w in plan.warnings)

    def test_hours_are_derived_from_minutes(self):
        cards = [_card("a", sort_order=0, flight_time=90.0)]
        assert _plan([_score(cards[0]["path"])], cards).total_flight_time_h == 1.5


class TestLaunchSites:
    def test_clustering_is_delegated_to_the_host(self):
        seen = {}

        class FakeSite:
            index = 1
            job_paths = ["kentta/a"]
            job_names = ["a"]
            dot_4326 = [22.84, 62.79]
            circle_center_4326 = [22.84, 62.79]
            radius_m = 120.0
            flight_time_min = 30.0
            max_altitude_m = 80.0

        def cluster(cards):
            seen["cards"] = cards
            return [FakeSite()]

        cards = [_card("a", sort_order=0)]
        plan = _plan([_score(cards[0]["path"])], cards, cluster=cluster)
        assert len(plan.launch_sites) == 1
        assert plan.launch_sites[0]["radius_m"] == 120.0

    def test_clustering_receives_this_days_route_not_the_folders(self):
        seen = {}

        def cluster(cards):
            seen["orders"] = [c["sort_order"] for c in cards]
            return []

        cards = [_card("a", sort_order=7), _card("b", sort_order=9)]
        _plan([_score(c["path"]) for c in cards], cards, cluster=cluster)
        assert seen["orders"] == [0, 1]

    def test_a_clustering_failure_does_not_fail_the_day(self):
        def cluster(cards):
            raise RuntimeError("shapely exploded")

        cards = [_card("a", sort_order=0)]
        plan = _plan([_score(cards[0]["path"])], cards, cluster=cluster)
        assert plan.jobs  # the day still stands
        assert any("clustering unavailable" in w for w in plan.warnings)

    def test_no_clustering_seam_is_not_an_error(self):
        cards = [_card("a", sort_order=0)]
        assert _plan([_score(cards[0]["path"])], cards, cluster=None).launch_sites == []


class TestEmptyDay:
    def test_a_day_serving_nothing_says_so(self):
        plan = _plan([], [_card("a")])
        assert plan.jobs == []
        assert any("no campaign" in w for w in plan.warnings)

    def test_campaign_ids_round_trip(self):
        cards = [_card("a", sort_order=0)]
        scores = [_score("kentta/a")]
        plan = _plan(scores, cards)
        assert plan.campaign_ids() == [scores[0].campaign_id]
        assert plan.job_paths == ["kentta/a"]


class TestPassedOverUrgency:
    """First-fit packing must never quietly skip a more urgent parcel."""

    def test_a_deferred_parcel_that_outranks_a_kept_one_is_flagged(self):
        cards = [
            _card("urgent_big", sort_order=0, flight_time=140.0),
            _card("relaxed_small", sort_order=1, flight_time=60.0),
            _card("soonest", sort_order=2, flight_time=60.0),
        ]
        scores = [
            _score("kentta/urgent_big", priority="high", closes=2),
            _score("kentta/relaxed_small", priority="high", closes=9),
            _score("kentta/soonest", priority="high", closes=1),
        ]
        plan = _plan(scores, cards, max_hours=2.0)
        assert {j.name for j in plan.deferred} == {"urgent_big"}
        assert any("urgent_big" in w and "outranks" in w for w in plan.warnings)

    def test_the_warning_names_what_it_was_passed_over_for(self):
        cards = [
            _card("big", sort_order=0, flight_time=140.0),
            _card("small", sort_order=1, flight_time=50.0),
        ]
        scores = [
            _score("kentta/big", priority="high", closes=1),
            _score("kentta/small", priority="high", closes=8),
        ]
        plan = _plan(scores, cards, max_hours=1.0)
        warning = next(w for w in plan.warnings if "outranks" in w)
        assert "small" in warning
        assert "--max-hours" in warning

    def test_no_flag_when_the_deferral_respects_urgency(self):
        cards = [
            _card("first", sort_order=0, flight_time=60.0),
            _card("later", sort_order=1, flight_time=60.0),
        ]
        scores = [
            _score("kentta/first", priority="high", closes=1),
            _score("kentta/later", priority="high", closes=9),
        ]
        plan = _plan(scores, cards, max_hours=1.0)
        assert {j.name for j in plan.deferred} == {"later"}
        assert not any("outranks" in w for w in plan.warnings)
