# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from pathlib import Path

from .models import HunterSpec
from .orchestration.config import load_config
from .orchestration.models import ConfigError


class SwarmerzConfigError(ValueError):
    pass


def load_hunter_specs(
    path: str | Path, overrides: list[str] | None = None
) -> tuple[HunterSpec, ...]:
    try:
        config = load_config(path, overrides)
    except (ConfigError, OSError) as exc:
        raise SwarmerzConfigError(str(exc)) from exc
    return tuple(
        HunterSpec(
            id=engine.id,
            provider=engine.provider,
            model=engine.effective_model,
            effort=engine.reasoning_effort,
            workers=engine.swarm_size,
            harness=engine.harness,
        )
        for engine in config.hunters
    )
