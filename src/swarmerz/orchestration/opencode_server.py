# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote

from .models import CommandSpec, ConfigError
from .opencode_permissions import OpenCodeEndpoint, _async_service_request


_LISTENING = re.compile(r"server listening on (http://127\.0\.0\.1:\d+)")


def attach_server(spec: CommandSpec, endpoint: OpenCodeEndpoint) -> CommandSpec:
    """Route an OpenCode run/resume to one explicit server, never the shared service."""
    argv = list(spec.argv)
    try:
        index = argv.index("run")
    except ValueError as exc:
        raise ConfigError("cannot attach an OpenCode server to a non-run command") from exc
    if "--server" in argv:
        raise ConfigError("OpenCode command already selects a server")
    argv[index + 1:index + 1] = ["--server", endpoint.url]
    env = dict(spec.env)
    env["OPENCODE_PASSWORD"] = endpoint.password
    return CommandSpec(tuple(argv), spec.cwd, env, spec.artifact_path)


def _server_ready(endpoint: OpenCodeEndpoint) -> bool:
    authorization = base64.b64encode(f"opencode:{endpoint.password}".encode()).decode("ascii")
    headers = {"Authorization": f"Basic {authorization}"}
    if endpoint.directory is not None:
        headers["x-opencode-directory"] = str(endpoint.directory)
    request = Request(
        f"{endpoint.url}/api/session?limit=1",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=2) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def _server_has_model(endpoint: OpenCodeEndpoint, model_ref: str) -> bool:
    provider_id, separator, model_id = model_ref.partition("/")
    if not separator or not model_id:
        raise ConfigError(f"invalid OpenCode model reference: {model_ref}")
    authorization = base64.b64encode(f"opencode:{endpoint.password}".encode()).decode("ascii")
    headers = {"Authorization": f"Basic {authorization}"}
    if endpoint.directory is not None:
        headers["x-opencode-directory"] = str(endpoint.directory)
    request = Request(
        f"{endpoint.url}/api/model?providerID={quote(provider_id)}",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"OpenCode model catalog unavailable: {exc}") from exc
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ConfigError("OpenCode model catalog response is invalid")
    return any(
        isinstance(item, dict)
        and (
            item.get("id") == model_ref
            or (item.get("providerID") == provider_id and item.get("modelID") == model_id)
        )
        for item in models
    )


_SYNC_SERVER_READY = _server_ready
_SYNC_SERVER_HAS_MODEL = _server_has_model


async def _async_server_ready(endpoint: OpenCodeEndpoint) -> bool:
    try:
        status, _body = await _async_service_request(
            endpoint, "/api/session?limit=1", timeout=2
        )
    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
        return False
    return status == 200


async def _async_server_has_model(
    endpoint: OpenCodeEndpoint, model_ref: str
) -> bool:
    provider_id, separator, model_id = model_ref.partition("/")
    if not separator or not model_id:
        raise ConfigError(f"invalid OpenCode model reference: {model_ref}")
    try:
        status, body = await _async_service_request(
            endpoint,
            f"/api/model?providerID={quote(provider_id)}",
            timeout=8,
        )
        if status != 200:
            raise ConfigError(f"OpenCode model catalog returned HTTP {status}")
        payload = json.loads(body.decode("utf-8"))
    except ConfigError:
        raise
    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ConfigError(f"OpenCode model catalog unavailable: {exc}") from exc
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ConfigError("OpenCode model catalog response is invalid")
    return any(
        isinstance(item, dict)
        and (
            item.get("id") == model_ref
            or (
                item.get("providerID") == provider_id
                and item.get("modelID") == model_id
            )
        )
        for item in models
    )


async def _check_server_ready(endpoint: OpenCodeEndpoint) -> bool:
    if _server_ready is not _SYNC_SERVER_READY:
        return _server_ready(endpoint)
    return await _async_server_ready(endpoint)


async def _check_server_has_model(
    endpoint: OpenCodeEndpoint, model_ref: str
) -> bool:
    if _server_has_model is not _SYNC_SERVER_HAS_MODEL:
        return _server_has_model(endpoint, model_ref)
    return await _async_server_has_model(endpoint, model_ref)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


