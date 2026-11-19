# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .orchestration.codex_models import read_chatgpt_codex_catalog
from .orchestration.config import load_config
from .orchestration.models import AppConfig, ConfigError, EFFORTS, EngineConfig
from .orchestration.ollama_probe import (
    list_ollama_models,
    normalize_ollama_base_url,
)


_CODEX_HARNESSES = {"codex", "codex-exec"}
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_DOCUMENTED_ALIASES = ("fable", "haiku", "opus", "sonnet")
_ACTIVE_HEADER = re.compile(
    r"(?m)^[ \t]*(\[\[?[A-Za-z0-9_.-]+\]?\])[ \t]*(?:#.*)?$"
)
_ANALYZER_HEADER = re.compile(r"(?m)^[ \t]*\[analyzer\][ \t]*(?:#.*)?$")
_Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ModelSelection:
    selector: str
    harness: str
    provider: str
    model: str
    harness_version: str
    supported_efforts: tuple[str, ...] = ()
    ollama_base_url: str | None = None


def _safe_text(value: object, *, maximum: int = 240) -> str:
    text = str(value)
    cleaned = "".join(
        character
        for character in text
        if character in "\t" or ord(character) >= 32 and ord(character) != 127
    ).replace("\t", " ").strip()
    return cleaned[:maximum]


def _run(
    runner: _Runner,
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _executable_version(
    runner: _Runner,
    executable: str,
    *,
    cwd: Path,
) -> tuple[str | None, str]:
    completed = _run(runner, (executable, "--version"), cwd=cwd)
    if completed is None or completed.returncode != 0:
        return None, "unversioned"
    line = next(
        (line for line in completed.stdout.splitlines() if line.strip()), ""
    )
    rendered = _safe_text(line)
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?)", rendered)
    return (rendered or None), (match.group(1) if match else "unversioned")


def _find_opencode(which: Callable[[str], str | None], home: Path) -> str | None:
    executable = which("opencode")
    if executable:
        return executable
    standard = home / ".opencode" / "bin" / "opencode"
    if standard.is_file() and os.access(standard, os.X_OK):
        return str(standard)
    return None


def _ollama_selector(base_url: str, model: str) -> str:
    normalized = normalize_ollama_base_url(base_url)
    endpoint_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"opencode/ollama@{endpoint_id}/{model}"


def _selector(engine: EngineConfig) -> str | None:
    if engine.command:
        return None
    if engine.harness == "opencode" and engine.provider:
        if engine.provider == "ollama" and engine.ollama_base_url:
            return _ollama_selector(engine.ollama_base_url, engine.effective_model)
        return f"opencode/{engine.provider}/{engine.effective_model}"
    if engine.harness in _CODEX_HARNESSES:
        return f"codex/{engine.effective_model}"
    if engine.harness == "claude-code":
        return f"claude-code/{engine.effective_model}"
    return None


def configured_routes(config: AppConfig) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []

    def add(role: str, engine: EngineConfig) -> None:
        routes.append({
            "role": role,
            "id": engine.id,
            "harness": engine.harness,
            "harness_version": engine.harness_version,
            "provider": engine.provider,
            "model": engine.effective_model,
            "selector": _selector(engine),
            "reasoning_effort": engine.reasoning_effort,
            "supported_efforts": list(engine.supported_efforts),
            "ollama_base_url": engine.ollama_base_url,
        })

    for hunter in config.hunters:
        add("hunter", hunter)
    add("judge", config.analyzer)
    if config.secondary_analyzer is not None:
        add("secondary_judge", config.secondary_analyzer)
    return routes


