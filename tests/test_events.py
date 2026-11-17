# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from pathlib import Path

import pytest

from swarmerz.events import clean_line, ingest_progress, ingest_report
from swarmerz.models import EventLevel, HunterPhase, HunterSpec, MissionPhase, MissionState


def _state() -> MissionState:
    return MissionState(
        title="test",
        instruction="review it",
        specs=(
            HunterSpec("deepseek", "deepseek", "deepseek-v4.1-flash-1017", "max", 4, "opencode"),
            HunterSpec("codex", "openai", "gpt-5.6-sol", "xhigh", 4, "codex-exec"),
        ),
    )


def test_clean_line_removes_brand_and_terminal_sequences() -> None:
    raw = "\x1b[31m[Swarmerz] [deepseek] started: opencode\x1b[0m"
    assert clean_line(raw) == "[deepseek] started: opencode"


def test_worker_progress_updates_fleet_metrics() -> None:
    state = _state()
    ingest_progress(
        state,
        "[Swarmerz] [deepseek] workers: 2/4 terminal (2 succeeded); "
        "worker timeout in 1437s; output tokens 35,923 (+4,641)",
    )

    hunter = state.hunters["deepseek"]
    assert hunter.phase is HunterPhase.HUNTING
    assert hunter.terminal_workers == 2
    assert hunter.active_workers == 2
    assert hunter.timeout_remaining == 1437
    assert hunter.output_tokens == 35923
    assert hunter.token_delta == 4641
    assert state.events[-1].source == "deepseek"
    assert state.events[-1].level is EventLevel.INFO


@pytest.mark.parametrize("body", [
    "message: Working on the requested task.",
    "command (completed): pwd",
    "tool read_file (completed)",
])
def test_live_activity_advances_codex_past_starting(body: str) -> None:
    state = _state()
    ingest_progress(state, "[codex] started: codex-exec model")
    assert state.hunters["codex"].phase is HunterPhase.STARTING

    ingest_progress(state, f"[codex/live] {body}")

    hunter = state.hunters["codex"]
    assert hunter.phase is HunterPhase.HUNTING
    assert hunter.detail == "live activity received"
    assert hunter.last_event == body
    assert hunter.active_workers == hunter.terminal_workers == hunter.output_tokens == 0
    assert state.events[-1].live
    assert state.events[-1].source == "codex"


def test_multiline_codex_message_retains_source_and_cannot_fake_lifecycle_events() -> None:
    state = _state()
    ingest_progress(state, "[codex] started: codex-exec model")
    body = (
        "message: Reviewing the status messages:\n"
        "started: a quoted log; failed (example); quorum reached; "
        "completed and answer sealed; consolidating"
    )

    ingest_progress(state, f"[Swarmerz] [codex/live] {body}")

    hunter = state.hunters["codex"]
    assert hunter.phase is HunterPhase.HUNTING
    assert state.phase is MissionPhase.HUNTING
    assert not hunter.report_ready
    assert state.events[-1].source == "codex"
    assert state.events[-1].message == body


@pytest.mark.parametrize("phase", [
    HunterPhase.BLOCKED, HunterPhase.RECOVERABLE, HunterPhase.CONSOLIDATING, HunterPhase.SEALED,
    HunterPhase.PARTIAL, HunterPhase.FAILED,
])
def test_late_live_output_preserves_lifecycle_state(phase: HunterPhase) -> None:
    state = _state()
    hunter = state.hunters["codex"]
    hunter.phase = phase
    hunter.detail = "existing lifecycle detail"
    state.phase = MissionPhase.ANALYZING

    ingest_progress(state, "[codex/live] message: started: a quoted status")

    assert hunter.phase is phase
    assert hunter.detail == "existing lifecycle detail"
    assert state.phase is MissionPhase.ANALYZING


def test_codex_startup_warning_is_not_evidence_of_active_work() -> None:
    state = _state()
    ingest_progress(state, "[codex] started: codex-exec model")

    ingest_progress(
        state,
        "[codex/live] WARNING: Codex model catalog cache refresh failed; "
        "continuing with the configured model",
    )

    assert state.hunters["codex"].phase is HunterPhase.STARTING
    assert state.events[-1].level is EventLevel.WARNING


def test_codex_recovery_lifecycle_is_visible_and_can_return_to_hunting() -> None:
    state = _state()
    ingest_progress(state, "[gpt-5.6-sol] started: codex-exec model")
    ingest_progress(
        state,
        "[gpt-5.6-sol] Codex turn failed; recovery available while other hunters run. "
        "Use /to codex MESSAGE to resume the same thread.",
    )

    hunter = state.hunters["codex"]
    assert hunter.phase is HunterPhase.RECOVERABLE
    assert state.events[-1].level is EventLevel.WARNING
    assert state.notice.startswith("ATTENTION · codex:")

    ingest_progress(
        state,
        "[gpt-5.6-sol] resuming recoverable Codex thread from targeted operator instruction",
    )
    assert hunter.phase is HunterPhase.HUNTING
    assert "operator instruction" in hunter.detail


