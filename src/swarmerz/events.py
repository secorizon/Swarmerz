# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import re
from pathlib import Path

from .models import (
    EventLevel,
    HunterPhase,
    HunterState,
    MissionPhase,
    MissionState,
)


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Strip the runtime prefix while keeping archived progress readable.
_BRAND = re.compile(r"^\[Swarmerz\]\s*")
_SOURCE = re.compile(r"^\[([^\]\r\n]+)\]\s*(.*)$", re.DOTALL)
_LIVE_ACTIVITY_PREFIXES = (
    "message:", "command (", "tool ", "web search:", "plan:",
    "step finished", "turn completed",
)
_WORKERS = re.compile(
    r"workers:\s*(\d+)/(\d+)\s+terminal.*?"
    r"worker timeout in\s*(\d+)s;\s*output tokens\s*([\d,]+)"
    r"(?:\s*\(([+-]?[\d,]+)\))?",
    re.IGNORECASE,
)
_SEALED = re.compile(
    r"^completed(?:\s+(\d+)/(\d+)\s+workers)?\s+and answer sealed"
    r"(?:\s+\(degraded\))?$",
    re.IGNORECASE,
)


def clean_line(line: str) -> str:
    return _BRAND.sub("", _ANSI.sub("", line)).strip()


def _level(message: str) -> EventLevel:
    lowered = message.lower()
    structural = lowered.split(":", 1)[0]
    if "failed (" in lowered or structural.startswith("error"):
        return EventLevel.ERROR
    if any(token in lowered for token in (
        "timed out", "timeout reached", "status is slow", "degraded", "partial",
        "warning", "rejected child", "unavailable", "missing hunters",
        "workers blocked", "unable to reject child", "repeatedly requested additional access",
        "permission request(s) pending", "recovery available",
    )):
        return EventLevel.WARNING
    if any(token in lowered for token in (
        "completed", "answer sealed", "ready", "valid and unchanged",
    )):
        return EventLevel.SUCCESS
    if "/live" in lowered or "tool " in lowered or "message:" in lowered:
        return EventLevel.TRACE
    return EventLevel.INFO


def ingest_progress(state: MissionState, raw: str) -> None:
    message = clean_line(raw)
    if not message:
        return
    source = "system"
    body = message
    live = False
    source_match = _SOURCE.match(message)
    if source_match:
        source_label, body = source_match.groups()
        live = "/live" in source_label
        if source_label.startswith(("primary analysis", "codex analysis")):
            source = "primary"
        elif source_label.startswith("baseline-first adjudication"):
            source = "baseline"
        elif source_label.startswith("secondary verification"):
            source = "secondary"
        elif source_label == "setup":
            source = "setup"
        else:
            hunter = state.resolve_hunter(source_label)
            source = hunter.spec.id if hunter else source_label.split("/", 1)[0]
            if hunter:
                _update_hunter(hunter, body, live=live)
                if hunter.phase in {HunterPhase.BLOCKED, HunterPhase.RECOVERABLE}:
                    state.notice = f"ATTENTION · {hunter.spec.id}: {_notice_text(body)}"

    # Live output contains model-authored text and command output. Lifecycle
    # phrases quoted there must not change the mission's structural state.
    if live:
        state.add_event(source, body, level=_level(message), live=True)
        return

    lowered = message.lower()
    if lowered.startswith("[setup]") or "preparing isolated workspaces" in lowered:
        state.phase = MissionPhase.SETUP
    if lowered.startswith("launching ") or "started:" in lowered:
        state.phase = MissionPhase.HUNTING
        if state.started_at is None:
            from datetime import UTC, datetime
            state.started_at = datetime.now(UTC)
    if lowered.startswith("[baseline-first adjudication]"):
        state.phase = MissionPhase.BASELINE
    if lowered.startswith(("[primary analysis]", "[codex analysis]")):
        state.phase = MissionPhase.ANALYZING
    if lowered.startswith("[secondary verification]"):
        state.phase = MissionPhase.VERIFYING
    if "quorum reached" in lowered:
        state.phase = MissionPhase.QUORUM
    if lowered.startswith("round completed"):
        state.phase = MissionPhase.COMPLETE
        state.run_dir = _artifact_path(message)
    elif lowered.startswith("round incomplete"):
        state.phase = MissionPhase.DEGRADED
        state.run_dir = _artifact_path(message)
    elif lowered.startswith("round failed"):
        state.phase = MissionPhase.FAILED
        state.run_dir = _artifact_path(message)
    state.add_event(source, body, level=_level(message), live=live)


