# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from prompt_toolkit.application import get_app
from prompt_toolkit.formatted_text import FormattedText


def terminal_columns() -> int:
    """Return a stable width both inside the deck and in renderer tests."""
    try:
        return get_app().output.get_size().columns
    except (AttributeError, RuntimeError):
        return 120


def render_keybar(
    items: list[tuple[str, str]],
    columns: int,
    *,
    max_rows: int = 2,
) -> FormattedText:
    """Pack (key, label) hints into rows that fit `columns`.

    Items flow left to right and wrap onto the next row; once every row is
    full, remaining hints are dropped (F1 always lists the complete map).
    """
    rows: list[list[tuple[str, str]]] = [[]]
    width = 2  # leading indent
    for key, label in items:
        item_width = len(key) + (1 + len(label) if label else 0)
        separator = 3 if rows[-1] else 0
        if rows[-1] and width + separator + item_width > columns:
            if len(rows) >= max_rows:
                continue
            rows.append([])
            width = 2
            separator = 0
        if not rows[-1] and width + item_width > columns:
            continue  # single hint wider than the terminal: skip it
        rows[-1].append((key, label))
        width += separator + item_width

    fragments: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        if not row:
            continue
        if index:
            fragments.append(("class:footer", "\n"))
        fragments.append(("class:footer", "  "))
        for position, (key, label) in enumerate(row):
            if position:
                fragments.append(("class:footer", "   "))
            fragments.append(("class:footer.key", key))
            if label:
                fragments.append(("class:footer", f" {label}"))
    return FormattedText(fragments)
