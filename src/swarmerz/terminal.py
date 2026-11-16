# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import os
from typing import TypeVar

from prompt_toolkit.application import Application


_Result = TypeVar("_Result")
_MOUSE_PACKET_SETTLE_SECONDS = 0.04


async def run_tui_application(application: Application[_Result]) -> _Result:
    """Run a TUI and return stdin to the shell without queued mouse packets.

    Prompt Toolkit disables mouse reporting when its renderer stops. A terminal
    can nevertheless have already emitted one final SGR mouse packet, which is
    then interpreted by the shell after the application exits. Disable mouse
    reporting once more, allow that packet to arrive, and discard pending input
    before returning control to the caller.
    """
    try:
        return await application.run_async()
    finally:
        _disable_mouse_reporting(application)
        await asyncio.sleep(_MOUSE_PACKET_SETTLE_SECONDS)
        _discard_pending_input(application)


def _disable_mouse_reporting(application: Application[object]) -> None:
    try:
        application.output.disable_mouse_support()
        application.output.flush()
    except (AttributeError, OSError, ValueError):
        # The terminal may already be detached. Cleanup must not hide the
        # application's real result or failure.
        pass


def _discard_pending_input(application: Application[object]) -> None:
    try:
        fd = application.input.fileno()
    except (AttributeError, OSError, ValueError):
        return

    if os.name == "posix" and os.isatty(fd):
        try:
            import termios
        except ImportError:
            pass
        else:
            try:
                termios.tcflush(fd, termios.TCIFLUSH)
                return
            except (OSError, termios.error):
                pass

    # Pipe inputs are used by the renderer tests. Their reader is nonblocking,
    # so consuming once is enough to discard any packet already waiting.
    try:
        application.input.read_keys()
        application.input.flush_keys()
    except (AttributeError, OSError, ValueError):
        pass
