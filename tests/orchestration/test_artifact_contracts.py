# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarmerz.orchestration.artifacts import read_answer
from swarmerz.orchestration.fanout import build_fanout_manifest, fanout_summary
from swarmerz.orchestration.models import ConfigError
from swarmerz.orchestration.schema_validation import schema_error
from swarmerz.orchestration.session_lock import SessionLock


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
