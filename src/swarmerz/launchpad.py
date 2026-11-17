# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame, TextArea

from .branding import render_brand_header
from .clipboard import copy_to_clipboard
from .keybar import render_keybar, terminal_columns
from .models import HunterSpec
from .terminal import run_tui_application
from .themes import initial_theme_name, next_theme_name, style_for


_SAFE_SESSION = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}$")
_LOCAL_PATH = re.compile(r"(?<![\w/.-])(?:~/[^\s\"']*|/[^\s\"']*)")
_MODES = ("codebase", "raw")


def _existing_local_paths(text: str) -> list[str]:
    """Absolute paths in the instruction that exist on this machine.

    Raw missions do not stage local evidence into hunter workspaces, so they
    must not depend on local paths. The core enforces this protocol boundary;
    it is not an operating-system claim about a hostile same-user process.
    """
    found = []
    for match in _LOCAL_PATH.findall(text):
        candidate = match.rstrip(".,;:!?)]}")
        try:
            if Path(candidate).expanduser().exists():
                found.append(candidate)
        except OSError:
            continue
    return found


@dataclass(frozen=True)
class LaunchRequest:
    context: str | None
    session: str
    instruction: str


class LaunchpadApp:
    """Staged terminal preflight that performs no model work."""

    _labels = {
        "mode": "MODE",
        "context": "CONTEXT",
        "session": "SESSION",
        "instruction": "COMMAND",
    }

    def __init__(
        self,
        *,
        config_path: Path,
        specs: tuple[HunterSpec, ...],
        context: str | None = None,
        session: str | None = None,
        instruction: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.config_path = config_path
        self.specs = specs
        self.cwd = (cwd or Path.cwd()).resolve()
        self._session_explicit = bool(session)
        self.values: dict[str, str] = {
            "mode": "codebase",
            "context": context or "",
            "session": session or _suggest_session(context, self.cwd),
            "instruction": instruction or "",
        }
        self.step_index = 0
        self.result: LaunchRequest | None = None
        self.cancelled = False
        self.theme_name = initial_theme_name()
        self.notice = (
            "Pick the mission type: codebase freezes a local folder as shared evidence; "
            "raw covers web audits, research, and reasoning. Left/right toggles."
        )

        self.header = Window(
            FormattedTextControl(self._render_header),
            height=2,
            style="class:root",
        )
        self.route = Window(
            FormattedTextControl(self._render_route),
            wrap_lines=True,
            style="class:panel",
        )
        self.fleet = Window(
            FormattedTextControl(self._render_fleet),
            wrap_lines=False,
            style="class:panel",
        )
        self.notice_bar = Window(
            FormattedTextControl(self._render_notice),
            height=1,
            style="class:notice",
        )
        self.command = TextArea(
            height=1,
            prompt=self._render_prompt,
            multiline=False,
            wrap_lines=False,
            accept_handler=self._accept,
            style="class:command",
        )
        self.footer = Window(
            FormattedTextControl(self._render_footer),
            height=1,
            style="class:footer",
        )
        self.command.text = self.values[self.step]
        self.command.buffer.cursor_position = len(self.command.text)

        body = HSplit([
            self.header,
            VSplit([
                Frame(self.route, title=" LAUNCH VECTOR ", style="class:panel"),
                Frame(
                    self.fleet,
                    title=" FLEET MANIFEST ",
                    width=Dimension(preferred=39, min=31, max=48),
                    style="class:panel",
                ),
            ]),
            self.notice_bar,
            Frame(self.command, title=" PREFLIGHT BUS ", height=3, style="class:panel"),
            self.footer,
        ])
        self.application: Application[None] = Application(
            layout=Layout(body, focused_element=self.command),
            key_bindings=self._bindings(),
            full_screen=True,
            mouse_support=True,
            style=style_for(self.theme_name),
        )

    @property
    def step(self) -> str:
        return self._active_steps()[self.step_index]

    def _active_steps(self) -> tuple[str, ...]:
        if self.values["mode"] == "raw":
            return ("mode", "session", "instruction")
        return ("mode", "context", "session", "instruction")

    async def run(self) -> LaunchRequest | None:
        await run_tui_application(self.application)
        return self.result

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("f6", eager=True)
        async def _copy_field(event: object) -> None:
            selected = ""
            if self.command.buffer.selection_state is not None:
                selected = self.command.buffer.document.cut_selection()[1].text
            text = selected or self.command.text
            if not text:
                self.notice = "Nothing to copy in the current preflight field."
                return
            self.application.clipboard.set_text(text)
            destination = await copy_to_clipboard(text, self.application.output)
            if destination == "system":
                self.notice = (
                    f"Copied {len(text)} characters from {self._labels[self.step]}."
                )
            elif destination == "unavailable":
                self.notice = (
                    "Copy unavailable in VTE: install wl-clipboard, then press F6 again."
                )
            else:
                self.notice = (
                    "Terminal clipboard request sent but cannot be verified. Install "
                    "the platform clipboard helper for confirmed copying."
                )

        @bindings.add("f9", eager=True)
        def _theme(event: object) -> None:
            self.theme_name = next_theme_name(self.theme_name)
            self.application.style = style_for(self.theme_name)
            self.notice = f"Theme: {self.theme_name}. F9 cycles through the templates."
            self.application.invalidate()

        @bindings.add("c-b", eager=True)
        def _back(event: object) -> None:
            self._back()

        mode_step = Condition(lambda: self.step == "mode")

        @bindings.add("left", eager=True, filter=mode_step)
        @bindings.add("right", eager=True, filter=mode_step)
        def _toggle_mode(event: object) -> None:
            current = self.values["mode"]
            self.values["mode"] = _MODES[0] if current == _MODES[1] else _MODES[1]
            self.command.text = self.values["mode"]
            self.command.buffer.cursor_position = len(self.command.text)
            self.application.invalidate()

        @bindings.add("c-q", eager=True)
        def _cancel(event: object) -> None:
            self.cancelled = True
            self.application.exit()

        return bindings

    def _accept(self, buffer: Buffer) -> bool:
        raw = buffer.text.strip()
        if raw == "/quit":
            self.cancelled = True
            self.application.exit()
            return True
        if raw == "/back":
            self._back()
            return True

        if self.step == "mode":
            choice = raw.lower()
            if choice in {"", "codebase", "code", "c"}:
                self.values["mode"] = "codebase"
                self.notice = (
                    "Choose the source directory to freeze. Leave blank for config/session context."
                )
            elif choice in {"raw", "r"}:
                self.values["mode"] = "raw"
                self.values["context"] = ""
                if not self._session_explicit:
                    self.values["session"] = _suggest_session(None, self.cwd)
                self.notice = (
                    "Raw mission: no local evidence is frozen. Name this mission session."
                )
            else:
                self.notice = "Type 'codebase' or 'raw' — left/right arrows toggle."
                return False
            self._advance()
            return True

        if self.step == "context":
            try:
                context = _resolve_context(raw, self.cwd)
            except ValueError as exc:
                self.notice = str(exc)
                return False
            self.values["context"] = str(context) if context else ""
            if not self._session_explicit:
                self.values["session"] = _suggest_session(
                    str(context) if context else None, self.cwd
                )
            self.notice = "Name this mission session. Existing names continue their frozen history."
            self._advance()
            return True

        if self.step == "session":
            if not _SAFE_SESSION.fullmatch(raw):
                self.notice = (
                    "Session must start with a letter or digit and use only letters, "
                    "digits, dot, underscore, or hyphen."
                )
                return False
            self.values["session"] = raw
            self._session_explicit = True
            self.notice = (
                f"Enter the mission instruction. Enter launches {len(self.specs)} independent hunters."
            )
            self._advance()
            return True

        if not raw:
            self.notice = "Mission instruction cannot be empty. Nothing has been launched."
            return False
        if self.values["mode"] == "raw":
            leaked = _existing_local_paths(raw)
            if leaked:
                self.notice = (
                    f"Instruction references local path {leaked[0]}, but RAW mode does not "
                    "stage local files. Ctrl-B to pick CODEBASE, or remove the path."
                )
                return False
        self.values["instruction"] = raw
        self.result = LaunchRequest(
            context=self.values["context"] or None,
            session=self.values["session"],
            instruction=raw,
        )
        self.application.exit()
        return True

    def _advance(self) -> None:
        self.step_index = min(len(self._active_steps()) - 1, self.step_index + 1)
        self._load_step_value()

    def _back(self) -> None:
        if self.step_index == 0:
            self.notice = "Already at the first preflight stage. Ctrl-Q cancels safely."
            return
        self.values[self.step] = self.command.text.strip()
        self.step_index -= 1
        self.notice = "Preflight moved back one stage. No model work has started."
        self._load_step_value()

    def _load_step_value(self) -> None:
        self.command.text = self.values[self.step]
        self.command.buffer.cursor_position = len(self.command.text)
        self.application.invalidate()

    def _render_header(self) -> FormattedText:
        return render_brand_header([
            ("class:muted", "  NEW SCAN  "),
            ("class:acid", f"STAGE {self.step_index + 1}/{len(self._active_steps())}"),
            ("class:muted", "    MODEL WORK  "),
            ("class:amber", "NOT STARTED"),
            ("class:muted", "    THEME  "),
            ("class:violet", self.theme_name.upper()),
        ])

    def _render_route(self) -> FormattedText:
        rows: list[tuple[str, str]] = [
            ("", "\n"),
            ("class:muted", "MISSION ROUTE\n\n"),
            ("class:done", " ◆  CONFIG\n"),
            ("class:soft", f"    {self.config_path}\n\n"),
        ]
        steps = self._active_steps()
        for index, step in enumerate(steps):
            if index < self.step_index:
                icon, style = "◆", "class:done"
            elif index == self.step_index:
                icon, style = "●", "class:active"
            else:
                icon, style = "○", "class:pending"
            rows.append((style, f" {icon}  {self._labels[step]}\n"))
            rows.append(("class:soft", f"    {self._display_value(step, index)}\n\n"))
        if self.values["mode"] == "raw":
            rows.extend([
                ("class:muted", " ○  CONTEXT\n"),
                ("class:muted", "    skipped — RAW mode stages no local files\n\n"),
            ])
        rows.extend([
            ("class:muted", "EXECUTION\n"),
            ("class:soft", f" {len(self.specs)} parents · "),
            ("class:accent", f"{sum(spec.workers for spec in self.specs)} workers"),
            ("class:soft", " · 1 blind adjudicator\n"),
        ])
        return FormattedText(rows)

    def _display_value(self, step: str, index: int) -> str:
        if index > self.step_index:
            return "pending"
        value = self.values[step]
        if step == "mode":
            if value == "raw":
                return "raw — web audits, research, reasoning; nothing frozen"
            return "codebase — freeze a local folder as shared evidence"
        if step == "context" and not value:
            return "config or existing session context"
        if step == "instruction" and value:
            return _clip(value, 72)
        return value or "waiting for operator"

    def _render_fleet(self) -> FormattedText:
        rows: list[tuple[str, str]] = [
            ("", "\n"),
            ("class:muted", "PROVIDER ROUTES\n\n"),
        ]
        for spec in self.specs:
            rows.extend([
                ("class:accent", " ◆ "),
                ("class:soft", _clip(spec.label.upper(), 9).ljust(10)),
                ("class:soft", _clip(spec.model, 15).ljust(16)),
                ("class:muted", f"×{spec.workers} "),
                ("class:violet", "auto" if spec.effort == "default" else spec.effort),
                ("", "\n"),
            ])
        return FormattedText(rows)

    def _render_prompt(self) -> FormattedText:
        return FormattedText([("class:command.prompt", f"  {self._labels[self.step]}  › ")])

    def _render_notice(self) -> FormattedText:
        return FormattedText([("class:notice", "  " + _clip(self.notice, 150))])

    def _render_footer(self) -> FormattedText:
        launch = "LAUNCH" if self.step == "instruction" else "NEXT"
        items = [
            ("Enter", launch),
            ("F6", "copy"),
            ("F9", "theme"),
            ("Ctrl-B", "back"),
            ("Ctrl-Q", "cancel"),
            ("/back /quit", ""),
        ]
        return render_keybar(items, terminal_columns(), max_rows=1)


def _resolve_context(value: str, cwd: Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Context cannot be resolved: {path}: {exc}") from exc
    if not resolved.is_dir():
        raise ValueError(f"Context is not a directory: {resolved}")
    return resolved


def _suggest_session(context: str | None, cwd: Path) -> str:
    source = Path(context).expanduser().name if context else cwd.name
    candidate = re.sub(r"[^a-zA-Z0-9_.-]+", "-", source).strip(".-_")
    return candidate[:96] or "swarm-session"


def _clip(value: str, width: int) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= width else compact[: max(1, width - 1)] + "…"
