# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from swarmerz.backend import (
    DemoBackend,
    HistoricalBackend,
    MissionRecord,
    SwarmerzBackend,
    _discover_missions,
    _hunter_outcomes,
    _review_hint_argv,
    _swarmerz_cli_executable,
)
from swarmerz.events import ingest_progress, ingest_report
from swarmerz.models import HunterSpec, MissionState


def test_demo_backend_exercises_the_real_event_pipeline() -> None:
    specs = (
        HunterSpec("alpha", "provider", "alpha-model", "high", 2, "opencode"),
        HunterSpec("beta", "provider", "beta-model", "max", 2, "codex-exec"),
    )
    state = MissionState("demo", "show it", specs)
    backend = DemoBackend(
        specs,
        speed=0.02,
        on_progress=lambda message: ingest_progress(state, message),
        on_report=lambda hunter, report: ingest_report(state, hunter, report),
    )

    result = asyncio.run(backend.run())

    assert result.status == "completed"
    assert result.final_answer.startswith("Demo synthesis")
    assert set(state.reports) == {"alpha", "beta"}
    assert state.sealed_count == 2
    assert state.token_total > 0
    assert backend.active is False


def test_demo_control_feedback_is_observable() -> None:
    specs = (HunterSpec("alpha", "provider", "model", "high", 1),)
    messages: list[str] = []
    backend = DemoBackend(
        specs,
        speed=0.02,
        on_progress=messages.append,
        on_report=lambda _hunter, _report: None,
    )

    outcome = asyncio.run(backend.send("check parser", "alpha"))

    assert outcome == ["alpha: delivered"]
    assert any("[control] alpha" in message for message in messages)


def test_manifest_outcomes_are_authoritative_for_terminal_fleet_state() -> None:
    manifest = {
        "partial_hunters": ["alpha"],
        "hunter_results": [
            {
                "hunter_id": "alpha",
                "status": "completed",
                "answer_sealed": True,
                "worker_completion": {"terminal": 3, "expected": 4, "complete": False},
            },
            {
                "hunter_id": "beta",
                "status": "failed",
                "answer_sealed": False,
                "worker_monitor": {"reason": "permission_required"},
            },
        ],
    }

    alpha, beta = _hunter_outcomes(manifest)

    assert alpha.answer_sealed
    assert alpha.terminal_workers == 3
    assert alpha.expected_workers == 4
    assert alpha.degraded
    assert beta.blocked


def test_replacement_attempt_does_not_render_more_than_configured_workers() -> None:
    manifest = {
        "hunter_results": [{
            "hunter_id": "alpha",
            "status": "completed",
            "answer_sealed": True,
            "worker_completion": {"terminal": 5, "expected": 4, "complete": True},
        }],
    }

    outcome = _hunter_outcomes(manifest)[0]

    assert outcome.terminal_workers == 4
    assert outcome.expected_workers == 4


def test_review_command_preserves_config_overrides_and_round_identity(
    tmp_path: Path,
) -> None:
    config = tmp_path / "swarm config.toml"
    manifest = {"session_id": "audit", "round_id": "round-1"}
    with mock.patch("swarmerz.backend._swarmerz_cli_executable", return_value="swarmerz"):
        argv = _review_hint_argv(config, ["analyzer.model=gpt-test"], manifest)

    assert argv == (
        "swarmerz", "chat", "--config", str(config.resolve()),
        "--set", "analyzer.model=gpt-test",
        "--session", "audit", "--round-id", "round-1",
    )


def test_review_launcher_stays_inside_the_swarmerz_product() -> None:
    executable = Path(_swarmerz_cli_executable())

    assert executable.name == "swarmerz"
    assert executable.parent.name == "bin"
    assert executable.parent.parent.name == "Swarmerz"


def test_open_review_resumes_after_first_success(tmp_path: Path) -> None:
    backend = SwarmerzBackend(
        config_path=tmp_path / "swarmerz.toml",
        instruction="review",
        context=None,
        session="audit",
        round_id="round-1",
        overrides=[],
        on_progress=lambda _: None,
        on_report=lambda _hunter, _report: None,
    )
    backend._review_argv = ("swarmerz", "chat", "--session", "audit")
    completed = mock.Mock(returncode=0)
    with mock.patch("swarmerz.backend.subprocess.run", return_value=completed) as run:
        assert "closed" in backend.open_review()
        assert "closed" in backend.open_review()

    assert run.call_args_list[0].args[0] == [
        "swarmerz", "chat", "--session", "audit",
    ]
    assert run.call_args_list[1].args[0] == [
        "swarmerz", "chat", "--session", "audit", "--resume",
    ]


