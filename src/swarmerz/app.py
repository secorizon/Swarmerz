# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import textwrap
from datetime import UTC, datetime
from typing import Callable

from prompt_toolkit.application import Application, get_app, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, HSplit, Layout, ScrollablePane, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.widgets import Frame, TextArea

from .backend import Backend, RunResult
from .branding import render_brand_header
from .clipboard import copy_to_clipboard
from .events import ingest_progress, ingest_report
from .models import (
    EventLevel,
    HunterPhase,
    MissionPhase,
    MissionState,
)
from .keybar import render_keybar
from .themes import initial_theme_name, next_theme_name, style_for, theme_names
from .workspace import WorkspaceControl


_STATUS_REFRESH_INTERVAL = 1.0


_PHASE_ORDER = (
    MissionPhase.SETUP,
    MissionPhase.BASELINE,
    MissionPhase.HUNTING,
    MissionPhase.QUORUM,
    MissionPhase.ANALYZING,
    MissionPhase.VERIFYING,
    MissionPhase.COMPLETE,
)


_FOOTER_FULL = [
    ("F1", "map"), ("F2", "signal"), ("F3", "reports"), ("F4", "synthesis"),
    ("F5", "mission"), ("F6", "copy"), ("F8", "Codex chat"),
    ("F9", "theme"), ("PgUp/PgDn", "scroll"), ("Tab", "focus"),
    ("Ctrl-B", "broadcast"), ("Ctrl-T", "target"), ("Ctrl-Q", "exit"),
]
_FOOTER_MEDIUM = [
    ("F1", "map"), ("F2", "signal"), ("F3", "reports"), ("F4", "synthesis"),
    ("F5", "mission"), ("F6", "copy"), ("F8", "chat"),
    ("F9", "theme"), ("PgUp/Dn", "scroll"), ("Tab", "focus"),
    ("Ctrl-B", "broadcast"), ("Ctrl-T", "target"), ("Ctrl-Q", "exit"),
]
_FOOTER_COMPACT = [
    ("F1", "map"), ("F2", "log"), ("F3", "rpt"), ("F4", "syn"), ("F5", "mis"),
    ("F6", "copy"), ("F8", "chat"), ("F9", "theme"),
    ("PgUp/Dn", "scroll"), ("Tab", "focus"), ("^B", "all"), ("^T", "target"),
    ("^Q", "exit"),
]


class _BoundedScrollablePane(ScrollablePane):
    """A virtual viewport that stays bounded while the command bus has focus."""

    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        scrollbar_width = 1 if self.show_scrollbar() else 0
        virtual_width = max(1, write_position.width - scrollbar_width)
        preferred = self.content.preferred_height(
            virtual_width, self.max_available_height
        ).preferred
        virtual_height = min(
            self.max_available_height,
            max(write_position.height, preferred),
        )
        self.vertical_scroll = max(
            0,
            min(self.vertical_scroll, virtual_height - write_position.height),
        )
        super().write_to_screen(
            screen,
            mouse_handlers,
            write_position,
            parent_style,
            erase_bg,
            z_index,
        )


