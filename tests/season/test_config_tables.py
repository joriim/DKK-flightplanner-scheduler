"""Crop and campaign library loading, and the replace-not-merge rule (spec §9)."""

from __future__ import annotations

import tomllib

import pytest

from flightmanager.season.config import (
    SeasonConfig,
    SeasonWeights,
    load_defaults_raw,
    load_season_config,
    load_season_tables,
)

_USER_CROP = """
[[crops]]
id = "my_barley"
label_fi = "Oma ohra"
label_en = "My barley"
base_temp_c = 5.0
model = "gdd"
[crops.stages]
sowing = 0
emergence = 120
[crops.provenance]
source = "farm records 2024-2026"
confidence = "medium"
calibrated_from = ["obs-1", "obs-2"]
"""

_USER_CAMPAIGN = """
[[campaigns]]
id = "my_flight"
label_fi = "Oma lento"
label_en = "My flight"
trigger_stage = "emergence"
required_gsd_cm = 2.0
"""


def _raw(text: str) -> dict:
    return tomllib.loads(text)


class TestBuiltIns:
    def test_the_shipped_library_loads(self, tables):
        assert len(tables.crops) == 5
        assert len(tables.campaigns) == 12
        assert tables.source.crops_from == "built-in"

    def test_every_spec_campaign_is_present(self, tables):
        expected = {
            "bare_soil_baseline",
            "emergence_count",
            "weed_map_early",
            "n_topdress_timing",
            "canopy_peak",
            "disease_watch",
            "anthesis_marker",
            "lodging_survey",
            "maturity_forecast",
            "preharvest_reference",
            "catch_crop_biomass",
            "s2_calibration",
        }
        assert set(tables.campaigns) == expected

    def test_every_crop_carries_the_appendix_b_placeholders(self, tables):
        """Spec §4.1: shipped thresholds must declare themselves provisional."""
        for crop in tables.crops.values():
            assert crop.provenance.confidence == "low"
            assert crop.provenance.calibrated_from == []
            assert crop.is_calibrated is False

    @pytest.mark.parametrize(
        "crop_id,stage,expected",
        [
            ("spring_barley", "emergence", 130),
            ("spring_barley", "harvest_ready", 1150),
            ("spring_wheat", "anthesis", 780),
            ("oats", "grain_fill_mid", 940),
            ("spring_rape", "emergence", 110),
        ],
    )
    def test_thresholds_match_appendix_b(self, tables, crop_id, stage, expected):
        assert tables.crop(crop_id).threshold(stage) == expected

    def test_every_crop_has_a_synthetic_sowing_stage(self, tables):
        for crop in tables.crops.values():
            assert crop.threshold("sowing") == 0

    def test_stage_thresholds_increase_monotonically(self, tables):
        for crop in tables.crops.values():
            values = [v for _, v in crop.ordered_stages()]
            assert values == sorted(values)

    def test_no_cross_reference_warnings_in_the_shipped_library(self, tables):
        assert tables.source.warnings == []

    def test_repeatable_types_declare_how_they_repeat(self, tables):
        for type_id in ("disease_watch", "maturity_forecast", "s2_calibration"):
            assert tables.campaign_type(type_id).repeat is not None

    def test_the_event_triggered_type_declares_its_threshold(self, tables):
        lodging = tables.campaign_type("lodging_survey")
        assert lodging.trigger_type == "weather_event"
        assert lodging.event_gust_ms > 0

    def test_satellite_calibration_requires_coincidence(self, tables):
        assert (
            tables.campaign_type("s2_calibration").satellite_coincidence == "required"
        )
        assert tables.campaign_type("s2_calibration").audience == "researcher"

    def test_defaults_file_parses_as_toml(self):
        raw = load_defaults_raw()
        assert {"crops", "campaigns"} <= set(raw)


