"""Opportunity scoring: hard gates, component weighting, ranking (spec §7.4, §13)."""

from __future__ import annotations

import datetime as _dt
import itertools

import pytest

from flightmanager.season import scheduling
from flightmanager.season.config import SeasonConfig
from flightmanager.season.models import Campaign, CampaignType, Window
from flightmanager.season.weather_history import HourlyWeather, HourSample

LAT, LON = 62.79, 22.84
#: Midsummer, so the sun is never the limiting factor unless a test makes it so.
DAY = _dt.date(2026, 6, 17)
TARGET = _dt.date(2026, 6, 17)


def _window(earliest=None, target=None, latest=None, **kwargs) -> Window:
    return Window(
        computed_at="2026-06-01T00:00:00+00:00",
        earliest=earliest or (TARGET - _dt.timedelta(days=3)),
        target=target or TARGET,
        latest=latest or (TARGET + _dt.timedelta(days=7)),
        **kwargs,
    )


def _campaign(**kwargs) -> Campaign:
    defaults = dict(
        campaign_id="2026-emergence_count-5241087453",
        type_id="emergence_count",
        season=2026,
        job_paths=["kentta/5241087453"],
        state="open",
        window=_window(),
    )
    defaults.update(kwargs)
    return Campaign(**defaults)


def _type(**kwargs) -> CampaignType:
    defaults = dict(
        id="emergence_count",
        label_fi="Orastuminen",
        label_en="Emergence / stand count",
        trigger_stage="emergence",
        required_gsd_cm=0.6,
        sensor="rgb",
        priority="high",
    )
    defaults.update(kwargs)
    return CampaignType(**defaults)


def _hours(
    wind=3.0, cloud=20.0, precip=0.0, gust=None, start=4, end=22
) -> dict[int, HourSample]:
    return {
        h: HourSample(
            hour_key=f"{DAY.isoformat()}T{h:02d}",
            wind_ms=wind,
            gust_ms=gust if gust is not None else wind * 1.2,
            cloud_pct=cloud,
            precip_mm=precip,
            temp_c=18.0,
        )
        for h in range(start, end)
    }


def _conditions(passes=None, **kwargs) -> scheduling.DayConditions:
    return scheduling.DayConditions(
        date=DAY, hours=_hours(**kwargs), passes=passes or []
    )


def _ctx(**kwargs) -> scheduling.ScoringContext:
    defaults = dict(cfg=SeasonConfig(), lat=LAT, lon=LON, utc_offset_s=3 * 3600)
    defaults.update(kwargs)
    return scheduling.ScoringContext(**defaults)


def _score(campaign=None, ct=None, conditions=None, ctx=None):
    return scheduling.score_campaign_on(
        campaign or _campaign(),
        ct or _type(),
        conditions if conditions is not None else _conditions(),
        ctx or _ctx(),
    )[0]


class TestHappyPath:
    def test_a_good_day_scores_well(self):
        score = _score()
        assert score.score > 0.6
        assert score.gates_failed == []

    def test_components_are_all_reported(self):
        """Spec §7.4: show the components, not just the total."""
        expected = {
            "in_window",
            "days_from_target",
            "wind",
            "cloud",
            "precip",
            "sun_elevation",
            "satellite_coincidence",
        }
        assert set(_score().components) == expected

    def test_every_component_is_in_unit_range(self):
        assert all(0.0 <= v <= 1.0 for v in _score().components.values())

    def test_priority_is_carried_for_the_field_day_planner(self):
        assert _score(ct=_type(priority="opportunistic")).priority == "opportunistic"


