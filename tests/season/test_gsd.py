"""GSD requirement → altitude, and the two failure modes (spec §5, §13)."""

from __future__ import annotations

import pytest

from flightmanager.season import gsd
from flightmanager.season.config import SeasonConfig
from flightmanager.season.models import CampaignType
from tests.season.conftest import ALL_DRONES, M3M_MS, M3M_RGB


def _type(gsd_cm: float, **kwargs) -> CampaignType:
    defaults = dict(
        id="t",
        label_fi="t",
        label_en="Test campaign",
        trigger_stage="emergence",
        required_gsd_cm=gsd_cm,
        sensor="rgb",
    )
    defaults.update(kwargs)
    return CampaignType(**defaults)


class TestInversion:
    """``height_from_gsd(gsd_from_height(h)) ≈ h`` across every profile."""

    @pytest.mark.parametrize("drone", ALL_DRONES, ids=lambda d: d.name)
    @pytest.mark.parametrize("height", [11.0, 25.0, 50.0, 80.0, 120.0])
    def test_roundtrip(self, drone, height):
        assert drone.height_from_gsd(drone.gsd_from_height(height)) == pytest.approx(
            height
        )

    @pytest.mark.parametrize("drone", ALL_DRONES, ids=lambda d: d.name)
    def test_resolve_lands_on_the_ideal_altitude_when_unclamped(self, drone):
        cfg = SeasonConfig()
        ct = _type(drone.gsd_from_height(60.0))
        res = gsd.resolve(ct, drone, cfg)
        assert res.altitude_m == pytest.approx(60.0, abs=0.1)
        assert res.clamped is False
        assert res.ok


class TestSanityAnchors:
    """Spec §5: M3M RGB 1.34 cm @ 50 m, MS 2.14 cm @ 50 m."""

    def test_rgb_anchor(self):
        assert M3M_RGB.gsd_from_height(50.0) == pytest.approx(1.34, abs=0.01)

    def test_ms_anchor(self):
        assert M3M_MS.gsd_from_height(50.0) == pytest.approx(2.14, abs=0.01)

    @pytest.mark.parametrize(
        "required_cm,expected_m",
        [(0.5, 19), (0.8, 30), (2.5, 93), (3.0, 112)],
    )
    def test_rgb_altitudes_match_the_spec_table(self, required_cm, expected_m):
        assert M3M_RGB.height_from_gsd(required_cm) == pytest.approx(expected_m, abs=1)

    @pytest.mark.parametrize("required_cm,expected_m", [(2.0, 47), (3.0, 70)])
    def test_ms_altitudes_match_the_spec_table(self, required_cm, expected_m):
        assert M3M_MS.height_from_gsd(required_cm) == pytest.approx(expected_m, abs=1)


class TestUnreachable:
    def test_floor_forcing_a_coarser_gsd_flags_rather_than_raises(self):
        """The spec is explicit: a flag, not an exception."""
        cfg = SeasonConfig(min_altitude_m=20.0)
        res = gsd.resolve(_type(0.3), M3M_MS, cfg)
        assert "gsd_unreachable" in res.flags
        assert res.ok is False
        assert res.achieved_gsd_cm > 0.3
        assert any("cannot reach" in r for r in res.reasons)

    def test_unreachable_names_a_profile_that_can_do_it(self):
        # At a 10 m floor the MS optics need 7 m for 0.3 cm/px and cannot get
        # there; the RGB channel's longer focal length reaches it at 11 m.
        cfg = SeasonConfig(min_altitude_m=10.0)
        ct = _type(0.3)
        assert gsd.resolve(ct, M3M_MS, cfg).ok is False
        assert gsd.resolve(ct, M3M_RGB, cfg).ok is True
        assert gsd.suggest_profiles(ct, ALL_DRONES, cfg) == ["m3m"]

    def test_clamping_down_to_the_ceiling_is_not_a_failure(self):
        """A coarse requirement met from below yields finer imagery, not an error."""
        cfg = SeasonConfig()
        res = gsd.resolve(_type(5.0), M3M_MS, cfg, max_height_agl_m=110.0)
        assert res.ok
        assert res.clamped
        assert res.altitude_m == 110.0
        assert res.achieved_gsd_cm < 5.0
        assert any("Requirement met from below" in r for r in res.reasons)

    def test_no_profile_can_meet_an_absurd_requirement(self):
        cfg = SeasonConfig(min_altitude_m=10.0)
        assert gsd.suggest_profiles(_type(0.01), ALL_DRONES, cfg) == []


