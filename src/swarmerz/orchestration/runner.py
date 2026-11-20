# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from ..models import display_model_name
from .adapters import (
    build_auth_probe_command,
    build_probe_command,
)
from .adjudication import AdjudicationMixin
from .artifacts import (
    attribute_analysis,
    evidence_contract_error,
    randomized_answer_order,
    read_json,
    seal_answer,
    sha256_file,
    write_final_report,
    write_json,
)
from .codex_models import check_chatgpt_codex_models
from .codex_appserver import CodexAppServer
from .claude_code import (
    claude_fanout_evidence,
    claude_resume_spec as _claude_resume_spec,
    is_native_claude,
    materialize_claude_output,
)
from .execution import execute
from .fanout import (
    answer_postdates_workers as _answer_postdates_workers,
    opencode_finalize_instruction as _opencode_finalize_instruction,
    opencode_worker_snapshot as _opencode_worker_snapshot,
    settled_partial_answer as _settled_partial_answer,
    wait_for_stable_final_answer as _wait_for_stable_final_answer,
)
from .hunter_execution import HunterExecutionMixin
from .local_context import instruction_for_workspace
from .live_control import LiveControl
from .models import (
    AppConfig,
    CommandSpec,
    ConfigError,
    EngineConfig,
    ResourceLimitsConfig,
)
from .ollama_probe import probe_ollama_model
from .opencode_permissions import (
    OpenCodeEndpoint,
    active_session_ids,
    call_opencode_api,
    interrupt_worker_session,
    list_worker_sessions,
    pending_worker_permissions,
    reject_worker_permission,
    steer_session,
)
from .opencode_server import IsolatedOpenCodeServer, _async_server_ready
from .protocol_support import (
    adjudicator_disclosure as _adjudicator_disclosure,
    new_id as _new_id,
    utc_now as _utc_now,
)
from .round_protocol import execute_round_protocol
from .resource_limits import ResourceLimiter
from .session_context import (
    existing_context_source as _existing_context_source,
    resolve_context,
    validate_context_tree as _validate_context_tree,
)
from .session_lifecycle import SessionLifecycleMixin
from .session_lock import SessionLock
from .storage import StorageManager


_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")


def _hunter_progress_name(engine: EngineConfig) -> str:
    if engine.provider == "ollama":
        return engine.effective_model
    if engine.harness in {"codex", "codex-exec", "claude-code"} and not engine.command:
        return display_model_name(engine.effective_model)
    return engine.id


_VALIDATION_METHODS = {"executed_reproduction", "source_inspection", "reasoning_only"}


def _valid_evidence_contract(record: Any, workspace: Path | None = None) -> bool:
    return evidence_contract_error(record, workspace) is None


@asynccontextmanager
async def _opencode_round_slot(
    emit: Callable[[str], None],
    lock_root: Path | None = None,
):
    """Serialize OpenCode rounds sharing this user's credential/session database."""
    lock_path = (lock_root or Path(tempfile.gettempdir())) / f"swarmerz-opencode-{os.getuid()}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ConfigError(f"OpenCode capacity lock is not owned by this user: {lock_path}")
    acquired = False
    next_notice = 0.0
    try:
        while not acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                now = time.monotonic()
                if now >= next_notice:
                    emit("[setup] another OpenCode round is active; waiting for its capacity slot")
                    next_notice = now + 60
                await asyncio.sleep(1)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()} started={_utc_now()}\n".encode())
        yield
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _shared_opencode_service_spec(
    prepared: list[tuple[EngineConfig, CommandSpec, Path]],
) -> CommandSpec | None:
    """Build a safe common template for isolated native OpenCode services."""
    native = [
        spec for engine, spec, _artifact_dir in prepared
        if engine.harness == "opencode" and not engine.command
    ]
    if not native:
        return None
    first = native[0]
    first_executable = first.argv[:first.argv.index("run")]
    role_local_names = {
        "SWARM_ROLE_ID", "SWARMERZ_SCRATCH_DIR", "TMPDIR", "TMP", "TEMP",
        "DOTNET_CLI_HOME", "CARGO_TARGET_DIR",
    }
    common_env = {key: value for key, value in first.env.items() if key != "SWARM_ROLE_ID"}
    comparison_env = {
        key: value for key, value in first.env.items() if key not in role_local_names
    }
    for spec in native[1:]:
        if spec.argv[:spec.argv.index("run")] != first_executable:
            raise ConfigError(
                "native OpenCode hunters must use the same CLI executable/version "
                "to use the common isolated-service template"
            )
        other_env = {
            key: value for key, value in spec.env.items() if key not in role_local_names
        }
        if other_env != comparison_env:
            raise ConfigError(
                "native OpenCode hunters have different server environments; "
                "the isolated-service template cannot preserve separate credential allowlists"
            )
    ollama_hunters = [
        engine for engine, _spec, _artifact_dir in prepared
        if engine.harness == "opencode" and not engine.command
        and engine.provider == "ollama"
    ]
    if ollama_hunters:
        endpoints = {engine.ollama_base_url for engine in ollama_hunters}
        if len(endpoints) != 1:
            raise ConfigError(
                "native Ollama hunters in one round must use "
                "the same ollama_base_url"
            )
        try:
            inline_config = json.loads(common_env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        except json.JSONDecodeError as exc:
            raise ConfigError("OPENCODE_CONFIG_CONTENT is not valid JSON") from exc
        if not isinstance(inline_config, dict):
            raise ConfigError("OPENCODE_CONFIG_CONTENT must be a JSON object")
        providers = inline_config.setdefault("providers", {})
        if not isinstance(providers, dict):
            raise ConfigError("OPENCODE_CONFIG_CONTENT.providers must be an object")
        ollama = providers.setdefault("ollama", {})
        if not isinstance(ollama, dict):
            raise ConfigError("OPENCODE_CONFIG_CONTENT.providers.ollama must be an object")
        settings = ollama.setdefault("settings", {})
        models = ollama.setdefault("models", {})
        if not isinstance(settings, dict) or not isinstance(models, dict):
            raise ConfigError("Ollama settings and models must be JSON objects")
        settings["baseURL"] = next(iter(endpoints))
        for engine in ollama_hunters:
            model_name = engine.effective_model.removeprefix("ollama/")
            models.setdefault(model_name, {
                "name": model_name,
                "capabilities": {
                    "tools": True,
                    "input": ["text"],
                    "output": ["text"],
                },
            })
        common_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline_config)
    if common_env.get("SWARMERZ_SCRATCH_DIR"):
        original_scratch = Path(common_env["SWARMERZ_SCRATCH_DIR"])
        service_scratch = original_scratch.parent / "opencode-service"
        for name in (
            "SWARMERZ_SCRATCH_DIR", "TMPDIR", "TMP", "TEMP",
            "DOTNET_CLI_HOME", "CARGO_TARGET_DIR",
        ):
            if name not in common_env:
                continue
            value = Path(common_env[name])
            common_env[name] = str(
                service_scratch / value.relative_to(original_scratch)
                if value != original_scratch
                else service_scratch
            )
    return CommandSpec(
        first.argv,
        first.cwd,
        {**common_env, "SWARM_ROLE_ID": "opencode-service"},
        first.artifact_path,
    )


