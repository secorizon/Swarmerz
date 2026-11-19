# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from .adapters import build_hunter_command
from .claude_code import claude_fanout_evidence, is_native_claude
from .artifacts import (
    collect_partial_notes,
    read_json,
    seal_answer,
    sha256_file,
    verify_worker_ledger,
    write_json,
)
from .fanout import build_fanout_manifest, fanout_summary
from .instructions import build_hunter_instruction
from .models import AppConfig, CommandSpec, ConfigError, EngineConfig
from .opencode_permissions import OpenCodeEndpoint
from .opencode_server import (
    IsolatedOpenCodeServer,
    attach_server,
    cleanup_private_opencode_state,
    provision_provider_environment,
)
from .protocol_support import engine_manifest as _engine_manifest


_HARD_OPENCODE_PROVIDER_FAILURE_MARKERS = (
    "provider.quota",
    "insufficient account funds",
    "provider.no-route",
    "authentication failed",
)
_OPENCODE_PROVIDER_ERROR = re.compile(r"(?<![a-z0-9_.-])provider\.[a-z0-9_-]+")


def _reported_harness_version(output: str) -> str | None:
    match = re.search(
        r"(?<![0-9])([0-9]+(?:\.[0-9]+)+(?:[-+][A-Za-z0-9.-]+)?)(?![0-9])",
        output,
    )
    return match.group(1) if match is not None else None


def _probe_uses_npx(spec: CommandSpec) -> bool:
    return Path(spec.argv[0]).name.lower() in {"npx", "npx.cmd", "npx.exe"}


def _with_resolved_harness_versions(
    config: AppConfig,
    versions: dict[tuple[str, str], str],
) -> AppConfig:
    def resolve(engine: EngineConfig) -> EngineConfig:
        if engine.command:
            return engine
        version = versions.get((engine.harness, engine.harness_version))
        return replace(engine, harness_version=version) if version else engine

    return replace(
        config,
        hunters=tuple(resolve(engine) for engine in config.hunters),
        analyzer=resolve(config.analyzer),
        secondary_analyzer=(
            resolve(config.secondary_analyzer)
            if config.secondary_analyzer is not None
            else None
        ),
    )


def _is_terminal_opencode_provider_failure(
    result: dict[str, Any],
    diagnostic: str,
    *,
    worker_status_observed: bool,
    observed_workers: int,
) -> bool:
    """Return whether a failed parent cannot produce a usable worker swarm.

    Quota, routing, and authentication failures retain their existing fail-fast
    behavior. Other provider errors are terminal only after OpenCode's session
    API has confirmed that the parent launched no direct child workers. This
    prevents a broken parent stream from abandoning children that are still
    doing useful work.
    """
    if result.get("status") not in {"failed", "failed_to_start"}:
        return False
    normalized = diagnostic.lower()
    if any(
        marker in normalized
        for marker in _HARD_OPENCODE_PROVIDER_FAILURE_MARKERS
    ):
        return True
    return bool(
        worker_status_observed
        and observed_workers == 0
        and _OPENCODE_PROVIDER_ERROR.search(normalized)
    )


