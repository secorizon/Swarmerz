# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTHOR = "Author: Laurent Gaffié, Secorizon.com"
SPDX = "SPDX-License-Identifier: GPL-3.0-only"
PRODUCT = (
    "This file is part of Swarmerz, a terminal operations deck for independent model swarms."
)
HASH_HEADER = f"# {AUTHOR}\n# {SPDX}\n# {PRODUCT}\n"
MARKDOWN_HEADER = f"<!--\n{AUTHOR}\n{SPDX}\n{PRODUCT}\n-->\n"
SCHEMA_COMMENT = f"{AUTHOR}. {SPDX}. {PRODUCT}"


def _python_files() -> list[Path]:
    return sorted((PROJECT_ROOT / "src" / "swarmerz").rglob("*.py")) + sorted(
        (PROJECT_ROOT / "tests").rglob("*.py")
    )


@pytest.mark.parametrize(
    "path",
    _python_files(),
    ids=lambda path: str(path.relative_to(PROJECT_ROOT)),
)
def test_python_files_have_attribution(path: Path) -> None:
    assert path.read_text(encoding="utf-8").startswith(HASH_HEADER)


@pytest.mark.parametrize(
    "path",
    [
        PROJECT_ROOT / ".gitignore",
        PROJECT_ROOT / "MANIFEST.in",
        PROJECT_ROOT / "pyproject.toml",
        PROJECT_ROOT / "swarmerz.toml",
        PROJECT_ROOT / "examples" / "local-ollama.toml",
        PROJECT_ROOT / ".github" / "workflows" / "ci.yml",
    ],
    ids=lambda path: str(path.relative_to(PROJECT_ROOT)),
)
def test_hash_comment_files_have_attribution(path: Path) -> None:
    assert path.read_text(encoding="utf-8").startswith(HASH_HEADER)


def test_shell_launcher_preserves_shebang_and_has_attribution() -> None:
    lines = (PROJECT_ROOT / "bin" / "swarmerz").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert "\n".join(lines[1:4]) + "\n" == HASH_HEADER


@pytest.mark.parametrize(
    "path",
    [
        *sorted(PROJECT_ROOT.glob("*.md")),
        *sorted((PROJECT_ROOT / "docs").glob("*.md")),
        *sorted((PROJECT_ROOT / "examples").glob("*.md")),
    ],
    ids=lambda path: str(path.relative_to(PROJECT_ROOT)),
)
def test_markdown_files_have_attribution(path: Path) -> None:
    assert path.read_text(encoding="utf-8").startswith(MARKDOWN_HEADER)


@pytest.mark.parametrize(
    "path",
    sorted((PROJECT_ROOT / "src" / "swarmerz" / "orchestration" / "schemas").glob("*.json")),
    ids=lambda path: path.name,
)
def test_json_schemas_have_attribution(path: Path) -> None:
    assert json.loads(path.read_text(encoding="utf-8"))["$comment"] == SCHEMA_COMMENT
