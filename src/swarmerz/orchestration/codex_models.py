# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import ConfigError, EngineConfig


def check_chatgpt_codex_models(engines: tuple[EngineConfig, ...]) -> list[str]:
    """Check a fresh local ChatGPT model catalog without spending model tokens.

    An absent or stale catalog is inconclusive, so only a fresh positive account
    catalog can reject a configured model before the other hunters launch.
    """
    native = [
        engine for engine in engines
        if engine.harness in {"codex", "codex-exec"} and not engine.command
    ]
    if not native:
        return []
    codex_root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        auth = json.loads((codex_root / "auth.json").read_text(encoding="utf-8"))
        catalog = json.loads((codex_root / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(auth, dict) or auth.get("auth_mode") != "chatgpt":
        return []
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        return []
    try:
        fetched_at = datetime.fromisoformat(str(catalog["fetched_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return []
    if fetched_at.tzinfo is None or not timedelta(0) <= datetime.now(UTC) - fetched_at <= timedelta(hours=24):
        return []
    available = {
        model["slug"]: model for model in catalog["models"]
        if isinstance(model, dict) and isinstance(model.get("slug"), str)
    }
    problems: list[str] = []
    for engine in native:
        model = available.get(engine.effective_model)
        if model is None:
            problems.append(f"{engine.id}: {engine.effective_model} is not in this ChatGPT account's model catalog")
            continue
        supported = model.get("supported_reasoning_levels")
        supported_efforts = {
            item if isinstance(item, str) else item.get("effort")
            for item in supported or []
            if isinstance(item, (str, dict))
        } if isinstance(supported, list) else set()
        if (
            engine.reasoning_effort != "default"
            and isinstance(supported, list)
            and engine.reasoning_effort not in supported_efforts
        ):
            problems.append(
                f"{engine.id}: {engine.effective_model} does not offer "
                f"reasoning_effort={engine.reasoning_effort} in this account's catalog"
            )
    if problems:
        raise ConfigError(
            "Codex model/account mismatch before launch: " + "; ".join(problems)
            + ". Choose a model and effort listed for this account, or authenticate "
            "Codex with an API key that can access the requested model."
        )
    return [engine.id for engine in native]