def _write_stored_round(
    output_root: Path,
    *,
    session: str,
    round_id: str,
    completed_at: str,
) -> Path:
    run_dir = output_root / "sessions" / session / "rounds" / round_id
    (run_dir / "hunters" / "alpha").mkdir(parents=True)
    (run_dir / "final").mkdir()
    manifest = {
        "session_id": session,
        "round_id": round_id,
        "status": "completed",
        "degraded": False,
        "started_at": "2026-10-01T10:00:00+00:00",
        "completed_at": completed_at,
        "sealed_answer_count": 1,
        "configured_answer_count": 1,
        "hunters": [{
            "id": "alpha",
            "provider": "provider",
            "effective_model": "alpha-model",
            "reasoning_effort": "high",
            "workers": 2,
            "subagents": 2,
            "harness": "opencode",
        }],
        "hunter_results": [{
            "hunter_id": "alpha",
            "status": "completed",
            "answer_sealed": True,
            "worker_completion": {"terminal": 2, "expected": 2, "complete": True},
            "worker_monitor": {"snapshot": {"output_tokens": 1234}},
        }],
        "analyzer": {"harness": "codex-exec"},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "instruction.txt").write_text("Audit the target", encoding="utf-8")
    (run_dir / "hunters" / "alpha" / "answer.json").write_text(
        json.dumps({"answer": "# Alpha report\nEvidence"}), encoding="utf-8"
    )
    (run_dir / "final" / "analysis.json").write_text(
        json.dumps({"recommended_answer": "Validated synthesis"}), encoding="utf-8"
    )
    return run_dir


def test_archive_discovery_is_newest_first_and_uses_manifest_fleet(
    tmp_path: Path,
) -> None:
    _write_stored_round(
        tmp_path,
        session="older",
        round_id="round-1",
        completed_at="2026-10-01T11:00:00+00:00",
    )
    newest = _write_stored_round(
        tmp_path,
        session="newest",
        round_id="round-2",
        completed_at="2026-10-02T11:00:00+00:00",
    )
    malformed = tmp_path / "sessions" / "broken" / "rounds" / "round-x"
    malformed.mkdir(parents=True)
    (malformed / "manifest.json").write_text("[]", encoding="utf-8")

    missions = _discover_missions(tmp_path)

    assert [mission.session_id for mission in missions] == ["newest", "older"]
    assert missions[0].run_dir == newest.resolve()
    assert missions[0].instruction == "Audit the target"
    assert missions[0].specs == (
        HunterSpec("alpha", "provider", "alpha-model", "high", 2, "opencode"),
    )


def test_historical_backend_restores_artifacts_without_live_model_work(
    tmp_path: Path,
) -> None:
    run_dir = _write_stored_round(
        tmp_path,
        session="audit",
        round_id="round-1",
        completed_at="2026-10-02T11:00:00+00:00",
    )
    mission = MissionRecord(
        session_id="audit",
        round_id="round-1",
        status="completed",
        degraded=False,
        sealed_count=1,
        configured_count=1,
        started_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        completed_at=datetime(2026, 10, 2, 11, tzinfo=UTC),
        run_dir=run_dir,
        instruction="Audit the target",
        specs=(HunterSpec("alpha", "provider", "alpha-model", "high", 2, "opencode"),),
    )
    progress: list[str] = []
    reports: dict[str, str] = {}
    backend = HistoricalBackend(
        config_path=tmp_path / "swarmerz.toml",
        overrides=[],
        mission=mission,
        on_progress=progress.append,
        on_report=reports.__setitem__,
    )
    result = asyncio.run(backend.run())
    assert backend.active is False
    assert result.historical is True
    assert result.final_answer == "Validated synthesis"
    assert result.hunter_outcomes[0].output_tokens == 1234
    assert reports == {"alpha": "# Alpha report\nEvidence"}
    assert "no model work was started" in progress[0]
    assert "--session audit --round-id round-1" in result.review_command