def _opencode_inventory(
    *,
    runner: _Runner,
    which: Callable[[str], str | None],
    cwd: Path,
    home: Path,
) -> dict[str, Any]:
    executable = _find_opencode(which, home)
    result: dict[str, Any] = {
        "installed": executable is not None,
        "executable": executable,
        "version": None,
        "harness_version": "unversioned",
        "providers": [],
        "errors": [],
    }
    if executable is None:
        return result
    result["version"], result["harness_version"] = _executable_version(
        runner, executable, cwd=cwd
    )

    names: dict[str, str] = {}
    connected: set[str] = set()
    auth = _run(
        runner,
        (
            executable, "auth", "list", "--standalone", "--format", "json",
            "--log-level", "error",
        ),
        cwd=cwd,
        timeout=45,
    )
    if auth is None or auth.returncode != 0:
        result["errors"].append("connected provider inventory unavailable")
    else:
        try:
            raw_auth = json.loads(auth.stdout)
        except json.JSONDecodeError:
            raw_auth = None
        if not isinstance(raw_auth, list):
            result["errors"].append("connected provider inventory was not valid JSON")
        else:
            for item in raw_auth:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                provider_id = _safe_text(item["id"], maximum=160)
                if not provider_id:
                    continue
                connected.add(provider_id)
                name = item.get("name")
                names[provider_id] = (
                    _safe_text(name) if isinstance(name, str) else provider_id
                )

    models = _run(
        runner,
        (executable, "models", "--standalone", "--log-level", "error"),
        cwd=cwd,
        timeout=60,
    )
    grouped: dict[str, list[str]] = {}
    if models is None or models.returncode != 0:
        result["errors"].append("model catalog unavailable")
    else:
        for raw_line in models.stdout.splitlines()[:10_000]:
            identifier = _safe_text(raw_line, maximum=512)
            if (
                not identifier
                or any(character.isspace() for character in identifier)
                or "/" not in identifier
            ):
                continue
            provider_id, model = identifier.split("/", 1)
            if not provider_id or not model:
                continue
            grouped.setdefault(provider_id, []).append(model)

    providers = connected | set(grouped)
    result["providers"] = [
        {
            "id": provider_id,
            "name": names.get(provider_id, provider_id),
            "credential_configured": provider_id in connected,
            "models": [
                {
                    "model": model,
                    "selector": f"opencode/{provider_id}/{model}",
                }
                for model in sorted(set(grouped.get(provider_id, [])), key=str.casefold)
            ],
        }
        for provider_id in sorted(providers, key=str.casefold)
    ]
    return result


def merge_ollama_catalog(
    inventory: dict[str, Any],
    base_url: str,
    models: Sequence[str],
) -> tuple[str, ...]:
    """Add or replace one explicitly discovered Ollama endpoint catalog."""
    normalized = normalize_ollama_base_url(base_url)
    endpoint_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    provider_id = f"ollama@{endpoint_id}"
    host = urlsplit(normalized).netloc
    opencode = inventory.setdefault("opencode", {})
    providers = opencode.setdefault("providers", [])
    if not isinstance(providers, list):
        raise ValueError("OpenCode provider inventory is malformed")
    entries = [
        {
            "model": model,
            "selector": _ollama_selector(normalized, model),
            "provider": "ollama",
            "ollama_base_url": normalized,
            "source": "Ollama /api/tags catalog",
        }
        for raw_model in models
        if (model := _safe_text(raw_model, maximum=256))
        and not any(character.isspace() for character in model)
    ]
    entries.sort(key=lambda entry: entry["model"].casefold())
    provider = {
        "id": provider_id,
        "provider": "ollama",
        "name": f"Ollama @ {host}",
        "credential_configured": False,
        "reachable": True,
        "ollama_base_url": normalized,
        "source": "Ollama /api/tags catalog",
        "models": entries,
    }
    for index, current in enumerate(providers):
        if isinstance(current, dict) and current.get("id") == provider_id:
            providers[index] = provider
            break
    else:
        providers.append(provider)
    return tuple(entry["selector"] for entry in entries)


