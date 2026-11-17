# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


def display_model_name(model: str) -> str:
    """Return a compact operator-facing model name without changing its ID."""
    leaf = model.rsplit("/", 1)[-1]
    return "daybreak" if "daybreak" in leaf.lower() else leaf


class MissionPhase(StrEnum):
    READY = "ready"
    SETUP = "setup"
    BASELINE = "baseline"
    HUNTING = "hunting"
    QUORUM = "quorum"
    ANALYZING = "analyzing"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    DEGRADED = "degraded"
    FAILED = "failed"


class HunterPhase(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    HUNTING = "hunting"
    BLOCKED = "blocked"
    RECOVERABLE = "recoverable"
    CONSOLIDATING = "consolidating"
    SEALED = "sealed"
    PARTIAL = "partial"
    FAILED = "failed"


class EventLevel(StrEnum):
    TRACE = "trace"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class HunterSpec:
    id: str
    provider: str
    model: str
    effort: str
    workers: int
    harness: str = ""

    @property
    def label(self) -> str:
        if self.provider == "ollama":
            return self.model
        if self.harness in {"codex", "codex-exec"}:
            return display_model_name(self.model)
        return self.id


@dataclass
class HunterState:
    spec: HunterSpec
    phase: HunterPhase = HunterPhase.QUEUED
    terminal_workers: int = 0
    active_workers: int = 0
    output_tokens: int = 0
    token_delta: int = 0
    timeout_remaining: int | None = None
    detail: str = "waiting for launch slot"
    last_event: str = ""
    report_ready: bool = False
    partial_notes: bool = False

    @property
    def progress(self) -> float:
        if self.spec.workers <= 0:
            return 0.0
        return min(1.0, self.terminal_workers / self.spec.workers)


@dataclass(frozen=True)
class MissionEvent:
    at: datetime
    source: str
    message: str
    level: EventLevel = EventLevel.INFO
    live: bool = False

    @classmethod
    def now(
        cls,
        source: str,
        message: str,
        *,
        level: EventLevel = EventLevel.INFO,
        live: bool = False,
    ) -> MissionEvent:
        return cls(datetime.now(UTC), source, message, level, live)


@dataclass
class MissionState:
    title: str
    instruction: str
    specs: tuple[HunterSpec, ...]
    phase: MissionPhase = MissionPhase.READY
    hunters: dict[str, HunterState] = field(init=False)
    events: deque[MissionEvent] = field(default_factory=lambda: deque(maxlen=4000))
    reports: dict[str, str] = field(default_factory=dict)
    final_answer: str = ""
    review_command: str = ""
    run_dir: Path | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    selected_index: int = 0
    view: str = "activity"
    filter_text: str = ""
    command_target: str = "all"
    notice: str = "Ready. Press F1 for the command map."
    analyzer_model: str = "judge"

    def __post_init__(self) -> None:
        self.hunters = {spec.id: HunterState(spec) for spec in self.specs}

    @property
    def selected(self) -> HunterState | None:
        if not self.hunters:
            return None
        values = list(self.hunters.values())
        self.selected_index %= len(values)
        return values[self.selected_index]

    @property
    def elapsed_seconds(self) -> int:
        if self.started_at is None:
            return 0
        end = self.finished_at or datetime.now(UTC)
        return max(0, int((end - self.started_at).total_seconds()))

    @property
    def sealed_count(self) -> int:
        return sum(h.phase == HunterPhase.SEALED for h in self.hunters.values())

    @property
    def failed_count(self) -> int:
        return sum(h.phase == HunterPhase.FAILED for h in self.hunters.values())

    @property
    def blocked_count(self) -> int:
        return sum(h.phase == HunterPhase.BLOCKED for h in self.hunters.values())

    @property
    def token_total(self) -> int:
        return sum(h.output_tokens for h in self.hunters.values())

    def resolve_hunter(self, label: str) -> HunterState | None:
        root = label.split("/", 1)[0]
        if root in self.hunters:
            return self.hunters[root]
        for hunter in self.hunters.values():
            if root in {hunter.spec.label, hunter.spec.model}:
                return hunter
        return None

    def select_next(self, direction: int) -> None:
        if self.hunters:
            self.selected_index = (self.selected_index + direction) % len(self.hunters)

    def add_event(
        self,
        source: str,
        message: str,
        *,
        level: EventLevel = EventLevel.INFO,
        live: bool = False,
    ) -> None:
        self.events.append(MissionEvent.now(source, message, level=level, live=live))
