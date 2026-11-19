# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from swarmerz.app import (
    SwarmerzApp,
    _WorkspaceWindow,
    _bar,
    _clip,
    _compact,
    _duration,
    _phase_index,
)
from swarmerz.branding import BRAND, SLOGAN, render_brand_header
from swarmerz.backend import HunterOutcome, RunResult
from swarmerz.models import HunterPhase, HunterSpec, MissionPhase, MissionState
from swarmerz.themes import initial_theme_name


def test_dense_display_helpers() -> None:
    assert _compact(999) == "999"
    assert _compact(12_300) == "12.3k"
    assert _compact(2_500_000) == "2.5m"
    assert _duration(3661) == "01:01:01"
    assert _bar(2, 4, 6) == "━━━───"
    assert _clip("one   two three", 9) == "one two …"


def test_brand_header_unites_secorizon_and_swarmerz_on_the_left() -> None:
    rendered = fragment_list_to_text(
        render_brand_header([("class:soft", "STATUS")])
    )
    assert rendered.splitlines()[0].startswith(f"  {BRAND}  //")
    assert "SECORIZON - SWARMERZ  //  CONSENSUS IS THE EXPLOIT." in rendered
    assert BRAND == "SECORIZON - SWARMERZ"
    assert SLOGAN in rendered
    assert "OFFENSIVE PENTESTS" not in rendered
    assert "VULNERABILITY RESEARCH" not in rendered
    assert rendered.endswith("STATUS")


def test_mono_is_the_default_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWARMERZ_THEME", raising=False)

    assert initial_theme_name() == "mono"


def test_terminal_phases_map_to_final_vector_position() -> None:
    assert _phase_index(MissionPhase.READY) == 0
    assert _phase_index(MissionPhase.BASELINE) == 1
    assert _phase_index(MissionPhase.ANALYZING) == 4
    assert _phase_index(MissionPhase.VERIFYING) == 5
    assert _phase_index(MissionPhase.COMPLETE) == 6
    assert _phase_index(MissionPhase.DEGRADED) == 6
    assert _phase_index(MissionPhase.FAILED) == 6


def test_empty_activity_view_has_a_useful_idle_state() -> None:
    app = object.__new__(SwarmerzApp)
    app.state = MissionState(
        "test", "wait", (HunterSpec("alpha", "provider", "model", "high", 1),)
    )

    rendered = app._render_activity()

    assert fragment_list_to_text(rendered) == "\n  Waiting for signal…"


def test_codex_hunter_is_displayed_by_model_name_not_harness_id() -> None:
    app = object.__new__(SwarmerzApp)
    app.state = MissionState(
        "test", "wait", (
            HunterSpec(
                "codex", "openai", "gpt-daybreak-blue-latest", "xhigh", 4,
                "codex-exec",
            ),
        )
    )

    fleet = fragment_list_to_text(app._render_fleet())
    assert "DAYBREAK" in fleet
    assert "CODEX" not in fleet
    assert app.state.hunters["codex"].spec.label == "daybreak"


def test_claude_code_hunter_is_displayed_by_model_name_not_harness_id() -> None:
    app = object.__new__(SwarmerzApp)
    app.state = MissionState(
        "test", "wait", (
            HunterSpec(
                "claude", "anthropic", "claude-mythos-5-2", "xhigh", 4,
                "claude-code",
            ),
        )
    )

    fleet = fragment_list_to_text(app._render_fleet())
    assert "MYTHOS-5-2" in fleet
    assert app.state.hunters["claude"].spec.label == "mythos-5-2"


def test_workspace_mouse_wheel_routes_to_virtual_viewport() -> None:
    motion: list[int] = []
    window = _WorkspaceWindow(
        FormattedTextControl("content"),
        on_scroll=motion.append,
    )
    down = MouseEvent(
        Point(x=1, y=1),
        MouseEventType.SCROLL_DOWN,
        MouseButton.NONE,
        frozenset(),
    )
    up = MouseEvent(
        Point(x=1, y=1),
        MouseEventType.SCROLL_UP,
        MouseButton.NONE,
        frozenset(),
    )

    window._mouse_handler(down)
    window._mouse_handler(up)

    assert motion == [3, -3]


