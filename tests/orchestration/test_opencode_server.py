# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarmerz.orchestration.artifacts import collect_partial_notes
from swarmerz.orchestration.models import CommandSpec, ConfigError
from swarmerz.orchestration.opencode_permissions import OpenCodeEndpoint
from swarmerz.orchestration.opencode_server import (
    IsolatedOpenCodeServer, _server_has_model, _server_ready, attach_server,
)
from swarmerz.orchestration.runner import (
    _opencode_round_slot,
    _per_hunter_opencode_service_spec,
    _shared_opencode_service_spec,
)


class IsolatedServerTests(unittest.TestCase):
    def test_readiness_checks_the_hunter_workspace(self) -> None:
        endpoint = OpenCodeEndpoint(
            "http://127.0.0.1:42001", "private-password", Path("/tmp/hunter-one")
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        with mock.patch("swarmerz.orchestration.opencode_server.urlopen", return_value=response) as open_url:
            self.assertTrue(_server_ready(endpoint))
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("X-opencode-directory"), "/tmp/hunter-one")

    def test_model_catalog_checks_requested_workspace_without_inference(self) -> None:
        endpoint = OpenCodeEndpoint(
            "http://127.0.0.1:42001", "private-password", Path("/tmp/ollama-hunter")
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"data": [{
            "id": "ollama/swarmerz:q38", "providerID": "ollama",
            "modelID": "swarmerz:q38",
        }]}).encode()
        with mock.patch("swarmerz.orchestration.opencode_server.urlopen", return_value=response) as open_url:
            self.assertTrue(_server_has_model(endpoint, "ollama/swarmerz:q38"))
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:42001/api/model?providerID=ollama")
        self.assertEqual(request.get_header("X-opencode-directory"), "/tmp/ollama-hunter")

    def test_workspace_readiness_retries_without_starting_a_second_service(self) -> None:
        async def exercise() -> None:
            endpoint = OpenCodeEndpoint(
                "http://127.0.0.1:42001", "private-password", Path("/tmp/hunter-one")
            )
            server = IsolatedOpenCodeServer(
                mock.Mock(returncode=None), endpoint, mock.Mock(), Path("/tmp/server.log")
            )
            with mock.patch("swarmerz.orchestration.opencode_server._server_ready", side_effect=[False, True]) as check, mock.patch(
                "swarmerz.orchestration.opencode_server.asyncio.sleep", new=mock.AsyncMock()
            ):
                await server.wait_ready(endpoint, timeout_seconds=5)
            self.assertEqual(check.call_count, 2)

        asyncio.run(exercise())

    def test_attaches_run_without_exposing_password_in_arguments(self) -> None:
        spec = CommandSpec(
            ("opencode", "run", "--model", "provider/model", "prompt"),
            Path("/tmp"), {"PATH": "/usr/bin"}, None,
        )
        endpoint = OpenCodeEndpoint("http://127.0.0.1:42001", "private-password")
        attached = attach_server(spec, endpoint)
        self.assertEqual(attached.argv[1:4], ("run", "--server", endpoint.url))
        self.assertEqual(attached.env["OPENCODE_PASSWORD"], "private-password")
        self.assertNotIn("private-password", " ".join(attached.argv))
        self.assertNotIn("--server", spec.argv)
        with self.assertRaisesRegex(ConfigError, "already selects"):
            attach_server(attached, endpoint)

    def test_starts_random_port_server_and_stops_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = CommandSpec(
                ("opencode", "run", "prompt"), root, {"PATH": "/usr/bin"}, None,
            )

            async def exercise() -> None:
                reader = asyncio.StreamReader()
                reader.feed_data(b"server listening on http://127.0.0.1:42001\n")
                reader.feed_eof()
                process = mock.Mock()
                process.stdout = reader
                process.pid = 12345
                process.returncode = None
                process.wait = mock.AsyncMock(return_value=0)
                with mock.patch(
                    "swarmerz.orchestration.opencode_server.asyncio.create_subprocess_exec",
                    new=mock.AsyncMock(return_value=process),
                ) as spawn, mock.patch(
                    "swarmerz.orchestration.opencode_server._server_ready", return_value=True
                ), mock.patch(
                    "swarmerz.orchestration.opencode_server._terminate", new=mock.AsyncMock()
                ) as terminate:
                    server = await IsolatedOpenCodeServer.start(
                        spec, artifact_dir=root / "artifacts"
                    )
                    self.assertEqual(server.endpoint.url, "http://127.0.0.1:42001")
                    call = spawn.call_args
                    self.assertIn("serve", call.args)
                    self.assertEqual(call.kwargs["env"]["OPENCODE_SERVER_PASSWORD"], server.endpoint.password)
                    self.assertTrue(call.kwargs["start_new_session"])
                    await server.stop()
                    terminate.assert_awaited_once_with(process)
                    result = json.loads(
                        (root / "artifacts" / "server-result.json").read_text()
                    )
                    self.assertEqual(result["returncode"], 0)
                    self.assertTrue(result["expected_stop"])

            asyncio.run(exercise())

    def test_cleanup_preserves_an_unexpected_server_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "server-result.json"
            result_path.write_text(json.dumps({
                "pid": 12345,
                "returncode": 7,
                "expected_stop": False,
            }))

            async def exercise() -> None:
                async def drained() -> None:
                    return None

                process = mock.Mock(returncode=7)
                server = IsolatedOpenCodeServer(
                    process,
                    OpenCodeEndpoint("http://127.0.0.1:42001", "private"),
                    asyncio.create_task(drained()),
                    root / "stderr.log",
                    result_path=result_path,
                )
                with mock.patch(
                    "swarmerz.orchestration.opencode_server._terminate", new=mock.AsyncMock()
                ) as terminate:
                    await server.stop()
                terminate.assert_awaited_once_with(process)

            asyncio.run(exercise())
            result = json.loads(result_path.read_text())
            self.assertFalse(result["expected_stop"])
            self.assertNotIn("stopped_at_epoch", result)

    def test_round_capacity_lock_serializes_two_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events: list[str] = []

            async def first() -> None:
                async with _opencode_round_slot(events.append):
                    events.append("first entered")
                    await asyncio.sleep(0.1)
                    events.append("first leaving")

            async def second() -> None:
                await asyncio.sleep(0.01)
                async with _opencode_round_slot(events.append):
                    events.append("second entered")

            async def exercise() -> None:
                await asyncio.gather(first(), second())

            with mock.patch(
                "swarmerz.orchestration.runner.tempfile.gettempdir", return_value=directory
            ):
                asyncio.run(exercise())
            self.assertLess(events.index("first leaving"), events.index("second entered"))
            self.assertTrue(any("waiting for its capacity slot" in event for event in events))

    def test_shared_service_preserves_separate_credentials(self) -> None:
        base = Path("/tmp/test-swarm")
        def role_environment(role: str) -> dict[str, str]:
            scratch = base / "cache" / "scratch" / "session" / "round" / "roles" / role
            return {
                "SWARMERZ_SCRATCH_DIR": str(scratch),
                "TMPDIR": str(scratch / "tmp"),
                "TMP": str(scratch / "tmp"),
                "TEMP": str(scratch / "tmp"),
                "DOTNET_CLI_HOME": str(scratch / "dotnet-home"),
                "CARGO_TARGET_DIR": str(scratch / "cargo-target"),
            }
        first = CommandSpec(("opencode", "run", "one"), base / "one", {
            "PATH": "/usr/bin", "SWARM_ROLE_ID": "one", "API_KEY_ONE": "secret-one",
            **role_environment("one"),
        }, None)
        second = CommandSpec(("opencode", "run", "two"), base / "two", {
            "PATH": "/usr/bin", "SWARM_ROLE_ID": "two", "API_KEY_ONE": "secret-one",
            **role_environment("two"),
        }, None)
        engines = [mock.Mock(harness="opencode", command=()) for _ in range(2)]
        shared = _shared_opencode_service_spec([
            (engines[0], first, base / "artifacts-one"),
            (engines[1], second, base / "artifacts-two"),
        ])
        self.assertIsNotNone(shared)
        assert shared is not None
        self.assertEqual(shared.env["SWARM_ROLE_ID"], "opencode-service")
        self.assertEqual(shared.env["API_KEY_ONE"], "secret-one")
        self.assertEqual(
            Path(shared.env["SWARMERZ_SCRATCH_DIR"]).name,
            "opencode-service",
        )
        different = CommandSpec(second.argv, second.cwd, {
            **second.env, "API_KEY_TWO": "secret-two",
        }, None)
        with self.assertRaisesRegex(ConfigError, "different server environments"):
            _shared_opencode_service_spec([
                (engines[0], first, base / "artifacts-one"),
                (engines[1], different, base / "artifacts-two"),
            ])

    def test_shared_service_registers_remote_ollama_model(self) -> None:
        base = Path("/tmp/test-swarm")
        env = {"PATH": "/usr/bin", "OPENCODE_CONFIG_CONTENT": '{"permissions":{"read":"allow"}}'}
        deepseek = mock.Mock(harness="opencode", command=(), provider="deepseek")
        ollama = mock.Mock(
            harness="opencode", command=(), provider="ollama",
            effective_model="swarmerz:q38",
            ollama_base_url="http://192.168.8.163:11434/v1",
        )
        first = CommandSpec(("opencode", "run", "one"), base / "deepseek", {
            **env, "SWARM_ROLE_ID": "deepseek",
        }, None)
        second = CommandSpec(("opencode", "run", "two"), base / "ollama", {
            **env, "SWARM_ROLE_ID": "ollama-local",
        }, None)
        shared = _shared_opencode_service_spec([
            (deepseek, first, base / "a"), (ollama, second, base / "b"),
        ])
        assert shared is not None
        config = json.loads(shared.env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permissions"], {"read": "allow"})
        self.assertEqual(
            config["providers"]["ollama"]["settings"]["baseURL"],
            "http://192.168.8.163:11434/v1",
        )
        self.assertTrue(
            config["providers"]["ollama"]["models"]["swarmerz:q38"]
            ["capabilities"]["tools"]
        )

    def test_per_hunter_service_uses_private_home_state_and_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / "scratch"
            spec = CommandSpec(
                ("opencode", "run", "prompt"),
                root / "workspace",
                {
                    "PATH": "/usr/bin",
                    "SWARMERZ_SCRATCH_DIR": str(scratch),
                    "OPENCODE_PASSWORD": "shared-password",
                    "OPENCODE_SERVER_PASSWORD": "shared-server-password",
                },
                None,
            )
            engine = mock.Mock(
                id="local-a",
                harness="opencode",
                command=(),
                provider="ollama",
                pass_env=(),
                env={},
                effective_model="local-model",
                ollama_base_url="http://127.0.0.1:11434/v1",
            )

            private = _per_hunter_opencode_service_spec(engine, spec)

            self.assertEqual(Path(private.env["HOME"]).name, "home")
            self.assertTrue(Path(private.env["HOME"]).is_dir())
            self.assertTrue(Path(private.env["XDG_DATA_HOME"]).is_dir())
            self.assertTrue(Path(private.env["XDG_STATE_HOME"]).is_dir())
            self.assertNotIn("OPENCODE_PASSWORD", private.env)
            self.assertNotIn("OPENCODE_SERVER_PASSWORD", private.env)
            inline = json.loads(private.env["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(
                inline["providers"]["ollama"]["settings"]["baseURL"],
                "http://127.0.0.1:11434/v1",
            )

    def test_per_hunter_hosted_service_requires_explicit_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = CommandSpec(
                ("opencode", "run", "prompt"),
                root,
                {"SWARMERZ_SCRATCH_DIR": str(root / "scratch")},
                None,
            )
            engine = mock.Mock(
                id="hosted-a", harness="opencode", command=(), provider="hosted",
                pass_env=(), env={}, effective_model="model", ollama_base_url=None,
            )
            with self.assertRaisesRegex(ConfigError, "stored OpenCode authentication"):
                _per_hunter_opencode_service_spec(engine, spec)

    def test_collects_only_regular_small_notes_without_sealing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            notes = workspace / ".swarm"
            notes.mkdir(parents=True)
            (notes / "worker-one.md").write_text("Draft finding one", encoding="utf-8")
            (notes / "parent.md").write_text("Parent notes", encoding="utf-8")
            (notes / "workers").mkdir()
            (notes / "workers" / "nested.md").write_text(
                "Nested completed worker report", encoding="utf-8"
            )
            (notes / "upstream" / "dependency").mkdir(parents=True)
            (notes / "upstream" / "dependency" / "README.md").write_text(
                "Copied dependency documentation", encoding="utf-8"
            )
            (notes / "history.json").write_text(json.dumps({"secret": "ignore"}), encoding="utf-8")
            (notes / "external.md").symlink_to(root / "outside.md")
            result = collect_partial_notes(workspace, root / "artifacts")
            self.assertIsNotNone(result)
            assert result is not None
            report = Path(result["path"]).read_text(encoding="utf-8")
            self.assertIn("Draft finding one", report)
            self.assertIn("Parent notes", report)
            self.assertIn("Nested completed worker report", report)
            self.assertIn("workers/nested.md", result["sources"])
            self.assertNotIn("Copied dependency documentation", report)
            self.assertNotIn("external.md", report)
            self.assertNotIn("secret", report)
            self.assertFalse((root / "artifacts" / "seal.json").exists())
