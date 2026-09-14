"""Persistence: atomicity, schema versioning, and concurrent edits (spec §13)."""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from pathlib import Path

import pytest

from flightmanager.season import store
from flightmanager.season.models import (
    SCHEMA_VERSION,
    Campaign,
    JobAssignment,
    SeasonPlan,
    Window,
)


def _plan(folder: str = "kentta", season: int = 2027) -> SeasonPlan:
    plan = store.new_plan(folder, season)
    plan.assignments.append(
        JobAssignment(
            job_path=f"{folder}/5241087453",
            crop_id="spring_barley",
            sowing_date=_dt.date(2027, 5, 14),
            sowing_date_source="farmer_reported",
        )
    )
    plan.campaigns.append(
        Campaign(
            campaign_id="2027-emergence_count-5241087453",
            type_id="emergence_count",
            season=season,
            job_paths=[f"{folder}/5241087453"],
            crop_id="spring_barley",
            state="open",
            window=Window(
                computed_at="2027-05-20T00:00:00+00:00",
                earliest=_dt.date(2027, 5, 24),
                target=_dt.date(2027, 5, 27),
                latest=_dt.date(2027, 6, 3),
                uncertainty_days=7.0,
            ),
        )
    )
    return plan


class TestRoundTrip:
    def test_save_then_load_is_lossless(self, tmp_path: Path):
        original = _plan()
        store.save_plan(tmp_path, original)
        loaded = store.load_plan(tmp_path, 2027)
        assert loaded.campaigns[0].window.target == _dt.date(2027, 5, 27)
        assert loaded.assignments[0].sowing_date == _dt.date(2027, 5, 14)
        assert loaded.schema_version == SCHEMA_VERSION

    def test_the_file_is_hand_readable_iso_dates(self, tmp_path: Path):
        """Spec §4.4: readable and diffable by hand — no Python date reprs."""
        store.save_plan(tmp_path, _plan())
        text = (tmp_path / "season_2027.json").read_text(encoding="utf-8")
        assert '"sowing_date": "2027-05-14"' in text
        assert "datetime.date" not in text
        json.loads(text)  # and it is valid JSON

    def test_missing_plan_returns_none(self, tmp_path: Path):
        assert store.load_plan(tmp_path, 2027) is None

    def test_load_or_create_makes_an_empty_plan(self, tmp_path: Path):
        plan = store.load_or_create(tmp_path, "kentta", 2027)
        assert plan.season == 2027 and plan.campaigns == []

    def test_path_and_filename_follow_the_spec(self, tmp_path: Path):
        assert store.plan_filename(2027) == "season_2027.json"
        assert store.plan_path(tmp_path, 2027) == tmp_path / "season_2027.json"

    def test_list_seasons_is_newest_first(self, tmp_path: Path):
        for year in (2025, 2027, 2026):
            store.save_plan(tmp_path, _plan(season=year))
        assert store.list_seasons(tmp_path) == [2027, 2026, 2025]

    def test_list_seasons_ignores_unrelated_files(self, tmp_path: Path):
        (tmp_path / "job_params.json").write_text("{}")
        (tmp_path / "season_notes.json").write_text("{}")
        store.save_plan(tmp_path, _plan())
        assert store.list_seasons(tmp_path) == [2027]


class TestAtomicity:
    def test_a_crash_mid_write_leaves_the_previous_plan_intact(
        self, tmp_path: Path, monkeypatch
    ):
        store.save_plan(tmp_path, _plan())
        before = (tmp_path / "season_2027.json").read_text(encoding="utf-8")

        def boom(*args, **kwargs):
            raise OSError("simulated crash during rename")

        monkeypatch.setattr(os, "replace", boom)
        updated = _plan()
        updated.campaigns[0].window.target = _dt.date(2027, 9, 9)
        with pytest.raises(OSError):
            store.save_plan(tmp_path, updated)

        # The old plan is still readable and unchanged, not truncated.
        assert (tmp_path / "season_2027.json").read_text(encoding="utf-8") == before
        assert store.load_plan(tmp_path, 2027).campaigns[0].window.target == _dt.date(
            2027, 5, 27
        )

    def test_no_temp_files_are_left_behind(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )
        with pytest.raises(OSError):
            store.save_plan(tmp_path, _plan())
        assert list(tmp_path.glob("*.tmp")) == []


class TestSchemaVersioning:
    def test_a_versionless_payload_is_migrated_to_v1(self):
        assert store.migrate({"season": 2027})["schema_version"] == 1

    def test_a_future_schema_is_refused_rather_than_truncated(self, tmp_path: Path):
        (tmp_path / "season_2027.json").write_text(
            json.dumps({"schema_version": 99, "season": 2027, "folder": "k"}),
            encoding="utf-8",
        )
        with pytest.raises(store.SeasonStoreError, match="schema v99"):
            store.load_plan(tmp_path, 2027)

    def test_save_stamps_the_current_version(self, tmp_path: Path):
        plan = _plan()
        plan.schema_version = 0
        store.save_plan(tmp_path, plan)
        assert plan.schema_version == SCHEMA_VERSION

    def test_save_refreshes_updated_at(self, tmp_path: Path):
        plan = _plan()
        plan.updated_at = "2000-01-01T00:00:00+00:00"
        store.save_plan(tmp_path, plan)
        assert plan.updated_at > "2001"


class TestCorruption:
    def test_invalid_json_names_the_file(self, tmp_path: Path):
        (tmp_path / "season_2027.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(store.SeasonStoreError, match="not valid JSON"):
            store.load_plan(tmp_path, 2027)

    def test_wrong_shape_is_reported_as_a_schema_mismatch(self, tmp_path: Path):
        (tmp_path / "season_2027.json").write_text(
            json.dumps({"schema_version": 1, "season": "not-a-year"}), encoding="utf-8"
        )
        with pytest.raises(store.SeasonStoreError, match="does not match"):
            store.load_plan(tmp_path, 2027)


class TestConcurrency:
    def test_simultaneous_edits_do_not_corrupt_the_plan(self, tmp_path: Path):
        """A CLI run and the UI editing the same plan must serialise."""
        store.save_plan(tmp_path, _plan())
        errors: list[Exception] = []

        def add_note(index: int) -> None:
            try:
                for _ in range(8):
                    with store.plan_lock(tmp_path, 2027, timeout_s=20):
                        plan = store.load_plan(tmp_path, 2027)
                        plan.log("plan", detail=f"writer {index}")
                        store.save_plan(tmp_path, plan)
            except Exception as exc:  # pragma: no cover - only on a real failure
                errors.append(exc)

        threads = [threading.Thread(target=add_note, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        final = store.load_plan(tmp_path, 2027)  # still parses
        assert len(final.recompute_log) == 32  # no write was lost

    def test_the_lock_file_sits_beside_the_plan(self, tmp_path: Path):
        with store.plan_lock(tmp_path, 2027):
            pass
        assert (tmp_path / ".season_2027.json.lock").exists()


class TestRecomputeLog:
    def test_the_log_is_capped_so_the_file_stays_readable(self):
        plan = _plan()
        for i in range(400):
            plan.log("plan", detail=str(i))
        assert len(plan.recompute_log) == 200
        assert plan.recompute_log[-1].detail == "399"  # newest kept
