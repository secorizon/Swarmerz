# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping

from prompt_toolkit.application import Application, get_app, run_in_terminal
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Dimension, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame

from .branding import render_brand_header
from .keybar import render_keybar, terminal_columns
from .model_inventory import (
    ModelSelection,
    available_model_selections,
    configured_routes,
    merge_ollama_catalog,
)
from .orchestration.models import AppConfig, ConfigError
from .orchestration.ollama_probe import list_ollama_models
from .terminal import run_tui_application
from .themes import initial_theme_name, next_theme_name, style_for


@dataclass(frozen=True)
class ConfigurableModel:
    selection: ModelSelection
    provider_name: str
    source: str
    connected: bool | None
    current_hunter_ids: tuple[str, ...]
    current_judge: bool

    @property
    def selector(self) -> str:
        return self.selection.selector

    @property
    def judge_capable(self) -> bool:
        return self.selection.harness in {"codex-exec", "claude-code"}


@dataclass(frozen=True)
class ModelConfigurationRequest:
    hunter_selectors: tuple[str, ...]
    judge_selector: str | None
    judge_changed: bool
    workers: int = 4


def _option_metadata(inventory: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    opencode = inventory.get("opencode", {})
    if isinstance(opencode, dict):
        for provider in opencode.get("providers", []):
            if not isinstance(provider, dict):
                continue
            provider_name = str(provider.get("name") or provider.get("id") or "OpenCode")
            connected = provider.get("credential_configured") is True
            for model in provider.get("models", []):
                if isinstance(model, dict) and isinstance(model.get("selector"), str):
                    metadata[model["selector"]] = {
                        "provider_name": provider_name,
                        "source": str(
                            model.get("source")
                            or provider.get("source")
                            or "OpenCode live project catalog"
                        ),
                        "connected": (
                            True if provider.get("reachable") is True else connected
                        ),
                    }
    codex = inventory.get("codex", {})
    if isinstance(codex, dict):
        source = (
            "Codex account catalog (fresh)"
            if codex.get("catalog_fresh")
            else "Codex account catalog (stale)"
        )
        for model in codex.get("models", []):
            if isinstance(model, dict) and isinstance(model.get("selector"), str):
                metadata[model["selector"]] = {
                    "provider_name": "OpenAI / Codex",
                    "source": source,
                    "connected": codex.get("authenticated_catalog") is True,
                }
    claude = inventory.get("claude_code", {})
    if isinstance(claude, dict):
        for model in claude.get("models", []):
            if isinstance(model, dict) and isinstance(model.get("selector"), str):
                source_name = {
                    "configured_default": "Claude Code local default",
                    "local_allowlist": "Claude Code local allowlist",
                    "cli_alias": "Claude Code CLI alias",
                    "documented_alias": "Claude Code documented alias",
                }.get(str(model.get("source")), "Claude Code local configuration")
                metadata[model["selector"]] = {
                    "provider_name": "Anthropic / Claude Code",
                    "source": source_name,
                    "connected": claude.get("authenticated") is True,
                }
    return metadata


def configurable_models(
    config: AppConfig,
    inventory: Mapping[str, Any],
) -> tuple[ConfigurableModel, ...]:
    selections = available_model_selections(inventory)
    metadata = _option_metadata(inventory)
    routes = configured_routes(config)
    hunter_ids: dict[str, list[str]] = {}
    current_judge_selector: str | None = None
    for route in routes:
        selector = route.get("selector")
        if not isinstance(selector, str):
            continue
        if route["role"] == "hunter":
            hunter_ids.setdefault(selector, []).append(str(route["id"]))
        elif route["role"] == "judge":
            current_judge_selector = selector

    options: list[ConfigurableModel] = []
    for selector, selection in selections.items():
        details = metadata.get(selector, {})
        options.append(ConfigurableModel(
            selection=selection,
            provider_name=str(details.get("provider_name") or selection.provider),
            source=str(details.get("source") or "Current Swarmerz profile"),
            connected=(
                details.get("connected")
                if isinstance(details.get("connected"), bool)
                else None
            ),
            current_hunter_ids=tuple(hunter_ids.get(selector, ())),
            current_judge=selector == current_judge_selector,
        ))
    configured_order: dict[str, int] = {}
    for index, route in enumerate(routes):
        selector = route.get("selector")
        if isinstance(selector, str):
            configured_order.setdefault(selector, index)
    harness_order = {"claude-code": 0, "codex-exec": 1, "opencode": 2}

    def catalog_order(option: ConfigurableModel) -> tuple[int, int, str, str]:
        if option.current_hunter_ids or option.current_judge:
            return (
                0,
                configured_order.get(option.selector, len(routes)),
                option.provider_name.casefold(),
                option.selection.model.casefold(),
            )
        return (
            1,
            harness_order.get(option.selection.harness, 9),
            option.provider_name.casefold(),
            option.selection.model.casefold(),
        )

    options.sort(key=catalog_order)
    return tuple(options)


class ModelConfiguratorApp:
    """Scrollable checkbox/radio editor for native model assignments."""

    _FILTERS = (
        ("ALL", None),
        ("CLAUDE", "claude-code"),
        ("CODEX", "codex-exec"),
        ("OPEN", "opencode"),
    )

    def __init__(self, config: AppConfig, inventory: dict[str, Any]) -> None:
        self.config = config
        self.inventory = inventory
        self.options = configurable_models(config, inventory)
        self.index = 0
        self.filter_index = 0
        self.screen = "select"
        self.result: ModelConfigurationRequest | None = None
        self.theme_name = initial_theme_name()
        self.selected_hunters = {
            option.selector for option in self.options if option.current_hunter_ids
        }
        self.current_judge_selector = next(
            (option.selector for option in self.options if option.current_judge),
            None,
        )
        self.judge_selector = self.current_judge_selector
        self.judge_changed = False
        self.workers = 4
        self.locked_hunters = tuple(
            route
            for route in configured_routes(config)
            if route["role"] == "hunter" and route.get("selector") is None
        )
        self.current_judge_label = (
            f"{config.analyzer.provider}/{config.analyzer.effective_model}"
        )
        self.notice = (
            "Space checks hunters. J assigns the highlighted Codex/Claude model as judge. "
            "O discovers an Ollama server; no model request is made."
        )

        self.header = Window(
            FormattedTextControl(self._render_header), height=2, style="class:root"
        )
        self.model_list = Window(
            FormattedTextControl(self._render_models),
            wrap_lines=False,
            style="class:panel",
        )
        self.detail = Window(
            FormattedTextControl(self._render_detail),
            wrap_lines=True,
            style="class:panel",
        )
        self.notice_bar = Window(
            FormattedTextControl(self._render_notice),
            height=2,
            wrap_lines=True,
            style="class:notice",
        )
        self.footer = Window(
            FormattedTextControl(self._render_footer), height=2, style="class:footer"
        )
        model_frame = Frame(
            self.model_list, title=" MODEL CATALOG ", style="class:panel"
        )
        detail_frame = Frame(
            self.detail,
            title=" ASSIGNMENT ",
            width=Dimension(preferred=48, min=34, max=64),
            style="class:panel",
        )
        body = HSplit([
            self.header,
            VSplit([
                model_frame,
                ConditionalContainer(
                    detail_frame,
                    filter=Condition(lambda: terminal_columns() >= 110),
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

    async def run(self) -> ModelConfigurationRequest | None:
        await run_tui_application(self.application)
        return self.result

    @property
    def visible_options(self) -> tuple[ConfigurableModel, ...]:
        harness = self._FILTERS[self.filter_index][1]
        if harness is None:
            return self.options
        return tuple(
            option for option in self.options
            if option.selection.harness == harness
        )

    @property
    def selected(self) -> ConfigurableModel | None:
        options = self.visible_options
        if not options:
            return None
        self.index %= len(options)
        return options[self.index]

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("up", eager=True)
        @bindings.add("c-p", eager=True)
        @bindings.add(Keys.ScrollUp, eager=True)
        def _up(event: object) -> None:
            self._move(-1)

        @bindings.add("down", eager=True)
        @bindings.add("c-n", eager=True)
        @bindings.add(Keys.ScrollDown, eager=True)
        def _down(event: object) -> None:
            self._move(1)

        @bindings.add("pageup", eager=True)
        def _page_up(event: object) -> None:
            self._move(-self._page_size())

        @bindings.add("pagedown", eager=True)
        def _page_down(event: object) -> None:
            self._move(self._page_size())

        @bindings.add("home", eager=True)
        def _home(event: object) -> None:
            self.index = 0
            self.application.invalidate()

        @bindings.add("end", eager=True)
        def _end(event: object) -> None:
            if self.visible_options:
                self.index = len(self.visible_options) - 1
            self.application.invalidate()

        @bindings.add("tab", eager=True)
        def _next_filter(event: object) -> None:
            self._cycle_filter(1)

        @bindings.add("s-tab", eager=True)
        def _previous_filter(event: object) -> None:
            self._cycle_filter(-1)

        @bindings.add("o", eager=True)
        async def _add_ollama(event: object) -> None:
            if self.screen == "select":
                await self._add_ollama_server()

        @bindings.add(" ", eager=True)
        def _toggle(event: object) -> None:
            if self.screen == "select":
                self._toggle_hunter()

        @bindings.add("j", eager=True)
        def _judge(event: object) -> None:
            if self.screen == "select":
                self._set_judge()

        @bindings.add("r", eager=True)
        def _reset_judge(event: object) -> None:
            if self.screen == "select":
                self.judge_selector = self.current_judge_selector
                self.judge_changed = False
                self.notice = "Judge choice reset to the current profile."
                self.application.invalidate()

        @bindings.add("[", eager=True)
        def _fewer_workers(event: object) -> None:
            if self.screen == "select":
                self.workers = max(2, self.workers - 1)
                self.notice = f"New hunters will use {self.workers} workers."
                self.application.invalidate()

        @bindings.add("]", eager=True)
        def _more_workers(event: object) -> None:
            if self.screen == "select":
                self.workers = min(32, self.workers + 1)
                self.notice = f"New hunters will use {self.workers} workers."
                self.application.invalidate()

        @bindings.add("enter", eager=True)
        def _enter(event: object) -> None:
            if self.screen == "select":
                self._open_review()
            else:
                self.result = ModelConfigurationRequest(
                    hunter_selectors=tuple(
                        option.selector
                        for option in self.options
                        if option.selector in self.selected_hunters
                    ),
                    judge_selector=self.judge_selector,
                    judge_changed=self.judge_changed,
                    workers=self.workers,
                )
                self.application.exit()

        @bindings.add("escape", eager=True)
        def _escape(event: object) -> None:
            if self.screen == "review":
                self.screen = "select"
                self.notice = "Review closed. Continue editing model assignments."
                self.application.invalidate()
            else:
                self.application.exit()

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
        options = self.visible_options
        if self.screen != "select" or not options:
            return
        self.index = max(0, min(len(options) - 1, self.index + direction))
        self.application.invalidate()

    def _cycle_filter(self, direction: int) -> None:
        if self.screen != "select":
            return
        self.filter_index = (self.filter_index + direction) % len(self._FILTERS)
        self.index = 0
        label = self._FILTERS[self.filter_index][0]
        self.notice = f"Catalog filter: {label}. Tab and Shift-Tab cycle harnesses."
        self.application.invalidate()

    async def _add_ollama_server(self) -> None:
        try:
            entered = await run_in_terminal(
                lambda: input(
                    "Ollama server URL "
                    "(for example 192.168.8.167:11434; blank cancels): "
                )
            )
        except (EOFError, KeyboardInterrupt):
            entered = ""
        if not entered.strip():
            self.notice = "Ollama discovery cancelled; no configuration changed."
            self.application.invalidate()
            return
        self.notice = f"Reading the Ollama model catalog at {_clip(entered, 120)}…"
        self.application.invalidate()
        try:
            normalized, _inventory_url, models = await asyncio.to_thread(
                list_ollama_models,
                entered,
            )
            selectors = merge_ollama_catalog(self.inventory, normalized, models)
        except (ConfigError, ValueError) as exc:
            self.notice = f"Ollama discovery failed: {_clip(exc, 180)}"
            self.application.invalidate()
            return

        self.options = configurable_models(self.config, self.inventory)
        self.filter_index = next(
            index
            for index, (_label, harness) in enumerate(self._FILTERS)
            if harness == "opencode"
        )
        options = self.visible_options
        selector_set = set(selectors)
        self.index = next(
            (
                index
                for index, option in enumerate(options)
                if option.selector in selector_set
            ),
            0,
        )
        if selectors:
            self.notice = (
                f"Discovered {len(selectors)} Ollama model(s) at {normalized}. "
                "Space checks one as a hunter; tool capability is verified at launch."
            )
        else:
            self.notice = f"Ollama is reachable at {normalized}, but reports no installed models."
        self.application.invalidate()

    def _toggle_hunter(self) -> None:
        option = self.selected
        if option is None:
            return
        if option.selector in self.selected_hunters:
            self.selected_hunters.remove(option.selector)
            if option.current_hunter_ids:
                self.notice = (
                    "Staged removal of hunter "
                    + ", ".join(option.current_hunter_ids)
                    + ". Enter opens the review before anything is written."
                )
            else:
                self.notice = "Removed this pending hunter selection."
        else:
            self.selected_hunters.add(option.selector)
            self.notice = (
                f"Checked {option.selection.model} as a hunter; new entries use "
                f"{self.workers} workers."
            )
        self.application.invalidate()

    def _set_judge(self) -> None:
        option = self.selected
        if option is None:
            return
        if not option.judge_capable:
            self.notice = (
                "OpenCode models can be checked as hunters, but the native judge must use "
                "Codex or Claude Code."
            )
            self.application.invalidate()
            return
        self.judge_selector = option.selector
        self.judge_changed = option.selector != self.current_judge_selector
        self.notice = (
            f"Staged {option.selection.model} as primary judge. Enter reviews the change."
        )
        self.application.invalidate()

    def _staged_hunter_count(self) -> int:
        count = len(self.locked_hunters)
        for option in self.options:
            if option.selector not in self.selected_hunters:
                continue
            count += len(option.current_hunter_ids) if option.current_hunter_ids else 1
        return count

    def _open_review(self) -> None:
        count = self._staged_hunter_count()
        if not 2 <= count <= 26:
            self.notice = f"A valid profile needs 2–26 hunters; the staged fleet has {count}."
            self.application.invalidate()
            return
        self.screen = "review"
        self.notice = (
            "Review the staged diff. Enter validates and atomically writes it; Esc returns "
            "to the catalog; Ctrl-Q cancels without writing."
        )
        self.application.invalidate()

    def _page_size(self) -> int:
        try:
            rows = get_app().output.get_size().rows
        except (AttributeError, RuntimeError):
            rows = 24
        # Header, notice, footer, frame borders, and the catalog's two-line
        # position/key legend all consume rows outside the model entries.
        return max(4, rows - 10)

    def _render_header(self) -> FormattedText:
        judge = next(
            (
                option.selection.model
                for option in self.options
                if option.selector == self.judge_selector
            ),
            self.current_judge_label,
        )
        return render_brand_header([
            ("class:muted", "  MODEL CONFIG  "),
            ("class:acid", self.screen.upper()),
            ("class:muted", "    HUNTERS  "),
            ("class:accent", str(self._staged_hunter_count())),
            ("class:muted", "    JUDGE  "),
            ("class:violet", _clip(judge, 28)),
            ("class:muted", "    FILTER  "),
            ("class:acid", self._FILTERS[self.filter_index][0]),
        ])

    def _render_models(self) -> FormattedText:
        if self.screen == "review":
            return self._render_review()
        options = self.visible_options
        if not options:
            return FormattedText([
                ("class:amber", "\n No models are available in this catalog filter.\n"),
                ("class:soft", " Press Tab to select another harness, or run doctor to inspect discovery."),
            ])
        page_size = self._page_size()
        start = max(0, min(self.index - page_size // 2, len(options) - page_size))
        end = min(len(options), start + page_size)
        rows: list[tuple[str, str]] = [
            ("class:muted", f" {self.index + 1}/{len(options)}  SPACE = HUNTER  J = JUDGE\n\n")
        ]
        for index in range(start, end):
            option = options[index]
            focused = index == self.index
            checked = option.selector in self.selected_hunters
            if checked and option.current_hunter_ids:
                checkbox, check_style = "[✓]", "class:done"
            elif checked:
                checkbox, check_style = "[+]", "class:acid"
            elif option.current_hunter_ids:
                checkbox, check_style = "[-]", "class:danger"
            else:
                checkbox, check_style = "[ ]", "class:muted"
            judge = "[J]" if option.selector == self.judge_selector else "[ ]"
            marker = " ▌ " if focused else " │ "
            harness = {
                "opencode": "OPEN",
                "codex-exec": "CODEX",
                "claude-code": "CLAUDE",
            }.get(option.selection.harness, option.selection.harness.upper())
            narrow = terminal_columns() < 110
            provider_width = 18 if narrow else 24
            model_width = max(18, terminal_columns() - 48) if narrow else 42
            label = (
                f"{harness:<6} "
                f"{_clip(option.provider_name, provider_width):<{provider_width}} "
                f"{_clip(option.selection.model, model_width)}"
            )
            rows.extend([
                ("class:selected.marker" if focused else "class:muted", marker),
                (check_style, checkbox + " "),
                ("class:violet" if judge == "[J]" else "class:muted", judge + " "),
                ("class:selected" if focused else "class:soft", label + "\n"),
            ])
        return FormattedText(rows)

    def _render_detail(self) -> FormattedText:
        if self.screen == "review":
            additions, removals = self._changes()
            return FormattedText([
                ("class:violet", "READY TO APPLY\n\n"),
                ("class:soft", f"Hunters after change: {self._staged_hunter_count()}\n"),
                ("class:acid", f"Additions: {len(additions)}\n"),
                ("class:danger" if removals else "class:muted", f"Removals: {len(removals)}\n"),
                ("class:soft", f"New-hunter workers: {self.workers}\n\n"),
                ("class:muted", "The profile is written only after the full TOML validator passes. "
                 "A failed validation leaves the original file untouched."),
            ])
        option = self.selected
        if option is None:
            return FormattedText([
                ("class:muted", "No native models discovered.\n\n"),
                ("class:soft", f"Preserved custom hunters: {len(self.locked_hunters)}"),
            ])
        hunter_state = (
            "checked"
            if option.selector in self.selected_hunters
            else "not selected"
        )
        if option.current_hunter_ids:
            current = ", ".join(option.current_hunter_ids)
        else:
            current = "not currently configured"
        connected = {
            True: "authenticated/configured",
            False: "project/environment catalog",
            None: "not reported",
        }[option.connected]
        judge_state = (
            "staged primary judge"
            if option.selector == self.judge_selector
            else "eligible" if option.judge_capable else "hunter only"
        )
        return FormattedText([
            ("class:accent", option.selection.model + "\n\n"),
            ("class:muted", "HARNESS\n"),
            ("class:soft", option.selection.harness + "\n\n"),
            ("class:muted", "PROVIDER / PLAN\n"),
            ("class:soft", option.provider_name + "\n"),
            ("class:muted", connected + "\n\n"),
            ("class:muted", "SELECTOR\n"),
            ("class:soft", option.selector + "\n\n"),
            ("class:muted", "SOURCE\n"),
            ("class:soft", option.source + "\n\n"),
            ("class:muted", "HUNTER\n"),
            ("class:acid" if hunter_state == "checked" else "class:soft", hunter_state + "\n"),
            ("class:soft", current + "\n\n"),
            ("class:muted", "JUDGE\n"),
            ("class:violet" if option.selector == self.judge_selector else "class:soft", judge_state),
        ])

    def _changes(self) -> tuple[list[ConfigurableModel], list[str]]:
        additions = [
            option
            for option in self.options
            if option.selector in self.selected_hunters and not option.current_hunter_ids
        ]
        removals = [
            hunter_id
            for option in self.options
            if option.selector not in self.selected_hunters
            for hunter_id in option.current_hunter_ids
        ]
        return additions, removals

    def _render_review(self) -> FormattedText:
        additions, removals = self._changes()
        rows: list[tuple[str, str]] = [
            ("class:violet", " STAGED MODEL CONFIGURATION\n\n"),
        ]
        if additions:
            rows.append(("class:acid", " ADD HUNTERS\n"))
            for option in additions:
                rows.append(("class:soft", f"   + {option.selector}\n"))
        if removals:
            rows.append(("class:danger", "\n REMOVE HUNTERS\n"))
            for hunter_id in removals:
                rows.append(("class:soft", f"   - {hunter_id}\n"))
        if self.judge_changed:
            rows.append(("class:violet", "\n REPLACE PRIMARY JUDGE\n"))
            rows.append(("class:soft", f"   {self.current_judge_label}\n"))
            rows.append(("class:soft", f"   → {self.judge_selector}\n"))
        if self.locked_hunters:
            rows.append(("class:muted", "\n PRESERVED CUSTOM HUNTERS\n"))
            for route in self.locked_hunters:
                rows.append(("class:soft", f"   = {route['id']}\n"))
        if not additions and not removals and not self.judge_changed:
            rows.append(("class:muted", " No configuration changes are staged.\n"))
        return FormattedText(rows)

    def _render_notice(self) -> FormattedText:
        return FormattedText([("class:notice", "  " + _clip(self.notice, 260))])

    def _render_footer(self) -> FormattedText:
        if self.screen == "review":
            items = [
                ("Enter", "validate + apply"),
                ("Esc", "back"),
                ("F9", "theme"),
                ("Ctrl-Q", "cancel"),
            ]
        else:
            items = [
                ("↑/↓ PgUp/PgDn", "scroll"),
                ("Tab", "filter"),
                ("O", "Ollama URL"),
                ("Space", "hunter"),
                ("J", "judge"),
                ("R", "reset judge"),
                ("[/]", f"workers {self.workers}"),
                ("Enter", "review"),
                ("F9", "theme"),
                ("Ctrl-Q", "cancel"),
            ]
        return render_keybar(items, terminal_columns(), max_rows=2)


def _clip(value: object, width: int) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= width else compact[: max(1, width - 1)] + "…"
