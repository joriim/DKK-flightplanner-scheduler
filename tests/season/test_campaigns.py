"""Campaign instantiation, window derivation, and the re-plan merge."""

from __future__ import annotations

import datetime as _dt

import pytest

from flightmanager.season.campaigns import (
    build_campaigns,
    compute_window,
    merge_campaign,
    repeat_targets,
)
from flightmanager.season.models import (
    Campaign,
    CampaignOutcome,
    CampaignType,
    InvalidTransition,
    JobAssignment,
    Window,
    check_transition,
    make_campaign_id,
)
from flightmanager.season.phenology import build_timeline
from tests.season.conftest import SOWING, TODAY, constant_series

JOB = "kentta/5241087453"


@pytest.fixture
def timeline(cfg, tables):
    series = constant_series(
        SOWING,
        260,
        15.0,
        archive_until=TODAY,
        forecast_until=TODAY + _dt.timedelta(days=14),
    )
    return build_timeline(series, tables.crop("spring_barley"), SOWING, cfg)


@pytest.fixture
def assignment() -> JobAssignment:
    return JobAssignment(
        job_path=JOB,
        crop_id="spring_barley",
        sowing_date=SOWING,
        sowing_date_source="farmer_reported",
    )


class TestWindowDerivation:
    def test_window_brackets_the_trigger_plus_offset(self, tables, timeline, cfg):
        ct = tables.campaign_type("emergence_count")
        window, flags, _ = compute_window(
            ct, timeline, tables.crop("spring_barley"), cfg
        )
        emergence = timeline.stage("emergence").date
        assert window.target == emergence + _dt.timedelta(days=7)
        assert window.earliest == window.target - _dt.timedelta(days=3)
        assert flags == []

    def test_hard_deadline_pulls_the_close_date_in(self, tables, timeline, cfg):
        ct = tables.campaign_type("emergence_count")
        window, _, _ = compute_window(ct, timeline, tables.crop("spring_barley"), cfg)
        tillering = timeline.stage("tillering").date
        # target + 10 d would run past tillering, so the stage wins.
        assert window.latest == tillering
        assert window.deadline_stage == "tillering"
        assert any("closes early" in n for n in window.notes)

    def test_negative_offset_puts_the_window_before_the_trigger(
        self, tables, timeline, cfg
    ):
        ct = tables.campaign_type("bare_soil_baseline")
        window, _, _ = compute_window(ct, timeline, tables.crop("spring_barley"), cfg)
        assert window.target == SOWING - _dt.timedelta(days=14)
        assert window.latest < SOWING  # stays clear of the sowing date itself

    def test_missing_stage_flags_instead_of_raising(self, tables, timeline, cfg):
        ct = CampaignType(
            id="x",
            label_fi="x",
            label_en="x",
            trigger_stage="no_such_stage",
            required_gsd_cm=3.0,
        )
        window, flags, reasons = compute_window(
            ct, timeline, tables.crop("spring_barley"), cfg
        )
        assert window is None
        assert flags == ["no_stage_threshold"]
        assert "no_such_stage" in reasons[0]

    def test_unreached_stage_flags_with_the_phenology_reason(self, tables, cfg):
        cold = build_timeline(
            constant_series(SOWING, 120, 7.0), tables.crop("spring_barley"), SOWING, cfg
        )
        ct = tables.campaign_type("preharvest_reference")
        window, flags, reasons = compute_window(
            ct, cold, tables.crop("spring_barley"), cfg
        )
        assert window is None
        assert flags == ["trigger_not_reached"]
        assert reasons and "°C·d" in reasons[0]

    def test_weather_event_campaigns_get_no_window_and_say_why(
        self, tables, timeline, cfg
    ):
        ct = tables.campaign_type("lodging_survey")
        window, flags, reasons = compute_window(
            ct, timeline, tables.crop("spring_barley"), cfg
        )
        assert window is None
        assert flags == ["weather_event_trigger"]
        assert "gust" in reasons[0]

    def test_uncalibrated_crop_puts_the_caveat_in_the_window_notes(
        self, tables, timeline, cfg
    ):
        ct = tables.campaign_type("emergence_count")
        window, _, _ = compute_window(ct, timeline, tables.crop("spring_barley"), cfg)
        assert window.confidence == "low"
        assert any("uncalibrated" in n for n in window.notes)
        assert "uncalibrated" in window.uncertainty_label()

    def test_window_past_its_own_deadline_is_flagged_not_swallowed(
        self, tables, timeline, cfg
    ):
        ct = CampaignType(
            id="bad",
            label_fi="x",
            label_en="Misconfigured",
            trigger_stage="emergence",
            offset_days=60,
            hard_deadline_stage="tillering",
            required_gsd_cm=3.0,
        )
        window, flags, _ = compute_window(
            ct, timeline, tables.crop("spring_barley"), cfg
        )
        assert "window_past_deadline" in flags
        assert window.earliest <= window.target <= window.latest


