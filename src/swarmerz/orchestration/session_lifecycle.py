# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Callable

from .artifacts import preserve_workspace_artifacts, read_json, write_json
from .models import ConfigError
from .protocol_support import (
    adjudicator_disclosure as _adjudicator_disclosure,
    engine_manifest as _engine_manifest,
    new_id as _new_id,
    utc_now as _utc_now,
)
from .session_context import (
    copy_independent_file as _copy_independent_file,
    context_copy_ignore as _context_copy_ignore,
    context_digest as _context_digest,
    validate_context_tree as _validate_context_tree,
)
from .storage import format_bytes


class SessionLifecycleMixin:
    """Own session locking, frozen context, history, manifests, and cleanup."""

    def _acquire_lock(self) -> None:
        self._session_lock.acquire()

    def _release_lock(self) -> None:
        self._session_lock.release()

    def _freeze_context_snapshot(self) -> None:
        """Make the canonical snapshot visibly non-writable between checks."""
        for directory, names, files in os.walk(self.context_snapshot, followlinks=False):
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode & ~0o222)
            for name in names:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode & ~0o222)
        self.context_snapshot.chmod(self.context_snapshot.stat().st_mode & ~0o222)

    def _stage_context(self, workspace: Path, *, label: str | None = None) -> None:
        if not self.context_snapshot.is_dir():
            return
        copied_files = 0

        def copy_file(source: str, destination: str) -> str:
            nonlocal copied_files
            try:
                result = _copy_independent_file(source, destination)
            except OSError as exc:
                raise ConfigError(
                    f"cannot create an independent context view in {workspace}: {exc}"
                ) from exc
            copied_files += 1
            return result

        shutil.copytree(
            self.context_snapshot,
            workspace / "context",
            symlinks=True,
            copy_function=copy_file,
        )
        self._emit(
            f"[setup] [{label or workspace.name}] independent context staged "
            f"({copied_files} files; copy-on-write when supported)"
        )

    def _verify_context_snapshot(self, phase: str) -> str | None:
        """Re-hash the canonical snapshot and fail closed on any content change."""
        expected = self.session.get("context_sha256") if self.session else None
        if expected is None:
            if self.context_snapshot.exists():
                raise ConfigError(
                    "frozen context exists without a recorded integrity digest"
                )
            return None
        if not isinstance(expected, str) or not expected:
            raise ConfigError("session context_sha256 is missing or invalid")
        if not self.context_snapshot.is_dir():
            raise ConfigError("recorded frozen context snapshot is missing")
        self._emit(f"[integrity] verifying frozen context: {phase}")
        actual = _context_digest(self.context_snapshot)
        passed = actual == expected
        if self.manifest:
            context_record = dict(self.manifest.get("context") or {})
            checks = list(context_record.get("integrity_checks") or [])
            checks.append(
                {
                    "phase": phase,
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                    "passed": passed,
                    "checked_at": _utc_now(),
                }
            )
            context_record["integrity_checks"] = checks
            self._update_manifest(context=context_record)
        if not passed:
            raise ConfigError(
                "frozen context integrity mismatch during "
                f"{phase}: expected {expected}, found {actual}"
            )
        self._freeze_context_snapshot()
        self._emit(f"[integrity] frozen context verified: {phase}")
        return actual

    def _copytree_with_progress(
        self, source: Path, destination: Path, *, label: str,
        symlinks: bool, ignore: Callable[..., Any] | None = None,
    ) -> None:
        copied_files = 0
        copied_bytes = 0
        started = time.monotonic()
        last_report = started
        self._emit(f"[setup] {label}: copying {source} (this may take several minutes)")

        def copy_file(from_path: str, to_path: str) -> str:
            nonlocal copied_files, copied_bytes, last_report
            result = shutil.copy2(from_path, to_path)
            copied_files += 1
            copied_bytes += os.path.getsize(from_path)
            now = time.monotonic()
            if now - last_report >= 10:
                self._emit(
                    f"[setup] {label}: {copied_files} files, "
                    f"{copied_bytes / 1024**2:.0f} MiB copied"
                )
                last_report = now
            return result

        shutil.copytree(
            source, destination, symlinks=symlinks, ignore=ignore,
            copy_function=copy_file,
        )
        self._emit(
            f"[setup] {label}: done ({copied_files} files, "
            f"{copied_bytes / 1024**2:.0f} MiB in {time.monotonic() - started:.0f}s)"
        )

    def _open_session(self) -> None:
        self._acquire_lock()
        if self.session_path.is_file():
            session = read_json(self.session_path)
            if not isinstance(session, dict):
                raise ConfigError(f"invalid session metadata: {self.session_path}")
            frozen_source = session.get("context_source")
            if self.context_source is not None:
                if frozen_source is None or str(self.context_source) != frozen_source:
                    raise ConfigError("an existing session cannot switch to a different context directory")
            if bool(frozen_source) != self.context_snapshot.is_dir():
                raise ConfigError("session context snapshot is missing or inconsistent")
            self.session = session
            if frozen_source is not None:
                self._verify_context_snapshot("session-open")
                self._emit(f"[setup] reusing frozen session context: {self.context_snapshot}")
        else:
            skipped_context_symlinks: tuple[str, ...] = ()
            if self.context_source is not None:
                marker = self.context_snapshot.parent / ".context-copying.json"
                if marker.is_file() and self.context_snapshot.is_dir():
                    previous = read_json(marker)
                    if not isinstance(previous, dict) or previous.get("source") != str(self.context_source):
                        raise ConfigError(
                            f"interrupted context snapshot has a different source: {self.context_snapshot}"
                        )
                    self._emit(
                        f"[setup] clearing app-owned snapshot from an interrupted copy: "
                        f"{self.context_snapshot}"
                    )
                    shutil.rmtree(self.context_snapshot)
                elif self.context_snapshot.exists():
                    raise ConfigError(
                        f"incomplete context snapshot exists without a copy marker: "
                        f"{self.context_snapshot}"
                    )
                self._emit(f"[setup] validating local context: {self.context_source}")
                skipped_context_symlinks = _validate_context_tree(
                    self.context_source,
                    allow_symlinks=self.config.context.allow_symlinks,
                    exclude=self.config.context.exclude,
                )
                if skipped_context_symlinks:
                    preview = ", ".join(skipped_context_symlinks[:3])
                    suffix = "" if len(skipped_context_symlinks) <= 3 else ", ..."
                    self._emit(
                        f"[setup] skipping {len(skipped_context_symlinks)} context "
                        f"symlink(s) because context.allow_symlinks is false: "
                        f"{preview}{suffix}"
                    )
                self.context_snapshot.parent.mkdir(parents=True, exist_ok=True)
                write_json(marker, {"source": str(self.context_source), "started_at": _utc_now()})
                self._copytree_with_progress(
                    self.context_source,
                    self.context_snapshot,
                    label="freezing session context",
                    symlinks=self.config.context.allow_symlinks,
                    ignore=_context_copy_ignore(
                        allow_symlinks=self.config.context.allow_symlinks,
                        exclude=self.config.context.exclude,
                    ),
                )
                self._emit("[setup] hashing frozen context for session integrity")
                last_hash_report = time.monotonic()

                def hash_progress(files: int, size: int) -> None:
                    nonlocal last_hash_report
                    now = time.monotonic()
                    if now - last_hash_report >= 10:
                        self._emit(
                            f"[setup] hashing frozen context: {files} files, "
                            f"{size / 1024**2:.0f} MiB read"
                        )
                        last_hash_report = now

                context_sha256 = _context_digest(self.context_snapshot, hash_progress)
                self._emit("[setup] frozen context hash complete")
                self._freeze_context_snapshot()
            else:
                context_sha256 = None
            self.session = {
                "schema_version": 1,
                "session_id": self.session_id,
                "created_at": _utc_now(),
                "context_source": str(self.context_source) if self.context_source else None,
                "context_sha256": context_sha256,
                "allow_symlinks": self.config.context.allow_symlinks,
                "context_skipped_symlink_count": len(skipped_context_symlinks),
                "context_exclude": list(self.config.context.exclude),
                "rounds": [],
            }
            write_json(self.session_path, self.session)
            if self.context_source is not None:
                marker.unlink(missing_ok=True)

        rounds = self.session.get("rounds", [])
        if not isinstance(rounds, list):
            raise ConfigError("session.rounds must be an array")
        self.round_number = len(rounds) + 1
        self.round_id = self.requested_round_id or f"{self.round_number:04d}-{_new_id('round')}"
        if any(item.get("round_id") == self.round_id for item in rounds if isinstance(item, dict)):
            raise ConfigError(f"round already exists: {self.round_id}")
        self.run_dir = self.session_dir / "rounds" / self.round_id
        if self.run_dir.exists():
            raise ConfigError(f"round directory already exists: {self.run_dir}")
        self.run_dir.mkdir(parents=True)
        rounds.append(
            {
                "number": self.round_number,
                "round_id": self.round_id,
                "status": "preparing",
                "started_at": _utc_now(),
            }
        )
        self.session["rounds"] = rounds
        write_json(self.session_path, self.session)

    def _set_session_round_status(self, status: str) -> None:
        for record in self.session["rounds"]:
            if record.get("round_id") == self.round_id:
                record["status"] = status
                if status not in {"preparing", "running"}:
                    record["completed_at"] = _utc_now()
                break
        write_json(self.session_path, self.session)

    def _initialize(self) -> None:
        self._open_session()
        (self.run_dir / "instruction.txt").write_text(self.instruction + "\n", encoding="utf-8")
        self.manifest = {
            "schema_version": 1,
            "session_id": self.session_id,
            "round_id": self.round_id,
            "round_number": self.round_number,
            "status": "preparing",
            "started_at": _utc_now(),
            "context": {
                "present": self.context_snapshot.is_dir(),
                "sha256": self.session.get("context_sha256"),
                "frozen_for_session": True,
                "workspace_copy_strategy": "reflink-or-copy",
                "integrity_checks": [],
            },
            "instruction_sha256": hashlib.sha256(self.instruction.encode()).hexdigest(),
            "required_answer_count": self.config.minimum_answers,
            "configured_answer_count": len(self.config.hunters),
            "opencode_worker_timeout_seconds": self.config.opencode_worker_timeout_seconds,
            "opencode_worker_poll_seconds": self.config.opencode_worker_poll_seconds,
            "opencode_answer_quiet_seconds": self.config.opencode_answer_quiet_seconds,
            "opencode_orphan_quiet_seconds": self.config.opencode_orphan_quiet_seconds,
            "codex_transport_retries": self.config.codex_transport_retries,
            "codex_retry_delay_seconds": self.config.codex_retry_delay_seconds,
            "hunters": [_engine_manifest(engine) for engine in self.config.hunters],
            "analyzer": _engine_manifest(self.config.analyzer),
            "secondary_analyzer": (
                _engine_manifest(self.config.secondary_analyzer)
                if self.config.secondary_analyzer is not None
                else None
            ),
            "adjudicator_disclosure": _adjudicator_disclosure(self.config),
            "independence": {
                "top_level_hunters_share_messages": False,
                "each_hunter_receives_only_its_own_prior_answers": True,
                "all_hunters_receive_prior_validated_findings": True,
                "orchestrator_discloses_hunter_outputs_only_after_all_finish": True,
                "same_user_hostile_process_isolation": False,
                "workspace_context_inodes_shared": False,
                "adjudicator_receives_blind_current_answers": True,
                "adjudicator_uses_opaque_scratch_staging": True,
                "blind_answer_order_randomized": True,
                "fresh_primary_process_per_round": True,
                "baseline_first": self.config.analyzer.baseline_first,
                "secondary_confirmation": self.config.secondary_analyzer is not None,
            },
        }
        write_json(self.run_dir / "manifest.json", self.manifest)
        self._set_session_round_status("running")
        self._emit(
            f"session={self.session_id} round={self.round_number} ({self.round_id})"
        )

    def _update_manifest(self, **updates: Any) -> None:
        self.manifest.update(updates)
        write_json(self.run_dir / "manifest.json", self.manifest)

    def _activate_managed_storage(self) -> None:
        if self._storage is None:
            raise ConfigError("managed storage is missing from the loaded configuration")
        report = self._storage.activate(f"{self.session_id}/{self.round_id}")
        self._storage_active = True
        self._emit(f"[storage] SSD cache: {self.config.storage.cache_root}")
        if report and report.get("deleted_bytes"):
            self._emit(
                "[storage] automatic maintenance reclaimed "
                f"{format_bytes(int(report['deleted_bytes']))}"
            )

    @staticmethod
    def _workspace_inventory(path: Path) -> tuple[int, int]:
        files = 0
        size = 0
        if not path.is_dir() or path.is_symlink():
            return files, size
        for directory, _names, names in os.walk(path, followlinks=False):
            for name in names:
                candidate = Path(directory) / name
                try:
                    if candidate.is_file() and not candidate.is_symlink():
                        files += 1
                        size += candidate.stat().st_size
                except OSError:
                    continue
        return files, size

    def _finalize_workspace_storage(self) -> dict[str, Any]:
        workspaces = self.run_dir / "workspaces"
        evidence: dict[str, Any] = {}
        workspace_artifact_pairs = [
            *(
                (
                    self.run_dir / "workspaces" / "hunters" / engine.id,
                    self.run_dir / "hunters" / engine.id,
                    f"hunter:{engine.id}",
                )
                for engine in self.config.hunters
            ),
            (
                self.run_dir / "workspaces" / "baseline-analyzer",
                self.run_dir / "baseline",
                "baseline",
            ),
            (
                self.run_dir / "workspaces" / "analyzer",
                self.run_dir / "analysis",
                "analysis",
            ),
            (
                self.run_dir / "workspaces" / "secondary-analyzer",
                self.run_dir / "secondary-analysis",
                "secondary-analysis",
            ),
            *(
                (workspace, artifact_dir, f"blind-{phase}")
                for workspace, artifact_dir, phase in self._adjudication_workspaces
            ),
        ]
        try:
            for workspace, artifact_dir, label in workspace_artifact_pairs:
                if not workspace.is_dir():
                    continue
                result = preserve_workspace_artifacts(workspace, artifact_dir)
                if result["artifact_count"]:
                    evidence[label] = result
        except OSError as exc:
            self._emit(
                f"[storage] workspace retained because durable evidence could not be sealed: {exc}"
            )
            return {
                "cache_root": str(self.config.storage.cache_root),
                "workspace_cleanup": "retained_after_evidence_error",
                "error": str(exc),
                "evidence": evidence,
            }

        files, logical_bytes = self._workspace_inventory(workspaces)
        if not self.config.storage.cleanup_successful_workspaces:
            return {
                "cache_root": str(self.config.storage.cache_root),
                "workspace_cleanup": "disabled",
                "workspace_files": files,
                "workspace_logical_bytes": logical_bytes,
                "evidence": evidence,
            }
        if workspaces.is_symlink():
            return {
                "cache_root": str(self.config.storage.cache_root),
                "workspace_cleanup": "retained_unsafe_path",
                "error": f"workspace root is a symlink: {workspaces}",
                "evidence": evidence,
            }
        expected_parent = self.run_dir.resolve()
        resolved = workspaces.resolve(strict=False)
        if resolved.parent != expected_parent or resolved.name != "workspaces":
            return {
                "cache_root": str(self.config.storage.cache_root),
                "workspace_cleanup": "retained_unsafe_path",
                "error": f"workspace root escaped round directory: {resolved}",
                "evidence": evidence,
            }

        try:
            if workspaces.exists():
                # Workspace context files have independent inodes. Directory
                # permissions are relaxed only so the disposable tree can be
                # removed after its durable evidence has been staged.
                for directory, names, _files in os.walk(workspaces, followlinks=False):
                    directory_path = Path(directory)
                    directory_path.chmod(directory_path.stat().st_mode | stat.S_IRWXU)
                    for name in names:
                        candidate = directory_path / name
                        if not candidate.is_symlink():
                            candidate.chmod(candidate.stat().st_mode | stat.S_IRWXU)
                shutil.rmtree(workspaces)
        except OSError as exc:
            self._emit(f"[storage] completed workspace cleanup failed; files retained: {exc}")
            return {
                "cache_root": str(self.config.storage.cache_root),
                "workspace_cleanup": "failed",
                "workspace_files": files,
                "workspace_logical_bytes": logical_bytes,
                "error": str(exc),
                "evidence": evidence,
            }
        self._emit(
            f"[storage] removed {files} disposable workspace files "
            f"({format_bytes(logical_bytes)} logical)"
        )
        return {
            "cache_root": str(self.config.storage.cache_root),
            "workspace_cleanup": "completed",
            "workspace_files_removed": files,
            "workspace_logical_bytes_removed": logical_bytes,
            "evidence": evidence,
        }

    def _prior_completed_rounds(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self.session.get("rounds", [])
            if isinstance(record, dict)
            and record.get("round_id") != self.round_id
            and record.get("status") == "completed"
        ]

    def _hunter_history(self, hunter_id: str) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        for record in self._prior_completed_rounds():
            prior = self.session_dir / "rounds" / str(record["round_id"])
            instruction_path = prior / "instruction.txt"
            answer_path = prior / "hunters" / hunter_id / "answer.json"
            if instruction_path.is_file() and answer_path.is_file():
                history.append(
                    {
                        "round_number": record["number"],
                        "instruction": instruction_path.read_text(encoding="utf-8").strip(),
                        "your_answer": read_json(answer_path),
                    }
                )
        return {
            "prior_rounds": history,
            "validated_findings": self._validated_findings_history(),
        }

    def _validated_findings_history(self) -> list[dict[str, Any]]:
        """Return the shared, attribution-free baseline from completed rounds.

        A finding crosses hunter boundaries only after a blind analyzer accepted
        it and linked it to at least one sealed answer. Provider, model, hunter,
        and blind-answer identifiers are deliberately not copied into the next
        round's hunter workspaces.
        """
        history: list[dict[str, Any]] = []
        allowed_severities = {
            "critical", "high", "medium", "low", "informational", "unknown",
        }
        for record in self._prior_completed_rounds():
            prior = self.session_dir / "rounds" / str(record["round_id"])
            instruction_path = prior / "instruction.txt"
            accepted_path = prior / "analysis" / "accepted-analysis.json"
            analysis_path = (
                accepted_path if accepted_path.is_file() else prior / "analysis" / "analysis.json"
            )
            if not instruction_path.is_file() or not analysis_path.is_file():
                continue
            analysis = read_json(analysis_path)
            raw_findings = analysis.get("validated_findings") if isinstance(analysis, dict) else None
            if not isinstance(raw_findings, list):
                continue
            findings: list[dict[str, Any]] = []
            for finding in raw_findings:
                if not isinstance(finding, dict):
                    continue
                title = finding.get("title")
                severity = finding.get("severity")
                validation = finding.get("validation")
                evidence = finding.get("evidence")
                source_ids = finding.get("source_answer_ids")
                if (
                    not isinstance(title, str)
                    or not title.strip()
                    or severity not in allowed_severities
                    or not isinstance(validation, str)
                    or not validation.strip()
                    or not isinstance(evidence, list)
                    or not all(isinstance(item, str) for item in evidence)
                    or not isinstance(source_ids, list)
                    or not source_ids
                    or not all(isinstance(item, str) and item for item in source_ids)
                ):
                    continue
                findings.append(
                    {
                        "title": title.strip(),
                        "severity": severity,
                        "validation": validation.strip(),
                        "evidence": evidence,
                        "provenance_class": finding.get(
                            "provenance_class", "prior_artifact_corroboration"
                        ),
                        "validation_method": finding.get(
                            "validation_method", "reasoning_only"
                        ),
                        "reproduction": finding.get(
                            "reproduction",
                            {
                                "commands": [],
                                "observed_result": "Historical adjudication did not record a reproduction contract.",
                                "artifact_refs": evidence,
                            },
                        ),
                        "supporting_hunter_count": len(set(source_ids)),
                    }
                )
            if findings:
                history.append(
                    {
                        "round_number": record["number"],
                        "round_id": record["round_id"],
                        "instruction": instruction_path.read_text(encoding="utf-8").strip(),
                        "findings": findings,
                    }
                )
        return history

    def _analyzer_history(self) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        for record in self._prior_completed_rounds():
            prior = self.session_dir / "rounds" / str(record["round_id"])
            instruction_path = prior / "instruction.txt"
            accepted_path = prior / "analysis" / "accepted-analysis.json"
            analysis_path = (
                accepted_path if accepted_path.is_file() else prior / "analysis" / "analysis.json"
            )
            if instruction_path.is_file() and analysis_path.is_file():
                history.append(
                    {
                        "round_number": record["number"],
                        "instruction": instruction_path.read_text(encoding="utf-8").strip(),
                        "your_analysis": read_json(analysis_path),
                    }
                )
        return {"prior_rounds": history}