def _per_hunter_opencode_service_spec(
    engine: EngineConfig,
    spec: CommandSpec,
) -> CommandSpec:
    """Give one native OpenCode hunter a private service state and HOME.

    This prevents accidental session/database sharing between hunters. It does
    not claim same-user or hostile-process isolation; callers that require that
    boundary must place each hunter in a separate OS sandbox or container.
    """
    if engine.harness != "opencode" or engine.command:
        raise ConfigError("per-hunter OpenCode service requires a native OpenCode hunter")
    env = dict(spec.env)
    scratch_value = env.get("SWARMERZ_SCRATCH_DIR")
    if not scratch_value:
        raise ConfigError(f"hunter {engine.id} has no managed scratch directory")
    private_root = Path(scratch_value) / "opencode-isolation"
    private_home = private_root / "home"
    private_data = private_root / "data"
    private_state = private_root / "state"
    private_config = private_root / "config"
    for path in (private_home, private_data, private_state, private_config):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    env.update(
        {
            "HOME": str(private_home),
            "XDG_DATA_HOME": str(private_data),
            "XDG_STATE_HOME": str(private_state),
            "XDG_CONFIG_HOME": str(private_config),
            "SWARM_ROLE_ID": engine.id,
        }
    )
    env.pop("OPENCODE_PASSWORD", None)
    env.pop("OPENCODE_SERVER_PASSWORD", None)
    if engine.provider == "ollama":
        try:
            inline_config = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        except json.JSONDecodeError as exc:
            raise ConfigError("OPENCODE_CONFIG_CONTENT is not valid JSON") from exc
        if not isinstance(inline_config, dict):
            raise ConfigError("OPENCODE_CONFIG_CONTENT must be a JSON object")
        providers = inline_config.setdefault("providers", {})
        if not isinstance(providers, dict):
            raise ConfigError("OPENCODE_CONFIG_CONTENT.providers must be an object")
        ollama = providers.setdefault("ollama", {})
        if not isinstance(ollama, dict):
            raise ConfigError("Ollama provider configuration must be an object")
        settings = ollama.setdefault("settings", {})
        models = ollama.setdefault("models", {})
        if not isinstance(settings, dict) or not isinstance(models, dict):
            raise ConfigError("Ollama settings and models must be JSON objects")
        settings["baseURL"] = engine.ollama_base_url
        model_name = engine.effective_model.removeprefix("ollama/")
        models.setdefault(
            model_name,
            {
                "name": model_name,
                "capabilities": {
                    "tools": True,
                    "input": ["text"],
                    "output": ["text"],
                },
            },
        )
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline_config)
    return CommandSpec(spec.argv, spec.cwd, env, spec.artifact_path)


def _redacted_argv(argv: tuple[str, ...], instruction: str) -> list[str]:
    digest = hashlib.sha256(instruction.encode()).hexdigest()
    return [
        f"<instruction sha256={digest}>" if instruction in token or len(token) > 2_000 else token
        for token in argv
    ]


def _diagnostic_tail(path: Path) -> str | None:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return None
    if not lines:
        return None
    for line in reversed(lines):
        if line.lower().startswith("error:"):
            return line[:1_500]
    return " | ".join(lines[-4:])[-1_500:]


def _json_error_diagnostic(path: Path) -> str | None:
    """Extract a useful error from a JSON/JSONL harness output stream."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result" and event.get("is_error"):
            error = event.get("result") or event.get("error") or event.get("subtype")
            return str(error or "Claude Code returned an error result")[-1_500:]
        if event.get("type") not in {"error", "turn.failed"}:
            continue
        error = event.get("error") or event.get("message")
        if isinstance(error, str):
            try:
                nested = json.loads(error)
                if isinstance(nested, dict):
                    error = nested.get("error") or nested
                    if isinstance(error, dict) and "status" not in error and "status" in nested:
                        error = {**error, "status": nested["status"]}
            except json.JSONDecodeError:
                pass
        if isinstance(error, dict):
            kind = error.get("type")
            message = error.get("message")
            status = error.get("status")
            detail = ": ".join(str(item) for item in (kind, message) if item)
            if status is not None:
                detail = f"{detail} (status {status})" if detail else f"status {status}"
            return detail or "harness returned an error event"
        if error:
            return str(error)[-1_500:]
    return None


def _codex_thread_id(path: Path) -> str | None:
    """Return the last persisted Codex thread in a JSONL execution log."""
    thread_id = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "thread.started":
                    candidate = event.get("thread_id")
                    if isinstance(candidate, str) and candidate:
                        thread_id = candidate
    except OSError:
        pass
    return thread_id


_CODEX_TRANSIENT_TRANSPORT_MARKERS = (
    "could not resolve host",
    "dns error",
    "error sending request for url",
    "failed to connect to websocket",
    "failed to lookup address information",
    "network is unreachable",
    "temporary failure in name resolution",
    "connection reset by peer",
)


_OPENCODE_RECOVERABLE_PROVIDER_MARKERS = (
    "provider.invalid-output",
    "tool call delta is missing id or name",
)

_OPENCODE_RECOVERABLE_SERVER_MARKERS = (
    "could not reach server at http://127.0.0.1:",
    "clienterror: transport",
    "connection reset by peer",
    "connection refused",
)


def _tail_text(path: Path, limit: int = 65_536) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _is_transient_codex_transport_failure(
    result: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
) -> bool:
    """Retry only failed Codex calls with an explicit transient network signature."""
    if result.get("status") not in {"failed", "failed_to_start"}:
        return False
    diagnostic = " ".join(
        (
            _tail_text(stdout_path),
            _tail_text(stderr_path),
            str(result.get("error") or ""),
        )
    ).lower()
    return any(marker in diagnostic for marker in _CODEX_TRANSIENT_TRANSPORT_MARKERS)


def _is_recoverable_opencode_provider_failure(
    result: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
) -> bool:
    """Recognize malformed successful provider streams that are safe to resume.

    This is deliberately used only by the post-worker consolidation turn. The
    initial parent turn must not be retried because doing so could launch a
    duplicate worker swarm.
    """
    if result.get("status") not in {"failed", "failed_to_start"}:
        return False
    diagnostic = " ".join(
        str(item)
        for item in (
            _json_error_diagnostic(stdout_path),
            _diagnostic_tail(stderr_path),
            result.get("error"),
        )
        if item
    ).lower()
    return any(
        marker in diagnostic
        for marker in (
            *_OPENCODE_RECOVERABLE_PROVIDER_MARKERS,
            *_OPENCODE_RECOVERABLE_SERVER_MARKERS,
        )
    )


def _is_opencode_server_transport_failure(
    result: dict[str, Any], stdout_path: Path, stderr_path: Path,
) -> bool:
    if result.get("status") not in {"failed", "failed_to_start"}:
        return False
    diagnostic = " ".join(
        str(item)
        for item in (
            _json_error_diagnostic(stdout_path),
            _diagnostic_tail(stderr_path),
            result.get("error"),
        )
        if item
    ).lower()
    return any(marker in diagnostic for marker in _OPENCODE_RECOVERABLE_SERVER_MARKERS)


def _opencode_session_id(path: Path) -> str | None:
    """Return the parent OpenCode session ID from a JSONL output stream."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            session_id = event.get("sessionID")
            if isinstance(session_id, str) and session_id.strip():
                return session_id
    return None


def _opencode_resume_spec(spec: CommandSpec, session_id: str, prompt: str) -> CommandSpec:
    """Resume the exact OpenCode parent session with a replacement instruction."""
    argv = list(spec.argv)
    try:
        run_index = argv.index("run")
    except ValueError as exc:
        raise ConfigError("cannot resume OpenCode command without a run subcommand") from exc
    if not argv or run_index == len(argv) - 1:
        raise ConfigError("cannot resume malformed OpenCode command")
    # build_hunter_command always places the instruction last. Keep the original
    # provider/model/options, select the existing session, and replace only that
    # instruction.
    argv = argv[:-1]
    argv[run_index + 1 : run_index + 1] = ["--session", session_id]
    argv.append(prompt)
    return CommandSpec(
        argv=tuple(argv),
        cwd=spec.cwd,
        env=spec.env,
        artifact_path=spec.artifact_path,
    )


def _excerpt(value: Any, limit: int = 420) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _event_error(event: dict[str, Any]) -> str | None:
    error = event.get("error")
    if isinstance(error, dict):
        kind = error.get("type")
        message = error.get("message")
        status = error.get("status")
        detail = ": ".join(str(item) for item in (kind, message) if item)
        if status is not None:
            detail = f"{detail} (status {status})" if detail else f"status {status}"
        return detail or _excerpt(error)
    return _excerpt(error) if error else None