class TestInstantiation:
    def test_every_campaign_type_is_instantiated(
        self, tables, assignment, timeline, cfg
    ):
        built = build_campaigns(tables, assignment, timeline, cfg, 2027)
        # 12 types, three of which repeat into several instances each.
        assert len({c.type_id for c in built}) == len(tables.campaigns)
        assert all(c.job_paths == [JOB] for c in built)

    def test_campaign_ids_are_stable_across_runs(
        self, tables, assignment, timeline, cfg
    ):
        first = build_campaigns(tables, assignment, timeline, cfg, 2027)
        second = build_campaigns(tables, assignment, timeline, cfg, 2027)
        assert [c.campaign_id for c in first] == [c.campaign_id for c in second]

    def test_id_shape_matches_the_spec(self):
        assert (
            make_campaign_id(2027, "emergence_count", "5241087453")
            == "2027-emergence_count-5241087453"
        )
        assert make_campaign_id(2027, "disease_watch", "5241087453", 2).endswith("-r2")

    def test_no_sowing_date_yields_planned_campaigns_with_a_flag(self, tables, cfg):
        bare = JobAssignment(job_path=JOB, crop_id="spring_barley")
        built = build_campaigns(tables, bare, None, cfg, 2027)
        assert built
        assert all(c.state == "planned" and c.window is None for c in built)
        assert all("sowing_date_required" in c.flags for c in built)

    def test_no_crop_says_crop_not_sowing(self, tables, cfg):
        bare = JobAssignment(job_path=JOB, sowing_date=SOWING)
        built = build_campaigns(tables, bare, None, cfg, 2027)
        assert all("crop_required" in c.flags for c in built)

    def test_audience_filter_drops_researcher_only_campaigns(
        self, tables, assignment, timeline, cfg
    ):
        farmer = build_campaigns(
            tables, assignment, timeline, cfg, 2027, audience="farmer"
        )
        assert "s2_calibration" not in {c.type_id for c in farmer}
        assert "emergence_count" in {c.type_id for c in farmer}

    def test_only_types_limits_the_build(self, tables, assignment, timeline, cfg):
        built = build_campaigns(
            tables, assignment, timeline, cfg, 2027, only_types=["emergence_count"]
        )
        assert {c.type_id for c in built} == {"emergence_count"}

    def test_unknown_type_is_an_error(self, tables, assignment, timeline, cfg):
        with pytest.raises(ValueError, match="unknown campaign type"):
            build_campaigns(
                tables, assignment, timeline, cfg, 2027, only_types=["nope"]
            )


