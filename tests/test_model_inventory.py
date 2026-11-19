# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

import swarmerz.model_inventory as model_inventory
from swarmerz.model_inventory import (
    apply_model_configuration,
    collect_model_inventory,
    merge_ollama_catalog,
    print_model_inventory,
)
from swarmerz.orchestration.config import load_config


def _profile(path: Path, *, baseline_first: bool = False) -> Path:
    path.write_text(
        f'''schema_version = 1
output_root = "./runs"
max_parallel_hunters = 2
minimum_answers = 2

[[hunters]]
id = "alpha"
harness = "command"
harness_version = "1"
provider = "local"
model = "alpha-model"
reasoning_effort = "high"
supported_efforts = ["high"]
timeout_seconds = 60
workers = 2
command = ["/bin/true"]

[[hunters]]
id = "beta"
harness = "command"
harness_version = "1"
provider = "local"
model = "beta-model"
reasoning_effort = "high"
supported_efforts = ["high"]
timeout_seconds = 60
workers = 2
command = ["/bin/true"]

[analyzer]
id = "old-judge"
harness = "command"
harness_version = "1"
provider = "local"
model = "judge-model"
reasoning_effort = "high"
supported_efforts = ["high"]
timeout_seconds = 120
blind_hunter_identity = true
baseline_first = {str(baseline_first).lower()}
command = ["/bin/true"]

# Optional verifier template remains after analyzer replacement.
#[secondary_analyzer]
#id = "verifier"
''',
        encoding="utf-8",
    )
    return path


def _inventory() -> dict[str, object]:
    return {
        "configured": [],
        "opencode": {
            "harness_version": "2.3.0",
            "providers": [{
                "id": "alibaba-token-plan",
                "models": [{
                    "model": "qwen4-max-preview",
                    "selector": "opencode/alibaba-token-plan/qwen4-max-preview",
                }, {
                    "model": "deepseek-v4.1-flash-1017",
                    "selector": "opencode/alibaba-token-plan/deepseek-v4.1-flash-1017",
                }],
            }],
        },
        "codex": {
            "harness_version": "0.170.0",
            "models": [{
                "model": "gpt-6.2-sol",
                "selector": "codex/gpt-6.2-sol",
                "supported_efforts": ["low", "medium", "high", "xhigh", "max"],
            }],
        },
        "claude_code": {"harness_version": "2.1.328", "models": []},
    }


def test_inventory_discovers_models_without_serializing_credentials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = load_config(_profile(tmp_path / "swarmerz.toml"))
    codex_root = tmp_path / ".codex"
    codex_root.mkdir()
    (codex_root / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": "CODEX-SECRET"}),
        encoding="utf-8",
    )
    (codex_root / "models_cache.json").write_text(
        json.dumps({
            "fetched_at": datetime.now(UTC).isoformat(),
            "models": [{
                "slug": "gpt-6.2-sol",
                "display_name": "GPT-6.2-Sol",
                "supported_reasoning_levels": [{"effort": "high"}],
            }],
        }),
        encoding="utf-8",
    )
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps({
            "model": "claude-mythos-5-2",
            "env": {"ANTHROPIC_API_KEY": "CLAUDE-SECRET"},
        }),
        encoding="utf-8",
    )

    executables = {
        "opencode": "/fake/opencode",
        "codex": "/fake/codex",
        "claude": "/fake/claude",
    }

    def fake_which(name: str) -> str | None:
        return executables.get(name)

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        if command == ("/fake/opencode", "--version"):
            output = "opencode v2.3.0\n"
        elif command[1:3] == ("auth", "list"):
            output = json.dumps([{
                "id": "alibaba-token-plan",
                "name": "Alibaba Token Plan",
                "connections": [{"token": "OPENCODE-SECRET"}],
            }])
        elif command[1] == "models":
            output = "alibaba-token-plan/qwen4-max-preview\n"
        elif command == ("/fake/codex", "--version"):
            output = "codex-cli 0.170.0\n"
        elif command == ("/fake/claude", "--version"):
            output = "2.1.328 (Claude Code)\n"
        elif command[1:3] == ("auth", "status"):
            output = json.dumps({
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "email": "private@example.test",
                "orgId": "PRIVATE-ORG",
            })
        elif command == ("/fake/claude", "--help"):
            output = (
                "--model <model> Use an alias for the latest model "
                "(e.g. 'fable', 'opus', or 'sonnet') or a model's full name.\n"
            )
        else:  # pragma: no cover - makes a new subprocess an immediate test failure
            raise AssertionError(command)
        return subprocess.CompletedProcess(argv, 0, output, "stderr SECRET")

    inventory = collect_model_inventory(
        config,
        runner=fake_run,
        which=fake_which,
        environment={"CODEX_HOME": str(codex_root)},
        home=tmp_path,
    )
    rendered = json.dumps(inventory)
    assert "OPENCODE-SECRET" not in rendered
    assert "CODEX-SECRET" not in rendered
    assert "CLAUDE-SECRET" not in rendered
    assert "private@example.test" not in rendered
    assert "PRIVATE-ORG" not in rendered
    assert inventory["opencode"]["providers"][0]["name"] == "Alibaba Token Plan"
    assert inventory["codex"]["models"][0]["selector"] == "codex/gpt-6.2-sol"
    assert {model["model"] for model in inventory["claude_code"]["models"]} == {
        "claude-mythos-5-2", "fable", "haiku", "opus", "sonnet",
    }
    haiku = next(
        model for model in inventory["claude_code"]["models"]
        if model["model"] == "haiku"
    )
    assert haiku["source"] == "documented_alias"
    assert inventory["claude_code"]["account_catalog_available"] is False

    print_model_inventory(inventory, json_output=True)
    assert "OPENCODE-SECRET" not in capsys.readouterr().out


