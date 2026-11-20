# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from typing import Any, Callable

from .artifacts import attribute_analysis, write_final_report, write_json
from .models import ConfigError


async def execute_round_protocol(
    runner: Any,
    *,
    opencode_round_slot: Callable[..., Any],
    utc_now: Callable[[], str],
    adjudicator_disclosure: Callable[[Any], dict[str, Any]],
) -> Any:
    """Execute one complete round without embedding harness recovery details.

    The runner supplies the session, hunter, and adjudication components. Keeping
    this protocol in a small module makes the product's state transitions
    reviewable without reading transport retries or OpenCode recovery code.
    """
    try:
        runner._initialize()
        runner._activate_managed_storage()
        runner._activate_resource_limits()
        runner._update_manifest(status="setup")
        await runner._prepare_harnesses()

        baseline: dict[str, Any] | None = None
        baseline_result: dict[str, Any] | None = None
        if runner.config.analyzer.baseline_first:
            runner._update_manifest(status="baseline")
            baseline, baseline_result = await runner._run_baseline()
            if baseline is None or baseline_result.get("status") != "completed":
                raise ConfigError(
                    "baseline-first adjudication failed; hunters were not launched"
                )
            runner._verify_context_snapshot("after-baseline")

        runner._update_manifest(status="hunting")
        native_opencode = any(
            engine.harness == "opencode" and not engine.command
            for engine in runner.config.hunters
        )
        if (
            runner.config.isolation.opencode_server_mode == "shared"
            and native_opencode
        ):
            async with opencode_round_slot(
                runner._emit, runner.config.storage.cache_root
            ):
                hunter_results = await runner._run_hunters()
        else:
            hunter_results = await runner._run_hunters()

        answers = runner._sealed_answers(hunter_results)
        expected_answers = len(runner.config.hunters)
        required_answers = runner.config.minimum_answers
        quorum_reached = len(answers) >= required_answers
        missing_hunters = [
            engine.id
            for engine in runner.config.hunters
            if not any(answer["hunter_id"] == engine.id for answer in answers)
        ]
        partial_hunters = [
            result["hunter_id"]
            for result in hunter_results
            if result.get("answer_sealed")
            and isinstance(result.get("worker_monitor"), dict)
            and result["worker_monitor"].get("reason") != "all_workers_terminal"
        ]
        runner._update_manifest(
            status="analyzing" if quorum_reached else "incomplete",
            hunter_results=hunter_results,
            sealed_answer_count=len(answers),
            required_answer_count=required_answers,
            configured_answer_count=expected_answers,
            degraded=len(answers) < expected_answers or bool(partial_hunters),
            missing_hunters=missing_hunters,
            partial_hunters=partial_hunters,
        )

        # No adjudicator sees hunter output unless the canonical evidence still
        # matches the digest recorded when the session was frozen.
        runner._verify_context_snapshot("before-adjudication")

        analysis: dict[str, Any] | None = None
        analyzer_result: dict[str, Any] | None = None
        verification: dict[str, Any] | None = None
        verifier_result: dict[str, Any] | None = None
        if quorum_reached:
            if len(answers) < expected_answers:
                runner._emit(
                    f"hunter quorum reached ({len(answers)}/{expected_answers}; required "
                    f"{required_answers}); primary analysis continuing without: "
                    f"{', '.join(missing_hunters)}"
                )
            analysis, analyzer_result = await runner._run_analyzer(answers, baseline)
            if analysis is not None and runner.config.secondary_analyzer is not None:
                verification, verifier_result = await runner._run_secondary_verifier(
                    answers, analysis
                )
        else:
            runner._emit(
                f"Primary analysis skipped: quorum requires {required_answers} of "
                f"{expected_answers} sealed answers; received {len(answers)}"
            )

        accepted_analysis = (
            runner._accepted_analysis(analysis, verification)
            if analysis is not None
            and (runner.config.secondary_analyzer is None or verification is not None)
            else None
        )
        runner._verify_context_snapshot("after-adjudication")
        if accepted_analysis is not None:
            write_json(
                runner.run_dir / "analysis" / "accepted-analysis.json",
                accepted_analysis,
            )
        final_analysis = (
            attribute_analysis(accepted_analysis, answers)
            if accepted_analysis is not None
            else None
        )
        write_final_report(
            runner.run_dir / "final",
            instruction=runner.instruction,
            answers=answers,
            analysis=final_analysis,
            expected_answer_count=expected_answers,
            minimum_answer_count=required_answers,
            partial_hunters=partial_hunters,
            partial_note_paths=[
                str(result["partial_notes"]["path"])
                for result in hunter_results
                if isinstance(result.get("partial_notes"), dict)
                and result["partial_notes"].get("path")
            ],
            adjudication=adjudicator_disclosure(runner.config),
            baseline=baseline,
            verification=verification,
            analyzer_result=analyzer_result,
            fanout_evidence=[
                {
                    "hunter_id": result.get("hunter_id"),
                    "fanout_evidence": result.get("fanout_evidence", "unverified"),
                    "fanout_summary": result.get("fanout_summary"),
                }
                for result in hunter_results
                if result.get("answer_sealed")
            ],
        )
        complete = (
            quorum_reached
            and analysis is not None
            and analyzer_result is not None
            and analyzer_result.get("status") == "completed"
            and (
                not runner.config.analyzer.baseline_first
                or baseline_result is not None
                and baseline_result.get("status") == "completed"
            )
            and (
                runner.config.secondary_analyzer is None
                or verifier_result is not None
                and verifier_result.get("status") == "completed"
                and verification is not None
            )
        )
        status = "completed" if complete else "incomplete"
        storage_result = (
            runner._finalize_workspace_storage()
            if complete
            else {
                "cache_root": str(runner.config.storage.cache_root),
                "workspace_cleanup": "retained_for_recovery",
                "scratch": "retained_until_expiry",
            }
        )
        runner._update_manifest(
            status=status,
            completed_at=utc_now(),
            analyzer_result=analyzer_result,
            baseline_result=baseline_result,
            verifier_result=verifier_result,
            storage=storage_result,
        )
        runner._set_session_round_status(status)
        runner._emit(f"round {status}; artifacts: {runner.run_dir}")
        return runner.run_dir
    except BaseException as exc:
        if runner.manifest:
            runner._update_manifest(
                status="failed", completed_at=utc_now(), error=str(exc)
            )
        if runner.session and runner.round_id:
            runner._set_session_round_status("failed")
        raise
    finally:
        if runner._storage_active and runner._storage is not None:
            try:
                runner._storage.deactivate(
                    successful=runner.manifest.get("status") == "completed"
                )
            except (OSError, ConfigError) as exc:
                runner._emit(f"[storage] scratch cleanup failed: {exc}")
            runner._storage_active = False
        runner._release_lock()