class TestHardGates:
    """Spec §13: each hard gate independently zeroes the score."""

    def test_outside_the_window(self):
        far = _campaign(
            window=_window(
                earliest=_dt.date(2026, 7, 1),
                target=_dt.date(2026, 7, 3),
                latest=_dt.date(2026, 7, 8),
            )
        )
        score = _score(campaign=far)
        assert score.score == 0.0
        assert scheduling.GATE_OUTSIDE_WINDOW in score.gates_failed

    def test_wind_above_the_limit(self):
        score = _score(conditions=_conditions(wind=14.0))
        assert score.score == 0.0
        assert scheduling.GATE_WIND in score.gates_failed

    def test_precipitation_above_the_threshold(self):
        score = _score(conditions=_conditions(precip=3.0))
        assert score.score == 0.0
        assert scheduling.GATE_PRECIP in score.gates_failed

    def test_no_hour_meeting_the_solar_floor(self):
        """Midwinter: the sun never clears 30° at this latitude."""
        winter = _dt.date(2026, 12, 15)
        conditions = scheduling.DayConditions(date=winter, hours={})
        campaign = _campaign(
            window=_window(
                earliest=winter - _dt.timedelta(days=2),
                target=winter,
                latest=winter + _dt.timedelta(days=2),
            )
        )
        score = _score(
            campaign=campaign, ct=_type(sensor="multispectral"), conditions=conditions
        )
        assert score.score == 0.0
        assert scheduling.GATE_SOLAR in score.gates_failed

    def test_job_not_flight_ready(self):
        ctx = _ctx(flight_ready={"kentta/5241087453": False})
        score = _score(ctx=ctx)
        assert score.score == 0.0
        assert scheduling.GATE_NOT_FLIGHT_READY in score.gates_failed

    def test_a_required_pass_that_does_not_exist(self):
        score = _score(
            ct=_type(satellite_coincidence="required", sensor="multispectral")
        )
        assert score.score == 0.0
        assert scheduling.GATE_NO_COINCIDENT_PASS in score.gates_failed

    def test_flight_ready_true_does_not_gate(self):
        ctx = _ctx(flight_ready={"kentta/5241087453": True})
        assert _score(ctx=ctx).gates_failed == []

    def test_an_unknown_flight_ready_does_not_gate(self):
        """A job the cards say nothing about must not be silently excluded."""
        assert _score(ctx=_ctx(flight_ready={})).gates_failed == []

    def test_gates_compose_without_masking_each_other(self):
        score = _score(conditions=_conditions(wind=20.0, precip=5.0))
        assert scheduling.GATE_WIND in score.gates_failed
        assert scheduling.GATE_PRECIP in score.gates_failed


class TestScoreRange:
    """Property: the score is always in [0, 1], whatever the inputs."""

    @pytest.mark.parametrize(
        "wind,cloud,precip",
        list(
            itertools.product(
                [0.0, 5.0, 9.9, 30.0], [0.0, 50.0, 100.0], [0.0, 0.4, 9.0]
            )
        ),
    )
    def test_score_stays_in_unit_range(self, wind, cloud, precip):
        score = _score(conditions=_conditions(wind=wind, cloud=cloud, precip=precip))
        assert 0.0 <= score.score <= 1.0
        assert all(0.0 <= v <= 1.0 for v in score.components.values())

    def test_absent_weather_does_not_break_scoring(self):
        bare = scheduling.DayConditions(date=DAY, hours={})
        score = _score(conditions=bare)
        assert 0.0 <= score.score <= 1.0

    def test_extreme_weights_still_bound_the_score(self):
        cfg = SeasonConfig()
        cfg.weights.wind = 1000.0
        score = _score(ctx=_ctx(cfg=cfg))
        assert 0.0 <= score.score <= 1.0


class TestComponentBehaviour:
    def test_calmer_wind_scores_higher(self):
        calm = _score(conditions=_conditions(wind=1.0)).components["wind"]
        breezy = _score(conditions=_conditions(wind=8.0)).components["wind"]
        assert calm > breezy

    def test_clearer_sky_scores_higher(self):
        clear = _score(conditions=_conditions(cloud=5.0)).components["cloud"]
        overcast = _score(conditions=_conditions(cloud=95.0)).components["cloud"]
        assert clear > overcast

    def test_the_target_day_scores_best_on_proximity(self):
        window = _window()
        at_target = scheduling.score_days_from_target(window, window.target)
        at_edge = scheduling.score_days_from_target(window, window.latest)
        assert at_target == 1.0
        assert at_target > at_edge

    def test_proximity_is_symmetric_about_the_target(self):
        window = _window(
            earliest=TARGET - _dt.timedelta(days=5),
            latest=TARGET + _dt.timedelta(days=5),
        )
        before = scheduling.score_days_from_target(
            window, TARGET - _dt.timedelta(days=2)
        )
        after = scheduling.score_days_from_target(
            window, TARGET + _dt.timedelta(days=2)
        )
        assert before == pytest.approx(after)

    def test_sun_component_tracks_the_usable_fraction_of_the_day(self):
        june = _score().components["sun_elevation"]
        # An RGB campaign in early September still qualifies, but for fewer hours.
        september = _dt.date(2026, 9, 5)
        conditions = scheduling.DayConditions(date=september, hours=_hours())
        campaign = _campaign(
            window=_window(
                earliest=september - _dt.timedelta(days=2),
                target=september,
                latest=september + _dt.timedelta(days=2),
            )
        )
        autumn = _score(campaign=campaign, conditions=conditions).components[
            "sun_elevation"
        ]
        assert june > autumn


