# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import ConfigError, StorageConfig


_SAFE_PART = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")
_MARKER_NAME = ".swarmerz-cache.json"
_LOCK_NAME = ".storage.lock"
_MAINTENANCE_NAME = ".maintenance.json"
_MARKER = {"format": "swarmerz-cache", "schema_version": 1}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_run_path(run_id: str) -> Path:
    parts = run_id.split("/")
    if parts and all(_SAFE_PART.fullmatch(part) for part in parts):
        return Path(*parts)
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:20]
    return Path(f"run-{digest}")


def _active_name(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24] + ".json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tree_size(path: Path) -> int:
    total = 0
    if not path.exists() or path.is_symlink():
        return 0
    for directory, names, files in os.walk(path, followlinks=False):
        for name in files:
            candidate = Path(directory) / name
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_symlink():
                try:
                    total += candidate.lstat().st_size
                except OSError:
                    pass
    return total


def _newest_use(path: Path) -> float:
    newest = 0.0
    if not path.exists() or path.is_symlink():
        return newest
    for directory, names, files in os.walk(path, followlinks=False):
        for name in [*names, *files]:
            candidate = Path(directory) / name
            try:
                metadata = candidate.stat(follow_symlinks=False)
            except OSError:
                continue
            newest = max(newest, metadata.st_atime, metadata.st_mtime)
    return newest