class TestReplaceNotMerge:
    """The ``[[drones]]`` precedent: any user entry replaces the whole list."""

    def test_user_crops_replace_the_built_in_list_entirely(self):
        tables = load_season_tables(_raw(_USER_CROP))
        assert set(tables.crops) == {"my_barley"}
        assert "spring_barley" not in tables.crops

    def test_the_replacement_is_announced_loudly(self):
        tables = load_season_tables(_raw(_USER_CROP))
        warning = " ".join(tables.source.warnings)
        assert "REPLACED, not merged" in warning
        assert "[[drones]]" in warning

    def test_user_campaigns_replace_only_the_campaign_list(self):
        tables = load_season_tables(_raw(_USER_CAMPAIGN))
        assert set(tables.campaigns) == {"my_flight"}
        assert len(tables.crops) == 5  # crops untouched

    def test_the_source_summary_names_where_each_list_came_from(self):
        tables = load_season_tables(_raw(_USER_CROP))
        assert "config.toml" in tables.source.summary()
        assert "built-in" in tables.source.summary()

    def test_a_calibrated_user_crop_reports_as_calibrated(self):
        tables = load_season_tables(_raw(_USER_CROP))
        assert tables.crop("my_barley").is_calibrated
        assert tables.uncalibrated_crops == []


class TestValidation:
    def test_duplicate_crop_ids_are_refused(self):
        with pytest.raises(ValueError, match="duplicate crop"):
            load_season_tables(_raw(_USER_CROP + _USER_CROP))

    def test_a_campaign_naming_an_unknown_stage_warns(self):
        raw = _raw(
            _USER_CROP
            + """
[[campaigns]]
id = "bad"
label_fi = "x"
label_en = "x"
trigger_stage = "anthesis"
required_gsd_cm = 3.0
"""
        )
        tables = load_season_tables(raw)
        assert any(
            "anthesis" in w and "no configured crop" in w
            for w in tables.source.warnings
        )

    def test_an_unknown_campaign_field_is_rejected(self):
        with pytest.raises(ValueError):
            load_season_tables(
                _raw(
                    """
[[campaigns]]
id = "x"
label_fi = "x"
label_en = "x"
trigger_stage = "emergence"
required_gsd_cm = 3.0
typoed_field = 1
"""
                )
            )

    def test_a_cadence_type_without_a_repeat_block_is_refused(self):
        with pytest.raises(ValueError, match="repeat"):
            load_season_tables(
                _raw(
                    """
[[campaigns]]
id = "x"
label_fi = "x"
label_en = "x"
trigger_type = "cadence"
trigger_stage = "emergence"
required_gsd_cm = 3.0
"""
                )
            )

    def test_a_weather_event_type_without_a_gust_threshold_is_refused(self):
        with pytest.raises(ValueError, match="event_gust_ms"):
            load_season_tables(
                _raw(
                    """
[[campaigns]]
id = "x"
label_fi = "x"
label_en = "x"
trigger_type = "weather_event"
required_gsd_cm = 3.0
"""
                )
            )


class TestSeasonConfig:
    def test_the_season_table_is_read_from_the_document_root(self):
        cfg = load_season_config(
            _raw("[season]\nmax_field_day_hours = 8.5\ndefault_crop = 'oats'\n")
        )
        assert cfg.max_field_day_hours == 8.5
        assert cfg.default_crop == "oats"

    def test_weights_are_nested(self):
        cfg = load_season_config(_raw("[season.weights]\nwind = 0.5\n"))
        assert cfg.weights.wind == 0.5

    def test_all_zero_weights_are_refused(self):
        with pytest.raises(ValueError, match="must not all be zero"):
            SeasonWeights(
                days_from_target=0,
                wind=0,
                cloud=0,
                sun_elevation=0,
                precip=0,
                satellite_coincidence=0,
            )

    def test_an_unknown_season_key_is_rejected_not_ignored(self):
        with pytest.raises(ValueError):
            load_season_config(_raw("[season]\ntpyo = 1\n"))

    def test_a_missing_season_table_yields_defaults(self):
        assert load_season_config({}) == SeasonConfig()


class TestAudienceFilter:
    def test_researcher_campaigns_are_excluded_from_the_farmer_view(self, tables):
        farmer_ids = {c.id for c in tables.for_audience("farmer")}
        assert "s2_calibration" not in farmer_ids
        assert "emergence_count" in farmer_ids

    def test_both_tagged_campaigns_appear_in_every_view(self, tables):
        for audience in ("farmer", "researcher"):
            ids = {c.id for c in tables.for_audience(audience)}
            assert "canopy_peak" in ids  # audience = "both"

    def test_no_audience_means_everything(self, tables):
        assert len(tables.for_audience(None)) == len(tables.campaigns)
