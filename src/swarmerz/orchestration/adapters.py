# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .models import AnalyzerConfig, AppConfig, CommandSpec, ConfigError, EngineConfig
from .storage import managed_environment


_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "NO_COLOR")


def _npx_command(*arguments: str) -> list[str]:
    executable = shutil.which("npx")
    if not executable:
        raise ConfigError(
            "the configured pinned harness is not installed and npx is unavailable; "
            "install the harness executable, install Node.js/npm (which provides npx), "
            "or select a custom command adapter"
        )
    return [executable, "--yes", *arguments]


def _environment(config: AppConfig, engine: EngineConfig, run_id: str) -> dict[str, str]:
    if config.isolation.inherit_environment:
        env = dict(os.environ)
    else:
        env = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
        for name in engine.pass_env:
            if name in os.environ:
                env[name] = os.environ[name]
    env.update(engine.env)
    env.update(managed_environment(config.storage, run_id, engine.id))
    env["SWARM_RUN_ID"] = run_id
    env["SWARM_ROLE_ID"] = engine.id
    return env


def _executable(engine: EngineConfig) -> list[str]:
    if engine.harness == "cline":
        if engine.harness_version not in {"", "unversioned", "latest"}:
            return _npx_command(f"cline@{engine.harness_version}")
        return ["cline"]
    if engine.harness == "opencode":
        installed = shutil.which("opencode")
        if installed:
            return [installed]
        standard_install = Path.home() / ".opencode" / "bin" / "opencode"
        if standard_install.is_file() and os.access(standard_install, os.X_OK):
            return [str(standard_install)]
        if engine.harness_version not in {"", "unversioned", "latest"}:
            # The package publishes both `opencode` and `opencode2`. Passing the
            # package directly to npx is ambiguous on npm 9, so select the binary.
            return _npx_command(
                "--package",
                f"@opencode/cli@{engine.harness_version}",
                "--",
                "opencode",
            )
        return ["opencode"]
    if engine.harness in {"codex", "codex-exec"}:
        installed = shutil.which("codex")
        if installed:
            return [installed]
        if engine.harness_version not in {"", "unversioned", "latest"}:
            return _npx_command(f"@openai/codex@{engine.harness_version}")
        return ["codex"]
    raise ConfigError(f"no executable mapping for harness {engine.harness}")


def _model_for(engine: EngineConfig, *, include_provider: bool) -> str:
    model = engine.effective_model
    if include_provider and engine.provider == "ollama" and not model.startswith("ollama/"):
        return f"ollama/{model}"
    if include_provider and engine.provider and "/" not in model:
        return f"{engine.provider}/{model}"
    return model


def _opencode_model(engine: EngineConfig) -> str:
    """Render OpenCode 2's provider/model#variant selector."""
    model = _model_for(engine, include_provider=True)
    if engine.reasoning_effort == "default":
        return model
    return f"{model}#{engine.reasoning_effort}"


def _format_custom(tokens: tuple[str, ...], values: dict[str, str]) -> tuple[str, ...]:
    rendered: list[str] = []
    for token in tokens:
        try:
            rendered.append(token.format_map(values))
        except KeyError as exc:
            raise ConfigError(f"unknown command placeholder {exc.args[0]!r}") from exc
    return tuple(rendered)


def build_hunter_command(
    config: AppConfig,
    engine: EngineConfig,
    *,
    run_id: str,
    workspace: Path,
    artifact_dir: Path,
    prompt: str,
    prompt_file: Path,
) -> CommandSpec:
    answer_path = workspace / engine.answer_path
    answer_schema = Path(__file__).resolve().parent / "schemas" / "answer.schema.json"
    if engine.command:
        values = {
            "workspace": str(workspace),
            "artifact_dir": str(artifact_dir),
            "answer": str(answer_path),
            "prompt": prompt,
            "prompt_file": str(prompt_file),
            "model": engine.effective_model,
            "provider": engine.provider,
            "effort": engine.reasoning_effort,
            "timeout_seconds": str(engine.timeout_seconds),
        }
        argv = list(_format_custom(engine.command + engine.extra_args, values))
    elif engine.harness == "cline":
        argv = _executable(engine)
        argv += [
            "--json",
            "--model",
            engine.effective_model,
            "--timeout",
            str(engine.timeout_seconds),
        ]
        argv.extend(engine.extra_args)
        argv.append(prompt)
    elif engine.harness == "opencode":
        argv = _executable(engine)
        argv += ["run", "--model", _opencode_model(engine)]
        argv += ["--format", "json", "--auto"]
        argv.extend(engine.extra_args)
        argv.append(prompt)
    elif engine.harness in {"codex", "codex-exec"}:
        argv = _executable(engine)
        argv += [
            "exec",
            "--disable",
            "apps",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "-m",
            engine.effective_model,
            "-c",
            f'model_reasoning_effort="{engine.reasoning_effort}"',
            "-c",
            'approval_policy="never"',
            "-c",
            f"agents.max_concurrent_threads_per_session={engine.swarm_size}",
            "--output-schema",
            str(answer_schema),
            "--output-last-message",
            str(answer_path),
        ]
        argv.extend(engine.extra_args)
        argv.append(prompt)
    else:
        raise ConfigError(f"unsupported hunter harness: {engine.harness}")

    return CommandSpec(
        argv=tuple(argv),
        cwd=workspace,
        env=_environment(config, engine, run_id),
        artifact_path=answer_path,
    )