def test_interactive_add_updates_full_fleet_counts(
    tmp_path: Path,
) -> None:
    path = _profile(tmp_path / "swarmerz.toml")
    config = load_config(path)

    change = apply_model_configuration(
        config,
        _inventory(),
        ("opencode/alibaba-token-plan/qwen4-max-preview",),
        workers=3,
        effort_overrides={
            "opencode/alibaba-token-plan/qwen4-max-preview": "max",
        },
    )

    updated = load_config(path)
    assert change["changed"] is True
    assert len(updated.hunters) == 3
    assert updated.max_parallel_hunters == 3
    assert updated.minimum_answers == 3
    qwen = updated.hunters[-1]
    assert (qwen.provider, qwen.model) == (
        "alibaba-token-plan", "qwen4-max-preview",
    )
    assert qwen.swarm_size == 3
    assert "# Optional verifier template" in path.read_text(encoding="utf-8")


def test_discovered_remote_ollama_model_keeps_its_server_url(
    tmp_path: Path,
) -> None:
    path = _profile(tmp_path / "swarmerz.toml")
    config = load_config(path)
    inventory = _inventory()

    selectors = merge_ollama_catalog(
        inventory,
        "192.168.8.167",
        ("qwen3:32b", "granite-code:latest"),
    )
    selected = next(
        selector for selector in selectors if selector.endswith("/qwen3:32b")
    )
    assert selected.startswith("opencode/ollama@")

    apply_model_configuration(config, inventory, (selected,), workers=2)

    updated = load_config(path)
    ollama = next(hunter for hunter in updated.hunters if hunter.provider == "ollama")
    assert ollama.model == "qwen3:32b"
    assert ollama.ollama_base_url == "http://192.168.8.167:11434/v1"


