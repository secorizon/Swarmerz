# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest import mock

from swarmerz.orchestration.models import ConfigError
from swarmerz.orchestration.opencode_permissions import (
    OpenCodeEndpoint,
    active_session_ids,
    call_opencode_api,
    interrupt_worker_session,
    list_worker_sessions,
    pending_worker_permissions,
    reject_worker_permission,
)


class OpenCodePermissionsTests(unittest.TestCase):
    def test_async_worker_status_uses_authenticated_local_http(self) -> None:
        payload = json.dumps({
            "data": [{"id": "ses_child", "parentID": "ses_parent"}]
        }).encode("utf-8")
        endpoint = OpenCodeEndpoint(
            "http://127.0.0.1:49374", "test-secret", Path("/tmp/hunter-two")
        )

        async def scenario() -> tuple[dict[str, object], bytes]:
            reader = asyncio.StreamReader()
            reader.feed_data(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + payload
            )
            reader.feed_eof()
            writer = mock.Mock()
            writer.drain = mock.AsyncMock()
            writer.wait_closed = mock.AsyncMock()
            with mock.patch(
                "swarmerz.orchestration.opencode_permissions.asyncio.open_connection",
                new=mock.AsyncMock(return_value=(reader, writer)),
            ):
                result = await call_opencode_api(
                    list_worker_sessions, "ses_parent", limit=4, endpoint=endpoint
                )
            request = b"".join(call.args[0] for call in writer.write.call_args_list)
            return result, request

        result, request = asyncio.run(scenario())
        self.assertEqual(result["data"][0]["id"], "ses_child")
        self.assertIn(b"GET /api/session?parentID=ses_parent&limit=20 HTTP/1.1", request)
        self.assertIn(b"Authorization: Basic ", request)
        self.assertIn(b"x-opencode-directory: /tmp/hunter-two", request)

    def test_reads_active_sessions_from_id_keyed_response(self) -> None:
        endpoint = OpenCodeEndpoint("http://127.0.0.1:49374", "test-secret")
        payload = {"data": {
            "ses_running": {"status": "busy"},
            "ses_record": {"id": "ses_other"},
            "ses_stopped": False,
        }}
        with mock.patch(
            "swarmerz.orchestration.opencode_permissions.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ) as request:
            self.assertEqual(
                active_session_ids(endpoint=endpoint), {"ses_running", "ses_record", "ses_other"}
            )
        self.assertTrue(request.call_args.args[0].full_url.endswith("/api/session/active"))

    def test_shared_service_status_uses_hunter_directory(self) -> None:
        endpoint = OpenCodeEndpoint(
            "http://127.0.0.1:49374", "test-secret", Path("/tmp/hunter-two")
        )
        payload = {"data": [{"id": "ses_child", "parentID": "ses_parent"}]}
        with mock.patch(
            "swarmerz.orchestration.opencode_permissions.urlopen",
            return_value=io.BytesIO(json.dumps(payload).encode()),
        ) as request:
            self.assertEqual(
                list_worker_sessions("ses_parent", limit=4, endpoint=endpoint), payload
            )
        self.assertEqual(
            request.call_args.args[0].get_header("X-opencode-directory"),
            "/tmp/hunter-two",
        )

    def test_lists_workers_over_authenticated_local_http_without_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service_dir = Path(directory) / "opencode"
            service_dir.mkdir()
            (service_dir / "service.json").write_text(
                json.dumps({"url": "http://127.0.0.1:49374", "password": "test-secret"}),
                encoding="utf-8",
            )
            payload = {"data": [{"id": "ses_child", "parentID": "ses_parent"}]}
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": directory}), mock.patch(
                "swarmerz.orchestration.opencode_permissions.urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as request:
                self.assertEqual(list_worker_sessions("ses_parent", limit=4), payload)
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_method(), "GET")
            self.assertIn("/api/session?parentID=ses_parent&limit=20", sent.full_url)
            self.assertTrue(sent.get_header("Authorization").startswith("Basic "))

    def test_reads_only_exact_worker_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service_dir = Path(directory) / "opencode"
            service_dir.mkdir()
            (service_dir / "service.json").write_text(
                json.dumps({"url": "http://127.0.0.1:49374", "password": "test-secret"}),
                encoding="utf-8",
            )
            payload = json.dumps({"data": [
                {"id": "per_1", "sessionID": "ses_worker", "action": "external_directory",
                 "resources": ["/outside/repository/*"]},
                {"id": "per_other", "sessionID": "ses_other", "action": "shell",
                 "resources": ["secret"]},
            ]}).encode()
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": directory}), mock.patch(
                "swarmerz.orchestration.opencode_permissions.urlopen",
                return_value=io.BytesIO(payload),
            ) as request:
                result = pending_worker_permissions(["ses_worker"])
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["action"], "external_directory")
            self.assertEqual(result[0]["resources"], ["/outside/repository/*"])
            self.assertTrue(
                request.call_args.args[0].full_url.endswith("/api/permission/request")
            )

    def test_permission_list_falls_back_for_older_opencode(self) -> None:
        missing = HTTPError(
            "http://127.0.0.1:49374/api/permission/request",
            404,
            "Not found",
            {},
            io.BytesIO(),
        )
        payload = json.dumps({"data": [
            {"id": "per_1", "sessionID": "ses_worker", "action": "shell",
             "resources": ["git status"]},
        ]}).encode()
        endpoint = OpenCodeEndpoint("http://127.0.0.1:49374", "test-secret")
        with mock.patch(
            "swarmerz.orchestration.opencode_permissions.urlopen",
            side_effect=[missing, io.BytesIO(payload)],
        ) as requests:
            result = pending_worker_permissions(["ses_worker"], endpoint=endpoint)
        self.assertEqual(result[0]["id"], "per_1")
        self.assertTrue(
            requests.call_args_list[1].args[0].full_url.endswith(
                "/api/session/ses_worker/permission"
            )
        )

    def test_async_permission_list_uses_one_location_request(self) -> None:
        endpoint = OpenCodeEndpoint(
            "http://127.0.0.1:49374", "test-secret", Path("/tmp/hunter-two")
        )
        payload = json.dumps({"data": [
            {"id": "per_1", "sessionID": "ses_worker", "action": "shell",
             "resources": ["git status"]},
            {"id": "per_other", "sessionID": "ses_other", "action": "read",
             "resources": ["secret"]},
        ]}).encode()

        async def scenario() -> list[dict[str, object]]:
            with mock.patch(
                "swarmerz.orchestration.opencode_permissions._async_service_request",
                new=mock.AsyncMock(return_value=(200, payload)),
            ) as request:
                result = await call_opencode_api(
                    pending_worker_permissions,
                    ["ses_worker", "ses_second"],
                    endpoint=endpoint,
                )
            request.assert_awaited_once_with(
                endpoint, "/api/permission/request", timeout=5
            )
            return result

        result = asyncio.run(scenario())
        self.assertEqual([item["id"] for item in result], ["per_1"])

    def test_async_permission_list_retries_one_transient_timeout(self) -> None:
        endpoint = OpenCodeEndpoint(
            "http://127.0.0.1:49374", "test-secret", Path("/tmp/hunter-two")
        )
        payload = json.dumps({"data": []}).encode()

        async def scenario() -> None:
            with mock.patch(
                "swarmerz.orchestration.opencode_permissions._async_service_request",
                new=mock.AsyncMock(side_effect=[TimeoutError("timed out"), (200, payload)]),
            ) as request:
                result = await call_opencode_api(
                    pending_worker_permissions,
                    ["ses_worker"],
                    endpoint=endpoint,
                )
            self.assertEqual(result, [])
            self.assertEqual(request.await_args_list, [
                mock.call(endpoint, "/api/permission/request", timeout=5),
                mock.call(endpoint, "/api/permission/request", timeout=15),
            ])

        asyncio.run(scenario())

    def test_refuses_to_send_credentials_to_remote_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service_dir = Path(directory) / "opencode"
            service_dir.mkdir()
            (service_dir / "service.json").write_text(
                json.dumps({"url": "https://example.com", "password": "test-secret"}),
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": directory}):
                with self.assertRaisesRegex(ConfigError, "not a local HTTP endpoint"):
                    pending_worker_permissions(["ses_worker"])

    def test_rejects_one_child_request_without_granting_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service_dir = Path(directory) / "opencode"
            service_dir.mkdir()
            (service_dir / "service.json").write_text(
                json.dumps({"url": "http://127.0.0.1:49374", "password": "test-secret"}),
                encoding="utf-8",
            )
            response = io.BytesIO()
            response.status = 204
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": directory}), mock.patch(
                "swarmerz.orchestration.opencode_permissions.urlopen", return_value=response
            ) as request:
                self.assertTrue(reject_worker_permission("ses_worker", "per_1"))
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_method(), "POST")
            self.assertTrue(sent.full_url.endswith("/api/session/ses_worker/permission/per_1/reply"))
            self.assertEqual(json.loads(sent.data)["decision"], "reject")
            self.assertNotIn("reply", json.loads(sent.data))
            self.assertNotIn("once", sent.data.decode())

    def test_interrupts_only_the_named_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service_dir = Path(directory) / "opencode"
            service_dir.mkdir()
            (service_dir / "service.json").write_text(
                json.dumps({"url": "http://127.0.0.1:49374", "password": "test-secret"}),
                encoding="utf-8",
            )
            response = io.BytesIO()
            response.status = 200
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": directory}), mock.patch(
                "swarmerz.orchestration.opencode_permissions.urlopen", return_value=response
            ) as request:
                interrupt_worker_session("ses_worker")
            sent = request.call_args.args[0]
            self.assertEqual(sent.get_method(), "POST")
            self.assertTrue(sent.full_url.endswith("/api/session/ses_worker/interrupt"))
            self.assertNotIn("ses_other", sent.full_url)

    def test_permission_reply_falls_back_only_when_server_requires_reply_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service_dir = Path(directory) / "opencode"
            service_dir.mkdir()
            (service_dir / "service.json").write_text(
                json.dumps({"url": "http://127.0.0.1:49374", "password": "test-secret"}),
                encoding="utf-8",
            )
            response = io.BytesIO()
            response.status = 204
            missing_reply = HTTPError(
                "http://127.0.0.1:49374", 400, "Invalid request", {},
                io.BytesIO(b'{"message":"Missing key at [\\"reply\\"]"}'),
            )
            with mock.patch.dict("os.environ", {"XDG_STATE_HOME": directory}), mock.patch(
                "swarmerz.orchestration.opencode_permissions.urlopen",
                side_effect=[missing_reply, response],
            ) as requests:
                reject_worker_permission("ses_worker", "per_1")
            self.assertEqual(json.loads(requests.call_args_list[0].args[0].data)["decision"], "reject")
            self.assertEqual(json.loads(requests.call_args_list[1].args[0].data)["reply"], "reject")

    def test_permission_rejection_is_idempotent_when_request_already_disappeared(self) -> None:
        missing = HTTPError(
            "http://127.0.0.1:49374", 404, "Not found", {},
            io.BytesIO(
                b'{"_tag":"PermissionNotFoundError","requestID":"per_1",'
                b'"message":"Permission request not found: per_1"}'
            ),
        )
        endpoint = OpenCodeEndpoint("http://127.0.0.1:49374", "test-secret")
        with mock.patch(
            "swarmerz.orchestration.opencode_permissions.urlopen", side_effect=missing
        ):
            self.assertFalse(
                reject_worker_permission("ses_worker", "per_1", endpoint=endpoint)
            )

    def test_permission_rejection_does_not_hide_unrelated_404(self) -> None:
        missing = HTTPError(
            "http://127.0.0.1:49374", 404, "Not found", {},
            io.BytesIO(b'{"message":"route not found"}'),
        )
        endpoint = OpenCodeEndpoint("http://127.0.0.1:49374", "test-secret")
        with mock.patch(
            "swarmerz.orchestration.opencode_permissions.urlopen", side_effect=missing
        ):
            with self.assertRaisesRegex(ConfigError, "HTTP 404"):
                reject_worker_permission("ses_worker", "per_1", endpoint=endpoint)
