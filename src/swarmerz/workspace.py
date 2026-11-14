# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

from typing import Callable

from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import (
    FormattedText,
    StyleAndTextTuples,
    fragment_list_to_text,
    split_lines,
)
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType


class _WorkspaceLexer(Lexer):
    """Keep the deck's existing colors in a selectable text buffer."""

    def __init__(self, fragments: FormattedText) -> None:
        self.lines = list(split_lines(fragments))

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        lines = self.lines
        return lambda index: lines[index] if index < len(lines) else []


class WorkspaceControl(BufferControl):
    """Read-only messages that hold their position during mouse selection."""

    def __init__(self, render: Callable[[], FormattedText]) -> None:
        super().__init__(buffer=Buffer(read_only=True), focusable=True)
        self._render = render
        self._fragments = FormattedText([])
        self._dragging = False

    @property
    def holding_selection(self) -> bool:
        return self._dragging or self.buffer.selection_state is not None

    @property
    def selected_text(self) -> str:
        if self.buffer.selection_state is None:
            return ""
        # Extract without clearing the highlight or modifying the read-only text.
        return self.buffer.document.cut_selection()[1].text

    def release_selection(self) -> None:
        self._dragging = False
        self.buffer.exit_selection()

    def sync(self) -> None:
        if get_app().layout.current_control is not self:
            self.release_selection()
        if self.holding_selection:
            return
        fragments = self._render()
        if fragments != self._fragments:
            self._fragments = fragments
            self.lexer = _WorkspaceLexer(fragments)
            text = fragment_list_to_text(fragments)
            self.buffer.set_document(
                Document(text, cursor_position=min(self.buffer.cursor_position, len(text))),
                bypass_readonly=True,
            )

    def mouse_handler(self, mouse_event: MouseEvent) -> object:
        if mouse_event.button != MouseButton.LEFT:
            return NotImplemented
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            # BufferControl normally focuses on mouse-up, swallowing the first
            # drag. Focus on mouse-down so selecting works on the first attempt.
            get_app().layout.focus(self)
            self._dragging = True
        result = super().mouse_handler(mouse_event)
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            self._dragging = False
        return result
