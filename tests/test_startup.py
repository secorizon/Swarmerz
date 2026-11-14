# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from prompt_toolkit.formatted_text import fragment_list_to_text

from swarmerz.backend import MissionRecord
from swarmerz.models import HunterSpec
from swarmerz.startup import StartupApp


def _mission() -> MissionRecord:
    return MissionRecord(
        session_id="lighthouse",
        round_id="round-1",
        status="completed",
        degraded=True,
        sealed_count=5,
        configured_count=6,
        started_at=datetime(2026, 10, 2, 12, tzinfo=UTC),
        completed_at=datetime(2026, 10, 2, 14, tzinfo=UTC),
        run_dir=Path("/tmp/lighthouse/round-1"),
        instruction="Review Lighthouse",
        specs=(HunterSpec("alpha", "provider", "model", "high", 1),),
    )


def test_startup_offers_new_and_previous_mission_paths() -> None:
    app = StartupApp((_mission(),))

    rendered = fragment_list_to_text(app._render_main())

    assert "START NEW MISSION" in rendered
    assert "LOAD PREVIOUS MISSION" in rendered


def test_startup_uses_shared_secorizon_branding() -> None:
    app = StartupApp(())

    rendered = fragment_list_to_text(app._render_header())

    assert "CONSENSUS IS THE EXPLOIT." in rendered
    assert "SECORIZON - SWARMERZ" in rendered
    assert "SECORIZON - SWARMERZ  //  CONSENSUS IS THE EXPLOIT." in rendered
    assert "OFFENSIVE PENTESTS" not in rendered


def test_archive_selection_returns_the_chosen_mission() -> None:
    mission = _mission()
    app = StartupApp((mission,))
    app.mode_index = 1
    with mock.patch.object(app.application, "exit") as exit_app:
        app._activate()
        assert app.screen == "history"
        assert app.result is None

        app._activate()

    assert app.result is not None
    assert app.result.mode == "load"
    assert app.result.mission is mission
    exit_app.assert_called_once_with()


def test_empty_archive_stays_safe_and_explains_why() -> None:
    app = StartupApp(())
    app.mode_index = 1

    app._activate()

    assert app.screen == "mode"
    assert app.result is None
    assert "No stored missions" in app.notice
