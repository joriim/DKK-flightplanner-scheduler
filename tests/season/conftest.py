"""Shared fixtures: synthetic weather, fake drones, and a fake host.

Nothing here touches the network or the host application.  The fake drone
mirrors the real ``DroneConfig`` camera maths exactly (same formulas, same M3M
constants), so the GSD tests check the module's *policy* against the host's
real optics without importing the host.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from flightmanager.season.config import SeasonConfig, load_season_tables
from flightmanager.season.integration import JobRef
from flightmanager.season.phenology import DailyTemp, TemperatureSeries

SOWING = _dt.date(2027, 5, 14)
TODAY = _dt.date(2027, 5, 20)


# ---------------------------------------------------------------------------
# Drones — same formulas as flightmanager.config.DroneConfig
# ---------------------------------------------------------------------------


@dataclass
class FakeDrone:
    """Camera maths lifted verbatim from the host's ``DroneConfig``."""

    name: str
    label: str
    focal_length_mm: float
    pixel_pitch_um: float
    image_width_px: int
    image_height_px: int
    min_capture_interval_s: float = 2.0
    battery_minutes: float = 28.0

    @property
    def sensor_h_mm(self) -> float:
        return self.image_height_px * self.pixel_pitch_um / 1000.0

    @property
    def sensor_w_mm(self) -> float:
        return self.image_width_px * self.pixel_pitch_um / 1000.0

    def height_from_gsd(self, gsd_cm: float) -> float:
        return (gsd_cm / 100) * self.focal_length_mm / (self.pixel_pitch_um / 1000)

    def gsd_from_height(self, height_m: float) -> float:
        return height_m * self.pixel_pitch_um / (self.focal_length_mm * 10)

    def auto_speed(self, altitude_m: float, overlap_front_pct: int) -> float:
        sensor_h_m = self.image_height_px * self.pixel_pitch_um * 1e-6
        footprint_m = altitude_m * sensor_h_m / (self.focal_length_mm * 1e-3)
        return (1 - overlap_front_pct / 100) * footprint_m / self.min_capture_interval_s


#: The real M3M profiles from the host's drones.toml.
M3M_RGB = FakeDrone(
    name="m3m",
    label="DJI Mavic 3 Multispectral — RGB channel",
    focal_length_mm=12.3,
    pixel_pitch_um=3.3,
    image_width_px=5280,
    image_height_px=3956,
    min_capture_interval_s=2.38,
)
M3M_MS = FakeDrone(
    name="m3m-ms",
    label="DJI Mavic 3 Multispectral — MS-limited GSD",
    focal_length_mm=7.06,
    pixel_pitch_um=3.02,
    image_width_px=2592,
    image_height_px=1944,
    min_capture_interval_s=1.868,
)

ALL_DRONES = [M3M_RGB, M3M_MS]


@pytest.fixture
def rgb_drone() -> FakeDrone:
    return M3M_RGB


@pytest.fixture
def ms_drone() -> FakeDrone:
    return M3M_MS


# ---------------------------------------------------------------------------
# Config and tables
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg() -> SeasonConfig:
    return SeasonConfig()


@pytest.fixture
def tables():
    return load_season_tables()


# ---------------------------------------------------------------------------
# Synthetic temperature series
# ---------------------------------------------------------------------------


def constant_series(
    start: _dt.date,
    days: int,
    mean_c: float = 15.0,
    *,
    archive_until: _dt.date | None = None,
    forecast_until: _dt.date | None = None,
    stale: bool = False,
) -> TemperatureSeries:
    """A flat series — the golden-file workhorse.

    At 15 °C with base 5 °C each day contributes exactly 10 °C·d, so a
    threshold of 130 lands on day 13 and the expected dates can be read off by
    hand instead of trusting the implementation to check itself.
    """
    out: list[DailyTemp] = []
    for i in range(days):
        day = start + _dt.timedelta(days=i)
        if archive_until is not None and day <= archive_until:
            source = "archive"
        elif forecast_until is not None and day <= forecast_until:
            source = "forecast"
        else:
            source = "normal"
        out.append(DailyTemp(day, mean_c, source))  # type: ignore[arg-type]
    return TemperatureSeries(days=out, lat=62.79, lon=22.84, stale=stale)


