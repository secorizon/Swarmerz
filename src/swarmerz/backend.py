# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol

from .models import HunterSpec
from .orchestration.config import load_config
from .orchestration.live_control import WRAP_UP
from .orchestration.runner import AuditRunner


ProgressCallback = Callable[[str], None]
ReportCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class HunterOutcome:
    hunter_id: str
    status: str
    answer_sealed: bool
    terminal_workers: int | None = None
    expected_workers: int | None = None
    degraded: bool = False
    partial_notes: bool = False
    blocked: bool = False
    output_tokens: int | None = None


@dataclass(frozen=True)
class RunResult:
    status: str
    run_dir: Path | None = None
    final_answer: str = ""
    degraded: bool = False
    hunter_outcomes: tuple[HunterOutcome, ...] = ()
    review_command: str = ""
    historical: bool = False
    analyzer_model: str = ""


@dataclass(frozen=True)
class MissionRecord:
    session_id: str
    round_id: str
    status: str
    degraded: bool
    sealed_count: int
    configured_count: int
    started_at: datetime | None
    completed_at: datetime | None
    run_dir: Path
    instruction: str
    specs: tuple[HunterSpec, ...]

    @property
    def display_time(self) -> str:
        moment = self.completed_at or self.started_at
        return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else "unknown time"


class Backend(Protocol):
    active: bool

    async def run(self) -> RunResult: ...

    async def send(self, message: str, target: str | None = None) -> list[str]: ...

    async def wrap(self, target: str | None = None) -> list[str]: ...

    def status(self) -> str: ...

    def open_review(self) -> str: ...


class SwarmerzBackend:
    def __init__(
        self,
        *,
        config_path: Path,
        instruction: str,
        context: str | None,
        session: str | None,
        round_id: str | None,
        overrides: list[str],
        on_progress: ProgressCallback,
        on_report: ReportCallback,
        action: str = "run",
        adopt_late_answers: bool = False,
    ) -> None:
        self.config_path = config_path
        self.instruction = instruction
        self.context = context
        self.session = session
        self.round_id = round_id
        self.overrides = overrides
        self.on_progress = on_progress
        self.on_report = on_report
        self.action = action
        self.adopt_late_answers = adopt_late_answers
        self.runner: AuditRunner | None = None
        self.active = False
        self._review_argv: tuple[str, ...] = ()
        self._review_started = False

    async def run(self) -> RunResult:
        config = load_config(str(self.config_path), self.overrides)
        if self.action == "analyze":
            self.runner = AuditRunner(
                config,
                "",
                session_id=self.session,
                round_id=self.round_id,
                progress=self.on_progress,
            )
        else:
            self.runner = AuditRunner(
                config,
                self.instruction,
                context_override=self.context,
                session_id=self.session,
                round_id=self.round_id,
                progress=self.on_progress,
                hunter_report=self.on_report,
                interactive=True,
            )
        self.active = True
        try:
            if self.action == "analyze":
                run_dir = await self.runner.analyze_existing(
                    adopt_late_answers=self.adopt_late_answers
                )
            else:
                run_dir = await self.runner.run()
        finally:
            self.active = False
        manifest = _read_json(run_dir / "manifest.json")
        final_answer = ""
        analysis_path = run_dir / "final" / "analysis.json"
        if analysis_path.is_file():
            analysis = _read_json(analysis_path)
            final_answer = str(analysis.get("recommended_answer") or "")
        review_command = ""
        if (
            manifest.get("status") == "completed"
            and config.analyzer.harness in {"codex", "codex-exec"}
            and not config.analyzer.command
        ):
            self._review_argv = _review_hint_argv(
                self.config_path, self.overrides, manifest
            )
            review_command = shlex.join(self._review_argv)
        return RunResult(
            status=str(manifest.get("status") or "unknown"),
            run_dir=run_dir,
            final_answer=final_answer,
            degraded=bool(manifest.get("degraded")),
            hunter_outcomes=_hunter_outcomes(manifest),
            review_command=review_command,
            analyzer_model=config.analyzer.effective_model,
        )

    async def send(self, message: str, target: str | None = None) -> list[str]:
        if self.runner is None or getattr(self.runner, "live_control", None) is None:
            return ["control transport is not ready"]
        return await self.runner.live_control.send(message, target)

    async def wrap(self, target: str | None = None) -> list[str]:
        return await self.send(WRAP_UP, target)

    def status(self) -> str:
        if self.runner is None or getattr(self.runner, "live_control", None) is None:
            return "core is starting"
        return self.runner.live_control.status()

    def open_review(self) -> str:
        if not self._review_argv:
            return "Judge follow-up is unavailable for this analyzer."
        argv = list(self._review_argv)
        if self._review_started:
            argv.append("--resume")
        try:
            result = subprocess.run(argv, check=False)
        except OSError as exc:
            return f"Could not open judge review: {exc}"
        if result.returncode == 0:
            self._review_started = True
            return "Judge review closed. Type /chat again to resume it."
        return f"Judge review exited with status {result.returncode}."


