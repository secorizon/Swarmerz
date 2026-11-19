# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .orchestration.models import AppConfig, EngineConfig, ResourceLimitsConfig


@dataclass(frozen=True)
class Diagnostic:
    name: str
    status: str
    detail: str


def _clipboard_diagnostic(
    *,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
    platform_name: str,
) -> Diagnostic:
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return Diagnostic(
            "clipboard",
            "info",
            "remote session detected; copying depends on the attached terminal's OSC 52 support",
        )
    if platform_name == "darwin":
        executable = which("pbcopy")
        if executable:
            return Diagnostic("clipboard", "ok", f"macOS clipboard helper: {executable}")
        return Diagnostic(
            "clipboard",
            "warn",
            "pbcopy is unavailable; missions still run, but confirmed clipboard copying is disabled",
        )
    if platform_name.startswith("linux"):
        if env.get("WAYLAND_DISPLAY"):
            executable = which("wl-copy")
            if executable:
                return Diagnostic("clipboard", "ok", f"Wayland clipboard helper: {executable}")
            return Diagnostic(
                "clipboard",
                "warn",
                "wl-copy is unavailable; install wl-clipboard for confirmed Wayland copying",
            )
        if env.get("DISPLAY"):
            executable = which("xclip") or which("xsel")
            if executable:
                return Diagnostic("clipboard", "ok", f"X11 clipboard helper: {executable}")
            return Diagnostic(
                "clipboard",
                "warn",
                "xclip/xsel is unavailable; install either helper for confirmed X11 copying",
            )
        return Diagnostic(
            "clipboard",
            "info",
            "no desktop display detected; clipboard helpers are optional for headless operation",
        )
    return Diagnostic(
        "clipboard",
        "info",
        "no native clipboard helper is defined for this platform; mission execution is unaffected",
    )


def _platform_diagnostic(platform_name: str) -> Diagnostic:
    if platform_name.startswith("linux"):
        return Diagnostic("platform", "ok", "Linux runtime detected")
    if platform_name == "darwin":
        return Diagnostic("platform", "ok", "macOS runtime detected")
    if platform_name == "win32":
        return Diagnostic(
            "platform",
            "error",
            "native Windows is unsupported; run Swarmerz inside WSL2",
        )
    return Diagnostic(
        "platform",
        "warn",
        f"untested platform {platform_name!r}; POSIX process and filesystem semantics are required",
    )


def _resource_diagnostic(
    config: AppConfig | None,
    *,
    which: Callable[[str], str | None],
    platform_name: str,
) -> Diagnostic:
    resources = config.resources if config is not None else ResourceLimitsConfig()
    if resources.mode == "off":
        return Diagnostic(
            "resource-containment",
            "info",
            "disabled by resources.mode=off; process-group timeout cleanup remains active",
        )
    if not platform_name.startswith("linux"):
        status = "error" if resources.mode == "required" else "info"
        return Diagnostic(
            "resource-containment",
            status,
            "systemd user scopes are Linux-only; process-group timeout cleanup remains active",
        )
    systemd_run = which("systemd-run")
    systemctl = which("systemctl")
    if not systemd_run or not systemctl:
        status = "error" if resources.mode == "required" else "warn"
        return Diagnostic(
            "resource-containment",
            status,
            "systemd-run/systemctl unavailable; workloads cannot receive cgroup memory/task ceilings",
        )
    return Diagnostic(
        "resource-containment",
        "ok",
        "Linux helpers available; mission startup verifies the user manager "
        f"({resources.memory_max_bytes / 1024**3:g} GiB memory, "
        f"{resources.memory_swap_max_bytes / 1024**3:g} GiB swap, "
        f"{resources.tasks_max} tasks per harness scope)",
    )


def _dependency_diagnostics() -> list[Diagnostic]:
    checks: list[Diagnostic] = []
    for distribution in ("prompt-toolkit", "jsonschema"):
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            checks.append(
                Diagnostic("dependency:" + distribution, "error", "not installed")
            )
        else:
            checks.append(
                Diagnostic("dependency:" + distribution, "ok", f"version {version}")
            )
    return checks


def _schema_diagnostic() -> Diagnostic:
    root = Path(__file__).resolve().parent / "orchestration" / "schemas"
    expected = (
        "answer.schema.json",
        "analysis.schema.json",
        "baseline.schema.json",
        "verification.schema.json",
    )
    missing = [name for name in expected if not (root / name).is_file()]
    if missing:
        return Diagnostic(
            "artifact-schemas",
            "error",
            "missing packaged schemas: " + ", ".join(missing),
        )
    return Diagnostic("artifact-schemas", "ok", f"{len(expected)} schemas available")


