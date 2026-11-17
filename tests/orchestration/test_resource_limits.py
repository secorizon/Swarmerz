# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import signal
import subprocess

import pytest

from swarmerz.orchestration.models import ConfigError, ResourceLimitsConfig
from swarmerz.orchestration.resource_limits import (
    ResourceLimiter,
    ResourceSupport,
    probe_systemd_user_scope,
)


def test_auto_mode_falls_back_without_changing_the_command() -> None:
    limiter = ResourceLimiter(
        ResourceLimitsConfig(),
        support_probe=lambda: ResourceSupport(False, "no user manager"),
    )

    status = limiter.activate()
    invocation = limiter.prepare(("tool", "argument"), role="hunter")

    assert status["enforced"] is False
    assert status["backend"] == "process-group-only"
    assert invocation.argv == ("tool", "argument")
    assert invocation.unit_name is None


def test_required_mode_fails_when_systemd_scope_is_unavailable() -> None:
    limiter = ResourceLimiter(
        ResourceLimitsConfig(mode="required"),
        support_probe=lambda: ResourceSupport(False, "no delegation"),
    )

    with pytest.raises(ConfigError, match="no delegation"):
        limiter.activate()


def test_linux_scope_wraps_without_reinterpreting_command_arguments() -> None:
    support = ResourceSupport(
        True,
        "ready",
        systemd_run="/usr/bin/systemd-run",
        systemctl="/usr/bin/systemctl",
        env_command="/usr/bin/env",
    )
    limiter = ResourceLimiter(
        ResourceLimitsConfig(
            memory_max_bytes=3 * 1024**3,
            memory_swap_max_bytes=1024**3,
            tasks_max=321,
        ),
        support_probe=lambda: support,
        host_environment={
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "UNRELATED_SECRET": "must-not-pass",
        },
    )
    original = ("agent", "audit $TMPDIR literally", "token;still-one-argument")
    restricted = {"PATH": "/usr/bin", "HOME": "/tmp/private-home"}

    invocation = limiter.prepare(
        original,
        role="hunter/one",
        runtime_seconds=90,
        environment=restricted,
    )

    assert invocation.unit_name is not None
    assert invocation.argv[0] == "/usr/bin/systemd-run"
    assert "--expand-environment=no" in invocation.argv
    assert "--property=MemoryMax=3221225472" in invocation.argv
    assert "--property=MemorySwapMax=1073741824" in invocation.argv
    assert "--property=TasksMax=321" in invocation.argv
    assert "--property=RuntimeMaxSec=90s" in invocation.argv
    separator = invocation.argv.index("--")
    child = invocation.argv[separator + 1 :]
    assert child[:6] == (
        "/usr/bin/env",
        "-u",
        "DBUS_SESSION_BUS_ADDRESS",
        "-u",
        "XDG_RUNTIME_DIR",
        "--",
    )
    assert child[6:] == original
    spawn_environment = invocation.spawn_environment(restricted)
    assert spawn_environment["DBUS_SESSION_BUS_ADDRESS"].endswith("/bus")
    assert spawn_environment["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert spawn_environment["HOME"] == "/tmp/private-home"
    assert "UNRELATED_SECRET" not in spawn_environment


def test_probe_checks_the_same_cgroup_controls_used_at_runtime() -> None:
    observed: list[tuple[str, ...]] = []

    def find(name: str) -> str | None:
        return (
            f"/usr/bin/{name}"
            if name in {"systemd-run", "systemctl", "env", "true"}
            else None
        )

    def run(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    support = probe_systemd_user_scope(
        platform_name="linux",
        which=find,
        run=run,
    )

    assert support.available
    command = observed[0]
    assert "--property=OOMPolicy=kill" in command
    assert "--property=KillMode=control-group" in command
    assert "--property=RuntimeMaxSec=60s" in command
    separator = command.index("--")
    assert command[separator + 1 : separator + 7] == (
        "/usr/bin/env",
        "-u",
        "DBUS_SESSION_BUS_ADDRESS",
        "-u",
        "XDG_RUNTIME_DIR",
        "--",
    )


def test_scope_signal_targets_every_process_in_the_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = ResourceSupport(
        True,
        "ready",
        systemd_run="/usr/bin/systemd-run",
        systemctl="/usr/bin/systemctl",
        env_command="/usr/bin/env",
    )
    limiter = ResourceLimiter(
        ResourceLimitsConfig(),
        support_probe=lambda: support,
        host_environment={},
    )
    invocation = limiter.prepare(("agent",), role="hunter")

    class Process:
        async def wait(self) -> int:
            return 0

    calls: list[tuple[object, ...]] = []

    async def spawn(*args: object, **_kwargs: object) -> Process:
        calls.append(args)
        return Process()

    monkeypatch.setattr(
        "swarmerz.orchestration.resource_limits.asyncio.create_subprocess_exec",
        spawn,
    )

    assert asyncio.run(limiter.signal_scope(invocation, signal.SIGTERM))
    assert calls[0][0:5] == (
        "/usr/bin/systemctl",
        "--user",
        "kill",
        "--kill-whom=all",
        "--signal=SIGTERM",
    )
