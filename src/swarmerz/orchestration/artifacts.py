# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .models import ConfigError
from .schema_validation import require_schema


_PROVENANCE_KEYS = {
    "hunter",
    "hunter_id",
    "model",
    "model_id",
    "model_name",
    "provider",
    "producer",
    "agent",
    "agent_id",
    "session_id",
    "harness",
}


def answer_labels(count: int) -> tuple[str, ...]:
    if not 1 <= count <= 26:
        raise ConfigError(f"answer count must be between 1 and 26; got {count}")
    return tuple(chr(ord("A") + index) for index in range(count))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc


def collect_partial_notes(workspace: Path, artifact_dir: Path) -> dict[str, Any] | None:
    """Preserve small, regular worker notes without pretending they are sealed answers."""
    note_dir = workspace / ".swarm"
    if not note_dir.is_dir() or note_dir.is_symlink():
        return None
    sections = [
        "# Interrupted hunter notes — unverified",
        "",
        "These are raw worker drafts. They did not satisfy the answer contract and have not been adjudicated.",
        "",
    ]
    sources: list[str] = []
    total_bytes = 0
    note_root = note_dir.resolve()
    candidates = list(note_dir.glob("*.md"))
    worker_dir = note_dir / "workers"
    if worker_dir.is_dir() and not worker_dir.is_symlink():
        candidates.extend(worker_dir.rglob("*.md"))
    for path in sorted(set(candidates))[:32]:
        if path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(note_root):
            continue
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                size = os.fstat(descriptor)
                if not stat.S_ISREG(size.st_mode) or size.st_size > 256_000:
                    continue
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    content = handle.read(256_001)
            finally:
                os.close(descriptor)
        except OSError:
            continue
        if len(content) > 256_000 or total_bytes + len(content) > 1_000_000:
            continue
        total_bytes += len(content)
        relative_name = path.relative_to(note_dir).as_posix()
        sources.append(relative_name)
        sections.extend([
            f"## {relative_name}", "", content.decode("utf-8", errors="replace"), ""
        ])
    if not sources:
        return None
    artifact_dir.mkdir(parents=True, exist_ok=True)
    destination = artifact_dir / "partial-notes.md"
    destination.write_text("\n".join(sections), encoding="utf-8")
    return {"path": str(destination), "sources": sources, "bytes": total_bytes, "unverified": True}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_answer(path: Path, *, minimum_workstreams: int) -> dict[str, Any]:
    value = read_json(path)
    require_schema(value, "answer.schema.json", where=str(path))
    assert isinstance(value, dict)
    workstreams = value["workstreams"]
    if len(workstreams) < minimum_workstreams:
        raise ConfigError(
            f"answer.workstreams must contain at least {minimum_workstreams} entries: {path}"
        )
    if not all(all(item[key].strip() for key in ("name", "scope", "conclusion")) for item in workstreams):
        raise ConfigError(f"answer.workstreams entries must be non-empty: {path}")
    return value


def seal_answer(source: Path, artifact_dir: Path, *, minimum_workstreams: int) -> tuple[dict[str, Any], dict[str, Any]]:
    if source.is_symlink() or not source.is_file():
        raise OSError(f"refusing non-regular answer artifact: {source}")
    size = source.stat().st_size
    if size > 64 * 1024 * 1024:
        raise OSError(f"answer artifact exceeds 64 MiB: {source}")
    answer = read_answer(source, minimum_workstreams=minimum_workstreams)
    destination = artifact_dir / "answer.json"
    if source.resolve() != destination.resolve():
        if destination.exists() or destination.is_symlink():
            raise OSError(f"refusing to replace existing answer artifact: {destination}")
        shutil.copy2(source, destination)
    digest = sha256_file(destination)
    destination.chmod(0o444)
    seal = {
        "algorithm": "sha256",
        "size_bytes": size,
        "artifacts": {"answer.json": digest},
        "sealed": True,
    }
    write_json(artifact_dir / "seal.json", seal)
    return answer, seal