class TestCeilings:
    def test_eu_open_category_limit_is_never_exceeded(self):
        cfg = SeasonConfig()
        res = gsd.resolve(_type(20.0), M3M_MS, cfg, max_height_agl_m=200.0)
        assert res.altitude_m <= gsd.EU_OPEN_CATEGORY_MAX_AGL_M

    def test_uas_zone_cap_wins_when_it_is_the_tightest(self):
        cfg = SeasonConfig()
        res = gsd.resolve(
            _type(5.0), M3M_MS, cfg, max_height_agl_m=110.0, zone_cap_m=45.0
        )
        assert res.altitude_m == 45.0
        assert res.ceiling_m == 45.0


class TestLowAltitudeWorkload:
    def test_low_flight_is_flagged_with_the_workload_reason(self):
        cfg = SeasonConfig(low_altitude_warn_m=25.0, min_altitude_m=5.0)
        res = gsd.resolve(_type(0.4), M3M_RGB, cfg)  # ≈15 m
        assert "low_altitude_workload" in res.flags
        assert res.ok  # flyable, just expensive
        assert any("more obstacle exposure" in r for r in res.reasons)

    def test_normal_altitude_carries_no_workload_flag(self):
        res = gsd.resolve(_type(3.0), M3M_RGB, SeasonConfig())
        assert res.flags == []


class TestStripGeometry:
    def test_swath_and_line_spacing_follow_the_overlap(self):
        ct = _type(3.0, overlap_side=0.70)
        res = gsd.resolve(ct, M3M_RGB, SeasonConfig())
        assert res.swath_width_m == pytest.approx(
            res.altitude_m * M3M_RGB.sensor_w_mm / M3M_RGB.focal_length_mm, abs=0.2
        )
        assert res.line_spacing_m == pytest.approx(res.swath_width_m * 0.30, abs=0.2)

    def test_more_side_overlap_means_tighter_lines(self):
        cfg = SeasonConfig()
        loose = gsd.resolve(_type(3.0, overlap_side=0.60), M3M_RGB, cfg)
        tight = gsd.resolve(_type(3.0, overlap_side=0.85), M3M_RGB, cfg)
        assert tight.line_spacing_m < loose.line_spacing_m

    def test_speed_comes_from_the_drone_profile(self):
        ct = _type(3.0, overlap_front=0.80)
        res = gsd.resolve(ct, M3M_RGB, SeasonConfig())
        assert res.speed_ms == pytest.approx(
            M3M_RGB.auto_speed(res.altitude_m, 80), abs=0.01
        )

    def test_overlaps_are_carried_through_as_percentages(self):
        res = gsd.resolve(
            _type(3.0, overlap_front=0.80, overlap_side=0.75), M3M_RGB, SeasonConfig()
        )
        assert (res.overlap_front_pct, res.overlap_side_pct) == (80, 75)


class TestRealCampaignLibrary:
    """Every shipped campaign must be flyable by at least one shipped profile."""

    def test_every_campaign_type_is_reachable_by_some_profile(self, tables):
        cfg = SeasonConfig()
        for ct in tables.campaigns.values():
            assert gsd.suggest_profiles(ct, ALL_DRONES, cfg), (
                f"no M3M profile can deliver {ct.id} at {ct.required_gsd_cm} cm/px"
            )

    def test_weed_mapping_is_the_low_altitude_case(self, tables):
        ct = tables.campaign_type("weed_map_early")
        res = gsd.resolve(ct, M3M_RGB, SeasonConfig())
        assert "low_altitude_workload" in res.flags

    def test_multispectral_campaigns_sit_high(self, tables):
        ct = tables.campaign_type("canopy_peak")
        res = gsd.resolve(ct, M3M_MS, SeasonConfig())
        assert 60 <= res.altitude_m <= 120