class HistoricalBackend:
    """Load one immutable round from disk without launching orchestration work."""

    def __init__(
        self,
        *,
        config_path: Path,
        overrides: list[str],
        mission: MissionRecord,
        on_progress: ProgressCallback,
        on_report: ReportCallback,
    ) -> None:
        self.config_path = config_path
        self.overrides = overrides
        self.mission = mission
        self.on_progress = on_progress
        self.on_report = on_report
        self.active = False
        self._review_argv: tuple[str, ...] = ()
        self._review_started = False

    async def run(self) -> RunResult:
        manifest = _read_json(self.mission.run_dir / "manifest.json")
        if (
            manifest.get("session_id") != self.mission.session_id
            or manifest.get("round_id") != self.mission.round_id
        ):
            raise ValueError("stored mission identity changed after archive selection")
        self.on_progress(
            f"[archive] loaded {self.mission.session_id}/{self.mission.round_id}; "
            "no model work was started"
        )
        for spec in self.mission.specs:
            answer_path = self.mission.run_dir / "hunters" / spec.id / "answer.json"
            if not answer_path.is_file():
                continue
            answer = _read_json(answer_path)
            report = answer.get("answer")
            if isinstance(report, str):
                self.on_report(spec.id, report)

        final_answer = ""
        analysis_path = self.mission.run_dir / "final" / "analysis.json"
        if analysis_path.is_file():
            analysis = _read_json(analysis_path)
            final_answer = str(analysis.get("recommended_answer") or "")

        review_command = ""
        analyzer = manifest.get("analyzer")
        analyzer_model = (
            str(
                analyzer.get("effective_model")
                or analyzer.get("model_snapshot")
                or analyzer.get("requested_model")
                or "judge"
            )
            if isinstance(analyzer, dict)
            else "judge"
        )
        if (
            manifest.get("status") == "completed"
            and isinstance(analyzer, dict)
            and analyzer.get("harness") in {"codex", "codex-exec"}
        ):
            self._review_argv = _review_hint_argv(
                self.config_path, self.overrides, manifest
            )
            review_command = shlex.join(self._review_argv)
        return RunResult(
            status=str(manifest.get("status") or "unknown"),
            run_dir=self.mission.run_dir,
            final_answer=final_answer,
            degraded=bool(manifest.get("degraded")),
            hunter_outcomes=_hunter_outcomes(manifest),
            review_command=review_command,
            historical=True,
            analyzer_model=analyzer_model,
        )

    async def send(self, message: str, target: str | None = None) -> list[str]:
        return ["loaded mission is read-only; no active workers"]

    async def wrap(self, target: str | None = None) -> list[str]:
        return ["loaded mission is already terminal"]

    def status(self) -> str:
        return "historical mission loaded read-only; no model work is active"

    def open_review(self) -> str:
        if not self._review_argv:
            return "Judge follow-up is unavailable for this stored mission."
        argv = list(self._review_argv)
        if self._review_started:
            argv.append("--resume")
        try:
            result = subprocess.run(argv, check=False)
        except OSError as exc:
            return f"Could not open judge review: {exc}"
        if result.returncode == 0:
            self._review_started = True
            return "Judge review closed. Type /chat again to resume it."
        return f"Judge review exited with status {result.returncode}."


