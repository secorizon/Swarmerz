# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

"""Claude Code command construction and structured-stream inspection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .artifacts import write_json
from .models import CommandSpec, ConfigError, EngineConfig


CLAUDE_CODE_HARNESSES = {"claude-code"}
_HUNTER_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,Agent"
_ADJUDICATOR_TOOLS = "Bash,Read,Write,Edit,Glob,Grep"


def is_native_claude(engine: EngineConfig) -> bool:
    return engine.harness in CLAUDE_CODE_HARNESSES and not engine.command


def _schema_argument(path: Path) -> str:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"unable to load Claude Code output schema {path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise ConfigError(f"Claude Code output schema must be an object: {path}")
    # Claude Code validates the inline object with a bundled validator that
    # does not register Swarmerz's Draft 2020-12 meta-schema URI. The schemas'
    # actual constraints use the common subset accepted by that validator, so
    # omit non-semantic document metadata only for the CLI argument. Swarmerz
    # still validates the materialized artifact against the canonical file.
    schema.pop("$schema", None)
    schema.pop("$comment", None)
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _worker_agents(count: int) -> str:
    agents: dict[str, dict[str, Any]] = {}
    worker_tools = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
    for number in range(1, count + 1):
        name = f"swarmerz-worker-{number:02d}"
        agents[name] = {
            "description": (
                f"Swarmerz independent investigation worker {number} of {count}; "
                "assign exactly one distinct workstream to this worker"
            ),
            "prompt": (
                f"You are Swarmerz worker {number} of {count}. Investigate only the distinct "
                "scope assigned by the parent. Work independently, test claims when practical, "
                "preserve durable evidence under .swarm/artifacts/, and return concise findings, "
                "commands, file references, uncertainty, and negative results to the parent. "
                "Do not launch other agents and do not identify the model or provider."
            ),
            "tools": worker_tools,
            "model": "inherit",
            "permissionMode": "dontAsk",
            "omitClaudeMd": True,
        }
    return json.dumps(agents, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def claude_code_arguments(
    engine: EngineConfig,
    *,
    schema_path: Path,
    prompt: str,
    hunter: bool,
) -> list[str]:
    """Return noninteractive Claude Code arguments with an explicit tool boundary."""
    tools = _HUNTER_TOOLS if hunter else _ADJUDICATOR_TOOLS
    arguments = [
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--restricted",
        "--strict-mcp-config",
        "--no-chrome",
        "--disable-slash-commands",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--tools", tools,
        "--allowedTools", tools,
        "--model", engine.effective_model,
        "--effort", engine.reasoning_effort,
        "--json-schema", _schema_argument(schema_path),
    ]
    if hunter:
        arguments.extend((
            "--forward-subagent-text",
            "--agents", _worker_agents(engine.swarm_size),
        ))
    else:
        # No custom agent is needed for adjudication, so safe mode can also
        # suppress CLAUDE.md, skills, hooks, plugins, and discovered agents.
        arguments.append("--safe-mode")
    arguments.extend(engine.extra_args)
    arguments.append(prompt)
    return arguments


def iter_claude_events(path: Path) -> Iterable[dict[str, Any]]:
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def inspect_claude_stream(path: Path) -> dict[str, Any]:
    """Return the last session/result and observable Agent calls in a JSONL stream."""
    session_id: str | None = None
    structured_output: dict[str, Any] | None = None
    result_error: str | None = None
    calls: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, str] = {}
    for event in iter_claude_events(path):
        candidate_session = event.get("session_id")
        if isinstance(candidate_session, str) and candidate_session:
            session_id = candidate_session
        event_type = event.get("type")
        if event_type == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            parent_id = event.get("parent_tool_use_id")
            for block in content:
                if (
                    parent_id is None
                    and isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Agent"
                ):
                    tool_id = block.get("id")
                    inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
                    if isinstance(tool_id, str) and tool_id:
                        calls[tool_id] = {
                            "id": tool_id,
                            "name": str(inputs.get("subagent_type") or ""),
                            "description": str(inputs.get("description") or ""),
                        }
        elif event_type == "user":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id")
                if isinstance(tool_id, str) and tool_id in calls:
                    outcomes[tool_id] = (
                        "failed" if bool(block.get("is_error")) else "succeeded"
                    )
        elif event_type == "result":
            candidate = event.get("structured_output")
            if isinstance(candidate, dict):
                structured_output = candidate
            if event.get("is_error"):
                detail = event.get("result") or event.get("error") or event.get("subtype")
                if detail:
                    result_error = str(detail)
    children = []
    for tool_id, call in calls.items():
        outcome = outcomes.get(tool_id)
        children.append({
            "id": tool_id,
            "title": call["name"] or call["description"] or "Claude Code worker",
            "outcome": outcome,
            "created_ms": 0,
            "updated_ms": 0,
            "output_tokens": 0,
        })
    return {
        "session_id": session_id,
        "structured_output": structured_output,
        "result_error": result_error,
        "calls": list(calls.values()),
        "children": children,
        "terminal_workers": len(outcomes),
    }


def materialize_claude_output(stdout_path: Path, artifact_path: Path | None) -> dict[str, Any]:
    inspection = inspect_claude_stream(stdout_path)
    structured = inspection.get("structured_output")
    if artifact_path is not None and isinstance(structured, dict):
        write_json(artifact_path, structured)
    return inspection


def claude_fanout_evidence(path: Path, expected_workers: int) -> dict[str, Any]:
    inspection = inspect_claude_stream(path)
    calls = inspection["calls"]
    expected_names = {
        f"swarmerz-worker-{number:02d}" for number in range(1, expected_workers + 1)
    }
    observed_names = {str(call.get("name") or "") for call in calls}
    verified = len(calls) == expected_workers and observed_names == expected_names
    terminal = int(inspection["terminal_workers"])
    snapshot = {
        "parent_session_id": inspection.get("session_id"),
        "expected_workers": expected_workers,
        "observed_workers": len(calls),
        "terminal_workers": terminal,
        "active_workers": max(0, len(calls) - terminal),
        "all_workers_terminal": verified and terminal >= expected_workers,
        "outcomes": {
            outcome: sum(child.get("outcome") == outcome for child in inspection["children"])
            for outcome in ("succeeded", "failed")
        },
        "active_titles": [
            str(child["title"]) for child in inspection["children"] if not child.get("outcome")
        ],
        "output_tokens": 0,
        "latest_terminal_ms": 0,
        "earliest_created_ms": 0,
        "children": inspection["children"],
    }
    return {
        "verified": verified,
        "reason": "agent_calls_verified" if verified else "agent_calls_incomplete",
        "snapshot": snapshot,
    }


def claude_resume_spec(spec: CommandSpec, session_id: str, prompt: str) -> CommandSpec:
    """Resume a persisted Claude Code session with a replacement instruction."""
    if not spec.argv or spec.argv[-1] == "--resume":
        raise ConfigError("cannot resume malformed Claude Code command")
    argv = list(spec.argv[:-1])
    argv.extend(("--resume", session_id, prompt))
    return CommandSpec(tuple(argv), spec.cwd, spec.env, spec.artifact_path)
