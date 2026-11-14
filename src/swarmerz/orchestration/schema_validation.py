# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .models import ConfigError


_SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas"
_SCHEMA_NAMES = {
    "answer.schema.json",
    "analysis.schema.json",
    "baseline.schema.json",
    "verification.schema.json",
}


@lru_cache(maxsize=len(_SCHEMA_NAMES))
def _validator(schema_name: str) -> Draft202012Validator:
    if schema_name not in _SCHEMA_NAMES:
        raise ConfigError(f"unknown artifact schema: {schema_name}")
    path = _SCHEMA_ROOT / schema_name
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot load artifact schema {path}: {exc}") from exc
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_error(value: Any, schema_name: str) -> str | None:
    """Return the first stable, path-qualified schema error, if any."""
    errors = sorted(
        _validator(schema_name).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return None
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{schema_name} {location}: {error.message}"


def require_schema(value: Any, schema_name: str, *, where: str) -> None:
    problem = schema_error(value, schema_name)
    if problem is not None:
        raise ConfigError(f"invalid structured artifact at {where}: {problem}")