class DemoBackend:
    def __init__(
        self,
        specs: tuple[HunterSpec, ...],
        *,
        on_progress: ProgressCallback,
        on_report: ReportCallback,
        speed: float = 0.22,
    ) -> None:
        self.specs = specs
        self.on_progress = on_progress
        self.on_report = on_report
        self.speed = max(0.02, speed)
        self.active = False

    async def _emit(self, message: str, delay: float = 1.0) -> None:
        self.on_progress(message)
        await asyncio.sleep(self.speed * delay)

    async def run(self) -> RunResult:
        self.active = True
        try:
            await self._emit("[setup] freezing context snapshot", 2)
            await self._emit("[setup] validating harness and provider routes", 2)
            await self._emit("[baseline-first adjudication] inspecting frozen context", 2)
            await self._emit("[baseline-first adjudication] completed")
            await self._emit(f"launching {len(self.specs)} independent parent swarms")
            for spec in self.specs:
                await self._emit(
                    f"[{spec.label}] started: {spec.harness} {spec.model} "
                    f"effort={spec.effort}"
                )
            max_workers = max(spec.workers for spec in self.specs)
            token_totals = {spec.id: 0 for spec in self.specs}
            for terminal in range(max_workers + 1):
                for index, spec in enumerate(self.specs):
                    done = min(terminal, spec.workers)
                    delta = 900 + index * 271 + terminal * 113
                    token_totals[spec.id] += delta
                    await self._emit(
                        f"[{spec.label}] workers: {done}/{spec.workers} terminal "
                        f"({done} succeeded); worker timeout in {max(0, 1800-terminal*211)}s; "
                        f"output tokens {token_totals[spec.id]} (+{delta})",
                        0.35,
                    )
                if terminal == 1 and self.specs:
                    await self._emit(
                        f"[{self.specs[0].label}/live] tool shell (completed): "
                        "focused source reconciliation"
                    )
            for spec in self.specs:
                await self._emit(
                    f"[{spec.label}] resuming OpenCode session to consolidate completed work"
                )
                report = (
                    f"# {spec.id} report\n\n"
                    f"Independent {spec.model} workstream completed. The report is sealed, "
                    "evidence-linked, and ready for blind adjudication.\n\n"
                    "## Strongest lead\n\nA bounded state transition deserves manual validation.\n"
                )
                self.on_report(spec.id, report)
                await self._emit(f"[{spec.label}] completed and answer sealed")
            await self._emit(
                f"quorum reached with {len(self.specs)}/{len(self.specs)} sealed answers"
            )
            await self._emit("[primary analysis] comparing blind reports and validating findings", 4)
            await self._emit("[primary analysis] completed")
            await self._emit("[secondary verification] optional verifier not configured")
            demo_artifacts = Path.cwd().resolve() / "swarm-runs" / "demo"
            await self._emit(f"round completed; artifacts: {demo_artifacts}")
            answer = (
                "Demo synthesis: the independent workstreams converged on one bounded lead. "
                "The evidence is sufficient for targeted reproduction, but not yet for a "
                "high-severity claim."
            )
            return RunResult("completed", demo_artifacts, answer)
        finally:
            self.active = False

    async def send(self, message: str, target: str | None = None) -> list[str]:
        destination = target or "all"
        await self._emit(f"[control] {destination}: delivered demo instruction", 0.2)
        return [f"{destination}: delivered"]

    async def wrap(self, target: str | None = None) -> list[str]:
        return await self.send("wrap up", target)

    def status(self) -> str:
        return "demo transports ready" if self.active else "demo complete"

    def open_review(self) -> str:
        return "Judge follow-up is unavailable in demo mode."


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return payload


def discover_missions(
    config_path: Path, overrides: list[str]
) -> tuple[MissionRecord, ...]:
    config = load_config(str(config_path), overrides)
    return _discover_missions(Path(config.output_root))


