# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from collections.abc import Iterable

from prompt_toolkit.formatted_text import FormattedText


SLOGAN = "CONSENSUS IS THE EXPLOIT."
BRAND = "SECORIZON - SWARMERZ"


def render_brand_header(
    metadata: Iterable[tuple[str, str]],
) -> FormattedText:
    """Render one static, left-aligned Secorizon and Swarmerz identity."""
    fragments: list[tuple[str, str]] = [
        ("class:brand.banner", f"  {BRAND}"),
        ("class:brand.shadow", f"  //  {SLOGAN}"),
        ("", "\n"),
    ]
    fragments.extend(metadata)
    return FormattedText(fragments)
