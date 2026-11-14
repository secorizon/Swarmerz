# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from swarmerz.orchestration.codex_models import check_chatgpt_codex_models
from swarmerz.orchestration.models import ConfigError, EngineConfig


def _engine(model: str) -> EngineConfig:
    return EngineConfig(
        id="codex", harness="codex-exec", harness_version="0.154.0",
        provider="openai", model=model, model_snapshot=None,
        reasoning_effort="xhigh", supported_efforts=("xhigh",), timeout_seconds=60,
    )


class CodexModelCatalogTests(unittest.TestCase):
    def _catalog(self, root: Path, fetched_at: datetime) -> None:
        (root / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
        (root / "models_cache.json").write_text(json.dumps({
            "fetched_at": fetched_at.isoformat(),
            "models": [{
                "slug": "gpt-5.6-sol",
                "supported_reasoning_levels": [{"effort": "xhigh"}],
            }],
        }), encoding="utf-8")

    def test_fresh_chatgpt_catalog_blocks_unavailable_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._catalog(Path(directory), datetime.now(UTC))
            with mock.patch.dict(os.environ, {"CODEX_HOME": directory}):
                with self.assertRaisesRegex(ConfigError, "gpt-6-sol is not in"):
                    check_chatgpt_codex_models((_engine("gpt-6-sol"),))
                self.assertEqual(
                    check_chatgpt_codex_models((_engine("gpt-5.6-sol"),)), ["codex"]
                )

    def test_stale_catalog_or_api_key_login_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._catalog(Path(directory), datetime.now(UTC) - timedelta(days=2))
            with mock.patch.dict(os.environ, {"CODEX_HOME": directory}):
                self.assertEqual(check_chatgpt_codex_models((_engine("gpt-6-sol"),)), [])
            self._catalog(Path(directory), datetime.now(UTC))
            (Path(directory) / "auth.json").write_text(
                json.dumps({"auth_mode": "apikey"}), encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": directory}):
                self.assertEqual(
                    check_chatgpt_codex_models((_engine("gpt-6-sol"),)),
                    [],
                )
