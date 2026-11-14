# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from .models import AppConfig, EngineConfig


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{timestamp}-{secrets.token_hex(4)}"


def engine_manifest(engine: EngineConfig) -> dict[str, Any]:
    return {
        "id": engine.id,
        "harness": engine.harness,
        "harness_version": engine.harness_version,
        "provider": engine.provider,
        "requested_model": engine.model,
        "model_snapshot": engine.model_snapshot,
        "effective_model": engine.effective_model,
        "reasoning_effort": engine.reasoning_effort,
        "supported_efforts": list(engine.supported_efforts),
        "timeout_seconds": engine.timeout_seconds,
        "worker_timeout_seconds": engine.worker_timeout_seconds,
        "workers": engine.workers,
        "subagents": engine.subagents,
        "pass_env_names": list(engine.pass_env),
        "ollama_base_url": engine.ollama_base_url,
    }


def adjudicator_disclosure(config: AppConfig) -> dict[str, Any]:
    def public_engine(engine: EngineConfig) -> dict[str, str]:
        return {
            "id": engine.id,
            "provider": engine.provider,
            "model": engine.effective_model,
            "harness": engine.harness,
        }

    def overlaps(engine: EngineConfig) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for hunter in config.hunters:
            shared = []
            if engine.provider and engine.provider == hunter.provider:
                shared.append("provider")
            if engine.effective_model == hunter.effective_model:
                shared.append("model")
            if engine.harness == hunter.harness:
                shared.append("harness")
            if shared:
                records.append({"hunter_id": hunter.id, "shared_axes": shared})
        return records

    disclosure: dict[str, Any] = {
        "primary": public_engine(config.analyzer),
        "primary_hunter_overlap": overlaps(config.analyzer),
    }
    if config.secondary_analyzer is not None:
        disclosure["secondary"] = public_engine(config.secondary_analyzer)
        disclosure["secondary_hunter_overlap"] = overlaps(config.secondary_analyzer)
        shared = []
        if config.secondary_analyzer.provider == config.analyzer.provider:
            shared.append("provider")
        if config.secondary_analyzer.effective_model == config.analyzer.effective_model:
            shared.append("model")
        if config.secondary_analyzer.harness == config.analyzer.harness:
            shared.append("harness")
        disclosure["primary_secondary_shared_axes"] = shared
    return disclosure