def _preserve_evidence_file(
    workspace_root: Path,
    source: Path,
    artifact_dir: Path,
) -> dict[str, Any]:
    relative = source.relative_to(workspace_root)
    digest = sha256_file(source)
    context_root = workspace_root / "context"
    if source.is_relative_to(context_root):
        return {
            "source": relative.as_posix(),
            "stored": f"session-context/{source.relative_to(context_root).as_posix()}",
            "sha256": digest,
            "size_bytes": source.stat().st_size,
            "copied": False,
        }
    destination = artifact_dir / "evidence" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if (
            destination.is_symlink()
            or not destination.is_file()
            or sha256_file(destination) != digest
        ):
            raise OSError(f"preserved evidence changed: {destination}")
    else:
        shutil.copy2(source, destination)
        destination.chmod(0o444)
    return {
        "source": relative.as_posix(),
        "stored": destination.relative_to(artifact_dir).as_posix(),
        "sha256": digest,
        "size_bytes": source.stat().st_size,
        "copied": True,
    }


def preserve_workspace_artifacts(workspace: Path, artifact_dir: Path) -> dict[str, Any]:
    """Seal explicitly staged durable evidence before a successful workspace cleanup."""
    source_root = workspace / ".swarm" / "artifacts"
    if not source_root.exists():
        return {"artifact_count": 0, "size_bytes": 0, "artifacts": []}
    if source_root.is_symlink() or not source_root.is_dir():
        raise OSError(f"refusing unsafe workspace artifact directory: {source_root}")
    workspace_root = workspace.resolve()
    artifacts: list[dict[str, Any]] = []
    for directory, names, files in os.walk(source_root, followlinks=False):
        directory_path = Path(directory)
        for name in names:
            if (directory_path / name).is_symlink():
                raise OSError(f"refusing symlink in workspace artifacts: {directory_path / name}")
        for name in files:
            path = directory_path / name
            if path.is_symlink() or not path.is_file():
                raise OSError(f"refusing non-regular workspace artifact: {path}")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(workspace_root):
                raise OSError(f"workspace artifact escapes workspace: {path}")
            artifacts.append(_preserve_evidence_file(workspace_root, resolved, artifact_dir))
    result = {
        "artifact_count": len(artifacts),
        "size_bytes": sum(int(item["size_bytes"]) for item in artifacts),
        "artifacts": sorted(artifacts, key=lambda item: str(item["source"])),
    }
    if artifacts:
        write_json(artifact_dir / "evidence-manifest.json", result)
    return result


def verify_worker_ledger(
    workspace: Path, artifact_dir: Path, *, minimum_workers: int
) -> dict[str, Any]:
    """Validate and seal a custom harness's auditable worker ledger."""
    source = workspace / ".swarm" / "worker-ledger.json"
    if source.is_symlink() or not source.is_file():
        return {"status": "unverified", "reason": "missing .swarm/worker-ledger.json"}
    try:
        ledger = read_json(source)
    except (ConfigError, OSError) as exc:
        return {"status": "unverified", "reason": str(exc)}
    workers = ledger.get("workers") if isinstance(ledger, dict) else None
    if not isinstance(workers, list) or len(workers) < minimum_workers:
        return {
            "status": "unverified",
            "reason": f"ledger requires at least {minimum_workers} worker entries",
        }
    worker_ids: set[str] = set()
    evidence_hashes: dict[str, str] = {}
    preserved_evidence: dict[str, str] = {}
    workspace_root = workspace.resolve()
    for worker in workers:
        if not isinstance(worker, dict):
            return {"status": "unverified", "reason": "ledger worker entry is not an object"}
        worker_id = worker.get("worker_id")
        scope = worker.get("scope")
        status = worker.get("status")
        evidence_refs = worker.get("evidence_refs")
        if (
            not isinstance(worker_id, str)
            or not worker_id.strip()
            or worker_id in worker_ids
            or not isinstance(scope, str)
            or not scope.strip()
            or status not in {"completed", "failed", "timed_out"}
            or not isinstance(evidence_refs, list)
            or not all(isinstance(item, str) and item for item in evidence_refs)
        ):
            return {"status": "unverified", "reason": "ledger worker entry is invalid"}
        worker_ids.add(worker_id)
        if status == "completed" and not evidence_refs:
            return {
                "status": "unverified",
                "reason": f"completed worker {worker_id!r} has no evidence_refs",
            }
        for reference in evidence_refs:
            path = workspace / reference
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                return {"status": "unverified", "reason": f"missing evidence ref: {reference}"}
            if (
                not resolved.is_relative_to(workspace_root)
                or path.is_symlink()
                or not path.is_file()
            ):
                return {"status": "unverified", "reason": f"unsafe evidence ref: {reference}"}
            evidence_hashes[reference] = sha256_file(path)
            try:
                preserved = _preserve_evidence_file(workspace_root, resolved, artifact_dir)
            except OSError as exc:
                return {"status": "unverified", "reason": str(exc)}
            preserved_evidence[reference] = str(preserved["stored"])
    destination = artifact_dir / "worker-ledger.json"
    source_digest = sha256_file(source)
    if destination.exists() or destination.is_symlink():
        if (
            destination.is_symlink()
            or not destination.is_file()
            or sha256_file(destination) != source_digest
        ):
            return {"status": "unverified", "reason": "sealed worker ledger changed"}
    else:
        shutil.copy2(source, destination)
        destination.chmod(0o444)
    seal = {
        "status": "ledger_verified",
        "worker_count": len(workers),
        "ledger_sha256": source_digest,
        "evidence_sha256": evidence_hashes,
        "preserved_evidence": preserved_evidence,
        "note": "Structurally verified self-reported ledger; not OS-level process attestation.",
    }
    write_json(artifact_dir / "worker-ledger-seal.json", seal)
    return seal


