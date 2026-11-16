# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

import asyncio
from io import StringIO

from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from swarmerz.startup import StartupApp
from swarmerz.terminal import run_tui_application


class _Output:
    def __init__(self) -> None:
        self.disable_calls = 0
        self.flush_calls = 0

    def disable_mouse_support(self) -> None:
        self.disable_calls += 1

    def flush(self) -> None:
        self.flush_calls += 1


class _Input:
    def __init__(self) -> None:
        self.read_calls = 0
        self.flush_calls = 0

    def fileno(self) -> int:
        return -1

    def read_keys(self) -> list[object]:
        self.read_calls += 1
        return [object()]

    def flush_keys(self) -> list[object]:
        self.flush_calls += 1
        return []


class _Application:
    def __init__(self) -> None:
        self.output = _Output()
        self.input = _Input()

    async def run_async(self) -> str:
        return "finished"


def test_tui_shutdown_disables_mouse_and_discards_queued_input() -> None:
    application = _Application()

    result = asyncio.run(run_tui_application(application))  # type: ignore[arg-type]

    assert result == "finished"
    assert application.output.disable_calls == 1
    assert application.output.flush_calls == 1
    assert application.input.read_calls == 1
    assert application.input.flush_calls == 1


def test_ctrl_q_discards_mouse_packet_delivered_during_terminal_teardown() -> None:
    async def exercise() -> None:
        stream = StringIO()
        output = Vt100_Output(
            stream, lambda: Size(rows=30, columns=100), enable_cpr=False
        )
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            app = StartupApp(())
            task = asyncio.create_task(app.run())
            while not app.application.is_running:
                await asyncio.sleep(0)

            pipe.send_text("\x11")  # Ctrl-Q.
            while app.application.is_running:
                await asyncio.sleep(0)

            # Reproduce the delayed SGR mouse packet that previously reached
            # Bash as visible text after the deck had left its alternate screen.
            pipe.send_text("\x1b[<51;111;25M")
            await asyncio.wait_for(task, timeout=3)

            assert pipe.read_keys() == []
            assert pipe.flush_keys() == []
            assert stream.getvalue().rfind("\x1b[?1000l") > stream.getvalue().rfind(
                "\x1b[?1000h"
            )

    asyncio.run(exercise())