class TestRepeats:
    def test_cadence_repeats_are_spaced_by_every_days(self, tables, timeline):
        ct = tables.campaign_type("maturity_forecast")
        targets = repeat_targets(ct, timeline, tables.crop("spring_barley"))
        assert len(targets) == ct.repeat.max_count
        dates = [t[1] for t in targets]
        assert (dates[1] - dates[0]).days == ct.repeat.every_days

    def test_thermal_repeats_track_the_crop_not_the_calendar(self, tables, timeline):
        ct = tables.campaign_type("s2_calibration")
        assert ct.repeat.mode == "thermal"
        targets = repeat_targets(ct, timeline, tables.crop("spring_barley"))
        # 200 °C·d at 10 °C·d/day is 20 days in this flat series.
        dates = [t[1] for t in targets]
        assert (dates[1] - dates[0]).days == 20

    def test_thermal_repeats_stretch_in_a_cold_season(self, tables, cfg):
        warm = build_timeline(
            constant_series(SOWING, 400, 15.0),
            tables.crop("spring_barley"),
            SOWING,
            cfg,
        )
        cold = build_timeline(
            constant_series(SOWING, 400, 10.0),
            tables.crop("spring_barley"),
            SOWING,
            cfg,
        )
        ct = tables.campaign_type("s2_calibration")
        warm_dates = [
            t[1] for t in repeat_targets(ct, warm, tables.crop("spring_barley"))
        ]
        cold_dates = [
            t[1] for t in repeat_targets(ct, cold, tables.crop("spring_barley"))
        ]
        assert (cold_dates[1] - cold_dates[0]).days > (
            warm_dates[1] - warm_dates[0]
        ).days

    def test_later_repeats_are_no_better_known_than_the_anchor(self, tables, timeline):
        ct = tables.campaign_type("maturity_forecast")
        targets = repeat_targets(ct, timeline, tables.crop("spring_barley"))
        bands = [t[3] for t in targets]
        assert bands == sorted(bands)

    def test_repeat_instances_get_distinct_ids(self, tables, assignment, timeline, cfg):
        built = build_campaigns(
            tables, assignment, timeline, cfg, 2027, only_types=["disease_watch"]
        )
        ids = [c.campaign_id for c in built]
        assert len(ids) == len(set(ids)) > 1
        assert [c.repeat_index for c in built] == list(range(len(built)))


class TestStateMachine:
    def test_legal_transition_passes(self):
        check_transition("open", "scheduled")

    def test_flown_is_terminal(self):
        with pytest.raises(InvalidTransition):
            check_transition("flown", "open")

    def test_unknown_state_is_rejected(self):
        with pytest.raises(InvalidTransition, match="unknown"):
            check_transition("nonsense", "open")

    def test_set_state_enforces_the_machine(self):
        c = Campaign(campaign_id="x", type_id="t", season=2027, state="flown")
        with pytest.raises(InvalidTransition):
            c.set_state("missed")


def _campaign(state="open", **kwargs) -> Campaign:
    window = Window(
        computed_at="2027-05-20T00:00:00+00:00",
        earliest=_dt.date(2027, 5, 24),
        target=_dt.date(2027, 5, 27),
        latest=_dt.date(2027, 6, 3),
    )
    defaults = dict(
        campaign_id="2027-emergence_count-5241087453",
        type_id="emergence_count",
        season=2027,
        job_paths=[JOB],
        state=state,
        window=window,
    )
    defaults.update(kwargs)
    return Campaign(**defaults)


