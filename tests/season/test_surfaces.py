"""The three surfaces: CLI verbs, REST routes, MCP tools (spec §10).

Phases 1 and 2 ship a deliberately partial surface.  These tests pin what exists
*and* what does not: a stub that answers "not implemented" is worse than an
absent command, because ``--help`` then advertises something that cannot work.
"""

from __future__ import annotations

import json

import pytest

from flightmanager.season import mcp_tools
from flightmanager.season.cli import season_app

#: Phase 1 CLI verbs (spec §10.1) plus the two library inspectors.
PHASE_1_COMMANDS = {"init", "plan", "status", "crops", "campaigns", "windows"}

#: Phase 2 adds day-level scheduling and the calendar export.
PHASE_2_COMMANDS = {"next", "day", "export"}

#: Verbs that belong to Phases 3 and 4 and must NOT be advertised yet.
LATER_PHASE_COMMANDS = {"apply", "log", "calibrate"}


def _command_names(app) -> set[str]:
    return {c.name for c in app.registered_commands}


class TestCli:
    def test_phase_1_verbs_are_registered(self):
        assert PHASE_1_COMMANDS <= _command_names(season_app)

    def test_phase_2_verbs_are_registered(self):
        assert PHASE_2_COMMANDS <= _command_names(season_app)

    def test_later_phase_verbs_are_absent_not_stubbed(self):
        assert _command_names(season_app) & LATER_PHASE_COMMANDS == set()

    def test_every_command_has_help_text(self):
        for command in season_app.registered_commands:
            assert command.help or command.callback.__doc__, (
                f"season {command.name} has no help text"
            )


class TestMcpTools:
    """Spec §10.3 read tools; the write tools arrive with Phase 3/4."""

    @staticmethod
    def _registered() -> dict:
        registered: dict[str, object] = {}

        class FakeMcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn

                return decorator

        mcp_tools.register(FakeMcp())
        return registered

    def test_the_three_read_tools_register(self):
        registered: dict[str, object] = {}

        class FakeMcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn

                return decorator

        mcp_tools.register(FakeMcp())
        assert set(registered) == {
            "season_status",
            "campaign_detail",
            "crop_profiles",
            "season_opportunities",
            "season_day_plan",
        }

    def test_every_tool_documents_itself_for_the_assistant(self):
        registered: dict[str, object] = {}

        class FakeMcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn

                return decorator

        mcp_tools.register(FakeMcp())
        for name, fn in registered.items():
            assert fn.__doc__ and len(fn.__doc__) > 100, f"{name} is under-documented"

    def test_the_status_tool_warns_the_assistant_about_precision(self):
        """An assistant must not be able to quote a window without its band."""
        registered: dict[str, object] = {}

        class FakeMcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn

                return decorator

        mcp_tools.register(FakeMcp())
        doc = registered["season_status"].__doc__
        assert "UNCALIBRATED" in doc
        assert "never as a single" in doc

    def test_a_missing_host_is_reported_as_json_not_an_exception(self, monkeypatch):
        registered: dict[str, object] = {}

        class FakeMcp:
            def tool(self):
                def decorator(fn):
                    registered[fn.__name__] = fn
                    return fn

                return decorator

        mcp_tools.register(FakeMcp())

        def no_host():
            from flightmanager.season.integration import HostUnavailable

            raise HostUnavailable("host not importable")

        monkeypatch.setattr(mcp_tools, "_context", no_host)
        payload = json.loads(registered["crop_profiles"]())
        assert "error" in payload


class TestRestRouter:
    def test_the_phase_2_routes_are_mounted(self):
        pytest.importorskip("fastapi")
        from flightmanager.season.api import router

        paths = {r.path for r in router.routes}
        assert "/api/season/{folder}/opportunities" in paths
        assert "/api/season/{folder}/day/{date}" in paths
        assert "/api/season/{folder}/ics" in paths

    def test_the_phase_1_routes_are_mounted(self):
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        from flightmanager.season.api import router

        paths = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
        assert ("/api/season/{folder}", ("GET",)) in paths
        assert ("/api/season/{folder}", ("POST",)) in paths
        assert ("/api/season/{folder}/plan", ("POST",)) in paths
        assert ("/api/season/{folder}/status", ("GET",)) in paths

    def test_library_routes_sit_under_a_reserved_prefix(self):
        """A folder must never be able to shadow ``/crops``."""
        pytest.importorskip("fastapi")
        from flightmanager.season.api import router

        paths = {r.path for r in router.routes}
        assert "/api/season/-/crops" in paths
        assert "/api/season/-/campaign-types" in paths

    def test_no_route_streams(self):
        """Spec §10.2: these are fast; no SSE."""
        pytest.importorskip("fastapi")
        from flightmanager.season import api

        source = (api.__file__ and open(api.__file__, encoding="utf-8").read()) or ""
        assert "SSEResponse" not in source
        assert "EventSource" not in source


class TestPhase2ToolDocumentation:
    """The scheduling tools must not let an assistant quote a bare number."""

    def test_the_opportunities_tool_explains_a_zero_score(self):
        doc = TestMcpTools._registered()["season_opportunities"].__doc__
        assert "HARD GATE" in doc
        assert "Report the gate, not just the number" in doc

    def test_the_opportunities_tool_warns_about_the_latitude_limit(self):
        doc = TestMcpTools._registered()["season_opportunities"].__doc__
        assert "62.8" in doc and "30 deg" in doc

    def test_the_day_plan_tool_explains_deferral(self):
        doc = TestMcpTools._registered()["season_day_plan"].__doc__
        assert "never silently dropped" in doc
        assert "order_source" in doc

    def test_a_bad_date_is_reported_as_json(self, monkeypatch):
        import json

        tools = TestMcpTools._registered()
        payload = json.loads(tools["season_day_plan"]("kentta", "not-a-date"))
        assert "error" in payload