def _command_available(
    token: str, which: Callable[[str], str | None]
) -> str | None:
    if "{" in token or "}" in token:
        return None
    expanded = Path(token).expanduser()
    if expanded.is_absolute() or "/" in token:
        return str(expanded) if expanded.is_file() and os.access(expanded, os.X_OK) else None
    return which(token)


def _engine_diagnostic(
    engine: EngineConfig,
    which: Callable[[str], str | None],
) -> Diagnostic:
    name = f"harness:{engine.id}"
    if engine.command:
        executable = _command_available(engine.command[0], which)
        if executable:
            return Diagnostic(name, "ok", f"command adapter: {executable}")
        return Diagnostic(
            name,
            "error",
            f"command adapter executable is unavailable: {engine.command[0]}",
        )

    executable_name = {
        "opencode": "opencode",
        "codex": "codex",
        "codex-exec": "codex",
        "claude-code": "claude",
        "cline": "cline",
    }.get(engine.harness)
    if executable_name is None:
        return Diagnostic(name, "error", f"unsupported harness: {engine.harness}")
    executable = which(executable_name)
    if executable_name == "opencode" and executable is None:
        standard = Path.home() / ".opencode" / "bin" / "opencode"
        if standard.is_file() and os.access(standard, os.X_OK):
            executable = str(standard)
    if executable:
        return Diagnostic(name, "ok", f"{engine.harness}: {executable}")
    if (
        engine.harness_version not in {"", "unversioned", "latest"}
        and (npx := which("npx"))
    ):
        return Diagnostic(
            name,
            "ok",
            f"{engine.harness} {engine.harness_version} can be resolved through {npx}",
        )
    return Diagnostic(
        name,
        "error",
        f"{executable_name} is unavailable and no pinned npx fallback can be used",
    )


def collect_diagnostics(
    config: AppConfig | None = None,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    platform_name: str | None = None,
) -> tuple[Diagnostic, ...]:
    environment = os.environ if env is None else env
    find_executable = shutil.which if which is None else which
    current_platform = sys.platform if platform_name is None else platform_name
    checks: list[Diagnostic] = [
        Diagnostic(
            "python",
            "ok" if sys.version_info >= (3, 12) else "error",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        ),
        _platform_diagnostic(current_platform),
        _resource_diagnostic(
            config,
            which=find_executable,
            platform_name=current_platform,
        ),
        _clipboard_diagnostic(
            env=environment,
            which=find_executable,
            platform_name=current_platform,
        ),
        *_dependency_diagnostics(),
        _schema_diagnostic(),
    ]
    if config is None:
        checks.append(
            Diagnostic(
                "configuration",
                "info",
                "not checked; pass --config to validate a deployment profile",
            )
        )
    else:
        checks.append(
            Diagnostic(
                "configuration",
                "ok",
                f"{config.config_path} ({len(config.hunters)} hunters)",
            )
        )
        engines: list[EngineConfig] = [*config.hunters, config.analyzer]
        if config.secondary_analyzer is not None:
            engines.append(config.secondary_analyzer)
        checks.extend(_engine_diagnostic(engine, find_executable) for engine in engines)
    return tuple(checks)


def diagnostics_exit_code(checks: Sequence[Diagnostic]) -> int:
    return 1 if any(check.status == "error" for check in checks) else 0


def print_diagnostics(
    checks: Sequence[Diagnostic], *, json_output: bool = False
) -> None:
    errors = sum(check.status == "error" for check in checks)
    warnings = sum(check.status == "warn" for check in checks)
    if json_output:
        print(json.dumps({
            "status": "error" if errors else "ready",
            "errors": errors,
            "warnings": warnings,
            "checks": [asdict(check) for check in checks],
        }, indent=2, sort_keys=True))
        return
    labels = {"ok": "OK", "warn": "WARN", "error": "ERROR", "info": "INFO"}
    print("Swarmerz doctor")
    for check in checks:
        print(f"[{labels[check.status]:5}] {check.name}: {check.detail}")
    print(
        f"Result: {errors} error(s), {warnings} warning(s). "
        "Warnings do not block mission execution."
    )


def clipboard_startup_warning(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    platform_name: str | None = None,
) -> str | None:
    check = _clipboard_diagnostic(
        env=os.environ if env is None else env,
        which=shutil.which if which is None else which,
        platform_name=sys.platform if platform_name is None else platform_name,
    )
    if check.status != "warn":
        return None
    return f"Optional clipboard warning: {check.detail}. Run `swarmerz doctor` for details."
