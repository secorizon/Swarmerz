# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from swarmerz.orchestration.config import load_config
from swarmerz.orchestration.local_context import instruction_for_workspace
from swarmerz.orchestration.models import ConfigError
from swarmerz.orchestration.ollama_probe import probe_ollama_model
from swarmerz.orchestration.runner import AuditRunner, _hunter_progress_name, _validate_context_tree


class ConfigTests(unittest.TestCase):
    def test_shipped_examples_pin_requested_harnesses_and_models(self) -> None:
        root = Path(__file__).resolve().parents[2]
        hosted = load_config(root / "swarmerz.toml")
        by_id = {hunter.id: hunter for hunter in hosted.hunters}

        self.assertTrue(all(
            hunter.harness_version == "2.3.0"
            for hunter in hosted.hunters
            if hunter.harness == "opencode"
        ))
        self.assertEqual(by_id["deepseek"].model, "deepseek-v4.1-flash-1017")
        self.assertEqual(by_id["kimi"].model, "k3.1-pro")
        self.assertEqual(by_id["codex"].harness_version, "0.169.0")
        self.assertEqual(by_id["codex"].model, "gpt-daybreak-blue-latest")
        self.assertEqual(hosted.analyzer.harness_version, "0.169.0")
        self.assertEqual(hosted.analyzer.model, "gpt-5.6-sol")
        self.assertFalse(hosted.analyzer.baseline_first)

        local = load_config(root / "examples" / "local-ollama.toml")
        self.assertEqual(len(local.hunters), 2)
        self.assertTrue(all(hunter.provider == "ollama" for hunter in local.hunters))
        self.assertEqual(local.minimum_answers, 2)
        self.assertFalse(local.analyzer.baseline_first)

    def test_example_excludes_agent_metadata_symlinks_without_allowing_source_symlinks(self) -> None:
        config = load_config(Path(__file__).resolve().parents[2] / "swarmerz.toml")
        self.assertFalse(config.context.allow_symlinks)
        self.assertIn(".agents", config.context.exclude)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "main.go").write_text("package main\n", encoding="utf-8")
            skills = source / ".agents" / "skills"
            skills.mkdir(parents=True)
            (skills / "pr-review").symlink_to("../../main.go")
            _validate_context_tree(
                source,
                allow_symlinks=config.context.allow_symlinks,
                exclude=config.context.exclude,
            )

    def test_disabled_context_symlinks_are_skipped_instead_of_rejecting_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
            (source / "license").symlink_to("main.rs")
            (source / "linked-dir").symlink_to(".", target_is_directory=True)
            outside = root / "outside.txt"
            outside.write_text("must not enter snapshot\n", encoding="utf-8")
            (source / "outside-link").symlink_to(outside)
            messages: list[str] = []
            runner = AuditRunner(
                load_config(self._write(root)),
                "Review local files",
                context_override=source,
                session_id="skip-links",
                progress=messages.append,
            )

            runner._open_session()
            try:
                self.assertTrue((runner.context_snapshot / "main.rs").is_file())
                self.assertFalse((runner.context_snapshot / "license").exists())
                self.assertFalse((runner.context_snapshot / "linked-dir").exists())
                self.assertFalse((runner.context_snapshot / "outside-link").exists())
                self.assertEqual(runner.session["context_skipped_symlink_count"], 3)
                self.assertTrue(any("skipping 3 context symlink(s)" in line for line in messages))
            finally:
                runner._release_lock()

    def _ollama_config(self, root: Path, *, base_url: str = "http://192.168.1.50:11434/v1") -> Path:
        path = self._write(root, hunter_count=2)
        content = path.read_text(encoding="utf-8")
        content = content.replace('harness = "command"', 'harness = "opencode"', 1)
        content = content.replace('provider = "test"', 'provider = "ollama"', 1)
        content = content.replace('model = "model-0"', 'model = "custom/team-coder:latest"', 1)
        content = content.replace('reasoning_effort = "low"', 'reasoning_effort = "default"', 1)
        content = content.replace(
            'supported_efforts = ["low", "medium"]',
            'supported_efforts = ["default"]',
            1,
        )
        content = content.replace('command = ["true"]', f'ollama_base_url = "{base_url}"', 1)
        path.write_text(content, encoding="utf-8")
        return path

    def test_ollama_hunter_uses_workspace_config_and_exact_model_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(self._ollama_config(Path(directory)))
            runner = AuditRunner(config, "Explain how to unclog a toilet", session_id="local-model")
            runner._open_session()
            try:
                engine, spec, _ = runner._prepare_hunter(config.hunters[0])
                self.assertEqual(engine.provider, "ollama")
                self.assertIn("ollama/custom/team-coder:latest", spec.argv)
                workspace_config = spec.cwd / "opencode.json"
                self.assertEqual(
                    json.loads(workspace_config.read_text(encoding="utf-8")),
                    {"providers": {"ollama": {
                        "settings": {"baseURL": "http://192.168.1.50:11434/v1"},
                        "models": {"custom/team-coder:latest": {"capabilities": {
                            "tools": True, "input": ["text"], "output": ["text"],
                        }}},
                    }}},
                )
                _, other_spec, _ = runner._prepare_hunter(config.hunters[1])
                self.assertFalse((other_spec.cwd / "opencode.json").exists())
                self.assertEqual(_hunter_progress_name(engine), "custom/team-coder:latest")
                self.assertEqual(_hunter_progress_name(config.hunters[1]), config.hunters[1].id)
            finally:
                runner._release_lock()

    def test_ollama_needs_no_stored_opencode_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(self._ollama_config(Path(directory)))
            runner = AuditRunner(config, "Explain how to unclog a toilet", session_id="local-model")
            runner._open_session()
            try:
                with mock.patch(
                    "swarmerz.orchestration.runner.probe_ollama_model",
                    return_value="http://192.168.1.50:11434/api/tags",
                ), mock.patch("swarmerz.orchestration.runner.build_probe_command", return_value=None), mock.patch(
                    "swarmerz.orchestration.runner.check_chatgpt_codex_models", return_value=[]
                ), mock.patch("swarmerz.orchestration.runner.build_auth_probe_command") as auth_probe:
                    asyncio.run(runner._prepare_harnesses())
                    auth_probe.assert_not_called()
            finally:
                runner._release_lock()

    def test_ollama_endpoint_rejects_credentials_and_wrong_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ConfigError, "without credentials"):
                load_config(self._ollama_config(root, base_url="http://user:secret@host:11434/v1"))
            local_config = load_config(self._ollama_config(root, base_url="http://x99.local:11434/v1"))
            self.assertEqual(local_config.hunters[0].ollama_base_url, "http://x99.local:11434/v1")
            path = self._ollama_config(root)
            path.write_text(
                path.read_text(encoding="utf-8").replace('provider = "ollama"', 'provider = "test"', 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "requires the OpenCode ollama provider"):
                load_config(path)

    def test_ollama_preflight_checks_inventory_without_inference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = load_config(self._ollama_config(Path(directory))).hunters[0]
            inventory = json.dumps({"models": [{"name": "custom/team-coder:latest"}]}).encode()
            detail = json.dumps({"capabilities": ["completion", "tools"]}).encode()
            opener = mock.Mock()
            opener.open.side_effect = [io.BytesIO(inventory), io.BytesIO(detail)]
            with mock.patch("swarmerz.orchestration.ollama_probe.build_opener", return_value=opener):
                self.assertEqual(
                    probe_ollama_model(engine), "http://192.168.1.50:11434/api/tags"
                )
                self.assertEqual(opener.open.call_args_list[0].args[0].full_url, "http://192.168.1.50:11434/api/tags")
                self.assertEqual(opener.open.call_args_list[1].args[0].full_url, "http://192.168.1.50:11434/api/show")
                opener.open.side_effect = [io.BytesIO(b'{"models": []}')]
                with self.assertRaisesRegex(ConfigError, "absent from"):
                    probe_ollama_model(engine)
                opener.open.side_effect = [io.BytesIO(inventory), io.BytesIO(b'{"capabilities":["completion"]}')]
                with self.assertRaisesRegex(ConfigError, "completion and tools"):
                    probe_ollama_model(engine)

    def _write(self, root: Path, effort: str = "low", hunter_count: int = 4) -> Path:
        hunters = []
        for index in range(hunter_count):
            hunters.append(
                textwrap.dedent(
                    f"""
                    [[hunters]]
                    id = "hunter-{index}"
                    harness = "command"
                    harness_version = "1"
                    provider = "test"
                    model = "model-{index}"
                    reasoning_effort = "{effort}"
                    supported_efforts = ["low", "medium"]
                    command = ["true"]
                    workers = 2
                    """
                )
            )
        config = root / "swarm.toml"
        config.write_text(
            textwrap.dedent(
                f"""
                schema_version = 1
                output_root = "./runs"
                max_parallel_hunters = {hunter_count}

                {''.join(hunters)}

                [analyzer]
                id = "codex-analyzer"
                harness = "codex-exec"
                harness_version = "1"
                provider = "openai"
                model = "codex-test"
                reasoning_effort = "medium"
                supported_efforts = ["medium"]
                command = ["true"]
                """
            ),
            encoding="utf-8",
        )
        return config

    def test_loads_configurable_hunter_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(self._write(Path(directory), hunter_count=5))
            self.assertEqual(len(config.hunters), 5)
            self.assertEqual(config.minimum_answers, 5)
            self.assertEqual(config.opencode_artifact_grace_seconds, 3600)
            self.assertEqual(config.opencode_worker_timeout_seconds, 3600)
            self.assertEqual(config.opencode_worker_poll_seconds, 30)
            self.assertEqual(config.opencode_answer_quiet_seconds, 180)
            self.assertEqual(config.opencode_orphan_quiet_seconds, 600)
            self.assertEqual(config.opencode_finalize_answer_quiet_seconds, 60)
            self.assertIsNone(config.hunters[0].worker_timeout_seconds)
            self.assertIsNone(config.context.path)
            self.assertEqual(config.analyzer.effective_model, "codex-test")
            self.assertFalse(config.analyzer.baseline_first)
            self.assertEqual(config.storage.max_cache_bytes, 40 * 1024**3)
            self.assertEqual(config.storage.max_cache_age_days, 30)
            self.assertEqual(config.storage.failed_scratch_retention_days, 7)
            self.assertTrue(config.storage.cleanup_successful_workspaces)

    def test_opencode_server_mode_defaults_shared_and_validates_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write(root)
            self.assertEqual(load_config(path).isolation.opencode_server_mode, "shared")

            text = path.read_text(encoding="utf-8").replace(
                "max_parallel_hunters = 4",
                'max_parallel_hunters = 4\n\n[isolation]\n'
                'opencode_server_mode = "per_hunter"',
            )
            path.write_text(text, encoding="utf-8")
            self.assertEqual(
                load_config(path).isolation.opencode_server_mode, "per_hunter"
            )

            path.write_text(
                text.replace('"per_hunter"', '"process_magic"'), encoding="utf-8"
            )
            with self.assertRaisesRegex(ConfigError, "opencode_server_mode"):
                load_config(path)

    def test_storage_root_is_dedicated_and_limits_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self._write(root)
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'output_root = "./runs"',
                    'output_root = "./runs"\n\n[storage]\n'
                    'cache_root = "./cache"\nmax_cache_gib = 2\n'
                    'max_cache_age_days = 10\nfailed_scratch_retention_days = 3\n'
                    'maintenance_interval_hours = 6\ncleanup_successful_workspaces = false',
                ),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.storage.cache_root, (root / "cache").resolve())
            self.assertEqual(config.storage.max_cache_bytes, 2 * 1024**3)
            self.assertFalse(config.storage.cleanup_successful_workspaces)

            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    'cache_root = "./cache"', 'cache_root = "./runs/cache"'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "must not contain"):
                load_config(path)

    def test_loads_and_validates_answer_quorum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory), hunter_count=5)
            text = path.read_text(encoding="utf-8").replace(
                "max_parallel_hunters = 5",
                "max_parallel_hunters = 5\nminimum_answers = 4",
            )
            path.write_text(text, encoding="utf-8")
            self.assertEqual(load_config(path).minimum_answers, 4)

            path.write_text(text.replace("minimum_answers = 4", "minimum_answers = 6"), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "minimum_answers"):
                load_config(path)

    def test_answer_quiet_period_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory))
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "max_parallel_hunters = 4",
                    "max_parallel_hunters = 4\nopencode_answer_quiet_seconds = 0",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "opencode_answer_quiet_seconds"):
                load_config(path)

            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "opencode_answer_quiet_seconds = 0",
                    "opencode_finalize_answer_quiet_seconds = 0",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "opencode_finalize_answer_quiet_seconds"):
                load_config(path)

    def test_hunter_worker_timeout_override_is_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory))
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "workers = 2", "workers = 2\nworker_timeout_seconds = 7200", 1
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_config(path).hunters[0].worker_timeout_seconds, 7200)
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "worker_timeout_seconds = 7200", "worker_timeout_seconds = 0"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "worker_timeout_seconds"):
                load_config(path)

    def test_rejects_unsupported_effort_without_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "requests effort 'max'"):
                load_config(self._write(Path(directory), effort="max"))

    def test_existing_local_folder_requires_context_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            config = load_config(self._write(root))
            with self.assertRaisesRegex(ConfigError, "--context"):
                AuditRunner(config, f"Audit {source}", session_id="local-audit")
            self.assertFalse((root / "runs").exists())
            self.assertEqual(
                instruction_for_workspace("Review https://github.com/org/repo", None),
                "Review https://github.com/org/repo",
            )

    def test_context_paths_are_remapped_and_generated_files_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "src").mkdir(parents=True)
            (source / "src" / "main.rs").write_text("fn main() {}", encoding="utf-8")
            (source / "target").mkdir()
            (source / "target" / "large.bin").write_bytes(b"generated")
            config_path = self._write(root)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    'output_root = "./runs"',
                    'output_root = "./runs"\n\n[context]\nexclude = ["target"]',
                ),
                encoding="utf-8",
            )
            runner = AuditRunner(
                load_config(config_path), f"Audit {source}/src/main.rs",
                context_override=source, session_id="local-audit",
            )
            self.assertEqual(runner.agent_instruction, "Audit context/src/main.rs")
            runner._open_session()
            try:
                self.assertTrue((runner.context_snapshot / "src" / "main.rs").is_file())
                self.assertFalse((runner.context_snapshot / "target").exists())
            finally:
                runner._release_lock()

    def test_interrupted_app_owned_context_copy_is_restarted_with_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "current.txt").write_text("current", encoding="utf-8")
            messages: list[str] = []
            runner = AuditRunner(
                load_config(self._write(root)), "Review local files",
                context_override=source, session_id="interrupted", progress=messages.append,
            )
            runner.context_snapshot.mkdir(parents=True)
            (runner.context_snapshot / "stale.txt").write_text("stale", encoding="utf-8")
            marker = runner.context_snapshot.parent / ".context-copying.json"
            marker.write_text(json.dumps({"source": str(source.resolve())}), encoding="utf-8")
            runner._open_session()
            try:
                self.assertFalse((runner.context_snapshot / "stale.txt").exists())
                self.assertEqual((runner.context_snapshot / "current.txt").read_text(), "current")
                self.assertFalse(marker.exists())
                self.assertTrue(any("freezing session context" in line for line in messages))
                self.assertTrue(any("hash complete" in line for line in messages))
            finally:
                runner._release_lock()

    def test_unmarked_context_copy_is_not_deleted_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            runner = AuditRunner(
                load_config(self._write(root)), "Review local files",
                context_override=source, session_id="unmarked",
            )
            runner.context_snapshot.mkdir(parents=True)
            (runner.context_snapshot / "keep.txt").write_text("keep", encoding="utf-8")
            try:
                with self.assertRaisesRegex(ConfigError, "without a copy marker"):
                    runner._open_session()
                self.assertTrue((runner.context_snapshot / "keep.txt").exists())
            finally:
                runner._release_lock()

    def test_requires_at_least_two_hunters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "between 2 and 26"):
                load_config(self._write(Path(directory), hunter_count=1))

    def test_rejects_answer_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory))
            text = path.read_text(encoding="utf-8").replace(
                "workers = 2",
                'answer_path = "../../shared.json"\nworkers = 2',
                1,
            )
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "stay inside"):
                load_config(path)

    def test_rejects_ephemeral_codex_hunter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory))
            text = path.read_text(encoding="utf-8").replace(
                'harness = "command"',
                'harness = "codex-exec"',
                1,
            ).replace(
                'command = ["true"]',
                'extra_args = ["--ephemeral"]',
                1,
            )
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "persisted parent thread"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