class TestSatelliteCoincidence:
    def _pass(self, hour: int, clear: bool = True) -> dict:
        return {
            "name": "Sentinel-2A",
            "peak_local": f"{DAY.isoformat()}T{hour:02d}:30:00+03:00",
            "clear_window": clear,
            "daytime": True,
        }

    def test_ignored_campaigns_score_zero_coincidence(self):
        score = _score(
            ct=_type(satellite_coincidence="ignored"),
            conditions=_conditions(passes=[self._pass(11)]),
        )
        assert score.components["satellite_coincidence"] == 0.0

    def test_ignoring_coincidence_does_not_cap_the_total(self):
        """The unused weight is dropped from the denominator, not left at zero."""
        perfect = _conditions(wind=0.0, cloud=0.0, precip=0.0)
        score = _score(ct=_type(satellite_coincidence="ignored"), conditions=perfect)
        assert score.score > 0.95

    def test_a_preferred_campaign_gains_from_a_clear_pass(self):
        ct = _type(satellite_coincidence="preferred", sensor="multispectral")
        without = _score(ct=ct, conditions=_conditions())
        with_pass = _score(ct=ct, conditions=_conditions(passes=[self._pass(12)]))
        assert with_pass.score > without.score
        assert with_pass.components["satellite_coincidence"] > 0.9

    def test_a_clouded_pass_is_worth_nothing(self):
        ct = _type(satellite_coincidence="preferred", sensor="multispectral")
        score = _score(
            ct=ct, conditions=_conditions(passes=[self._pass(12, clear=False)])
        )
        assert score.components["satellite_coincidence"] == 0.0

    def test_coincidence_decays_with_time_from_the_flight(self):
        ct = _type(satellite_coincidence="preferred", sensor="multispectral")
        near = _score(ct=ct, conditions=_conditions(passes=[self._pass(12)]))
        far = _score(ct=ct, conditions=_conditions(passes=[self._pass(19)]))
        assert (
            near.components["satellite_coincidence"]
            > far.components["satellite_coincidence"]
        )

    def test_a_required_campaign_passes_its_gate_with_a_clear_pass(self):
        ct = _type(satellite_coincidence="required", sensor="multispectral")
        score = _score(ct=ct, conditions=_conditions(passes=[self._pass(12)]))
        assert scheduling.GATE_NO_COINCIDENT_PASS not in score.gates_failed
        assert score.score > 0


class TestUsableHours:
    def test_wind_removes_sunlit_hours(self):
        ctx = _ctx()
        solar_day = ctx.solar_day(DAY, 20.0)
        gusty = {
            h: HourSample(hour_key="", wind_ms=20.0) for h in solar_day.qualifying_hours
        }
        usable = scheduling.usable_hours(
            scheduling.DayConditions(date=DAY, hours=gusty), solar_day, ctx.cfg, 10.0
        )
        assert usable.hours == []
        assert usable.blocked_by_wind

    def test_rain_removes_sunlit_hours(self):
        ctx = _ctx()
        solar_day = ctx.solar_day(DAY, 20.0)
        wet = {
            h: HourSample(hour_key="", wind_ms=2.0, precip_mm=5.0)
            for h in solar_day.qualifying_hours
        }
        usable = scheduling.usable_hours(
            scheduling.DayConditions(date=DAY, hours=wet), solar_day, ctx.cfg, 10.0
        )
        assert usable.hours == []
        assert usable.blocked_by_precip

    def test_cloud_is_not_a_gate_on_hours(self):
        """Overcast is bad for radiometry and fine for structural RGB."""
        ctx = _ctx()
        solar_day = ctx.solar_day(DAY, 20.0)
        overcast = {
            h: HourSample(hour_key="", wind_ms=2.0, cloud_pct=100.0)
            for h in solar_day.qualifying_hours
        }
        usable = scheduling.usable_hours(
            scheduling.DayConditions(date=DAY, hours=overcast), solar_day, ctx.cfg, 10.0
        )
        assert usable.hours

    def test_missing_hourly_data_falls_back_to_the_solar_floor(self):
        ctx = _ctx()
        solar_day = ctx.solar_day(DAY, 20.0)
        usable = scheduling.usable_hours(
            scheduling.DayConditions(date=DAY, hours={}), solar_day, ctx.cfg, 10.0
        )
        assert usable.hours == solar_day.qualifying_hours

    def test_labels_and_midpoint(self):
        usable = scheduling.UsableHours(hours=[10, 11, 12, 15])
        assert usable.labels() == ["10:00-13:00", "15:00-16:00"]
        assert usable.midpoint_hour() == 11.5