@pytest.fixture
def flat_series() -> TemperatureSeries:
    """15 °C every day: archive through TODAY, forecast +14 d, normal after."""
    return constant_series(
        SOWING,
        260,
        15.0,
        archive_until=TODAY,
        forecast_until=TODAY + _dt.timedelta(days=14),
    )


# ---------------------------------------------------------------------------
# Fake host
# ---------------------------------------------------------------------------


@dataclass
class FakeHost:
    """A :class:`HostContext` backed by a temp directory and a job list."""

    root: Path
    jobs: list[JobRef] = field(default_factory=list)
    drone_list: list[FakeDrone] = field(default_factory=lambda: list(ALL_DRONES))
    active: FakeDrone | None = field(default_factory=lambda: M3M_RGB)
    raw: dict = field(default_factory=dict)
    max_height_agl_m: float = 110.0
    forecast_url: str = "https://example.invalid/forecast"
    forecast_ttl_hours: float = 3.0
    #: Host weather settings the season module reads rather than duplicates.
    drone_wind_limit_ms: float | None = 10.0
    daytime_start_h: int = 6
    daytime_end_h: int = 18
    #: Job cards, keyed by path. Empty means "derive a minimal card per job".
    cards: dict[str, dict] = field(default_factory=dict)
    slots: list[dict] = field(default_factory=list)

    @property
    def output_dir(self) -> Path:
        return self.root

    @property
    def cache_dir(self) -> str:
        return str(self.root / "cache")

    def folder_dir(self, folder: str) -> Path:
        if not folder or "/" in folder or folder in (".", ".."):
            raise ValueError(f"unsafe folder name: {folder!r}")
        path = self.root / folder
        path.mkdir(parents=True, exist_ok=True)
        return path

    def jobs_in_folder(self, folder: str) -> list[JobRef]:
        return [j for j in self.jobs if j.folder == folder]

    def drones(self) -> list[FakeDrone]:
        return self.drone_list

    def active_drone(self) -> FakeDrone | None:
        return self.active

    def raw_config(self) -> dict:
        return self.raw

    def job_cards(self, folder: str) -> list[dict]:
        """Minimal cards derived from the job list, unless overridden."""
        out = []
        for index, job in enumerate(self.jobs_in_folder(folder)):
            if job.path in self.cards:
                out.append(self.cards[job.path])
                continue
            out.append(
                {
                    "path": job.path,
                    "name": job.name,
                    "sort_order": index,
                    "flight_time_min": 20.0,
                    "battery_count": 1,
                    "flight_ready": True,
                    "takeoff_point_4326": [job.lon, job.lat]
                    if job.has_position
                    else None,
                }
            )
        return out

    def day_slots(self, folder: str) -> list[dict]:
        return list(self.slots)

    def cluster_launch_sites(self, cards: list[dict]) -> list:
        return []


@pytest.fixture
def host(tmp_path: Path) -> FakeHost:
    """A folder ``kentta`` holding three parcels a few hundred metres apart."""
    jobs = [
        JobRef(
            path="kentta/5241087453",
            name="5241087453",
            folder="kentta",
            lat=62.79,
            lon=22.84,
        ),
        JobRef(
            path="kentta/5241087454",
            name="5241087454",
            folder="kentta",
            lat=62.80,
            lon=22.85,
        ),
        JobRef(
            path="kentta/5241087455",
            name="5241087455",
            folder="kentta",
            lat=62.78,
            lon=22.83,
        ),
    ]
    return FakeHost(root=tmp_path, jobs=jobs)


@pytest.fixture
def patched_series(monkeypatch, flat_series):
    """Make ``planner.recompute`` use the synthetic series instead of the network."""
    from flightmanager.season import weather_history as wx

    def fake_build_series(lat, lon, start, end, cfg, cache_dir, **kwargs):
        today = kwargs.get("today") or TODAY
        span = (end - start).days + 1
        return constant_series(
            start,
            max(span, 1),
            15.0,
            archive_until=today,
            forecast_until=today + _dt.timedelta(days=cfg.forecast_horizon_days),
        )

    monkeypatch.setattr(wx, "build_series", fake_build_series)
    return fake_build_series
