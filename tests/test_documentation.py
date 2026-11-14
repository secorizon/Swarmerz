# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")


def _markdown_files() -> list[Path]:
    return [
        PROJECT_ROOT / "README.md",
        *sorted((PROJECT_ROOT / "docs").glob("*.md")),
        *sorted((PROJECT_ROOT / "examples").glob("*.md")),
    ]


@pytest.mark.parametrize(
    "document",
    _markdown_files(),
    ids=lambda path: str(path.relative_to(PROJECT_ROOT)),
)
def test_local_documentation_links_resolve(document: Path) -> None:
    broken: list[str] = []
    for target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
        destination = target.strip().split(maxsplit=1)[0].strip("<>")
        if not destination or destination.startswith(("#", "http://", "https://")):
            continue
        relative = unquote(destination.split("#", 1)[0])
        if not (document.parent / relative).resolve().exists():
            broken.append(target)

    assert not broken, f"broken local links in {document}: {broken}"
