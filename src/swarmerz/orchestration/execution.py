# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Callable

from .models import CommandSpec, ConfigError
from .resource_limits import ResourceInvocation, ResourceLimiter
from .storage import prepare_managed_environment


async def execute(
    spec: CommandSpec,
    *,
    timeout_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
    on_output: Callable[[str, str], None] | None = None,
    append: bool = False,
    stop_event: asyncio.Event | None = None,
    resource_limiter: ResourceLimiter | None = None,
) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    stopped = False
    try:
        prepare_managed_environment(spec.env)
    except OSError as exc:
        return {
            "status": "failed_to_start",
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": f"unable to prepare managed storage: {exc}",
        }
    try:
        invocation = (
            resource_limiter.prepare(
                spec.argv,
                role=spec.env.get("SWARM_ROLE_ID", "command"),
                runtime_seconds=timeout_seconds + 30,
                environment=spec.env,
            )
            if resource_limiter is not None
            else ResourceInvocation(spec.argv)
        )
    except ConfigError as exc:
        return {
            "status": "failed_to_start",
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": str(exc),
        }
    scope = (
        {"resource_scope": invocation.unit_name}
        if invocation.unit_name is not None
        else {}
    )
    mode = "ab" if append else "wb"
    with stdout_path.open(mode) as stdout_handle, stderr_path.open(mode) as stderr_handle:
        pump_tasks: list[asyncio.Task[None]] = []
        try:
            process = await asyncio.create_subprocess_exec(
                *invocation.argv,
                cwd=spec.cwd,
                env=invocation.spawn_environment(spec.env),
                stdout=asyncio.subprocess.PIPE if on_output is not None else stdout_handle,
                stderr=asyncio.subprocess.PIPE if on_output is not None else stderr_handle,
                start_new_session=True,
                limit=16 * 1024 * 1024,
            )
        except FileNotFoundError as exc:
            return {
                "status": "failed_to_start",
                "exit_code": None,
                "timed_out": False,
                "duration_seconds": round(time.monotonic() - started, 3),
                "error": str(exc),
                **scope,
            }

        async def pump(
            reader: asyncio.StreamReader,
            handle: Any,
            stream_name: str,
        ) -> None:
            while line := await reader.readline():
                handle.write(line)
                handle.flush()
                try:
                    on_output(stream_name, line.decode("utf-8", errors="replace").rstrip("\r\n"))
                except Exception:
                    # A display callback must never interfere with the provider process
                    # or the complete raw logs.
                    continue

        if on_output is not None:
            assert process.stdout is not None and process.stderr is not None
            pump_tasks = [
                asyncio.create_task(pump(process.stdout, stdout_handle, "stdout")),
                asyncio.create_task(pump(process.stderr, stderr_handle, "stderr")),
            ]
        wait_task = asyncio.create_task(process.wait())
        stop_task = asyncio.create_task(stop_event.wait()) if stop_event is not None else None

        async def signal_process_tree(signal_to_send: signal.Signals) -> None:
            if resource_limiter is not None:
                await resource_limiter.signal_scope(invocation, signal_to_send)
            try:
                os.killpg(process.pid, signal_to_send)
            except ProcessLookupError:
                pass

        async def terminate_process() -> int:
            await signal_process_tree(signal.SIGTERM)
            try:
                return await asyncio.wait_for(asyncio.shield(wait_task), timeout=10)
            except TimeoutError:
                await signal_process_tree(signal.SIGKILL)
                return await wait_task

        try:
            waits = {wait_task}
            if stop_task is not None:
                waits.add(stop_task)
            completed, _ = await asyncio.wait(
                waits, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task in completed:
                exit_code = wait_task.result()
            else:
                stopped = stop_task is not None and stop_task in completed
                timed_out = not stopped
                exit_code = await terminate_process()
        except asyncio.CancelledError:
            await terminate_process()
            raise
        finally:
            if stop_task is not None:
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
            if pump_tasks:
                await asyncio.gather(*pump_tasks, return_exceptions=True)
    return {
        "status": (
            "stopped" if stopped else
            "timed_out" if timed_out else
            "completed" if exit_code == 0 else "failed"
        ),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stopped": stopped,
        "duration_seconds": round(time.monotonic() - started, 3),
        **scope,
    }


def parse_json_output(path: Path) -> Any | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    parsed: list[Any] = []
    for line in text.splitlines():
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed or None
