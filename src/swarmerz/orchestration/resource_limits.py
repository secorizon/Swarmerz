# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import itertools
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Sequence

from .models import ConfigError, ResourceLimitsConfig


_USER_BUS_ENV = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")


@dataclass(frozen=True)
class ResourceSupport:
    available: bool
    detail: str
    systemd_run: str | None = None
    systemctl: str | None = None
    env_command: str | None = None


@dataclass(frozen=True)
class ResourceInvocation:
    argv: tuple[str, ...]
    unit_name: str | None = None
    environment_additions: tuple[tuple[str, str], ...] = ()

    def spawn_environment(self, base: Mapping[str, str]) -> dict[str, str]:
        environment = dict(base)
        environment.update(self.environment_additions)
        return environment


def probe_systemd_user_scope(
    *,
    platform_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> ResourceSupport:
    """Verify that this login can create a constrained transient user scope."""
    platform = sys.platform if platform_name is None else platform_name
    if not platform.startswith("linux"):
        return ResourceSupport(False, "systemd user scopes are available only on Linux")

    find = shutil.which if which is None else which
    systemd_run = find("systemd-run")
    systemctl = find("systemctl")
    true_command = find("true")
    env_command = find("env")
    if not systemd_run or not systemctl or not true_command or not env_command:
        return ResourceSupport(
            False,
            "systemd-run, systemctl, env, and true are required for Linux resource scopes",
        )

    execute = subprocess.run if run is None else run
    unit_name = (
        f"swarmerz-resource-probe-{os.getpid()}-{time.monotonic_ns()}.scope"
    )
    command = (
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        f"--unit={unit_name}",
        "--expand-environment=no",
        "--property=MemoryMax=33554432",
        "--property=MemorySwapMax=0",
        "--property=TasksMax=16",
        "--property=OOMPolicy=kill",
        "--property=KillMode=control-group",
        "--property=TimeoutStopSec=10s",
        "--property=RuntimeMaxSec=60s",
        "--",
        env_command,
        "-u",
        "DBUS_SESSION_BUS_ADDRESS",
        "-u",
        "XDG_RUNTIME_DIR",
        "--",
        true_command,
    )
    try:
        completed = execute(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ResourceSupport(
            False,
            f"systemd user-scope probe failed: {exc}",
            systemd_run,
            systemctl,
        )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        suffix = detail[-1] if detail else f"exit {completed.returncode}"
        return ResourceSupport(
            False,
            f"systemd user-scope probe failed: {suffix}",
            systemd_run,
            systemctl,
        )
    return ResourceSupport(
        True,
        "systemd user scopes accept memory, swap, task, and group-kill controls",
        systemd_run,
        systemctl,
        env_command,
    )


@lru_cache(maxsize=1)
def _cached_systemd_user_scope_probe() -> ResourceSupport:
    return probe_systemd_user_scope()


class ResourceLimiter:
    """Place harness trees outside the controller's cgroup when supported."""

    def __init__(
        self,
        config: ResourceLimitsConfig,
        *,
        support_probe: Callable[[], ResourceSupport] | None = None,
        host_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._support_probe = support_probe or _cached_systemd_user_scope_probe
        self._host_environment = (
            os.environ if host_environment is None else host_environment
        )
        self._support: ResourceSupport | None = None
        self._active = False
        self._counter = itertools.count(1)

    @property
    def enabled(self) -> bool:
        return bool(self._active and self._support and self._support.available)

    def activate(self) -> dict[str, object]:
        if self._active:
            return self.manifest()
        if self.config.mode == "off":
            self._support = ResourceSupport(False, "disabled by resources.mode=off")
            self._active = True
            return self.manifest()

        support = self._support_probe()
        self._support = support
        if not support.available and self.config.mode == "required":
            raise ConfigError(
                "resources.mode=required but Linux resource containment is unavailable: "
                f"{support.detail}"
            )
        self._active = True
        return self.manifest()

    def manifest(self) -> dict[str, object]:
        support = self._support
        return {
            "mode": self.config.mode,
            "backend": "systemd-user-scope" if self.enabled else "process-group-only",
            "enforced": self.enabled,
            "memory_max_bytes": self.config.memory_max_bytes,
            "memory_swap_max_bytes": self.config.memory_swap_max_bytes,
            "tasks_max": self.config.tasks_max,
            "detail": support.detail if support is not None else "not activated",
        }

    def prepare(
        self,
        argv: Sequence[str],
        *,
        role: str,
        runtime_seconds: int | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> ResourceInvocation:
        if not self._active:
            self.activate()
        if not self.enabled:
            return ResourceInvocation(tuple(argv))

        assert self._support is not None and self._support.systemd_run is not None
        safe_role = re.sub(r"[^a-zA-Z0-9_-]+", "-", role).strip("-")[:48]
        safe_role = safe_role or "workload"
        unit_name = (
            f"swarmerz-{os.getpid()}-{next(self._counter)}-{safe_role}.scope"
        )
        wrapped = [
            self._support.systemd_run,
            "--user",
            "--scope",
            "--quiet",
            f"--unit={unit_name}",
            "--expand-environment=no",
            f"--description=Swarmerz workload: {safe_role}",
            "--property=OOMPolicy=kill",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=10s",
        ]
        if self.config.memory_max_bytes:
            wrapped.append(
                f"--property=MemoryMax={self.config.memory_max_bytes}"
            )
        if self.config.memory_swap_max_bytes:
            wrapped.append(
                f"--property=MemorySwapMax={self.config.memory_swap_max_bytes}"
            )
        if self.config.tasks_max:
            wrapped.append(f"--property=TasksMax={self.config.tasks_max}")
        if runtime_seconds is not None:
            wrapped.append(f"--property=RuntimeMaxSec={max(60, runtime_seconds)}s")
        base_environment = environment or {}
        additions = tuple(
            (name, self._host_environment[name])
            for name in _USER_BUS_ENV
            if not base_environment.get(name) and self._host_environment.get(name)
        )
        child_argv = list(argv)
        if additions:
            if not self._support.env_command:
                raise ConfigError(
                    "Linux resource containment cannot hide launcher-only "
                    "user-bus environment variables because env is unavailable"
                )
            cleaned = [self._support.env_command]
            for name, _value in additions:
                cleaned.extend(("-u", name))
            cleaned.extend(("--", *child_argv))
            child_argv = cleaned
        wrapped.extend(("--", *child_argv))
        return ResourceInvocation(tuple(wrapped), unit_name, additions)

    async def signal_scope(
        self,
        invocation: ResourceInvocation,
        signal_to_send: signal.Signals,
    ) -> bool:
        """Signal every process in one scope; callers retain a process-group fallback."""
        if not invocation.unit_name or not self.enabled:
            return False
        assert self._support is not None and self._support.systemctl is not None
        try:
            process = await asyncio.create_subprocess_exec(
                self._support.systemctl,
                "--user",
                "kill",
                "--kill-whom=all",
                f"--signal={signal_to_send.name}",
                invocation.unit_name,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            returncode = await asyncio.wait_for(process.wait(), timeout=5)
            return returncode == 0
        except (OSError, TimeoutError):
            return False