def build_probe_command(
    config: AppConfig,
    engine: EngineConfig,
    *,
    run_id: str,
    cwd: Path,
) -> CommandSpec | None:
    if engine.command or engine.harness == "command":
        return None
    return CommandSpec(
        argv=tuple(_executable(engine) + ["--version"]),
        cwd=cwd,
        env=_environment(config, engine, run_id),
        artifact_path=None,
    )


def build_auth_probe_command(
    config: AppConfig,
    engine: EngineConfig,
    *,
    run_id: str,
    cwd: Path,
) -> CommandSpec:
    return CommandSpec(
        # A private probe must not restart or load the shared background service.
        argv=tuple(_executable(engine) + ["auth", "list", "--standalone", "--format", "json"]),
        cwd=cwd,
        env=_environment(config, engine, run_id),
        artifact_path=None,
    )


def build_analyzer_command(
    config: AppConfig,
    engine: AnalyzerConfig,
    *,
    run_id: str,
    workspace: Path,
    artifact_dir: Path,
    answers_path: Path,
    prompt: str,
    schema_path: Path | None = None,
) -> CommandSpec:
    return _build_adjudicator_command(
        config,
        engine,
        run_id=run_id,
        workspace=workspace,
        artifact_dir=artifact_dir,
        inputs={"answers": answers_path},
        prompt=prompt,
        phase="analysis",
        schema_name="analysis.schema.json",
        schema_path=schema_path,
        result_name="analysis.json",
    )


def build_baseline_command(
    config: AppConfig,
    engine: AnalyzerConfig,
    *,
    run_id: str,
    workspace: Path,
    artifact_dir: Path,
    prompt: str,
    schema_path: Path | None = None,
) -> CommandSpec:
    return _build_adjudicator_command(
        config,
        engine,
        run_id=run_id,
        workspace=workspace,
        artifact_dir=artifact_dir,
        inputs={},
        prompt=prompt,
        phase="baseline",
        schema_name="baseline.schema.json",
        schema_path=schema_path,
        result_name="baseline.json",
    )


def build_verifier_command(
    config: AppConfig,
    engine: AnalyzerConfig,
    *,
    run_id: str,
    workspace: Path,
    artifact_dir: Path,
    answers_path: Path,
    primary_analysis_path: Path,
    prompt: str,
    schema_path: Path | None = None,
) -> CommandSpec:
    return _build_adjudicator_command(
        config,
        engine,
        run_id=run_id,
        workspace=workspace,
        artifact_dir=artifact_dir,
        inputs={"answers": answers_path, "primary_analysis": primary_analysis_path},
        prompt=prompt,
        phase="verification",
        schema_name="verification.schema.json",
        schema_path=schema_path,
        result_name="verification.json",
    )


def _build_adjudicator_command(
    config: AppConfig,
    engine: AnalyzerConfig,
    *,
    run_id: str,
    workspace: Path,
    artifact_dir: Path,
    inputs: dict[str, Path],
    prompt: str,
    phase: str,
    schema_name: str,
    schema_path: Path | None,
    result_name: str,
) -> CommandSpec:
    schema = schema_path or Path(__file__).resolve().parent / "schemas" / schema_name
    result = artifact_dir / result_name
    if engine.command:
        values = {
            "workspace": str(workspace),
            "artifact_dir": str(artifact_dir),
            "answers": str(inputs.get("answers", "")),
            "primary_analysis": str(inputs.get("primary_analysis", "")),
            "result": str(result),
            "schema": str(schema),
            "prompt": prompt,
            "phase": phase,
            "model": engine.effective_model,
            "provider": engine.provider,
            "effort": engine.reasoning_effort,
            "timeout_seconds": str(engine.timeout_seconds),
        }
        argv = list(_format_custom(engine.command + engine.extra_args, values))
    else:
        argv = _executable(engine)
        argv += [
            "exec",
            "--disable",
            "apps",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "-m",
            engine.effective_model,
            "-c",
            f'model_reasoning_effort="{engine.reasoning_effort}"',
            "-c",
            'approval_policy="never"',
            "-c",
            'history.persistence="save-all"',
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(result),
        ]
        argv.extend(engine.extra_args)
        argv.append(prompt)

    return CommandSpec(
        argv=tuple(argv),
        cwd=workspace,
        env=_environment(config, engine, run_id),
        artifact_path=result,
    )