def _live_event_summary(harness: str, stream: str, line: str) -> str | None:
    """Render useful live JSONL events without dumping prompts or hidden reasoning."""
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        if stream == "stderr" and re.search(r"\b(error|warning|failed|denied)\b", line, re.I):
            return _excerpt(line)
        return None
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type", ""))
    if event_type in {"error", "turn.failed"}:
        error = event.get("error")
        if (
            harness == "opencode"
            and isinstance(error, dict)
            and error.get("type") == "provider.invalid-output"
        ):
            return (
                "RECOVERABLE provider stream fault: "
                f"{_event_error(event) or _excerpt(event)}; "
                "child workers remain monitored"
            )
        return f"ERROR: {_event_error(event) or _excerpt(event)}"

    if harness == "claude-code":
        if event_type == "system" and event.get("subtype") == "init":
            return "session initialized"
        if event_type == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    prefix = "worker message" if event.get("parent_tool_use_id") else "message"
                    return f"{prefix}: {_excerpt(block['text'])}"
                if block.get("type") == "tool_use":
                    tool = str(block.get("name") or "tool")
                    inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
                    detail = (
                        inputs.get("description")
                        or inputs.get("command")
                        or inputs.get("pattern")
                        or inputs.get("file_path")
                    )
                    suffix = f": {_excerpt(detail)}" if detail else ""
                    return f"tool {tool} (started){suffix}"
        if event_type == "result":
            if event.get("is_error"):
                detail = event.get("result") or event.get("error") or event.get("subtype")
                return f"ERROR: {_excerpt(detail or event)}"
            usage = event.get("usage")
            return f"turn completed; usage={_excerpt(usage)}" if usage else "turn completed"
        return None

    if harness == "opencode":
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event_type == "text" and part.get("text"):
            return f"message: {_excerpt(part['text'])}"
        if event_type == "tool_use":
            tool = part.get("tool") or part.get("name") or "tool"
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = state.get("status") or "active"
            inputs = state.get("input")
            if isinstance(inputs, dict):
                detail = (
                    inputs.get("description")
                    or inputs.get("command")
                    or inputs.get("query")
                    or inputs.get("path")
                )
            else:
                detail = inputs
            suffix = f": {_excerpt(detail)}" if detail else ""
            return f"tool {tool} ({status}){suffix}"
        if event_type in {"step_finish", "step-finish"}:
            reason = part.get("reason") or event.get("reason")
            return f"step finished{': ' + _excerpt(reason) if reason else ''}"
        return None

    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = str(item.get("type", ""))
    if event_type in {"item.started", "item.updated", "item.completed"}:
        if item_type == "agent_message" and item.get("text"):
            return f"message: {_excerpt(item['text'])}"
        if item_type == "command_execution":
            command = item.get("command") or item.get("cmd")
            status = item.get("status") or event_type.removeprefix("item.")
            return f"command ({status}): {_excerpt(command)}" if command else f"command ({status})"
        if item_type in {"mcp_tool_call", "tool_call"}:
            tool = item.get("tool") or item.get("name") or item.get("server") or "tool"
            status = item.get("status") or event_type.removeprefix("item.")
            return f"tool {tool} ({status})"
        if item_type == "web_search":
            query = item.get("query") or item.get("text") or "search"
            return f"web search: {_excerpt(query)}"
        if item_type in {"plan", "plan_update"}:
            plan = item.get("text") or item.get("plan") or item
            return f"plan: {_excerpt(plan)}"
    if event_type == "turn.completed":
        usage = event.get("usage")
        return f"turn completed; usage={_excerpt(usage)}" if usage else "turn completed"
    return None



