# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import swarmerz.model_configurator as model_configurator

from swarmerz.model_configurator import ModelConfiguratorApp
from swarmerz.orchestration.config import load_config

from test_model_inventory import _inventory, _profile


def test_model_configurator_stages_checkboxes_and_judge_before_review(
    tmp_path: Path,
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
    app = ModelConfiguratorApp(config, _inventory())

    assert app.options[0].selection.model == "qwen4-max-preview"

    qwen = next(
        index for index, option in enumerate(app.options)
        if option.selection.model == "qwen4-max-preview"
    )
    deepseek = next(
        index for index, option in enumerate(app.options)
        if option.selection.model == "deepseek-v4.1-flash-1017"
    )
    codex = next(
        index for index, option in enumerate(app.options)
        if option.selection.model == "gpt-6.2-sol"
    )

    app.index = qwen
    app._toggle_hunter()
    app.index = deepseek
    app._toggle_hunter()
    app.index = codex
    app._set_judge()
    app._open_review()

    assert app.screen == "review"
    assert app._staged_hunter_count() == 2  # one preserved command adapter + DeepSeek
    assert app.judge_selector == "codex/gpt-6.2-sol"
    assert app.judge_changed is True
    additions, removals = app._changes()
    assert [option.selection.model for option in additions] == [
        "deepseek-v4.1-flash-1017"
    ]
    assert removals == ["qwen-old"]


def test_model_configurator_filters_claude_models_and_fits_last_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = _profile(tmp_path / "swarmerz.toml")
    inventory = _inventory()
    inventory["claude_code"]["models"] = [
        {
            "model": model,
            "selector": f"claude-code/{model}",
            "source": "documented_alias",
        }
        for model in ("fable", "haiku", "opus", "sonnet")
    ]
    app = ModelConfiguratorApp(load_config(path), inventory)

    assert [option.selection.harness for option in app.options] == [
        "claude-code", "claude-code", "claude-code", "claude-code",
        "codex-exec", "opencode", "opencode",
    ]

    app._cycle_filter(1)
    assert app._FILTERS[app.filter_index][0] == "CLAUDE"
    assert [option.selection.model for option in app.visible_options] == [
        "fable", "haiku", "opus", "sonnet",
    ]

    monkeypatch.setattr(
        model_configurator,
        "get_app",
        lambda: SimpleNamespace(output=SimpleNamespace(get_size=lambda: SimpleNamespace(rows=24))),
    )
    app.index = len(app.visible_options) - 1
    rendered = "".join(fragment for _style, fragment in app._render_models())
    assert "sonnet" in rendered
    assert app._page_size() == 14
