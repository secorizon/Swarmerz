# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import ConfigError, EngineConfig


@dataclass(frozen=True)
class CodexCatalogModel:
    slug: str
    display_name: str
    supported_efforts: tuple[str, ...]
    efforts_declared: bool


@dataclass(frozen=True)
class CodexModelCatalog:
    fetched_at: datetime
    fresh: bool
    models: tuple[CodexCatalogModel, ...]


def read_chatgpt_codex_catalog(
    codex_root: Path | None = None,
    *,
    now: datetime | None = None,
) -> CodexModelCatalog | None:
    """Read Codex's credential-free ChatGPT model cache.

    The authentication file is inspected only to confirm the login mode. No
    account identity, token, or other authentication field leaves this
    function.
    """
    root = codex_root or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        auth = json.loads((root / "auth.json").read_text(encoding="utf-8"))
        raw_catalog = json.loads(
            (root / "models_cache.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(auth, dict) or auth.get("auth_mode") != "chatgpt":
        return None
    if not isinstance(raw_catalog, dict) or not isinstance(
        raw_catalog.get("models"), list
    ):
        return None
    try:
        fetched_at = datetime.fromisoformat(
            str(raw_catalog["fetched_at"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError):
        return None
    if fetched_at.tzinfo is None:
        return None

    current = now or datetime.now(UTC)
    age = current - fetched_at
    fresh = timedelta(0) <= age <= timedelta(hours=24)
    models: list[CodexCatalogModel] = []
    for item in raw_catalog["models"]:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            continue
        raw_efforts = item.get("supported_reasoning_levels")
        efforts = tuple(dict.fromkeys(
            effort
            for value in raw_efforts or []
            if isinstance(value, (str, dict))
            and isinstance(
                effort := value if isinstance(value, str) else value.get("effort"),
                str,
            )
        )) if isinstance(raw_efforts, list) else ()
        display_name = item.get("display_name")
        models.append(CodexCatalogModel(
            slug=item["slug"],
            display_name=(
                display_name if isinstance(display_name, str) else item["slug"]
            ),
            supported_efforts=efforts,
            efforts_declared=isinstance(raw_efforts, list),
        ))
    return CodexModelCatalog(
        fetched_at=fetched_at,
        fresh=fresh,
        models=tuple(models),
    )


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
    catalog = read_chatgpt_codex_catalog()
    if catalog is None or not catalog.fresh:
        return []
    available = {model.slug: model for model in catalog.models}
    problems: list[str] = []
    for engine in native:
        model = available.get(engine.effective_model)
        if model is None:
            problems.append(f"{engine.id}: {engine.effective_model} is not in this ChatGPT account's model catalog")
            continue
        supported_efforts = set(model.supported_efforts)
        if (
            engine.reasoning_effort != "default"
            and model.efforts_declared
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
