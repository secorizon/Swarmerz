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
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest import mock

from swarmerz.orchestration.codex_appserver import CodexAppServer
from swarmerz.orchestration.live_control import LiveControl
from swarmerz.orchestration.models import CommandSpec, ConfigError
from swarmerz.orchestration.opencode_permissions import OpenCodeEndpoint, steer_session
from swarmerz.orchestration.runner import AuditRunner


class LiveControlTests(unittest.TestCase):
    def test_status_names_queued_ollama_model(self) -> None:
        control = LiveControl({"ollama-local": "swarmerz:q38"})
        control.active.add("ollama-local")
        self.assertEqual(
            control.status(), "swarmerz:q38 (ollama-local): queued to start"
        )
        control.mark_started("ollama-local")
        self.assertEqual(
            control.status(),
            "swarmerz:q38 (ollama-local): started; steering transport not ready",
        )
        control.register_opencode(
            "ollama-local",
            OpenCodeEndpoint("http://127.0.0.1:1234", "private", Path("/tmp/ollama")),
        )
        self.assertEqual(
            control.status(), "swarmerz:q38 (ollama-local): OpenCode starting"
        )

    def test_opencode_broadcast_targets_only_unfinished_children(self) -> None:
        async def scenario() -> list[str]:
            control = LiveControl()
            control.register_opencode(
                "glm", OpenCodeEndpoint("http://127.0.0.1:1234", "private", Path("/tmp/glm"))
            )
            control.set_opencode_session("glm", "ses_parent")
            control.parent_returned("glm")
            with mock.patch("swarmerz.orchestration.live_control.list_worker_sessions", return_value={
                "data": [
                    {"id": "ses_live", "parentID": "ses_parent"},
                    {"id": "ses_done", "parentID": "ses_parent", "outcome": "succeeded"},
                    {"id": "ses_other", "parentID": "different"},
                ]
            }), mock.patch("swarmerz.orchestration.live_control.steer_session") as steer:
                outcome = await control.send("Please finish")
                self.assertEqual(steer.call_count, 1)
                self.assertEqual(steer.call_args.args[:2], ("ses_live", "Please finish"))
                return outcome
        self.assertEqual(asyncio.run(scenario()), ["glm: delivered to 1/1 active session(s)"])

    def test_codex_steering_uses_active_turn_id(self) -> None:
        async def scenario() -> None:
            client = CodexAppServer()
            client.thread_id = "thread-a"
            client.turn_id = "turn-b"
            with mock.patch.object(client, "_request", new_callable=mock.AsyncMock) as request:
                await client.steer("Wrap up")
                request.assert_awaited_once_with("turn/steer", {
                    "threadId": "thread-a",
                    "input": [{"type": "text", "text": "Wrap up"}],
                    "expectedTurnId": "turn-b",
                })
        asyncio.run(scenario())

    def test_recoverable_codex_requires_targeted_followup(self) -> None:
        async def scenario() -> None:
            control = LiveControl()
            client = CodexAppServer()
            client.thread_id = "thread-a"
            control.register_codex("codex", client)
            control.mark_recoverable("codex")

            self.assertEqual(await control.send("continue"), [])
            self.assertIn("Codex RECOVERABLE", control.status())
            self.assertEqual(
                await control.send("continue", "codex"),
                ["codex: recovery instruction queued for the same Codex thread"],
            )
            self.assertEqual(
                await control.send("another instruction", "codex"),
                [
                    "codex: recovery not queued "
                    "(a recovery instruction is already queued)"
                ],
            )
            self.assertEqual(
                await control.next_recovery("codex", timeout_seconds=0.01), "continue"
            )

        asyncio.run(scenario())

    def test_failed_codex_turn_can_resume_while_peer_is_open(self) -> None:
        async def scenario(root: Path) -> None:
            engine = SimpleNamespace(
                id="codex", harness="codex-exec", command=(), timeout_seconds=10,
                provider="openai", effective_model="gpt-daybreak-blue-latest",
            )
            runner = object.__new__(AuditRunner)
            runner.config = SimpleNamespace(hunters=(engine,))
            runner.live_control = LiveControl()
            runner.live_control.active.update({"codex", "peer"})
            runner._resource_limiter = None
            messages: list[str] = []
            runner.progress = messages.append
            client = CodexAppServer()
            turns: list[str | None] = []

            async def start(*_args: object, **_kwargs: object) -> None:
                client.thread_id = "thread-a"

            async def run(
                _spec: CommandSpec, _engine: object, *, prompt: str | None = None,
            ) -> dict[str, object]:
                turns.append(prompt)
                if len(turns) == 1:
                    return {
                        "status": "failed", "exit_code": 1, "timed_out": False,
                        "duration_seconds": 0, "error": "request denied",
                    }
                return {
                    "status": "completed", "exit_code": 0, "timed_out": False,
                    "duration_seconds": 0,
                }

            spec = CommandSpec(
                argv=("codex", "exec", "initial prompt"), cwd=root, env={},
                artifact_path=root / "answer.json",
            )
            with mock.patch(
                "swarmerz.orchestration.runner.CodexAppServer", return_value=client
            ), mock.patch.object(client, "start", side_effect=start), mock.patch.object(
                client, "run", side_effect=run
            ):
                task = asyncio.create_task(runner._execute_with_transport_retries(
                    engine, spec, role="codex", stdout_path=root / "stdout.jsonl",
                    stderr_path=root / "stderr.log",
                ))
                for _ in range(100):
                    if "codex" in runner.live_control.recoverable:
                        break
                    await asyncio.sleep(0)
                self.assertIn("codex", runner.live_control.recoverable)
                self.assertEqual(
                    await runner.live_control.send("continue", "codex"),
                    ["codex: recovery instruction queued for the same Codex thread"],
                )
                result = await asyncio.wait_for(task, timeout=2)

            self.assertEqual(turns, [None, "continue"])
            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["resumed_after_failure"])
            self.assertEqual(result["recovery_count"], 1)
            self.assertEqual(
                result["recovery_attempts"][0]["instruction_sha256"],
                "e256ee8e7aff6957a781d8328f0f68e26996564c81fa458da59fbca2305138ad",
            )
            self.assertTrue(any("recovery available" in item for item in messages))
            self.assertTrue(any("resuming recoverable" in item for item in messages))
            self.assertTrue(all("[codex]" not in item for item in messages))
            self.assertTrue(any("[daybreak]" in item for item in messages))

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_failed_opencode_session_can_resume_while_peer_is_open(self) -> None:
        async def scenario(root: Path) -> None:
            endpoint = OpenCodeEndpoint(
                "http://127.0.0.1:1234", "private", root / "workspace"
            )
            engine = SimpleNamespace(
                id="deepseek", harness="opencode", command=(), provider="deepseek",
                effective_model="deepseek-v4.1-flash-1017", swarm_size=2,
            )
            runner = object.__new__(AuditRunner)
            runner.config = SimpleNamespace(opencode_worker_poll_seconds=0)
            runner.live_control = LiveControl()
            runner.live_control.register_opencode("deepseek", endpoint)
            runner.live_control.active.add("peer")
            messages: list[str] = []
            runner.progress = messages.append
            result: dict[str, object] = {
                "status": "failed", "exit_code": 1, "timed_out": False,
                "duration_seconds": 1, "answer_sealed": False,
                "worker_contract_verified": True, "artifact_error": "missing answer",
            }
            spec = CommandSpec(
                argv=("opencode", "run"), cwd=root, env={},
                artifact_path=root / ".swarm" / "answer.json",
            )
            ready_to_seal = False

            def try_seal() -> bool:
                result["answer_sealed"] = ready_to_seal
                return ready_to_seal

            async def finalize(*_args: object, **kwargs: object) -> dict[str, object]:
                nonlocal ready_to_seal
                self.assertEqual(kwargs["log_stem"], "operator-recovery-01")
                ready_to_seal = True
                return {
                    "status": "completed", "exit_code": 0, "timed_out": False,
                    "duration_seconds": 0,
                }

            async def wait_for_artifact(*_args: object, **_kwargs: object) -> bool:
                return try_seal()

            with mock.patch.object(
                runner, "_opencode_resume_spec", return_value=spec
            ), mock.patch.object(
                runner, "_execute_opencode_finalize", side_effect=finalize
            ):
                task = asyncio.create_task(runner._recover_opencode_from_operator(
                    engine,
                    spec,
                    artifact_dir=root,
                    session_id="ses-parent",
                    endpoint=endpoint,
                    result=result,
                    try_seal=try_seal,
                    wait_for_late_artifact=wait_for_artifact,
                ))
                for _ in range(100):
                    if "deepseek" in runner.live_control.recoverable:
                        break
                    await asyncio.sleep(0)
                self.assertIn("deepseek", runner.live_control.recoverable)
                self.assertIn("OpenCode RECOVERABLE", runner.live_control.status())
                self.assertEqual(
                    await runner.live_control.send("continue", "deepseek"),
                    [
                        "deepseek: recovery instruction queued for the same "
                        "OpenCode session"
                    ],
                )
                await asyncio.wait_for(task, timeout=2)

            self.assertTrue(result["answer_sealed"])
            self.assertTrue(result["resumed_after_failure"])
            self.assertEqual(result["operator_recovery_count"], 1)
            self.assertEqual(
                result["operator_recovery_attempts"][0]["instruction_sha256"],
                "e256ee8e7aff6957a781d8328f0f68e26996564c81fa458da59fbca2305138ad",
            )
            self.assertTrue(any("OpenCode session failed" in item for item in messages))
            self.assertTrue(any("resuming recoverable OpenCode" in item for item in messages))

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_opencode_request_uses_steer_not_a_new_run(self) -> None:
        endpoint = OpenCodeEndpoint("http://127.0.0.1:1234", "private", Path("/tmp/glm"))
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data":{"sessionID":"ses_live"}}'
        with mock.patch("swarmerz.orchestration.opencode_permissions.urlopen", return_value=response) as send:
            steer_session("ses_live", "Wrap up", endpoint=endpoint)
        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "text": "Wrap up", "delivery": "steer", "resume": True,
        })
        self.assertEqual(request.get_header("X-opencode-directory"), "/tmp/glm")

    def test_opencode_steering_reports_http_error_body(self) -> None:
        endpoint = OpenCodeEndpoint("http://127.0.0.1:1234", "private", Path("/tmp/glm"))
        invalid = HTTPError(
            "http://127.0.0.1:1234", 400, "Bad Request", {},
            io.BytesIO(b'{"message":"Rejected steering input"}'),
        )
        with mock.patch(
            "swarmerz.orchestration.opencode_permissions.urlopen", side_effect=invalid
        ), self.assertRaisesRegex(ConfigError, r'HTTP 400: .*Rejected steering input'):
            steer_session("ses_live", "Wrap up", endpoint=endpoint)

    def test_opencode_steering_falls_back_to_nested_v2_schema(self) -> None:
        endpoint = OpenCodeEndpoint("http://127.0.0.1:1234", "private", Path("/tmp/glm"))
        missing_prompt = HTTPError(
            "http://127.0.0.1:1234", 400, "Bad Request", {},
            io.BytesIO(b'{"message":"Missing key at [\\"prompt\\"]"}'),
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data":{"sessionID":"ses_live"}}'
        with mock.patch(
            "swarmerz.orchestration.opencode_permissions.urlopen",
            side_effect=[missing_prompt, response],
        ) as send:
            steer_session("ses_live", "Wrap up", endpoint=endpoint)
        self.assertEqual(json.loads(send.call_args_list[0].args[0].data), {
            "text": "Wrap up", "delivery": "steer", "resume": True,
        })
        self.assertEqual(json.loads(send.call_args_list[1].args[0].data), {
            "prompt": {"text": "Wrap up"}, "delivery": "steer", "resume": True,
        })

    def test_codex_model_refresh_timeout_is_deduplicated_but_preserved(self) -> None:
        async def scenario(root: Path) -> None:
            client = CodexAppServer()
            stderr = asyncio.StreamReader()
            refresh = (
                "ERROR codex_models_manager::manager: failed to refresh available models: "
                "request timed out\n"
            )
            ttl = (
                "\033[2m2026-09-30T23:52:40Z\033[0m \033[31mERROR\033[0m "
                "\033[2mcodex_models_manager::manager\033[0m\033[2m:\033[0m "
                "failed to renew cache TTL: EOF while parsing a value at line 1 column 0\n"
            )
            stale_poll = (
                "\033[31mERROR\033[0m \033[2mcodex_core::tools::router\033[0m: "
                "error=write_stdin failed: Unknown process id 18396\n"
            )
            stderr.feed_data(
                (refresh + refresh + ttl + ttl + stale_poll + stale_poll
                 + "WARN another warning\n").encode()
            )
            stderr.feed_eof()
            client.process = SimpleNamespace(stderr=stderr)
            messages: list[str] = []
            log = root / "stderr.log"
            await client._read_stderr(log, messages.append)
            self.assertEqual(messages, [
                "WARNING: Codex model catalog cache refresh failed; continuing with the configured model",
                "WARNING: Codex command polling raced with process exit; the worker can continue",
                "WARN another warning",
            ])
            self.assertEqual(
                log.read_text(),
                refresh + refresh + ttl + ttl + stale_poll + stale_poll
                + "WARN another warning\n",
            )

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_codex_appserver_turn_produces_answer_without_model_call(self) -> None:
        class FakeWriter:
            def __init__(self, reader: asyncio.StreamReader) -> None:
                self.reader = reader
                self.messages: list[dict[str, object]] = []

            def write(self, payload: bytes) -> None:
                request = json.loads(payload)
                self.messages.append(request)
                request_id = request.get("id")
                if request_id is None:
                    return
                method = request["method"]
                if method == "initialize":
                    response = {"id": request_id, "result": {"userAgent": "fake"}}
                elif method == "thread/start":
                    response = {"id": request_id, "result": {"thread": {"id": "thread-a"}}}
                else:
                    response = {"id": request_id, "result": {"turn": {
                        "id": "turn-b", "status": "inProgress",
                    }}}
                self.reader.feed_data((json.dumps(response) + "\n").encode())
                if method == "turn/start":
                    answer = {"answer": "Test response", "key_points": [],
                              "assumptions": [], "uncertainties": [], "sources": [],
                              "workstreams": [
                                  {"name": f"worker-{index}", "scope": "source",
                                   "conclusion": "done"}
                                  for index in range(4)
                              ], "confidence": 0.5}
                    for event in (
                        {"method": "turn/started", "params": {
                            "threadId": "thread-a", "turn": {"id": "turn-b"}}},
                        {"method": "item/completed", "params": {
                            "threadId": "thread-a", "item": {
                                "type": "agentMessage", "text": json.dumps(answer),
                            }}},
                        {"method": "turn/completed", "params": {
                            "turn": {"id": "turn-b", "status": "completed"}}},
                    ):
                        self.reader.feed_data((json.dumps(event) + "\n").encode())

            async def drain(self) -> None:
                pass

        async def scenario(root: Path) -> None:
            stdout = asyncio.StreamReader()
            stderr = asyncio.StreamReader()
            stderr.feed_eof()
            writer = FakeWriter(stdout)
            fake_process = SimpleNamespace(
                stdin=writer, stdout=stdout, stderr=stderr, returncode=0, pid=123,
            )
            scratch = (
                root / "cache" / "scratch" / "session" / "round" / "roles" / "codex"
            )
            spec = CommandSpec(
                argv=("codex", "exec", "--output-last-message", "answer.json", "prompt"),
                cwd=root,
                env={
                    "SWARMERZ_CACHE_ROOT": str(root / "cache"),
                    "SWARMERZ_SCRATCH_DIR": str(scratch),
                    "TMPDIR": str(scratch / "tmp"),
                    "TMP": str(scratch / "tmp"),
                    "TEMP": str(scratch / "tmp"),
                },
                artifact_path=root / "answer.json",
            )
            engine = SimpleNamespace(effective_model="gpt-test", reasoning_effort="high",
                                     timeout_seconds=10, swarm_size=4)
            client = CodexAppServer()

            async def fake_launch(*_args: object, **_kwargs: object) -> object:
                self.assertTrue(Path(spec.env["SWARMERZ_SCRATCH_DIR"]).is_dir())
                self.assertTrue(Path(spec.env["TMPDIR"]).is_dir())
                return fake_process

            launch = mock.AsyncMock(side_effect=fake_launch)
            with mock.patch(
                "swarmerz.orchestration.codex_appserver.asyncio.create_subprocess_exec",
                launch,
            ):
                await client.start(spec, engine, stdout_path=root / "stdout.jsonl",
                                   stderr_path=root / "stderr.log", emit=lambda _: None)
                result = await client.run(spec, engine)
                resumed = await client.run(spec, engine, prompt="continue")
            self.assertIn("agents.max_concurrent_threads_per_session=4", launch.call_args.args)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(json.loads(spec.artifact_path.read_text())["answer"], "Test response")
            turn_requests = [
                item for item in writer.messages if item.get("method") == "turn/start"
            ]
            self.assertEqual(turn_requests[0]["params"]["effort"], "high")
            self.assertEqual(
                turn_requests[1]["params"]["input"],
                [{"type": "text", "text": "continue"}],
            )
            stdout.feed_eof()
            await client.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_completed_codex_turn_without_answer_is_a_recoverable_failure(self) -> None:
        async def scenario(root: Path) -> None:
            client = CodexAppServer()
            client.thread_id = "thread-a"
            spec = CommandSpec(
                argv=("codex", "exec", "prompt"), cwd=root, env={},
                artifact_path=root / "missing-answer.json",
            )
            engine = SimpleNamespace(
                effective_model="gpt-test", reasoning_effort="high",
                timeout_seconds=10, swarm_size=1,
            )

            async def request(method: str, _params: object) -> dict[str, object]:
                self.assertEqual(method, "turn/start")
                asyncio.get_running_loop().call_soon(
                    client._completed.set_result,
                    {"turn": {"id": "turn-b", "status": "completed"}},
                )
                return {"turn": {"id": "turn-b", "status": "inProgress"}}

            with mock.patch.object(client, "_request", side_effect=request):
                result = await client.run(spec, engine)

            self.assertEqual(result["status"], "failed")
            self.assertIn("did not produce an answer", result["error"])

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

if __name__ == "__main__":
    unittest.main()