class TestMerge:
    """``season plan`` refreshes the model's fields and nothing else."""

    def test_operator_data_survives_a_replan(self):
        stored = _campaign(
            state="flown",
            flown=_dt.date(2027, 5, 26),
            flight_job_name="kentta/5241087453",
            notes="flown between showers",
            outcome=CampaignOutcome(flown_date=_dt.date(2027, 5, 26), bbch=12),
        )
        fresh = _campaign(state="open")
        merged = merge_campaign(stored, fresh, TODAY)
        assert merged.state == "flown"
        assert merged.outcome.bbch == 12
        assert merged.notes == "flown between showers"
        assert merged.flight_job_name == "kentta/5241087453"

    def test_derived_window_is_refreshed(self):
        stored = _campaign()
        fresh = _campaign()
        fresh.window.target = _dt.date(2027, 6, 1)
        fresh.window.latest = _dt.date(2027, 6, 8)
        merged = merge_campaign(stored, fresh, TODAY)
        assert merged.window.target == _dt.date(2027, 6, 1)

    def test_manual_override_survives_and_wins(self):
        manual = Window(
            computed_at="2027-05-01T00:00:00+00:00",
            earliest=_dt.date(2027, 6, 10),
            target=_dt.date(2027, 6, 12),
            latest=_dt.date(2027, 6, 14),
            basis="manual",
        )
        stored = _campaign(manual_window=manual)
        merged = merge_campaign(stored, _campaign(), TODAY)
        assert merged.manual_window == manual
        assert merged.effective_window().target == _dt.date(2027, 6, 12)

    def test_a_closed_window_becomes_missed(self):
        merged = merge_campaign(_campaign(), _campaign(), _dt.date(2027, 7, 1))
        assert merged.state == "missed"

    def test_the_clock_never_revises_a_decision(self):
        for decided in ("flown", "skipped", "cancelled"):
            merged = merge_campaign(
                _campaign(state=decided), _campaign(), _dt.date(2027, 7, 1)
            )
            assert merged.state == decided

    def test_a_window_that_moves_back_into_the_future_un_misses(self):
        stored = _campaign(state="missed")
        fresh = _campaign()
        fresh.window.earliest = _dt.date(2027, 6, 20)
        fresh.window.target = _dt.date(2027, 6, 24)
        fresh.window.latest = _dt.date(2027, 7, 1)
        merged = merge_campaign(stored, fresh, _dt.date(2027, 6, 10))
        assert merged.state == "open"

    def test_scheduled_is_kept_while_the_window_is_open(self):
        merged = merge_campaign(_campaign(state="scheduled"), _campaign(), TODAY)
        assert merged.state == "scheduled"


class TestGroundSprayerNotice:
    def test_spray_related_types_carry_the_disclaimer(self, tables):
        ct = tables.campaign_type("weed_map_early")
        notice = ct.ground_sprayer_notice()
        assert notice is not None
        assert "GROUND SPRAYER" in notice
        assert "2009/128" in notice

    def test_unrelated_types_carry_none(self, tables):
        assert tables.campaign_type("emergence_count").ground_sprayer_notice() is None

    def test_the_shipped_library_flags_every_plant_protection_campaign(self, tables):
        flagged = {c.id for c in tables.campaigns.values() if c.drone_spray_related}
        assert flagged == {"weed_map_early", "disease_watch", "anthesis_marker"}


class TestRepeatUncertainty:
    """A repeat that already happened is a known date (spec §13)."""

    def test_an_observed_repeat_collapses_to_zero(self, tables, cfg):
        past_sowing = _dt.date(2026, 5, 14)
        series = constant_series(
            past_sowing, 260, 15.0, archive_until=past_sowing + _dt.timedelta(days=200)
        )
        timeline = build_timeline(
            series, tables.crop("spring_barley"), past_sowing, cfg
        )
        for ct_id in ("maturity_forecast", "s2_calibration"):
            targets = repeat_targets(
                tables.campaign_type(ct_id), timeline, tables.crop("spring_barley")
            )
            observed = [t for t in targets if t[2] == "observed"]
            assert observed, f"{ct_id} produced no observed repeats"
            assert all(t[3] == 0.0 for t in observed), (
                f"{ct_id} kept a band on an archive-backed date"
            )

    def test_a_projected_repeat_still_widens_with_index(self, tables, timeline):
        targets = repeat_targets(
            tables.campaign_type("maturity_forecast"),
            timeline,
            tables.crop("spring_barley"),
        )
        projected = [t for t in targets if t[2] != "observed"]
        bands = [t[3] for t in projected]
        assert bands == sorted(bands)
