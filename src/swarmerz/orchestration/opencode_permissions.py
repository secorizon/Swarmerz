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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .models import ConfigError


_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class OpenCodeEndpoint:
    url: str
    password: str
    directory: Path | None = None


def _service_credentials(endpoint: OpenCodeEndpoint | None = None) -> tuple[str, str]:
    if endpoint is None:
        state_root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        service_path = state_root / "opencode" / "service.json"
        try:
            service = json.loads(service_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"OpenCode service credentials unavailable: {service_path}") from exc
        url = service.get("url") if isinstance(service, dict) else None
        password = service.get("password") if isinstance(service, dict) else None
    else:
        url, password = endpoint.url, endpoint.password
    if not isinstance(url, str) or not isinstance(password, str) or not password:
        raise ConfigError("OpenCode service credentials are incomplete")
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigError("OpenCode service URL is not a local HTTP endpoint")
    return url.rstrip("/"), password


def _headers(password: str, endpoint: OpenCodeEndpoint | None) -> dict[str, str]:
    authorization = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
    headers = {"Authorization": f"Basic {authorization}"}
    if endpoint is not None and endpoint.directory is not None:
        headers["x-opencode-directory"] = str(endpoint.directory)
    return headers


async def _async_service_request(
    endpoint: OpenCodeEndpoint | None,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    timeout: float = 5,
    content_type: str | None = None,
) -> tuple[int, bytes]:
    """Make one local HTTP request without blocking the supervisor event loop."""
    service_url, password = _service_credentials(endpoint)
    parsed = urlsplit(service_url)
    host = parsed.hostname
    if host is None:
        raise ConfigError("OpenCode service URL has no host")
    port = parsed.port or 80
    base_path = parsed.path.rstrip("/")
    request_path = f"{base_path}{path}"
    headers = {
        **_headers(password, endpoint),
        "Host": f"[{host}]:{port}" if ":" in host else f"{host}:{port}",
        "Accept": "application/json",
        "Connection": "close",
    }
    if body is not None:
        headers["Content-Length"] = str(len(body))
    if content_type is not None:
        headers["Content-Type"] = content_type
    if any("\r" in value or "\n" in value for value in headers.values()):
        raise ConfigError("OpenCode service headers contain an invalid newline")

    async def exchange() -> tuple[int, bytes]:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            head = [f"{method} {request_path} HTTP/1.1"]
            head.extend(f"{name}: {value}" for name, value in headers.items())
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("utf-8"))
            if body:
                writer.write(body)
            await writer.drain()

            status_line = await reader.readline()
            parts = status_line.decode("iso-8859-1", errors="replace").split(None, 2)
            if len(parts) < 2 or not parts[1].isdigit():
                raise ValueError("invalid HTTP status line")
            status = int(parts[1])
            response_headers: dict[str, str] = {}
            header_bytes = len(status_line)
            while True:
                line = await reader.readline()
                header_bytes += len(line)
                if header_bytes > 65_536:
                    raise ValueError("HTTP response headers are too large")
                if line in {b"\r\n", b"\n"}:
                    break
                if not line:
                    raise ValueError("incomplete HTTP response headers")
                name, separator, value = line.decode(
                    "iso-8859-1", errors="replace"
                ).partition(":")
                if not separator:
                    raise ValueError("invalid HTTP response header")
                response_headers[name.strip().lower()] = value.strip()

            if status in {204, 304}:
                response_body = b""
            elif "chunked" in response_headers.get("transfer-encoding", "").lower():
                chunks: list[bytes] = []
                while True:
                    size_line = await reader.readline()
                    size = int(size_line.split(b";", 1)[0].strip(), 16)
                    if size == 0:
                        while await reader.readline() not in {b"\r\n", b"\n", b""}:
                            pass
                        break
                    chunks.append(await reader.readexactly(size))
                    if await reader.readexactly(2) != b"\r\n":
                        raise ValueError("invalid chunked HTTP response")
                response_body = b"".join(chunks)
            elif "content-length" in response_headers:
                length = int(response_headers["content-length"])
                if length < 0 or length > 64 * 1024 * 1024:
                    raise ValueError("invalid HTTP response length")
                response_body = await reader.readexactly(length)
            else:
                response_body = await reader.read()
            return status, response_body
        finally:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                await writer.wait_closed()

    try:
        return await asyncio.wait_for(exchange(), timeout=timeout)
    except TimeoutError as exc:
        raise TimeoutError("timed out") from exc


