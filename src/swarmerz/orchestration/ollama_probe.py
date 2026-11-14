# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from .models import ConfigError, EngineConfig


def probe_ollama_model(engine: EngineConfig) -> str:
    """Check the native Ollama inventory without starting a model or spending tokens."""
    base_url = engine.ollama_base_url or "http://127.0.0.1:11434/v1"
    parsed = urlsplit(base_url)
    native_path = parsed.path.rstrip("/")[:-3] + "/api/tags"
    inventory_url = urlunsplit((parsed.scheme, parsed.netloc, native_path, "", ""))
    request = Request(inventory_url, headers={"Accept": "application/json"})
    try:
        # Match the isolated OpenCode server: do not inherit a shell HTTP proxy.
        with build_opener(ProxyHandler({})).open(request, timeout=8) as response:
            inventory = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"Ollama hunter {engine.id!r} cannot read {inventory_url}: {exc}. "
            "Check that the Ollama server is reachable from this machine."
        ) from exc
    if not isinstance(inventory, dict) or not isinstance(inventory.get("models"), list):
        raise ConfigError(f"Ollama hunter {engine.id!r} received an invalid /api/tags inventory")
    model_name = engine.effective_model.removeprefix("ollama/")
    available = {
        value
        for item in inventory["models"]
        if isinstance(item, dict)
        for value in (item.get("name"), item.get("model"))
        if isinstance(value, str)
    }
    if model_name not in available:
        raise ConfigError(
            f"Ollama hunter {engine.id!r} model {model_name!r} is absent from "
            f"{inventory_url}; choose an exact name from that server's ollama list"
        )
    detail_url = urlunsplit((parsed.scheme, parsed.netloc, native_path.removesuffix("/tags") + "/show", "", ""))
    detail_request = Request(
        detail_url,
        data=json.dumps({"model": model_name}).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with build_opener(ProxyHandler({})).open(detail_request, timeout=8) as response:
            details = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"Ollama hunter {engine.id!r} cannot inspect {model_name!r} at "
            f"{detail_url}: {exc}"
        ) from exc
    capabilities = details.get("capabilities") if isinstance(details, dict) else None
    if not isinstance(capabilities, list) or not {"completion", "tools"}.issubset(capabilities):
        raise ConfigError(
            f"Ollama model {model_name!r} must report completion and tools capabilities "
            "to run an OpenCode hunter swarm"
        )
    return inventory_url