def _blind(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _blind(item) for key, item in value.items() if key.lower() not in _PROVENANCE_KEYS}
    if isinstance(value, list):
        return [_blind(item) for item in value]
    return value


def build_blind_bundle(instruction: str, answers: list[dict[str, Any]]) -> dict[str, Any]:
    labels = answer_labels(len(answers))
    return {
        "instruction": instruction,
        "answers": [
            {"answer_id": label, "response": _blind(record["answer"])}
            for label, record in zip(labels, answers, strict=True)
        ],
    }


def attribute_analysis(
    analysis: dict[str, Any],
    answers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add hunter attribution only after the primary adjudicator completed blind review."""
    labels = answer_labels(len(answers))
    hunter_by_label = {
        label: str(record["hunter_id"]).upper()
        for label, record in zip(labels, answers, strict=True)
    }
    attributed = copy.deepcopy(analysis)
    findings = attributed.get("validated_findings")
    if not isinstance(findings, list):
        return attributed

    rendered: list[str] = ["## Validated findings", ""]
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        source_ids = finding.get("source_answer_ids", [])
        hunters = [hunter_by_label[item] for item in source_ids if item in hunter_by_label]
        title = str(finding.get("title", "Untitled finding")).strip()
        prefix = ", ".join(hunters) if hunters else "UNKNOWN"
        attributed_title = f"[{prefix}]: {title}"
        finding["title"] = attributed_title
        finding["source_hunters"] = hunters
        rendered.extend(
            [
                f"### {attributed_title}",
                "",
                f"**Severity:** {str(finding.get('severity', 'unknown')).upper()}",
                "",
                f"**Provenance:** {str(finding.get('provenance_class', 'unclassified'))}",
                "",
                f"**Validation method:** {str(finding.get('validation_method', 'reasoning_only'))}",
                "",
                str(finding.get("validation", "")),
                "",
            ]
        )
        evidence = finding.get("evidence")
        if isinstance(evidence, list) and evidence:
            rendered.extend(["**Evidence:**", ""])
            rendered.extend(f"- {item}" for item in evidence)
            rendered.append("")

    if findings:
        original = str(attributed.get("recommended_answer", "")).strip()
        rendered.extend(["## Adjudicator synthesis", "", original])
        attributed["recommended_answer"] = "\n".join(rendered).strip()
    return attributed


def write_final_report(
    final_dir: Path,
    *,
    instruction: str,
    answers: list[dict[str, Any]],
    analysis: dict[str, Any] | None,
    expected_answer_count: int | None = None,
    minimum_answer_count: int | None = None,
    partial_hunters: list[str] | None = None,
    partial_note_paths: list[str] | None = None,
    adjudication: dict[str, Any] | None = None,
    baseline: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    fanout_evidence: list[dict[str, Any]] | None = None,
) -> None:
    labeled = []
    labels = answer_labels(len(answers)) if answers else ()
    for label, record in zip(labels, answers, strict=True):
        labeled.append(
            {
                "answer_id": label,
                "hunter_id": record["hunter_id"],
                "answer": record["answer"],
                "sha256": record["sha256"],
            }
        )
    write_json(final_dir / "answers.json", labeled)
    if analysis is not None:
        write_json(final_dir / "analysis.json", analysis)

    expected = expected_answer_count if expected_answer_count is not None else len(answers)
    minimum = minimum_answer_count if minimum_answer_count is not None else expected
    lines = [
        "# Swarmerz Report",
        "",
        "## Instruction",
        "",
        instruction,
        "",
        "## Coverage",
        "",
        f"Received {len(answers)} of {expected} configured hunter answers; analysis quorum: {minimum}.",
        "",
    ]
    if partial_hunters:
        lines.extend([
            "Partial internal swarm coverage: " + ", ".join(partial_hunters) + ".",
            "",
        ])
    if partial_note_paths:
        lines.extend([
            "## Unverified interrupted notes",
            "",
            "These drafts did not satisfy the hunter answer contract and were not included in adjudication.",
            "",
        ])
        lines.extend(f"- {path}" for path in partial_note_paths)
        lines.append("")
    if fanout_evidence:
        lines.extend(["## Hunter fan-out evidence", ""])
        for item in fanout_evidence:
            summary = item.get("fanout_summary")
            detail = ""
            if isinstance(summary, dict):
                detail = (
                    f"; observed children {summary.get('observed_child_count', 0)}/"
                    f"{summary.get('expected_children', 0)}; reported workstreams "
                    f"{summary.get('reported_workstream_count', 0)} "
                    f"({summary.get('unique_reported_name_scope_count', 0)} unique name/scope pairs)"
                )
            lines.append(
                f"- {item.get('hunter_id', 'unknown')}: "
                f"{item.get('fanout_evidence', 'unverified')}{detail}"
            )
        lines.extend([
            "",
            "Observed sessions establish fan-out, not semantic independence. A self-reported ledger is "
            "structurally checked and sealed, but is not OS-level process attestation.",
            "",
        ])
    if adjudication:
        lines.extend(["## Adjudicator independence", ""])
        primary = adjudication.get("primary", {})
        lines.append(
            "Primary: "
            f"{primary.get('provider', 'unknown')}/{primary.get('model', 'unknown')} "
            f"via {primary.get('harness', 'unknown')}."
        )
        lines.append("")
        primary_overlap = adjudication.get("primary_hunter_overlap", [])
        if primary_overlap:
            rendered = ", ".join(
                f"{item.get('hunter_id')} ({'/'.join(item.get('shared_axes', []))})"
                for item in primary_overlap
            )
            lines.extend([f"Hunter substrate overlap: {rendered}.", ""])
        else:
            lines.extend(["Hunter substrate overlap: none detected from configured metadata.", ""])
        secondary = adjudication.get("secondary")
        if isinstance(secondary, dict):
            lines.extend([
                "Secondary verifier: "
                f"{secondary.get('provider', 'unknown')}/{secondary.get('model', 'unknown')} "
                f"via {secondary.get('harness', 'unknown')}.",
                "",
            ])
        lines.extend([
            "This is configured-substrate disclosure, not proof of stylistic anonymity or model independence.",
            "",
        ])
    if baseline is not None:
        lines.extend([
            "## Baseline-first assessment",
            "",
            str(baseline.get("summary", "")),
            "",
            f"Candidate findings recorded before hunter disclosure: {len(baseline.get('candidate_findings', []))}.",
            "",
        ])
    if verification is not None:
        counts = {status: 0 for status in ("confirmed", "rejected", "uncertain")}
        for item in verification.get("finding_assessments", []):
            if isinstance(item, dict) and item.get("status") in counts:
                counts[item["status"]] += 1
        lines.extend([
            "## Secondary verification",
            "",
            str(verification.get("summary", "")),
            "",
            (
                f"Confirmed: {counts['confirmed']}; rejected: {counts['rejected']}; "
                f"uncertain: {counts['uncertain']}. Only confirmed findings are carried forward."
            ),
            "",
        ])
    if analysis is None:
        lines.extend(
            [
                "## Adjudicator analysis",
                "",
                "Not produced because the configured hunter-answer quorum was not reached or the analyzer failed.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "## Adjudicator recommended answer",
                "",
                str(analysis.get("recommended_answer", "")),
                "",
                "## Adjudicator comparison summary",
                "",
                str(analysis.get("summary", "")),
                "",
            ]
        )
    for record in labeled:
        lines.extend(
            [
                f"## Answer {record['answer_id']} ({record['hunter_id']})",
                "",
                str(record["answer"].get("answer", "")),
                "",
            ]
        )
    final_dir.mkdir(parents=True, exist_ok=True)
    (final_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
