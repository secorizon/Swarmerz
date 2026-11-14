# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from pathlib import Path

import pytest

from swarmerz.config import SwarmerzConfigError, load_hunter_specs


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_hunter_specs_come_from_the_unified_configuration() -> None:
    specs = load_hunter_specs(
        PROJECT_ROOT / "swarmerz.toml",
        ["hunters.0.model=deepseek-test-snapshot"],
    )

    deepseek = specs[0]
    assert deepseek.id == "deepseek"
    assert deepseek.model == "deepseek-test-snapshot"
    assert deepseek.provider == "alibaba-token-plan"
    assert deepseek.workers == 4
    assert deepseek.harness == "opencode"


def test_missing_configuration_is_actionable(tmp_path: Path) -> None:
    with pytest.raises(SwarmerzConfigError, match="config file does not exist"):
        load_hunter_specs(tmp_path / "missing.toml")
