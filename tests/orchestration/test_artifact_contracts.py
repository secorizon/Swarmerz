# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarmerz.orchestration.adjudication import (
    _analysis_contract_error,
    _promote_analysis_source_references,
)
from swarmerz.orchestration.artifacts import (
    evidence_contract_error,
    promote_source_inspection_references,
    randomized_answer_order,
    read_answer,
    verified_reproduction_references,
)
from swarmerz.orchestration.fanout import build_fanout_manifest, fanout_summary
from swarmerz.orchestration.models import ConfigError
from swarmerz.orchestration.schema_validation import schema_error
from swarmerz.orchestration.session_lock import SessionLock
from swarmerz.orchestration.session_context import copy_independent_file


def _answer() -> dict[str, object]:
    return {
        "answer": "reviewed",
        "key_points": [],
        "assumptions": [],
        "uncertainties": [],
        "sources": [],
        "workstreams": [
            {"name": "source", "scope": "parser", "conclusion": "complete"},
            {"name": "repro", "scope": "runtime", "conclusion": "complete"},
        ],
        "confidence": 0.8,
    }


def test_answer_reader_enforces_canonical_schema(tmp_path: Path) -> None:
    path = tmp_path / "answer.json"
    path.write_text(json.dumps(_answer()), encoding="utf-8")
    assert read_answer(path, minimum_workstreams=2)["answer"] == "reviewed"

    missing = _answer()
    del missing["confidence"]
    path.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"answer\.schema\.json.*confidence"):
        read_answer(path, minimum_workstreams=2)

    extra = _answer()
    extra["provider"] = "should-not-enter-contract"
    path.write_text(json.dumps(extra), encoding="utf-8")
    with pytest.raises(ConfigError, match=r"answer\.schema\.json.*provider"):
        read_answer(path, minimum_workstreams=2)


def test_all_machine_artifact_schemas_reject_incomplete_objects() -> None:
    for schema_name in (
        "answer.schema.json",
        "analysis.schema.json",
        "baseline.schema.json",
        "verification.schema.json",
    ):
        problem = schema_error({}, schema_name)
        assert problem is not None
        assert schema_name in problem


