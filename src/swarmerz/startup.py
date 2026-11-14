# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame

from .branding import render_brand_header
from .backend import MissionRecord
from .keybar import render_keybar, terminal_columns
from .themes import initial_theme_name, next_theme_name, style_for


@dataclass(frozen=True)
class StartupRequest:
    mode: str
    mission: MissionRecord | None = None


class StartupApp:
    """Choose a new mission or load one completed/stored round without model work."""

    def __init__(
        self,
        missions: tuple[MissionRecord, ...],
        *,
        warning: str | None = None,
    ) -> None:
        self.missions = missions
        self.screen = "mode"
        self.mode_index = 0
        self.mission_index = 0
        self.result: StartupRequest | None = None
        self.theme_name = initial_theme_name()
        self.notice = warning or (
            "Choose whether to start new work or inspect a stored mission."
        )

        self.header = Window(
            FormattedTextControl(self._render_header), height=2, style="class:root"
        )
        self.main = Window(
            FormattedTextControl(self._render_main),
            wrap_lines=False,
            style="class:panel",
        )
        self.detail = Window(
            FormattedTextControl(self._render_detail),
            wrap_lines=True,
            style="class:panel",
        )
        self.notice_bar = Window(
            FormattedTextControl(self._render_notice), height=1, style="class:notice"
        )
        self.footer = Window(
            FormattedTextControl(self._render_footer), height=1, style="class:footer"
        )
        body = HSplit([
            self.header,
            VSplit([
                Frame(self.main, title=" START ", style="class:panel"),
                Frame(
                    self.detail,
                    title=" MISSION DETAIL ",
                    width=Dimension(preferred=48, min=34, max=64),
                    style="class:panel",
                ),
            ]),
            self.notice_bar,
            self.footer,
        ])
        self.application: Application[None] = Application(
            layout=Layout(body),
            key_bindings=self._bindings(),
            full_screen=True,
            mouse_support=True,
            style=style_for(self.theme_name),
        )

    async def run(self) -> StartupRequest | None:
        await self.application.run_async()
        return self.result

    @property
    def selected_mission(self) -> MissionRecord | None:
        if not self.missions:
            return None
        self.mission_index %= len(self.missions)
        return self.missions[self.mission_index]

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("up", eager=True)
        @bindings.add("c-p", eager=True)
        def _up(event: object) -> None:
            self._move(-1)

        @bindings.add("down", eager=True)
        @bindings.add("c-n", eager=True)
        @bindings.add("tab", eager=True)
        def _down(event: object) -> None:
            self._move(1)

        @bindings.add("enter", eager=True)
        def _enter(event: object) -> None:
            self._activate()

        @bindings.add("n", eager=True)
        def _new(event: object) -> None:
            if self.screen == "mode":
                self.result = StartupRequest("new")
                self.application.exit()

        @bindings.add("l", eager=True)
        def _load(event: object) -> None:
            if self.screen == "mode":
                self.mode_index = 1
                self._activate()

        @bindings.add("escape", eager=True)
        @bindings.add("c-b", eager=True)
        def _back(event: object) -> None:
            if self.screen == "history":
                self.screen = "mode"
                self.notice = "Choose whether to start new work or inspect a stored mission."
                self.application.invalidate()

        @bindings.add("c-q", eager=True)
        @bindings.add("q", eager=True)
        def _quit(event: object) -> None:
            self.application.exit()

        @bindings.add("f9", eager=True)
        def _theme(event: object) -> None:
            self.theme_name = next_theme_name(self.theme_name)
            self.application.style = style_for(self.theme_name)
            self.notice = f"Theme: {self.theme_name}. F9 cycles through the templates."
            self.application.invalidate()

        return bindings

    def _move(self, direction: int) -> None:
        if self.screen == "mode":
            self.mode_index = (self.mode_index + direction) % 2
        elif self.missions:
            self.mission_index = (self.mission_index + direction) % len(self.missions)
        self.application.invalidate()

    def _activate(self) -> None:
        if self.screen == "mode":
            if self.mode_index == 0:
                self.result = StartupRequest("new")
                self.application.exit()
                return
            if not self.missions:
                self.notice = "No stored missions were found under this configuration's output root."
                self.application.invalidate()
                return
            self.screen = "history"
            self.notice = "Select a round. Loading is read-only and starts no model work."
            self.application.invalidate()
            return
        mission = self.selected_mission
        if mission is not None:
            self.result = StartupRequest("load", mission)
            self.application.exit()

    def _render_header(self) -> FormattedText:
        return render_brand_header([
            ("class:muted", "  STARTUP   "),
            ("class:acid", "SELECT MODE" if self.screen == "mode" else "SELECT ROUND"),
            ("class:muted", "    STORED ROUNDS  "),
            ("class:accent", str(len(self.missions))),
            ("class:muted", "    THEME  "),
            ("class:violet", self.theme_name.upper()),
        ])

    def _render_main(self) -> FormattedText:
        if self.screen == "mode":
            choices = (
                ("START NEW MISSION", "Configure context, session, and command, then launch."),
                ("LOAD PREVIOUS MISSION", "Open stored evidence and synthesis without rerunning."),
            )
            rows: list[tuple[str, str]] = [("", "\n"), ("class:muted", "MISSION MODE\n\n")]
            for index, (title, detail) in enumerate(choices):
                selected = index == self.mode_index
                rows.extend([
                    ("class:selected.marker" if selected else "class:muted", " ▌ " if selected else " │ "),
                    ("class:selected" if selected else "class:accent", title + "\n"),
                    ("class:soft", f"    {detail}\n\n"),
                ])
            return FormattedText(rows)

        rows = [("", "\n"), ("class:muted", "RECENT MISSIONS  //  NEWEST FIRST\n\n")]
        if not self.missions:
            rows.append(("class:amber", " No stored missions found."))
            return FormattedText(rows)
        start = max(0, min(self.mission_index - 6, len(self.missions) - 12))
        end = min(len(self.missions), start + 12)
        for index in range(start, end):
            mission = self.missions[index]
            selected = index == self.mission_index
            marker = " ▌ " if selected else " │ "
            rows.extend([
                ("class:selected.marker" if selected else "class:muted", marker),
                ("class:selected" if selected else "class:accent", mission.session_id),
                ("class:soft", f"  {mission.round_id}\n"),
                ("class:muted", "    "),
                ("class:acid" if mission.status == "completed" else "class:amber",
                 mission.status.upper()),
                ("class:soft", f"  {mission.display_time}  sealed "
                 f"{mission.sealed_count}/{mission.configured_count}\n"),
            ])
        return FormattedText(rows)

    def _render_detail(self) -> FormattedText:
        if self.screen == "mode":
            if self.mode_index == 0:
                return FormattedText([
                    ("class:accent", "START NEW MISSION\n\n"),
                    ("class:soft", "Continue to the existing safe preflight. No model work "
                     "starts until the final command is submitted."),
                ])
            return FormattedText([
                ("class:violet", "LOAD PREVIOUS MISSION\n\n"),
                ("class:soft", f"Browse {len(self.missions)} stored round(s). Fleet status, "
                 "sealed reports, synthesis, artifacts, and Codex follow-up are restored."),
            ])
        mission = self.selected_mission
        if mission is None:
            return FormattedText([("class:muted", "No mission selected.")])
        coverage = (
            f"{mission.sealed_count}/{mission.configured_count} sealed"
            + (" · degraded" if mission.degraded else "")
        )
        return FormattedText([
            ("class:violet", mission.session_id + "\n\n"),
            ("class:muted", "ROUND\n"),
            ("class:soft", mission.round_id + "\n\n"),
            ("class:muted", "STATUS\n"),
            ("class:acid" if mission.status == "completed" else "class:amber",
             mission.status.upper() + "\n"),
            ("class:soft", coverage + "\n\n"),
            ("class:muted", "INSTRUCTION\n"),
            ("class:soft", _clip(mission.instruction, 420)),
        ])

    def _render_notice(self) -> FormattedText:
        return FormattedText([("class:notice", "  " + _clip(self.notice, 150))])

    def _render_footer(self) -> FormattedText:
        if self.screen == "history":
            items = [
                ("↑/↓", "select"), ("Enter", "load read-only"),
                ("Esc/Ctrl-B", "back"), ("F9", "theme"), ("Ctrl-Q", "cancel"),
            ]
        else:
            items = [
                ("↑/↓", "select"), ("Enter", "continue"), ("N", "new"),
                ("L", "load"), ("F9", "theme"), ("Ctrl-Q", "cancel"),
            ]
        return render_keybar(items, terminal_columns(), max_rows=1)


def _clip(value: str, width: int) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= width else compact[: max(1, width - 1)] + "…"