def test_inventory_rediscovers_an_ollama_url_already_in_the_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _profile(tmp_path / "swarmerz.toml")
    text = path.read_text(encoding="utf-8").replace(
        '''id = "alpha"
harness = "command"
harness_version = "1"
provider = "local"
model = "alpha-model"
reasoning_effort = "high"
supported_efforts = ["high"]
timeout_seconds = 60
workers = 2
command = ["/bin/true"]''',
        '''id = "ollama-qwen"
harness = "opencode"
harness_version = "2.3.0"
provider = "ollama"
model = "qwen3:32b"
reasoning_effort = "default"
supported_efforts = ["default"]
timeout_seconds = 60
workers = 2
ollama_base_url = "http://192.168.8.167:11434/v1"''',
        1,
    )
    path.write_text(text, encoding="utf-8")
    calls: list[tuple[str, int]] = []

    def fake_catalog(url: str, *, timeout: int = 8):
        calls.append((url, timeout))
        return (
            url,
            "http://192.168.8.167:11434/api/tags",
            ("granite-code:latest", "qwen3:32b"),
        )

    monkeypatch.setattr(model_inventory, "list_ollama_models", fake_catalog)
    inventory = collect_model_inventory(
        load_config(path),
        which=lambda _name: None,
        home=tmp_path,
    )

    assert calls == [("http://192.168.8.167:11434/v1", 3)]
    provider = next(
        provider for provider in inventory["opencode"]["providers"]
        if provider["name"] == "Ollama @ 192.168.8.167:11434"
    )
    models = {entry["model"]: entry for entry in provider["models"]}
    assert set(models) == {"granite-code:latest", "qwen3:32b"}
    assert models["qwen3:32b"]["configured_as"] == ["hunter:ollama-qwen"]


def test_interactive_judge_choice_preserves_baseline_and_trailing_template(
    tmp_path: Path,
) -> None:
    path = _profile(tmp_path / "swarmerz.toml", baseline_first=True)
    config = load_config(path)

    change = apply_model_configuration(
        config,
        _inventory(),
        (),
        judge_selector="codex/gpt-6.2-sol",
        judge_changed=True,
        effort_overrides={"codex/gpt-6.2-sol": "xhigh"},
    )

    updated = load_config(path)
    assert change["changed"] is True
    assert updated.analyzer.harness == "codex-exec"
    assert updated.analyzer.model == "gpt-6.2-sol"
    assert updated.analyzer.reasoning_effort == "xhigh"
    assert updated.analyzer.baseline_first is True
    assert updated.analyzer.timeout_seconds == 120
    assert "# Optional verifier template" in path.read_text(encoding="utf-8")


def test_interactive_opencode_choice_cannot_replace_native_judge(tmp_path: Path) -> None:
    path = _profile(tmp_path / "swarmerz.toml")
    config = load_config(path)
    before = path.read_bytes()

    with pytest.raises(ValueError, match="OpenCode adjudication is not implemented"):
        apply_model_configuration(
            config,
            _inventory(),
            (),
            judge_selector="opencode/alibaba-token-plan/qwen4-max-preview",
            judge_changed=True,
        )

    assert path.read_bytes() == before


def test_interactive_reconcile_preserves_custom_hunter_and_applies_one_atomic_profile(
    tmp_path: Path,
) -> None:
    path = _profile(tmp_path / "swarmerz.toml")
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '''id = "alpha"
harness = "command"
harness_version = "1"
provider = "local"
model = "alpha-model"
reasoning_effort = "high"
supported_efforts = ["high"]
timeout_seconds = 60
workers = 2
command = ["/bin/true"]''',
        '''id = "qwen-old"
harness = "opencode"
harness_version = "2.3.0"
provider = "alibaba-token-plan"
model = "qwen4-max-preview"
reasoning_effort = "max"
supported_efforts = ["max"]
timeout_seconds = 60
workers = 2''',
        1,
    )
    path.write_text(text, encoding="utf-8")
    config = load_config(path)

    change = apply_model_configuration(
        config,
        _inventory(),
        ("opencode/alibaba-token-plan/deepseek-v4.1-flash-1017",),
        judge_selector="codex/gpt-6.2-sol",
        judge_changed=True,
        workers=3,
    )

    updated = load_config(path)
    assert change["changed"] is True
    assert change["removed"] == ["qwen-old"]
    assert [hunter.id for hunter in updated.hunters if hunter.harness == "command"] == ["beta"]
    deepseek = next(hunter for hunter in updated.hunters if hunter.harness == "opencode")
    assert deepseek.model == "deepseek-v4.1-flash-1017"
    assert deepseek.swarm_size == 3
    assert updated.analyzer.harness == "codex-exec"
    assert updated.analyzer.model == "gpt-6.2-sol"
    assert "# Optional verifier template" in path.read_text(encoding="utf-8")
