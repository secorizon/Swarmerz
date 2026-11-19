# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from swarmerz.cli import main
from swarmerz.doctor import (
    Diagnostic,
    _engine_diagnostic,
    clipboard_startup_warning,
    collect_diagnostics,
    diagnostics_exit_code,
)


def _without_helpers(_name: str) -> None:
    return None


def test_missing_wayland_clipboard_helper_is_a_non_blocking_warning() -> None:
    checks = collect_diagnostics(
        env={"WAYLAND_DISPLAY": "wayland-0"},
        which=_without_helpers,
        platform_name="linux",
    )

    clipboard = next(check for check in checks if check.name == "clipboard")
    assert clipboard.status == "warn"
    assert "install wl-clipboard" in clipboard.detail
    assert diagnostics_exit_code(checks) == 0
    assert "Optional clipboard warning" in (
        clipboard_startup_warning(
            env={"WAYLAND_DISPLAY": "wayland-0"},
            which=_without_helpers,
            platform_name="linux",
        )
        or ""
    )


def test_macos_uses_pbcopy_without_requiring_linux_helpers() -> None:
    def find(name: str) -> str | None:
        return "/usr/bin/pbcopy" if name == "pbcopy" else None

    checks = collect_diagnostics(env={}, which=find, platform_name="darwin")

    platform = next(check for check in checks if check.name == "platform")
    clipboard = next(check for check in checks if check.name == "clipboard")
    assert platform.status == "ok"
    assert clipboard == Diagnostic(
        "clipboard", "ok", "macOS clipboard helper: /usr/bin/pbcopy"
    )
    assert clipboard_startup_warning(
        env={}, which=find, platform_name="darwin"
    ) is None


def test_doctor_is_noninteractive_and_emits_machine_readable_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "swarmerz.cli.collect_diagnostics",
        lambda _config: (
            Diagnostic("clipboard", "warn", "optional helper is unavailable"),
        ),
    )

    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["warnings"] == 1
    assert payload["errors"] == 0


def test_doctor_returns_failure_only_for_required_components(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "swarmerz.cli.collect_diagnostics",
        lambda _config: (Diagnostic("dependency:jsonschema", "error", "not installed"),),
    )

    assert main(["doctor", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["errors"] == 1


def test_doctor_detects_native_claude_code() -> None:
    engine = SimpleNamespace(
        id="claude",
        command=(),
        harness="claude-code",
        harness_version="2.1.328",
    )

    diagnostic = _engine_diagnostic(
        engine,
        lambda name: "/home/test/.local/bin/claude" if name == "claude" else None,
    )

    assert diagnostic == Diagnostic(
        "harness:claude", "ok", "claude-code: /home/test/.local/bin/claude"
    )