def test_context_views_have_independent_inodes_and_content(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.c"
    destination = tmp_path / "workspace.c"
    source.write_text("original\n", encoding="utf-8")
    source.chmod(0o444)

    copy_independent_file(source, destination)

    assert (source.stat().st_dev, source.stat().st_ino) != (
        destination.stat().st_dev,
        destination.stat().st_ino,
    )
    destination.chmod(0o644)
    destination.write_text("planted\n", encoding="utf-8")
    assert source.read_text(encoding="utf-8") == "original\n"


def test_blind_answer_order_accepts_a_fresh_permutation() -> None:
    answers = [{"hunter_id": name} for name in ("one", "two", "three")]
    shuffled = randomized_answer_order(answers, shuffle=lambda values: values.reverse())
    assert [item["hunter_id"] for item in shuffled] == ["three", "two", "one"]
    assert [item["hunter_id"] for item in answers] == ["one", "two", "three"]


def test_reproduction_references_must_resolve_to_captured_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "context" / "source.c"
    artifact = workspace / ".swarm" / "artifacts" / "trigger.txt"
    source.parent.mkdir(parents=True)
    artifact.parent.mkdir(parents=True)
    source.write_text("int main(void) {}\n", encoding="utf-8")
    artifact.write_text("triggered\n", encoding="utf-8")

    inspected = {
        "validation_method": "source_inspection",
        "reproduction": {
            "commands": [],
            "observed_result": "inspected",
            "artifact_refs": ["context/source.c:1"],
        },
    }
    reproduced = {
        "validation_method": "executed_reproduction",
        "reproduction": {
            "commands": ["run-trigger"],
            "observed_result": "triggered",
            "artifact_refs": [".swarm/artifacts/trigger.txt"],
        },
    }
    assert verified_reproduction_references(inspected, workspace) is not None
    assert verified_reproduction_references(reproduced, workspace) is not None

    reproduced["reproduction"]["artifact_refs"] = []
    assert verified_reproduction_references(reproduced, workspace) is None
    reproduced["reproduction"]["artifact_refs"] = ["claim without a file"]
    assert verified_reproduction_references(reproduced, workspace) is None
    reproduced["reproduction"]["artifact_refs"] = [".swarm/artifacts/trigger.txt"]
    reproduced["reproduction"]["commands"] = [""]
    assert verified_reproduction_references(reproduced, workspace) is None


def test_source_inspection_promotes_existing_evidence_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "context" / "ssl" / "record.c"
    source.parent.mkdir(parents=True)
    source.write_text("int inspect_me(void) { return 1; }\n", encoding="utf-8")
    finding = {
        "title": "State survives reset",
        "evidence": [
            "source reasoning",
            "context/ssl/record.c:1",
            "context/ssl/record.c:1",
            "context/missing.c:9",
            ".swarm/evidence/A/report.txt",
        ],
        "provenance_class": "independent_discovery",
        "validation_method": "source_inspection",
        "reproduction": {
            "commands": [],
            "observed_result": "the reset omits the state field",
            "artifact_refs": [],
        },
    }

    self_error = evidence_contract_error(finding, workspace)
    assert self_error == "source_inspection requires at least one regular source reference"
    assert promote_source_inspection_references(finding, workspace) == [
        "context/ssl/record.c:1"
    ]
    assert finding["reproduction"]["artifact_refs"] == ["context/ssl/record.c:1"]
    assert evidence_contract_error(finding, workspace) is None


def test_source_reference_promotion_does_not_weaken_other_contracts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "context" / "source.c"
    source.parent.mkdir(parents=True)
    source.write_text("int main(void) {}\n", encoding="utf-8")
    finding = {
        "evidence": ["context/source.c:1"],
        "provenance_class": "independent_discovery",
        "validation_method": "executed_reproduction",
        "reproduction": {
            "commands": ["run-trigger"],
            "observed_result": "triggered",
            "artifact_refs": [],
        },
    }

    assert promote_source_inspection_references(finding, workspace) == []
    assert evidence_contract_error(finding, workspace) == (
        "executed_reproduction requires at least one captured artifact reference"
    )


def test_analysis_contract_repairs_omitted_refs_and_names_unrepairable_finding(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "context" / "ssl" / "state.c"
    source.parent.mkdir(parents=True)
    source.write_text("int state;\n", encoding="utf-8")
    analysis = {
        "summary": "reviewed",
        "agreement": [],
        "disagreements": [],
        "answer_assessments": [
            {"answer_id": "A", "strengths": [], "weaknesses": [], "factual_risks": []}
        ],
        "validated_findings": [
            {
                "title": "State survives reset",
                "severity": "medium",
                "source_answer_ids": ["A"],
                "validation": "The reset omits this field.",
                "evidence": ["context/ssl/state.c:1"],
                "provenance_class": "independent_discovery",
                "validation_method": "source_inspection",
                "reproduction": {
                    "commands": [],
                    "observed_result": "state remains set",
                    "artifact_refs": [],
                },
            }
        ],
        "unresolved_questions": [],
        "recommended_answer": "Fix the reset path.",
        "confidence": 0.8,
    }

    repairs = _promote_analysis_source_references(analysis, workspace)
    assert repairs == [
        {
            "finding_index": 0,
            "title": "State survives reset",
            "artifact_refs": ["context/ssl/state.c:1"],
            "source": "finding.evidence",
        }
    ]
    assert _analysis_contract_error(
        analysis, expected_ids={"A"}, workspace=workspace
    ) is None

    analysis["validated_findings"][0]["reproduction"]["artifact_refs"] = []
    analysis["validated_findings"][0]["evidence"] = ["no resolvable source path"]
    assert _promote_analysis_source_references(analysis, workspace) == []
    assert _analysis_contract_error(
        analysis, expected_ids={"A"}, workspace=workspace
    ) == (
        'validated_findings[0] "State survives reset": source_inspection requires '
        "at least one regular source reference"
    )


def test_session_lock_recovers_stale_marker_but_blocks_live_owner(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    marker = session_dir / ".running"
    marker.mkdir(parents=True)
    (marker / "owner.json").write_text('{"pid": 999999999}\n', encoding="utf-8")

    first = SessionLock(session_dir, "audit")
    first.acquire()
    owner = json.loads((marker / "owner.json").read_text(encoding="utf-8"))
    assert owner["pid"] == os.getpid()

    second = SessionLock(session_dir, "audit")
    with pytest.raises(ConfigError, match="already has a running round"):
        second.acquire()

    first.release()
    assert not marker.exists()
    second.acquire()
    second.release()


def test_fanout_manifest_records_observation_without_claiming_independence() -> None:
    answer = _answer()
    answer["workstreams"] = [
        {"name": "Parser", "scope": "Input paths", "conclusion": "one"},
        {"name": " parser ", "scope": "input   paths", "conclusion": "two"},
    ]
    engine = SimpleNamespace(id="local-a", harness="opencode", swarm_size=2)
    result = {
        "fanout_evidence": "monitored_child_sessions",
        "worker_contract_verified": True,
        "worker_monitor": {
            "reason": "all_workers_terminal",
            "snapshot": {
                "children": [
                    {
                        "id": "child-1", "title": "parser review", "outcome": "succeeded",
                        "created_ms": 10, "updated_ms": 20, "output_tokens": 100,
                    },
                    {
                        "id": "child-2", "title": "runtime review", "outcome": "succeeded",
                        "created_ms": 11, "updated_ms": 21, "output_tokens": 120,
                    },
                ]
            },
        },
    }

    manifest = build_fanout_manifest(engine, answer, result)
    summary = fanout_summary(manifest)

    assert manifest["observed_child_count"] == 2
    assert manifest["duplicate_reported_workstream_indexes"] == [[0, 1]]
    assert manifest["semantic_independence_verified"] is False
    assert summary["semantic_independence_verified"] is False
    assert any("not distinct reasoning" in item for item in manifest["limitations"])