def _json_payload(body: bytes) -> object:
    return json.loads(body.decode("utf-8"))


def list_worker_sessions(
    parent_session_id: str, *, limit: int, endpoint: OpenCodeEndpoint | None = None
) -> dict[str, object]:
    """Query the existing local service without invoking OpenCode's CLI recovery path."""
    if not _SESSION_ID.fullmatch(parent_session_id):
        raise ConfigError("invalid OpenCode parent session ID")
    service_url, password = _service_credentials(endpoint)
    query = urlencode({"parentID": parent_session_id, "limit": max(20, limit)})
    request = Request(
        f"{service_url}/api/session?{query}",
        headers=_headers(password, endpoint),
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"OpenCode worker status unavailable: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ConfigError("OpenCode worker status response is invalid")
    return payload


def active_session_ids(*, endpoint: OpenCodeEndpoint | None = None) -> set[str]:
    """Return session IDs that OpenCode currently considers active.

    OpenCode 2.x has returned both an ID-keyed object and a record array from
    this endpoint. Accept either shape, but fail closed on an unknown response
    so callers never classify a worker as orphaned from ambiguous state.
    """
    service_url, password = _service_credentials(endpoint)
    request = Request(
        f"{service_url}/api/session/active",
        headers=_headers(password, endpoint),
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"OpenCode active-session status unavailable: {exc}") from exc
    records = payload.get("data") if isinstance(payload, dict) else None
    active: set[str] = set()
    if isinstance(records, dict):
        for key, value in records.items():
            if isinstance(key, str) and _SESSION_ID.fullmatch(key) and value is not False:
                active.add(key)
            if isinstance(value, dict):
                session_id = value.get("id") or value.get("sessionID")
                if isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id):
                    active.add(session_id)
        return active
    if isinstance(records, list):
        for record in records:
            if isinstance(record, str):
                session_id = record
            elif isinstance(record, dict):
                session_id = record.get("id") or record.get("sessionID")
            else:
                continue
            if isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id):
                active.add(session_id)
        return active
    raise ConfigError("OpenCode active-session response is invalid")


