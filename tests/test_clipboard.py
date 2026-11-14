# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

import asyncio
import base64
from io import StringIO
from pathlib import Path
import sys

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output.vt100 import Vt100_Output

from swarmerz.clipboard import copy_to_clipboard


@pytest.mark.parametrize("exit_code", [0, 1])
def test_desktop_helper_receives_exact_text_and_failure_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    received = tmp_path / "received.txt"
    helper = tmp_path / "xclip"
    helper.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "assert sys.argv[1:] == ['-selection', 'clipboard']\n"
        f"Path({str(received)!r}).write_bytes(sys.stdin.buffer.read())\n"
        f"sys.exit({exit_code})\n"
    )
    helper.chmod(0o755)
    for name in (
        "SSH_CONNECTION", "SSH_TTY", "WAYLAND_DISPLAY", "TMUX", "VTE_VERSION"
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISPLAY", ":test")
    monkeypatch.setenv("PATH", str(tmp_path))

    stream = StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
    text = "complete message Ω漢😀\nsecond line $(literal) `text`\n"

    destination = asyncio.run(copy_to_clipboard(text, output))

    assert received.read_bytes() == text.encode("utf-8")
    if exit_code == 0:
        assert destination == "system"
        assert not stream.getvalue()
    else:
        assert destination == "terminal"
        payload = base64.b64encode(text.encode()).decode()
        assert stream.getvalue() == f"\x1b]52;c;{payload}\x07"


def test_ssh_copy_targets_terminal_instead_of_remote_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "test")
    monkeypatch.setenv("DISPLAY", ":test")
    monkeypatch.delenv("TMUX", raising=False)

    def no_remote_helper(name: str) -> None:
        raise AssertionError("Remote desktop clipboard must not be used")

    monkeypatch.setattr("swarmerz.clipboard.shutil.which", no_remote_helper)
    stream = StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)

    assert asyncio.run(copy_to_clipboard("hello", output)) == "terminal"
    assert stream.getvalue() == "\x1b]52;c;aGVsbG8=\x07"


def test_wayland_wl_copy_receives_exact_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = tmp_path / "received.txt"
    helper = tmp_path / "wl-copy"
    helper.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "assert not sys.argv[1:]\n"
        f"Path({str(received)!r}).write_bytes(sys.stdin.buffer.read())\n"
    )
    helper.chmod(0o755)
    for name in ("SSH_CONNECTION", "SSH_TTY", "DISPLAY", "TMUX", "VTE_VERSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("PATH", str(tmp_path))
    stream = StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
    text = "pane-local selection Ω漢😀\nsecond line"

    assert asyncio.run(copy_to_clipboard(text, output)) == "system"
    assert received.read_bytes() == text.encode("utf-8")
    assert not stream.getvalue()


def test_wayland_vte_without_wl_copy_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("SSH_CONNECTION", "SSH_TTY", "DISPLAY", "TMUX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("VTE_VERSION", "8400")
    monkeypatch.setattr("swarmerz.clipboard.shutil.which", lambda _: None)
    stream = StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)

    assert asyncio.run(copy_to_clipboard("hello", output)) == "unavailable"
    assert not stream.getvalue()


def test_macos_pbcopy_receives_exact_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = tmp_path / "received.txt"
    helper = tmp_path / "pbcopy"
    helper.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "assert not sys.argv[1:]\n"
        f"Path({str(received)!r}).write_bytes(sys.stdin.buffer.read())\n"
    )
    helper.chmod(0o755)
    for name in (
        "SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY", "TMUX",
        "VTE_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("swarmerz.clipboard.sys.platform", "darwin")
    stream = StringIO()
    output = Vt100_Output(stream, lambda: Size(rows=24, columns=80), enable_cpr=False)
    text = "macOS selection Ω漢😀\nsecond line"

    assert asyncio.run(copy_to_clipboard(text, output)) == "system"
    assert received.read_bytes() == text.encode("utf-8")
    assert not stream.getvalue()
