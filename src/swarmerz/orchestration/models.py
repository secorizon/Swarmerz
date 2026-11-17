# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EFFORTS = ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max")
CLINE_EFFORTS = ("none",)


@dataclass(frozen=True)
class ContextConfig:
    path: Path | None = None
    allow_symlinks: bool = False
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineConfig:
    id: str
    harness: str
    harness_version: str
    provider: str
    model: str
    model_snapshot: str | None
    reasoning_effort: str
    supported_efforts: tuple[str, ...]
    timeout_seconds: int
    command: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    pass_env: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    answer_path: str = ".swarm/answer.json"
    workers: int = 1
    subagents: int = 0
    ollama_base_url: str | None = None
    worker_timeout_seconds: int | None = None

    @property
    def effective_model(self) -> str:
        return self.model_snapshot or self.model

    @property
    def swarm_size(self) -> int:
        return max(self.workers, self.subagents)


@dataclass(frozen=True)
class AnalyzerConfig(EngineConfig):
    blind_hunter_identity: bool = True
    baseline_first: bool = False


@dataclass(frozen=True)
class IsolationConfig:
    mode: str = "workspace"
    inherit_environment: bool = False
    opencode_server_mode: str = "shared"


@dataclass(frozen=True)
class ResourceLimitsConfig:
    mode: str = "auto"
    memory_max_bytes: int = 16 * 1024**3
    memory_swap_max_bytes: int = 2 * 1024**3
    tasks_max: int = 512


@dataclass(frozen=True)
class StorageConfig:
    cache_root: Path
    max_cache_bytes: int = 40 * 1024**3
    max_cache_age_days: int = 30
    failed_scratch_retention_days: int = 7
    maintenance_interval_hours: int = 24
    cleanup_successful_workspaces: bool = True


@dataclass(frozen=True)
class AppConfig:
    schema_version: int
    config_path: Path
    output_root: Path
    max_parallel_hunters: int
    minimum_answers: int
    opencode_artifact_grace_seconds: int
    opencode_worker_timeout_seconds: int
    opencode_worker_poll_seconds: int
    opencode_answer_quiet_seconds: int
    opencode_orphan_quiet_seconds: int
    opencode_finalize_timeout_seconds: int
    opencode_finalize_answer_quiet_seconds: int
    codex_transport_retries: int
    codex_retry_delay_seconds: int
    context: ContextConfig
    hunters: tuple[EngineConfig, ...]
    analyzer: AnalyzerConfig
    secondary_analyzer: AnalyzerConfig | None
    isolation: IsolationConfig
    resources: ResourceLimitsConfig
    storage: StorageConfig
    raw: dict[str, Any]


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    artifact_path: Path | None


class ConfigError(ValueError):
    """Raised when configuration is invalid or ambiguous."""