def steer_session(
    session_id: str, message: str, *, endpoint: OpenCodeEndpoint | None = None
) -> None:
    """Append a user instruction to one live OpenCode session."""
    if not _SESSION_ID.fullmatch(session_id):
        raise ConfigError("invalid OpenCode session ID")
    if not message.strip():
        raise ConfigError("steering message is empty")
    service_url, password = _service_credentials(endpoint)
    url = f"{service_url}/api/session/{quote(session_id, safe='')}/prompt"
    # Current OpenCode 2.x uses a flattened prompt body. Retain the nested
    # fallback for older generated v2 contracts and use it only when requested.
    bodies = (
        {"text": message, "delivery": "steer", "resume": True},
        {"prompt": {"text": message}, "delivery": "steer", "resume": True},
    )
    for index, body in enumerate(bodies):
        request = Request(
            url,
            data=json.dumps(body).encode(),
            headers={**_headers(password, endpoint), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            if (
                index == 0
                and exc.code == 400
                and "Missing key" in detail
                and "prompt" in detail
            ):
                continue
            suffix = detail[:300] or str(exc.reason)
            raise ConfigError(
                f"OpenCode steering failed for {session_id}: HTTP {exc.code}: {suffix}"
            ) from exc
        except (OSError, ValueError) as exc:
            raise ConfigError(f"OpenCode steering failed for {session_id}: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ConfigError(f"OpenCode steering response invalid for {session_id}")
        return


def pending_worker_permissions(
    session_ids: list[str], *, endpoint: OpenCodeEndpoint | None = None
) -> list[dict[str, object]]:
    """Read pending requests for exact child sessions; never reply or save approvals.

    OpenCode 2.0.21 exposes a location-wide endpoint, so one request can replace
    one request per active worker. Fall back to the session endpoint for older
    2.x servers that do not provide the batched route.
    """
    session_id_set = set(session_ids)
    for session_id in session_id_set:
        if not _SESSION_ID.fullmatch(session_id):
            raise ConfigError("invalid OpenCode child session ID in worker status")
    if not session_id_set:
        return []
    service_url, password = _service_credentials(endpoint)
    request = Request(
        f"{service_url}/api/permission/request",
        headers=_headers(password, endpoint),
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code != 404:
            raise ConfigError(f"OpenCode permission status unavailable: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(f"OpenCode permission status unavailable: {exc}") from exc
    else:
        return _pending_permissions_from_payload(payload, session_id_set)

    pending: list[dict[str, object]] = []
    for session_id in session_id_set:
        request = Request(
            f"{service_url}/api/session/{quote(session_id, safe='')}/permission",
            headers=_headers(password, endpoint),
        )
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.load(response)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"OpenCode permission status unavailable for {session_id}: {exc}") from exc
        pending.extend(_pending_permissions_from_payload(payload, {session_id}))
    return pending


def _pending_permissions_from_payload(
    payload: object, session_ids: set[str]
) -> list[dict[str, object]]:
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ConfigError("OpenCode permission response is invalid")
    pending: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        session_id = record.get("sessionID")
        if not isinstance(session_id, str) or session_id not in session_ids:
            continue
        resources = record.get("resources")
        pending.append(
            {
                "id": str(record.get("id") or ""),
                "session_id": session_id,
                "action": str(record.get("action") or "unknown"),
                "resources": (
                    [str(item) for item in resources]
                    if isinstance(resources, list)
                    else []
                ),
            }
        )
    return pending


def reject_worker_permission(
    session_id: str, request_id: str, *, endpoint: OpenCodeEndpoint | None = None
) -> bool:
    """Reject one child's pending request; return false if it already disappeared.

    OpenCode can return two equivalent pending records for one tool action and
    remove both when the first is rejected. Treat the resulting, request-specific
    ``PermissionNotFoundError`` as an idempotent success instead of failing the
    whole hunter. Never grant access or save a rule.
    """
    if not _SESSION_ID.fullmatch(session_id) or not _SESSION_ID.fullmatch(request_id):
        raise ConfigError("invalid OpenCode session or permission request ID")
    service_url, password = _service_credentials(endpoint)
    message = (
        "This unattended worker cannot receive additional permissions. Continue without "
        "this resource, stay inside the assigned workspace, and do not inspect sensitive "
        "files or dependency caches."
    )
    # Current OpenCode 2.x requires "decision"; retain the generated-contract
    # fallback and use it only when the server explicitly requests "reply".
    for field in ("decision", "reply"):
        request = Request(
            f"{service_url}/api/session/{quote(session_id, safe='')}/permission/"
            f"{quote(request_id, safe='')}/reply",
            data=json.dumps({field: "reject", "message": message}).encode("utf-8"),
            headers={**_headers(password, endpoint), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                if response.status != 204:
                    raise ConfigError(
                        f"OpenCode permission rejection returned status {response.status}"
                    )
            return True
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if field == "decision" and exc.code == 400 and "Missing key" in detail and "reply" in detail:
                continue
            if (
                exc.code == 404
                and "PermissionNotFoundError" in detail
                and request_id in detail
            ):
                return False
            raise ConfigError(
                f"OpenCode permission rejection failed for child {session_id}: "
                f"HTTP {exc.code}: {detail[:300]}"
            ) from exc
        except OSError as exc:
            raise ConfigError(
                f"OpenCode permission rejection failed for child {session_id}: {exc}"
            ) from exc


def interrupt_worker_session(
    session_id: str, *, endpoint: OpenCodeEndpoint | None = None
) -> None:
    """Stop an unfinished OpenCode child or parent session."""
    if not _SESSION_ID.fullmatch(session_id):
        raise ConfigError("invalid OpenCode child session ID")
    service_url, password = _service_credentials(endpoint)
    request = Request(
        f"{service_url}/api/session/{quote(session_id, safe='')}/interrupt",
        data=b"",
        headers=_headers(password, endpoint),
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status not in {200, 204}:
                raise ConfigError(
                    f"OpenCode worker interruption returned status {response.status}"
                )
    except OSError as exc:
        raise ConfigError(
            f"OpenCode worker interruption failed for child {session_id}: {exc}"
        ) from exc


async def async_list_worker_sessions(
    parent_session_id: str, *, limit: int, endpoint: OpenCodeEndpoint | None = None
) -> dict[str, object]:
    if not _SESSION_ID.fullmatch(parent_session_id):
        raise ConfigError("invalid OpenCode parent session ID")
    query = urlencode({"parentID": parent_session_id, "limit": max(20, limit)})
    try:
        status, body = await _async_service_request(
            endpoint, f"/api/session?{query}", timeout=5
        )
        if status != 200:
            raise ConfigError(f"OpenCode worker status returned HTTP {status}")
        payload = _json_payload(body)
    except ConfigError:
        raise
    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ConfigError(f"OpenCode worker status unavailable: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ConfigError("OpenCode worker status response is invalid")
    return payload


async def async_active_session_ids(
    *, endpoint: OpenCodeEndpoint | None = None
) -> set[str]:
    try:
        status, body = await _async_service_request(
            endpoint, "/api/session/active", timeout=5
        )
        if status != 200:
            raise ConfigError(f"OpenCode active-session status returned HTTP {status}")
        payload = _json_payload(body)
    except ConfigError:
        raise
    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ConfigError(f"OpenCode active-session status unavailable: {exc}") from exc
    records = payload.get("data") if isinstance(payload, dict) else None
    active: set[str] = set()
    if isinstance(records, dict):
        for key, value in records.items():
            if isinstance(key, str) and _SESSION_ID.fullmatch(key) and value is not False:
                active.add(key)
            if isinstance(value, dict):
                session_id = value.get("id") or value.get("sessionID")
                if isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id):
                    active.add(session_id)
        return active
    if isinstance(records, list):
        for record in records:
            if isinstance(record, str):
                session_id = record
            elif isinstance(record, dict):
                session_id = record.get("id") or record.get("sessionID")
            else:
                continue
            if isinstance(session_id, str) and _SESSION_ID.fullmatch(session_id):
                active.add(session_id)
        return active
    raise ConfigError("OpenCode active-session response is invalid")


async def async_steer_session(
    session_id: str, message: str, *, endpoint: OpenCodeEndpoint | None = None
) -> None:
    if not _SESSION_ID.fullmatch(session_id):
        raise ConfigError("invalid OpenCode session ID")
    if not message.strip():
        raise ConfigError("steering message is empty")
    path = f"/api/session/{quote(session_id, safe='')}/prompt"
    bodies = (
        {"text": message, "delivery": "steer", "resume": True},
        {"prompt": {"text": message}, "delivery": "steer", "resume": True},
    )
    for index, request_body in enumerate(bodies):
        try:
            status, body = await _async_service_request(
                endpoint,
                path,
                method="POST",
                body=json.dumps(request_body).encode("utf-8"),
                timeout=10,
                content_type="application/json",
            )
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise ConfigError(f"OpenCode steering failed for {session_id}: {exc}") from exc
        detail = body.decode("utf-8", errors="replace").strip()
        if index == 0 and status == 400 and "Missing key" in detail and "prompt" in detail:
            continue
        if not 200 <= status < 300:
            raise ConfigError(
                f"OpenCode steering failed for {session_id}: HTTP {status}: {detail[:300]}"
            )
        try:
            payload = _json_payload(body)
        except ValueError as exc:
            raise ConfigError(f"OpenCode steering response invalid for {session_id}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ConfigError(f"OpenCode steering response invalid for {session_id}")
        return


async def async_pending_worker_permissions(
    session_ids: list[str], *, endpoint: OpenCodeEndpoint | None = None
) -> list[dict[str, object]]:
    session_id_set = set(session_ids)
    for session_id in session_id_set:
        if not _SESSION_ID.fullmatch(session_id):
            raise ConfigError("invalid OpenCode child session ID in worker status")
    if not session_id_set:
        return []

    async def request(path: str) -> tuple[int, bytes]:
        # A busy shared OpenCode service can take longer than the ordinary
        # five-second status budget while model turns are streaming. Retry one
        # timeout with a fresh connection and a wider window before declaring
        # the permission monitor unavailable. This never approves a request.
        for timeout in (5, 15):
            try:
                return await _async_service_request(endpoint, path, timeout=timeout)
            except TimeoutError:
                if timeout == 15:
                    raise
        raise AssertionError("unreachable permission request retry state")

    try:
        status, body = await request("/api/permission/request")
        if status != 404:
            if status != 200:
                raise ConfigError(
                    f"OpenCode permission status returned HTTP {status}"
                )
            payload = _json_payload(body)
            return _pending_permissions_from_payload(payload, session_id_set)
    except ConfigError:
        raise
    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ConfigError(f"OpenCode permission status unavailable: {exc}") from exc

    pending: list[dict[str, object]] = []
    for session_id in session_id_set:
        try:
            status, body = await request(
                f"/api/session/{quote(session_id, safe='')}/permission"
            )
            if status != 200:
                raise ConfigError(
                    f"OpenCode permission status returned HTTP {status} for {session_id}"
                )
            payload = _json_payload(body)
        except ConfigError:
            raise
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise ConfigError(
                f"OpenCode permission status unavailable for {session_id}: {exc}"
            ) from exc
        pending.extend(_pending_permissions_from_payload(payload, {session_id}))
    return pending


async def async_reject_worker_permission(
    session_id: str, request_id: str, *, endpoint: OpenCodeEndpoint | None = None
) -> bool:
    if not _SESSION_ID.fullmatch(session_id) or not _SESSION_ID.fullmatch(request_id):
        raise ConfigError("invalid OpenCode session or permission request ID")
    message = (
        "This unattended worker cannot receive additional permissions. Continue without "
        "this resource, stay inside the assigned workspace, and do not inspect sensitive "
        "files or dependency caches."
    )
    path = (
        f"/api/session/{quote(session_id, safe='')}/permission/"
        f"{quote(request_id, safe='')}/reply"
    )
    for field in ("decision", "reply"):
        try:
            status, body = await _async_service_request(
                endpoint,
                path,
                method="POST",
                body=json.dumps({field: "reject", "message": message}).encode("utf-8"),
                timeout=5,
                content_type="application/json",
            )
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise ConfigError(
                f"OpenCode permission rejection failed for child {session_id}: {exc}"
            ) from exc
        detail = body.decode("utf-8", errors="replace")
        if status == 204:
            return True
        if field == "decision" and status == 400 and "Missing key" in detail and "reply" in detail:
            continue
        if status == 404 and "PermissionNotFoundError" in detail and request_id in detail:
            return False
        raise ConfigError(
            f"OpenCode permission rejection failed for child {session_id}: "
            f"HTTP {status}: {detail[:300]}"
        )
    raise ConfigError(f"OpenCode permission rejection failed for child {session_id}")


async def async_interrupt_worker_session(
    session_id: str, *, endpoint: OpenCodeEndpoint | None = None
) -> None:
    if not _SESSION_ID.fullmatch(session_id):
        raise ConfigError("invalid OpenCode child session ID")
    try:
        status, _body = await _async_service_request(
            endpoint,
            f"/api/session/{quote(session_id, safe='')}/interrupt",
            method="POST",
            body=b"",
            timeout=5,
        )
    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ConfigError(
            f"OpenCode worker interruption failed for child {session_id}: {exc}"
        ) from exc
    if status not in {200, 204}:
        raise ConfigError(f"OpenCode worker interruption returned status {status}")


_ASYNC_EQUIVALENTS: dict[Callable[..., Any], Callable[..., Awaitable[Any]]] = {
    list_worker_sessions: async_list_worker_sessions,
    active_session_ids: async_active_session_ids,
    steer_session: async_steer_session,
    pending_worker_permissions: async_pending_worker_permissions,
    reject_worker_permission: async_reject_worker_permission,
    interrupt_worker_session: async_interrupt_worker_session,
}


async def call_opencode_api(
    function: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Use native async HTTP for production and direct calls for injected test doubles."""
    async_function = _ASYNC_EQUIVALENTS.get(function)
    if async_function is not None:
        return await async_function(*args, **kwargs)
    return function(*args, **kwargs)