def _artifact_path(message: str) -> Path | None:
    marker = "artifacts:"
    if marker not in message:
        return None
    candidate = message.split(marker, 1)[1].strip()
    return Path(candidate) if candidate else None


def _update_hunter(hunter: HunterState, body: str, *, live: bool = False) -> None:
    # Kept separate from event classification so model text cannot accidentally
    # turn a healthy hunter red merely by discussing an error string.
    lowered = body.lower()
    hunter.last_event = body
    if live:
        # Codex emits live messages without OpenCode's worker summaries. Those
        # messages prove activity, but do not provide worker counts or tokens.
        if (
            hunter.phase in {HunterPhase.QUEUED, HunterPhase.STARTING}
            and lowered.startswith(_LIVE_ACTIVITY_PREFIXES)
        ):
            hunter.phase = HunterPhase.HUNTING
            hunter.detail = "live activity received"
        return
    # Terminal lifecycle states are authoritative. A delayed worker heartbeat
    # must not move a sealed/failed card back to HUNTING or CONSOLIDATING.
    if hunter.phase in {
        HunterPhase.SEALED, HunterPhase.PARTIAL, HunterPhase.FAILED,
    }:
        return
    sealed_match = _SEALED.match(body)
    if sealed_match:
        terminal, expected = sealed_match.groups()
        if terminal is not None and expected is not None:
            hunter.terminal_workers = int(terminal)
        else:
            hunter.terminal_workers = max(hunter.terminal_workers, hunter.spec.workers)
        # Any unfinished workers have been abandoned by the time evidence is
        # sealed; a degraded 3/4 result must not look like one worker is live.
        hunter.active_workers = 0
        hunter.phase = HunterPhase.SEALED
        hunter.report_ready = True
        hunter.detail = (
            "sealed evidence ready for the judge (degraded worker coverage)"
            if "(degraded)" in lowered
            else "sealed evidence ready for the judge"
        )
        return
    worker_match = _WORKERS.search(body)
    if worker_match:
        terminal, total, timeout, tokens, delta = worker_match.groups()
        hunter.terminal_workers = int(terminal)
        hunter.active_workers = max(0, int(total) - int(terminal))
        hunter.timeout_remaining = int(timeout)
        hunter.output_tokens = int(tokens.replace(",", ""))
        hunter.token_delta = int(delta.replace(",", "")) if delta else 0
        hunter.phase = (
            HunterPhase.CONSOLIDATING
            if hunter.terminal_workers >= hunter.spec.workers
            else HunterPhase.HUNTING
        )
        hunter.detail = (
            f"{hunter.terminal_workers}/{hunter.spec.workers} workers terminal"
        )
    elif "started:" in lowered:
        hunter.phase = HunterPhase.STARTING
        hunter.detail = "parent online; opening worker fan-out"
    elif any(marker in lowered for marker in (
        "workers blocked by",
        "repeatedly requested additional access",
        "unable to reject child",
    )):
        hunter.phase = HunterPhase.BLOCKED
        hunter.detail = body
    elif "recovery available" in lowered:
        hunter.phase = HunterPhase.RECOVERABLE
        hunter.active_workers = 0
        hunter.detail = body
    elif "resuming recoverable" in lowered:
        hunter.phase = HunterPhase.HUNTING
        hunter.detail = "resuming the same harness session from operator instruction"
    elif "resuming opencode session" in lowered or "consolidat" in lowered:
        hunter.phase = HunterPhase.CONSOLIDATING
        hunter.detail = "reconciling worker evidence"
    elif "failed (" in lowered:
        hunter.phase = HunterPhase.FAILED
        hunter.active_workers = 0
        hunter.detail = body
    elif "preserved unverified worker notes" in lowered:
        hunter.partial_notes = True
        if hunter.phase is not HunterPhase.FAILED:
            hunter.phase = HunterPhase.PARTIAL
        hunter.detail = "unverified notes preserved"
    elif "worker timeout reached" in lowered:
        hunter.phase = HunterPhase.CONSOLIDATING
        hunter.detail = "deadline reached; preserving completed work"


def ingest_report(state: MissionState, hunter_id: str, report: str) -> None:
    state.reports[hunter_id] = report
    hunter = state.hunters.get(hunter_id)
    if hunter:
        hunter.report_ready = True
    state.add_event(
        hunter_id,
        f"standalone report captured ({len(report):,} characters)",
        level=EventLevel.SUCCESS,
    )


def _notice_text(message: str, width: int = 112) -> str:
    compact = " ".join(message.split())
    return compact if len(compact) <= width else compact[: width - 1] + "…"
