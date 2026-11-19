# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from .models import ConfigError, EngineConfig


_MAX_INVENTORY_BYTES = 4 * 1024 * 1024


def normalize_ollama_base_url(value: str) -> str:
    """Return a validated OpenAI-compatible Ollama URL for a local or LAN server."""
    raw = value.strip()
    if not raw:
        raise ConfigError("Ollama server URL cannot be empty")
    supplied_scheme = "://" in raw
    if not supplied_scheme:
        raw = "http://" + raw
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"Ollama server URL is malformed: {exc}") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            "Ollama server URL must use http(s) without credentials, query, or fragment"
        )
    netloc = parsed.netloc
    if not supplied_scheme and port is None:
        netloc += ":11434"
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    elif not path.endswith("/v1"):
        raise ConfigError("Ollama server URL must end in /v1")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def list_ollama_models(
    base_url: str,
    *,
    timeout: int = 8,
) -> tuple[str, str, tuple[str, ...]]:
    """Read a local or LAN Ollama catalog without loading a model."""
    normalized = normalize_ollama_base_url(base_url)
    parsed = urlsplit(normalized)
    native_path = parsed.path.rstrip("/")[:-3] + "/api/tags"
    inventory_url = urlunsplit((parsed.scheme, parsed.netloc, native_path, "", ""))
    request = Request(inventory_url, headers={"Accept": "application/json"})
    try:
        # LAN inference endpoints must be contacted directly, not through a shell proxy.
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            payload = response.read(_MAX_INVENTORY_BYTES + 1)
        if len(payload) > _MAX_INVENTORY_BYTES:
            raise ValueError("response exceeds 4 MiB")
        inventory = json.loads(payload)
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"cannot read Ollama model inventory at {inventory_url}: {exc}. "
            "Check that the server is reachable from this machine."
        ) from exc
    if not isinstance(inventory, dict) or not isinstance(inventory.get("models"), list):
        raise ConfigError(f"Ollama server returned an invalid inventory from {inventory_url}")
    available = {
        cleaned
        for item in inventory["models"]
        if isinstance(item, dict)
        for value in (item.get("name"), item.get("model"))
        if isinstance(value, str)
        if (cleaned := value.strip())
        and len(cleaned) <= 256
        and all(ord(character) >= 32 and ord(character) != 127 for character in cleaned)
    }
    return normalized, inventory_url, tuple(sorted(available, key=str.casefold))


def probe_ollama_model(engine: EngineConfig) -> str:
    """Check the native Ollama inventory without starting a model or spending tokens."""
    base_url = engine.ollama_base_url or "http://127.0.0.1:11434/v1"
    try:
        normalized, inventory_url, available = list_ollama_models(base_url)
    except ConfigError as exc:
        raise ConfigError(
            f"Ollama hunter {engine.id!r} {exc}"
        ) from exc
    model_name = engine.effective_model.removeprefix("ollama/")
    if model_name not in available:
        raise ConfigError(
            f"Ollama hunter {engine.id!r} model {model_name!r} is absent from "
            f"{inventory_url}; choose an exact name from that server's ollama list"
        )
    parsed = urlsplit(normalized)
    native_path = parsed.path.rstrip("/")[:-3] + "/api/tags"
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