class _WorkspaceWindow(Window):
    """Route wheel motion anywhere in the workspace to its virtual viewport."""

    def __init__(self, *args: object, on_scroll: Callable[[int], None], **kwargs: object) -> None:
        self._on_scroll = on_scroll
        super().__init__(*args, **kwargs)

    def _mouse_handler(self, mouse_event: MouseEvent) -> object:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._on_scroll(-3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._on_scroll(3)
            return None
        return super()._mouse_handler(mouse_event)


class SwarmerzApp:
    """Full-screen mission control for one swarm round."""

    def __init__(self, state: MissionState, backend_factory: Callable[..., Backend]) -> None:
        self.state = state
        self._backend_factory = backend_factory
        self.backend: Backend | None = None
        self._backend_task: asyncio.Task[None] | None = None
        self._ticker_task: asyncio.Task[None] | None = None
        self._pre_help_view = "activity"
        self.theme_name = initial_theme_name()
        self._show_fleet = Condition(lambda: _terminal_columns() >= 72)
        self._show_telemetry = Condition(lambda: _terminal_columns() >= 118)

        self.header = Window(
            FormattedTextControl(self._render_header),
            height=2,
            style="class:root",
        )
        self.fleet = Window(
            FormattedTextControl(self._render_fleet),
            wrap_lines=False,
            style="class:panel",
        )
        self.workspace_control = WorkspaceControl(self._render_main)
        self.main = _WorkspaceWindow(
            self.workspace_control,
            on_scroll=self._scroll_workspace,
            wrap_lines=True,
            style="class:panel class:workspace",
            dont_extend_height=True,
        )
        self.workspace = _BoundedScrollablePane(
            self.main,
            keep_cursor_visible=False,
            keep_focused_window_visible=False,
            show_scrollbar=True,
            display_arrows=True,
            up_arrow_symbol="▴",
            down_arrow_symbol="▾",
        )
        self.telemetry = Window(
            FormattedTextControl(self._render_telemetry),
            wrap_lines=True,
            style="class:panel",
        )
        self.notice = Window(
            FormattedTextControl(self._render_notice),
            height=1,
            style="class:notice",
        )
        self.command = TextArea(
            height=1,
            prompt=FormattedText([("class:command.prompt", "  COMMAND  › ")]),
            multiline=False,
            wrap_lines=False,
            accept_handler=self._accept_command,
            style="class:command",
        )
        self.footer = Window(
            FormattedTextControl(self._render_footer),
            height=Dimension(min=1, max=2),
            style="class:footer",
        )

        body = HSplit([
            self.header,
            VSplit([
                ConditionalContainer(
                    Frame(
                        self.fleet,
                        title=" FLEET ",
                        width=Dimension(preferred=31, min=28, max=35),
                        style="class:panel",
                    ),
                    filter=self._show_fleet,
                ),
                Frame(self.workspace, title=" WORKSPACE ", style="class:panel"),
                ConditionalContainer(
                    Frame(
                        self.telemetry,
                        title=" MISSION ",
                        width=Dimension(preferred=33, min=30, max=38),
                        style="class:panel",
                    ),
                    filter=self._show_telemetry,
                ),
            ]),
            self.notice,
            Frame(self.command, title=" STEERING BUS ", height=3, style="class:panel"),
            self.footer,
        ])
        self.application: Application[None] = Application(
            layout=Layout(body, focused_element=self.command),
            key_bindings=self._bindings(),
            full_screen=True,
            mouse_support=True,
            style=style_for(self.theme_name),
            before_render=lambda _: self.workspace_control.sync(),
        )

    async def run(self) -> None:
        if self.state.started_at is None:
            self.state.started_at = datetime.now(UTC)
        self.backend = self._backend_factory(
            on_progress=self._on_progress,
            on_report=self._on_report,
        )
        self._backend_task = asyncio.create_task(self._drive_backend())
        # Completed archives have a fixed elapsed time and no background work,
        # so they need redraws only for input and explicit state changes.
        if self.state.finished_at is None:
            self._ticker_task = asyncio.create_task(self._ticker())
        try:
            await self.application.run_async()
        finally:
            if self._ticker_task:
                self._ticker_task.cancel()
                await asyncio.gather(self._ticker_task, return_exceptions=True)
            # Ctrl-Q is refused while active. If the terminal disappears or the
            # renderer stops for another reason, keep the orchestration task
            # alive and wait for its artifact-preserving terminal state.
            if self._backend_task:
                if not self._backend_task.done():
                    await asyncio.shield(self._backend_task)
                await asyncio.gather(self._backend_task, return_exceptions=True)

    async def _drive_backend(self) -> None:
        assert self.backend is not None
        try:
            result = await self.backend.run()
        except Exception as exc:  # surfaced in the deck, with artifacts retained by core
            self.state.phase = MissionPhase.FAILED
            self.state.notice = f"Backend failed: {exc}"
            self.state.add_event("system", str(exc), level=EventLevel.ERROR)
        else:
            self._apply_result(result)
        finally:
            if self.state.finished_at is None:
                self.state.finished_at = datetime.now(UTC)
            self.application.invalidate()

    def _apply_result(self, result: RunResult) -> None:
        self.state.run_dir = result.run_dir
        self.state.final_answer = result.final_answer
        self.state.review_command = result.review_command
        self._reconcile_hunters(result)
        if result.status == "completed":
            self.state.phase = (
                MissionPhase.DEGRADED
                if result.degraded
                or self.state.failed_count
                or self.state.blocked_count
                or any(h.partial_notes for h in self.state.hunters.values())
                else MissionPhase.COMPLETE
            )
            if result.historical:
                self.state.notice = (
                    "Stored mission loaded read-only. F3 reports; F4 synthesis; "
                    "F8 Codex follow-up."
                    if self.state.review_command
                    else "Stored mission loaded read-only. F3 reports; F4 synthesis."
                )
            else:
                self.state.notice = (
                    "Mission sealed. F4 opens synthesis; F8 opens Codex follow-up; Ctrl-Q exits."
                    if self.state.review_command
                    else "Mission sealed. F4 opens synthesis; Ctrl-Q exits."
                )
        elif result.status == "incomplete":
            self.state.phase = MissionPhase.DEGRADED
            self.state.notice = "Mission preserved with degraded coverage. Review artifacts."
        else:
            self.state.phase = MissionPhase.FAILED
            self.state.notice = f"Mission ended with status: {result.status}"

    def _reconcile_hunters(self, result: RunResult) -> None:
        """Make the sealed manifest authoritative over transient progress events."""
        for outcome in result.hunter_outcomes:
            hunter = self.state.hunters.get(outcome.hunter_id)
            if hunter is None:
                continue
            if outcome.terminal_workers is not None:
                hunter.terminal_workers = outcome.terminal_workers
            if outcome.output_tokens is not None:
                hunter.output_tokens = outcome.output_tokens
            if outcome.expected_workers is not None:
                hunter.active_workers = max(
                    0, outcome.expected_workers - hunter.terminal_workers
                )
            else:
                hunter.active_workers = 0
            hunter.partial_notes = outcome.partial_notes
            if outcome.answer_sealed:
                if outcome.terminal_workers is None:
                    # Codex-exec rounds do not emit OpenCode worker-completion
                    # metadata. A sealed answer is nevertheless terminal.
                    hunter.terminal_workers = max(
                        hunter.terminal_workers, hunter.spec.workers
                    )
                hunter.phase = HunterPhase.SEALED
                hunter.report_ready = True
                hunter.active_workers = 0
                hunter.detail = (
                    "sealed evidence ready for Codex (degraded worker coverage)"
                    if outcome.degraded
                    else "sealed evidence ready for Codex"
                )
            elif outcome.blocked:
                hunter.phase = HunterPhase.BLOCKED
                hunter.detail = "permission request prevented a sealed answer"
            elif outcome.partial_notes:
                hunter.phase = HunterPhase.PARTIAL
                hunter.detail = "unverified notes preserved"
            else:
                hunter.phase = HunterPhase.FAILED
                hunter.detail = f"mission ended without a sealed answer ({outcome.status})"

    async def _ticker(self) -> None:
        while self.state.finished_at is None:
            await asyncio.sleep(_STATUS_REFRESH_INTERVAL)
            self.application.invalidate()

    def _on_progress(self, message: str) -> None:
        ingest_progress(self.state, message)
        if (
            not self.workspace_control.holding_selection
            and self.state.view == "activity"
            and self.workspace.vertical_scroll
        ):
            # Signal is newest-first. Preserve an operator's historical
            # viewport instead of snapping it toward incoming live events.
            self.workspace.vertical_scroll += 1
        self.application.invalidate()

    def _on_report(self, hunter_id: str, report: str) -> None:
        ingest_report(self.state, hunter_id, report)
        self.application.invalidate()

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("f1", eager=True)
        def _help(event: object) -> None:
            if self.state.view == "help":
                self._switch_view(self._pre_help_view)
            else:
                self._pre_help_view = self.state.view
                self._switch_view("help")

        @bindings.add("f2", eager=True)
        def _activity(event: object) -> None:
            self._switch_view("activity")

        @bindings.add("f3", eager=True)
        def _reports(event: object) -> None:
            self._switch_view("reports")

        @bindings.add("f4", eager=True)
        def _synthesis(event: object) -> None:
            self._switch_view("synthesis")

        @bindings.add("f5", eager=True)
        def _mission(event: object) -> None:
            self._switch_view("mission")

        @bindings.add("f8", eager=True)
        def _review(event: object) -> None:
            self._switch_view("review")

        @bindings.add("f9", eager=True)
        def _theme(event: object) -> None:
            self._apply_theme(next_theme_name(self.theme_name))

        workspace_focused = Condition(
            lambda: self.application.layout.current_control is self.workspace_control
        )

        @bindings.add("c-c", eager=True, filter=workspace_focused)
        @bindings.add("f6", eager=True)
        @bindings.add("f7", eager=True)
        async def _copy_workspace(event: object) -> None:
            text = self.workspace_control.selected_text or self.workspace_control.buffer.text
            if not text:
                return
            self.application.clipboard.set_text(text)
            destination = await copy_to_clipboard(text, self.application.output)
            if destination == "system":
                self.state.notice = (
                    f"Copied {len(text)} characters from WORKSPACE. "
                    "Esc returns to the command bus."
                )
            elif destination == "unavailable":
                self.state.notice = (
                    "Copy unavailable in VTE: install wl-clipboard. Use plain drag "
                    "inside WORKSPACE, then F6; Shift-drag is terminal-wide."
                )
            else:
                self.state.notice = (
                    "Terminal clipboard request sent but cannot be verified. Install "
                    "the platform clipboard helper for confirmed copying."
                )

        @bindings.add("escape", eager=True, filter=workspace_focused)
        def _command_focus(event: object) -> None:
            self._release_workspace()

        @bindings.add("c-n", eager=True)
        @bindings.add("tab", eager=True)
        def _next(event: object) -> None:
            self._release_workspace()
            self.state.select_next(1)
            self.workspace.vertical_scroll = 0

        @bindings.add("c-p", eager=True)
        @bindings.add("s-tab", eager=True)
        def _previous(event: object) -> None:
            self._release_workspace()
            self.state.select_next(-1)
            self.workspace.vertical_scroll = 0

        @bindings.add("c-b", eager=True)
        def _broadcast(event: object) -> None:
            self._prefill("/all ")

        @bindings.add("c-t", eager=True)
        def _target(event: object) -> None:
            selected = self.state.selected
            self._prefill(f"/to {selected.spec.id} " if selected else "/all ")

        @bindings.add("pageup", eager=True)
        def _page_up(event: object) -> None:
            self._scroll_workspace(-self._workspace_page_size())

        @bindings.add("pagedown", eager=True)
        def _page_down(event: object) -> None:
            self._scroll_workspace(self._workspace_page_size())

        empty_command = Condition(
            lambda: workspace_focused() or not self.command.text
        )

        @bindings.add("up", eager=True, filter=empty_command)
        def _line_up(event: object) -> None:
            self._scroll_workspace(-1)

        @bindings.add("down", eager=True, filter=empty_command)
        def _line_down(event: object) -> None:
            self._scroll_workspace(1)

        @bindings.add("home", eager=True, filter=empty_command)
        def _top(event: object) -> None:
            self.workspace.vertical_scroll = 0

        @bindings.add("end", eager=True, filter=empty_command)
        def _bottom(event: object) -> None:
            # The bounded pane clamps this sentinel to the exact final page.
            self.workspace.vertical_scroll = self.workspace.max_available_height

        @bindings.add("c-q", eager=True)
        def _quit(event: object) -> None:
            if self.backend is not None and self.backend.active:
                self.state.notice = (
                    "Mission is active; Ctrl-Q will not terminate model work. Use /wrap first."
                )
                return
            self.application.exit()

        return bindings

    def _switch_view(self, view: str) -> None:
        self._release_workspace()
        self.state.view = view
        self.workspace.vertical_scroll = 0

    def _apply_theme(self, name: str) -> None:
        self.theme_name = name
        self.application.style = style_for(name)
        self.state.notice = f"Theme: {name}. F9 cycles; /theme NAME picks directly."
        self.application.invalidate()

    def _release_workspace(self) -> None:
        self.workspace_control.release_selection()
        self.application.layout.focus(self.command)

    def _scroll_workspace(self, delta: int) -> None:
        self.workspace.vertical_scroll = max(
            0, self.workspace.vertical_scroll + delta
        )
        if hasattr(self, "application"):
            self.application.invalidate()

    def _workspace_page_size(self) -> int:
        try:
            rows = get_app().output.get_size().rows
        except (AttributeError, RuntimeError):
            rows = 24
        return max(4, rows - 13)

    def _prefill(self, text: str) -> None:
        self._release_workspace()
        self.command.text = text
        self.command.buffer.cursor_position = len(text)
        self.application.layout.focus(self.command)

    def _accept_command(self, buffer: Buffer) -> bool:
        command = buffer.text.strip()
        buffer.text = ""
        if command:
            asyncio.create_task(self._handle_command(command))
        return True

    async def _handle_command(self, command: str) -> None:
        assert self.backend is not None
        if command in {"/help", "?"}:
            self._pre_help_view = self.state.view
            self._switch_view("help")
            return
        if command == "/status":
            self.state.notice = self.backend.status()
            return
        if command == "/wrap" or command.startswith("/wrap "):
            target = command.split(maxsplit=1)[1] if " " in command else None
            outcomes = await self.backend.wrap(target)
            self._control_outcomes(outcomes)
            return
        if command.startswith("/focus "):
            hunter_id = command.split(maxsplit=1)[1]
            ids = list(self.state.hunters)
            if hunter_id not in ids:
                self.state.notice = f"Unknown hunter: {hunter_id}"
            else:
                self.state.selected_index = ids.index(hunter_id)
                self.workspace.vertical_scroll = 0
                self.state.notice = f"Focused {hunter_id}"
            return
        if command.startswith("/view "):
            view = command.split(maxsplit=1)[1]
            if view not in {"activity", "reports", "synthesis", "mission", "review", "help"}:
                self.state.notice = "Views: activity, reports, synthesis, mission, review, help"
            else:
                self._switch_view(view)
            return
        if command.startswith("/filter"):
            self.state.filter_text = command.removeprefix("/filter").strip().lower()
            self.workspace.vertical_scroll = 0
            self.state.notice = (
                f"Signal filter: {self.state.filter_text or 'off'}"
            )
            return
        if command == "/theme" or command.startswith("/theme "):
            requested = command.removeprefix("/theme").strip().lower()
            if not requested:
                self.state.notice = "Themes: " + ", ".join(theme_names())
            elif requested not in theme_names():
                self.state.notice = (
                    f"Unknown theme: {requested}. Themes: {', '.join(theme_names())}"
                )
            else:
                self._apply_theme(requested)
            return
        if command == "/clear":
            self.state.events.clear()
            self.workspace.vertical_scroll = 0
            self.state.notice = "Local signal rail cleared; artifacts are unchanged."
            return
        if command in {"/quit", "/exit"}:
            if self.backend.active:
                self.state.notice = "Cannot exit while active. Use /wrap; no work was killed."
            else:
                self.application.exit()
            return
        if command == "/chat":
            if self.backend.active:
                self.state.notice = "Codex follow-up is available after the mission is sealed."
                return
            if not self.state.review_command:
                self.state.notice = "Codex follow-up is unavailable for this analyzer."
                return
            self.state.notice = "Opening Codex review; the deck will return when the chat closes."
            self.application.invalidate()
            self.state.notice = await run_in_terminal(self.backend.open_review)
            self.application.invalidate()
            return

        target: str | None = None
        message = command
        if command.startswith("/all "):
            message = command.split(maxsplit=1)[1]
        elif command.startswith("/to "):
            parts = command.split(maxsplit=2)
            if len(parts) != 3:
                self.state.notice = "Usage: /to HUNTER message"
                return
            target, message = parts[1], parts[2]
            if target not in self.state.hunters:
                self.state.notice = f"Unknown hunter: {target}"
                return
        elif command.startswith("/"):
            self.state.notice = "Unknown command. Press F1 for the command map."
            return
        outcomes = await self.backend.send(message, target)
        self._control_outcomes(outcomes)

    def _control_outcomes(self, outcomes: list[str]) -> None:
        for outcome in outcomes:
            self.state.add_event("control", outcome, level=EventLevel.INFO)
        self.state.notice = " · ".join(outcomes) if outcomes else "No active destination"

    def _render_header(self) -> FormattedText:
        elapsed = _duration(self.state.elapsed_seconds)
        return render_brand_header([
            ("class:header.meta", f"  MISSION   {self.state.title}"),
            ("class:header.meta", "    ELAPSED  "),
            ("class:accent", elapsed),
            ("class:header.meta", "    PHASE  "),
            ("class:header.phase", self.state.phase.value.upper()),
            ("class:header.meta", "    THEME  "),
            ("class:violet", self.theme_name.upper()),
        ])

    def _render_fleet(self) -> FormattedText:
        fragments: list[tuple[str, str]] = [("", "\n")]
        selected = self.state.selected
        for hunter in self.state.hunters.values():
            is_selected = hunter is selected
            base = "class:selected" if is_selected else ""
            marker = "▌" if is_selected else "│"
            fragments.extend([
                ("class:selected.marker" if is_selected else "class:muted", marker + " "),
                (base + " class:soft", _clip(hunter.spec.id.upper(), 11).ljust(12)),
                (f"class:phase.{hunter.phase.value}", hunter.phase.value.upper()),
                ("", "\n"),
                (base + " class:muted", "  "),
                (f"class:phase.{hunter.phase.value}", _bar(hunter.terminal_workers, hunter.spec.workers, 7)),
                (base + " class:soft", f" {hunter.terminal_workers}/{hunter.spec.workers}"),
                (base + " class:accent", _compact(hunter.output_tokens).rjust(8)),
                ("", "\n"),
            ])
        return FormattedText(fragments)

    def _render_main(self) -> FormattedText:
        if self.state.view == "reports":
            return self._render_report()
        if self.state.view == "synthesis":
            return self._render_synthesis()
        if self.state.view == "mission":
            return self._render_telemetry(full_instruction=True)
        if self.state.view == "review":
            return self._render_review()
        if self.state.view == "help":
            return self._render_help()
        return self._render_activity()

    def _render_activity(self) -> FormattedText:
        events = list(self.state.events)
        if self.state.filter_text:
            needle = self.state.filter_text
            events = [
                event for event in events
                if needle in event.message.lower() or needle in event.source.lower()
            ]
        # Bound renderer cost while preserving far more than a terminal page.
        # The complete stream remains available in the round artifacts.
        fragments: list[tuple[str, str]] = []
        visible = events[-1000:]
        if not visible:
            return FormattedText([("class:muted", "\n  Waiting for signal…")])
        for event in reversed(visible):
            fragments.extend([
                ("class:event.time", event.at.astimezone().strftime("%H:%M:%S") + "  "),
                ("class:event.source", _clip(event.source, 12).ljust(12) + " "),
                (f"class:event.{event.level.value}", event.message),
                ("", "\n"),
            ])
        return FormattedText(fragments)

    def _render_report(self) -> FormattedText:
        selected = self.state.selected
        if selected is None:
            return FormattedText([("class:muted", "No hunter selected.")])
        report = self.state.reports.get(selected.spec.id)
        if not report:
            return FormattedText([
                ("class:report.heading", f"{selected.spec.id.upper()} REPORT\n\n"),
                ("class:muted", "No sealed standalone report has arrived yet.\n\n"),
                ("class:soft", selected.last_event or selected.detail),
            ])
        return FormattedText([
            ("class:report.heading", f"{selected.spec.id.upper()}  //  SEALED REPORT\n\n"),
            ("class:soft", report),
        ])

    def _render_synthesis(self) -> FormattedText:
        if not self.state.final_answer:
            return FormattedText([
                ("class:report.heading", "CODEX SYNTHESIS\n\n"),
                ("class:muted", "The blind adjudication has not completed yet.\n"),
                ("class:soft", "Swarmerz will place the final standalone answer here."),
            ])
        return FormattedText([
            ("class:report.heading", "CODEX SYNTHESIS  //  FINAL\n\n"),
            ("class:soft", self.state.final_answer),
        ])

    def _render_review(self) -> FormattedText:
        if not self.state.review_command:
            return FormattedText([
                ("class:report.heading", "CODEX FOLLOW-UP\n\n"),
                ("class:muted", "A readable Codex review becomes available after successful analysis."),
            ])
        return FormattedText([
            ("class:report.heading", "CODEX FOLLOW-UP  //  INTERACTIVE\n\n"),
            ("class:soft", "Continue the completed analysis in this terminal:\n"),
            ("class:violet", "  /chat\n\n"),
            ("class:soft", "Or open another terminal tab and run:\n\n"),
            ("class:accent", self.state.review_command),
            ("class:muted", "\n\nThe first launch opens a clean readable review. "
             "Run the displayed command with --resume to continue that chat later."),
        ])

    def _render_help(self) -> FormattedText:
        return FormattedText([
            ("class:report.heading", "COMMAND MAP\n\n"),
            ("class:accent", "F2"), ("class:soft", "  signal rail       "),
            ("class:accent", "F3"), ("class:soft", "  hunter reports\n"),
            ("class:accent", "F4"), ("class:soft", "  adjudicator synthesis   "),
            ("class:accent", "F5"), ("class:soft", "  mission telemetry\n"),
            ("class:accent", "F8"), ("class:soft", "  Codex follow-up / review chat\n"),
            ("class:accent", "F9"), ("class:soft", "  cycle color theme (or /theme NAME)\n"),
            ("class:accent", "F6"),
            ("class:soft", "  copy selection, or entire WORKSPACE with no selection\n"),
            ("class:accent", "Drag + Ctrl-C"),
            ("class:soft", "  select and copy text inside WORKSPACE only\n"),
            ("class:accent", "F7"),
            ("class:soft", "  compatibility alias for F6\n"),
            ("class:accent", "Tab / Ctrl-N"), ("class:soft", "  next hunter\n"),
            ("class:accent", "Shift-Tab"), ("class:soft", "  previous hunter  "),
            ("class:accent", "↑/↓ · PgUp/PgDn"),
            ("class:soft", "  scroll any workspace\n"),
            ("class:accent", "Home / End · wheel"),
            ("class:soft", "  jump or mouse-scroll\n"),
            ("", "\n"),
            ("class:violet", "/all MESSAGE"), ("class:soft", "       steer every active parent and worker\n"),
            ("class:violet", "/to ID MESSAGE"), ("class:soft", "     target one hunter swarm\n"),
            ("class:violet", "/wrap [ID]"), ("class:soft", "         ask all or one swarm to finish cleanly\n"),
            ("class:violet", "/status"), ("class:soft", "            inspect steering readiness\n"),
            ("class:violet", "/focus ID"), ("class:soft", "          select a hunter card\n"),
            ("class:violet", "/filter TEXT"), ("class:soft", "       filter the local signal rail\n"),
            ("class:violet", "/theme [NAME]"), ("class:soft", "      list or pick a color theme\n"),
            ("class:violet", "/clear"), ("class:soft", "             clear only the local rail, never artifacts\n"),
            ("class:violet", "/quit"), ("class:soft", "              exit only after the mission is terminal\n"),
            ("class:violet", "/chat"), ("class:soft", "              open or resume the completed Codex review\n"),
            ("", "\n"),
            ("class:amber", "Safety invariant: closing the deck never kills active model work."),
        ])

    def _render_telemetry(self, *, full_instruction: bool = False) -> FormattedText:
        selected = self.state.selected
        fragments: list[tuple[str, str]] = [
            ("", "\n"),
            ("class:muted", "PHASE VECTOR\n"),
        ]
        current_index = _phase_index(self.state.phase)
        for index, (label, phase) in enumerate(zip(
            ("FREEZE", "BASE", "HUNT", "QUORUM", "JUDGE", "VERIFY", "SEAL"),
            _PHASE_ORDER,
        )):
            if index == 6 and self.state.phase is MissionPhase.FAILED:
                icon, style = "×", "class:danger"
            elif index == 6 and self.state.phase is MissionPhase.DEGRADED:
                icon, style = "◇", "class:amber"
            elif index < current_index or self.state.phase is MissionPhase.COMPLETE:
                icon, style = "◆", "class:acid"
            elif index == current_index:
                icon, style = "●", "class:accent"
            else:
                icon, style = "○", "class:muted"
            fragments.extend([(style, f" {icon} {label:<7}")])
            if index in {1, 3, 5}:
                fragments.append(("", "\n"))
        fragments.extend([
            ("", "\n\n"),
            ("class:muted", "EVIDENCE  "),
            ("class:soft", "sealed "),
            ("class:acid", str(self.state.sealed_count)),
            ("class:soft", f"/{len(self.state.hunters)}  ·  tokens "),
            ("class:accent", _compact(self.state.token_total)),
            ("", "\n"),
            ("class:soft", "          failed "),
            ("class:danger" if self.state.failed_count else "class:muted", str(self.state.failed_count)),
            ("class:soft", "  ·  blocked "),
            ("class:amber" if self.state.blocked_count else "class:muted", str(self.state.blocked_count)),
            ("", "\n\n"),
            ("class:muted", "COMMAND\n"),
        ])
        instruction = self.state.instruction.strip()
        if full_instruction:
            # The F5 workspace is a bounded scrollable pane, so a long
            # mission command stays fully readable with the usual scroll keys.
            fragments.append(("class:soft", (instruction or "(no instruction recorded)") + "\n\n"))
        else:
            preview = textwrap.fill(_clip(instruction, 90) or "(no instruction recorded)", width=30)
            fragments.extend([
                ("class:soft", f" {preview}\n"),
                ("class:muted", " F5 shows the full command\n\n"),
            ])
        if selected:
            fragments.extend([
                ("class:muted", "FOCUS  "),
                ("class:violet", f"{selected.spec.id}"),
                ("class:muted", f"  {selected.spec.harness} · {selected.spec.effort}\n"),
                ("class:soft", f" {_clip(selected.spec.provider, 32)}\n"),
                ("class:soft", f" {_clip(selected.spec.model, 32)}\n"),
                ("class:soft", textwrap.fill(selected.detail, width=32) + "\n"),
            ])
        if self.state.run_dir:
            fragments.extend([
                ("", "\n"),
                ("class:muted", "ARTIFACTS\n"),
                ("class:soft", textwrap.fill(str(self.state.run_dir), width=32)),
            ])
        return FormattedText(fragments)

    def _render_notice(self) -> FormattedText:
        return FormattedText([("class:notice", "  " + _clip(self.state.notice, 150))])

    def _render_footer(self) -> FormattedText:
        if self.application.layout.current_control is self.workspace_control:
            return FormattedText([
                ("class:footer.key", "  F6 / Ctrl-C"), ("class:footer", " copy · "),
                ("class:footer.key", "Esc"), ("class:footer", " command"),
            ])
        columns = _terminal_columns()
        if columns >= 118:
            items = _FOOTER_FULL
        elif columns >= 80:
            items = _FOOTER_MEDIUM
        else:
            items = _FOOTER_COMPACT
        return render_keybar(items, columns, max_rows=2)


def _phase_index(phase: MissionPhase) -> int:
    mapping = {
        MissionPhase.READY: 0,
        MissionPhase.SETUP: 0,
        MissionPhase.BASELINE: 1,
        MissionPhase.HUNTING: 2,
        MissionPhase.QUORUM: 3,
        MissionPhase.ANALYZING: 4,
        MissionPhase.VERIFYING: 5,
        MissionPhase.COMPLETE: 6,
        MissionPhase.DEGRADED: 6,
        MissionPhase.FAILED: 6,
    }
    return mapping[phase]


def _duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _bar(value: int, maximum: int, width: int = 8) -> str:
    filled = width if maximum <= 0 else round(width * min(value, maximum) / maximum)
    return "━" * filled + "─" * (width - filled)


def _clip(value: str, width: int) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= width else compact[: max(1, width - 1)] + "…"


def _terminal_columns() -> int:
    """Return a stable width both inside the deck and in renderer tests."""
    try:
        return get_app().output.get_size().columns
    except (AttributeError, RuntimeError):
        return 120
