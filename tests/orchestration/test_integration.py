# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from swarmerz.orchestration.config import load_config
from swarmerz.orchestration.models import CommandSpec, ConfigError, StorageConfig
from swarmerz.orchestration.opencode_permissions import OpenCodeEndpoint
from swarmerz.orchestration.runner import (
    AuditRunner, _diagnostic_tail, _settled_partial_answer,
    _wait_for_stable_final_answer,
)
from swarmerz.review import chat


class IntegrationTests(unittest.TestCase):
    def test_baseline_first_and_secondary_confirmation_gate_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hunter_script = root / "hunter.py"
            hunter_script.write_text(
                textwrap.dedent(
                    """
                    import json, pathlib, sys
                    output = pathlib.Path(sys.argv[1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({
                        "answer": "two candidate issues",
                        "key_points": [], "assumptions": [], "uncertainties": [], "sources": [],
                        "workstreams": [
                            {"name": "one", "scope": "source", "conclusion": "done"},
                            {"name": "two", "scope": "repro", "conclusion": "done"}
                        ],
                        "confidence": 0.8
                    }))
                    evidence = output.parent / "worker-evidence.txt"
                    evidence.write_text("worker evidence")
                    (output.parent / "worker-ledger.json").write_text(json.dumps({
                        "workers": [
                            {"worker_id": "one", "scope": "source", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]},
                            {"worker_id": "two", "scope": "repro", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]}
                        ]
                    }))
                    """
                ),
                encoding="utf-8",
            )
            adjudicator_script = root / "adjudicator.py"
            adjudicator_script.write_text(
                textwrap.dedent(
                    """
                    import json, pathlib, sys
                    phase, workspace, answers_arg, primary_arg, result_arg = sys.argv[1:]
                    result = pathlib.Path(result_arg)
                    result.parent.mkdir(parents=True, exist_ok=True)
                    if phase == "baseline":
                        assert not answers_arg
                        result.write_text(json.dumps({
                            "summary": "independent baseline",
                            "candidate_findings": [],
                            "uncertainties": [],
                            "confidence": 0.7
                        }))
                    elif phase == "analysis":
                        bundle = json.loads(pathlib.Path(answers_arg).read_text())
                        labels = [item["answer_id"] for item in bundle["answers"]]
                        assert (pathlib.Path(workspace) / ".swarm" / "baseline.json").is_file()
                        finding = lambda title: {
                            "title": title,
                            "severity": "high",
                            "source_answer_ids": labels,
                            "validation": "source inspected",
                            "evidence": ["context/file.c:1"],
                            "provenance_class": "independent_discovery",
                            "validation_method": "source_inspection",
                            "reproduction": {
                                "commands": [],
                                "observed_result": "inspected source",
                                "artifact_refs": ["context/file.c:1"]
                            }
                        }
                        result.write_text(json.dumps({
                            "summary": "primary",
                            "agreement": [], "disagreements": [],
                            "answer_assessments": [
                                {"answer_id": label, "strengths": [], "weaknesses": [], "factual_risks": []}
                                for label in labels
                            ],
                            "validated_findings": [finding("confirmed issue"), finding("rejected issue")],
                            "unresolved_questions": [],
                            "recommended_answer": "primary synthesis",
                            "confidence": 0.8
                        }))
                    elif phase == "verification":
                        primary = json.loads(pathlib.Path(primary_arg).read_text())
                        assert len(primary["validated_findings"]) == 2
                        result.write_text(json.dumps({
                            "summary": "one survives independent confirmation",
                            "finding_assessments": [
                                {"finding_index": 0, "status": "confirmed", "assessment": "supported", "validation_method": "source_inspection", "evidence": ["context/file.c:1"]},
                                {"finding_index": 1, "status": "rejected", "assessment": "not supported", "validation_method": "reasoning_only", "evidence": []}
                            ],
                            "recommended_answer_assessment": "usable",
                            "confidence": 0.8
                        }))
                    else:
                        raise SystemExit("unknown phase")
                    """
                ),
                encoding="utf-8",
            )
            hunters = "".join(
                textwrap.dedent(
                    f"""
                    [[hunters]]
                    id = "{hunter_id}"
                    harness = "command"
                    harness_version = "1"
                    provider = "hunter-provider"
                    model = "{hunter_id}"
                    reasoning_effort = "low"
                    supported_efforts = ["low"]
                    workers = 2
                    command = ["{sys.executable}", "{hunter_script}", "{{answer}}"]
                    """
                )
                for hunter_id in ("one", "two")
            )
            common_command = (
                f'["{sys.executable}", "{adjudicator_script}", "{{phase}}", '
                '"{workspace}", "{answers}", "{primary_analysis}", "{result}"]'
            )
            config_path = root / "swarm.toml"
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    schema_version = 1
                    output_root = "{root / 'runs'}"
                    max_parallel_hunters = 2
                    minimum_answers = 2
                    {hunters}

                    [analyzer]
                    id = "primary"
                    harness = "command"
                    harness_version = "1"
                    provider = "openai"
                    model = "codex-test"
                    reasoning_effort = "high"
                    supported_efforts = ["high"]
                    baseline_first = true
                    command = {common_command}

                    [secondary_analyzer]
                    id = "secondary"
                    harness = "command"
                    harness_version = "1"
                    provider = "independent-provider"
                    model = "verifier-test"
                    reasoning_effort = "high"
                    supported_efforts = ["high"]
                    command = {common_command}
                    """
                ),
                encoding="utf-8",
            )

            run_dir = asyncio.run(
                AuditRunner(
                    load_config(config_path),
                    "review this target",
                    session_id="dual",
                    round_id="first",
                ).run()
            )
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            self.assertTrue((run_dir / "baseline" / "baseline.json").is_file())
            self.assertTrue((run_dir / "secondary-analysis" / "verification.json").is_file())
            accepted = json.loads(
                (run_dir / "analysis" / "accepted-analysis.json").read_text()
            )
            self.assertEqual(
                [finding["title"] for finding in accepted["validated_findings"]],
                ["confirmed issue"],
            )
            report = (run_dir / "final" / "report.md").read_text()
            self.assertIn("Baseline-first assessment", report)
            self.assertIn("Secondary verification", report)
            self.assertIn("Only confirmed findings are carried forward", report)

    def test_opencode_api_probe_runs_off_supervisor_event_loop(self) -> None:
        config = SimpleNamespace(
            output_root=Path("/tmp"),
            context=SimpleNamespace(path=None),
        )
        runner = AuditRunner(config, "ordinary task", session_id="test")
        probe = mock.Mock(return_value="unused")
        offload = mock.AsyncMock(return_value="ready")

        with mock.patch("swarmerz.orchestration.runner.call_opencode_api", new=offload):
            result = asyncio.run(runner._opencode_api_call(probe, "argument", flag=True))

        self.assertEqual(result, "ready")
        offload.assert_awaited_once_with(probe, "argument", flag=True)
        probe.assert_not_called()

    def test_stack_trace_diagnostic_keeps_the_actual_transport_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "stderr.log"
            log.write_text(
                "Error: Could not reach server at http://127.0.0.1:33703\n"
                "    at internal (native:7:39)\n"
                "  [cause]: TimeoutError: The operation timed out.\n"
                "}\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _diagnostic_tail(log),
                "Error: Could not reach server at http://127.0.0.1:33703",
            )

    def test_worker_monitor_stops_after_service_outage_without_cli_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "glm"
            artifact_dir.mkdir(parents=True)
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=60,
                opencode_worker_poll_seconds=0.01,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(id="glm", swarm_size=4, answer_path=".swarm/answer.json")
            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions",
                side_effect=ConfigError("connection reset by peer"),
            ) as query, mock.patch(
                "swarmerz.orchestration.runner.execute"
            ) as launch:
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir
                ))
            self.assertEqual(result["reason"], "status_unavailable")
            self.assertEqual(query.call_count, 3)
            launch.assert_not_called()
            self.assertIn("connection reset by peer", result["status_errors"][0])

    def test_worker_monitor_backs_off_through_temporary_status_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "glm"
            artifact_dir.mkdir(parents=True)
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=60,
                opencode_worker_poll_seconds=0.01,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(
                id="glm", swarm_size=1, answer_path=".swarm/answer.json",
                worker_timeout_seconds=None,
            )
            terminal = {"data": [{
                "id": "ses_worker", "parentID": "ses_parent", "outcome": "succeeded",
            }]}
            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions",
                side_effect=[*(ConfigError("timed out") for _ in range(4)), terminal],
            ) as query:
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir
                ))
            self.assertEqual(result["reason"], "all_workers_terminal")
            self.assertEqual(query.call_count, 5)
            self.assertEqual(len(result["status_errors"]), 3)

    def test_worker_monitor_stops_when_permission_endpoint_keeps_resetting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "mimo"
            artifact_dir.mkdir(parents=True)
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=60,
                opencode_worker_poll_seconds=0.01,
            )
            progress: list[str] = []
            runner = AuditRunner(
                config, "ordinary task", session_id="test", progress=progress.append
            )
            engine = SimpleNamespace(id="mimo", swarm_size=1, answer_path=".swarm/answer.json")
            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions",
                return_value={"data": [{"id": "ses_worker", "parentID": "ses_parent"}]},
            ) as query, mock.patch(
                "swarmerz.orchestration.runner.pending_worker_permissions",
                side_effect=ConfigError("connection reset by peer"),
            ) as permissions:
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir
                ))
            self.assertEqual(result["reason"], "status_unavailable")
            self.assertEqual(query.call_count, 3)
            self.assertEqual(permissions.call_count, 3)
            self.assertIn("connection reset by peer", result["permission_status_errors"][0])
            self.assertEqual(progress, [
                "[mimo] OpenCode permission monitor unavailable after three attempts; "
                "no permission request was observed; preserving work without a paid retry"
            ])

    def test_partial_answer_must_postdate_completed_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            answer_path = Path(directory) / "answer.json"
            answer_path.write_text(json.dumps({
                "answer": "Report", "key_points": [], "assumptions": [],
                "uncertainties": [], "sources": [], "confidence": 0.7,
                "workstreams": [
                    {"name": f"w{i}", "scope": "review", "conclusion": "done"}
                    for i in range(4)
                ],
            }), encoding="utf-8")
            answer_mtime_ms = answer_path.stat().st_mtime_ns // 1_000_000
            snapshot = {
                "expected_workers": 4, "observed_workers": 4, "active_workers": 1,
                "outcomes": {"succeeded": 3}, "latest_terminal_ms": answer_mtime_ms + 1,
            }
            self.assertFalse(_settled_partial_answer(
                answer_path, snapshot, minimum_workstreams=4
            ))
            snapshot["latest_terminal_ms"] = answer_mtime_ms - 1
            self.assertTrue(_settled_partial_answer(
                answer_path, snapshot, minimum_workstreams=4
            ))
            answer_path.write_text("not JSON", encoding="utf-8")
            self.assertFalse(_settled_partial_answer(
                answer_path, snapshot, minimum_workstreams=4
            ))

    def test_adopts_only_late_valid_hunter_answer_without_rerunning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "sessions" / "review" / "rounds" / "round1"
            artifact_dir = run_dir / "hunters" / "glm"
            source = run_dir / "workspaces" / "hunters" / "glm" / ".swarm" / "answer.json"
            artifact_dir.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            result = {"hunter_id": "glm", "status": "failed", "answer_sealed": False,
                      "worker_contract_verified": True, "artifact_error": "permission pending"}
            result_path = artifact_dir / "result.json"
            result_path.write_text(json.dumps(result), encoding="utf-8")
            source.write_text(json.dumps({
                "answer": "Complete answer", "key_points": [], "assumptions": [],
                "uncertainties": [], "sources": [], "confidence": 0.7,
                "workstreams": [{"name": "one", "scope": "first", "conclusion": "done"}],
            }), encoding="utf-8")
            old = time.time() - 60
            os.utime(result_path, (old - 60, old - 60))
            os.utime(source, (old, old))
            config = SimpleNamespace(output_root=root, context=SimpleNamespace(path=None))
            runner = AuditRunner(config, "ordinary task", session_id="review")
            runner.run_dir = run_dir
            runner.manifest = {"hunter_results": [result], "partial_hunters": []}
            hunters = [{"id": "glm", "workers": 1}]
            runner._adopt_late_hunter_answers(hunters, {"glm": result})
            self.assertTrue((artifact_dir / "seal.json").is_file())
            self.assertTrue(result["answer_sealed"])
            self.assertEqual(runner.manifest["partial_hunters"], ["glm"])
            self.assertEqual(runner.manifest["sealed_answer_count"], 1)

    def test_worker_monitor_rejects_sensitive_read_then_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "mimo"
            artifact_dir.mkdir(parents=True)
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=60,
                opencode_worker_poll_seconds=0.01,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(id="mimo", swarm_size=1, answer_path=".swarm/answer.json")

            calls = 0

            def fake_sessions(*_args: object, **_kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                worker = {"id": "ses_worker", "parentID": "ses_parent", "title": "worker"}
                if calls == 2:
                    worker["outcome"] = "succeeded"
                return {"data": [worker]}

            pending = [{
                "id": "per_1", "session_id": "ses_worker", "action": "read",
                "resources": ["context/service/configs/.env.dev"],
            }]
            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions", side_effect=fake_sessions
            ), mock.patch(
                "swarmerz.orchestration.runner.pending_worker_permissions", return_value=pending
            ), mock.patch(
                "swarmerz.orchestration.runner.reject_worker_permission"
            ) as reject:
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir
                ))
            reject.assert_called_once_with("ses_worker", "per_1", endpoint=None)
            self.assertEqual(result["reason"], "all_workers_terminal")
            self.assertEqual(result["query_count"], 2)
            self.assertEqual(result["rejected_permissions"], pending)

    def test_worker_monitor_rejects_each_request_from_the_same_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "deepseek"
            artifact_dir.mkdir(parents=True)
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=60,
                opencode_worker_poll_seconds=0.01,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(id="deepseek", swarm_size=1, answer_path=".swarm/answer.json")
            calls = 0

            def fake_sessions(*_args: object, **_kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                worker = {"id": "ses_worker", "parentID": "ses_parent"}
                if calls == 2:
                    worker["outcome"] = "succeeded"
                return {"data": [worker]}

            pending = [
                {"id": "per_1", "session_id": "ses_worker", "action": "read",
                 "resources": ["context/backend.env"]},
                {"id": "per_2", "session_id": "ses_worker", "action": "read",
                 "resources": ["context/frontend.env"]},
            ]
            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions", side_effect=fake_sessions
            ), mock.patch(
                "swarmerz.orchestration.runner.pending_worker_permissions", return_value=pending
            ), mock.patch(
                "swarmerz.orchestration.runner.reject_worker_permission"
            ) as reject:
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir
                ))
            self.assertEqual(reject.call_count, 2)
            self.assertEqual(
                [call.args[:2] for call in reject.call_args_list],
                [("ses_worker", "per_1"), ("ses_worker", "per_2")],
            )
            self.assertEqual(result["reason"], "all_workers_terminal")
            self.assertEqual(result["rejected_permissions"], pending)

    def test_worker_monitor_rejects_typo_path_then_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "glm"
            artifact_dir.mkdir(parents=True)
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=60,
                opencode_worker_poll_seconds=0.01,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(id="glm", swarm_size=1, answer_path=".swarm/answer.json")
            calls = 0

            def fake_sessions(*_args: object, **_kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                worker = {"id": "ses_worker", "parentID": "ses_parent", "title": "worker"}
                if calls == 2:
                    worker["outcome"] = "succeeded"
                return {"data": [worker]}

            pending = [{
                "id": "per_1", "session_id": "ses_worker", "action": "external_directory",
                "resources": ["/wrong/swarm-runits/*"],
            }]
            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions", side_effect=fake_sessions
            ), mock.patch(
                "swarmerz.orchestration.runner.pending_worker_permissions", return_value=pending
            ), mock.patch(
                "swarmerz.orchestration.runner.reject_worker_permission"
            ) as reject:
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir
                ))
            reject.assert_called_once_with("ses_worker", "per_1", endpoint=None)
            self.assertEqual(result["reason"], "all_workers_terminal")
            self.assertEqual(result["rejected_permissions"], pending)

    def test_worker_monitor_does_not_retry_a_failed_permission_rejection_forever(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "deepseek"
            artifact_dir.mkdir(parents=True)
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=60,
                opencode_worker_poll_seconds=0.01,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(id="deepseek", swarm_size=1, answer_path=".swarm/answer.json")

            def fake_sessions(*_args: object, **_kwargs: object) -> dict[str, object]:
                return {"data": [{"id": "ses_worker", "parentID": "ses_parent"}]}

            pending = [{
                "id": "per_1", "session_id": "ses_worker", "action": "external_directory",
                "resources": ["/tmp/*"],
            }]
            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions", side_effect=fake_sessions
            ), mock.patch(
                "swarmerz.orchestration.runner.pending_worker_permissions", return_value=pending
            ), mock.patch(
                "swarmerz.orchestration.runner.reject_worker_permission",
                side_effect=ConfigError("HTTP 400: missing decision"),
            ):
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir
                ))
            self.assertEqual(result["reason"], "permission_required")
            self.assertEqual(result["query_count"], 1)
            self.assertEqual(result["pending_permissions"], pending)

    def test_worker_monitor_seals_quiet_partial_answer_and_interrupts_straggler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "mimo"
            answer_path = root / "round" / "workspaces" / "hunters" / "mimo" / ".swarm" / "answer.json"
            artifact_dir.mkdir(parents=True)
            answer_path.parent.mkdir(parents=True)
            answer_path.write_text(json.dumps({
                "answer": "Combined report from three completed workers",
                "key_points": [], "assumptions": [], "uncertainties": [], "sources": [],
                "confidence": 0.7,
                "workstreams": [
                    {"name": f"worker {i}", "scope": "review", "conclusion": "done"}
                    for i in range(4)
                ],
            }), encoding="utf-8")
            now_ms = int(time.time() * 1_000)
            children = [
                {
                    "id": f"ses_worker_{i}", "parentID": "ses_parent",
                    "outcome": "succeeded" if i < 3 else None,
                    "tokens": {"output": 100},
                    "time": {"created": now_ms - 2_000, "idle": now_ms - 1_000 if i < 3 else 0},
                }
                for i in range(4)
            ]
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=60,
                opencode_worker_poll_seconds=0.01,
                opencode_answer_quiet_seconds=0.03,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(id="mimo", swarm_size=4, answer_path=".swarm/answer.json")

            def fake_sessions(*_args: object, **_kwargs: object) -> dict[str, object]:
                return {"data": children}

            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions", side_effect=fake_sessions
            ), mock.patch(
                "swarmerz.orchestration.runner.pending_worker_permissions", return_value=[]
            ), mock.patch(
                "swarmerz.orchestration.runner.interrupt_worker_session"
            ) as interrupt:
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir
                ))
            self.assertEqual(result["reason"], "partial_answer_ready")
            self.assertEqual(result["snapshot"]["terminal_workers"], 3)
            self.assertEqual(result["interrupted_sessions"], ["ses_worker_3"])
            interrupt.assert_called_once_with("ses_worker_3", endpoint=None)

    def test_worker_timeout_interrupts_all_unfinished_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "qwen"
            artifact_dir.mkdir(parents=True)
            created_ms = int(time.time() * 1_000) - 10_000
            children = [
                {
                    "id": f"ses_worker_{index}", "parentID": "ses_parent",
                    "title": f"worker {index}", "time": {"created": created_ms},
                    **({"outcome": "succeeded"} if index == 0 else {}),
                }
                for index in range(4)
            ]
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=1,
                opencode_worker_poll_seconds=0.01,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(id="qwen", swarm_size=4, answer_path=".swarm/answer.json")
            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions", return_value={"data": children}
            ), mock.patch(
                "swarmerz.orchestration.runner.pending_worker_permissions", return_value=[]
            ), mock.patch("swarmerz.orchestration.runner.interrupt_worker_session") as interrupt:
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir,
                ))
            self.assertEqual(result["reason"], "worker_timeout")
            self.assertEqual(result["interrupted_sessions"], [
                "ses_worker_1", "ses_worker_2", "ses_worker_3",
            ])
            self.assertEqual(interrupt.call_count, 3)

    def test_worker_monitor_recovers_then_interrupts_orphaned_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "round" / "hunters" / "mimo"
            artifact_dir.mkdir(parents=True)
            child = {
                "id": "ses_worker", "parentID": "ses_parent", "title": "worker",
                "tokens": {"output": 100},
                "time": {"created": int(time.time() * 1_000) - 10_000, "updated": 1},
            }
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_worker_timeout_seconds=60,
                opencode_worker_poll_seconds=0.01,
                opencode_orphan_quiet_seconds=0.02,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(
                id="mimo", swarm_size=1, answer_path=".swarm/answer.json",
                worker_timeout_seconds=None,
            )
            with mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions",
                return_value={"data": [child]},
            ), mock.patch(
                "swarmerz.orchestration.runner.pending_worker_permissions", return_value=[]
            ), mock.patch(
                "swarmerz.orchestration.runner.active_session_ids", return_value=set()
            ) as active, mock.patch(
                "swarmerz.orchestration.runner.steer_session"
            ) as resume, mock.patch(
                "swarmerz.orchestration.runner.interrupt_worker_session"
            ) as interrupt:
                result = asyncio.run(runner._monitor_opencode_workers(
                    engine, parent_session_id="ses_parent", artifact_dir=artifact_dir
                ))
            self.assertEqual(result["reason"], "orphaned_workers")
            self.assertEqual(result["recovered_sessions"], ["ses_worker"])
            self.assertEqual(result["orphaned_sessions"], ["ses_worker"])
            active.assert_called()
            resume.assert_called_once()
            interrupt.assert_called_once_with("ses_worker", endpoint=None)

    def test_finalization_stops_after_fresh_valid_answer_settles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "hunters" / "qwen"
            workspace = root / "workspaces" / "qwen"
            artifact_dir.mkdir(parents=True)
            answer_path = workspace / ".swarm" / "answer.json"
            answer_path.parent.mkdir(parents=True)
            script = (
                "import json, pathlib, time; "
                f"p=pathlib.Path({str(answer_path)!r}); "
                "p.write_text(json.dumps({'answer':'final','key_points':[],"
                "'assumptions':[],'uncertainties':[],'sources':[],'confidence':0.7,"
                "'workstreams':["
                "{'name':'one','scope':'a','conclusion':'done'},"
                "{'name':'two','scope':'b','conclusion':'done'}]})); "
                "time.sleep(30)"
            )
            spec = CommandSpec(
                argv=(sys.executable, "-c", script), cwd=workspace,
                env=dict(os.environ), artifact_path=answer_path,
            )
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_finalize_timeout_seconds=10,
                opencode_finalize_answer_quiet_seconds=0.1,
            )
            runner = AuditRunner(config, "ordinary task", session_id="test")
            engine = SimpleNamespace(id="qwen", harness="opencode", swarm_size=2)
            started = time.monotonic()
            with mock.patch("swarmerz.orchestration.runner.interrupt_worker_session") as interrupt:
                result = asyncio.run(runner._execute_opencode_finalize(
                    engine, spec, artifact_dir=artifact_dir,
                    session_id="ses_parent", endpoint=None,
                ))
            self.assertLess(time.monotonic() - started, 6)
            self.assertTrue(result["artifact_ready_stop"])
            self.assertEqual(result["status"], "stopped")
            interrupt.assert_called_once_with("ses_parent", endpoint=None)
            self.assertEqual(json.loads(answer_path.read_text())["answer"], "final")

    def test_finalization_retries_malformed_provider_stream_on_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "hunters" / "qwen"
            workspace = root / "workspaces" / "qwen"
            artifact_dir.mkdir(parents=True)
            answer_path = workspace / ".swarm" / "answer.json"
            spec = CommandSpec(
                argv=("opencode", "run", "--session", "ses_parent", "finalize"),
                cwd=workspace,
                env={},
                artifact_path=answer_path,
            )
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_finalize_timeout_seconds=10,
                opencode_finalize_answer_quiet_seconds=0.1,
            )
            progress: list[str] = []
            runner = AuditRunner(
                config, "ordinary task", session_id="test", progress=progress.append
            )
            engine = SimpleNamespace(id="qwen", harness="opencode", swarm_size=4)
            calls = 0

            async def fake_execute(
                _spec: CommandSpec, **kwargs: object
            ) -> dict[str, object]:
                nonlocal calls
                calls += 1
                stdout_path = kwargs["stdout_path"]
                stderr_path = kwargs["stderr_path"]
                assert isinstance(stdout_path, Path)
                assert isinstance(stderr_path, Path)
                stdout_path.parent.mkdir(parents=True, exist_ok=True)
                mode = "a" if kwargs.get("append") else "w"
                with stdout_path.open(mode, encoding="utf-8") as handle:
                    if calls == 1:
                        handle.write(json.dumps({
                            "type": "error", "sessionID": "ses_parent",
                            "error": {
                                "type": "provider.invalid-output",
                                "message": "OpenAI Chat tool call delta is missing id or name",
                                "status": 200,
                            },
                        }) + "\n")
                    else:
                        handle.write(json.dumps({
                            "type": "text", "sessionID": "ses_parent",
                            "part": {"text": "consolidated"},
                        }) + "\n")
                stderr_path.touch()
                return {
                    "status": "failed" if calls == 1 else "completed",
                    "exit_code": 1 if calls == 1 else 0,
                    "timed_out": False,
                    "duration_seconds": 0.1,
                }

            with mock.patch(
                "swarmerz.orchestration.runner.execute", side_effect=fake_execute
            ) as execute_mock:
                result = asyncio.run(runner._execute_opencode_finalize(
                    engine,
                    spec,
                    artifact_dir=artifact_dir,
                    session_id="ses_parent",
                    endpoint=None,
                ))

            self.assertEqual(execute_mock.await_count, 2)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["provider_retry_count"], 1)
            self.assertEqual(len(result["provider_attempts"]), 2)
            self.assertTrue(any(
                "malformed provider stream during consolidation" in item
                for item in progress
            ))

    def test_finalization_retries_temporary_server_transport_on_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "hunters" / "deepseek"
            workspace = root / "workspaces" / "deepseek"
            artifact_dir.mkdir(parents=True)
            answer_path = workspace / ".swarm" / "answer.json"
            spec = CommandSpec(
                argv=("opencode", "run", "--session", "ses_parent", "finalize"),
                cwd=workspace,
                env={},
                artifact_path=answer_path,
            )
            config = SimpleNamespace(
                output_root=root,
                context=SimpleNamespace(path=None),
                opencode_finalize_timeout_seconds=10,
                opencode_finalize_answer_quiet_seconds=0.1,
            )
            progress: list[str] = []
            runner = AuditRunner(
                config, "ordinary task", session_id="test", progress=progress.append
            )
            engine = SimpleNamespace(id="deepseek", harness="opencode", swarm_size=4)
            calls = 0

            async def fake_execute(
                _spec: CommandSpec, **kwargs: object
            ) -> dict[str, object]:
                nonlocal calls
                calls += 1
                stdout_path = kwargs["stdout_path"]
                stderr_path = kwargs["stderr_path"]
                assert isinstance(stdout_path, Path)
                assert isinstance(stderr_path, Path)
                stdout_path.parent.mkdir(parents=True, exist_ok=True)
                mode = "a" if kwargs.get("append") else "w"
                with stdout_path.open(mode, encoding="utf-8"):
                    pass
                with stderr_path.open(mode, encoding="utf-8") as handle:
                    if calls == 1:
                        handle.write(
                            "Error: Could not reach server at http://127.0.0.1:39855\n"
                            "  [cause]: ClientError: Transport\n"
                        )
                return {
                    "status": "failed" if calls == 1 else "completed",
                    "exit_code": 1 if calls == 1 else 0,
                    "timed_out": False,
                    "duration_seconds": 0.1,
                }

            with mock.patch(
                "swarmerz.orchestration.runner.execute", side_effect=fake_execute
            ) as execute_mock, mock.patch.object(
                runner, "_wait_for_opencode_server", new=mock.AsyncMock(return_value=True)
            ) as wait_ready:
                result = asyncio.run(runner._execute_opencode_finalize(
                    engine,
                    spec,
                    artifact_dir=artifact_dir,
                    session_id="ses_parent",
                    endpoint=OpenCodeEndpoint(
                        "http://127.0.0.1:39855", "private", workspace
                    ),
                ))

            self.assertEqual(execute_mock.await_count, 2)
            wait_ready.assert_awaited_once()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["provider_retry_count"], 1)
            self.assertTrue(any(
                "temporary OpenCode server transport failure" in item
                for item in progress
            ))

    def test_finalization_does_not_accept_invalid_answer_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            answer_path = Path(directory) / "answer.json"
            answer_path.write_text('{"answer":"draft"}', encoding="utf-8")

            async def check() -> None:
                watcher = asyncio.create_task(_wait_for_stable_final_answer(
                    answer_path, minimum_workstreams=1, quiet_seconds=0.1,
                ))
                try:
                    await asyncio.sleep(0.2)
                    self.assertFalse(watcher.done())
                    answer_path.write_text(json.dumps({
                        "answer": "final",
                        "key_points": [],
                        "assumptions": [],
                        "uncertainties": [],
                        "sources": [],
                        "workstreams": [{"name": "one", "scope": "review", "conclusion": "done"}],
                        "confidence": 0.7,
                    }), encoding="utf-8")
                    await asyncio.wait_for(watcher, timeout=1)
                finally:
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher

            asyncio.run(check())

    def test_review_chat_starts_clean_thread_and_can_resume_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "sessions" / "review" / "rounds" / "first"
            (run_dir / "final").mkdir(parents=True)
            (run_dir / "final" / "report.md").write_text("# Readable report\n", encoding="utf-8")
            (run_dir / "final" / "analysis.json").write_text(
                json.dumps({"recommended_answer": "reviewed"}), encoding="utf-8"
            )
            (run_dir / "analysis").mkdir()
            (run_dir / "analysis" / "answer-map.json").write_text(
                json.dumps({"A": "hunter"}), encoding="utf-8"
            )
            (run_dir / "analysis" / "blind-answers.json").write_text(
                json.dumps({"answers": [{"answer_id": "A"}]}), encoding="utf-8"
            )
            context_file = root / "sessions" / "review" / "input" / "context" / "source.c"
            context_file.parent.mkdir(parents=True)
            context_file.write_text("int main(void) {}\n", encoding="utf-8")
            (run_dir / "manifest.json").write_text(
                json.dumps({"session_id": "review", "round_id": "first", "status": "completed"}),
                encoding="utf-8",
            )
            config = SimpleNamespace(
                output_root=root,
                analyzer=SimpleNamespace(
                    harness="codex-exec", command=(), harness_version="0.154.0",
                    effective_model="gpt-test", reasoning_effort="high",
                ),
            )
            output = io.StringIO()
            with mock.patch(
                "swarmerz.review.shutil.which",
                side_effect=lambda name: "/usr/bin/npx" if name == "npx" else None,
            ), contextlib.redirect_stdout(output):
                self.assertEqual(chat(config, "review", "first", resume=False, print_command=True), 0)
            command = output.getvalue()
            self.assertIn("@openai/codex@0.154.0", command)
            self.assertIn("Do not print raw JSON", command)
            self.assertNotIn("--output-schema", command)
            self.assertNotIn(" resume ", command)
            private = run_dir / "workspaces" / "analyzer" / ".swarm"
            self.assertTrue((private / "final-report.md").is_file())
            self.assertTrue((private / "final-analysis.json").is_file())
            self.assertTrue((private / "answer-map.json").is_file())
            self.assertTrue((private / "answers.json").is_file())
            linked_context = run_dir / "workspaces" / "analyzer" / "context" / "source.c"
            self.assertEqual(
                (context_file.stat().st_dev, context_file.stat().st_ino),
                (linked_context.stat().st_dev, linked_context.stat().st_ino),
            )
            output = io.StringIO()
            with mock.patch(
                "swarmerz.review.shutil.which",
                side_effect=lambda name: "/usr/bin/npx" if name == "npx" else None,
            ), contextlib.redirect_stdout(output):
                self.assertEqual(chat(config, "review", "first", resume=True, print_command=True), 0)
            self.assertIn("resume --last", output.getvalue())
            self.assertNotIn("Do not print raw JSON", output.getvalue())

            output = io.StringIO()
            with mock.patch(
                "swarmerz.review.shutil.which", return_value="/opt/bin/codex"
            ), contextlib.redirect_stdout(output):
                self.assertEqual(
                    chat(config, "review", "first", resume=False, print_command=True), 0
                )
            self.assertTrue(output.getvalue().startswith("/opt/bin/codex "))
            self.assertNotIn("npx", output.getvalue())

            config.storage = StorageConfig(cache_root=root / "cache")
            completed = SimpleNamespace(returncode=0)
            with mock.patch(
                "swarmerz.review.shutil.which", return_value="/opt/bin/codex"
            ), mock.patch(
                "swarmerz.review.sys.stdin.isatty", return_value=True
            ), mock.patch(
                "swarmerz.review.sys.stdout.isatty", return_value=True
            ), mock.patch(
                "swarmerz.review.subprocess.run", return_value=completed
            ) as launch:
                self.assertEqual(
                    chat(config, "review", "first", resume=False, print_command=False), 0
                )
            launched_environment = launch.call_args.kwargs["env"]
            self.assertTrue(
                Path(launched_environment["TMPDIR"]).is_relative_to(root / "cache")
            )
            self.assertFalse(Path(launched_environment["TMPDIR"]).exists())

    def test_failed_analysis_retries_sealed_answers_without_rerunning_hunters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hunter_script = root / "hunter.py"
            hunter_script.write_text(
                "import json, pathlib, sys\n"
                "count = pathlib.Path(sys.argv[2])\n"
                "count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))\n"
                "output = pathlib.Path(sys.argv[1])\n"
                "output.write_text(json.dumps({"
                "'answer':'done','key_points':[],'assumptions':[],'uncertainties':[],"
                "'sources':[],'workstreams':[{'name':'a','scope':'a','conclusion':'done'},"
                "{'name':'b','scope':'b','conclusion':'done'}],'confidence':0.8}))\n"
                "evidence = output.parent / 'worker-evidence.txt'\n"
                "evidence.write_text('worker evidence')\n"
                "(output.parent / 'worker-ledger.json').write_text(json.dumps({'workers':["
                "{'worker_id':'a','scope':'a','status':'completed','evidence_refs':['.swarm/worker-evidence.txt']},"
                "{'worker_id':'b','scope':'b','status':'completed','evidence_refs':['.swarm/worker-evidence.txt']}] }))\n",
                encoding="utf-8",
            )
            analyzer_script = root / "analyzer.py"
            analyzer_script.write_text(
                "import json, pathlib, sys\n"
                "marker = pathlib.Path(sys.argv[3])\n"
                "if not marker.exists():\n"
                "    marker.write_text('failed once')\n"
                "    raise SystemExit(1)\n"
                "bundle = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
                "labels = [item['answer_id'] for item in bundle['answers']]\n"
                "pathlib.Path(sys.argv[2]).write_text(json.dumps({"
                "'summary':'recovered','agreement':[],'disagreements':[],"
                "'answer_assessments':[{'answer_id':x,'strengths':[],'weaknesses':[],"
                "'factual_risks':[]} for x in labels],'validated_findings':[],"
                "'unresolved_questions':[],'recommended_answer':'recovered review','confidence':0.8}))\n",
                encoding="utf-8",
            )
            hunters = "".join(
                f'[[hunters]]\nid = "{name}"\nharness = "command"\nharness_version = "1"\n'
                f'provider = "test"\nmodel = "fake"\nreasoning_effort = "low"\n'
                f'supported_efforts = ["low"]\nworkers = 2\n'
                f'command = ["{sys.executable}", "{hunter_script}", "{{answer}}", "{root / (name + ".count")}"]\n'
                for name in ("one", "two")
            )
            config_path = root / "swarm.toml"
            config_path.write_text(
                f'schema_version = 1\noutput_root = "{root / "runs"}"\n'
                f'minimum_answers = 2\n{hunters}\n[analyzer]\n'
                f'id = "codex-analyzer"\nharness = "codex-exec"\nharness_version = "test"\n'
                f'provider = "openai"\nmodel = "fake"\nreasoning_effort = "low"\n'
                f'supported_efforts = ["low"]\n'
                f'command = ["{sys.executable}", "{analyzer_script}", "{{answers}}", "{{result}}", "{root / "failed.marker"}"]\n',
                encoding="utf-8",
            )
            run_dir = asyncio.run(AuditRunner(
                load_config(config_path), "review this", session_id="retry", round_id="first"
            ).run())
            incomplete_manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(incomplete_manifest["status"], "incomplete")
            self.assertEqual(
                incomplete_manifest["storage"]["workspace_cleanup"],
                "retained_for_recovery",
            )
            self.assertTrue((run_dir / "workspaces").is_dir())
            analyzed_dir = asyncio.run(AuditRunner(
                load_config(config_path), "", session_id="retry", round_id="first"
            ).analyze_existing())
            self.assertEqual(analyzed_dir, run_dir)
            analysis = json.loads((run_dir / "final" / "analysis.json").read_text())
            self.assertEqual(analysis["recommended_answer"], "recovered review")
            self.assertEqual(json.loads((run_dir / "manifest.json").read_text())["status"], "completed")
            self.assertEqual((root / "one.count").read_text(), "1")
            self.assertEqual((root / "two.count").read_text(), "1")
            self.assertEqual(len(list((run_dir / "analysis" / "attempts").iterdir())), 1)
            self.assertFalse((run_dir / "workspaces").exists())

    def test_native_opencode_monitors_workers_and_replaces_provisional_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_opencode = root / "opencode"
            fake_opencode.write_text(
                textwrap.dedent(
                    f"""\
                    #!{sys.executable}
                    import json, os, pathlib, sys, time

                    args = sys.argv[1:]
                    cwd = pathlib.Path.cwd()
                    hunter = cwd.name
                    parent = "parent-" + hunter
                    answer = cwd / ".swarm" / "answer.json"

                    def answer_body(label):
                        return {{
                            "answer": label + " " + hunter,
                            "key_points": [], "assumptions": [], "uncertainties": [], "sources": [],
                            "workstreams": [
                                {{"name": "worker-a", "scope": "one", "conclusion": "done"}},
                                {{"name": "worker-b", "scope": "two", "conclusion": "done"}}
                            ],
                            "confidence": 0.8
                        }}

                    if args == ["--version"]:
                        print("opencode v2.0.21")
                    elif args and args[0] == "run" and "--session" in args:
                        answer.parent.mkdir(parents=True, exist_ok=True)
                        answer.write_text(json.dumps(answer_body("finalized")))
                        print(json.dumps({{"type": "text", "sessionID": parent, "part": {{"text": "finalized"}}}}))
                    elif args and args[0] == "run":
                        answer.parent.mkdir(parents=True, exist_ok=True)
                        answer.write_text(json.dumps(answer_body("provisional")))
                        os.utime(answer, (time.time() - 10, time.time() - 10))
                        print(json.dumps({{"type": "step_start", "sessionID": parent}}), flush=True)
                        # The parent deliberately cannot return until Swarmerz's
                        # worker monitor starts. This guards against reintroducing
                        # the old parent-before-monitor deadlock.
                        marker = cwd / ".monitor-started"
                        deadline = time.time() + 8
                        while not marker.exists() and time.time() < deadline:
                            time.sleep(0.01)
                        if not marker.exists():
                            raise SystemExit("worker monitor never started")
                    else:
                        raise SystemExit("unexpected fake OpenCode arguments: " + repr(args))
                    """
                ),
                encoding="utf-8",
            )
            fake_opencode.chmod(0o755)

            analyzer_script = root / "analyzer.py"
            analyzer_script.write_text(
                textwrap.dedent(
                    """
                    import json, pathlib, sys
                    bundle = json.loads(pathlib.Path(sys.argv[1]).read_text())
                    labels = [item["answer_id"] for item in bundle["answers"]]
                    pathlib.Path(sys.argv[2]).write_text(json.dumps({
                        "summary": "worker-aware", "agreement": [], "disagreements": [],
                        "answer_assessments": [
                            {"answer_id": label, "strengths": [], "weaknesses": [], "factual_risks": []}
                            for label in labels
                        ],
                        "validated_findings": [], "unresolved_questions": [],
                        "recommended_answer": "worker-aware synthesis", "confidence": 0.8
                    }))
                    """
                ),
                encoding="utf-8",
            )
            hunters = "".join(
                textwrap.dedent(
                    f"""
                    [[hunters]]
                    id = "{hunter_id}"
                    harness = "opencode"
                    harness_version = "2.0.21"
                    provider = "fake"
                    model = "fake-model"
                    reasoning_effort = "default"
                    supported_efforts = ["default"]
                    timeout_seconds = 10
                    workers = 2
                    subagents = 2
                    pass_env = ["FAKE_OPENCODE_KEY"]
                    """
                )
                for hunter_id in ("alpha", "beta")
            )
            config_path = root / "swarm.toml"
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    schema_version = 1
                    output_root = "{root / 'runs'}"
                    max_parallel_hunters = 2
                    minimum_answers = 2
                    opencode_worker_timeout_seconds = 10
                    opencode_worker_poll_seconds = 1
                    opencode_artifact_grace_seconds = 1
                    opencode_finalize_timeout_seconds = 5

                    {hunters}

                    [analyzer]
                    id = "codex-analyzer"
                    harness = "codex-exec"
                    harness_version = "test"
                    provider = "openai"
                    model = "codex-test"
                    reasoning_effort = "high"
                    supported_efforts = ["high"]
                    command = ["{sys.executable}", "{analyzer_script}", "{{answers}}", "{{result}}"]
                    """
                ),
                encoding="utf-8",
            )

            progress: list[str] = []
            test_env = {
                "PATH": f"{root}:{os.environ.get('PATH', '')}",
                "FAKE_OPENCODE_KEY": "test-only",
            }
            poll_count: dict[str, int] = {}

            def fake_sessions(
                parent_session_id: str, *, limit: int, endpoint: OpenCodeEndpoint | None = None
            ) -> dict[str, object]:
                self.assertGreaterEqual(limit, 2)
                self.assertIsNotNone(endpoint)
                assert endpoint is not None
                self.assertEqual(endpoint.directory.name, parent_session_id.removeprefix("parent-"))
                (endpoint.directory / ".monitor-started").write_text("started")
                count = poll_count.get(parent_session_id, 0) + 1
                poll_count[parent_session_id] = count
                now = int(time.time() * 1000)
                children = [
                    {
                        "id": parent_session_id + "-a", "parentID": parent_session_id,
                        "title": "worker A", "outcome": "succeeded",
                        "time": {"created": now - 2000, "updated": now - 1500, "idle": now - 1000},
                    },
                    {
                        "id": parent_session_id + "-b", "parentID": parent_session_id,
                        "title": "worker B", "time": {"created": now - 1900, "updated": now - 1200},
                    },
                ]
                if count >= 2:
                    children[1]["outcome"] = "succeeded"
                    children[1]["time"]["idle"] = now
                return {"data": children}

            class FakeServer:
                endpoint = OpenCodeEndpoint("http://127.0.0.1:40001", "test-password")
                stderr_path = root / "fake-server.stderr.log"

                async def wait_ready(self, endpoint: OpenCodeEndpoint) -> None:
                    assert endpoint.directory is not None

                async def stop(self) -> None:
                    return None

            async def fake_start(*_args: object, **_kwargs: object) -> FakeServer:
                return FakeServer()

            @contextlib.asynccontextmanager
            async def no_op_round_slot(_emit: object, _lock_root: object = None):
                yield

            with mock.patch.dict(os.environ, test_env), mock.patch(
                "swarmerz.orchestration.runner.pending_worker_permissions", return_value=[]
            ), mock.patch(
                "swarmerz.orchestration.runner.list_worker_sessions", side_effect=fake_sessions
            ), mock.patch(
                "swarmerz.orchestration.runner.IsolatedOpenCodeServer.start", side_effect=fake_start
            ) as server_start, mock.patch(
                "swarmerz.orchestration.runner._opencode_round_slot", no_op_round_slot
            ):
                run_dir = asyncio.run(
                    AuditRunner(
                        load_config(config_path),
                        "worker-aware lifecycle",
                        session_id="worker-aware",
                        round_id="first",
                        progress=progress.append,
                    ).run()
                )
            self.assertEqual(server_start.await_count, 1)

            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            for result in manifest["hunter_results"]:
                self.assertTrue(result["answer_sealed"])
                self.assertTrue(result["worker_contract_verified"])
                self.assertEqual(result["worker_monitor"]["reason"], "all_workers_terminal")
                self.assertEqual(result["worker_monitor"]["snapshot"]["terminal_workers"], 2)
                self.assertEqual(result["retry_count"], 0)
                self.assertTrue(result["isolated_server"]["shared_by_native_hunters"])
                hunter_dir = run_dir / "hunters" / result["hunter_id"]
                self.assertTrue((hunter_dir / "provisional-answer.json").is_file())
                answer = json.loads((hunter_dir / "answer.json").read_text())
                self.assertTrue(answer["answer"].startswith("finalized "))
            self.assertTrue(any("workers: 1/2 terminal" in item for item in progress))
            self.assertTrue(any(
                "resuming OpenCode session to consolidate" in item for item in progress
            ))
            alpha_opened = next(
                index for index, item in enumerate(progress)
                if "[alpha] parent session opened" in item
            )
            beta_started = next(
                index for index, item in enumerate(progress)
                if "[beta] started:" in item
            )
            self.assertLess(alpha_opened, beta_started)
            self.assertTrue(any(
                "OpenCode startup queue (hunts run concurrently once opened): alpha -> beta"
                in item for item in progress
            ))

    def test_opencode_waits_for_background_artifact_and_recovers_inactivity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            answer_body = {
                "answer": "finished",
                "key_points": [],
                "assumptions": [],
                "uncertainties": [],
                "sources": [],
                "workstreams": [
                    {"name": "a", "scope": "one", "conclusion": "done"},
                    {"name": "b", "scope": "two", "conclusion": "done"},
                ],
                "confidence": 0.8,
            }
            writer_script = root / "writer.py"
            writer_script.write_text(
                textwrap.dedent(
                    f"""
                    import json, pathlib, sys, time
                    time.sleep(float(sys.argv[2]))
                    output = pathlib.Path(sys.argv[1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({answer_body!r}))
                    evidence = output.parent / "worker-evidence.txt"
                    evidence.write_text("worker evidence")
                    (output.parent / "worker-ledger.json").write_text(json.dumps({{
                        "workers": [
                            {{"worker_id": "one", "scope": "one", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]}},
                            {{"worker_id": "two", "scope": "two", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]}}
                        ]
                    }}))
                    """
                ),
                encoding="utf-8",
            )
            late_script = root / "late.py"
            late_script.write_text(
                textwrap.dedent(
                    f"""
                    import subprocess, sys
                    subprocess.Popen(
                        [sys.executable, {str(writer_script)!r}, sys.argv[1], "0.2"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    print('{{"type":"step_start","sessionID":"ses_late"}}', flush=True)
                    """
                ),
                encoding="utf-8",
            )
            recovery_script = root / "recovery.py"
            recovery_script.write_text(
                textwrap.dedent(
                    f"""
                    import json, pathlib, sys
                    args = sys.argv[1:]
                    if "--session" not in args:
                        print('{{"type":"error","sessionID":"ses_recover",'
                              '"error":{{"message":"Session interrupted: inactivity"}}}}', flush=True)
                        raise SystemExit(1)
                    output = pathlib.Path(next(item for item in args if item.endswith("answer.json")))
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({answer_body!r}))
                    evidence = output.parent / "worker-evidence.txt"
                    evidence.write_text("worker evidence")
                    (output.parent / "worker-ledger.json").write_text(json.dumps({{
                        "workers": [
                            {{"worker_id": "one", "scope": "one", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]}},
                            {{"worker_id": "two", "scope": "two", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]}}
                        ]
                    }}))
                    print('{{"type":"text","sessionID":"ses_recover",'
                          '"part":{{"text":"finalized"}}}}', flush=True)
                    """
                ),
                encoding="utf-8",
            )
            normal_script = root / "normal.py"
            normal_script.write_text(
                textwrap.dedent(
                    f"""
                    import json, pathlib, sys
                    output = pathlib.Path(sys.argv[1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({answer_body!r}))
                    evidence = output.parent / "worker-evidence.txt"
                    evidence.write_text("worker evidence")
                    (output.parent / "worker-ledger.json").write_text(json.dumps({{
                        "workers": [
                            {{"worker_id": "one", "scope": "one", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]}},
                            {{"worker_id": "two", "scope": "two", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]}}
                        ]
                    }}))
                    """
                ),
                encoding="utf-8",
            )
            analyzer_script = root / "analyzer.py"
            analyzer_script.write_text(
                textwrap.dedent(
                    """
                    import json, pathlib, sys
                    bundle = json.loads(pathlib.Path(sys.argv[1]).read_text())
                    labels = [item["answer_id"] for item in bundle["answers"]]
                    assert labels == ["A", "B", "C"]
                    pathlib.Path(sys.argv[2]).write_text(json.dumps({
                        "summary": "all recovered",
                        "agreement": [], "disagreements": [],
                        "answer_assessments": [
                            {"answer_id": label, "strengths": [], "weaknesses": [], "factual_risks": []}
                            for label in labels
                        ],
                        "validated_findings": [], "unresolved_questions": [],
                        "recommended_answer": "recovered synthesis", "confidence": 0.8
                    }))
                    """
                ),
                encoding="utf-8",
            )
            config_path = root / "swarm.toml"
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    schema_version = 1
                    output_root = "{root / 'runs'}"
                    max_parallel_hunters = 3
                    minimum_answers = 3
                    opencode_artifact_grace_seconds = 2
                    opencode_finalize_timeout_seconds = 2

                    [[hunters]]
                    id = "late"
                    harness = "opencode"
                    harness_version = "test"
                    provider = "test"
                    model = "late"
                    reasoning_effort = "default"
                    supported_efforts = ["default"]
                    workers = 2
                    command = ["{sys.executable}", "{late_script}", "{{answer}}", "{{prompt}}"]

                    [[hunters]]
                    id = "recover"
                    harness = "opencode"
                    harness_version = "test"
                    provider = "test"
                    model = "recover"
                    reasoning_effort = "default"
                    supported_efforts = ["default"]
                    workers = 2
                    command = ["{sys.executable}", "{recovery_script}", "run", "{{answer}}", "{{prompt}}"]

                    [[hunters]]
                    id = "normal"
                    harness = "command"
                    harness_version = "test"
                    provider = "test"
                    model = "normal"
                    reasoning_effort = "low"
                    supported_efforts = ["low"]
                    workers = 2
                    command = ["{sys.executable}", "{normal_script}", "{{answer}}"]

                    [analyzer]
                    id = "codex-analyzer"
                    harness = "codex-exec"
                    harness_version = "test"
                    provider = "openai"
                    model = "codex-test"
                    reasoning_effort = "high"
                    supported_efforts = ["high"]
                    command = ["{sys.executable}", "{analyzer_script}", "{{answers}}", "{{result}}"]
                    """
                ),
                encoding="utf-8",
            )

            run_dir = asyncio.run(
                AuditRunner(
                    load_config(config_path),
                    "test OpenCode lifecycle",
                    session_id="opencode-lifecycle",
                    round_id="first",
                ).run()
            )
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            results = {item["hunter_id"]: item for item in manifest["hunter_results"]}
            self.assertTrue(results["late"]["answer_sealed"])
            self.assertGreater(results["late"]["background_artifact_wait_seconds"], 0)
            self.assertTrue(results["recover"]["answer_sealed"])
            self.assertEqual(results["recover"]["initial_attempt"]["status"], "failed")
            self.assertEqual(results["recover"]["recovery"]["session_id"], "ses_recover")

    def test_hunter_quorum_allows_codex_analysis_after_one_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hunter_script = root / "hunter.py"
            hunter_script.write_text(
                textwrap.dedent(
                    """
                    import json, pathlib, sys
                    output = pathlib.Path(sys.argv[1])
                    hunter_id = sys.argv[2]
                    if hunter_id == "two":
                        raise SystemExit(7)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({
                        "answer": f"answer from {hunter_id}",
                        "key_points": [], "assumptions": [], "uncertainties": [], "sources": [],
                        "workstreams": [
                            {"name": "a", "scope": "one", "conclusion": "done"},
                            {"name": "b", "scope": "two", "conclusion": "done"}
                        ],
                        "confidence": 0.8
                    }))
                    evidence = output.parent / "worker-evidence.txt"
                    evidence.write_text("worker evidence")
                    (output.parent / "worker-ledger.json").write_text(json.dumps({
                        "workers": [
                            {"worker_id": "one", "scope": "one", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]},
                            {"worker_id": "two", "scope": "two", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]}
                        ]
                    }))
                    """
                ),
                encoding="utf-8",
            )
            analyzer_script = root / "analyzer.py"
            analyzer_script.write_text(
                textwrap.dedent(
                    """
                    import json, pathlib, sys
                    bundle = json.loads(pathlib.Path(sys.argv[1]).read_text())
                    assert [item["answer_id"] for item in bundle["answers"]] == ["A", "B"]
                    pathlib.Path(sys.argv[2]).write_text(json.dumps({
                        "summary": "reviewed quorum",
                        "agreement": [], "disagreements": [],
                        "answer_assessments": [
                            {"answer_id": label, "strengths": [], "weaknesses": [], "factual_risks": []}
                            for label in ["A", "B"]
                        ],
                        "validated_findings": [], "unresolved_questions": [],
                        "recommended_answer": "quorum synthesis", "confidence": 0.8
                    }))
                    """
                ),
                encoding="utf-8",
            )
            hunters = "".join(
                textwrap.dedent(
                    f"""
                    [[hunters]]
                    id = "{hunter_id}"
                    harness = "command"
                    harness_version = "1"
                    provider = "test"
                    model = "model-{hunter_id}"
                    reasoning_effort = "low"
                    supported_efforts = ["low"]
                    workers = 2
                    command = ["{sys.executable}", "{hunter_script}", "{{answer}}", "{hunter_id}"]
                    """
                )
                for hunter_id in ("one", "two", "three")
            )
            config_path = root / "swarm.toml"
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    schema_version = 1
                    output_root = "{root / 'runs'}"
                    max_parallel_hunters = 3
                    minimum_answers = 2

                    {hunters}

                    [analyzer]
                    id = "codex-analyzer"
                    harness = "codex-exec"
                    harness_version = "1"
                    provider = "openai"
                    model = "codex-test"
                    reasoning_effort = "high"
                    supported_efforts = ["high"]
                    command = ["{sys.executable}", "{analyzer_script}", "{{answers}}", "{{result}}"]
                    """
                ),
                encoding="utf-8",
            )

            run_dir = asyncio.run(
                AuditRunner(
                    load_config(config_path),
                    "test quorum",
                    session_id="quorum",
                    round_id="first",
                ).run()
            )
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "completed")
            self.assertTrue(manifest["degraded"])
            self.assertEqual(manifest["sealed_answer_count"], 2)
            self.assertEqual(manifest["missing_hunters"], ["two"])
            self.assertEqual(
                json.loads((run_dir / "final" / "analysis.json").read_text())["summary"],
                "reviewed quorum",
            )

    def test_two_round_session_preserves_private_lineages_and_frozen_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = root / "subject"
            context.mkdir()
            (context / "subject.txt").write_text("frozen-v1\n", encoding="utf-8")

            hunter_script = root / "fake_hunter.py"
            hunter_script.write_text(
                textwrap.dedent(
                    """
                    import json, pathlib, sys
                    output = pathlib.Path(sys.argv[1])
                    workspace = pathlib.Path(sys.argv[2])
                    hunter_id = sys.argv[3]
                    history = json.loads((workspace / ".swarm" / "history.json").read_text())
                    context_value = (workspace / "context" / "subject.txt").read_text().strip()
                    for prior in history["prior_rounds"]:
                        assert prior["your_answer"]["answer"].startswith(hunter_id + ":")
                    validated = sum(len(item["findings"]) for item in history["validated_findings"])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps({
                        "answer": f"{hunter_id}:history={len(history['prior_rounds'])}:validated={validated}:context={context_value}",
                        "key_points": ["independent"],
                        "assumptions": [],
                        "uncertainties": [],
                        "sources": ["context/subject.txt"],
                        "workstreams": [
                            {"name": "one", "scope": "first", "conclusion": "done"},
                            {"name": "two", "scope": "second", "conclusion": "done"}
                        ],
                        "confidence": 0.8
                    }))
                    evidence = output.parent / "worker-evidence.txt"
                    evidence.write_text("worker evidence")
                    durable = output.parent / "artifacts"
                    durable.mkdir()
                    (durable / "poc.txt").write_text("durable poc")
                    (output.parent / "worker-ledger.json").write_text(json.dumps({
                        "workers": [
                            {"worker_id": "one", "scope": "first", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]},
                            {"worker_id": "two", "scope": "second", "status": "completed", "evidence_refs": [".swarm/worker-evidence.txt"]}
                        ]
                    }))
                    """
                ),
                encoding="utf-8",
            )
            analyzer_script = root / "fake_codex_analyzer.py"
            analyzer_script.write_text(
                textwrap.dedent(
                    """
                    import json, pathlib, sys
                    answers_path = pathlib.Path(sys.argv[1])
                    result_path = pathlib.Path(sys.argv[2])
                    workspace = pathlib.Path(sys.argv[3])
                    bundle = json.loads(answers_path.read_text())
                    history = json.loads((workspace / ".swarm" / "history.json").read_text())
                    assert [item["answer_id"] for item in bundle["answers"]] == ["A", "B", "C", "D"]
                    assert all("provider" not in item["response"] for item in bundle["answers"])
                    result_path.parent.mkdir(parents=True, exist_ok=True)
                    result_path.write_text(json.dumps({
                        "summary": f"reviewed four; prior={len(history['prior_rounds'])}",
                        "agreement": ["all answered"],
                        "disagreements": [],
                        "answer_assessments": [
                            {"answer_id": label, "strengths": [], "weaknesses": [], "factual_risks": []}
                            for label in ["A", "B", "C", "D"]
                        ],
                        "validated_findings": [
                            {
                                "title": "Example issue",
                                "severity": "high",
                                "source_answer_ids": ["A", "C"],
                                "validation": "Confirmed by the adjudicator.",
                                "evidence": ["reproduction succeeds"],
                                "provenance_class": "independent_discovery",
                                "validation_method": "executed_reproduction",
                                "reproduction": {
                                    "commands": ["test-command"],
                                    "observed_result": "reproduction succeeds",
                                    "artifact_refs": []
                                }
                            }
                        ],
                        "unresolved_questions": [],
                        "recommended_answer": "Codex synthesis",
                        "confidence": 0.9
                    }))
                    """
                ),
                encoding="utf-8",
            )

            hunters = []
            for hunter_id in ("one", "two", "three", "four"):
                hunters.append(
                    textwrap.dedent(
                        f"""
                        [[hunters]]
                        id = "{hunter_id}"
                        harness = "command"
                        harness_version = "1"
                        provider = "test"
                        model = "{hunter_id}-model"
                        reasoning_effort = "low"
                        supported_efforts = ["low"]
                        workers = 2
                        subagents = 2
                        command = ["{sys.executable}", "{hunter_script}", "{{answer}}", "{{workspace}}", "{hunter_id}"]
                        """
                    )
                )
            config_path = root / "swarm.toml"
            config_path.write_text(
                textwrap.dedent(
                    f"""
                    schema_version = 1
                    output_root = "{root / 'runs'}"
                    max_parallel_hunters = 4

                    [context]
                    allow_symlinks = false

                    [isolation]
                    mode = "workspace"
                    inherit_environment = false

                    {''.join(hunters)}

                    [analyzer]
                    id = "codex-analyzer"
                    harness = "codex-exec"
                    harness_version = "1"
                    provider = "openai"
                    model = "codex-test"
                    reasoning_effort = "high"
                    supported_efforts = ["high"]
                    command = ["{sys.executable}", "{analyzer_script}", "{{answers}}", "{{result}}", "{{workspace}}"]
                    """
                ),
                encoding="utf-8",
            )

            config = load_config(config_path)
            progress: list[str] = []
            first = AuditRunner(
                config,
                "First pass on the supplied subject",
                context_override=context,
                session_id="same-subject",
                round_id="first",
                progress=progress.append,
            )
            first_dir = asyncio.run(first.run())
            self.assertEqual(json.loads((first_dir / "manifest.json").read_text())["status"], "completed")
            self.assertEqual(sum("freezing session context: copying" in line for line in progress), 1)

            (context / "subject.txt").write_text("changed-v2\n", encoding="utf-8")
            second = AuditRunner(
                config,
                "Second pass: look for anything missed",
                session_id="same-subject",
                round_id="second",
                progress=progress.append,
            )
            second_dir = asyncio.run(second.run())
            second_manifest = json.loads((second_dir / "manifest.json").read_text())
            self.assertEqual(second_manifest["status"], "completed")
            self.assertEqual(second_manifest["sealed_answer_count"], 4)
            self.assertEqual(sum("freezing session context: copying" in line for line in progress), 1)
            self.assertTrue(any("reusing frozen session context" in line for line in progress))

            answers = json.loads((second_dir / "final" / "answers.json").read_text())
            self.assertTrue(all(
                "history=1:validated=1:context=frozen-v1" in item["answer"]["answer"]
                for item in answers
            ))
            analysis = json.loads((second_dir / "final" / "analysis.json").read_text())
            self.assertEqual(analysis["summary"], "reviewed four; prior=1")
            self.assertEqual(analysis["validated_findings"][0]["title"], "[ONE, THREE]: Example issue")
            self.assertEqual(analysis["validated_findings"][0]["source_hunters"], ["ONE", "THREE"])
            self.assertIn("### [ONE, THREE]: Example issue", analysis["recommended_answer"])

            session = json.loads((root / "runs" / "sessions" / "same-subject" / "session.json").read_text())
            self.assertEqual([item["status"] for item in session["rounds"]], ["completed", "completed"])
            self.assertEqual(len(session["rounds"]), 2)
            snapshot_file = root / "runs" / "sessions" / "same-subject" / "input" / "context" / "subject.txt"
            self.assertEqual(snapshot_file.stat().st_mode & 0o222, 0)
            self.assertEqual(snapshot_file.read_text(), "frozen-v1\n")
            self.assertFalse((second_dir / "workspaces").exists())
            self.assertEqual(second_manifest["storage"]["workspace_cleanup"], "completed")
            for hunter_id in ("one", "two", "three", "four"):
                preserved = (
                    second_dir / "hunters" / hunter_id / "evidence"
                    / ".swarm" / "worker-evidence.txt"
                )
                self.assertEqual(preserved.read_text(), "worker evidence")
                poc = (
                    second_dir / "hunters" / hunter_id / "evidence"
                    / ".swarm" / "artifacts" / "poc.txt"
                )
                self.assertEqual(poc.read_text(), "durable poc")
                evidence_manifest = json.loads(
                    (second_dir / "hunters" / hunter_id / "evidence-manifest.json").read_text()
                )
                self.assertEqual(evidence_manifest["artifact_count"], 1)
            self.assertTrue(any("no duplicate file data" in message for message in progress))
            self.assertTrue(any("disposable workspace files" in message for message in progress))
            self.assertTrue(any("launching 4" in message for message in progress))
            self.assertTrue(any("primary analysis] completed" in message for message in progress))

            third_progress: list[str] = []
            third_reports: dict[str, str] = {}
            third_dir = asyncio.run(AuditRunner(
                load_config(config_path),
                "Third pass",
                session_id="same-subject",
                progress=third_progress.append,
                hunter_report=third_reports.__setitem__,
            ).run())
            third_analysis = json.loads((third_dir / "final" / "analysis.json").read_text())
            self.assertIn("Codex synthesis", third_analysis["recommended_answer"])
            self.assertTrue(any("launching 4 independent parent swarms" in item for item in third_progress))
            self.assertIn("one:history=2:validated=2:context=frozen-v1", third_reports["one"])
            self.assertEqual(set(third_reports), {"one", "two", "three", "four"})

    def test_validated_findings_reach_hunters_without_private_lineage_or_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                output_root=root / "runs",
                context=SimpleNamespace(path=None),
                hunters=(),
            )
            runner = AuditRunner(config, "Round 2", session_id="audit")
            runner.round_id = "second"
            runner.session = {
                "rounds": [
                    {"number": 1, "round_id": "first", "status": "completed"},
                    {"number": 2, "round_id": "second", "status": "running"},
                ]
            }
            prior = runner.session_dir / "rounds" / "first"
            (prior / "analysis").mkdir(parents=True)
            (prior / "instruction.txt").write_text("Audit the target\n", encoding="utf-8")
            (prior / "analysis" / "analysis.json").write_text(
                json.dumps({
                    "validated_findings": [
                        {
                            "title": "Broken authorization",
                            "severity": "high",
                            "source_answer_ids": ["A", "C"],
                            "source_hunters": ["secret-one", "secret-two"],
                            "validation": "The unauthorized path is reproducible.",
                            "evidence": ["request 7 returns another tenant's record"],
                            "provider": "secret-provider",
                        }
                    ]
                }),
                encoding="utf-8",
            )

            history = runner._hunter_history("new-hunter")

            self.assertEqual(history["prior_rounds"], [])
            self.assertEqual(history["validated_findings"], [
                {
                    "round_number": 1,
                    "round_id": "first",
                    "instruction": "Audit the target",
                    "findings": [
                        {
                            "title": "Broken authorization",
                            "severity": "high",
                            "validation": "The unauthorized path is reproducible.",
                            "evidence": ["request 7 returns another tenant's record"],
                            "provenance_class": "prior_artifact_corroboration",
                            "validation_method": "reasoning_only",
                            "reproduction": {
                                "commands": [],
                                "observed_result": "Historical adjudication did not record a reproduction contract.",
                                "artifact_refs": ["request 7 returns another tenant's record"],
                            },
                            "supporting_hunter_count": 2,
                        }
                    ],
                }
            ])
            serialized = json.dumps(history)
            self.assertNotIn("source_answer_ids", serialized)
            self.assertNotIn("source_hunters", serialized)
            self.assertNotIn("secret-provider", serialized)


if __name__ == "__main__":
    unittest.main()