def _make_directories_writable(path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
        return
    for directory, names, _files in os.walk(path, followlinks=False):
        directory_path = Path(directory)
        try:
            directory_path.chmod(directory_path.stat().st_mode | 0o700)
        except OSError:
            continue
        for name in names:
            candidate = directory_path / name
            if candidate.is_symlink():
                continue
            try:
                candidate.chmod(candidate.stat().st_mode | 0o700)
            except OSError:
                continue


def _remove_tree(path: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved = path.resolve(strict=False)
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise ConfigError(f"refusing cache deletion outside {resolved_root}: {resolved}")
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        _make_directories_writable(path)
        shutil.rmtree(path)


def remove_managed_subtree(path: Path, root: Path) -> None:
    """Remove one validated subtree without relaxing the surrounding root."""
    _remove_tree(path, root)


def _remove_empty_directories(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        return
    for directory, _names, _files in os.walk(root, topdown=False, followlinks=False):
        candidate = Path(directory)
        if candidate == root:
            continue
        try:
            candidate.rmdir()
        except OSError:
            continue


def _unlink_cache_entry(path: Path) -> None:
    """Remove one cache entry without permanently relaxing read-only module dirs."""
    parent = path.parent
    original_mode = parent.stat().st_mode & 0o7777
    changed = not bool(original_mode & 0o200)
    if changed:
        parent.chmod(original_mode | 0o300)
    try:
        path.unlink()
    finally:
        if changed and parent.exists():
            parent.chmod(original_mode)


def managed_environment(config: StorageConfig, run_id: str, role: str) -> dict[str, str]:
    run_path = _safe_run_path(run_id)
    role_name = role if _SAFE_PART.fullmatch(role) else hashlib.sha256(role.encode()).hexdigest()[:20]
    scratch = config.cache_root / "scratch" / run_path / "roles" / role_name
    shared = config.cache_root / "shared"
    temporary = scratch / "tmp"
    return {
        "SWARMERZ_CACHE_ROOT": str(config.cache_root),
        "SWARMERZ_SCRATCH_DIR": str(scratch),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "XDG_CACHE_HOME": str(shared / "xdg"),
        "GOCACHE": str(shared / "go-build"),
        "GOMODCACHE": str(shared / "go-mod"),
        "NUGET_PACKAGES": str(shared / "nuget"),
        "NPM_CONFIG_CACHE": str(shared / "npm"),
        "npm_config_cache": str(shared / "npm"),
        "PIP_CACHE_DIR": str(shared / "pip"),
        "UV_CACHE_DIR": str(shared / "uv"),
        "CCACHE_DIR": str(shared / "ccache"),
        "DOTNET_CLI_HOME": str(scratch / "dotnet-home"),
        "CARGO_TARGET_DIR": str(scratch / "cargo-target"),
    }


def prepare_managed_environment(environment: dict[str, str]) -> None:
    root_value = environment.get("SWARMERZ_CACHE_ROOT")
    if not root_value:
        return
    root = Path(root_value).resolve(strict=False)
    for name in (
        "SWARMERZ_SCRATCH_DIR", "TMPDIR", "XDG_CACHE_HOME", "GOCACHE",
        "GOMODCACHE", "NUGET_PACKAGES", "NPM_CONFIG_CACHE", "PIP_CACHE_DIR",
        "UV_CACHE_DIR", "CCACHE_DIR", "DOTNET_CLI_HOME", "CARGO_TARGET_DIR",
    ):
        value = environment.get(name)
        if not value:
            continue
        path = Path(value).resolve(strict=False)
        if path == root or not path.is_relative_to(root):
            raise OSError(f"managed environment path escapes cache root: {name}={path}")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)


class StorageManager:
    def __init__(self, config: StorageConfig) -> None:
        self.config = config
        self.root = config.cache_root
        self.run_id: str | None = None
        self.run_root: Path | None = None
        self.active_path: Path | None = None

    def _validate_root(self) -> None:
        resolved = self.root.resolve(strict=False)
        if resolved == Path("/") or resolved == Path.home().resolve():
            raise ConfigError(f"unsafe Swarmerz cache root: {resolved}")
        if self.root.exists() and self.root.is_symlink():
            raise ConfigError(f"Swarmerz cache root must not be a symlink: {self.root}")

    def _ensure_root(self) -> None:
        self._validate_root()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        marker = self.root / _MARKER_NAME
        if marker.exists():
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"invalid Swarmerz cache marker: {marker}") from exc
            if value != _MARKER:
                raise ConfigError(f"refusing unrecognized cache directory: {self.root}")
            return
        unexpected = [
            path for path in self.root.iterdir()
            if path.name not in {_LOCK_NAME, _MARKER_NAME + ".tmp"}
        ]
        if unexpected:
            raise ConfigError(
                f"refusing to claim nonempty unmarked cache directory: {self.root}"
            )
        _atomic_json(marker, _MARKER)

    def _require_existing_root(self) -> bool:
        self._validate_root()
        if not self.root.exists():
            return False
        marker = self.root / _MARKER_NAME
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"refusing unmarked cache directory: {self.root}") from exc
        if value != _MARKER:
            raise ConfigError(f"refusing unrecognized cache directory: {self.root}")
        return True

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._ensure_root()
        with (self.root / _LOCK_NAME).open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _live_active(self, *, remove_stale: bool) -> list[dict[str, Any]]:
        active_dir = self.root / "active"
        if not active_dir.is_dir():
            return []
        live: list[dict[str, Any]] = []
        for marker in active_dir.glob("*.json"):
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
                pid = int(value.get("pid", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pid = 0
                value = {}
            if _pid_alive(pid):
                live.append({**value, "marker": str(marker)})
            elif remove_stale:
                marker.unlink(missing_ok=True)
        return live

    def _maintenance_due(self, now: float) -> bool:
        marker = self.root / _MAINTENANCE_NAME
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            completed_at = float(value.get("completed_at_epoch", 0))
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return True
        return now - completed_at >= self.config.maintenance_interval_hours * 3600

    def _scratch_runs(self) -> list[tuple[Path, dict[str, Any]]]:
        scratch = self.root / "scratch"
        if not scratch.is_dir():
            return []
        records: list[tuple[Path, dict[str, Any]]] = []
        for marker in scratch.rglob(".run.json"):
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = {}
            records.append((marker.parent, value))
        return records

    def _prune_locked(self, *, now: float) -> dict[str, Any]:
        if self._live_active(remove_stale=True):
            raise ConfigError("cannot prune the Swarmerz cache while a run is active")
        before = _tree_size(self.root / "shared") + _tree_size(self.root / "scratch")
        deleted_bytes = 0
        deleted_files = 0
        deleted_scratch_runs = 0

        scratch_cutoff = now - self.config.failed_scratch_retention_days * 86400
        for run_root, record in self._scratch_runs():
            last_activity = float(record.get("last_activity_epoch", 0) or 0)
            if not last_activity:
                last_activity = _newest_use(run_root)
            if last_activity and last_activity < scratch_cutoff:
                size = _tree_size(run_root)
                _remove_tree(run_root, self.root)
                deleted_bytes += size
                deleted_scratch_runs += 1

        shared = self.root / "shared"
        cache_cutoff = now - self.config.max_cache_age_days * 86400
        candidates: list[tuple[float, int, Path]] = []
        if shared.is_dir():
            for directory, names, files in os.walk(shared, followlinks=False):
                for name in files:
                    path = Path(directory) / name
                    try:
                        metadata = path.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    last_use = max(metadata.st_atime, metadata.st_mtime)
                    if path.is_symlink() or last_use < cache_cutoff:
                        try:
                            _unlink_cache_entry(path)
                            deleted_bytes += metadata.st_size
                            deleted_files += 1
                        except OSError:
                            continue
                    else:
                        candidates.append((last_use, metadata.st_size, path))
                for name in names:
                    path = Path(directory) / name
                    if path.is_symlink():
                        try:
                            _unlink_cache_entry(path)
                            deleted_files += 1
                        except OSError:
                            pass
            _remove_empty_directories(shared)

        current = _tree_size(shared) + _tree_size(self.root / "scratch")
        target = int(self.config.max_cache_bytes * 0.9)
        if current > self.config.max_cache_bytes:
            scratch_records = sorted(
                self._scratch_runs(),
                key=lambda item: float(item[1].get("last_activity_epoch", 0) or 0),
            )
            for run_root, _record in scratch_records:
                if current <= target:
                    break
                size = _tree_size(run_root)
                _remove_tree(run_root, self.root)
                current -= size
                deleted_bytes += size
                deleted_scratch_runs += 1
            if current > target:
                for _last_use, size, path in sorted(candidates, key=lambda item: item[0]):
                    if current <= target:
                        break
                    try:
                        _unlink_cache_entry(path)
                    except OSError:
                        continue
                    current -= size
                    deleted_bytes += size
                    deleted_files += 1
                _remove_empty_directories(shared)

        after = _tree_size(shared) + _tree_size(self.root / "scratch")
        _remove_empty_directories(self.root / "scratch")
        report = {
            "before_bytes": before,
            "after_bytes": after,
            "deleted_bytes": max(deleted_bytes, before - after),
            "deleted_files": deleted_files,
            "deleted_scratch_runs": deleted_scratch_runs,
            "completed_at_epoch": now,
        }
        _atomic_json(self.root / _MAINTENANCE_NAME, report)
        return report

    def activate(self, run_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock():
            live = self._live_active(remove_stale=True)
            report = None
            if not live and self._maintenance_due(now):
                report = self._prune_locked(now=now)
            active_dir = self.root / "active"
            active_dir.mkdir(exist_ok=True, mode=0o700)
            active_path = active_dir / _active_name(run_id)
            _atomic_json(active_path, {
                "run_id": run_id,
                "pid": os.getpid(),
                "started_at_epoch": now,
            })
            run_root = self.root / "scratch" / _safe_run_path(run_id)
            run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _atomic_json(run_root / ".run.json", {
                "run_id": run_id,
                "status": "active",
                "pid": os.getpid(),
                "last_activity_epoch": now,
            })
            self.run_id = run_id
            self.run_root = run_root
            self.active_path = active_path
            return report

    def deactivate(self, *, successful: bool) -> None:
        if self.run_id is None or self.run_root is None or self.active_path is None:
            return
        now = time.time()
        with self._lock():
            if successful:
                _remove_tree(self.run_root, self.root)
                _remove_empty_directories(self.root / "scratch")
            elif self.run_root.is_dir():
                _atomic_json(self.run_root / ".run.json", {
                    "run_id": self.run_id,
                    "status": "retained_after_failure",
                    "pid": os.getpid(),
                    "last_activity_epoch": now,
                })
            self.active_path.unlink(missing_ok=True)
        self.run_id = None
        self.run_root = None
        self.active_path = None

    def status(self) -> dict[str, Any]:
        if not self._require_existing_root():
            return {
                "root": str(self.root), "exists": False, "shared_bytes": 0,
                "scratch_bytes": 0, "total_bytes": 0, "active_runs": [],
            }
        return {
            "root": str(self.root),
            "exists": True,
            "shared_bytes": _tree_size(self.root / "shared"),
            "scratch_bytes": _tree_size(self.root / "scratch"),
            "total_bytes": (
                _tree_size(self.root / "shared") + _tree_size(self.root / "scratch")
            ),
            "active_runs": self._live_active(remove_stale=False),
            "max_cache_bytes": self.config.max_cache_bytes,
        }

    def prune(self) -> dict[str, Any]:
        with self._lock():
            return self._prune_locked(now=time.time())

    def clear(self) -> dict[str, Any]:
        if not self._require_existing_root():
            return {"deleted_bytes": 0, "root": str(self.root)}
        with self._lock():
            if self._live_active(remove_stale=True):
                raise ConfigError("cannot clear the Swarmerz cache while a run is active")
            deleted = _tree_size(self.root / "shared") + _tree_size(self.root / "scratch")
            for name in ("shared", "scratch"):
                _remove_tree(self.root / name, self.root)
            (self.root / _MAINTENANCE_NAME).unlink(missing_ok=True)
            return {"deleted_bytes": deleted, "root": str(self.root)}


def format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"
