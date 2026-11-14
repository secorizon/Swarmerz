# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

"""Run-local steering registry; it never shares hunter output between hunters."""

from __future__ import annotations

from dataclasses import dataclass

from .codex_appserver import CodexAppServer
from .models import ConfigError
from .opencode_permissions import (
    OpenCodeEndpoint,
    call_opencode_api,
    list_worker_sessions,
    steer_session,
)


WRAP_UP = (
    "Please wrap up now. Stop opening new investigation branches. Ask your active "
    "workers to finish or preserve their best partial results, then consolidate "
    "the available evidence and write the required .swarm/answer.json. Clearly "
    "mark incomplete work and uncertainty. Do not invent findings."
)


@dataclass
class OpenCodeTarget:
    endpoint: OpenCodeEndpoint
    session_id: str | None = None
    parent_running: bool = True
    active: bool = True


class LiveControl:
    def __init__(self, labels: dict[str, str] | None = None) -> None:
        self.opencode: dict[str, OpenCodeTarget] = {}
        self.codex: dict[str, CodexAppServer] = {}
        self.active: set[str] = set()
        self.started: set[str] = set()
        self.labels = labels or {}

    def mark_started(self, hunter_id: str) -> None:
        self.started.add(hunter_id)

    def register_opencode(self, hunter_id: str, endpoint: OpenCodeEndpoint) -> None:
        self.opencode[hunter_id] = OpenCodeTarget(endpoint)
        self.active.add(hunter_id)

    def register_codex(self, hunter_id: str, client: CodexAppServer) -> None:
        self.codex[hunter_id] = client
        self.active.add(hunter_id)

    def set_opencode_session(self, hunter_id: str, session_id: str) -> None:
        if hunter_id in self.opencode and self.opencode[hunter_id].session_id is None:
            self.opencode[hunter_id].session_id = session_id

    def parent_returned(self, hunter_id: str) -> None:
        if hunter_id in self.opencode:
            self.opencode[hunter_id].parent_running = False

    def finished(self, hunter_id: str) -> None:
        self.active.discard(hunter_id)
        self.started.discard(hunter_id)
        if hunter_id in self.opencode:
            self.opencode[hunter_id].active = False
        self.codex.pop(hunter_id, None)

    def status(self) -> str:
        if not self.active:
            return "No hunters are active."
        parts = []
        for hunter_id in sorted(self.active):
            label = self.labels.get(hunter_id, hunter_id)
            if label != hunter_id:
                label = f"{label} ({hunter_id})"
            if hunter_id in self.opencode:
                target = self.opencode[hunter_id]
                parts.append(f"{label}: OpenCode {'ready' if target.session_id else 'starting'}")
            elif hunter_id in self.codex:
                client = self.codex[hunter_id]
                parts.append(f"{label}: Codex {'ready' if client.turn_id else 'starting'}")
            elif hunter_id in self.started:
                parts.append(f"{label}: started; steering transport not ready")
            else:
                parts.append(f"{label}: queued to start")
        return "; ".join(parts)

    async def send(self, message: str, target_id: str | None = None) -> list[str]:
        if not message.strip():
            return ["Empty message ignored"]
        selected = [target_id] if target_id else sorted(self.active)
        outcomes: list[str] = []
        for hunter_id in selected:
            if hunter_id not in self.active:
                outcomes.append(f"{hunter_id}: not active")
                continue
            if hunter_id in self.codex:
                try:
                    await self.codex[hunter_id].steer(message)
                    outcomes.append(f"{hunter_id}: delivered to active Codex turn")
                except (RuntimeError, TimeoutError, OSError, ConfigError) as exc:
                    outcomes.append(f"{hunter_id}: not delivered ({exc})")
                continue
            target = self.opencode.get(hunter_id)
            if target is None:
                outcomes.append(f"{hunter_id}: harness does not support live steering")
                continue
            if not target.session_id:
                outcomes.append(f"{hunter_id}: session not ready; retry shortly")
                continue
            try:
                payload = await call_opencode_api(
                    list_worker_sessions,
                    target.session_id,
                    limit=100,
                    endpoint=target.endpoint,
                )
                records = payload.get("data", [])
                children = [
                    str(record["id"]) for record in records
                    if isinstance(record, dict)
                    and record.get("parentID") == target.session_id
                    and isinstance(record.get("id"), str)
                    and not record.get("outcome")
                ]
                ids = ([target.session_id] if target.parent_running else []) + children
                if not ids:
                    outcomes.append(f"{hunter_id}: no active parent or child session")
                    continue
                delivered = 0
                errors: list[str] = []
                for session_id in ids:
                    try:
                        await call_opencode_api(
                            steer_session, session_id, message, endpoint=target.endpoint
                        )
                        delivered += 1
                    except (RuntimeError, OSError, ConfigError) as exc:
                        errors.append(f"{session_id}: {exc}")
                result = f"{hunter_id}: delivered to {delivered}/{len(ids)} active session(s)"
                if errors:
                    result += f"; failed: {'; '.join(errors)}"
                outcomes.append(result)
            except (RuntimeError, OSError, ConfigError) as exc:
                outcomes.append(f"{hunter_id}: not delivered ({exc})")
        return outcomes
