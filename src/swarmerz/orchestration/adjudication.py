# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import copy
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from .adapters import (
    build_analyzer_command,
    build_baseline_command,
    build_verifier_command,
)
from .artifacts import (
    answer_labels,
    build_blind_bundle,
    preserve_reproduction_references,
    preserve_workspace_artifacts,
    randomized_answer_order,
    read_json,
    sha256_file,
    write_json,
)
from .execution import parse_json_output
from .instructions import (
    build_analyzer_instruction,
    build_baseline_instruction,
    build_verifier_instruction,
)
from .models import AnalyzerConfig, CommandSpec, ConfigError
from .protocol_support import engine_manifest as _engine_manifest
from .schema_validation import schema_error
from .session_context import copy_independent_file


class AdjudicationMixin:
    """Own baseline, blind primary review, and optional secondary verification."""

    def _new_adjudication_stage(
        self, phase: str, artifact_dir: Path,
    ) -> tuple[Path, Path]:
        if self._storage is None or self._storage.run_root is None:
            raise ConfigError("blind adjudication staging requires active managed storage")
        root = self._storage.allocate_opaque_scratch(phase)
        workspace = root / "workspace"
        output = root / "output"
        workspace.mkdir(mode=0o700)
        output.mkdir(mode=0o700)
        self._adjudication_workspaces.append((workspace, artifact_dir, phase))
        return workspace, output

    @staticmethod
    def _isolate_adjudication_runtime(
        spec: CommandSpec, workspace: Path, phase: str,
    ) -> CommandSpec:
        """Keep judge temporary state out of the per-hunter run scratch tree."""
        runtime = workspace.parent / "runtime"
        environment = dict(spec.env)
        environment.update(
            {
                "SWARMERZ_SCRATCH_DIR": str(runtime),
                "TMPDIR": str(runtime / "tmp"),
                "TMP": str(runtime / "tmp"),
                "TEMP": str(runtime / "tmp"),
                "DOTNET_CLI_HOME": str(runtime / "dotnet-home"),
                "CARGO_TARGET_DIR": str(runtime / "cargo-target"),
                "SWARM_RUN_ID": f"blind-{workspace.parent.name}",
                "SWARM_ROLE_ID": f"blind-{phase}",
            }
        )
        return CommandSpec(spec.argv, spec.cwd, environment, spec.artifact_path)

    @staticmethod
    def _stage_output_schema(workspace: Path, schema_name: str) -> Path:
        source = Path(__file__).resolve().parent / "schemas" / schema_name
        destination = workspace / ".swarm" / "output-schema.json"
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(0o444)
        return destination

    @staticmethod
    def _publish_stage_files(
        execution_dir: Path, artifact_dir: Path, names: tuple[str, ...],
    ) -> None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            source = execution_dir / name
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_file():
                raise ConfigError(f"refusing unsafe adjudication artifact: {source}")
            shutil.copy2(source, artifact_dir / name)

    def _stage_blind_hunter_evidence(
        self, workspace: Path, answers: list[dict[str, Any]],
    ) -> None:
        evidence_root = workspace / ".swarm" / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        index: list[dict[str, Any]] = []
        for label, answer in zip(answer_labels(len(answers)), answers, strict=True):
            hunter_id = str(answer["hunter_id"])
            hunter_workspace = self.run_dir / "workspaces" / "hunters" / hunter_id
            hunter_artifacts = self.run_dir / "hunters" / hunter_id
            if hunter_workspace.is_dir():
                preserve_workspace_artifacts(hunter_workspace, hunter_artifacts)
            source = hunter_artifacts / "evidence"
            destination = evidence_root / label
            artifacts: list[dict[str, Any]] = []
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(
                    source,
                    destination,
                    symlinks=False,
                    copy_function=copy_independent_file,
                )
                for path in sorted(destination.rglob("*")):
                    if path.is_symlink():
                        raise ConfigError(f"unsafe symlink in blind hunter evidence: {path}")
                    if path.is_file():
                        artifacts.append(
                            {
                                "path": path.relative_to(workspace).as_posix(),
                                "sha256": sha256_file(path),
                                "size_bytes": path.stat().st_size,
                            }
                        )
            else:
                destination.mkdir()
            index.append({"answer_id": label, "artifacts": artifacts})
        write_json(workspace / ".swarm" / "evidence-index.json", {"answers": index})

    def _sealed_answers(self, hunter_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results_by_id = {item["hunter_id"]: item for item in hunter_results}
        answers: list[dict[str, Any]] = []
        for engine in self.config.hunters:
            result = results_by_id.get(engine.id, {})
            path = self.run_dir / "hunters" / engine.id / "answer.json"
            if not result.get("answer_sealed") or not path.is_file():
                continue
            answers.append(
                {
                    "hunter_id": engine.id,
                    "answer": read_json(path),
                    "sha256": sha256_file(path),
                }
            )
        return randomized_answer_order(answers)

    async def _execute_adjudication_phase(
        self,
        engine: AnalyzerConfig,
        spec: CommandSpec,
        *,
        artifact_dir: Path,
        execution_dir: Path,
        role: str,
        validate: Callable[[Any], bool],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        self._emit(
            f"[{role}] started: {engine.provider}/{engine.effective_model} "
            f"effort={engine.reasoning_effort}"
        )
        started = time.monotonic()
        native_codex = engine.harness in {"codex", "codex-exec"} and not engine.command
        retry_limit = self.config.codex_transport_retries if native_codex else 0
        runtime_budget = (
            engine.timeout_seconds * (retry_limit + 1)
            + self.config.codex_retry_delay_seconds * retry_limit
        )
        task = asyncio.create_task(
            self._execute_with_transport_retries(
                engine,
                spec,
                role=role,
                stdout_path=execution_dir / "stdout.jsonl",
                stderr_path=execution_dir / "stderr.log",
            )
        )
        while not task.done():
            done, _pending = await asyncio.wait({task}, timeout=60)
            if not done:
                elapsed = int(time.monotonic() - started)
                remaining = max(0, int(runtime_budget - elapsed))
                self._emit(
                    f"[{role}] still running: elapsed {elapsed}s; "
                    f"timeout budget remaining {remaining}s"
                )
        result = await task
        artifact: Any = None
        if spec.artifact_path and spec.artifact_path.is_file():
            try:
                artifact = read_json(spec.artifact_path)
            except ConfigError as exc:
                result["artifact_error"] = str(exc)
        else:
            artifact = parse_json_output(execution_dir / "stdout.jsonl")
            if isinstance(artifact, dict) and spec.artifact_path:
                write_json(spec.artifact_path, artifact)
        if not validate(artifact):
            artifact = None
            result["artifact_error"] = result.get(
                "artifact_error", "missing or invalid structured artifact"
            )
        result["artifact_valid"] = artifact is not None
        result["artifact_sha256"] = (
            sha256_file(spec.artifact_path)
            if artifact is not None and spec.artifact_path and spec.artifact_path.is_file()
            else None
        )
        published = ["stdout.jsonl", "stderr.log"]
        if spec.artifact_path is not None:
            published.append(spec.artifact_path.name)
        self._publish_stage_files(execution_dir, artifact_dir, tuple(published))
        write_json(artifact_dir / "result.json", result)
        if artifact is not None and result.get("status") == "completed":
            self._emit(f"[{role}] completed")
        else:
            detail = (
                self._json_error_diagnostic(execution_dir / "stdout.jsonl")
                or result.get("artifact_error")
                or result.get("error")
                or "no valid structured artifact"
            )
            self._emit(f"[{role}] FAILED ({result['status']}): {detail}")
        return artifact, result

    async def _run_baseline(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        artifact_dir = self.run_dir / "baseline"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        workspace, execution_dir = self._new_adjudication_stage(
            "baseline", artifact_dir
        )
        if self.context_snapshot.is_dir():
            self._stage_context(workspace, label="baseline")
        private_dir = workspace / ".swarm"
        private_dir.mkdir(exist_ok=True)
        schema_path = self._stage_output_schema(workspace, "baseline.schema.json")
        history = self._analyzer_history()
        write_json(private_dir / "history.json", history)
        prompt = build_baseline_instruction(
            self.agent_instruction,
            has_context=self.context_snapshot.is_dir(),
            has_history=bool(history["prior_rounds"]),
        )
        (artifact_dir / "instruction.md").write_text(prompt, encoding="utf-8")
        spec = build_baseline_command(
            self.config,
            self.config.analyzer,
            run_id=f"{self.session_id}/{self.round_id}/baseline",
            workspace=workspace,
            artifact_dir=execution_dir,
            prompt=prompt,
            schema_path=schema_path,
        )
        spec = self._isolate_adjudication_runtime(spec, workspace, "baseline")
        write_json(
            artifact_dir / "launch.json",
            {
                **_engine_manifest(self.config.analyzer),
                "phase": "baseline_before_hunter_disclosure",
                "argv": self._redacted_argv(spec.argv, prompt),
                "environment_names": sorted(spec.env),
            },
        )

        def valid(value: Any) -> bool:
            if schema_error(value, "baseline.schema.json") is not None:
                return False
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("summary"), str)
                or not isinstance(value.get("candidate_findings"), list)
                or not isinstance(value.get("uncertainties"), list)
                or not isinstance(value.get("confidence"), (int, float))
            ):
                return False
            return all(
                isinstance(item, dict)
                and isinstance(item.get("title"), str)
                and bool(item["title"].strip())
                and isinstance(item.get("assessment"), str)
                and bool(item["assessment"].strip())
                and isinstance(item.get("evidence"), list)
                and all(isinstance(evidence, str) for evidence in item["evidence"])
                and self._valid_evidence_contract(item, workspace)
                for item in value["candidate_findings"]
            )

        baseline, result = await self._execute_adjudication_phase(
            self.config.analyzer,
            spec,
            artifact_dir=artifact_dir,
            execution_dir=execution_dir,
            role="baseline-first adjudication",
            validate=valid,
        )
        if baseline is not None:
            try:
                preserve_reproduction_references(
                    workspace,
                    list(baseline.get("candidate_findings", [])),
                    artifact_dir,
                )
            except OSError as exc:
                result["artifact_valid"] = False
                result["artifact_error"] = f"reproduction evidence could not be sealed: {exc}"
                write_json(artifact_dir / "result.json", result)
                raise ConfigError(result["artifact_error"]) from exc
        return baseline, result

    async def _run_analyzer(
        self, answers: list[dict[str, Any]], baseline: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        artifact_dir = self.run_dir / "analysis"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        workspace, execution_dir = self._new_adjudication_stage(
            "primary", artifact_dir
        )
        if self.context_snapshot.is_dir():
            self._stage_context(workspace)
        private_dir = workspace / ".swarm"
        private_dir.mkdir(exist_ok=True)
        schema_path = self._stage_output_schema(workspace, "analysis.schema.json")
        self._stage_blind_hunter_evidence(workspace, answers)
        answers_path = private_dir / "answers.json"
        blind_bundle = build_blind_bundle(self.agent_instruction, answers)
        write_json(answers_path, blind_bundle)
        write_json(artifact_dir / "blind-answers.json", blind_bundle)
        shutil.copy2(
            private_dir / "evidence-index.json",
            artifact_dir / "blind-evidence-index.json",
        )
        history = self._analyzer_history()
        write_json(private_dir / "history.json", history)
        if baseline is not None:
            write_json(private_dir / "baseline.json", baseline)
        prompt = build_analyzer_instruction(
            answer_count=len(answers),
            has_context=self.context_snapshot.is_dir(),
            has_history=bool(history["prior_rounds"]),
            has_baseline=baseline is not None,
        )
        (artifact_dir / "instruction.md").write_text(prompt, encoding="utf-8")
        spec = build_analyzer_command(
            self.config,
            self.config.analyzer,
            run_id=f"{self.session_id}/{self.round_id}",
            workspace=workspace,
            artifact_dir=execution_dir,
            answers_path=answers_path,
            prompt=prompt,
            schema_path=schema_path,
        )
        spec = self._isolate_adjudication_runtime(spec, workspace, "primary")
        write_json(
            artifact_dir / "launch.json",
            {
                **_engine_manifest(self.config.analyzer),
                "blind_hunter_identity": self.config.analyzer.blind_hunter_identity,
                "argv": self._redacted_argv(spec.argv, prompt),
                "environment_names": sorted(spec.env),
                "history_rounds": len(history["prior_rounds"]),
            },
        )
        self._emit(
            f"[primary analysis] started: {self.config.analyzer.effective_model} "
            f"effort={self.config.analyzer.reasoning_effort}"
        )
        analysis_started = time.monotonic()
        native_codex = (
            self.config.analyzer.harness in {"codex", "codex-exec"}
            and not self.config.analyzer.command
        )
        retry_limit = self.config.codex_transport_retries if native_codex else 0
        runtime_budget = (
            self.config.analyzer.timeout_seconds * (retry_limit + 1)
            + self.config.codex_retry_delay_seconds * retry_limit
        )
        analysis_task = asyncio.create_task(
            self._execute_with_transport_retries(
                self.config.analyzer,
                spec,
                role="primary analysis",
                stdout_path=execution_dir / "stdout.jsonl",
                stderr_path=execution_dir / "stderr.log",
            )
        )
        while not analysis_task.done():
            done, _pending = await asyncio.wait({analysis_task}, timeout=60)
            if not done:
                elapsed = int(time.monotonic() - analysis_started)
                remaining = max(0, int(runtime_budget - elapsed))
                self._emit(
                    f"[primary analysis] still running: elapsed {elapsed}s; "
                    f"timeout budget remaining {remaining}s"
                )
        result = await analysis_task
        thread_id = self._codex_thread_id(execution_dir / "stdout.jsonl")
        if thread_id and not self.config.analyzer.command:
            result["codex_thread_id"] = thread_id
        analysis: Any = None
        if spec.artifact_path and spec.artifact_path.is_file():
            try:
                analysis = read_json(spec.artifact_path)
            except ConfigError as exc:
                result["artifact_error"] = str(exc)
        else:
            analysis = parse_json_output(execution_dir / "stdout.jsonl")
            if isinstance(analysis, dict) and spec.artifact_path:
                write_json(spec.artifact_path, analysis)
        assessments = analysis.get("answer_assessments") if isinstance(analysis, dict) else None
        analysis_schema_problem = schema_error(analysis, "analysis.schema.json")
        assessment_ids = (
            {item.get("answer_id") for item in assessments if isinstance(item, dict)}
            if isinstance(assessments, list)
            else set()
        )
        expected_ids = set(answer_labels(len(answers)))
        findings = analysis.get("validated_findings") if isinstance(analysis, dict) else None
        finding_sources_valid = isinstance(findings, list) and all(
            isinstance(finding, dict)
            and isinstance(finding.get("title"), str)
            and bool(finding["title"].strip())
            and isinstance(finding.get("validation"), str)
            and bool(finding["validation"].strip())
            and isinstance(finding.get("source_answer_ids"), list)
            and bool(finding["source_answer_ids"])
            and all(isinstance(source_id, str) for source_id in finding["source_answer_ids"])
            and len(set(finding["source_answer_ids"])) == len(finding["source_answer_ids"])
            and set(finding["source_answer_ids"]) <= expected_ids
            and self._valid_evidence_contract(finding, workspace)
            for finding in findings
        )
        if (
            analysis_schema_problem is not None
            or not isinstance(analysis, dict)
            or not isinstance(analysis.get("recommended_answer"), str)
            or not analysis["recommended_answer"].strip()
            or len(assessments or []) != len(answers)
            or assessment_ids != expected_ids
            or not finding_sources_valid
        ):
            analysis = None
            result["artifact_error"] = result.get(
                "artifact_error",
                analysis_schema_problem or "missing or invalid structured analysis",
            )
        result["analysis_valid"] = analysis is not None
        result["analysis_sha256"] = (
            sha256_file(spec.artifact_path)
            if analysis is not None and spec.artifact_path and spec.artifact_path.is_file()
            else None
        )
        self._publish_stage_files(
            execution_dir,
            artifact_dir,
            ("stdout.jsonl", "stderr.log", "analysis.json"),
        )
        if analysis is not None and result.get("status") == "completed":
            try:
                preserve_reproduction_references(
                    workspace,
                    list(analysis.get("validated_findings", [])),
                    artifact_dir,
                )
            except OSError as exc:
                result["analysis_valid"] = False
                result["artifact_error"] = f"reproduction evidence could not be sealed: {exc}"
                write_json(artifact_dir / "result.json", result)
                raise ConfigError(result["artifact_error"]) from exc
            write_json(
                artifact_dir / "answer-map.json",
                dict(zip(answer_labels(len(answers)), (item["hunter_id"] for item in answers), strict=True)),
            )
            write_json(artifact_dir / "result.json", result)
            self._emit("[primary analysis] completed")
        else:
            write_json(artifact_dir / "result.json", result)
            detail = (
                self._json_error_diagnostic(execution_dir / "stdout.jsonl")
                or result.get("artifact_error")
                or result.get("error")
                or "no valid analysis"
            )
            self._emit(f"[primary analysis] FAILED ({result['status']}): {detail}")
        return analysis, result

    async def _run_secondary_verifier(
        self,
        answers: list[dict[str, Any]],
        analysis: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        engine = self.config.secondary_analyzer
        if engine is None:
            raise ConfigError("secondary verifier is not configured")
        artifact_dir = self.run_dir / "secondary-analysis"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        workspace, execution_dir = self._new_adjudication_stage(
            "secondary", artifact_dir
        )
        if self.context_snapshot.is_dir():
            self._stage_context(workspace, label="secondary verifier")
        private_dir = workspace / ".swarm"
        private_dir.mkdir(exist_ok=True)
        schema_path = self._stage_output_schema(workspace, "verification.schema.json")
        self._stage_blind_hunter_evidence(workspace, answers)
        primary_artifacts = (
            self.run_dir / "analysis" / "evidence" / ".swarm" / "artifacts"
        )
        if primary_artifacts.is_dir():
            shutil.copytree(
                primary_artifacts,
                private_dir / "artifacts",
                symlinks=False,
                copy_function=copy_independent_file,
            )
        answers_path = private_dir / "answers.json"
        primary_path = private_dir / "primary-analysis.json"
        write_json(answers_path, build_blind_bundle(self.agent_instruction, answers))
        write_json(primary_path, analysis)
        finding_count = len(analysis.get("validated_findings", []))
        prompt = build_verifier_instruction(
            finding_count=finding_count,
            has_context=self.context_snapshot.is_dir(),
        )
        (artifact_dir / "instruction.md").write_text(prompt, encoding="utf-8")
        spec = build_verifier_command(
            self.config,
            engine,
            run_id=f"{self.session_id}/{self.round_id}/secondary",
            workspace=workspace,
            artifact_dir=execution_dir,
            answers_path=answers_path,
            primary_analysis_path=primary_path,
            prompt=prompt,
            schema_path=schema_path,
        )
        spec = self._isolate_adjudication_runtime(spec, workspace, "secondary")
        write_json(
            artifact_dir / "launch.json",
            {
                **_engine_manifest(engine),
                "blind_hunter_identity": engine.blind_hunter_identity,
                "phase": "secondary_finding_confirmation",
                "argv": self._redacted_argv(spec.argv, prompt),
                "environment_names": sorted(spec.env),
            },
        )

        def valid(value: Any) -> bool:
            if schema_error(value, "verification.schema.json") is not None:
                return False
            assessments = value.get("finding_assessments") if isinstance(value, dict) else None
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("summary"), str)
                or not isinstance(value.get("recommended_answer_assessment"), str)
                or not isinstance(value.get("confidence"), (int, float))
                or not isinstance(assessments, list)
                or len(assessments) != finding_count
            ):
                return False
            indices = []
            for item in assessments:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("finding_index"), int)
                    or item.get("status") not in {"confirmed", "rejected", "uncertain"}
                    or item.get("validation_method") not in self._VALIDATION_METHODS
                    or not isinstance(item.get("assessment"), str)
                    or not item["assessment"].strip()
                    or not isinstance(item.get("evidence"), list)
                    or not all(isinstance(evidence, str) for evidence in item["evidence"])
                ):
                    return False
                indices.append(item["finding_index"])
            return set(indices) == set(range(finding_count)) and len(indices) == len(set(indices))

        return await self._execute_adjudication_phase(
            engine,
            spec,
            artifact_dir=artifact_dir,
            execution_dir=execution_dir,
            role="secondary verification",
            validate=valid,
        )

    def _accepted_analysis(
        self,
        analysis: dict[str, Any],
        verification: dict[str, Any] | None,
    ) -> dict[str, Any]:
        accepted = copy.deepcopy(analysis)
        if verification is None:
            return accepted
        findings = list(accepted.get("validated_findings", []))
        assessments = {
            item["finding_index"]: item
            for item in verification.get("finding_assessments", [])
            if isinstance(item, dict) and isinstance(item.get("finding_index"), int)
        }
        confirmed = [
            finding
            for index, finding in enumerate(findings)
            if assessments.get(index, {}).get("status") == "confirmed"
        ]
        unconfirmed = [
            {
                "finding": finding,
                "verification": assessments.get(index),
            }
            for index, finding in enumerate(findings)
            if assessments.get(index, {}).get("status") != "confirmed"
        ]
        accepted["validated_findings"] = confirmed
        accepted["secondary_verification"] = {
            "summary": verification.get("summary", ""),
            "confirmed_count": len(confirmed),
            "unconfirmed_findings": unconfirmed,
        }
        return accepted