@dataclass
class IsolatedOpenCodeServer:
    """A private authenticated endpoint, shared by native hunters in one round.

    OpenCode's database is user-global even when server ports are distinct, so
    the runner must create only one of these at a time for a round.
    """

    process: asyncio.subprocess.Process
    endpoint: OpenCodeEndpoint
    stdout_task: asyncio.Task[None]
    stderr_path: Path
    exit_task: asyncio.Task[None] | None = None
    result_path: Path | None = None
    started_at: float | None = None

    async def wait_ready(
        self, endpoint: OpenCodeEndpoint, *, timeout_seconds: int = 45,
    ) -> None:
        """Verify the already-running service accepts this hunter workspace."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self.process.returncode is not None:
                raise ConfigError(
                    f"shared OpenCode service exited ({self.process.returncode}); "
                    f"log: {self.stderr_path}"
                )
            if await _check_server_ready(endpoint):
                return
            if time.monotonic() >= deadline:
                raise ConfigError(
                    f"shared OpenCode service did not answer for {endpoint.directory} "
                    f"within {timeout_seconds}s; log: {self.stderr_path}"
                )
            await asyncio.sleep(2)

    async def wait_model(
        self, endpoint: OpenCodeEndpoint, model_ref: str, *, timeout_seconds: int = 30,
    ) -> None:
        """Fail before inference if this workspace cannot select the requested model."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self.process.returncode is not None:
                raise ConfigError(f"shared OpenCode service exited while checking {model_ref}")
            if await _check_server_has_model(endpoint, model_ref):
                return
            if time.monotonic() >= deadline:
                raise ConfigError(
                    f"OpenCode cannot route {model_ref} for {endpoint.directory}; "
                    "the model is absent from this workspace's OpenCode catalog"
                )
            await asyncio.sleep(2)

    @classmethod
    async def start(
        cls, spec: CommandSpec, *, artifact_dir: Path, startup_seconds: int = 45
    ) -> IsolatedOpenCodeServer:
        try:
            run_index = spec.argv.index("run")
        except ValueError as exc:
            raise ConfigError("native OpenCode hunter has no run command") from exc
        executable = spec.argv[:run_index]
        password = secrets.token_urlsafe(32)
        env = dict(spec.env)
        env["OPENCODE_SERVER_PASSWORD"] = password
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = artifact_dir / "isolated-server.stderr.log"
        with stderr_path.open("wb") as stderr_handle:
            process = await asyncio.create_subprocess_exec(
                *executable, "serve", "--hostname", "127.0.0.1", "--port", "0",
                cwd=spec.cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_handle,
                start_new_session=True,
            )
        assert process.stdout is not None
        deadline = time.monotonic() + startup_seconds
        endpoint: OpenCodeEndpoint | None = None
        try:
            while endpoint is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConfigError("isolated OpenCode server did not announce its URL")
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                except TimeoutError as exc:
                    raise ConfigError("isolated OpenCode server startup timed out") from exc
                if not line:
                    raise ConfigError(
                        f"isolated OpenCode server exited during startup; log: {stderr_path}"
                    )
                match = _LISTENING.search(line.decode("utf-8", errors="replace"))
                if match:
                    endpoint = OpenCodeEndpoint(match.group(1), password)
            while not await _check_server_ready(endpoint):
                if process.returncode is not None or time.monotonic() >= deadline:
                    raise ConfigError(
                        f"isolated OpenCode server failed its authenticated readiness check; log: {stderr_path}"
                    )
                await asyncio.sleep(0.2)
        except BaseException:
            await _terminate(process)
            raise

        async def drain_stdout() -> None:
            # The server can print its password. Never persist or display stdout.
            while await process.stdout.readline():
                pass

        result_path = artifact_dir / "server-result.json"
        started_at = time.time()

        async def record_exit() -> None:
            returncode = await process.wait()
            result_path.write_text(
                json.dumps(
                    {
                        "pid": process.pid,
                        "returncode": returncode,
                        "started_at_epoch": started_at,
                        "exited_at_epoch": time.time(),
                        "expected_stop": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

        server = cls(
            process,
            endpoint,
            asyncio.create_task(drain_stdout()),
            stderr_path,
            result_path=result_path,
            started_at=started_at,
        )
        server.exit_task = asyncio.create_task(record_exit())
        return server

    async def stop(self) -> None:
        stop_requested_while_running = self.process.returncode is None
        await _terminate(self.process)
        if self.exit_task is not None:
            await asyncio.gather(self.exit_task, return_exceptions=True)
        if self.result_path is not None and self.result_path.is_file():
            try:
                result = json.loads(self.result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = {}
            result["expected_stop"] = stop_requested_while_running
            if stop_requested_while_running:
                result["stopped_at_epoch"] = time.time()
            self.result_path.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(self.stdout_task, timeout=5)
