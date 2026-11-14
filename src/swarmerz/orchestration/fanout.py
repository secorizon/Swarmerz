# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import stat
import time
from pathlib import Path
from typing import Any

from .artifacts import read_answer
from .models import ConfigError, EngineConfig


_SPACE = re.compile(r"\s+")


def opencode_worker_snapshot(
    payload: Any,
    *,
    parent_session_id: str,
    expected_workers: int,
) -> dict[str, Any]:
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ConfigError("OpenCode session.list response has no data array")
    children: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or record.get("parentID") != parent_session_id:
            continue
        timing = record.get("time") if isinstance(record.get("time"), dict) else {}
        tokens = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
        outcome = record.get("outcome")
        children.append(
            {
                "id": str(record.get("id") or ""),
                "title": str(record.get("title") or record.get("id") or "worker"),
                "outcome": str(outcome) if outcome else None,
                "created_ms": int(timing.get("created") or 0),
                "updated_ms": int(timing.get("updated") or 0),
                "idle_ms": int(timing.get("idle") or 0),
                "output_tokens": int(tokens.get("output") or 0),
            }
        )
    children.sort(key=lambda item: (item["created_ms"], item["id"]))
    terminal = [item for item in children if item["outcome"]]
    active = [item for item in children if not item["outcome"]]
    outcome_counts: dict[str, int] = {}
    for item in terminal:
        outcome = str(item["outcome"])
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    latest_terminal_ms = max(
        (int(item["idle_ms"] or item["updated_ms"]) for item in terminal),
        default=0,
    )
    earliest_created_ms = min(
        (int(item["created_ms"]) for item in children if item["created_ms"]),
        default=0,
    )
    return {
        "parent_session_id": parent_session_id,
        "expected_workers": expected_workers,
        "observed_workers": len(children),
        "terminal_workers": len(terminal),
        "active_workers": len(active),
        "all_workers_terminal": len(children) >= expected_workers and not active,
        "outcomes": outcome_counts,
        "active_titles": [str(item["title"]) for item in active],
        "output_tokens": sum(item["output_tokens"] for item in children),
        "latest_terminal_ms": latest_terminal_ms,
        "earliest_created_ms": earliest_created_ms,
        "children": children,
    }


def answer_postdates_workers(answer_path: Path, snapshot: dict[str, Any]) -> bool:
    if (
        not snapshot.get("all_workers_terminal")
        or not answer_path.is_file()
        or answer_path.is_symlink()
    ):
        return False
    latest_terminal_ms = int(snapshot.get("latest_terminal_ms") or 0)
    if latest_terminal_ms <= 0:
        return False
    return answer_path.stat().st_mtime_ns // 1_000_000 >= latest_terminal_ms


def settled_partial_answer(
    answer_path: Path,
    snapshot: dict[str, Any],
    *,
    minimum_workstreams: int,
) -> bool:
    """Accept an early answer only when it postdates all but one succeeded child."""
    expected = int(snapshot.get("expected_workers") or 0)
    if (
        expected < 2
        or int(snapshot.get("observed_workers") or 0) < expected
        or int(snapshot.get("active_workers") or 0) != 1
        or int(dict(snapshot.get("outcomes") or {}).get("succeeded") or 0)
        < expected - 1
        or not answer_path.is_file()
        or answer_path.is_symlink()
    ):
        return False
    latest_terminal_ms = int(snapshot.get("latest_terminal_ms") or 0)
    if latest_terminal_ms <= 0:
        return False
    try:
        if answer_path.stat().st_mtime_ns // 1_000_000 < latest_terminal_ms:
            return False
        read_answer(answer_path, minimum_workstreams=minimum_workstreams)
    except (ConfigError, OSError):
        return False
    return True


def opencode_finalize_instruction(
    answer_path: str,
    minimum_workstreams: int,
    worker_monitor: dict[str, Any] | None = None,
) -> str:
    snapshot = worker_monitor.get("snapshot", {}) if isinstance(worker_monitor, dict) else {}
    summary = {
        "reason": worker_monitor.get("reason") if isinstance(worker_monitor, dict) else "unknown",
        "expected_workers": snapshot.get("expected_workers"),
        "observed_workers": snapshot.get("observed_workers"),
        "terminal_workers": snapshot.get("terminal_workers"),
        "active_workers": snapshot.get("active_workers"),
        "outcomes": snapshot.get("outcomes"),
        "active_titles": snapshot.get("active_titles"),
    }
    return f"""FINALIZE THE PREVIOUS TURN NOW.

The external worker monitor reports:
{json.dumps(summary, ensure_ascii=False, sort_keys=True)}

Do not launch any new subagents, wait for unfinished ones, fetch new web resources, or start another investigation. Use only the completed child results, files, notifications, and evidence already present in this session/workspace. Represent failed or timed-out workers honestly and abandon any remaining stragglers. Critically reconcile the completed work and synthesize the strongest answer available now.

Do all checking before writing the final artifact. Replace any provisional answer and write fresh valid JSON to {answer_path} using the exact answer schema from the original instruction. Include at least {minimum_workstreams} workstream entries; a failed or timed-out worker may be represented honestly with its scope and outcome. Verify that the file exists and parses as JSON, then end the turn immediately. Do not return a prose-only response and do not end before writing the artifact.
"""


