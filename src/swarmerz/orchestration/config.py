# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import copy
import json
import math
import os
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .models import (
    AnalyzerConfig,
    AppConfig,
    CLINE_EFFORTS,
    ConfigError,
    ContextConfig,
    EFFORTS,
    EngineConfig,
    IsolationConfig,
    ResourceLimitsConfig,
    StorageConfig,
)


_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_CODEX_HARNESSES = {"codex", "codex-exec"}
_ANALYZER_HARNESSES = {*_CODEX_HARNESSES, "command"}
_KNOWN_HARNESSES = {"cline", "opencode", "codex", "codex-exec", "command"}


def _required(table: dict[str, Any], key: str, where: str) -> Any:
    if key not in table:
        raise ConfigError(f"missing {where}.{key}")
    return table[key]


def _as_tuple(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where} must be an array of strings")
    return tuple(value)


def _parse_engine(table: dict[str, Any], where: str, *, analyzer: bool = False) -> EngineConfig:
    engine_id = str(table.get("id", "codex-analyzer" if analyzer else ""))
    if not _SAFE_ID.fullmatch(engine_id):
        raise ConfigError(f"{where}.id must match {_SAFE_ID.pattern!r}")

    harness = str(_required(table, "harness", where)).lower()
    if harness not in _KNOWN_HARNESSES:
        raise ConfigError(f"{where}.harness is unsupported: {harness}")
    if analyzer and harness not in _ANALYZER_HARNESSES:
        raise ConfigError(
            f"{where}.harness must be Codex-backed or an explicit command adapter; got {harness}"
        )

    effort = str(_required(table, "reasoning_effort", where)).lower()
    if effort not in EFFORTS:
        raise ConfigError(f"{where}.reasoning_effort must be one of {', '.join(EFFORTS)}")

    declared_efforts = _as_tuple(table.get("supported_efforts"), f"{where}.supported_efforts")
    if harness == "cline":
        supported_efforts = declared_efforts or CLINE_EFFORTS
        impossible = set(supported_efforts) - set(CLINE_EFFORTS)
        if impossible:
            raise ConfigError(
                f"{where}.supported_efforts contains values the Cline CLI cannot select: {sorted(impossible)}"
            )
    elif harness in _CODEX_HARNESSES:
        supported_efforts = declared_efforts or ("minimal", "low", "medium", "high", "xhigh")
    else:
        if not declared_efforts:
            raise ConfigError(
                f"{where}.supported_efforts is required for {harness}; capability downgrades are never inferred"
            )
        supported_efforts = declared_efforts

    unknown_declared = set(supported_efforts) - set(EFFORTS)
    if unknown_declared:
        raise ConfigError(f"{where}.supported_efforts contains unknown values: {sorted(unknown_declared)}")
    if effort not in supported_efforts:
        raise ConfigError(
            f"{where} requests effort {effort!r}, but declares support for {', '.join(supported_efforts)}"
        )

    timeout_seconds = int(table.get("timeout_seconds", 14_400))
    if timeout_seconds <= 0:
        raise ConfigError(f"{where}.timeout_seconds must be positive")

    command = _as_tuple(table.get("command"), f"{where}.command")
    if harness == "command" and not command:
        raise ConfigError(f"{where}.command is required for the command harness")

    env_table = table.get("env", {})
    if not isinstance(env_table, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, int, float, bool)) for key, value in env_table.items()
    ):
        raise ConfigError(f"{where}.env must be a string-keyed scalar table")

    ollama_base_url = table.get("ollama_base_url")
    if ollama_base_url is not None:
        if harness != "opencode" or str(table.get("provider", "")) != "ollama":
            raise ConfigError(f"{where}.ollama_base_url requires the OpenCode ollama provider")
        if not isinstance(ollama_base_url, str):
            raise ConfigError(f"{where}.ollama_base_url must be a URL string")
        try:
            parsed_url = urlsplit(ollama_base_url)
            hostname = parsed_url.hostname
            _port = parsed_url.port
        except ValueError as exc:
            raise ConfigError(f"{where}.ollama_base_url is malformed: {exc}") from exc
        if (
            parsed_url.scheme not in {"http", "https"}
            or not hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
            or not parsed_url.path.rstrip("/").endswith("/v1")
        ):
            raise ConfigError(
                f"{where}.ollama_base_url must be an http(s) Ollama URL ending in /v1 "
                "without credentials, query, or fragment"
            )
    common: dict[str, Any] = {
        "id": engine_id,
        "harness": harness,
        "harness_version": str(table.get("harness_version", "unversioned")),
        "provider": str(table.get("provider", "")),
        "model": str(_required(table, "model", where)),
        "model_snapshot": str(table["model_snapshot"]) if table.get("model_snapshot") else None,
        "reasoning_effort": effort,
        "supported_efforts": tuple(supported_efforts),
        "timeout_seconds": timeout_seconds,
        "command": command,
        "extra_args": _as_tuple(table.get("extra_args"), f"{where}.extra_args"),
        "pass_env": _as_tuple(table.get("pass_env"), f"{where}.pass_env"),
        "env": {str(key): str(value) for key, value in env_table.items()},
        "answer_path": str(table.get("answer_path", ".swarm/answer.json")),
        "workers": int(table.get("workers", 1)),
        "subagents": int(table.get("subagents", 0)),
        "ollama_base_url": ollama_base_url,
        "worker_timeout_seconds": (
            int(table["worker_timeout_seconds"])
            if table.get("worker_timeout_seconds") is not None
            else None
        ),
    }
    if common["workers"] <= 0 or common["subagents"] < 0:
        raise ConfigError(f"{where}.workers must be positive and subagents must be non-negative")
    if (
        common["worker_timeout_seconds"] is not None
        and common["worker_timeout_seconds"] <= 0
    ):
        raise ConfigError(f"{where}.worker_timeout_seconds must be positive")
    if not analyzer and max(common["workers"], common["subagents"]) < 2:
        raise ConfigError(f"{where} must configure at least two internal swarm workers or subagents")
    if not analyzer and harness in _CODEX_HARNESSES and "--ephemeral" in common["extra_args"]:
        raise ConfigError(
            f"{where}.extra_args cannot use --ephemeral because Codex hunter subagents "
            "must attach to a persisted parent thread"
        )

    answer_path = PurePosixPath(common["answer_path"])
    if answer_path.is_absolute() or ".." in answer_path.parts:
        raise ConfigError(f"{where}.answer_path must stay inside the hunter workspace")

    if analyzer:
        blind_hunter_identity = bool(table.get("blind_hunter_identity", True))
        if not blind_hunter_identity:
            raise ConfigError(f"{where}.blind_hunter_identity must remain true")
        return AnalyzerConfig(
            **common,
            blind_hunter_identity=blind_hunter_identity,
            baseline_first=bool(table.get("baseline_first", False)),
        )
    return EngineConfig(**common)


