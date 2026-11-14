# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from swarmerz.orchestration.models import ConfigError, StorageConfig
from swarmerz.orchestration.storage import (
    StorageManager,
    managed_environment,
    prepare_managed_environment,
)


class StorageTests(unittest.TestCase):
    def _config(self, root: Path, *, max_bytes: int = 1024 * 1024) -> StorageConfig:
        return StorageConfig(
            cache_root=root / "cache",
            max_cache_bytes=max_bytes,
            max_cache_age_days=1,
            failed_scratch_retention_days=1,
            maintenance_interval_hours=1,
            cleanup_successful_workspaces=True,
        )

    def test_managed_environment_keeps_temp_and_tool_caches_under_cache_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            environment = managed_environment(config, "session/round", "hunter")
            prepare_managed_environment(environment)

            root = config.cache_root.resolve()
            for name in (
                "TMPDIR", "GOCACHE", "GOMODCACHE", "NUGET_PACKAGES",
                "NPM_CONFIG_CACHE", "PIP_CACHE_DIR", "UV_CACHE_DIR",
                "DOTNET_CLI_HOME", "CARGO_TARGET_DIR",
            ):
                path = Path(environment[name]).resolve()
                self.assertTrue(path.is_relative_to(root), name)
                self.assertTrue(path.is_dir(), name)

    def test_successful_run_drops_scratch_but_keeps_shared_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            manager = StorageManager(config)
            manager.activate("session/round")
            environment = managed_environment(config, "session/round", "hunter")
            prepare_managed_environment(environment)
            shared = Path(environment["GOCACHE"]) / "object.a"
            shared.write_bytes(b"shared")
            scratch = Path(environment["TMPDIR"]) / "build.o"
            scratch.write_bytes(b"scratch")
            self.assertEqual(len(manager.status()["active_runs"]), 1)

            manager.deactivate(successful=True)

            self.assertTrue(shared.is_file())
            self.assertFalse((config.cache_root / "scratch" / "session" / "round").exists())
            self.assertEqual(manager.status()["active_runs"], [])

    def test_prune_removes_expired_failed_scratch_and_read_only_cache_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            manager = StorageManager(config)
            manager.activate("failed/round")
            environment = managed_environment(config, "failed/round", "hunter")
            prepare_managed_environment(environment)
            cached = Path(environment["GOMODCACHE"]) / "module" / "source.go"
            cached.parent.mkdir(parents=True)
            cached.write_text("package module\n", encoding="utf-8")
            old = time.time() - 3 * 86400
            os.utime(cached, (old, old))
            cached.parent.chmod(0o555)
            manager.deactivate(successful=False)

            run_marker = config.cache_root / "scratch" / "failed" / "round" / ".run.json"
            record = json.loads(run_marker.read_text(encoding="utf-8"))
            record["last_activity_epoch"] = old
            run_marker.write_text(json.dumps(record), encoding="utf-8")
            os.utime(run_marker, (old, old))

            report = manager.prune()

            self.assertGreater(report["deleted_bytes"], 0)
            self.assertFalse(cached.exists())
            self.assertFalse(run_marker.parent.exists())

    def test_size_cap_evicts_oldest_shared_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), max_bytes=100)
            manager = StorageManager(config)
            manager.activate("size/round")
            manager.deactivate(successful=True)
            shared = config.cache_root / "shared" / "tool"
            shared.mkdir(parents=True)
            older = shared / "older.bin"
            newer = shared / "newer.bin"
            older.write_bytes(b"a" * 80)
            newer.write_bytes(b"b" * 80)
            now = time.time()
            os.utime(older, (now - 60, now - 60))
            os.utime(newer, (now, now))

            manager.prune()

            self.assertFalse(older.exists())
            self.assertLessEqual(manager.status()["total_bytes"], 90)

    def test_clear_refuses_active_run_and_only_clears_marked_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            manager = StorageManager(config)
            manager.activate("active/round")
            with self.assertRaisesRegex(ConfigError, "run is active"):
                manager.clear()
            manager.deactivate(successful=False)
            report = manager.clear()
            self.assertGreaterEqual(report["deleted_bytes"], 0)
            self.assertTrue((config.cache_root / ".swarmerz-cache.json").is_file())

            unmarked = root / "unmarked"
            unmarked.mkdir()
            (unmarked / "keep.txt").write_text("do not claim", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "unmarked"):
                StorageManager(StorageConfig(cache_root=unmarked)).status()
            self.assertTrue((unmarked / "keep.txt").is_file())


if __name__ == "__main__":
    unittest.main()
