"""Persistence for ``season_<year>.json`` — atomic, versioned, hand-diffable.

One file per folder per season, living beside the folder's jobs at
``<output_root>/<folder>/season_<year>.json``.  Deliberately a **new file type
at folder level**, not a mutation of any job's ``job_params.json`` (spec §2):
existing schemas are not touched and the season module stores no geometry.

Two properties this module is responsible for:

*Atomicity* — write to a sibling temp file, fsync, rename.  A crash mid-write
leaves the previous plan intact rather than a truncated one.  The host's
``job_store.write_json_atomic`` is the house implementation and is used when the
host package is importable; the local fallback is byte-for-byte the same dance
so behaviour does not depend on how the module was installed.

*Serialisation* — a season plan is edited by a CLI, a web UI and an MCP tool,
sometimes at once.  :func:`plan_lock` wraps the read-modify-write in a file
lock so the last writer merges rather than clobbers.  It is a lock on the
**plan file only**: the season module must never take the pipeline lock, which
is what keeps a season call from blocking on a running export (spec §2).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

from flightmanager.season.models import SCHEMA_VERSION, SeasonPlan

log = logging.getLogger(__name__)

_PLAN_RE = re.compile(r"^season_(\d{4})\.json$")


class SeasonStoreError(Exception):
    """Raised when a plan cannot be read, migrated or written."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def plan_filename(season: int) -> str:
    return f"season_{season}.json"


def plan_path(folder_dir: Path | str, season: int) -> Path:
    return Path(folder_dir) / plan_filename(season)


def list_seasons(folder_dir: Path | str) -> list[int]:
    """Seasons with a stored plan in *folder_dir*, newest first."""
    folder = Path(folder_dir)
    if not folder.is_dir():
        return []
    years = [
        int(m.group(1))
        for p in folder.iterdir()
        if (m := _PLAN_RE.match(p.name)) is not None
    ]
    return sorted(years, reverse=True)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _write_json_atomic(path: Path, obj: Any) -> None:
    """Prefer the host's implementation; fall back to an identical local one."""
    try:
        from flightmanager.storage.job_store import write_json_atomic

        write_json_atomic(path, obj)
        return
    except ImportError:
        pass

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def plan_lock(
    folder_dir: Path | str, season: int, timeout_s: float = 10.0
) -> Iterator[None]:
    """Serialise read-modify-write on one plan file.

    Uses ``filelock`` when available (it is a host dependency), so a CLI run and
    the web UI editing the same plan queue instead of racing.  Without it the
    context manager is a no-op: the atomic rename still guarantees the file is
    never *corrupt*, only that one of two simultaneous edits can be lost.
    """
    try:
        from filelock import FileLock, Timeout
    except ImportError:
        yield
        return

    lock_path = Path(folder_dir) / f".{plan_filename(season)}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=timeout_s)
    try:
        with lock:
            yield
    except Timeout as exc:
        raise SeasonStoreError(
            f"another process is editing {plan_filename(season)} in "
            f"{folder_dir} (waited {timeout_s:.0f}s)"
        ) from exc


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Bring an on-disk payload up to :data:`SCHEMA_VERSION`.

    v1 is the first shape, so there is nothing to migrate yet.  The dispatch is
    here from the start because the alternative — adding it when it is first
    needed — means the version that needs it is the one that cannot read the
    files already on disk.
    """
    version = int(raw.get("schema_version", 0))
    if version > SCHEMA_VERSION:
        raise SeasonStoreError(
            f"season plan is schema v{version}, but this build understands "
            f"v{SCHEMA_VERSION}. Upgrade flightmanager rather than letting an "
            f"older build rewrite (and truncate) a newer plan."
        )
    if version < 1:
        raw["schema_version"] = 1
    return raw


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def load_plan(folder_dir: Path | str, season: int) -> SeasonPlan | None:
    """Read a stored plan, or ``None`` when the folder has none for *season*."""
    path = plan_path(folder_dir, season)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SeasonStoreError(f"{path} is not valid JSON: {exc}") from exc
    try:
        return SeasonPlan.model_validate(migrate(raw))
    except SeasonStoreError:
        raise
    except Exception as exc:
        raise SeasonStoreError(
            f"{path} does not match the season schema: {exc}"
        ) from exc


def save_plan(folder_dir: Path | str, plan: SeasonPlan) -> Path:
    """Write *plan* atomically and return the path it landed at.

    Serialised in JSON mode so dates are ISO strings: the file has to stay
    readable and diffable by hand (spec §4.4), and a Python ``date`` repr in a
    JSON file is neither.
    """
    plan.schema_version = SCHEMA_VERSION
    plan.updated_at = _dt.datetime.now(tz=_dt.timezone.utc).isoformat(
        timespec="seconds"
    )
    path = plan_path(folder_dir, plan.season)
    _write_json_atomic(path, plan.model_dump(mode="json"))
    return path


def new_plan(folder: str, season: int) -> SeasonPlan:
    """An empty plan stamped with the current time."""
    now = _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")
    return SeasonPlan(
        schema_version=SCHEMA_VERSION,
        season=season,
        folder=folder,
        created_at=now,
        updated_at=now,
    )


def load_or_create(folder_dir: Path | str, folder: str, season: int) -> SeasonPlan:
    """Load the folder's plan for *season*, creating an empty one if absent."""
    return load_plan(folder_dir, season) or new_plan(folder, season)