def _discover_missions(output_root: Path) -> tuple[MissionRecord, ...]:
    records: list[MissionRecord] = []
    sessions_root = output_root / "sessions"
    if not sessions_root.is_dir():
        return ()
    for manifest_path in sessions_root.glob("*/rounds/*/manifest.json"):
        try:
            manifest = _read_json(manifest_path)
            session_id = str(manifest["session_id"])
            round_id = str(manifest["round_id"])
            run_dir = manifest_path.parent.resolve()
            expected = (
                sessions_root / session_id / "rounds" / round_id
            ).resolve()
            if run_dir != expected:
                continue
            hunters = manifest.get("hunters")
            if not isinstance(hunters, list):
                continue
            specs = tuple(
                _historical_spec(item) for item in hunters if isinstance(item, dict)
            )
            if not specs:
                continue
            instruction_path = run_dir / "instruction.txt"
            instruction = (
                instruction_path.read_text(encoding="utf-8").strip()
                if instruction_path.is_file() else ""
            )
            records.append(MissionRecord(
                session_id=session_id,
                round_id=round_id,
                status=str(manifest.get("status") or "unknown"),
                degraded=bool(manifest.get("degraded")),
                sealed_count=int(manifest.get("sealed_answer_count") or 0),
                configured_count=int(
                    manifest.get("configured_answer_count") or len(specs)
                ),
                started_at=_parse_datetime(manifest.get("started_at")),
                completed_at=_parse_datetime(manifest.get("completed_at")),
                run_dir=run_dir,
                instruction=instruction,
                specs=specs,
            ))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    records.sort(
        key=lambda item: item.completed_at or item.started_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return tuple(records)


def _historical_spec(record: dict[str, object]) -> HunterSpec:
    workers = max(int(record.get("workers") or 1), int(record.get("subagents") or 0))
    return HunterSpec(
        id=str(record.get("id") or "unknown"),
        provider=str(record.get("provider") or "unknown"),
        model=str(
            record.get("effective_model")
            or record.get("model_snapshot")
            or record.get("requested_model")
            or "unknown"
        ),
        effort=str(record.get("reasoning_effort") or "default"),
        workers=max(1, workers),
        harness=str(record.get("harness") or "unknown"),
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _swarmerz_cli_executable() -> str:
    """Return the launcher from this product checkout when available."""
    checkout = Path(__file__).resolve().parents[2] / "bin" / "swarmerz"
    return str(checkout) if checkout.is_file() else "swarmerz"


def _review_hint_argv(
    config_path: Path,
    overrides: list[str],
    manifest: dict[str, object],
) -> tuple[str, ...]:
    argv = [
        _swarmerz_cli_executable(),
        "chat",
        "--config",
        str(config_path.expanduser().resolve()),
    ]
    for override in overrides:
        argv.extend(("--set", override))
    argv.extend((
        "--session", str(manifest["session_id"]),
        "--round-id", str(manifest["round_id"]),
    ))
    return tuple(argv)


def _hunter_outcomes(manifest: dict[str, object]) -> tuple[HunterOutcome, ...]:
    partial_hunters = {
        str(item) for item in (manifest.get("partial_hunters") or [])
    }
    records = manifest.get("hunter_results")
    if not isinstance(records, list):
        return ()
    outcomes: list[HunterOutcome] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("hunter_id"), str):
            continue
        completion = record.get("worker_completion")
        terminal: int | None = None
        expected: int | None = None
        complete = True
        if isinstance(completion, dict):
            terminal_value = completion.get("terminal")
            expected_value = completion.get("expected")
            terminal = int(terminal_value) if terminal_value is not None else None
            expected = int(expected_value) if expected_value is not None else None
            if terminal is not None and expected is not None:
                # A replacement attempt may make the artifact's terminal-attempt
                # count exceed the configured fan-out. The fleet card represents
                # completed worker slots, so keep its fraction bounded.
                terminal = min(terminal, expected)
            complete = bool(completion.get("complete"))
        monitor = record.get("worker_monitor")
        blocked = (
            isinstance(monitor, dict)
            and monitor.get("reason") == "permission_required"
        )
        hunter_id = str(record["hunter_id"])
        outcomes.append(HunterOutcome(
            hunter_id=hunter_id,
            status=str(record.get("status") or "unknown"),
            answer_sealed=bool(record.get("answer_sealed")),
            terminal_workers=terminal,
            expected_workers=expected,
            degraded=hunter_id in partial_hunters or not complete,
            partial_notes=isinstance(record.get("partial_notes"), dict),
            blocked=blocked,
            output_tokens=_outcome_tokens(record),
        ))
    return tuple(outcomes)


def _outcome_tokens(record: dict[str, object]) -> int | None:
    monitor = record.get("worker_monitor")
    if not isinstance(monitor, dict):
        return None
    snapshot = monitor.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("output_tokens") is None:
        return None
    return int(snapshot["output_tokens"])
