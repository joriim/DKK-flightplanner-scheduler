"""The regression spec §13 predicts: a season call must not take the pipeline lock.

Season planning is cheap — no tiles, no orbit propagation, no export — and the
host runs a single-worker executor where a second job returns HTTP 409.  If the
season module ever reached into ``pipeline``, a ``season status`` request would
start queueing behind a running KMZ export, and the failure would look like a
mysterious hang rather than a design error.

The check is structural rather than behavioural on purpose: there is no way to
"accidentally" pass it, and it fails at the moment the import is added rather
than the first time someone exports a job while planning a season.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from flightmanager.season.integration import FORBIDDEN_IMPORTS

SEASON_DIR = Path(__file__).resolve().parents[2] / "src" / "flightmanager" / "season"

#: Names that would mean the module had reached for the host's job lock.
_LOCK_NAMES = {"_pipeline_guard", "job_lock", "active_job_id", "pipeline_lock"}


def _season_sources() -> list[Path]:
    return sorted(SEASON_DIR.glob("*.py"))


def _imported_modules(tree: ast.AST) -> set[str]:
    """Every module named by an import, at module scope or inside a function."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


def test_the_season_package_has_sources_to_check():
    assert _season_sources(), "no season sources found — the guard would be vacuous"


@pytest.mark.parametrize("path", _season_sources(), ids=lambda p: p.name)
def test_no_module_imports_the_pipeline(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = _imported_modules(tree)
    for forbidden in FORBIDDEN_IMPORTS:
        offending = {
            m for m in imported if m == forbidden or m.startswith(forbidden + ".")
        }
        assert not offending, (
            f"{path.name} imports {sorted(offending)}. The season module must never "
            f"reach into the pipeline: it would take the single-worker job lock and "
            f"a season request would queue behind a running export."
        )


@pytest.mark.parametrize("path", _season_sources(), ids=lambda p: p.name)
def test_no_module_touches_the_job_lock_by_name(path: Path):
    """Catches ``_st.job_lock`` and friends, which no import statement reveals."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert not (used & _LOCK_NAMES), (
        f"{path.name} references {sorted(used & _LOCK_NAMES)} — that is the host's "
        f"pipeline lock, which season code must not acquire."
    )


def test_importing_the_whole_package_does_not_pull_in_the_pipeline():
    """Belt and braces: nothing drags the pipeline in transitively either.

    Run in a subprocess rather than by purging ``sys.modules`` in-process:
    re-importing the package here would mint a second copy of every class, and
    ``except HostUnavailable`` elsewhere in the suite would stop matching.
    """
    script = textwrap.dedent(
        """
        import sys
        import flightmanager.season
        import flightmanager.season.campaigns
        import flightmanager.season.config
        import flightmanager.season.gsd
        import flightmanager.season.integration
        import flightmanager.season.phenology
        import flightmanager.season.planner
        import flightmanager.season.store
        import flightmanager.season.weather_history
        leaked = [m for m in sys.modules if m.startswith("flightmanager.pipeline")]
        print(",".join(leaked))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", (
        f"importing the season package pulled in {result.stdout.strip()}"
    )


def test_the_only_lock_is_the_plan_file_lock():
    """The season module's one lock is per plan file, not global."""
    source = (SEASON_DIR / "store.py").read_text(encoding="utf-8")
    assert "def plan_lock" in source
    assert ".lock" in source  # a sibling file beside the plan, not a global lock


class TestEngineStandsAlone:
    """The engine must import without the host package present at all."""

    @pytest.mark.parametrize(
        "module",
        [
            "flightmanager.season.models",
            "flightmanager.season.config",
            "flightmanager.season.phenology",
            "flightmanager.season.gsd",
            "flightmanager.season.campaigns",
            "flightmanager.season.store",
            "flightmanager.season.weather_history",
            "flightmanager.season.integration",
            "flightmanager.season.planner",
        ],
    )
    def test_module_imports_without_the_host(self, module: str):
        __import__(module)

    def test_host_imports_are_lazy_not_module_scope(self):
        """``integration`` must not import the host at module scope."""
        tree = ast.parse((SEASON_DIR / "integration.py").read_text(encoding="utf-8"))
        module_level = {
            alias.name if isinstance(node, ast.Import) else node.module
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        host_side = {
            m
            for m in module_level
            if m
            and m.startswith("flightmanager.")
            and not m.startswith("flightmanager.season")
        }
        assert host_side == set(), (
            f"integration.py imports {sorted(host_side)} at module scope; host "
            f"imports must stay inside functions so the engine works standalone."
        )
