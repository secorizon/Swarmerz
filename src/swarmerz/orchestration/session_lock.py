# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import fcntl
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from .models import ConfigError


class SessionLock:
    """Crash-safe, same-host session lock with a human-readable live marker."""

    def __init__(self, session_dir: Path, session_id: str) -> None:
        self.session_dir = session_dir
        self.session_id = session_id
        self.lock_path = session_dir / ".running.lock"
        self.marker_dir = session_dir / ".running"
        self._descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> None:
        if self.acquired:
            return
        self.session_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise ConfigError(f"unsafe session lock file: {self.lock_path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise ConfigError(
                f"session already has a running round: {self.session_id}"
            ) from exc

        try:
            self._remove_stale_marker()
            self.marker_dir.mkdir(mode=0o700)
            owner = self.marker_dir / "owner.json"
            owner.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "started_at": datetime.now(UTC).isoformat(),
                        "lock": str(self.lock_path),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.ftruncate(descriptor, 0)
            os.write(
                descriptor,
                f"pid={os.getpid()} started={datetime.now(UTC).isoformat()}\n".encode(),
            )
            self._descriptor = descriptor
        except BaseException:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise

    def _remove_stale_marker(self) -> None:
        if not self.marker_dir.exists() and not self.marker_dir.is_symlink():
            return
        if self.marker_dir.is_symlink() or not self.marker_dir.is_dir():
            raise ConfigError(f"unsafe stale session marker: {self.marker_dir}")
        entries = list(self.marker_dir.iterdir())
        if any(entry.name != "owner.json" or entry.is_symlink() for entry in entries):
            raise ConfigError(
                f"refusing unexpected contents in stale session marker: {self.marker_dir}"
            )
        owner = self.marker_dir / "owner.json"
        owner.unlink(missing_ok=True)
        self.marker_dir.rmdir()

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        try:
            owner = self.marker_dir / "owner.json"
            owner.unlink(missing_ok=True)
            if self.marker_dir.is_dir() and not self.marker_dir.is_symlink():
                self.marker_dir.rmdir()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            self._descriptor = None

