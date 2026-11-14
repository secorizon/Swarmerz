# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

import asyncio
import base64
from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from swarmerz.app import SwarmerzApp
from swarmerz.launchpad import LaunchpadApp
from swarmerz.models import HunterSpec, MissionState


def _screen_text(app: SwarmerzApp | LaunchpadApp) -> str:
    screen = app.application.renderer.last_rendered_screen
    assert screen is not None
    return "\n".join(
        "".join(row[x].char for x in sorted(row))
        for _, row in sorted(screen.data_buffer.items())
    )


@pytest.mark.parametrize("preflight", [False, True])
@pytest.mark.parametrize("columns", [60, 120])
def test_f6_copies_current_pane_without_releasing_mouse(
    preflight: bool, columns: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("swarmerz.clipboard.shutil.which", lambda _: None)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("VTE_VERSION", raising=False)

    async def exercise() -> None:
        stream = StringIO()
        output = Vt100_Output(
            stream, lambda: Size(rows=30, columns=columns), enable_cpr=False
        )
        specs = (HunterSpec("alpha", "provider", "model", "high", 1),)
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            if preflight:
                app = LaunchpadApp(config_path=Path("swarmerz.toml"), specs=specs)
            else:
                state = MissionState("test", "wait", specs)
                for index in range(80):
                    state.add_event("alpha", f"existing event {index}")
                app = SwarmerzApp(state, lambda **_: None)
                app.workspace.vertical_scroll = 3
            app.command.text = "unfinished command"
            app.application.refresh_interval = None
            rendered: asyncio.Queue[None] = asyncio.Queue()
            app.application.after_render += lambda _: rendered.put_nowait(None)
            task = asyncio.create_task(app.application.run_async())

            async def next_render() -> None:
                await asyncio.wait_for(rendered.get(), timeout=3)

            try:
                await next_render()
                assert "\x1b[?1000h" in stream.getvalue()
                assert "F6" in _screen_text(app)
                expected = (
                    app.command.text
                    if isinstance(app, LaunchpadApp)
                    else app.workspace_control.buffer.text
                )
                start = stream.tell()
                pipe.send_text("\x1b[17~")  # F6, through the actual input decoder.
                await next_render()
                assert app.application.mouse_support()
                assert "COPY MODE" not in _screen_text(app)
                assert "\x1b[?1000l" not in stream.getvalue()[start:]
                payload = base64.b64encode(expected.encode()).decode()
                assert f"\x1b]52;c;{payload}\x07" in stream.getvalue()[start:]
                assert app.application.clipboard.get_data().text == expected
                assert app.command.text == "unfinished command"
            finally:
                app.application.exit()
                await asyncio.wait_for(task, timeout=3)

    asyncio.run(exercise())


@pytest.mark.parametrize("columns", [60, 100, 140])
def test_workspace_mouse_selection_copies_full_wrapped_messages(
    columns: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("swarmerz.clipboard.shutil.which", lambda _: None)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("VTE_VERSION", raising=False)

    async def exercise() -> None:
        stream = StringIO()
        output = Vt100_Output(
            stream, lambda: Size(rows=35, columns=columns), enable_cpr=False
        )
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            state = MissionState(
                "test", "wait", (HunterSpec("alpha", "provider", "model", "high", 1),)
            )
            message = "SELECT_START " + "long text " * 15 + "Ω漢😀\nTAIL_END"
            for index in range(50):
                state.add_event("alpha", f"old message {index}")
            state.add_event("alpha", message)
            app = SwarmerzApp(state, lambda **_: None)
            app.command.text = "unfinished command"
            app.application.refresh_interval = None
            rendered: asyncio.Queue[None] = asyncio.Queue()
            app.application.after_render += lambda _: rendered.put_nowait(None)
            task = asyncio.create_task(app.application.run_async())

            async def next_render() -> None:
                await asyncio.wait_for(rendered.get(), timeout=3)

            def locate(needle: str) -> tuple[int, int]:
                screen = app.application.renderer.last_rendered_screen
                assert screen is not None
                for y, row in sorted(screen.data_buffer.items()):
                    line = "".join(row[x].char or " " for x in range(columns))
                    x = line.find(needle)
                    if x >= 0:
                        return x, y
                raise AssertionError(f"Not visible: {needle}")

            def mouse(button: int, x: int, y: int, *, release: bool = False) -> None:
                pipe.send_text(f"\x1b[<{button};{x + 1};{y + 1}{'m' if release else 'M'}")

            try:
                await next_render()
                assert message in app.workspace_control.buffer.text
                start_x, start_y = locate("SELECT_START")
                end_x, end_y = locate("TAIL_END")
                end_x += len("TAIL_END")
                assert end_y > start_y + 1  # The message really wraps on screen.

                mouse(0, start_x, start_y)
                await next_render()
                app._on_progress("[alpha] new message during drag")
                await next_render()
                assert "new message during drag" not in app.workspace_control.buffer.text
                mouse(32, end_x, end_y)
                mouse(0, end_x, end_y, release=True)
                await next_render()
                assert app.workspace_control.selected_text == message
                assert app.workspace_control.buffer.read_only()
                assert "F6 / Ctrl-C" in _screen_text(app)
                screen = app.application.renderer.last_rendered_screen
                assert screen is not None
                assert "class:selected" in screen.data_buffer[start_y][start_x].style

                for key in ("\x1b[17~", "\x03", "\x1b[18~"):  # F6, Ctrl-C, F7.
                    start = stream.tell()
                    pipe.send_text(key)
                    await next_render()
                    expected = base64.b64encode(message.encode()).decode()
                    assert f"\x1b]52;c;{expected}\x07" in stream.getvalue()[start:]
                    assert app.application.clipboard.get_data().text == message
                    assert app.application.is_running

                pipe.send_text("\x1b")  # Esc restores command editing and live messages.
                await next_render()
                assert app.application.layout.current_control is app.command.control
                assert not app.workspace_control.holding_selection
                assert "new message during drag" in app.workspace_control.buffer.text
                assert app.command.text == "unfinished command"

                # Wheel scrolling still routes to the workspace after selection.
                x, y = locate("TAIL_END")
                mouse(65, x, y)
                while app.workspace.vertical_scroll == 0:
                    await next_render()
                assert app.workspace.vertical_scroll > 0

                expected = app.workspace_control.buffer.text
                pipe.send_text("\x1b[18~")
                await next_render()
                assert app.application.clipboard.get_data().text == expected
            finally:
                app.application.exit()
                await asyncio.wait_for(task, timeout=3)

    asyncio.run(exercise())