class AuditRunner(SessionLifecycleMixin, HunterExecutionMixin, AdjudicationMixin):
    _VALIDATION_METHODS = _VALIDATION_METHODS
    _valid_evidence_contract = staticmethod(_valid_evidence_contract)
    _hunter_progress_name = staticmethod(_hunter_progress_name)
    _redacted_argv = staticmethod(_redacted_argv)
    _diagnostic_tail = staticmethod(_diagnostic_tail)
    _json_error_diagnostic = staticmethod(_json_error_diagnostic)
    _codex_thread_id = staticmethod(_codex_thread_id)
    _shared_opencode_service_spec = staticmethod(_shared_opencode_service_spec)
    _per_hunter_opencode_service_spec = staticmethod(_per_hunter_opencode_service_spec)
    _opencode_session_id = staticmethod(_opencode_session_id)
    _opencode_resume_spec = staticmethod(_opencode_resume_spec)
    _settled_partial_answer = staticmethod(_settled_partial_answer)
    _answer_postdates_workers = staticmethod(_answer_postdates_workers)
    _opencode_finalize_instruction = staticmethod(_opencode_finalize_instruction)

    def _probe_ollama_model(self, engine: EngineConfig) -> str:
        return probe_ollama_model(engine)

    def _check_chatgpt_codex_models(
        self, engines: tuple[EngineConfig, ...]
    ) -> list[str]:
        return check_chatgpt_codex_models(engines)

    def _build_probe_command(self, *args: Any, **kwargs: Any) -> CommandSpec | None:
        return build_probe_command(*args, **kwargs)

    def _build_auth_probe_command(self, *args: Any, **kwargs: Any) -> CommandSpec:
        return build_auth_probe_command(*args, **kwargs)

    async def _execute_command(self, spec: CommandSpec, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("resource_limiter", self._resource_limiter)
        return await execute(spec, **kwargs)

    def _activate_resource_limits(
        self, *, manifest_key: str = "resource_limits"
    ) -> None:
        status = self._resource_limiter.activate()
        if self.manifest:
            self._update_manifest(**{manifest_key: status})
        if status["enforced"]:
            memory_gib = int(status["memory_max_bytes"]) / 1024**3
            swap_gib = int(status["memory_swap_max_bytes"]) / 1024**3
            self._emit(
                "[resources] Linux user scopes enabled: "
                f"{memory_gib:g} GiB memory, {swap_gib:g} GiB swap, "
                f"{status['tasks_max']} tasks per harness scope"
            )
        elif status["mode"] == "off":
            self._emit("[resources] operating-system workload limits disabled by configuration")
        else:
            self._emit(
                "[resources] WARNING: operating-system workload limits unavailable; "
                f"using process-group cleanup only ({status['detail']})"
            )

    @staticmethod
    def _interrupt_worker_session() -> Callable[..., Any]:
        return interrupt_worker_session

    def __init__(
        self,
        config: AppConfig,
        instruction: str,
        *,
        context_override: str | Path | None = None,
        session_id: str | None = None,
        round_id: str | None = None,
        progress: Callable[[str], None] | None = None,
        hunter_report: Callable[[str, str], None] | None = None,
        interactive: bool = False,
    ) -> None:
        self.config = config
        self.instruction = instruction
        self.session_id = session_id or _new_id("session")
        self.requested_round_id = round_id
        for label, value in (("session id", self.session_id), ("round id", round_id)):
            if value is not None and not _SAFE_ID.fullmatch(value):
                raise ConfigError(f"{label} must match {_SAFE_ID.pattern!r}")
        self.context_source = resolve_context(config, context_override)
        self.frozen_context_source = self.context_source or _existing_context_source(
            config, self.session_id
        )
        self.agent_instruction = instruction_for_workspace(
            instruction, self.frozen_context_source
        )
        self.session_dir = config.output_root / "sessions" / self.session_id
        self.session_path = self.session_dir / "session.json"
        self.context_snapshot = self.session_dir / "input" / "context"
        self.lock_dir = self.session_dir / ".running"
        self._session_lock = SessionLock(self.session_dir, self.session_id)
        self.round_id = ""
        self.round_number = 0
        self.run_dir = self.session_dir / "rounds" / "uninitialized"
        self.manifest: dict[str, Any] = {}
        self.session: dict[str, Any] = {}
        self._storage = StorageManager(config.storage) if hasattr(config, "storage") else None
        self._storage_active = False
        self._adjudication_workspaces: list[tuple[Path, Path, str]] = []
        self._resource_limiter = ResourceLimiter(
            getattr(config, "resources", ResourceLimitsConfig(mode="off"))
        )
        self.progress = progress
        self.hunter_report = hunter_report
        self.live_control = LiveControl({
            engine.id: _hunter_progress_name(engine) for engine in config.hunters
        }) if interactive else None
        self._opencode_start_events: dict[str, asyncio.Event] = {}
        # The OpenCode service is shared by every native hunter in the round.
        # Limit concurrent nonblocking loopback probes so monitoring cannot
        # stampede the shared server.
        self._opencode_api_slots = asyncio.Semaphore(2)

    def _emit(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    async def _opencode_api_call(
        self, function: Callable[..., Any], *args: Any, **kwargs: Any,
    ) -> Any:
        async with self._opencode_api_slots:
            return await call_opencode_api(function, *args, **kwargs)

    async def _wait_for_opencode_server(
        self, endpoint: OpenCodeEndpoint | None, *, timeout_seconds: float = 20,
    ) -> bool:
        if endpoint is None:
            await asyncio.sleep(min(timeout_seconds, 2))
            return False
        deadline = time.monotonic() + timeout_seconds
        while True:
            async with self._opencode_api_slots:
                ready = await _async_server_ready(endpoint)
            if ready:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    def _live_output(self, role: str, harness: str) -> Callable[[str, str], None]:
        def emit_event(stream: str, line: str) -> None:
            if harness == "opencode" and stream == "stdout":
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if isinstance(event, dict) and isinstance(event.get("sessionID"), str):
                    hunter_id = role.split("/", 1)[0]
                    if self.live_control is not None:
                        self.live_control.set_opencode_session(hunter_id, event["sessionID"])
                    started = self._opencode_start_events.get(hunter_id)
                    if started is not None:
                        started.set()
            summary = _live_event_summary(harness, stream, line)
            if summary:
                hunter_id, separator, suffix = role.partition("/")
                engine = next(
                    (item for item in self.config.hunters if item.id == hunter_id), None
                )
                display_role = (
                    _hunter_progress_name(engine) + (separator + suffix if separator else "")
                    if engine is not None else role
                )
                self._emit(f"[{display_role}/live] {summary}")

        return emit_event

    async def _execute_with_transport_retries(
        self,
        engine: EngineConfig,
        spec: CommandSpec,
        *,
        role: str,
        stdout_path: Path,
        stderr_path: Path,
    ) -> dict[str, Any]:
        is_native_codex = engine.harness in {"codex", "codex-exec"} and not engine.command
        native_claude = is_native_claude(engine)
        claude_hunter = bool(
            self.live_control is not None
            and native_claude
            and role == engine.id
            and role in {hunter.id for hunter in self.config.hunters}
        )
        if claude_hunter:
            self.live_control.register_claude(role)

        def claude_hunter_artifact_ready(candidate: dict[str, Any]) -> bool:
            if not candidate.get("claude_final_output_ready"):
                return False
            if not spec.artifact_path or not spec.artifact_path.is_file():
                return False
            try:
                if not isinstance(read_json(spec.artifact_path), dict):
                    return False
            except (ConfigError, OSError):
                return False
            fanout = claude_fanout_evidence(stdout_path, engine.swarm_size)
            return bool(
                fanout.get("verified")
                and fanout["snapshot"].get("all_workers_terminal")
            )
        if (
            self.live_control is not None and is_native_codex
            and role == engine.id
            and role in {hunter.id for hunter in self.config.hunters}
        ):
            started = time.monotonic()
            client = CodexAppServer()
            display_role = _hunter_progress_name(engine)
            try:
                longest_peer_runtime = max(
                    (
                        peer.timeout_seconds
                        + (
                            (
                                peer.worker_timeout_seconds
                                or self.config.opencode_worker_timeout_seconds
                            )
                            + self.config.opencode_finalize_timeout_seconds
                            if peer.harness == "opencode" and not peer.command
                            else 0
                        )
                    )
                    for peer in self.config.hunters
                )
                await client.start(
                    spec, engine, stdout_path=stdout_path, stderr_path=stderr_path,
                    emit=lambda message: self._emit(f"[{display_role}/live] {message}"),
                    resource_limiter=self._resource_limiter,
                    runtime_seconds=(
                        longest_peer_runtime + engine.timeout_seconds + 300
                    ),
                )
                self.live_control.register_codex(role, client)
                result = await client.run(spec, engine)
                initial_failure = dict(result) if result.get("status") != "completed" else None
                recovery_attempts: list[dict[str, Any]] = []
                while (
                    result.get("status") != "completed"
                    and not result.get("answer_contract_valid")
                    and client.thread_id is not None
                    and (
                        self.live_control.recovery_window_open(role)
                        or self.live_control.has_pending_recovery(role)
                    )
                ):
                    self.live_control.mark_recoverable(role)
                    detail = str(result.get("error") or result.get("status") or "failed")
                    self._emit(
                        f"[{display_role}] Codex turn failed; recovery available while other "
                        f"hunters run. Use /to {role} MESSAGE to resume the same thread. "
                        f"Detail: {detail[:240]}"
                    )
                    followup: str | None = None
                    while (
                        self.live_control.recovery_window_open(role)
                        or self.live_control.has_pending_recovery(role)
                    ):
                        followup = await self.live_control.next_recovery(role)
                        if followup is not None:
                            break
                    if followup is None:
                        break
                    self.live_control.mark_resuming(role)
                    self._emit(
                        f"[{display_role}] resuming recoverable Codex thread from targeted "
                        "operator instruction"
                    )
                    attempt_started = time.monotonic()
                    try:
                        result = await client.run(spec, engine, prompt=followup)
                    except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
                        result = {
                            "status": (
                                "timed_out" if isinstance(exc, TimeoutError) else "failed"
                            ),
                            "exit_code": None,
                            "timed_out": isinstance(exc, TimeoutError),
                            "error": str(exc),
                        }
                    result["duration_seconds"] = round(
                        time.monotonic() - attempt_started, 3
                    )
                    recovery_attempts.append({
                        "instruction_sha256": hashlib.sha256(
                            followup.encode("utf-8")
                        ).hexdigest(),
                        **result,
                    })
                if recovery_attempts:
                    result = dict(result)
                    result["initial_failure"] = initial_failure
                    result["recovery_attempts"] = recovery_attempts
                    result["recovery_count"] = len(recovery_attempts)
                    result["resumed_after_failure"] = bool(
                        result.get("status") == "completed"
                        or result.get("answer_contract_valid")
                    )
                result["duration_seconds"] = round(time.monotonic() - started, 3)
                result["codex_thread_id"] = client.thread_id
                return result
            except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
                return {
                    "status": "timed_out" if isinstance(exc, TimeoutError) else "failed",
                    "exit_code": None,
                    "timed_out": isinstance(exc, TimeoutError),
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error": str(exc),
                }
            finally:
                self.live_control.finished(role)
                await client.close()
        retry_limit = self.config.codex_transport_retries if is_native_codex else 0
        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {}

        for attempt in range(retry_limit + 1):
            result = await self._execute_command(
                spec,
                timeout_seconds=engine.timeout_seconds,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                on_output=self._live_output(role, engine.harness),
                append=attempt > 0,
            )
            claude_inspection: dict[str, Any] = {}
            if native_claude:
                claude_inspection = materialize_claude_output(
                    stdout_path, spec.artifact_path
                )
                session_id = claude_inspection.get("session_id")
                if isinstance(session_id, str) and session_id:
                    result["claude_session_id"] = session_id
                if claude_inspection.get("result_error") and not result.get("error"):
                    result["error"] = str(claude_inspection["result_error"])
                result["claude_final_output_ready"] = bool(
                    result.get("status") == "completed"
                    and claude_inspection.get("structured_output_ready")
                )
            diagnostic = _diagnostic_tail(stderr_path) or _json_error_diagnostic(stdout_path)
            attempts.append({**result, "diagnostic": diagnostic})

            artifact_exists = bool(
                spec.artifact_path
                and spec.artifact_path.is_file()
                and spec.artifact_path.stat().st_size > 0
            )
            transient = _is_transient_codex_transport_failure(result, stdout_path, stderr_path)
            if artifact_exists or not transient or attempt >= retry_limit:
                break

            retry_number = attempt + 1
            delay = self.config.codex_retry_delay_seconds
            self._emit(
                f"[{role}] transient Codex transport failure; retry "
                f"{retry_number}/{retry_limit} in {delay}s"
            )
            if delay:
                await asyncio.sleep(delay)

        if claude_hunter:
            session_id = result.get("claude_session_id")
            artifact_ready = claude_hunter_artifact_ready(result)
            initial_failure = dict(result) if not artifact_ready else None
            recovery_attempts: list[dict[str, Any]] = []
            while (
                not artifact_ready
                and isinstance(session_id, str)
                and session_id
                and (
                    self.live_control.recovery_window_open(role)
                    or self.live_control.has_pending_recovery(role)
                )
            ):
                self.live_control.mark_recoverable(role)
                detail = str(
                    result.get("error")
                    or result.get("status")
                    or "no valid structured answer"
                )
                self._emit(
                    f"[{_hunter_progress_name(engine)}] Claude Code turn failed; recovery "
                    f"available while other hunters run. Use /to {role} MESSAGE to resume "
                    f"the same session. Detail: {detail[:240]}"
                )
                followup: str | None = None
                while (
                    self.live_control.recovery_window_open(role)
                    or self.live_control.has_pending_recovery(role)
                ):
                    followup = await self.live_control.next_recovery(role)
                    if followup is not None:
                        break
                if followup is None:
                    break
                self.live_control.mark_resuming(role)
                self._emit(
                    f"[{_hunter_progress_name(engine)}] resuming recoverable Claude Code "
                    "session from targeted operator instruction"
                )
                recovery_spec = _claude_resume_spec(spec, session_id, followup)
                recovery_started = time.monotonic()
                recovery_result = await self._execute_command(
                    recovery_spec,
                    timeout_seconds=engine.timeout_seconds,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    on_output=self._live_output(role, engine.harness),
                    append=True,
                )
                inspection = materialize_claude_output(stdout_path, spec.artifact_path)
                resumed_session = inspection.get("session_id")
                if isinstance(resumed_session, str) and resumed_session:
                    session_id = resumed_session
                    recovery_result["claude_session_id"] = session_id
                if inspection.get("result_error"):
                    recovery_result["error"] = str(inspection["result_error"])
                recovery_result["claude_final_output_ready"] = bool(
                    recovery_result.get("status") == "completed"
                    and inspection.get("structured_output_ready")
                )
                recovery_result["duration_seconds"] = round(
                    time.monotonic() - recovery_started, 3
                )
                recovery_attempts.append({
                    "instruction_sha256": hashlib.sha256(
                        followup.encode("utf-8")
                    ).hexdigest(),
                    **recovery_result,
                })
                result = recovery_result
                artifact_ready = claude_hunter_artifact_ready(result)
            if recovery_attempts:
                result = dict(result)
                result["initial_failure"] = initial_failure
                result["recovery_attempts"] = recovery_attempts
                result["recovery_count"] = len(recovery_attempts)
                result["resumed_after_failure"] = artifact_ready
                result["duration_seconds"] = round(
                    float((initial_failure or {}).get("duration_seconds") or 0)
                    + sum(
                        float(item.get("duration_seconds") or 0)
                        for item in recovery_attempts
                    ),
                    3,
                )
            self.live_control.finished(role)

        result = dict(result)
        result["retry_count"] = len(attempts) - 1
        if len(attempts) > 1:
            result["attempts"] = attempts
            result["duration_seconds"] = round(
                sum(float(item.get("duration_seconds") or 0) for item in attempts),
                3,
            )
        return result

    async def _monitor_opencode_workers(
        self,
        engine: EngineConfig,
        *,
        parent_session_id: str,
        artifact_dir: Path,
        endpoint: OpenCodeEndpoint | None = None,
        observation: dict[str, Any] | None = None,
        observation_ready: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """Poll OpenCode's local session API until direct child sessions terminate."""
        started = time.monotonic()
        timeout = int(
            getattr(engine, "worker_timeout_seconds", None)
            or self.config.opencode_worker_timeout_seconds
        )
        poll_seconds = self.config.opencode_worker_poll_seconds
        orphan_quiet_seconds = float(
            getattr(self.config, "opencode_orphan_quiet_seconds", 600)
        )
        consecutive_errors = 0
        query_count = 0
        last_signature: tuple[Any, ...] | None = None
        next_notice = 0.0
        last_reported_output_tokens = 0
        snapshot: dict[str, Any] = {
            "parent_session_id": parent_session_id,
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
        reason = "worker_timeout"
        errors: list[str] = []
        permission_errors: list[str] = []
        consecutive_permission_errors = 0
        prior_pending_ids: set[str] = set()
        pending_permissions: list[dict[str, object]] = []
        rejected_permissions: list[dict[str, object]] = []
        rejected_ids: set[str] = set()
        rejected_by_child: dict[str, int] = {}
        answer_path = artifact_dir.parent.parent / "workspaces" / "hunters" / engine.id / engine.answer_path
        answer_signature: tuple[int, int] | None = None
        answer_quiet_since: float | None = None
        last_observed_tokens: int | None = None
        interrupted_sessions: list[str] = []
        interrupt_errors: list[str] = []
        child_activity: dict[str, tuple[tuple[int, int], float]] = {}
        recovery_attempts: dict[str, float] = {}
        recovered_sessions: list[str] = []
        orphaned_sessions: list[str] = []
        active_status_errors: list[str] = []

        while True:
            query_count += 1
            stdout_path = artifact_dir / "worker-status.stdout.json"
            stderr_path = artifact_dir / "worker-status.stderr.log"
            status_fresh = False
            try:
                payload = await self._opencode_api_call(
                    list_worker_sessions,
                    parent_session_id,
                    limit=engine.swarm_size + 5,
                    endpoint=endpoint,
                )
                snapshot = _opencode_worker_snapshot(
                    payload,
                    parent_session_id=parent_session_id,
                    expected_workers=engine.swarm_size,
                )
                write_json(stdout_path, payload)
                consecutive_errors = 0
                status_fresh = True
                write_json(artifact_dir / "worker-status.json", snapshot)
                if observation is not None:
                    observation["snapshot"] = snapshot
                    observation["query_count"] = query_count
                if observation_ready is not None:
                    observation_ready.set()
            except (ConfigError, OSError) as exc:
                consecutive_errors += 1
                detail = str(exc)
                with stderr_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"attempt {query_count}: {detail}\n")
                errors.append(detail)
                timeout_error = "timed out" in detail.lower()
                failure_limit = 12 if timeout_error else 3
                if consecutive_errors == 3 and timeout_error:
                    self._emit(
                        f"[{engine.id}] OpenCode status is slow; backing off without "
                        "interrupting live work"
                    )
                if consecutive_errors >= failure_limit:
                    reason = "status_unavailable"
                    self._emit(
                        f"[{engine.id}] OpenCode worker status unavailable after "
                        f"{consecutive_errors} attempts; preserving work without a paid retry"
                    )
                    break
                delay = (
                    min(float(poll_seconds) * consecutive_errors, 60.0)
                    if timeout_error else min(float(poll_seconds), 5.0)
                )
                await asyncio.sleep(delay)
                continue

            child_ids = [
                str(item["id"])
                for item in snapshot.get("children", [])
                if isinstance(item, dict) and item.get("id") and not item.get("outcome")
            ]
            pending_permissions = []
            permission_probe_ok = not child_ids
            if child_ids:
                try:
                    pending_permissions = await self._opencode_api_call(
                        pending_worker_permissions, child_ids, endpoint=endpoint
                    )
                    consecutive_permission_errors = 0
                    permission_probe_ok = True
                    snapshot["pending_permissions"] = pending_permissions
                    write_json(artifact_dir / "worker-status.json", snapshot)
                    current_ids = {
                        str(item.get("id") or "")
                        or f"{item.get('session_id')}:{item.get('action')}:{item.get('resources')}"
                        for item in pending_permissions
                    }
                    unhandled_requests = [
                        item for item in pending_permissions
                        if str(item.get("id") or "") not in rejected_ids
                    ]
                    for item in unhandled_requests:
                        child_id = str(item.get("session_id") or "")
                        request_id = str(item.get("id") or "")
                        if rejected_by_child.get(child_id, 0) >= 3:
                            reason = "permission_required"
                            self._emit(
                                f"[{engine.id}] child {child_id} repeatedly requested "
                                "additional access; stopping after three rejections"
                            )
                            break
                        try:
                            rejection_applied = await self._opencode_api_call(
                                reject_worker_permission,
                                child_id,
                                request_id,
                                endpoint=endpoint,
                            )
                        except (ConfigError, OSError) as exc:
                            permission_errors.append(str(exc))
                            reason = "permission_required"
                            self._emit(
                                f"[{engine.id}] unable to reject child {child_id} "
                                f"permission request: {exc}"
                            )
                            break
                        rejected_ids.add(request_id)
                        if rejection_applied:
                            rejected_by_child[child_id] = rejected_by_child.get(child_id, 0) + 1
                            rejected_permissions.append(item)
                            self._emit(
                                f"[{engine.id}] rejected child {child_id} "
                                f"{item.get('action', 'unknown')} permission request: "
                                f"{_excerpt(item.get('resources', []), 240)}; "
                                "worker can continue without the resource"
                            )
                        else:
                            self._emit(
                                f"[{engine.id}] child {child_id} permission request "
                                f"{request_id} was already resolved; continuing"
                            )
                    if reason == "permission_required":
                        break
                    unresolved_ids = current_ids - rejected_ids
                    if unresolved_ids and unresolved_ids & prior_pending_ids:
                        reason = "permission_required"
                        blocked = sorted({
                            str(resource)
                            for item in pending_permissions
                            for resource in item.get("resources", [])
                        })
                        self._emit(
                            f"[{engine.id}] workers blocked by {len(pending_permissions)} "
                            f"pending OpenCode permission request(s): {_excerpt(blocked, 240)}"
                        )
                        break
                    if unresolved_ids and not prior_pending_ids:
                        self._emit(
                            f"[{engine.id}] {len(unresolved_ids)} child permission "
                            "request(s) pending; checking again before continuing"
                        )
                    prior_pending_ids = unresolved_ids
                except (ConfigError, OSError) as exc:
                    permission_errors.append(str(exc))
                    consecutive_permission_errors += 1
                    if consecutive_permission_errors >= 3:
                        reason = "status_unavailable"
                        self._emit(
                            f"[{engine.id}] OpenCode permission monitor unavailable after "
                            "three attempts; no permission request was observed; "
                            "preserving work without a paid retry"
                        )
                        break
                    await asyncio.sleep(min(float(poll_seconds), 5.0))
                    continue
            else:
                consecutive_permission_errors = 0

            now_monotonic = time.monotonic()
            nonterminal_children = [
                child for child in snapshot.get("children", [])
                if isinstance(child, dict) and not child.get("outcome") and child.get("id")
            ]
            current_nonterminal_ids: set[str] = set()
            quiet_candidates: list[dict[str, Any]] = []
            for child in nonterminal_children:
                child_id = str(child["id"])
                current_nonterminal_ids.add(child_id)
                activity_signature = (
                    int(child.get("output_tokens") or 0),
                    int(child.get("updated_ms") or 0),
                )
                prior_activity = child_activity.get(child_id)
                if prior_activity is None or prior_activity[0] != activity_signature:
                    child_activity[child_id] = (activity_signature, now_monotonic)
                elif now_monotonic - prior_activity[1] >= orphan_quiet_seconds:
                    quiet_candidates.append(child)
            for child_id in set(child_activity) - current_nonterminal_ids:
                child_activity.pop(child_id, None)
                recovery_attempts.pop(child_id, None)

            if (
                status_fresh
                and permission_probe_ok
                and not pending_permissions
                and int(snapshot.get("observed_workers") or 0) >= engine.swarm_size
                and quiet_candidates
            ):
                try:
                    active_ids = await self._opencode_api_call(
                        active_session_ids, endpoint=endpoint
                    )
                except (ConfigError, OSError) as exc:
                    active_status_errors.append(str(exc))
                    if len(active_status_errors) == 1:
                        self._emit(
                            f"[{engine.id}] active-session probe unavailable; "
                            "orphan detection deferred"
                        )
                else:
                    inactive = [
                        child for child in quiet_candidates
                        if str(child["id"]) not in active_ids
                    ]
                    for child in inactive:
                        child_id = str(child["id"])
                        if child_id not in recovery_attempts:
                            try:
                                await self._opencode_api_call(
                                    steer_session,
                                    child_id,
                                    "Your previous provider stream ended without a terminal outcome. "
                                    "Resume the assigned work from existing evidence, write the promised "
                                    "worker report, and finish now. Do not broaden scope or request "
                                    "additional permissions.",
                                    endpoint=endpoint,
                                )
                            except (ConfigError, OSError) as exc:
                                active_status_errors.append(
                                    f"recovery {child_id}: {exc}"
                                )
                                orphaned_sessions.append(child_id)
                            else:
                                recovery_attempts[child_id] = now_monotonic
                                child_activity[child_id] = (
                                    child_activity[child_id][0], now_monotonic
                                )
                                recovered_sessions.append(child_id)
                                self._emit(
                                    f"[{engine.id}] resuming inactive child {child_id} once "
                                    "after its output stopped changing"
                                )
                        elif now_monotonic - recovery_attempts[child_id] >= orphan_quiet_seconds:
                            orphaned_sessions.append(child_id)
                    if orphaned_sessions:
                        orphaned_sessions = sorted(set(orphaned_sessions))
                        reason = "orphaned_workers"
                        self._emit(
                            f"[{engine.id}] {len(orphaned_sessions)} child worker(s) remained "
                            "inactive after recovery; finalizing completed partial work"
                        )
                        break

            elapsed = time.monotonic() - started
            earliest_created_ms = int(snapshot.get("earliest_created_ms") or 0)
            if earliest_created_ms:
                worker_elapsed = max(0.0, (time.time() * 1_000 - earliest_created_ms) / 1_000)
            else:
                worker_elapsed = elapsed
            remaining = max(0.0, timeout - worker_elapsed)
            signature = (
                snapshot.get("observed_workers"),
                snapshot.get("terminal_workers"),
                snapshot.get("active_workers"),
                tuple(sorted(dict(snapshot.get("outcomes") or {}).items())),
                tuple(snapshot.get("active_titles") or ()),
            )
            if signature != last_signature or elapsed >= next_notice:
                outcomes = dict(snapshot.get("outcomes") or {})
                outcome_text = ", ".join(
                    f"{count} {name}" for name, count in sorted(outcomes.items())
                ) or "no terminal outcomes"
                active_titles = snapshot.get("active_titles") or []
                active_text = f"; nonterminal: {_excerpt(active_titles, 240)}" if active_titles else ""
                output_tokens = int(snapshot.get("output_tokens") or 0)
                token_delta = max(0, output_tokens - last_reported_output_tokens)
                self._emit(
                    f"[{engine.id}] workers: {snapshot.get('terminal_workers', 0)}/"
                    f"{engine.swarm_size} terminal ({outcome_text}); "
                    f"worker timeout in {int(remaining)}s; "
                    f"output tokens {output_tokens} (+{token_delta}){active_text}"
                )
                last_signature = signature
                last_reported_output_tokens = output_tokens
                next_notice = elapsed + 60

            if snapshot.get("all_workers_terminal"):
                reason = "all_workers_terminal"
                break
            output_tokens = int(snapshot.get("output_tokens") or 0)
            if last_observed_tokens is None or output_tokens != last_observed_tokens:
                answer_quiet_since = None
            last_observed_tokens = output_tokens
            if (
                status_fresh
                and permission_probe_ok
                and not pending_permissions
                and _settled_partial_answer(
                    answer_path, snapshot, minimum_workstreams=engine.swarm_size
                )
            ):
                try:
                    stat = answer_path.stat()
                except OSError:
                    answer_signature = None
                    answer_quiet_since = None
                else:
                    current_signature = (stat.st_mtime_ns, stat.st_size)
                    if current_signature != answer_signature or answer_quiet_since is None:
                        answer_quiet_since = time.monotonic()
                    answer_signature = current_signature
                    if time.monotonic() - answer_quiet_since >= self.config.opencode_answer_quiet_seconds:
                        reason = "partial_answer_ready"
                        self._emit(
                            f"[{engine.id}] valid answer written after {engine.swarm_size - 1}/"
                            f"{engine.swarm_size} workers succeeded; last child made no output for "
                            f"{self.config.opencode_answer_quiet_seconds}s; sealing partial answer"
                        )
                        break
            else:
                answer_signature = None
                answer_quiet_since = None
            if remaining <= 0:
                reason = "worker_timeout"
                self._emit(
                    f"[{engine.id}] worker timeout reached; finalizing with completed partial work"
                )
                break
            await asyncio.sleep(min(float(poll_seconds), remaining))

        if reason in {"partial_answer_ready", "worker_timeout", "orphaned_workers"}:
            unfinished = [
                child for child in snapshot.get("children", [])
                if isinstance(child, dict) and not child.get("outcome") and child.get("id")
            ]
            if unfinished:
                self._emit(
                    f"[{engine.id}] interrupting {len(unfinished)} unfinished child worker(s) "
                    f"after {reason.replace('_', ' ')}"
                )
            for child in unfinished:
                child_id = str(child["id"])
                try:
                    await self._opencode_api_call(
                        interrupt_worker_session, child_id, endpoint=endpoint
                    )
                    interrupted_sessions.append(child_id)
                except (ConfigError, OSError) as exc:
                    interrupt_errors.append(f"{child_id}: {exc}")
                    self._emit(
                        f"[{engine.id}] could not interrupt unfinished child {child_id}: {exc}"
                    )

        monitor = {
            "reason": reason,
            "wait_seconds": round(time.monotonic() - started, 3),
            "query_count": query_count,
            "status_errors": errors[-3:],
            "permission_status_errors": permission_errors[-3:],
            "pending_permissions": pending_permissions if reason == "permission_required" else [],
            "rejected_permissions": rejected_permissions,
            "interrupted_sessions": interrupted_sessions,
            "interrupt_errors": interrupt_errors,
            "recovered_sessions": recovered_sessions,
            "orphaned_sessions": orphaned_sessions,
            "active_status_errors": active_status_errors[-3:],
            "snapshot": snapshot,
        }
        write_json(artifact_dir / "worker-monitor.json", monitor)
        return monitor

    async def _execute_opencode_finalize(
        self,
        engine: EngineConfig,
        spec: CommandSpec,
        *,
        artifact_dir: Path,
        session_id: str,
        endpoint: OpenCodeEndpoint | None,
        log_stem: str = "finalize",
    ) -> dict[str, Any]:
        if spec.artifact_path is None:
            raise ConfigError("OpenCode finalization requires an answer artifact path")
        stdout_path = artifact_dir / f"{log_stem}.stdout.jsonl"
        stderr_path = artifact_dir / f"{log_stem}.stderr.log"
        provider_retry_limit = 1
        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {}

        for attempt in range(provider_retry_limit + 1):
            stop_event = asyncio.Event()
            run_task = asyncio.create_task(self._execute_command(
                spec,
                timeout_seconds=self.config.opencode_finalize_timeout_seconds,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                on_output=self._live_output(f"{engine.id}/finalize", engine.harness),
                append=attempt > 0,
                stop_event=stop_event,
            ))
            answer_task = asyncio.create_task(_wait_for_stable_final_answer(
                spec.artifact_path,
                minimum_workstreams=engine.swarm_size,
                quiet_seconds=self.config.opencode_finalize_answer_quiet_seconds,
            ))
            try:
                done, _ = await asyncio.wait(
                    {run_task, answer_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if run_task in done:
                    result = run_task.result()
                else:
                    answer_task.result()
                    self._emit(
                        f"[{engine.id}] final answer valid and unchanged for "
                        f"{self.config.opencode_finalize_answer_quiet_seconds}s; "
                        "stopping the consolidation turn"
                    )
                    try:
                        await self._opencode_api_call(
                            interrupt_worker_session, session_id, endpoint=endpoint
                        )
                    except (ConfigError, OSError) as exc:
                        self._emit(
                            f"[{engine.id}] could not interrupt the finalization session: {exc}; "
                            "keeping its bounded timeout"
                        )
                        result = await run_task
                    else:
                        stop_event.set()
                        result = await run_task
                        result["artifact_ready_stop"] = True
            finally:
                answer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await answer_task
                if not run_task.done():
                    run_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await run_task

            attempts.append({
                **result,
                "diagnostic": (
                    _json_error_diagnostic(stdout_path)
                    or _diagnostic_tail(stderr_path)
                    or result.get("error")
                ),
            })
            recoverable = _is_recoverable_opencode_provider_failure(
                result, stdout_path, stderr_path
            )
            if result.get("artifact_ready_stop") or not recoverable or attempt >= provider_retry_limit:
                break
            if _is_opencode_server_transport_failure(result, stdout_path, stderr_path):
                self._emit(
                    f"[{engine.id}] temporary OpenCode server transport failure during "
                    "consolidation; waiting for the same server and session"
                )
                ready = await self._wait_for_opencode_server(endpoint)
                if not ready:
                    self._emit(
                        f"[{engine.id}] OpenCode server health check remained slow; "
                        "retrying the same consolidation session once"
                    )
            else:
                self._emit(
                    f"[{engine.id}] malformed provider stream during consolidation; "
                    "resuming the same session once more"
                )

        result = dict(result)
        result["provider_retry_count"] = len(attempts) - 1
        if len(attempts) > 1:
            result["provider_attempts"] = attempts
            result["duration_seconds"] = round(
                sum(float(item.get("duration_seconds") or 0) for item in attempts),
                3,
            )
        return result

    async def run(self) -> Path:
        return await execute_round_protocol(
            self,
            opencode_round_slot=_opencode_round_slot,
            utc_now=_utc_now,
            adjudicator_disclosure=_adjudicator_disclosure,
        )

    def _adopt_late_hunter_answers(
        self, original_hunters: list[dict[str, Any]], results_by_id: dict[str, dict[str, Any]]
    ) -> None:
        """Seal only complete artifacts created after their hunter was marked failed."""
        adopted: list[str] = []
        for hunter in original_hunters:
            hunter_id = str(hunter["id"])
            result = results_by_id.get(hunter_id)
            if not result or result.get("answer_sealed") or not result.get("worker_contract_verified"):
                continue
            artifact_dir = self.run_dir / "hunters" / hunter_id
            result_path = artifact_dir / "result.json"
            workspace = self.run_dir / "workspaces" / "hunters" / hunter_id
            source = workspace / ".swarm" / "answer.json"
            if not result_path.is_file() or not source.is_file() or source.is_symlink():
                continue
            if not source.resolve().is_relative_to(workspace.resolve()):
                raise ConfigError(f"late answer escapes hunter workspace: {hunter_id}")
            source_stat = source.stat()
            if source_stat.st_mtime_ns <= result_path.stat().st_mtime_ns:
                continue
            if time.time_ns() - source_stat.st_mtime_ns < 30_000_000_000:
                raise ConfigError(f"late answer is still too recent to adopt: {hunter_id}")
            if (artifact_dir / "answer.json").exists() or (artifact_dir / "seal.json").exists():
                raise ConfigError(f"unsealed hunter already has artifact files: {hunter_id}")
            answer, seal = seal_answer(
                source, artifact_dir, minimum_workstreams=int(hunter["workers"])
            )
            if sha256_file(source) != seal["artifacts"]["answer.json"]:
                raise ConfigError(f"late answer changed while being sealed: {hunter_id}")
            original_failure = result.get("diagnostic") or result.get("artifact_error")
            result.update({
                "status": "completed",
                "answer_sealed": True,
                "seal": seal,
                "late_answer_adopted": {
                    "at": _utc_now(),
                    "source": str(source),
                    "source_mtime_ns": source_stat.st_mtime_ns,
                    "original_failure": original_failure,
                    "worker_outcomes_unverified": True,
                },
            })
            result.pop("artifact_error", None)
            result.pop("diagnostic", None)
            write_json(result_path, result)
            adopted.append(hunter_id)
            self._emit(
                f"[primary analysis] adopted late {hunter_id} answer "
                f"({len(answer['workstreams'])} workstreams); child outcomes remain unverified"
            )
        if adopted:
            sealed = [
                hunter_id for hunter_id, result in results_by_id.items()
                if result.get("answer_sealed")
            ]
            previous_partial = list(self.manifest.get("partial_hunters") or [])
            self._update_manifest(
                hunter_results=list(results_by_id.values()),
                sealed_answer_count=len(sealed),
                missing_hunters=[
                    hunter["id"] for hunter in original_hunters
                    if hunter["id"] not in sealed
                ],
                partial_hunters=sorted(set(previous_partial + adopted)),
                degraded=True,
            )

    async def analyze_existing(self, *, adopt_late_answers: bool = False) -> Path:
        """Retry only primary analysis for an existing round with sealed hunter answers."""
        if not self.requested_round_id:
            raise ConfigError("analyze requires --round-id")
        if not self.session_path.is_file():
            raise ConfigError(f"session does not exist: {self.session_id}")
        self.round_id = self.requested_round_id
        self.run_dir = self.session_dir / "rounds" / self.round_id
        if not self.run_dir.is_dir():
            raise ConfigError(f"round does not exist: {self.round_id}")
        self._acquire_lock()
        try:
            self.session = read_json(self.session_path)
            self.manifest = read_json(self.run_dir / "manifest.json")
            if self.manifest.get("session_id") != self.session_id or self.manifest.get("round_id") != self.round_id:
                raise ConfigError("round manifest does not match requested session and round")
            if self.manifest.get("status") == "completed":
                raise ConfigError("round already has a completed primary analysis")
            instruction_path = self.run_dir / "instruction.txt"
            original_instruction = instruction_path.read_text(encoding="utf-8").rstrip("\n")
            self.instruction = original_instruction
            self.agent_instruction = (
                instruction_for_workspace(original_instruction, self.frozen_context_source)
                if self.frozen_context_source is not None
                else original_instruction
            )
            self.round_number = int(self.manifest["round_number"])
            self._activate_managed_storage()
            self._activate_resource_limits(
                manifest_key="analysis_retry_resource_limits"
            )
            self._verify_context_snapshot("analysis-retry-start")

            results_by_id = {
                item["hunter_id"]: item
                for item in self.manifest.get("hunter_results", [])
                if isinstance(item, dict) and isinstance(item.get("hunter_id"), str)
            }
            original_hunters = self.manifest.get("hunters", [])
            if adopt_late_answers:
                self._adopt_late_hunter_answers(original_hunters, results_by_id)
            answers: list[dict[str, Any]] = []
            for hunter in original_hunters:
                hunter_id = hunter["id"]
                if not results_by_id.get(hunter_id, {}).get("answer_sealed"):
                    continue
                artifact_dir = self.run_dir / "hunters" / hunter_id
                answer_path = artifact_dir / "answer.json"
                seal = read_json(artifact_dir / "seal.json")
                digest = sha256_file(answer_path)
                if seal.get("artifacts", {}).get("answer.json") != digest:
                    raise ConfigError(f"sealed answer changed: {hunter_id}")
                answers.append({"hunter_id": hunter_id, "answer": read_json(answer_path), "sha256": digest})

            answers = randomized_answer_order(answers)

            required = int(self.manifest.get("required_answer_count", len(original_hunters)))
            if len(answers) < required:
                raise ConfigError(f"analysis quorum requires {required} sealed answers; found {len(answers)}")

            analysis_dir = self.run_dir / "analysis"
            previous = [
                analysis_dir / name
                for name in (
                    "analysis.json", "analysis.raw.json", "accepted-analysis.json",
                    "stdout.jsonl", "stderr.log",
                    "result.json", "launch.json", "instruction.md", "blind-answers.json",
                    "blind-evidence-index.json", "answer-map.json",
                    "reproduction-seal.json", "evidence",
                    "evidence-manifest.json",
                )
                if (analysis_dir / name).exists()
            ]
            if previous:
                archive = analysis_dir / "attempts" / _new_id("retry")
                archive.mkdir(parents=True)
                for path in previous:
                    path.rename(archive / path.name)
                self._emit(f"[primary analysis] previous failed attempt archived: {archive}")

            self._update_manifest(status="analyzing", analyzer_result=None)
            self._emit(f"[primary analysis] reusing {len(answers)} sealed hunter answers; no hunters relaunched")
            baseline: dict[str, Any] | None = None
            baseline_result: dict[str, Any] | None = None
            if self.config.analyzer.baseline_first:
                baseline_path = self.run_dir / "baseline" / "baseline.json"
                if baseline_path.is_file():
                    baseline = read_json(baseline_path)
                    baseline_result_path = self.run_dir / "baseline" / "result.json"
                    baseline_result = (
                        read_json(baseline_result_path)
                        if baseline_result_path.is_file()
                        else {"status": "completed", "artifact_valid": True, "reused": True}
                    )
                else:
                    baseline, baseline_result = await self._run_baseline()
                if baseline is None:
                    raise ConfigError("baseline-first adjudication failed")
            analysis, analyzer_result = await self._run_analyzer(answers, baseline)
            verification: dict[str, Any] | None = None
            verifier_result: dict[str, Any] | None = None
            if analysis is not None and self.config.secondary_analyzer is not None:
                verification, verifier_result = await self._run_secondary_verifier(answers, analysis)
            self._verify_context_snapshot("analysis-retry-finish")
            accepted_analysis = (
                self._accepted_analysis(analysis, verification)
                if analysis is not None
                and (self.config.secondary_analyzer is None or verification is not None)
                else None
            )
            if accepted_analysis is not None:
                write_json(analysis_dir / "accepted-analysis.json", accepted_analysis)
            final_analysis = (
                attribute_analysis(accepted_analysis, answers)
                if accepted_analysis is not None
                else None
            )
            write_final_report(
                self.run_dir / "final",
                instruction=self.instruction,
                answers=answers,
                analysis=final_analysis,
                expected_answer_count=len(original_hunters),
                minimum_answer_count=required,
                partial_hunters=list(self.manifest.get("partial_hunters") or []),
                adjudication=_adjudicator_disclosure(self.config),
                baseline=baseline,
                verification=verification,
                analyzer_result=analyzer_result,
                fanout_evidence=[
                    {
                        "hunter_id": result.get("hunter_id"),
                        "fanout_evidence": result.get("fanout_evidence", "unverified"),
                        "fanout_summary": result.get("fanout_summary"),
                    }
                    for result in results_by_id.values()
                    if result.get("answer_sealed")
                ],
            )
            status = (
                "completed"
                if accepted_analysis is not None
                and analyzer_result.get("status") == "completed"
                and (
                    self.config.secondary_analyzer is None
                    or verifier_result is not None
                    and verifier_result.get("status") == "completed"
                )
                else "incomplete"
            )
            storage_result = (
                self._finalize_workspace_storage()
                if status == "completed"
                else {
                    "cache_root": str(self.config.storage.cache_root),
                    "workspace_cleanup": "retained_for_recovery",
                    "scratch": "retained_until_expiry",
                }
            )
            self._update_manifest(
                status=status,
                completed_at=_utc_now(),
                analyzer_result=analyzer_result,
                baseline_result=baseline_result,
                verifier_result=verifier_result,
                storage=storage_result,
            )
            self._set_session_round_status(status)
            self._emit(f"round {status}; artifacts: {self.run_dir}")
            return self.run_dir
        finally:
            if self._storage_active and self._storage is not None:
                try:
                    self._storage.deactivate(
                        successful=self.manifest.get("status") == "completed"
                    )
                except (OSError, ConfigError) as exc:
                    self._emit(f"[storage] scratch cleanup failed: {exc}")
                self._storage_active = False
            self._release_lock()