class TestFlags:
    def test_gusts_are_flagged_against_a_passing_mean(self):
        """The spec's own example flag (§4.5)."""
        score = _score(conditions=_conditions(wind=5.0, gust=9.0))
        assert any("gust" in f for f in score.flags)

    def test_the_solar_wall_is_stated_not_merely_scored(self):
        """Spec §7.2: say so explicitly rather than returning a low score."""
        october = _dt.date(2026, 10, 20)
        campaign = _campaign(
            window=_window(
                earliest=october - _dt.timedelta(days=3),
                target=october,
                latest=october + _dt.timedelta(days=3),
            )
        )
        score = _score(
            campaign=campaign,
            ct=_type(sensor="multispectral"),
            conditions=scheduling.DayConditions(date=october, hours=_hours()),
        )
        assert any("not possible" in f and "latitude" in f for f in score.flags)

    def test_a_default_wind_limit_is_disclosed(self):
        ctx = _ctx(wind_limit_ms=10.0, wind_limit_is_default=True)
        assert any("drone_wind_limit_ms is unset" in f for f in _score(ctx=ctx).flags)


class TestScoreDay:
    def _pairs(self):
        return [
            (_campaign(), _type()),
            (
                _campaign(
                    campaign_id="2026-canopy_peak-5241087454",
                    type_id="canopy_peak",
                    job_paths=["kentta/5241087454"],
                ),
                _type(
                    id="canopy_peak",
                    label_en="Peak canopy",
                    sensor="multispectral",
                    required_gsd_cm=3.0,
                    priority="normal",
                ),
            ),
        ]

    def test_a_day_collects_every_campaign_it_serves(self):
        opportunity = scheduling.score_day(DAY, self._pairs(), _conditions(), _ctx())
        assert len(opportunity.campaign_ids) == 2
        assert opportunity.score > 0

    def test_the_headline_score_is_the_best_campaign(self):
        opportunity = scheduling.score_day(DAY, self._pairs(), _conditions(), _ctx())
        assert opportunity.score == max(c.score for c in opportunity.servable())
        assert opportunity.mean_score <= opportunity.score

    def test_blocked_campaigns_are_kept_but_not_counted(self):
        pairs = self._pairs()
        ctx = _ctx(flight_ready={"kentta/5241087453": False})
        opportunity = scheduling.score_day(DAY, pairs, _conditions(), ctx)
        assert len(opportunity.per_campaign) == 2
        assert len(opportunity.campaign_ids) == 1

    def test_best_hours_are_the_most_permissive_across_campaigns(self):
        """An RGB campaign can fly hours a multispectral one cannot."""
        september = _dt.date(2026, 9, 8)
        pairs = [
            (
                _campaign(
                    window=_window(
                        earliest=september - _dt.timedelta(days=3),
                        target=september,
                        latest=september + _dt.timedelta(days=3),
                    )
                ),
                _type(sensor="rgb"),
            ),
            (
                _campaign(
                    campaign_id="ms",
                    type_id="canopy_peak",
                    window=_window(
                        earliest=september - _dt.timedelta(days=3),
                        target=september,
                        latest=september + _dt.timedelta(days=3),
                    ),
                ),
                _type(id="canopy_peak", sensor="multispectral", required_gsd_cm=3.0),
            ),
        ]
        conditions = scheduling.DayConditions(date=september, hours=_hours())
        opportunity = scheduling.score_day(september, pairs, conditions, _ctx())
        assert opportunity.best_hours

    def test_a_day_where_nothing_flies_says_which_gate(self):
        opportunity = scheduling.score_day(
            DAY, self._pairs(), _conditions(wind=25.0), _ctx()
        )
        assert opportunity.score == 0.0
        assert any("no campaign can be flown" in f for f in opportunity.flags)

    def test_duplicate_campaign_flags_are_not_repeated(self):
        opportunity = scheduling.score_day(
            DAY, self._pairs() * 4, _conditions(wind=5.0, gust=9.0), _ctx()
        )
        gust_flags = [f for f in opportunity.flags if "gust" in f]
        assert len(gust_flags) == 1

    def test_a_golden_day_is_flagged(self):
        conditions = _conditions()
        conditions.golden = True
        opportunity = scheduling.score_day(DAY, self._pairs(), conditions, _ctx())
        assert any("golden day" in f for f in opportunity.flags)


