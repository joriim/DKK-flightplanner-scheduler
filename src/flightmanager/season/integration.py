"""The seam between the season module and the host flight manager.

Coupling is deliberately narrow and one-directional (spec §11).  The season
module *reads* drone profiles, job manifests and the forecast/satellite
modules; it writes only its own plan file.  ``pipeline.py`` is never imported,
and **the pipeline lock is never taken** — that is the regression this seam
exists to prevent, so there is a test asserting no pipeline import happens.

Everything host-facing is behind :class:`HostContext`, which keeps the planner
testable without the host package installed: the tests pass a fake context, and
``FlightmanagerHost`` is the real one built from an ``AppConfig``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: Modules the season module must never pull in.  Importing ``pipeline`` is how
#: the pipeline lock would end up held by a season request.
FORBIDDEN_IMPORTS = ("flightmanager.pipeline",)


class HostUnavailable(RuntimeError):
    """Raised when a host-backed operation is attempted without the host."""


@dataclass
class JobRef:
    """One job as the season module sees it: a path and a centroid.

    No geometry is copied or stored — the season plan references jobs by path
    and re-reads the host's polygons when it needs a coordinate (spec §2).
    """

    path: str
    name: str
    folder: str | None = None
    lat: float | None = None
    lon: float | None = None

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


class HostContext(Protocol):
    """What the season planner needs from the surrounding application."""

    @property
    def output_dir(self) -> Path: ...

    @property
    def cache_dir(self) -> str: ...

    @property
    def max_height_agl_m(self) -> float: ...

    @property
    def forecast_url(self) -> str: ...

    @property
    def forecast_ttl_hours(self) -> float: ...

    @property
    def drone_wind_limit_ms(self) -> float | None: ...

    @property
    def daytime_start_h(self) -> int: ...

    @property
    def daytime_end_h(self) -> int: ...

    def folder_dir(self, folder: str) -> Path: ...

    def jobs_in_folder(self, folder: str) -> list[JobRef]: ...

    def drones(self) -> list[Any]: ...

    def active_drone(self) -> Any | None: ...

    def raw_config(self) -> dict[str, Any]: ...

    def job_cards(self, folder: str) -> list[dict[str, Any]]: ...

    def day_slots(self, folder: str) -> list[dict[str, Any]]: ...

    def cluster_launch_sites(self, cards: list[dict[str, Any]]) -> list[Any]: ...


# ---------------------------------------------------------------------------
# The real host
# ---------------------------------------------------------------------------


@dataclass
class FlightmanagerHost:
    """:class:`HostContext` backed by a live ``AppConfig``.

    Every host import is made inside a method, not at module scope, so this
    module imports cleanly on its own and a missing host surfaces as a clear
    :class:`HostUnavailable` at the point of use rather than an ImportError at
    start-up.
    """

    config: Any
    config_path: str | None = None
    _raw: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def output_dir(self) -> Path:
        return Path(self.config.output.output_dir).resolve()

    @property
    def cache_dir(self) -> str:
        return self.config.cache.cache_dir

    @property
    def max_height_agl_m(self) -> float:
        return float(self.config.flight.max_height_agl_m)

    @property
    def forecast_url(self) -> str:
        return self.config.weather.open_meteo_url

    @property
    def forecast_ttl_hours(self) -> float:
        return float(self.config.weather.cache_max_age_hours)

    @property
    def drone_wind_limit_ms(self) -> float | None:
        """The host's flyability ceiling. ``None`` means the host has none set."""
        limit = self.config.weather.drone_wind_limit_ms
        return float(limit) if limit is not None else None

    @property
    def daytime_start_h(self) -> int:
        """Start of the host's daytime window — one notion of "day" throughout."""
        return int(self.config.weather.daytime_start_h)

    @property
    def daytime_end_h(self) -> int:
        return int(self.config.weather.daytime_end_h)

    def folder_dir(self, folder: str) -> Path:
        """Resolve a folder name through the host's own traversal guard.

        ``resolve_folder_dir`` is the single choke point that keeps a folder
        name from escaping the output directory; the season API takes folder
        names straight off HTTP requests, so it must go through that guard and
        not build the path itself.
        """
        from flightmanager.storage.job_store import resolve_folder_dir

        if not folder:
            raise ValueError(
                "a season plan needs a named folder — the output root cannot "
                "hold one, because season_<year>.json is per folder"
            )
        return resolve_folder_dir(self.output_dir, folder)

    def jobs_in_folder(self, folder: str) -> list[JobRef]:
        """Jobs in *folder*, each with its polygon centroid in EPSG:4326."""
        from flightmanager.storage.job_store import (
            best_polygon,
            resolve_job_dir,
            scan_jobs,
        )

        out: list[JobRef] = []
        for group in scan_jobs(self.output_dir):
            if group.get("name") != folder:
                continue
            for card in group.get("jobs", []):
                path = card["path"]
                ref = JobRef(path=path, name=card.get("name") or path, folder=folder)
                try:
                    job_dir = resolve_job_dir(self.output_dir, path)[2]
                    geom = best_polygon(job_dir)
                except Exception as exc:  # a broken job must not fail the plan
                    log.warning("Could not read geometry for %s: %s", path, exc)
                    geom = None
                if geom:
                    lat, lon = _centroid(geom)
                    ref.lat, ref.lon = lat, lon
                out.append(ref)
        return sorted(out, key=lambda j: j.path)

    def drones(self) -> list[Any]:
        return list(self.config.drones)

    def active_drone(self) -> Any | None:
        try:
            return self.config.active_drone()
        except Exception:
            return None

    def job_cards(self, folder: str) -> list[dict[str, Any]]:
        """Full job cards for *folder*, with geometry.

        Carries what the field-day planner needs and the season plan
        deliberately does not store: takeoff point, survey polygon, flight
        order, estimated flight time, battery count and ``flight_ready``.
        """
        from flightmanager.storage.job_store import scan_jobs

        for group in scan_jobs(self.output_dir, with_polygon=True):
            if group.get("name") == folder:
                return list(group.get("jobs", []))
        return []

    def day_slots(self, folder: str) -> list[dict[str, Any]]:
        """The host's own satellite + weather day slots for this folder.

        Consumes ``build_forecast`` rather than reimplementing it (spec §7.1):
        the MGRS tile lookup, CelesTrak orbit propagation, clear-sky pass
        qualification and the "golden day" concept all already exist and are
        expensive to recompute. Returns an empty list when the forecast cannot
        be built — satellite coincidence then simply scores zero rather than
        failing the whole ranking.
        """
        from flightmanager.forecasting.forecast import build_forecast
        from flightmanager.storage.job_store import job_centroids, resolve_folder_dir

        centroids = job_centroids(self.output_dir, folder=folder)
        if not centroids:
            return []
        try:
            payload = build_forecast(
                centroids,
                self.config.satellites,
                self.config.weather,
                self.cache_dir,
                folder_dir=resolve_folder_dir(self.output_dir, folder),
            )
        except Exception as exc:
            log.warning("Could not build forecast for %s: %s", folder, exc)
            return []
        return list(payload.get("days", []))

    def cluster_launch_sites(self, cards: list[dict[str, Any]]) -> list[Any]:
        """Group jobs into launch sites using the host's own clustering."""
        from flightmanager.forecasting.launch_sites import cluster_jobs

        return list(cluster_jobs(cards))

    def raw_config(self) -> dict[str, Any]:
        """The parsed ``config.toml`` document, for the ``[[crops]]`` tables.

        ``AppConfig`` has no field for them — they are season-module tables that
        sit at the top level beside ``[[drones]]`` — so the file is re-read
        rather than reconstructed from the model.
        """
        if self._raw is not None:
            return self._raw
        import tomllib

        path = Path(self.config_path or "config.toml")
        if not path.exists():
            example = Path("config.example.toml")
            if example.exists():
                path = example
            else:
                self._raw = {}
                return self._raw
        with open(path, "rb") as fh:
            self._raw = tomllib.load(fh)
        return self._raw


def _centroid(geom: dict) -> tuple[float, float]:
    """(lat, lon) centroid of a GeoJSON geometry already in EPSG:4326."""
    from shapely.geometry import shape

    c = shape(geom).centroid
    return (c.y, c.x)


def load_host(config_path: str = "config.toml") -> FlightmanagerHost:
    """Build a host context from the application's config file."""
    try:
        from flightmanager.config import load_config
    except ImportError as exc:
        raise HostUnavailable(
            "the dkk-flightmanager package is not importable — the season "
            "module's engine works standalone, but its CLI, REST and MCP "
            "surfaces need the host application"
        ) from exc
    return FlightmanagerHost(config=load_config(config_path), config_path=config_path)