async def wait_for_stable_final_answer(
    answer_path: Path, *, minimum_workstreams: int, quiet_seconds: float,
) -> None:
    """Return only after a fresh, schema-valid answer stops changing."""
    signature: tuple[int, int, int] | None = None
    stable_since: float | None = None
    while True:
        await asyncio.sleep(max(0.05, min(1.0, quiet_seconds / 4)))
        current: tuple[int, int, int] | None = None
        try:
            if not answer_path.is_symlink():
                info = answer_path.stat()
                if stat.S_ISREG(info.st_mode) and info.st_size <= 64 * 1024 * 1024:
                    read_answer(answer_path, minimum_workstreams=minimum_workstreams)
                    current = (info.st_ino, info.st_mtime_ns, info.st_size)
        except (ConfigError, OSError, UnicodeError):
            pass
        now = time.monotonic()
        if current is None:
            signature = None
            stable_since = None
        elif current != signature:
            signature = current
            stable_since = now
        elif stable_since is not None and now - stable_since >= quiet_seconds:
            return


def _normalized(value: str) -> str:
    return _SPACE.sub(" ", value.strip().lower())


def build_fanout_manifest(
    engine: EngineConfig,
    answer: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Describe observable fan-out without claiming semantic independence."""
    workstreams = answer.get("workstreams", [])
    reported: list[dict[str, Any]] = []
    fingerprints: dict[str, list[int]] = {}
    for index, item in enumerate(workstreams):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        scope = str(item.get("scope", ""))
        conclusion = str(item.get("conclusion", ""))
        fingerprint = hashlib.sha256(
            f"{_normalized(name)}\0{_normalized(scope)}".encode()
        ).hexdigest()
        fingerprints.setdefault(fingerprint, []).append(index)
        reported.append(
            {
                "index": index,
                "name": name,
                "scope": scope,
                "conclusion_sha256": hashlib.sha256(conclusion.encode()).hexdigest(),
                "name_scope_sha256": fingerprint,
            }
        )
    duplicates = [indices for indices in fingerprints.values() if len(indices) > 1]

    monitor = result.get("worker_monitor")
    snapshot = monitor.get("snapshot", {}) if isinstance(monitor, dict) else {}
    children = snapshot.get("children", []) if isinstance(snapshot, dict) else []
    observed_children = [
        {
            "session_id": str(child.get("id", "")),
            "title": str(child.get("title", "")),
            "outcome": child.get("outcome"),
            "created_ms": int(child.get("created_ms") or 0),
            "updated_ms": int(child.get("updated_ms") or 0),
            "output_tokens": int(child.get("output_tokens") or 0),
        }
        for child in children
        if isinstance(child, dict)
    ]
    evidence_kind = str(result.get("fanout_evidence", "unverified"))
    ledger = result.get("worker_ledger")
    return {
        "hunter_id": engine.id,
        "harness": engine.harness,
        "expected_children": engine.swarm_size,
        "evidence_kind": evidence_kind,
        "worker_contract_verified": bool(result.get("worker_contract_verified", False)),
        "observed_child_sessions": observed_children,
        "observed_child_count": len(observed_children),
        "worker_monitor_reason": (
            monitor.get("reason") if isinstance(monitor, dict) else None
        ),
        "self_reported_ledger": ledger if isinstance(ledger, dict) else None,
        "reported_workstreams": reported,
        "reported_workstream_count": len(reported),
        "unique_reported_name_scope_count": len(fingerprints),
        "duplicate_reported_workstream_indexes": duplicates,
        "semantic_independence_verified": False,
        "limitations": [
            "Observed child sessions prove process/session fan-out, not distinct reasoning.",
            "Reported workstream scopes are model-authored and cannot be cryptographically matched to child cognition.",
        ],
    }


def fanout_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_kind": manifest.get("evidence_kind", "unverified"),
        "expected_children": manifest.get("expected_children", 0),
        "observed_child_count": manifest.get("observed_child_count", 0),
        "reported_workstream_count": manifest.get("reported_workstream_count", 0),
        "unique_reported_name_scope_count": manifest.get(
            "unique_reported_name_scope_count", 0
        ),
        "duplicate_reported_workstream_indexes": manifest.get(
            "duplicate_reported_workstream_indexes", []
        ),
        "semantic_independence_verified": False,
    }