def _codex_inventory(
    *,
    runner: _Runner,
    which: Callable[[str], str | None],
    cwd: Path,
    home: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    executable = which("codex")
    result: dict[str, Any] = {
        "installed": executable is not None,
        "executable": executable,
        "version": None,
        "harness_version": "unversioned",
        "authenticated_catalog": False,
        "catalog_fresh": False,
        "fetched_at": None,
        "models": [],
        "note": None,
    }
    if executable is None:
        return result
    result["version"], result["harness_version"] = _executable_version(
        runner, executable, cwd=cwd
    )
    configured_root = environment.get("CODEX_HOME")
    codex_root = (
        Path(configured_root).expanduser()
        if configured_root
        else home / ".codex"
    )
    catalog = read_chatgpt_codex_catalog(codex_root)
    if catalog is None:
        result["note"] = (
            "Codex did not expose a local ChatGPT account catalog; API-key and "
            "uncached logins are not probed for credentials."
        )
        return result
    result["authenticated_catalog"] = True
    result["catalog_fresh"] = catalog.fresh
    result["fetched_at"] = catalog.fetched_at.isoformat()
    result["models"] = [
        {
            "model": model_id,
            "display_name": _safe_text(model.display_name),
            "selector": f"codex/{model_id}",
            "supported_efforts": [
                effort
                for effort in model.supported_efforts
                if re.fullmatch(r"[a-zA-Z0-9_.-]+", effort)
            ],
        }
        for model in catalog.models
        if (model_id := _safe_text(model.slug, maximum=256))
        and not any(character.isspace() for character in model_id)
    ]
    if not catalog.fresh:
        result["note"] = "Codex account catalog is older than 24 hours and may be stale."
    return result


def _read_claude_settings_models(
    *,
    cwd: Path,
    home: Path,
    environment: Mapping[str, str],
) -> tuple[list[str], list[str]]:
    configured_dir = environment.get("CLAUDE_CONFIG_DIR")
    config_dir = (
        Path(configured_dir).expanduser()
        if configured_dir
        else home / ".claude"
    )
    paths = (
        config_dir / "settings.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    )
    selected: list[str] = []
    allowlisted: list[str] = []
    for path in paths:
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        model = raw.get("model")
        if isinstance(model, str) and (safe := _safe_text(model, maximum=256)):
            selected.append(safe)
        available = raw.get("availableModels")
        if isinstance(available, list):
            allowlisted.extend(
                safe
                for value in available
                if isinstance(value, str)
                and (safe := _safe_text(value, maximum=256))
            )
    environment_model = environment.get("ANTHROPIC_MODEL")
    if environment_model and (safe := _safe_text(environment_model, maximum=256)):
        selected.append(safe)
    return list(dict.fromkeys(selected)), list(dict.fromkeys(allowlisted))


def _claude_aliases(help_text: str) -> list[str]:
    match = re.search(
        r"Use an\s+alias for the latest model\s+\(e\.g\.\s*(.*?)\)\s+or a\s+model's",
        help_text,
        flags=re.DOTALL,
    )
    if match is None:
        return []
    return list(dict.fromkeys(
        alias
        for alias in re.findall(r"'([^']+)'", match.group(1))
        if alias and re.fullmatch(r"[A-Za-z0-9_.-]+", alias)
    ))


def _claude_inventory(
    *,
    runner: _Runner,
    which: Callable[[str], str | None],
    cwd: Path,
    home: Path,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    executable = which("claude")
    result: dict[str, Any] = {
        "installed": executable is not None,
        "executable": executable,
        "version": None,
        "harness_version": "unversioned",
        "authenticated": False,
        "auth_method": None,
        "api_provider": None,
        "account_catalog_available": False,
        "models": [],
        "note": None,
    }
    if executable is None:
        return result
    result["version"], result["harness_version"] = _executable_version(
        runner, executable, cwd=cwd
    )
    status = _run(
        runner,
        (executable, "auth", "status", "--json"),
        cwd=cwd,
    )
    if status is not None and status.returncode == 0:
        try:
            raw_status = json.loads(status.stdout)
        except json.JSONDecodeError:
            raw_status = None
        if isinstance(raw_status, dict):
            result["authenticated"] = raw_status.get("loggedIn") is True
            for source, target in (
                ("authMethod", "auth_method"),
                ("apiProvider", "api_provider"),
            ):
                value = raw_status.get(source)
                if isinstance(value, str):
                    result[target] = _safe_text(value)

    help_result = _run(runner, (executable, "--help"), cwd=cwd)
    advertised_aliases = (
        _claude_aliases(help_result.stdout)
        if help_result is not None and help_result.returncode == 0
        else []
    )
    documented_aliases = [
        alias
        for alias in _CLAUDE_DOCUMENTED_ALIASES
        if alias not in advertised_aliases
    ]
    selected, allowlisted = _read_claude_settings_models(
        cwd=cwd,
        home=home,
        environment=environment,
    )
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, values in (
        ("configured_default", selected),
        ("local_allowlist", allowlisted),
        ("cli_alias", advertised_aliases),
        ("documented_alias", documented_aliases),
    ):
        for model in values:
            if model in seen:
                continue
            seen.add(model)
            entries.append({
                "model": model,
                "selector": f"claude-code/{model}",
                "source": source,
            })
    result["models"] = entries
    result["note"] = (
        "Claude Code has no read-only CLI command for the account's complete model "
        "catalog. These are local defaults, allowlisted entries, aliases advertised by "
        "the installed CLI, and stable aliases documented by Anthropic; account "
        "entitlement is enforced when Claude Code starts."
    )
    return result


def collect_model_inventory(
    config: AppConfig,
    *,
    runner: _Runner | None = None,
    which: Callable[[str], str | None] | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Collect configured routes and redacted harness model catalogs."""
    run_command = subprocess.run if runner is None else runner
    find_executable = shutil.which if which is None else which
    env = os.environ if environment is None else environment
    user_home = Path.home() if home is None else home
    cwd = config.config_path.parent
    routes = configured_routes(config)
    opencode = _opencode_inventory(
        runner=run_command,
        which=find_executable,
        cwd=cwd,
        home=user_home,
    )
    inventory = {
        "schema_version": 1,
        "config_path": str(config.config_path),
        "configured": routes,
        "opencode": opencode,
        "codex": _codex_inventory(
            runner=run_command,
            which=find_executable,
            cwd=cwd,
            home=user_home,
            environment=env,
        ),
        "claude_code": _claude_inventory(
            runner=run_command,
            which=find_executable,
            cwd=cwd,
            home=user_home,
            environment=env,
        ),
    }
    configured_ollama_urls = dict.fromkeys(
        hunter.ollama_base_url
        for hunter in config.hunters
        if hunter.harness == "opencode"
        and hunter.provider == "ollama"
        and hunter.ollama_base_url
    )
    for base_url in configured_ollama_urls:
        assert base_url is not None
        try:
            normalized, _inventory_url, models = list_ollama_models(
                base_url,
                timeout=3,
            )
            merge_ollama_catalog(inventory, normalized, models)
        except ConfigError as exc:
            opencode["errors"].append(str(exc))
    update_inventory_configured(inventory, config)
    return inventory


def update_inventory_configured(
    inventory: dict[str, Any],
    config: AppConfig,
) -> None:
    """Refresh role assignments in an already collected catalog."""
    routes = configured_routes(config)
    inventory["configured"] = routes
    roles: dict[str, list[str]] = {}
    for route in routes:
        selector = route.get("selector")
        if isinstance(selector, str):
            roles.setdefault(selector, []).append(f"{route['role']}:{route['id']}")
    for provider in inventory["opencode"]["providers"]:
        for model in provider["models"]:
            model["configured_as"] = roles.get(model["selector"], [])
    for model in inventory["codex"]["models"]:
        model["configured_as"] = roles.get(model["selector"], [])
    for model in inventory["claude_code"]["models"]:
        model["configured_as"] = roles.get(model["selector"], [])


def available_model_selections(
    inventory: Mapping[str, Any],
) -> dict[str, ModelSelection]:
    selections: dict[str, ModelSelection] = {}
    opencode = inventory.get("opencode", {})
    if isinstance(opencode, dict):
        version = str(opencode.get("harness_version") or "unversioned")
        for provider in opencode.get("providers", []):
            if not isinstance(provider, dict) or not isinstance(provider.get("id"), str):
                continue
            for entry in provider.get("models", []):
                if not isinstance(entry, dict):
                    continue
                selector = entry.get("selector")
                model = entry.get("model")
                if isinstance(selector, str) and isinstance(model, str):
                    selection_provider = entry.get("provider", provider["id"])
                    if not isinstance(selection_provider, str):
                        continue
                    ollama_base_url = entry.get("ollama_base_url")
                    selections[selector] = ModelSelection(
                        selector,
                        "opencode",
                        selection_provider,
                        model,
                        version,
                        ollama_base_url=(
                            ollama_base_url
                            if isinstance(ollama_base_url, str)
                            else None
                        ),
                    )
    codex = inventory.get("codex", {})
    if isinstance(codex, dict):
        version = str(codex.get("harness_version") or "unversioned")
        for entry in codex.get("models", []):
            if not isinstance(entry, dict):
                continue
            selector = entry.get("selector")
            model = entry.get("model")
            efforts = entry.get("supported_efforts", [])
            if isinstance(selector, str) and isinstance(model, str):
                selections[selector] = ModelSelection(
                    selector,
                    "codex-exec",
                    "openai",
                    model,
                    version,
                    tuple(value for value in efforts if isinstance(value, str)),
                )
    claude = inventory.get("claude_code", {})
    if isinstance(claude, dict):
        version = str(claude.get("harness_version") or "unversioned")
        for entry in claude.get("models", []):
            if not isinstance(entry, dict):
                continue
            selector = entry.get("selector")
            model = entry.get("model")
            if isinstance(selector, str) and isinstance(model, str):
                selections[selector] = ModelSelection(
                    selector,
                    "claude-code",
                    "anthropic",
                    model,
                    version,
                    _CLAUDE_EFFORTS,
                )
    for route in inventory.get("configured", []):
        if not isinstance(route, dict):
            continue
        selector = route.get("selector")
        harness = route.get("harness")
        provider = route.get("provider")
        model = route.get("model")
        if not all(isinstance(value, str) for value in (selector, harness, provider, model)):
            continue
        if harness not in {"opencode", "codex", "codex-exec", "claude-code"}:
            continue
        normalized_harness = "codex-exec" if harness in _CODEX_HARNESSES else harness
        supported = route.get("supported_efforts", [])
        selections.setdefault(selector, ModelSelection(
            selector=selector,
            harness=normalized_harness,
            provider=provider,
            model=model,
            harness_version=str(route.get("harness_version") or "unversioned"),
            supported_efforts=tuple(
                value for value in supported if isinstance(value, str)
            ) if isinstance(supported, list) else (),
            ollama_base_url=(
                route.get("ollama_base_url")
                if isinstance(route.get("ollama_base_url"), str)
                else None
            ),
        ))
    return selections


def _derive_id(model: str, *, judge: bool, existing: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_.-]+", "-", model.rsplit("/", 1)[-1]).strip("-.")
    if not base or not base[0].isalnum():
        base = "model-" + base.lstrip("_.-")
    suffix = "-judge" if judge else ""
    base = (base[: 64 - len(suffix)] + suffix).strip("-.")
    candidate = base
    counter = 2
    while candidate in existing:
        tail = f"-{counter}"
        candidate = base[: 64 - len(tail)].rstrip("-.") + tail
        counter += 1
    return candidate


def _engine_settings(
    selection: ModelSelection,
    *,
    matching_route: Mapping[str, Any] | None = None,
    requested_effort: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    supported = tuple(
        value for value in selection.supported_efforts if value in EFFORTS
    )
    if matching_route is not None:
        configured_supported = matching_route.get("supported_efforts", [])
        if isinstance(configured_supported, list):
            supported = tuple(
                value for value in configured_supported if value in EFFORTS
            ) or supported
    if requested_effort is None:
        if matching_route is not None:
            selected_effort = str(matching_route["reasoning_effort"])
        elif selection.harness == "opencode":
            selected_effort = "default"
        elif "high" in supported:
            selected_effort = "high"
        else:
            selected_effort = supported[0] if supported else "high"
    else:
        selected_effort = requested_effort
    if selected_effort not in EFFORTS:
        raise ValueError(f"unsupported reasoning effort: {selected_effort}")
    if selection.harness == "claude-code" and selected_effort not in _CLAUDE_EFFORTS:
        raise ValueError(
            "Claude Code effort must be low, medium, high, xhigh, or max"
        )
    if supported and selected_effort != "default" and selected_effort not in supported:
        raise ValueError(
            f"{selection.selector} does not advertise reasoning effort "
            f"{selected_effort!r}"
        )
    if selection.harness == "opencode":
        supported = (selected_effort,)
    elif selected_effort == "default" and "default" not in supported:
        supported = ("default", *supported)
    elif not supported:
        supported = (selected_effort,)
    return selected_effort, supported


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _replace_root_integer(text: str, key: str, value: int) -> str:
    pattern = re.compile(
        rf"(?m)^([ \t]*{re.escape(key)}[ \t]*=[ \t]*)(\d[\d_]*)([^\n]*)$"
    )
    return pattern.sub(lambda match: f"{match.group(1)}{value}{match.group(3)}", text, count=1)


def _analyzer_span(text: str) -> tuple[int, int]:
    match = _ANALYZER_HEADER.search(text)
    if match is None:
        raise ValueError("configuration has no [analyzer] table")
    hard_end = len(text)
    for header in _ACTIVE_HEADER.finditer(text, match.end()):
        name = header.group(1).strip("[]")
        if name != "analyzer" and not name.startswith("analyzer."):
            hard_end = header.start()
            break
    segment = text[match.start():hard_end]
    offset = 0
    last_active_end = 0
    for line in segment.splitlines(keepends=True):
        stripped = line.strip()
        offset += len(line)
        if stripped and not stripped.startswith("#"):
            last_active_end = offset
    if not last_active_end:
        raise ValueError("configuration has an empty [analyzer] table")
    return match.start(), match.start() + last_active_end


def _engine_block(
    selection: ModelSelection,
    *,
    engine_id: str,
    effort: str,
    supported_efforts: Sequence[str],
    judge: bool,
    workers: int,
    timeout_seconds: int,
    baseline_first: bool,
) -> str:
    header = "[analyzer]" if judge else "[[hunters]]"
    lines = [
        header,
        f"id = {_toml_string(engine_id)}",
        f"harness = {_toml_string(selection.harness)}",
        f"harness_version = {_toml_string(selection.harness_version)}",
        f"provider = {_toml_string(selection.provider)}",
        f"model = {_toml_string(selection.model)}",
        f"reasoning_effort = {_toml_string(effort)}",
        f"supported_efforts = {_toml_array(supported_efforts)}",
        f"timeout_seconds = {timeout_seconds}",
    ]
    if selection.ollama_base_url is not None:
        lines.append(
            f"ollama_base_url = {_toml_string(selection.ollama_base_url)}"
        )
    if judge:
        lines.extend((
            "blind_hunter_identity = true",
            f"baseline_first = {'true' if baseline_first else 'false'}",
            "pass_env = []",
        ))
    else:
        lines.extend((
            f"workers = {workers}",
            f"subagents = {workers}",
            "pass_env = []",
        ))
    return "\n".join(lines) + "\n"


def _write_validated_config(config: AppConfig, text: str) -> None:
    path = config.config_path
    mode = path.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        load_config(temporary)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _hunter_spans(text: str) -> list[tuple[int, int]]:
    headers = [
        match
        for match in _ACTIVE_HEADER.finditer(text)
        if match.group(1) == "[[hunters]]"
    ]
    spans: list[tuple[int, int]] = []
    for match in headers:
        hard_end = len(text)
        for header in _ACTIVE_HEADER.finditer(text, match.end()):
            name = header.group(1).strip("[]")
            if name.startswith("hunters."):
                continue
            hard_end = header.start()
            break
        segment = text[match.start():hard_end]
        offset = 0
        last_active_end = 0
        for line in segment.splitlines(keepends=True):
            stripped = line.strip()
            offset += len(line)
            if stripped and not stripped.startswith("#"):
                last_active_end = offset
        if not last_active_end:
            raise ValueError("configuration contains an empty [[hunters]] table")
        spans.append((match.start(), match.start() + last_active_end))
    return spans


def apply_model_configuration(
    config: AppConfig,
    inventory: Mapping[str, Any],
    hunter_selectors: Sequence[str],
    *,
    judge_selector: str | None = None,
    judge_changed: bool = False,
    workers: int = 4,
    effort_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Atomically reconcile native hunter checkboxes and an optional judge choice.

    Command-adapter and other non-native hunters are deliberately preserved;
    the interactive catalog cannot safely reconstruct their custom commands.
    """
    if workers < 2:
        raise ValueError("new hunter workers must be at least 2")
    selections = available_model_selections(inventory)
    requested = tuple(dict.fromkeys(hunter_selectors))
    missing = [selector for selector in requested if selector not in selections]
    if missing:
        raise ValueError(
            "selected model disappeared from the discovered catalog: "
            + ", ".join(missing)
        )
    if judge_changed:
        if judge_selector is None or judge_selector not in selections:
            raise ValueError("the selected judge is unavailable")
        if selections[judge_selector].harness == "opencode":
            raise ValueError(
                "native OpenCode adjudication is not implemented; choose Codex or Claude Code"
            )

    overrides = effort_overrides or {}
    current_routes = configured_routes(config)
    route_by_selector: dict[str, Mapping[str, Any]] = {}
    for route in current_routes:
        selector = route.get("selector")
        if isinstance(selector, str):
            route_by_selector.setdefault(selector, route)

    current_hunter_selectors = {
        selector
        for hunter in config.hunters
        if (selector := _selector(hunter)) is not None
    }
    requested_set = set(requested)
    additions = [
        selector for selector in requested if selector not in current_hunter_selectors
    ]
    removed_ids = [
        hunter.id
        for hunter in config.hunters
        if (selector := _selector(hunter)) is not None and selector not in requested_set
    ]
    preserved_hunters = [
        hunter
        for hunter in config.hunters
        if (selector := _selector(hunter)) is None or selector in requested_set
    ]
    final_count = len(preserved_hunters) + len(additions)
    if not 2 <= final_count <= 26:
        raise ValueError(
            f"the configured fleet must contain 2–26 hunters; selection would contain {final_count}"
        )

    current_judge_selector = _selector(config.analyzer)
    effective_judge_change = judge_changed and judge_selector != current_judge_selector
    if not additions and not removed_ids and not effective_judge_change:
        return {
            "changed": False,
            "added": [],
            "removed": [],
            "judge": current_judge_selector,
            "hunter_count": len(config.hunters),
            "config_path": str(config.config_path),
        }

    original = config.config_path.read_text(encoding="utf-8")
    spans = _hunter_spans(original)
    if len(spans) != len(config.hunters):
        raise ValueError(
            "could not map parsed hunters back to their TOML tables; profile was not modified"
        )
    updated = original
    for index in reversed(range(len(config.hunters))):
        if config.hunters[index].id not in removed_ids:
            continue
        start, end = spans[index]
        updated = updated[:start] + updated[end:]

    existing_ids = {
        hunter.id for hunter in preserved_hunters
    } | {config.analyzer.id}
    if config.secondary_analyzer is not None:
        existing_ids.add(config.secondary_analyzer.id)
    added: list[dict[str, str]] = []
    blocks: list[str] = []
    for selector in additions:
        selection = selections[selector]
        engine_id = _derive_id(selection.model, judge=False, existing=existing_ids)
        existing_ids.add(engine_id)
        selected_effort, supported = _engine_settings(
            selection,
            matching_route=route_by_selector.get(selector),
            requested_effort=overrides.get(selector),
        )
        blocks.append(_engine_block(
            selection,
            engine_id=engine_id,
            effort=selected_effort,
            supported_efforts=supported,
            judge=False,
            workers=workers,
            timeout_seconds=14_400,
            baseline_first=False,
        ))
        added.append({
            "id": engine_id,
            "selector": selector,
            "reasoning_effort": selected_effort,
        })
    if blocks:
        analyzer_start, _ = _analyzer_span(updated)
        updated = (
            updated[:analyzer_start]
            + "\n".join(blocks)
            + "\n"
            + updated[analyzer_start:]
        )

    old_count = len(config.hunters)
    if config.max_parallel_hunters == old_count or config.max_parallel_hunters < final_count:
        updated = _replace_root_integer(updated, "max_parallel_hunters", final_count)
    if config.minimum_answers == old_count or config.minimum_answers > final_count:
        updated = _replace_root_integer(updated, "minimum_answers", final_count)

    judge_change: dict[str, str] | None = None
    if effective_judge_change:
        assert judge_selector is not None
        selection = selections[judge_selector]
        selected_effort, supported = _engine_settings(
            selection,
            matching_route=route_by_selector.get(judge_selector),
            requested_effort=overrides.get(judge_selector),
        )
        judge_id = _derive_id(selection.model, judge=True, existing=existing_ids)
        start, end = _analyzer_span(updated)
        replacement = _engine_block(
            selection,
            engine_id=judge_id,
            effort=selected_effort,
            supported_efforts=supported,
            judge=True,
            workers=1,
            timeout_seconds=config.analyzer.timeout_seconds,
            baseline_first=config.analyzer.baseline_first,
        )
        updated = updated[:start] + replacement + updated[end:]
        judge_change = {
            "id": judge_id,
            "selector": judge_selector,
            "reasoning_effort": selected_effort,
        }

    _write_validated_config(config, updated)
    return {
        "changed": True,
        "added": added,
        "removed": removed_ids,
        "judge": judge_change,
        "hunter_count": final_count,
        "config_path": str(config.config_path),
    }


def print_model_inventory(
    inventory: Mapping[str, Any],
    *,
    json_output: bool = False,
) -> None:
    if json_output:
        print(json.dumps(inventory, indent=2, sort_keys=True))
        return
    print(f"Swarmerz profile: {inventory['config_path']}")
    print("Configured roles:")
    for route in inventory.get("configured", []):
        selector = route.get("selector") or f"{route['provider']}/{route['model']}"
        print(
            f"  {route['role']:15} {route['id']:20} {selector} "
            f"(effort={route['reasoning_effort']})"
        )

    opencode = inventory.get("opencode", {})
    version = opencode.get("version") or "not installed"
    print(f"\nOpenCode: {version}")
    if not opencode.get("installed"):
        print("  unavailable")
    for provider in opencode.get("providers", []):
        status = (
            "credential configured"
            if provider.get("credential_configured")
            else "available from project/environment"
        )
        print(f"  {provider['name']} [{provider['id']}] — {status}")
        for model in provider.get("models", []):
            roles = model.get("configured_as", [])
            suffix = f"  [{', '.join(roles)}]" if roles else ""
            print(f"    {model['selector']}{suffix}")
    for error in opencode.get("errors", []):
        print(f"  note: {error}")

    codex = inventory.get("codex", {})
    version = codex.get("version") or "not installed"
    print(f"\nCodex: {version}")
    if not codex.get("installed"):
        print("  unavailable")
    elif codex.get("models"):
        freshness = "fresh" if codex.get("catalog_fresh") else "stale"
        print(f"  authenticated account catalog ({freshness})")
        for model in codex["models"]:
            efforts = ",".join(model.get("supported_efforts", []))
            roles = model.get("configured_as", [])
            suffix = f"  [{', '.join(roles)}]" if roles else ""
            effort_note = f"  efforts={efforts}" if efforts else ""
            print(f"    {model['selector']}{effort_note}{suffix}")
    if codex.get("note"):
        print(f"  note: {codex['note']}")

    claude = inventory.get("claude_code", {})
    version = claude.get("version") or "not installed"
    print(f"\nClaude Code: {version}")
    if not claude.get("installed"):
        print("  unavailable")
    else:
        auth = "authenticated" if claude.get("authenticated") else "not authenticated"
        print(f"  {auth}")
        for model in claude.get("models", []):
            roles = model.get("configured_as", [])
            suffix = f"  [{', '.join(roles)}]" if roles else ""
            print(f"    {model['selector']}  source={model['source']}{suffix}")
    if claude.get("note"):
        print(f"  note: {claude['note']}")