def _parse_override_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def apply_overrides(raw: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    result = copy.deepcopy(raw)
    for override in overrides:
        if "=" not in override:
            raise ConfigError(f"override must be PATH=VALUE: {override}")
        dotted, value_text = override.split("=", 1)
        parts = dotted.split(".")
        if not parts or any(not part for part in parts):
            raise ConfigError(f"invalid override path: {dotted}")
        cursor: Any = result
        for part in parts[:-1]:
            if isinstance(cursor, list):
                try:
                    cursor = cursor[int(part)]
                except (ValueError, IndexError) as exc:
                    raise ConfigError(f"invalid list index in override: {dotted}") from exc
            elif isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            else:
                raise ConfigError(f"unknown override path: {dotted}")
        leaf = parts[-1]
        if isinstance(cursor, list):
            try:
                cursor[int(leaf)] = _parse_override_value(value_text)
            except (ValueError, IndexError) as exc:
                raise ConfigError(f"invalid list index in override: {dotted}") from exc
        elif isinstance(cursor, dict) and leaf in cursor:
            cursor[leaf] = _parse_override_value(value_text)
        else:
            raise ConfigError(f"unknown override path: {dotted}")
    return result


def _optional_path(base: Path, value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    else:
        path = path.resolve()
    return path


def _resource_gib(value: Any, where: str) -> int:
    try:
        gib = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} must be a non-negative number") from exc
    if not math.isfinite(gib) or gib < 0:
        raise ConfigError(f"{where} must be a non-negative number")
    return int(gib * 1024**3)


def load_config(path: str | Path, overrides: list[str] | None = None) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file does not exist: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    raw = apply_overrides(raw, overrides or [])
    schema_version = int(raw.get("schema_version", 1))
    if schema_version != 1:
        raise ConfigError(f"unsupported schema_version: {schema_version}")

    base = config_path.parent
    output_root = Path(str(raw.get("output_root", "./swarm-runs"))).expanduser()
    if not output_root.is_absolute():
        output_root = (base / output_root).resolve()
    else:
        output_root = output_root.resolve()

    storage_table = raw.get("storage", {})
    if not isinstance(storage_table, dict):
        raise ConfigError("storage must be a table")
    configured_cache_root = _optional_path(base, storage_table.get("cache_root"))
    if configured_cache_root is None:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        cache_base = (
            Path(xdg_cache).expanduser()
            if xdg_cache and Path(xdg_cache).expanduser().is_absolute()
            else Path.home() / ".cache"
        )
        cache_root = (cache_base / "swarmerz").resolve()
    else:
        cache_root = configured_cache_root
    if (
        cache_root == output_root
        or cache_root in output_root.parents
        or output_root in cache_root.parents
    ):
        raise ConfigError("storage.cache_root and output_root must not contain one another")
    if cache_root == Path("/") or cache_root == Path.home().resolve():
        raise ConfigError("storage.cache_root must be a dedicated cache directory")

    try:
        max_cache_gib = float(storage_table.get("max_cache_gib", 40))
    except (TypeError, ValueError) as exc:
        raise ConfigError("storage.max_cache_gib must be a positive number") from exc
    if not math.isfinite(max_cache_gib) or max_cache_gib <= 0:
        raise ConfigError("storage.max_cache_gib must be a positive number")
    max_cache_age_days = int(storage_table.get("max_cache_age_days", 30))
    failed_scratch_retention_days = int(
        storage_table.get("failed_scratch_retention_days", 7)
    )
    maintenance_interval_hours = int(
        storage_table.get("maintenance_interval_hours", 24)
    )
    if max_cache_age_days <= 0:
        raise ConfigError("storage.max_cache_age_days must be positive")
    if failed_scratch_retention_days <= 0:
        raise ConfigError("storage.failed_scratch_retention_days must be positive")
    if maintenance_interval_hours <= 0:
        raise ConfigError("storage.maintenance_interval_hours must be positive")
    cleanup_successful_workspaces = storage_table.get(
        "cleanup_successful_workspaces", True
    )
    if not isinstance(cleanup_successful_workspaces, bool):
        raise ConfigError("storage.cleanup_successful_workspaces must be true or false")

    resources_table = raw.get("resources", {})
    if not isinstance(resources_table, dict):
        raise ConfigError("resources must be a table")
    resources_mode = str(resources_table.get("mode", "auto")).lower()
    if resources_mode not in {"auto", "required", "off"}:
        raise ConfigError("resources.mode must be auto, required, or off")
    resource_memory_max_bytes = _resource_gib(
        resources_table.get("memory_max_gib", 16),
        "resources.memory_max_gib",
    )
    resource_memory_swap_max_bytes = _resource_gib(
        resources_table.get("memory_swap_max_gib", 2),
        "resources.memory_swap_max_gib",
    )
    resource_tasks_max = int(resources_table.get("tasks_max", 512))
    if resource_tasks_max < 0:
        raise ConfigError("resources.tasks_max must be non-negative")

    context_table = raw.get("context", {})
    hunter_tables = _required(raw, "hunters", "root")
    analyzer_table = _required(raw, "analyzer", "root")
    secondary_analyzer_table = raw.get("secondary_analyzer")
    if not isinstance(context_table, dict):
        raise ConfigError("context must be a table")
    context_exclude = context_table.get("exclude", [])
    if (
        not isinstance(context_exclude, list)
        or any(
            not isinstance(pattern, str)
            or not pattern
            or "/" in pattern
            or "\\" in pattern
            for pattern in context_exclude
        )
    ):
        raise ConfigError("context.exclude must be an array of nonempty file/directory name patterns")
    if not isinstance(hunter_tables, list) or not 2 <= len(hunter_tables) <= 26:
        raise ConfigError("hunters must contain between 2 and 26 tables")
    if not isinstance(analyzer_table, dict):
        raise ConfigError("analyzer must be a table")
    if secondary_analyzer_table is not None and not isinstance(secondary_analyzer_table, dict):
        raise ConfigError("secondary_analyzer must be a table")

    max_parallel_hunters = int(raw.get("max_parallel_hunters", len(hunter_tables)))
    if max_parallel_hunters < len(hunter_tables):
        raise ConfigError(
            "max_parallel_hunters must be at least the configured hunter count "
            "so all hunters can start independently"
        )

    minimum_answers = int(raw.get("minimum_answers", len(hunter_tables)))
    if not 2 <= minimum_answers <= len(hunter_tables):
        raise ConfigError(
            f"minimum_answers must be between 2 and the configured hunter count ({len(hunter_tables)})"
        )
    opencode_artifact_grace_seconds = int(raw.get("opencode_artifact_grace_seconds", 3600))
    if opencode_artifact_grace_seconds < 0:
        raise ConfigError("opencode_artifact_grace_seconds must be non-negative")
    opencode_worker_timeout_seconds = int(
        raw.get("opencode_worker_timeout_seconds", opencode_artifact_grace_seconds)
    )
    if opencode_worker_timeout_seconds <= 0:
        raise ConfigError("opencode_worker_timeout_seconds must be positive")
    opencode_worker_poll_seconds = int(raw.get("opencode_worker_poll_seconds", 30))
    if opencode_worker_poll_seconds <= 0:
        raise ConfigError("opencode_worker_poll_seconds must be positive")
    opencode_answer_quiet_seconds = int(raw.get("opencode_answer_quiet_seconds", 180))
    if opencode_answer_quiet_seconds <= 0:
        raise ConfigError("opencode_answer_quiet_seconds must be positive")
    opencode_orphan_quiet_seconds = int(raw.get("opencode_orphan_quiet_seconds", 600))
    if opencode_orphan_quiet_seconds <= 0:
        raise ConfigError("opencode_orphan_quiet_seconds must be positive")
    opencode_finalize_timeout_seconds = int(raw.get("opencode_finalize_timeout_seconds", 900))
    if opencode_finalize_timeout_seconds <= 0:
        raise ConfigError("opencode_finalize_timeout_seconds must be positive")
    opencode_finalize_answer_quiet_seconds = int(
        raw.get("opencode_finalize_answer_quiet_seconds", 60)
    )
    if opencode_finalize_answer_quiet_seconds <= 0:
        raise ConfigError("opencode_finalize_answer_quiet_seconds must be positive")
    codex_transport_retries = int(raw.get("codex_transport_retries", 2))
    if not 0 <= codex_transport_retries <= 10:
        raise ConfigError("codex_transport_retries must be between 0 and 10")
    codex_retry_delay_seconds = int(raw.get("codex_retry_delay_seconds", 10))
    if codex_retry_delay_seconds < 0:
        raise ConfigError("codex_retry_delay_seconds must be non-negative")

    context_path = _optional_path(base, context_table.get("path"))
    if context_path is not None and not context_path.is_dir():
        raise ConfigError(f"context.path is not a directory: {context_path}")

    hunters = tuple(_parse_engine(table, f"hunters[{index}]") for index, table in enumerate(hunter_tables))
    ids = [hunter.id for hunter in hunters]
    duplicates = sorted({hunter_id for hunter_id in ids if ids.count(hunter_id) > 1})
    if duplicates:
        raise ConfigError(f"hunter ids must be unique: {', '.join(duplicates)}")

    isolation_table = raw.get("isolation", {})
    if not isinstance(isolation_table, dict):
        raise ConfigError("isolation must be a table")
    isolation_mode = str(isolation_table.get("mode", "workspace"))
    if isolation_mode != "workspace":
        raise ConfigError("only workspace isolation is currently implemented")
    opencode_server_mode = str(
        isolation_table.get("opencode_server_mode", "shared")
    ).lower()
    if opencode_server_mode not in {"shared", "per_hunter"}:
        raise ConfigError(
            "isolation.opencode_server_mode must be shared or per_hunter"
        )

    analyzer = _parse_engine(analyzer_table, "analyzer", analyzer=True)
    secondary_analyzer = (
        _parse_engine(secondary_analyzer_table, "secondary_analyzer", analyzer=True)
        if secondary_analyzer_table is not None
        else None
    )
    if secondary_analyzer is not None:
        if secondary_analyzer.id == analyzer.id:
            raise ConfigError("secondary_analyzer.id must differ from analyzer.id")
        if (
            secondary_analyzer.provider,
            secondary_analyzer.effective_model,
        ) == (analyzer.provider, analyzer.effective_model):
            raise ConfigError(
                "secondary_analyzer must use a different provider or model from analyzer"
            )

    return AppConfig(
        schema_version=schema_version,
        config_path=config_path,
        output_root=output_root,
        max_parallel_hunters=max_parallel_hunters,
        minimum_answers=minimum_answers,
        opencode_artifact_grace_seconds=opencode_artifact_grace_seconds,
        opencode_worker_timeout_seconds=opencode_worker_timeout_seconds,
        opencode_worker_poll_seconds=opencode_worker_poll_seconds,
        opencode_answer_quiet_seconds=opencode_answer_quiet_seconds,
        opencode_orphan_quiet_seconds=opencode_orphan_quiet_seconds,
        opencode_finalize_timeout_seconds=opencode_finalize_timeout_seconds,
        opencode_finalize_answer_quiet_seconds=opencode_finalize_answer_quiet_seconds,
        codex_transport_retries=codex_transport_retries,
        codex_retry_delay_seconds=codex_retry_delay_seconds,
        context=ContextConfig(
            path=context_path,
            allow_symlinks=bool(context_table.get("allow_symlinks", False)),
            exclude=tuple(context_exclude),
        ),
        hunters=hunters,
        analyzer=analyzer,
        secondary_analyzer=secondary_analyzer,
        isolation=IsolationConfig(
            mode=isolation_mode,
            inherit_environment=bool(isolation_table.get("inherit_environment", False)),
            opencode_server_mode=opencode_server_mode,
        ),
        resources=ResourceLimitsConfig(
            mode=resources_mode,
            memory_max_bytes=resource_memory_max_bytes,
            memory_swap_max_bytes=resource_memory_swap_max_bytes,
            tasks_max=resource_tasks_max,
        ),
        storage=StorageConfig(
            cache_root=cache_root,
            max_cache_bytes=int(max_cache_gib * 1024**3),
            max_cache_age_days=max_cache_age_days,
            failed_scratch_retention_days=failed_scratch_retention_days,
            maintenance_interval_hours=maintenance_interval_hours,
            cleanup_successful_workspaces=cleanup_successful_workspaces,
        ),
        raw=raw,
    )
