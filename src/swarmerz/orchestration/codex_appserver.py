# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

"""Small stdio app-server client used only for steerable Codex hunter runs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
from pathlib import Path
from typing import Any, Callable

from .models import CommandSpec, EngineConfig


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class CodexAppServer:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._completed: asyncio.Future[dict[str, Any]] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._last_message = ""

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        async with self._write_lock:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            await self.process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout=30)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise RuntimeError(f"Codex {method}: {response['error']}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Codex {method}: invalid response")
        return result

    async def _read_stdout(self, path: Path, emit: Callable[[str], None]) -> None:
        assert self.process is not None and self.process.stdout is not None
        with path.open("wb") as log:
            while line := await self.process.stdout.readline():
                log.write(line)
                log.flush()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                request_id = event.get("id")
                if isinstance(request_id, int) and request_id in self._pending:
                    future = self._pending[request_id]
                    if not future.done():
                        future.set_result(event)
                    continue
                method = event.get("method")
                params = event.get("params") if isinstance(event.get("params"), dict) else {}
                if method == "turn/completed":
                    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                    if turn.get("id") == self.turn_id:
                        self.turn_id = None
                        if self._completed is not None and not self._completed.done():
                            self._completed.set_result(params)
                elif method == "item/completed":
                    item = params.get("item") if isinstance(params.get("item"), dict) else {}
                    if (
                        item.get("type") == "agentMessage"
                        and isinstance(item.get("text"), str)
                        and params.get("threadId", self.thread_id) == self.thread_id
                    ):
                        self._last_message = item["text"]
                        emit(f"message: {self._last_message[:300]}")
                elif method == "turn/started":
                    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                    if (
                        isinstance(turn.get("id"), str)
                        and params.get("threadId", self.thread_id) == self.thread_id
                    ):
                        self.turn_id = turn["id"]
                elif method == "error":
                    error = params.get("error") if isinstance(params.get("error"), dict) else {}
                    emit(f"ERROR: {error.get('message', 'Codex turn failed')}")
        error = RuntimeError("Codex app-server closed before completing the turn")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        if self._completed is not None and not self._completed.done():
            self._completed.set_exception(error)

    async def _read_stderr(self, path: Path, emit: Callable[[str], None]) -> None:
        assert self.process is not None and self.process.stderr is not None
        model_catalog_warning_reported = False
        command_poll_warning_reported = False
        with path.open("wb") as log:
            while line := await self.process.stderr.readline():
                log.write(line)
                log.flush()
                decoded = line.decode(errors="replace").strip()
                # Codex writes colored tracing output to stderr when it inherits a
                # terminal. Match diagnostics against a plain copy while retaining
                # the original bytes in the artifact log.
                plain = _ANSI_ESCAPE.sub("", decoded)
                if any(marker in plain for marker in (
                    "codex_models_manager::manager: failed to refresh available models: "
                    "request timed out",
                    "codex_models_manager::manager: failed to renew cache TTL: "
                    "EOF while parsing a value",
                )):
                    if not model_catalog_warning_reported:
                        emit(
                            "WARNING: Codex model catalog cache refresh failed; continuing "
                            "with the configured model"
                        )
                        model_catalog_warning_reported = True
                    continue
                if (
                    "codex_core::tools::router" in plain
                    and "write_stdin failed: Unknown process id" in plain
                ):
                    if not command_poll_warning_reported:
                        emit(
                            "WARNING: Codex command polling raced with process exit; "
                            "the worker can continue"
                        )
                        command_poll_warning_reported = True
                    continue
                if "ERROR" in plain or "WARN" in plain:
                    emit(decoded[:300])

    async def start(
        self, spec: CommandSpec, engine: EngineConfig, *, stdout_path: Path,
        stderr_path: Path, emit: Callable[[str], None],
    ) -> None:
        # The normal hunter command is `codex exec ... PROMPT`; retain its pinned
        # executable and environment, but use the documented live RPC transport.
        exec_index = spec.argv.index("exec")
        argv = (
            *spec.argv[:exec_index], "app-server", "--disable", "apps",
            "-c", f"agents.max_concurrent_threads_per_session={engine.swarm_size}",
            *getattr(engine, "extra_args", ()),
        )
        self.process = await asyncio.create_subprocess_exec(
            *argv, cwd=spec.cwd, env=spec.env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=16 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._read_stdout(stdout_path, emit))
        self._stderr_task = asyncio.create_task(self._read_stderr(stderr_path, emit))
        await self._request("initialize", {"clientInfo": {
            "name": "swarmerz", "title": "Swarmerz", "version": "1.0.0",
        }})
        await self._send({"method": "initialized", "params": {}})
        started = await self._request("thread/start", {
            "model": engine.effective_model, "cwd": str(spec.cwd),
            "approvalPolicy": "never", "sandbox": "workspace-write",
            "serviceName": "swarmerz",
        })
        thread = started.get("thread") if isinstance(started.get("thread"), dict) else {}
        self.thread_id = thread.get("id")
        if not isinstance(self.thread_id, str):
            raise RuntimeError("Codex thread/start returned no thread id")

    async def run(self, spec: CommandSpec, engine: EngineConfig) -> dict[str, Any]:
        assert self.thread_id is not None
        self._completed = asyncio.get_running_loop().create_future()
        schema_path = Path(__file__).resolve().parent / "schemas" / "answer.schema.json"
        prompt = spec.argv[-1]
        started = await self._request("turn/start", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt}],
            "model": engine.effective_model,
            "effort": engine.reasoning_effort,
            "cwd": str(spec.cwd),
            "approvalPolicy": "never",
            "outputSchema": json.loads(schema_path.read_text(encoding="utf-8")),
        })
        turn = started.get("turn") if isinstance(started.get("turn"), dict) else {}
        turn_id = turn.get("id")
        if not isinstance(turn_id, str):
            raise RuntimeError("Codex turn/start returned no turn id")
        if not self._completed.done():
            self.turn_id = turn_id
        completed = await asyncio.wait_for(self._completed, timeout=engine.timeout_seconds)
        turn = completed.get("turn") if isinstance(completed.get("turn"), dict) else {}
        status = turn.get("status")
        if status == "completed" and spec.artifact_path and not spec.artifact_path.is_file():
            # The structured final message is also an acceptable artifact when
            # Codex followed the output schema but did not write the file itself.
            try:
                answer = json.loads(self._last_message)
                spec.artifact_path.parent.mkdir(parents=True, exist_ok=True)
                spec.artifact_path.write_text(json.dumps(answer), encoding="utf-8")
            except (ValueError, OSError):
                pass
        error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
        return {"status": "completed" if status == "completed" else "failed",
                "exit_code": 0 if status == "completed" else 1,
                "timed_out": False, "duration_seconds": 0,
                **({"error": error.get("message", str(error))} if error else {})}

    async def steer(self, message: str) -> None:
        if not self.thread_id or not self.turn_id:
            raise RuntimeError("Codex hunter has no active turn")
        await self._request("turn/steer", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": message}],
            "expectedTurnId": self.turn_id,
        })

    async def close(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