class HunterExecutionMixin:
    """Own hunter preparation, harness preflight, execution, and answer sealing."""

    async def _recover_opencode_from_operator(
        self,
        engine: EngineConfig,
        spec: CommandSpec,
        *,
        artifact_dir: Path,
        session_id: str,
        endpoint: OpenCodeEndpoint,
        result: dict[str, Any],
        try_seal: Callable[[], bool],
        wait_for_late_artifact: Callable[..., Awaitable[bool]],
    ) -> None:
        """Keep a verified native OpenCode session targetable while peers run."""
        if self.live_control is None:
            return
        attempts: list[dict[str, Any]] = []
        initial_failure = {
            key: result.get(key)
            for key in ("status", "exit_code", "timed_out", "artifact_error", "diagnostic")
            if result.get(key) is not None
        }
        while (
            not result.get("answer_sealed")
            and (
                self.live_control.recovery_window_open(engine.id)
                or self.live_control.has_pending_recovery(engine.id)
            )
        ):
            self.live_control.mark_recoverable(engine.id)
            detail = str(
                result.get("diagnostic")
                or result.get("artifact_error")
                or result.get("error")
                or result.get("status")
                or "failed"
            )
            display_name = self._hunter_progress_name(engine)
            self._emit(
                f"[{display_name}] OpenCode session failed; recovery available while "
                f"other hunters run. Use /to {engine.id} MESSAGE to resume the same "
                f"session. Detail: {detail[:240]}"
            )
            instruction: str | None = None
            while (
                self.live_control.recovery_window_open(engine.id)
                or self.live_control.has_pending_recovery(engine.id)
            ):
                instruction = await self.live_control.next_recovery(engine.id)
                if instruction is not None:
                    break
            if instruction is None:
                break
            self.live_control.mark_resuming(engine.id)
            self._emit(
                f"[{display_name}] resuming recoverable OpenCode session from targeted "
                "operator instruction"
            )
            recovery_spec = self._opencode_resume_spec(
                spec, session_id, instruction
            )
            log_stem = f"operator-recovery-{len(attempts) + 1:02d}"
            attempt_started = time.monotonic()
            try:
                recovery_result = await self._execute_opencode_finalize(
                    engine,
                    recovery_spec,
                    artifact_dir=artifact_dir,
                    session_id=session_id,
                    endpoint=endpoint,
                    log_stem=log_stem,
                )
            except (ConfigError, OSError, RuntimeError, TimeoutError) as exc:
                recovery_result = {
                    "status": "timed_out" if isinstance(exc, TimeoutError) else "failed",
                    "exit_code": None,
                    "timed_out": isinstance(exc, TimeoutError),
                    "error": str(exc),
                }
            recovery_result["duration_seconds"] = round(
                time.monotonic() - attempt_started, 3
            )
            attempts.append({
                "instruction_sha256": hashlib.sha256(
                    instruction.encode("utf-8")
                ).hexdigest(),
                "session_id": session_id,
                **recovery_result,
            })
            result["status"] = recovery_result.get("status")
            result["exit_code"] = recovery_result.get("exit_code")
            result["timed_out"] = recovery_result.get("timed_out")
            result["duration_seconds"] = round(
                float(result.get("duration_seconds") or 0)
                + float(recovery_result.get("duration_seconds") or 0),
                3,
            )
            if result.get("worker_contract_verified"):
                try_seal()
                if (
                    not result.get("answer_sealed")
                    and recovery_result.get("status") == "completed"
                ):
                    await wait_for_late_artifact(
                        min(self.config.opencode_worker_poll_seconds, 30),
                        phase="operator_recovery_artifact",
                    )
            if result.get("answer_sealed"):
                result["status"] = "completed"
                break

        if attempts:
            result["operator_recovery_initial_failure"] = initial_failure
            result["operator_recovery_attempts"] = attempts
            result["operator_recovery_count"] = len(attempts)
            result["resumed_after_failure"] = bool(result.get("answer_sealed"))

    def _prepare_hunter(self, engine: EngineConfig) -> tuple[EngineConfig, Any, Path]:
        workspace = self.run_dir / "workspaces" / "hunters" / engine.id
        artifact_dir = self.run_dir / "hunters" / engine.id
        workspace.mkdir(parents=True)
        artifact_dir.mkdir(parents=True)
        self._stage_context(workspace, label=self._hunter_progress_name(engine))
        if engine.ollama_base_url is not None:
            write_json(
                workspace / "opencode.json",
                {"providers": {"ollama": {
                    "settings": {"baseURL": engine.ollama_base_url},
                    "models": {engine.effective_model.removeprefix("ollama/"): {
                        "capabilities": {
                            "tools": True, "input": ["text"], "output": ["text"],
                        },
                    }},
                }}},
            )
        private_dir = workspace / ".swarm"
        private_dir.mkdir()
        history = self._hunter_history(engine.id)
        write_json(private_dir / "history.json", history)
        validated_finding_count = sum(
            len(item["findings"]) for item in history["validated_findings"]
        )
        history_rounds = {
            item["round_number"]
            for section in (history["prior_rounds"], history["validated_findings"])
            for item in section
        }
        if validated_finding_count:
            self._emit(
                f"[setup] [{self._hunter_progress_name(engine)}] injecting "
                f"{validated_finding_count} prior validated finding(s) from "
                f"{len(history['validated_findings'])} completed round(s)"
            )
        prompt = build_hunter_instruction(
            self.agent_instruction,
            engine,
            hunter_count=len(self.config.hunters),
            has_context=self.context_snapshot.is_dir(),
            has_history=bool(history["prior_rounds"] or history["validated_findings"]),
        )
        prompt_file = artifact_dir / "instruction.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        spec = build_hunter_command(
            self.config,
            engine,
            run_id=f"{self.session_id}/{self.round_id}",
            workspace=workspace,
            artifact_dir=artifact_dir,
            prompt=prompt,
            prompt_file=prompt_file,
        )
        write_json(
            artifact_dir / "launch.json",
            {
                **_engine_manifest(engine),
                "argv": self._redacted_argv(spec.argv, prompt),
                "environment_names": sorted(spec.env),
                "answer_path": str(spec.artifact_path),
                "history_rounds": len(history_rounds),
                "private_history_rounds": len(history["prior_rounds"]),
                "validated_finding_rounds": len(history["validated_findings"]),
                "validated_finding_count": validated_finding_count,
            },
        )
        return engine, spec, artifact_dir

    async def _prepare_harnesses(self) -> None:
        for engine in self.config.hunters:
            if engine.harness == "opencode" and engine.provider == "ollama" and not engine.command:
                self._emit(f"[setup] checking Ollama model {engine.effective_model} at its configured endpoint")
                inventory_url = self._probe_ollama_model(engine)
                self._emit(f"[setup] Ollama model present: {engine.effective_model} ({inventory_url})")
        adjudicators = (self.config.analyzer,) + (
            (self.config.secondary_analyzer,)
            if self.config.secondary_analyzer is not None
            else ()
        )
        checked_codex = self._check_chatgpt_codex_models((*self.config.hunters, *adjudicators))
        if checked_codex:
            self._emit(
                "[setup] Codex account model catalog includes: " + ", ".join(checked_codex)
            )
        setup_root = self.run_dir / "setup"
        setup_root.mkdir(parents=True, exist_ok=True)
        seen: set[tuple[str, str]] = set()
        resolved_versions: dict[tuple[str, str], str] = {}
        resolution_sources: dict[tuple[str, str], str] = {}
        for engine in (*self.config.hunters, *adjudicators):
            key = (engine.harness, engine.harness_version)
            if key in seen:
                continue
            seen.add(key)
            spec = self._build_probe_command(
                self.config,
                engine,
                run_id=f"{self.session_id}/{self.round_id}/setup",
                cwd=self.run_dir,
            )
            if spec is None:
                continue
            role_dir = setup_root / engine.id
            first_use = " (first use may download the pinned CLI)" if spec.argv[0] == "npx" else ""
            self._emit(
                f"[setup] checking {engine.harness} {engine.harness_version}{first_use}"
            )
            task = asyncio.create_task(
                self._execute_command(
                    spec,
                    timeout_seconds=900,
                    stdout_path=role_dir / "stdout.log",
                    stderr_path=role_dir / "stderr.log",
                )
            )
            while not task.done():
                done, _pending = await asyncio.wait({task}, timeout=15)
                if not done:
                    self._emit(f"[setup] still preparing {engine.harness} {engine.harness_version}")
            result = await task
            write_json(role_dir / "result.json", result)
            if result.get("status") != "completed":
                detail = self._diagnostic_tail(role_dir / "stderr.log") or result.get("error") or "probe failed"
                raise ConfigError(
                    f"unable to prepare {engine.harness} {engine.harness_version}: {detail}; "
                    f"log: {role_dir / 'stderr.log'}"
                )
            version = (role_dir / "stdout.log").read_text(encoding="utf-8", errors="replace").strip()
            expected = engine.harness_version
            pinned_mismatch = expected not in {"", "unversioned", "latest"} and not re.search(
                rf"(?<![0-9]){re.escape(expected)}(?![0-9])",
                version,
            )
            reported = _reported_harness_version(version)
            uses_npx = _probe_uses_npx(spec)
            if pinned_mismatch:
                if uses_npx or reported is None:
                    raise ConfigError(
                        f"{engine.harness} version mismatch: configured {expected}, "
                        f"probe returned {version or '<empty>'}"
                    )
                self._emit(
                    f"[setup] {engine.harness} configured {expected}; using installed "
                    f"{reported} and recording the resolved runtime version"
                )
            if reported is not None:
                resolved_versions[key] = reported
                resolution_sources[key] = "npx" if uses_npx else "installed"
            self._emit(f"[setup] ready: {version or engine.harness}")

        if resolved_versions:
            configured = self.config
            runtime_versions: dict[str, dict[str, str]] = {}
            for engine in (*configured.hunters, *adjudicators):
                if engine.command:
                    continue
                key = (engine.harness, engine.harness_version)
                resolved = resolved_versions.get(key)
                if resolved is None:
                    continue
                runtime_versions[engine.id] = {
                    "configured": engine.harness_version,
                    "resolved": resolved,
                    "source": resolution_sources[key],
                }
            self.config = _with_resolved_harness_versions(
                configured,
                resolved_versions,
            )
            if self.manifest:
                self._update_manifest(
                    runtime_harness_versions=runtime_versions,
                    hunters=[
                        _engine_manifest(engine) for engine in self.config.hunters
                    ],
                    analyzer=_engine_manifest(self.config.analyzer),
                    secondary_analyzer=(
                        _engine_manifest(self.config.secondary_analyzer)
                        if self.config.secondary_analyzer is not None
                        else None
                    ),
                )

        opencode_engines = [
            engine
            for engine in self.config.hunters
            if engine.harness == "opencode" and not engine.command
        ]
        stored_auth_engines = [
            engine
            for engine in opencode_engines
            if self.config.isolation.opencode_server_mode != "per_hunter"
            if engine.provider != "ollama"
            and not any(name in os.environ for name in engine.pass_env)
            and not engine.env
        ]
        if stored_auth_engines:
            provider_names = ", ".join(sorted({engine.provider for engine in stored_auth_engines}))
            self._emit(f"[setup] checking OpenCode provider authentication: {provider_names}")
            auth_dir = setup_root / "opencode-auth"
            auth_spec = self._build_auth_probe_command(
                self.config,
                stored_auth_engines[0],
                run_id=f"{self.session_id}/{self.round_id}/auth",
                cwd=self.run_dir,
            )
            auth_result = await self._execute_command(
                auth_spec,
                timeout_seconds=60,
                stdout_path=auth_dir / "stdout.json",
                stderr_path=auth_dir / "stderr.log",
            )
            write_json(auth_dir / "result.json", auth_result)
            try:
                providers = read_json(auth_dir / "stdout.json")
            except (ConfigError, OSError):
                providers = []
            authenticated_ids = {
                str(provider.get("id"))
                for provider in providers
                if isinstance(provider, dict)
                and isinstance(provider.get("connections"), list)
                and bool(provider["connections"])
            } if isinstance(providers, list) else set()
            missing = sorted({engine.provider for engine in stored_auth_engines} - authenticated_ids)
            if auth_result.get("status") != "completed" or missing:
                detail = self._diagnostic_tail(auth_dir / "stderr.log")
                suffix = f": {detail}" if detail else ""
                raise ConfigError(
                    "OpenCode provider authentication missing for "
                    f"{', '.join(missing) if missing else provider_names}. Run OpenCode and use `/connect` "
                    f"before starting the swarm{suffix}"
                )
            self._emit(f"[setup] OpenCode provider authentication ready: {provider_names}")

        runtime_adjudicators = (self.config.analyzer,) + (
            (self.config.secondary_analyzer,)
            if self.config.secondary_analyzer is not None
            else ()
        )
        claude_engines = [
            engine
            for engine in (*self.config.hunters, *runtime_adjudicators)
            if is_native_claude(engine)
        ]
        for engine in claude_engines:
            auth_dir = setup_root / f"{engine.id}-auth"
            self._emit(f"[setup] checking Claude Code authentication for {engine.id}")
            auth_spec = self._build_auth_probe_command(
                self.config,
                engine,
                run_id=f"{self.session_id}/{self.round_id}/auth/{engine.id}",
                cwd=self.run_dir,
            )
            auth_result = await self._execute_command(
                auth_spec,
                timeout_seconds=60,
                stdout_path=auth_dir / "stdout.json",
                stderr_path=auth_dir / "stderr.log",
            )
            write_json(auth_dir / "result.json", auth_result)
            try:
                status = read_json(auth_dir / "stdout.json")
            except (ConfigError, OSError):
                status = {}
            # `claude auth status` also returns account email and organization
            # metadata. Retain only non-identifying readiness fields in the run.
            if isinstance(status, dict):
                write_json(auth_dir / "stdout.json", {
                    "loggedIn": status.get("loggedIn") is True,
                    "authMethod": status.get("authMethod"),
                    "apiProvider": status.get("apiProvider"),
                })
            if (
                auth_result.get("status") != "completed"
                or not isinstance(status, dict)
                or status.get("loggedIn") is not True
            ):
                detail = self._diagnostic_tail(auth_dir / "stderr.log")
                suffix = f": {detail}" if detail else ""
                raise ConfigError(
                    f"Claude Code authentication is unavailable for {engine.id}. "
                    f"Run `claude auth login` before starting the swarm{suffix}"
                )
            self._emit(f"[setup] Claude Code authentication ready for {engine.id}")

    async def _run_hunters(self) -> list[dict[str, Any]]:
        prepared = [self._prepare_hunter(engine) for engine in self.config.hunters]
        per_hunter_servers = self.config.isolation.opencode_server_mode == "per_hunter"
        native_opencode_engines = [
            engine
            for engine in self.config.hunters
            if engine.harness == "opencode" and not engine.command
        ]

        def server_runtime(engine: EngineConfig) -> int:
            worker_timeout = (
                engine.worker_timeout_seconds
                or self.config.opencode_worker_timeout_seconds
            )
            return (
                engine.timeout_seconds
                + worker_timeout
                + self.config.opencode_finalize_timeout_seconds
                + 300
            )

        shared_server_runtime = max(
            (server_runtime(engine) for engine in native_opencode_engines),
            default=3600,
        )
        service_template_spec = (
            None if per_hunter_servers else self._shared_opencode_service_spec(prepared)
        )
        active = {engine.id for engine in self.config.hunters}
        started_hunters: set[str] = set()
        self._opencode_start_events = {
            engine.id: asyncio.Event()
            for engine in self.config.hunters
            if engine.harness == "opencode" and not engine.command
        }
        native_start_lock = asyncio.Lock()
        # Shared mode keeps one writer for the user's common OpenCode state and
        # routes requests by x-opencode-directory. Per-hunter mode instead gives
        # every service private HOME/XDG state and may start them concurrently.
        shared_server: IsolatedOpenCodeServer | None = None
        shared_server_error: str | None = None
        if self.live_control is not None:
            self.live_control.active.update(active)
        self._emit(f"launching {len(self.config.hunters)} independent parent swarms")
        native_queue = [
            self._hunter_progress_name(engine)
            for engine in self.config.hunters
            if engine.harness == "opencode" and not engine.command
        ]
        if len(native_queue) > 1 and not per_hunter_servers:
            self._emit(
                "[setup] OpenCode startup queue (hunts run concurrently once opened): "
                + " -> ".join(native_queue)
            )
        elif native_queue and per_hunter_servers:
            self._emit(
                "[setup] opt-in per-hunter OpenCode services enabled; "
                "each native hunter receives a private password, HOME, and state root"
            )

        async def run_one_inner(
            engine: EngineConfig, spec: CommandSpec, artifact_dir: Path,
            endpoint: OpenCodeEndpoint | None,
        ) -> dict[str, Any]:
            started_hunters.add(engine.id)
            if self.live_control is not None:
                self.live_control.mark_started(engine.id)
            self._emit(
                f"[{self._hunter_progress_name(engine)}] started: {engine.harness} {engine.effective_model} "
                f"effort={engine.reasoning_effort}"
            )
            stdout_path = artifact_dir / "stdout.jsonl"
            stderr_path = artifact_dir / "stderr.log"
            if self.live_control is not None and endpoint is not None:
                self.live_control.register_opencode(engine.id, endpoint)
            native_opencode = engine.harness == "opencode" and not engine.command
            native_claude = is_native_claude(engine)
            session_id: str | None = None
            worker_monitor: dict[str, Any] | None = None
            parent_forced_stop: dict[str, Any] | None = None
            provider_failure = False
            monitor_observation: dict[str, Any] = {}
            monitor_observation_ready = asyncio.Event()
            execution_started = time.monotonic()

            def provider_failure_monitor(
                snapshot: dict[str, Any] | None = None,
                *,
                query_count: int = 0,
            ) -> dict[str, Any]:
                worker_snapshot = dict(snapshot) if isinstance(snapshot, dict) else {
                    "parent_session_id": session_id,
                    "expected_workers": engine.swarm_size,
                    "observed_workers": 0,
                    "terminal_workers": 0,
                    "active_workers": 0,
                    "all_workers_terminal": False,
                    "outcomes": {},
                    "active_titles": [],
                    "output_tokens": 0,
                    "latest_terminal_ms": 0,
                    "earliest_created_ms": 0,
                    "children": [],
                }
                return {
                    "reason": "provider_failure",
                    "wait_seconds": round(time.monotonic() - execution_started, 3),
                    "query_count": query_count,
                    "status_errors": [],
                    "snapshot": worker_snapshot,
                }

            execution_task = asyncio.create_task(self._execute_with_transport_retries(
                engine,
                spec,
                role=engine.id,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            ))

            if native_opencode:
                # The session ID is normally the first JSONL event. Start worker
                # supervision immediately instead of waiting for a parent that may
                # itself be blocked forever on an orphaned child tool call.
                while not execution_task.done():
                    session_id = self._opencode_session_id(stdout_path)
                    if session_id:
                        break
                    await asyncio.sleep(0.1)
                if session_id is None:
                    session_id = self._opencode_session_id(stdout_path)

            monitor_task: asyncio.Task[dict[str, Any]] | None = None
            if native_opencode and session_id:
                monitor_task = asyncio.create_task(self._monitor_opencode_workers(
                    engine,
                    parent_session_id=session_id,
                    artifact_dir=artifact_dir,
                    endpoint=endpoint,
                    observation=monitor_observation,
                    observation_ready=monitor_observation_ready,
                ))

            result: dict[str, Any] | None = None
            if monitor_task is not None:
                done, _ = await asyncio.wait(
                    {execution_task, monitor_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if execution_task in done:
                    result = execution_task.result()
                    diagnostic = " ".join((
                        self._diagnostic_tail(stderr_path) or "",
                        self._json_error_diagnostic(stdout_path) or "",
                        str(result.get("error") or ""),
                    )).lower()
                    hard_provider_failure = _is_terminal_opencode_provider_failure(
                        result,
                        diagnostic,
                        worker_status_observed=False,
                        observed_workers=0,
                    )
                    if (
                        not hard_provider_failure
                        and _OPENCODE_PROVIDER_ERROR.search(diagnostic)
                        and not monitor_observation_ready.is_set()
                    ):
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(
                                monitor_observation_ready.wait(),
                                timeout=min(
                                    max(float(self.config.opencode_worker_poll_seconds), 0.1),
                                    5.0,
                                ),
                            )
                    observed_snapshot = monitor_observation.get("snapshot")
                    worker_status_observed = (
                        monitor_observation_ready.is_set()
                        and isinstance(observed_snapshot, dict)
                    )
                    observed_workers = (
                        int(observed_snapshot.get("observed_workers") or 0)
                        if isinstance(observed_snapshot, dict)
                        else 0
                    )
                    provider_failure = _is_terminal_opencode_provider_failure(
                        result,
                        diagnostic,
                        worker_status_observed=worker_status_observed,
                        observed_workers=observed_workers,
                    )
                    if provider_failure:
                        if monitor_task.done():
                            # Retrieve a simultaneous monitor result so a rare
                            # monitor exception is not silently discarded.
                            monitor_task.result()
                        else:
                            monitor_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await monitor_task
                        worker_monitor = provider_failure_monitor(
                            observed_snapshot if isinstance(observed_snapshot, dict) else None,
                            query_count=int(monitor_observation.get("query_count") or 0),
                        )
                        write_json(artifact_dir / "worker-monitor.json", worker_monitor)
                        if observed_workers == 0:
                            self._emit(
                                f"[{engine.id}] parent provider failed before launching "
                                "workers; skipping the worker timeout"
                            )
                    else:
                        worker_monitor = await monitor_task
                else:
                    worker_monitor = monitor_task.result()
                    reason = str(worker_monitor.get("reason") or "unknown")
                    # A parent which has not returned after every direct worker
                    # became terminal is waiting on stale child state. Resume the
                    # exact session immediately for one bounded consolidation turn
                    # instead of spending the entire finalization budget here first.
                    grace = 0
                    if not execution_task.done():
                        interrupt_error: str | None = None
                        try:
                            await self._opencode_api_call(
                                self._interrupt_worker_session(), session_id, endpoint=endpoint
                            )
                        except (ConfigError, OSError) as exc:
                            interrupt_error = str(exc)
                        parent_forced_stop = {
                            "reason": reason,
                            "grace_seconds": grace,
                            "interrupt_error": interrupt_error,
                        }
                        self._emit(
                            f"[{engine.id}] stopping parent session after worker monitor "
                            f"finished ({reason})"
                        )
                        try:
                            result = await asyncio.wait_for(
                                asyncio.shield(execution_task), timeout=15
                            )
                        except TimeoutError:
                            execution_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await execution_task
                            result = {
                                "status": "stopped",
                                "exit_code": None,
                                "timed_out": False,
                                "stopped": True,
                                "duration_seconds": round(
                                    time.monotonic() - execution_started, 3
                                ),
                                "error": "parent CLI did not exit after session interruption",
                            }
                    elif result is None:
                        result = execution_task.result()

            if result is None:
                result = await execution_task
            if self.live_control is not None:
                self.live_control.parent_returned(engine.id)
            result.update({"hunter_id": engine.id, "answer_sealed": False})
            if native_claude:
                claude_workers = claude_fanout_evidence(stdout_path, engine.swarm_size)
                result["worker_monitor"] = claude_workers
                result["worker_contract_verified"] = bool(claude_workers["verified"])
                result["fanout_evidence"] = (
                    "observed_claude_agent_calls"
                    if result["worker_contract_verified"]
                    else "unverified"
                )
                snapshot = claude_workers["snapshot"]
                result["worker_completion"] = {
                    "terminal": int(snapshot.get("terminal_workers") or 0),
                    "expected": engine.swarm_size,
                    "complete": bool(snapshot.get("all_workers_terminal")),
                    "reason": claude_workers["reason"],
                }
            if engine.command or engine.harness == "command":
                ledger = verify_worker_ledger(
                    spec.cwd, artifact_dir, minimum_workers=engine.swarm_size
                )
                result["worker_ledger"] = ledger
                result["worker_contract_verified"] = ledger.get("status") == "ledger_verified"
                result["fanout_evidence"] = (
                    "self_reported_ledger"
                    if result["worker_contract_verified"]
                    else "unverified"
                )
            if parent_forced_stop is not None:
                result["parent_forced_stop"] = parent_forced_stop
            sealed_answer: dict[str, Any] | None = None

            def try_seal() -> bool:
                nonlocal sealed_answer
                if result["answer_sealed"]:
                    return True
                if native_claude:
                    claude_workers = claude_fanout_evidence(
                        stdout_path, engine.swarm_size
                    )
                    result["worker_monitor"] = claude_workers
                    result["worker_contract_verified"] = bool(claude_workers["verified"])
                    result["fanout_evidence"] = (
                        "observed_claude_agent_calls"
                        if result["worker_contract_verified"]
                        else "unverified"
                    )
                    if not result["worker_contract_verified"]:
                        observed = int(
                            claude_workers["snapshot"].get("observed_workers") or 0
                        )
                        result["artifact_error"] = (
                            f"Claude Code launched {observed}/{engine.swarm_size} "
                            "verifiable Swarmerz worker agents"
                        )
                        return False
                if engine.command or engine.harness == "command":
                    ledger = verify_worker_ledger(
                        spec.cwd, artifact_dir, minimum_workers=engine.swarm_size
                    )
                    result["worker_ledger"] = ledger
                    result["worker_contract_verified"] = (
                        ledger.get("status") == "ledger_verified"
                    )
                    result["fanout_evidence"] = (
                        "self_reported_ledger"
                        if result["worker_contract_verified"]
                        else "unverified"
                    )
                    if not result["worker_contract_verified"]:
                        result["artifact_error"] = (
                            "custom harness answer cannot be sealed without a valid "
                            f"worker ledger: {ledger.get('reason', 'invalid ledger')}"
                        )
                        return False
                if not spec.artifact_path or not spec.artifact_path.is_file():
                    result["artifact_error"] = f"missing answer file: {spec.artifact_path}"
                    return False
                try:
                    sealed_answer, seal = seal_answer(
                        spec.artifact_path,
                        artifact_dir,
                        minimum_workstreams=engine.swarm_size,
                    )
                    result["seal"] = seal
                    result["answer_sealed"] = True
                    result.pop("artifact_error", None)
                    return True
                except (OSError, ConfigError) as exc:
                    result["artifact_error"] = str(exc)
                    return False

            async def wait_for_late_artifact(seconds: int, *, phase: str) -> bool:
                if seconds <= 0:
                    return try_seal()
                started = time.monotonic()
                next_notice = 0.0
                while True:
                    if try_seal():
                        result[f"{phase}_wait_seconds"] = round(time.monotonic() - started, 3)
                        return True
                    elapsed = time.monotonic() - started
                    if elapsed >= seconds:
                        result[f"{phase}_wait_seconds"] = round(elapsed, 3)
                        return False
                    if elapsed >= next_notice:
                        remaining = max(0, int(seconds - elapsed))
                        if phase in {"recovery_artifact", "operator_recovery_artifact"}:
                            self._emit(
                                f"[{engine.id}] finalization returned; waiting up to "
                                f"{remaining}s for the answer artifact"
                            )
                        else:
                            self._emit(
                                f"[{engine.id}] custom OpenCode adapter returned; waiting up to "
                                f"{remaining}s for its answer artifact"
                            )
                        next_notice = elapsed + 60
                    await asyncio.sleep(min(5.0, max(0.1, seconds - elapsed)))

            json_diagnostic = self._json_error_diagnostic(stdout_path)
            initial_diagnostic = self._diagnostic_tail(stderr_path) or json_diagnostic
            if session_id is None and engine.harness == "opencode":
                session_id = self._opencode_session_id(stdout_path)
            diagnostic_text = " ".join(
                str(item)
                for item in (initial_diagnostic, json_diagnostic, result.get("error"))
                if item
            ).lower()
            provider_failure = provider_failure or any(
                marker in diagnostic_text
                for marker in (
                    "provider.quota", "insufficient account funds",
                    "provider.no-route", "auth",
                )
            )
            needs_finalize = False

            if native_opencode:
                if not session_id:
                    result["artifact_error"] = "OpenCode returned no parent session ID; worker swarm cannot be verified"
                elif provider_failure:
                    if worker_monitor is None or worker_monitor.get("reason") != "provider_failure":
                        prior_snapshot = (
                            worker_monitor.get("snapshot")
                            if isinstance(worker_monitor, dict)
                            and isinstance(worker_monitor.get("snapshot"), dict)
                            else None
                        )
                        worker_monitor = provider_failure_monitor(prior_snapshot)
                        write_json(artifact_dir / "worker-monitor.json", worker_monitor)
                    result["worker_monitor"] = worker_monitor
                    result["worker_contract_verified"] = False
                    result["fanout_evidence"] = "unverified"
                else:
                    if (
                        spec.artifact_path
                        and spec.artifact_path.is_file()
                        and not spec.artifact_path.is_symlink()
                    ):
                        self._emit(
                            f"[{engine.id}] provisional answer detected; waiting for verified worker outcomes"
                        )
                    if worker_monitor is None:
                        worker_monitor = await self._monitor_opencode_workers(
                            engine,
                            parent_session_id=session_id,
                            artifact_dir=artifact_dir,
                            endpoint=endpoint,
                        )
                    result["worker_monitor"] = worker_monitor
                    worker_snapshot = worker_monitor["snapshot"]
                    worker_contract_verified = (
                        int(worker_snapshot.get("observed_workers") or 0) >= engine.swarm_size
                    )
                    result["worker_contract_verified"] = worker_contract_verified
                    result["fanout_evidence"] = (
                        "monitored_child_sessions" if worker_contract_verified else "unverified"
                    )
                    result["worker_completion"] = {
                        "terminal": int(worker_snapshot.get("terminal_workers") or 0),
                        "expected": engine.swarm_size,
                        "complete": worker_monitor.get("reason") == "all_workers_terminal",
                        "reason": worker_monitor.get("reason"),
                    }
                    if worker_monitor.get("reason") == "permission_required":
                        result["status"] = "failed"
                        result["artifact_error"] = (
                            "OpenCode child permission remained unresolved; see "
                            "worker-monitor.json for the exact path or action. "
                            "Check worker path typos; use --context only for intentional "
                            "local source folders."
                        )
                    elif worker_monitor.get("reason") == "status_unavailable":
                        # A dead shared service cannot safely run a consolidation turn.
                        # Keep the in-workspace artifact untouched for inspection or
                        # a later recovery. Only seal a demonstrably settled partial.
                        if (
                            spec.artifact_path
                            and worker_contract_verified
                            and self._settled_partial_answer(
                                spec.artifact_path,
                                worker_snapshot,
                                minimum_workstreams=engine.swarm_size,
                            )
                        ):
                            try_seal()
                        if result["answer_sealed"]:
                            result["status"] = "completed"
                        else:
                            result["status"] = "failed"
                            result["artifact_error"] = (
                                "OpenCode service became unavailable; provisional answer "
                                "was preserved, but worker completion could not be verified. "
                                "See worker-monitor.json and worker-status.stderr.log."
                            )
                    else:
                        answer_is_post_worker = bool(
                            spec.artifact_path
                            and worker_contract_verified
                            and (
                                worker_monitor.get("reason") == "partial_answer_ready"
                                or self._answer_postdates_workers(spec.artifact_path, worker_snapshot)
                            )
                        )
                        if answer_is_post_worker:
                            try_seal()
                        else:
                            needs_finalize = True
                            if (
                                spec.artifact_path
                                and spec.artifact_path.is_file()
                                and not spec.artifact_path.is_symlink()
                            ):
                                provisional_path = artifact_dir / "provisional-answer.json"
                                if not provisional_path.exists():
                                    shutil.copy2(spec.artifact_path, provisional_path)
                                spec.artifact_path.unlink()
                                result["provisional_answer"] = str(provisional_path)
                            elif spec.artifact_path and spec.artifact_path.is_symlink():
                                spec.artifact_path.unlink()
                                result["provisional_answer_rejected"] = "symlink"
            else:
                try_seal()
                if (
                    not result["answer_sealed"]
                    and engine.harness == "opencode"
                    and result.get("status") == "completed"
                ):
                    await wait_for_late_artifact(
                        self.config.opencode_artifact_grace_seconds,
                        phase="background_artifact",
                    )

            if not result["answer_sealed"]:
                resumable = (
                    bool(session_id)
                    and not provider_failure
                    and not (
                        worker_monitor
                        and worker_monitor.get("reason") in {
                            "permission_required", "status_unavailable"
                        }
                    )
                    and (
                        needs_finalize
                        or result.get("status") in {"completed", "timed_out"}
                        or "inactivity" in diagnostic_text
                        or "session interrupted" in diagnostic_text
                    )
                )
                if resumable:
                    self._emit(
                        f"[{engine.id}] resuming OpenCode session to consolidate completed work"
                    )
                    finalize_prompt = self._opencode_finalize_instruction(
                        engine.answer_path,
                        engine.swarm_size,
                        worker_monitor,
                    )
                    recovery_spec = self._opencode_resume_spec(spec, str(session_id), finalize_prompt)
                    recovery_result = await self._execute_opencode_finalize(
                        engine,
                        recovery_spec,
                        artifact_dir=artifact_dir,
                        session_id=str(session_id),
                        endpoint=endpoint,
                    )
                    result["initial_attempt"] = {
                        "status": result.get("status"),
                        "exit_code": result.get("exit_code"),
                        "timed_out": result.get("timed_out"),
                        "duration_seconds": result.get("duration_seconds"),
                        "diagnostic": initial_diagnostic,
                    }
                    result["recovery"] = {
                        **recovery_result,
                        "session_id": session_id,
                    }
                    result["status"] = recovery_result.get("status")
                    result["exit_code"] = recovery_result.get("exit_code")
                    result["timed_out"] = recovery_result.get("timed_out")
                    result["duration_seconds"] = round(
                        float(result["initial_attempt"].get("duration_seconds") or 0)
                        + float(recovery_result.get("duration_seconds") or 0),
                        3,
                    )
                    if engine.command or engine.harness == "command":
                        ledger = verify_worker_ledger(
                            spec.cwd, artifact_dir, minimum_workers=engine.swarm_size
                        )
                        result["worker_ledger"] = ledger
                        result["worker_contract_verified"] = (
                            ledger.get("status") == "ledger_verified"
                        )
                        result["fanout_evidence"] = (
                            "self_reported_ledger"
                            if result["worker_contract_verified"]
                            else "unverified"
                        )
                    worker_contract_verified = bool(result.get("worker_contract_verified", True))
                    if worker_contract_verified:
                        try_seal()
                    else:
                        result["artifact_error"] = (
                            f"OpenCode launched fewer than {engine.swarm_size} verifiable child sessions"
                        )
                    if (
                        worker_contract_verified
                        and not result["answer_sealed"]
                        and recovery_result.get("status") == "completed"
                    ):
                        await wait_for_late_artifact(
                            min(self.config.opencode_worker_poll_seconds, 30),
                            phase="recovery_artifact",
                        )

            if (
                not result["answer_sealed"]
                and native_opencode
                and session_id
                and endpoint is not None
                and self.live_control is not None
                and bool(result.get("worker_contract_verified"))
                and not (
                    worker_monitor
                    and worker_monitor.get("reason") == "status_unavailable"
                )
            ):
                await self._recover_opencode_from_operator(
                    engine,
                    spec,
                    artifact_dir=artifact_dir,
                    session_id=str(session_id),
                    endpoint=endpoint,
                    result=result,
                    try_seal=try_seal,
                    wait_for_late_artifact=wait_for_late_artifact,
                )

            if not result["answer_sealed"]:
                partial_notes = collect_partial_notes(spec.cwd, artifact_dir)
                if partial_notes:
                    result["partial_notes"] = partial_notes
                    self._emit(f"[{engine.id}] preserved unverified worker notes: {partial_notes['path']}")
                if worker_monitor and worker_monitor.get("reason") in {
                    "permission_required", "status_unavailable"
                }:
                    result["diagnostic"] = result.get("artifact_error") or initial_diagnostic
                else:
                    result["diagnostic"] = (
                        self._diagnostic_tail(artifact_dir / "finalize.stderr.log")
                        or self._json_error_diagnostic(artifact_dir / "finalize.stdout.jsonl")
                        or initial_diagnostic
                        or result.get("artifact_error")
                    )
            elif "recovery" in result:
                result["status"] = "completed"
            if result["answer_sealed"] and sealed_answer is not None:
                manifest = build_fanout_manifest(engine, sealed_answer, result)
                write_json(artifact_dir / "fanout-manifest.json", manifest)
                (artifact_dir / "fanout-manifest.json").chmod(0o444)
                result["fanout_manifest_sha256"] = sha256_file(
                    artifact_dir / "fanout-manifest.json"
                )
                result["fanout_summary"] = fanout_summary(manifest)
            write_json(artifact_dir / "result.json", result)
            active.discard(engine.id)
            if self.live_control is not None:
                self.live_control.finished(engine.id)
            if result["answer_sealed"]:
                completion = result.get("worker_completion")
                if isinstance(completion, dict):
                    terminal = int(completion.get("terminal") or 0)
                    expected = int(completion.get("expected") or engine.swarm_size)
                    degraded = not bool(completion.get("complete"))
                    suffix = " (degraded)" if degraded else ""
                    self._emit(
                        f"[{self._hunter_progress_name(engine)}] completed {terminal}/{expected} "
                        f"workers and answer sealed{suffix}"
                    )
                else:
                    self._emit(f"[{self._hunter_progress_name(engine)}] completed and answer sealed")
                if self.hunter_report is not None and sealed_answer is not None:
                    self.hunter_report(engine.id, str(sealed_answer["answer"]))
            else:
                detail = (
                    result.get("diagnostic")
                    or result.get("artifact_error")
                    or result.get("error")
                    or "no valid answer"
                )
                self._emit(f"[{self._hunter_progress_name(engine)}] FAILED ({result['status']}): {detail}")
            return result

        async def run_one(
            engine: EngineConfig, spec: CommandSpec, artifact_dir: Path
        ) -> dict[str, Any]:
            nonlocal shared_server, shared_server_error
            if engine.harness != "opencode" or engine.command:
                return await run_one_inner(engine, spec, artifact_dir, None)

            async def startup_failure(detail: str) -> dict[str, Any]:
                result = {
                    "hunter_id": engine.id,
                    "status": "failed_to_start",
                    "answer_sealed": False,
                    "worker_contract_verified": False,
                    "diagnostic": detail,
                }
                write_json(artifact_dir / "result.json", result)
                active.discard(engine.id)
                if self.live_control is not None:
                    self.live_control.finished(engine.id)
                self._emit(f"[{self._hunter_progress_name(engine)}] FAILED (startup): {detail}")
                return result

            if per_hunter_servers:
                private_server: IsolatedOpenCodeServer | None = None
                private_spec: CommandSpec | None = None
                try:
                    private_spec = self._per_hunter_opencode_service_spec(engine, spec)
                    private_server = await IsolatedOpenCodeServer.start(
                        private_spec,
                        artifact_dir=self.run_dir / "opencode-services" / engine.id,
                        resource_limiter=self._resource_limiter,
                        runtime_seconds=server_runtime(engine),
                    )
                    hunter_endpoint = OpenCodeEndpoint(
                        private_server.endpoint.url,
                        private_server.endpoint.password,
                        private_spec.cwd,
                    )
                    attached_spec = attach_server(private_spec, hunter_endpoint)
                    await private_server.wait_ready(hunter_endpoint)
                    if engine.provider == "ollama":
                        await private_server.wait_model(
                            hunter_endpoint,
                            f"ollama/{engine.effective_model.removeprefix('ollama/')}",
                        )
                    else:
                        provider_auth = await private_server.wait_provider_auth(
                            hunter_endpoint, engine.provider
                        )
                        if not provider_auth.enabled:
                            provision = provision_provider_environment(
                                private_spec,
                                provider_auth.environment_names,
                            )
                            if provision.forwarded_names:
                                await private_server.stop()
                                private_server = None
                                private_spec = provision.spec
                                self._emit(
                                    f"[{self._hunter_progress_name(engine)}] forwarding "
                                    "private provider credential variable(s): "
                                    + ", ".join(provision.forwarded_names)
                                )
                                private_server = await IsolatedOpenCodeServer.start(
                                    private_spec,
                                    artifact_dir=self.run_dir / "opencode-services" / engine.id,
                                    resource_limiter=self._resource_limiter,
                                    runtime_seconds=server_runtime(engine),
                                )
                                hunter_endpoint = OpenCodeEndpoint(
                                    private_server.endpoint.url,
                                    private_server.endpoint.password,
                                    private_spec.cwd,
                                )
                                attached_spec = attach_server(
                                    private_spec, hunter_endpoint
                                )
                                await private_server.wait_ready(hunter_endpoint)
                                provider_auth = await private_server.wait_provider_auth(
                                    hunter_endpoint, engine.provider
                                )
                            if not provider_auth.enabled:
                                supported = ", ".join(
                                    provider_auth.environment_names
                                ) or "none"
                                available = ", ".join(
                                    provision.available_names
                                ) or "none"
                                raise ConfigError(
                                    f"private OpenCode hunter {engine.id} cannot authenticate "
                                    f"provider {engine.provider}; provider-supported environment "
                                    f"variables: {supported}; available: {available}. Set a "
                                    "provider API key in the host environment, configure a "
                                    "reviewed pass_env variable, or use shared mode for an "
                                    "OAuth/token-plan account. Swarmerz will not copy the "
                                    "user's OpenCode credential database"
                                )
                    self._emit(
                        f"[{self._hunter_progress_name(engine)}] private OpenCode workspace ready on "
                        f"{private_server.endpoint.url}"
                    )
                    result = await run_one_inner(
                        engine, attached_spec, artifact_dir, hunter_endpoint
                    )
                    result["isolated_server"] = {
                        "url": private_server.endpoint.url,
                        "stderr_log": str(private_server.stderr_path),
                        "shared_by_native_hunters": False,
                        "private_home_and_state": True,
                    }
                    write_json(artifact_dir / "result.json", result)
                    return result
                except (ConfigError, OSError) as exc:
                    return await startup_failure(str(exc))
                finally:
                    try:
                        if private_server is not None:
                            await private_server.stop()
                    finally:
                        if private_spec is not None:
                            try:
                                cleanup_private_opencode_state(private_spec)
                            except (ConfigError, OSError) as exc:
                                self._emit(
                                    f"[security] [{self._hunter_progress_name(engine)}] "
                                    f"private OpenCode credential-state cleanup failed: {exc}"
                                )

            if service_template_spec is None:
                raise ConfigError("native OpenCode service template is unavailable")

            task: asyncio.Task[dict[str, Any]] | None = None
            async with native_start_lock:
                if shared_server_error is not None:
                    return await startup_failure(shared_server_error)
                if shared_server is None:
                    try:
                        self._emit(
                            "[setup] starting one OpenCode service for all native "
                            "hunters in this round"
                        )
                        shared_server = await IsolatedOpenCodeServer.start(
                            service_template_spec,
                            artifact_dir=self.run_dir / "opencode-services" / "shared",
                            resource_limiter=self._resource_limiter,
                            runtime_seconds=shared_server_runtime,
                        )
                        self._emit(
                            "[setup] shared OpenCode service ready on "
                            f"{shared_server.endpoint.url}"
                        )
                    except (ConfigError, OSError) as exc:
                        shared_server_error = str(exc)
                        return await startup_failure(shared_server_error)
                try:
                    hunter_endpoint = OpenCodeEndpoint(
                        shared_server.endpoint.url, shared_server.endpoint.password, spec.cwd
                    )
                    attached_spec = attach_server(spec, hunter_endpoint)
                    await shared_server.wait_ready(hunter_endpoint)
                    if engine.provider == "ollama":
                        await shared_server.wait_model(
                            hunter_endpoint,
                            f"ollama/{engine.effective_model.removeprefix('ollama/')}",
                        )
                except (ConfigError, OSError) as exc:
                    return await startup_failure(str(exc))
                self._emit(
                    f"[{self._hunter_progress_name(engine)}] OpenCode workspace ready on "
                    f"{shared_server.endpoint.url}"
                )
                task = asyncio.create_task(
                    run_one_inner(engine, attached_spec, artifact_dir, hunter_endpoint)
                )
                started_event = self._opencode_start_events[engine.id]
                event_task = asyncio.create_task(started_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {task, event_task}, timeout=120,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await event_task
                if not done:
                    self._emit(
                        f"[{self._hunter_progress_name(engine)}] no parent session ID after 120s; leaving the "
                        "hunter running without a paid restart and checking service "
                        "health before the next launch"
                    )
                if event_task in done and not task.done():
                    self._emit(
                        f"[{self._hunter_progress_name(engine)}] parent session opened; starting next OpenCode hunter"
                    )
                    await asyncio.sleep(2)
            assert task is not None and shared_server is not None
            result = await task
            result["isolated_server"] = {
                "url": shared_server.endpoint.url,
                "stderr_log": str(shared_server.stderr_path),
                "shared_by_native_hunters": True,
            }
            write_json(artifact_dir / "result.json", result)
            return result

        async def heartbeat() -> None:
            progress_names = {
                engine.id: self._hunter_progress_name(engine)
                for engine in self.config.hunters
            }
            while active:
                await asyncio.sleep(60)
                if active:
                    running = sorted(active & started_hunters)
                    queued = sorted(active - started_hunters)
                    details = []
                    if running:
                        details.append(
                            "still running: "
                            + ", ".join(progress_names[hunter_id] for hunter_id in running)
                        )
                    if queued:
                        details.append(
                            "queued to start: "
                            + ", ".join(progress_names[hunter_id] for hunter_id in queued)
                        )
                    self._emit("; ".join(details))

        # All independent workspaces and prompts exist before the first parent starts.
        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            return await asyncio.gather(*(run_one(*item) for item in prepared))
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            if shared_server is not None:
                await shared_server.stop()