class TestRanking:
    def _opportunity(self, day, score, count=1):
        from flightmanager.season.models import Opportunity

        return Opportunity(
            date=day, score=score, campaign_ids=[f"c{i}" for i in range(count)]
        )

    def test_best_score_first(self):
        ranked = scheduling.rank_opportunities(
            [
                self._opportunity(DAY, 0.4),
                self._opportunity(DAY + _dt.timedelta(days=1), 0.9),
            ]
        )
        assert ranked[0].score == 0.9

    def test_ties_break_on_how_much_the_day_clears(self):
        ranked = scheduling.rank_opportunities(
            [
                self._opportunity(DAY, 0.8, count=1),
                self._opportunity(DAY + _dt.timedelta(days=1), 0.8, count=5),
            ]
        )
        assert len(ranked[0].campaign_ids) == 5

    def test_full_ties_break_on_the_earlier_date(self):
        ranked = scheduling.rank_opportunities(
            [
                self._opportunity(DAY + _dt.timedelta(days=3), 0.8, count=2),
                self._opportunity(DAY, 0.8, count=2),
            ]
        )
        assert ranked[0].date == DAY


class TestGreedyRoute:
    """Ported from the browser UI's ``list-math.js``; must match it exactly."""

    def test_starts_at_the_northernmost_point(self):
        points = [("s", 62.0, 22.0), ("n", 63.0, 22.0), ("m", 62.5, 22.0)]
        assert scheduling.greedy_route(points)[0] == "n"

    def test_ties_on_latitude_break_westernmost_first(self):
        points = [("e", 63.0, 23.0), ("w", 63.0, 22.0)]
        assert scheduling.greedy_route(points)[0] == "w"

    def test_visits_every_point_once(self):
        points = [(f"p{i}", 62.0 + i * 0.01, 22.0 + i * 0.01) for i in range(8)]
        route = scheduling.greedy_route(points)
        assert sorted(route) == sorted(p[0] for p in points)

    def test_takes_the_nearest_next_point(self):
        points = [("start", 63.0, 22.0), ("near", 62.99, 22.0), ("far", 62.0, 22.0)]
        assert scheduling.greedy_route(points) == ["start", "near", "far"]

    def test_trivial_inputs(self):
        assert scheduling.greedy_route([]) == []
        assert scheduling.greedy_route([("a", 1.0, 2.0)]) == ["a"]

    def test_haversine_matches_a_known_distance(self):
        # One degree of latitude is ~111.2 km.
        assert scheduling.haversine_m(62.0, 22.0, 63.0, 22.0) == pytest.approx(
            111195, rel=0.01
        )


class TestDayConditions:
    def test_hourly_wins_over_daily_aggregates(self):
        conditions = scheduling.DayConditions(
            date=DAY, hours=_hours(wind=2.0), daily_wind_ms=9.0
        )
        assert conditions.mean_wind(6, 18) == pytest.approx(2.0)

    def test_daily_aggregates_fill_in_when_hourly_is_missing(self):
        conditions = scheduling.DayConditions(
            date=DAY, hours={}, daily_wind_ms=9.0, daily_cloud_pct=40.0
        )
        assert conditions.mean_wind(6, 18) == 9.0
        assert conditions.mean_cloud(6, 18) == 40.0
        assert not conditions.has_hourly

    def test_precipitation_sums_rather_than_averages(self):
        conditions = scheduling.DayConditions(
            date=DAY, hours=_hours(precip=0.5, start=6, end=10)
        )
        assert conditions.total_precip(6, 10) == pytest.approx(2.0)

    def test_only_clear_passes_count(self):
        conditions = scheduling.DayConditions(
            date=DAY,
            passes=[{"clear_window": True}, {"clear_window": False}],
        )
        assert len(conditions.clear_sky_passes()) == 1

    def test_build_day_conditions_merges_both_sources(self):
        hourly = HourlyWeather(
            by_hour={
                f"{DAY.isoformat()}T{h:02d}": HourSample(hour_key="", wind_ms=3.0)
                for h in range(6, 18)
            }
        )
        slots = [
            {
                "date": DAY.isoformat(),
                "weather": {"wind_avg_ms": 9.0, "cloud_pct": 50.0, "precip_mm": 0.1},
                "satellites": [{"clear_window": True, "name": "S2A"}],
                "golden": True,
            }
        ]
        merged = scheduling.build_day_conditions([DAY], hourly, slots)[DAY]
        assert merged.has_hourly
        assert merged.golden
        assert len(merged.passes) == 1
        assert merged.daily_cloud_pct == 50.0