def test_permission_probe_timeout_is_warning_not_hunter_failure() -> None:
    state = _state()
    ingest_progress(
        state,
        "[kimi] permission-status probe unavailable: OpenCode permission status "
        "unavailable for ses_123: timed out",
    )

    assert state.events[-1].level is EventLevel.WARNING
    # Unknown sources stay visible but cannot corrupt a configured hunter card.
    assert state.failed_count == 0


def test_partial_notes_are_preserved_without_false_seal() -> None:
    state = _state()
    ingest_progress(state, "[deepseek] worker timeout reached; finalizing partial work")
    ingest_progress(
        state,
        "[deepseek] preserved unverified worker notes: /tmp/round/deepseek/partial-notes.md",
    )

    hunter = state.hunters["deepseek"]
    assert hunter.partial_notes is True
    assert hunter.phase is HunterPhase.PARTIAL
    assert hunter.report_ready is False


def test_unresolved_permission_request_becomes_visible_blocker() -> None:
    state = _state()
    ingest_progress(
        state,
        "[deepseek] workers blocked by 1 pending OpenCode permission request(s): "
        "['/outside/context']",
    )

    hunter = state.hunters["deepseek"]
    assert hunter.phase is HunterPhase.BLOCKED
    assert state.blocked_count == 1
    assert state.events[-1].level is EventLevel.WARNING
    assert state.notice.startswith("ATTENTION · deepseek:")


def test_sealed_report_and_round_completion() -> None:
    state = _state()
    ingest_report(state, "deepseek", "# report\nEvidence")
    ingest_progress(state, "[deepseek] completed and answer sealed")
    ingest_progress(state, "quorum reached with 1/2 sealed answers")
    assert state.phase is MissionPhase.QUORUM

    ingest_progress(state, "[primary analysis] comparing blind reports")
    assert state.phase is MissionPhase.ANALYZING
    assert state.events[-1].source == "primary"

    ingest_progress(state, "round completed; artifacts: /tmp/session/round-1")
    assert state.phase is MissionPhase.COMPLETE
    assert state.run_dir == Path("/tmp/session/round-1")
    assert state.hunters["deepseek"].phase is HunterPhase.SEALED
    assert state.reports["deepseek"].startswith("# report")


def test_historical_codex_analysis_label_remains_readable() -> None:
    state = _state()
    ingest_progress(state, "[codex analysis] comparing blind reports")
    assert state.phase is MissionPhase.ANALYZING
    assert state.events[-1].source == "primary"


def test_baseline_and_secondary_progress_have_distinct_mission_phases() -> None:
    state = _state()
    ingest_progress(state, "[baseline-first adjudication] started: openai/codex")
    assert state.phase is MissionPhase.BASELINE
    assert state.events[-1].source == "baseline"

    ingest_progress(state, "[secondary verification] started: independent/verifier")
    assert state.phase is MissionPhase.VERIFYING
    assert state.events[-1].source == "secondary"


def test_counted_opencode_completion_seals_card_and_is_terminal() -> None:
    state = _state()
    ingest_progress(
        state,
        "[deepseek] workers: 4/4 terminal (4 succeeded); worker timeout in 10s; "
        "output tokens 49,900 (+100)",
    )
    ingest_progress(
        state,
        "[deepseek] completed 4/4 workers and answer sealed",
    )

    hunter = state.hunters["deepseek"]
    assert hunter.phase is HunterPhase.SEALED
    assert hunter.terminal_workers == 4
    assert hunter.active_workers == 0

    # A delayed heartbeat cannot regress an authoritative terminal state.
    ingest_progress(
        state,
        "[deepseek] workers: 3/4 terminal (3 succeeded); worker timeout in 0s; "
        "output tokens 50,000 (+100)",
    )
    assert hunter.phase is HunterPhase.SEALED
    assert hunter.terminal_workers == 4


def test_degraded_counted_completion_keeps_true_worker_count() -> None:
    state = _state()
    ingest_progress(
        state,
        "[deepseek] completed 3/4 workers and answer sealed (degraded)",
    )

    hunter = state.hunters["deepseek"]
    assert hunter.phase is HunterPhase.SEALED
    assert hunter.terminal_workers == 3
    assert hunter.active_workers == 0
    assert "degraded worker coverage" in hunter.detail
