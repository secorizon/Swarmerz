# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import sys

from prompt_toolkit.output import Output


async def copy_to_clipboard(text: str, output: Output) -> str:
    """Use a desktop clipboard helper, or request the terminal's clipboard."""
    commands: list[tuple[str, ...]] = []
    local_desktop = not (
        os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")
    )
    if local_desktop:
        if sys.platform == "darwin":
            commands.append(("pbcopy",))
        elif os.environ.get("WAYLAND_DISPLAY"):
            commands.append(("wl-copy",))
        elif os.environ.get("DISPLAY"):
            commands.extend([
                ("xclip", "-selection", "clipboard"),
                ("xsel", "--clipboard", "--input"),
            ])
    for command in commands:
        if not shutil.which(command[0]):
            continue
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.communicate(text.encode("utf-8")), timeout=2)
            if process.returncode == 0:
                return "system"
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    # VTE-based terminals (including Ptyxis and GNOME Terminal) deliberately
    # ignore OSC 52. Do not report an unverifiable request as a successful copy.
    if local_desktop and os.environ.get("VTE_VERSION"):
        return "unavailable"

    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sequence = f"\x1b]52;c;{payload}\x07"
    if os.environ.get("TMUX"):
        sequence = "\x1bPtmux;" + sequence.replace("\x1b", "\x1b\x1b") + "\x1b\\"
    output.write_raw(sequence)
    output.flush()
    # OSC 52 writes have no acknowledgment, so do not claim that the terminal
    # accepted the request.
    return "terminal"