class TestSchedulableSelection:
    def test_only_open_and_scheduled_campaigns_are_scored(self, tables):
        from flightmanager.season.models import SeasonPlan

        plan = SeasonPlan(
            season=2026,
            folder="kentta",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        for state in ("open", "scheduled", "flown", "missed", "skipped", "planned"):
            plan.campaigns.append(
                _campaign(
                    campaign_id=f"c-{state}",
                    state=state,
                    window=_window() if state != "planned" else None,
                )
            )
        pairs = scheduling.schedulable_campaigns(plan, tables)
        assert {c.state for c, _ in pairs} == {"open", "scheduled"}

    def test_audience_filters_the_selection(self, tables):
        from flightmanager.season.models import SeasonPlan

        plan = SeasonPlan(
            season=2026,
            folder="kentta",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        plan.campaigns.append(_campaign(campaign_id="s2", type_id="s2_calibration"))
        assert scheduling.schedulable_campaigns(plan, tables, audience="farmer") == []
        assert scheduling.schedulable_campaigns(plan, tables, audience="researcher")


class TestZeroIsNotMissing:
    """A measured zero must never be mistaken for absent data."""

    def test_dead_calm_scores_as_perfect_wind_not_unknown(self):
        conditions = scheduling.DayConditions(
            date=DAY, hours=_hours(wind=0.0), daily_wind_ms=9.0
        )
        assert conditions.mean_wind(6, 18) == 0.0
        assert _score(conditions=conditions).components["wind"] == 1.0

    def test_zero_cloud_is_kept(self):
        conditions = scheduling.DayConditions(
            date=DAY, hours=_hours(cloud=0.0), daily_cloud_pct=80.0
        )
        assert conditions.mean_cloud(6, 18) == 0.0

    def test_zero_precipitation_is_kept(self):
        conditions = scheduling.DayConditions(
            date=DAY, hours=_hours(precip=0.0), daily_precip_mm=5.0
        )
        assert conditions.total_precip(6, 18) == 0.0


class TestDayComponentsExplainTheHeadline:
    """The printed breakdown must belong to the campaign that set the score."""

    def test_components_come_from_the_best_campaign_not_an_average(self):
        pairs = [
            (_campaign(campaign_id="near"), _type()),
            (
                _campaign(
                    campaign_id="far",
                    job_paths=["kentta/5241087454"],
                    window=_window(
                        earliest=TARGET - _dt.timedelta(days=1),
                        target=TARGET + _dt.timedelta(days=6),
                        latest=TARGET + _dt.timedelta(days=7),
                    ),
                ),
                _type(id="canopy_peak"),
            ),
        ]
        opportunity = scheduling.score_day(DAY, pairs, _conditions(), _ctx())
        best = max(opportunity.servable(), key=lambda c: c.score)
        assert opportunity.components == best.components

    def test_the_full_component_set_is_exposed_on_the_day(self):
        """Spec §4.5 shows all seven keys on an Opportunity."""
        opportunity = scheduling.score_day(
            DAY, [(_campaign(), _type())], _conditions(), _ctx()
        )
        assert set(opportunity.components) == {
            "in_window",
            "days_from_target",
            "wind",
            "cloud",
            "precip",
            "sun_elevation",
            "satellite_coincidence",
        }

    def test_a_fully_blocked_day_still_reports_a_breakdown(self):
        opportunity = scheduling.score_day(
            DAY, [(_campaign(), _type())], _conditions(wind=30.0), _ctx()
        )
        assert opportunity.components