def test_final_result_reconciles_fleet_and_exposes_codex_follow_up() -> None:
    app = object.__new__(SwarmerzApp)
    app.state = MissionState(
        "test", "wait", (
            HunterSpec("alpha", "provider", "model-a", "high", 4),
            HunterSpec("beta", "provider", "model-b", "high", 4),
        )
    )
    app.state.hunters["alpha"].phase = HunterPhase.CONSOLIDATING
    app.state.hunters["beta"].phase = HunterPhase.HUNTING

    app._apply_result(RunResult(
        status="completed",
        run_dir=Path("/tmp/round"),
        final_answer="validated answer",
        degraded=True,
        hunter_outcomes=(
            HunterOutcome("alpha", "completed", True, 4, 4),
            HunterOutcome("beta", "completed", True, 3, 4, degraded=True),
        ),
        review_command="swarmerz chat --session test --round-id round-1",
        analyzer_model="gpt-5.6-sol",
    ))

    assert app.state.phase is MissionPhase.DEGRADED
    assert app.state.sealed_count == 2
    assert app.state.hunters["alpha"].phase is HunterPhase.SEALED
    assert app.state.hunters["beta"].phase is HunterPhase.SEALED
    assert app.state.hunters["beta"].terminal_workers == 3
    assert "F8" in app.state.notice

    app.state.view = "review"
    rendered = fragment_list_to_text(app._render_main())
    assert "GPT-5.6-SOL FOLLOW-UP" in rendered
    assert "/chat" in rendered
    assert app.state.review_command in rendered

    app.state.view = "synthesis"
    rendered = fragment_list_to_text(app._render_main())
    assert "GPT-5.6-SOL SYNTHESIS" in rendered


def test_historical_result_is_clearly_marked_read_only() -> None:
    app = object.__new__(SwarmerzApp)
    app.state = MissionState(
        "archive", "inspect", (
            HunterSpec("alpha", "provider", "model-a", "high", 1),
        )
    )

    app._apply_result(RunResult(
        status="completed",
        hunter_outcomes=(HunterOutcome("alpha", "completed", True),),
        historical=True,
    ))

    assert app.state.phase is MissionPhase.COMPLETE
    assert app.state.hunters["alpha"].phase is HunterPhase.SEALED
    assert app.state.hunters["alpha"].terminal_workers == 1
    assert "loaded read-only" in app.state.notice


def test_completed_archive_does_not_redraw_while_idle() -> None:
    class ArchiveBackend:
        active = False

        async def run(self) -> RunResult:
            return RunResult(status="completed", historical=True)

    async def exercise() -> None:
        specs = (HunterSpec("alpha", "provider", "model", "high", 1),)
        state = MissionState("archive", "inspect", specs)
        state.started_at = datetime(2026, 10, 1, tzinfo=UTC)
        state.finished_at = datetime(2026, 10, 2, tzinfo=UTC)

        with create_pipe_input() as pipe, create_app_session(
            input=pipe, output=DummyOutput()
        ):
            app = SwarmerzApp(state, lambda **_: ArchiveBackend())
            rendered = asyncio.Event()
            render_count = 0

            def after_render(_: object) -> None:
                nonlocal render_count
                render_count += 1
                rendered.set()

            app.application.after_render += after_render
            task = asyncio.create_task(app.run())
            try:
                await asyncio.wait_for(rendered.wait(), timeout=1)
                # Let the backend result trigger its one explicit repaint.
                await asyncio.sleep(0.1)
                settled_count = render_count
                await asyncio.sleep(1.1)
                assert render_count == settled_count
                assert app._ticker_task is None
                assert app.application.refresh_interval is None
            finally:
                app.application.exit()
                await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise())
