# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path

from .artifacts import read_json
from .models import AppConfig, ConfigError


_SESSION_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")


def resolve_context(config: AppConfig, override: str | Path | None) -> Path | None:
    selected = (
        Path(override).expanduser().resolve()
        if override is not None
        else config.context.path
    )
    if selected is None:
        return None
    if not selected.is_dir():
        raise ConfigError(f"context directory does not exist: {selected}")
    if config.output_root == selected or config.output_root.is_relative_to(selected):
        raise ConfigError("output_root must not be inside the context directory")
    return selected


def existing_context_source(config: AppConfig, session_id: str | None) -> Path | None:
    if session_id is None:
        return None
    if not _SESSION_ID.fullmatch(session_id):
        raise ConfigError(f"session id must match {_SESSION_ID.pattern!r}")
    session_path = config.output_root / "sessions" / session_id / "session.json"
    if not session_path.is_file():
        return None
    session = read_json(session_path)
    source = session.get("context_source") if isinstance(session, dict) else None
    return Path(source).resolve() if isinstance(source, str) and source else None


def validate_context_tree(
    source: Path, *, allow_symlinks: bool, exclude: tuple[str, ...] = ()
) -> tuple[str, ...]:
    def excluded(name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in exclude)

    skipped_symlinks: list[str] = []
    for directory, names, files in os.walk(source, followlinks=False):
        kept_directories = []
        for name in names:
            if excluded(name):
                continue
            path = Path(directory) / name
            if path.is_symlink() and not allow_symlinks:
                skipped_symlinks.append(path.relative_to(source).as_posix())
                continue
            kept_directories.append(name)
        names[:] = kept_directories
        for name in files:
            if excluded(name):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                if not allow_symlinks:
                    skipped_symlinks.append(path.relative_to(source).as_posix())
            elif not path.is_file():
                raise ConfigError(f"context contains an unsupported special file: {path}")
    return tuple(sorted(skipped_symlinks))


def context_copy_ignore(
    *, allow_symlinks: bool, exclude: tuple[str, ...]
) -> Callable[[str, list[str]], set[str]]:
    """Apply configured exclusions and safely omit links when links are disabled."""

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {
            name
            for name in names
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in exclude)
        }
        if not allow_symlinks:
            ignored.update(
                name for name in names if (Path(directory) / name).is_symlink()
            )
        return ignored

    return ignore


def context_digest(
    source: Path, progress: Callable[[int, int], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    files = 0
    size = 0
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source).as_posix().encode()
        if path.is_symlink():
            digest.update(b"L\0" + relative + b"\0" + os.readlink(path).encode() + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    if progress is not None:
                        progress(files, size)
            files += 1
        elif path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
    return digest.hexdigest()

