# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

import asyncio
import json
from pathlib import Path

import pytest

from swarmerz.cli import _parser, _resolve_config_path, _run_headless, main
from swarmerz.config import SwarmerzConfigError
from swarmerz.launchpad import _resolve_context, _suggest_session
from swarmerz.startup import StartupApp


def test_run_command_needs_no_configuration_flags() -> None:
    args = _parser().parse_args(["run"])

    assert args.config is None
    assert args.context is None
    assert args.session is None
    assert args.instruction is None


def test_run_and_analyze_expose_headless_json_mode() -> None:
    run = _parser().parse_args([
        "run", "--headless", "--json", "--instruction", "audit it",
    ])
    analyze = _parser().parse_args([
        "analyze", "--headless", "--json", "--session", "audit",
        "--round-id", "round-1",
    ])

    assert run.headless is True
    assert run.headless_json is True
    assert run.instruction == "audit it"
    assert analyze.headless is True
    assert analyze.headless_json is True


def test_headless_run_does_not_require_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def completed(_args: object) -> int:
        return 0

    monkeypatch.setattr("swarmerz.cli._run_headless", completed)
    assert main(["run", "--headless", "--instruction", "audit it"]) == 0


def test_headless_json_keeps_progress_off_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "round"
    (run_dir / "final").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({
            "status": "completed", "session_id": "audit", "round_id": "round-1",
        }),
        encoding="utf-8",
    )

    class FakeRunner:
        def __init__(self, _config: object, _instruction: str, **kwargs: object) -> None:
            self.session_id = str(kwargs.get("session_id") or "generated")
            self.round_id = str(kwargs.get("round_id") or "generated")
            self.progress = kwargs["progress"]

        async def run(self) -> Path:
            self.progress("hunting")
            return run_dir

    config_path = tmp_path / "swarmerz.toml"
    config_path.write_text("schema_version = 1\n", encoding="utf-8")
    monkeypatch.setattr("swarmerz.cli._resolve_config_path", lambda _path: config_path)
    monkeypatch.setattr("swarmerz.cli.load_config", lambda *_args: object())
    monkeypatch.setattr("swarmerz.cli.AuditRunner", FakeRunner)
    args = _parser().parse_args([
        "run", "--headless", "--json", "--session", "audit",
        "--instruction", "audit it",
    ])

    assert asyncio.run(_run_headless(args)) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "completed"
    assert payload["report"] == str(run_dir / "final" / "report.md")
    assert captured.err == "hunting\n"


def test_chat_command_uses_the_public_swarmerz_cli() -> None:
    args = _parser().parse_args([
        "chat", "--session", "audit", "--round-id", "round-1",
    ])

    assert args.command == "chat"
    assert args.session == "audit"
    assert args.round_id == "round-1"


def test_cache_maintenance_is_part_of_the_public_cli() -> None:
    args = _parser().parse_args(["cache", "prune", "--json"])

    assert args.command == "cache"
    assert args.action == "prune"
    assert args.json_output is True


def test_doctor_is_part_of_the_public_cli() -> None:
    args = _parser().parse_args(["doctor", "--json"])

    assert args.command == "doctor"
    assert args.json_output is True


def test_startup_surfaces_optional_platform_helper_warning() -> None:
    warning = "Optional clipboard warning: install wl-clipboard."

    assert StartupApp((), warning=warning).notice == warning


def test_default_config_prefers_working_directory(tmp_path: Path) -> None:
    config = tmp_path / "swarmerz.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")

    assert _resolve_config_path(None, cwd=tmp_path, project_root=tmp_path / "tool") == config


def test_default_config_finds_the_swarmerz_checkout(tmp_path: Path) -> None:
    tool = tmp_path / "Swarmerz"
    tool.mkdir()
    config = tool / "swarmerz.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")

    working = tmp_path / "work"
    working.mkdir()
    resolved = _resolve_config_path(None, cwd=working, project_root=tool)

    assert resolved == config


def test_missing_explicit_config_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(SwarmerzConfigError, match="cannot read configuration"):
        _resolve_config_path(Path("missing.toml"), cwd=tmp_path)


def test_context_is_resolved_relative_to_launch_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    assert _resolve_context("source", tmp_path) == source
    assert _resolve_context("", tmp_path) is None


def test_context_rejects_files_and_missing_paths(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        _resolve_context(str(file_path), tmp_path)
    with pytest.raises(ValueError, match="cannot be resolved"):
        _resolve_context("missing", tmp_path)


def test_session_suggestion_is_safe_and_context_aware(tmp_path: Path) -> None:
    assert _suggest_session("/work/OpenSSL Review!", tmp_path) == "OpenSSL-Review"
    assert _suggest_session(None, tmp_path) == tmp_path.name
