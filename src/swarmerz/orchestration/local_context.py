# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import re
from pathlib import Path

from .models import ConfigError


# Explicit filesystem paths only. URLs and ordinary prose remain arbitrary instructions.
_PATH = re.compile(r"(?<![\w:/])(?P<path>(?:~/|\.\.?/|/)[^\s\"'`<>|,;()\[\]{}]+)")
_LINE_SUFFIX = re.compile(r":\d+$")


def instruction_for_workspace(instruction: str, context_source: Path | None) -> str:
    """Map existing local paths to the frozen context, or fail before paid work.

    This is deliberately conservative: it does not infer a context from arbitrary
    prose or URLs. The source instruction is still retained unchanged in artifacts.
    """
    source = context_source.resolve() if context_source is not None else None

    def replace(match: re.Match[str]) -> str:
        candidate = match.group("path")
        path_text = candidate.rstrip(".!?:")
        path_text = _LINE_SUFFIX.sub("", path_text)
        suffix = candidate[len(path_text):]
        if not path_text:
            return candidate
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved = path.resolve()

        if source is not None and resolved.is_relative_to(source):
            relative = resolved.relative_to(source)
            mapped = "context" if relative == Path(".") else f"context/{relative.as_posix()}"
            return mapped + suffix
        if not resolved.exists():
            return candidate
        if source is None:
            raise ConfigError(
                f"instruction references local path {resolved} outside hunter workspaces; "
                "start a new session with --context set to that folder (or its parent)"
            )
        raise ConfigError(
            f"instruction references local path {resolved} outside frozen context {source}; "
            "use a new session with a context that contains every required local path"
        )

    return _PATH.sub(replace, instruction)
