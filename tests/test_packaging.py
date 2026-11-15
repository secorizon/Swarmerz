# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
)
FORBIDDEN_RELEASE_DIRECTORIES = {
    ".github",
    "__pycache__",
    "bin",
    "docs",
    "examples",
    "test",
    "tests",
}
FORBIDDEN_RELEASE_SUFFIXES = {
    ".bat",
    ".cfg",
    ".cmd",
    ".conf",
    ".crt",
    ".env",
    ".ini",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".ps1",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}
FORBIDDEN_RELEASE_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
PACKAGING_CONFIGURATION = {"pyproject.toml", "setup.cfg"}
RUNTIME_SCHEMAS = {
    "analysis.schema.json",
    "answer.schema.json",
    "baseline.schema.json",
    "verification.schema.json",
}


def _release_tree(tmp_path: Path) -> Path:
    release = tmp_path / "release-source"
    shutil.copytree(
        PROJECT_ROOT,
        release,
        ignore=shutil.ignore_patterns(
            ".pytest_cache",
            ".release-venv",
            ".venv",
            "*.egg-info",
            "__pycache__",
            "build",
            "dist",
            "swarm-runs",
        ),
    )
    return release


def _normalized_members(names: set[str], *, source_archive: bool) -> set[str]:
    if not source_archive:
        return names
    normalized: set[str] = set()
    for name in names:
        parts = Path(name).parts
        if len(parts) > 1:
            normalized.add(Path(*parts[1:]).as_posix())
    return normalized


def _assert_safe_release_members(
    names: set[str], *, source_archive: bool,
) -> None:
    normalized = _normalized_members(names, source_archive=source_archive)
    assert "swarmerz.toml" not in normalized
    for name in normalized:
        path = Path(name)
        assert not (set(path.parts) & FORBIDDEN_RELEASE_DIRECTORIES), name
        assert path.name.lower() not in FORBIDDEN_RELEASE_NAMES, name
        assert not path.name.startswith("test_"), name
        assert not path.name.endswith("_test.py"), name
        if path.suffix.lower() in FORBIDDEN_RELEASE_SUFFIXES:
            assert source_archive and name in PACKAGING_CONFIGURATION, name
        if path.suffix.lower() == ".json":
            assert "schemas" in path.parts and path.name in RUNTIME_SCHEMAS, name


def _assert_no_embedded_credentials(content: bytes, name: str) -> None:
    text = content.decode("utf-8", errors="ignore")
    for pattern in SECRET_PATTERNS:
        assert pattern.search(text) is None, f"credential-like material in {name}"


def test_release_artifacts_are_installable_and_contain_only_runtime_material(
    tmp_path: Path,
) -> None:
    source = _release_tree(tmp_path)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    env = {
        **os.environ,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
    }
    isolated_env = {
        name: value
        for name, value in env.items()
        if name not in {"PYTHONHOME", "PYTHONPATH"}
    }
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            str(source),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    wheel = next(wheelhouse.glob("swarmerz-*.whl"))

    sdist_dir = tmp_path / "sdist"
    sdist_dir.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from setuptools.build_meta import build_sdist; "
                "build_sdist(sys.argv[1])"
            ),
            str(sdist_dir),
        ],
        check=True,
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
    )
    sdist = next(sdist_dir.glob("swarmerz-*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        metadata = archive.read(metadata_name).decode()
        entry_points = archive.read(entry_points_name).decode()
        _assert_safe_release_members(names, source_archive=False)
        for name in names:
            if not name.endswith("/"):
                _assert_no_embedded_credentials(archive.read(name), name)

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = set(archive.getnames())
        _assert_safe_release_members(sdist_names, source_archive=True)
        for member in archive.getmembers():
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                _assert_no_embedded_credentials(stream.read(), member.name)

    assert "swarmerz/cli.py" in names
    assert "swarmerz/orchestration/schemas/answer.schema.json" in names
    assert "swarmerz/orchestration/schemas/analysis.schema.json" in names
    assert "Requires-Dist: jsonschema" in metadata
    assert "Requires-Dist: prompt-toolkit" in metadata
    assert "Author: Laurent Gaffié" in metadata
    assert "Project-URL: Homepage, https://secorizon.com" in metadata
    assert "swarmerz = swarmerz.cli:main" in entry_points

    environment = tmp_path / "installed"
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = environment / ("Scripts/swarmerz.exe" if os.name == "nt" else "bin/swarmerz")

    # Keep this artifact smoke test offline while making it independent of
    # whether pytest itself runs inside a virtual environment. A nested venv's
    # --system-site-packages only exposes the base interpreter, not its parent
    # venv, so share the current environment's already-installed dependencies
    # explicitly. The nested site-packages directory remains first on sys.path.
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        check=True,
        env=isolated_env,
        capture_output=True,
        text=True,
    )
    nested_site = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        env=isolated_env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (Path(nested_site) / "swarmerz-test-runtime.pth").write_text(
        sysconfig.get_path("purelib") + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            str(python),
            "-c",
            (
                "from pathlib import Path; import sys, swarmerz; "
                "assert Path(swarmerz.__file__).resolve().is_relative_to("
                "Path(sys.prefix).resolve())"
            ),
        ],
        check=True,
        env=isolated_env,
        capture_output=True,
        text=True,
    )
    version = subprocess.run(
        [str(command), "--version"],
        check=True,
        cwd=tmp_path,
        env=isolated_env,
        capture_output=True,
        text=True,
    )
    doctor = subprocess.run(
        [str(command), "doctor", "--json"],
        check=True,
        cwd=tmp_path,
        env=isolated_env,
        capture_output=True,
        text=True,
    )

    assert version.stdout.startswith("swarmerz ")
    payload = json.loads(doctor.stdout)
    assert payload["status"] == "ready"
    assert any(check["name"] == "artifact-schemas" for check in payload["checks"])
