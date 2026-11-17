# Author: Laurent Gaffié, Secorizon.com
# SPDX-License-Identifier: GPL-3.0-only
# This file is part of Swarmerz, a terminal operations deck for independent model swarms.

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import __version__
from .app import SwarmerzApp
from .backend import (
    DemoBackend,
    HistoricalBackend,
    SwarmerzBackend,
    discover_missions,
)
from .config import SwarmerzConfigError, load_hunter_specs
from .doctor import (
    clipboard_startup_warning,
    collect_diagnostics,
    diagnostics_exit_code,
    print_diagnostics,
)
from .launchpad import LaunchpadApp
from .models import HunterSpec, MissionState
from .orchestration.artifacts import read_json
from .orchestration.config import load_config
from .orchestration.runner import AuditRunner
from .orchestration.storage import StorageManager, format_bytes
from .review import chat
from .startup import StartupApp


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarmerz",
        description="Terminal mission control for independent local, hosted, or mixed model swarms.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="launch a new swarm mission in the operations deck")
    _core_options(run, instruction=True)
    run.add_argument("--context", help="local folder to freeze into the session")
    run.add_argument("--session", help="named history session")
    run.add_argument("--round-id", help="explicit round identifier")
    run.add_argument(
        "--headless", action="store_true",
        help="run without the operations deck (requires an instruction)",
    )
    run.add_argument("--json", action="store_true", dest="headless_json")

    analyze = subparsers.add_parser(
        "analyze", help="retry primary adjudication for an existing round in the deck"
    )
    _core_options(analyze, instruction=False)
    analyze.add_argument("--session", required=True)
    analyze.add_argument("--round-id", required=True)
    analyze.add_argument("--adopt-late-answers", action="store_true")
    analyze.add_argument(
        "--headless", action="store_true",
        help="retry adjudication without the operations deck",
    )
    analyze.add_argument("--json", action="store_true", dest="headless_json")

    chat = subparsers.add_parser(
        "chat", help="open a follow-up when the primary analyzer is native Codex"
    )
    _core_options(chat, instruction=False)
    chat.add_argument("--session", required=True)
    chat.add_argument("--round-id", required=True)
    chat.add_argument("--resume", action="store_true")
    chat.add_argument("--print-command", action="store_true")

    cache = subparsers.add_parser(
        "cache", help="inspect or maintain Swarmerz's disposable SSD cache"
    )
    _core_options(cache, instruction=False)
    cache.add_argument("action", choices=("status", "prune", "clear"))
    cache.add_argument("--json", action="store_true", dest="json_output")

    doctor = subparsers.add_parser(
        "doctor", help="check packaging, platform helpers, and an optional deployment profile"
    )
    _core_options(doctor, instruction=False)
    doctor.add_argument("--json", action="store_true", dest="json_output")

    demo = subparsers.add_parser("demo", help="experience the deck without launching models")
    demo.add_argument("--speed", type=float, default=0.22, help="demo pacing multiplier")
    return parser


def _core_options(parser: argparse.ArgumentParser, *, instruction: bool) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help="configuration file (default: ./swarmerz.toml)",
    )
    parser.add_argument(
        "--set", dest="overrides", action="append", default=[], metavar="PATH=VALUE"
    )
    if instruction:
        source = parser.add_mutually_exclusive_group()
        source.add_argument("--instruction")
        source.add_argument("--instruction-file", type=Path)


def _read_instruction(args: argparse.Namespace) -> str | None:
    if args.instruction is not None:
        return str(args.instruction)
    if args.instruction_file is None:
        return None
    try:
        return args.instruction_file.expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SwarmerzConfigError(
            f"cannot read instruction file: {args.instruction_file}"
        ) from exc


def _demo_specs() -> tuple[HunterSpec, ...]:
    return (
        HunterSpec("deepseek", "alibaba-token-plan", "deepseek-v4.1-flash-1017", "max", 4, "opencode"),
        HunterSpec("glm", "zai-coding-plan", "glm-5.3", "max", 4, "opencode"),
        HunterSpec("kimi", "kimi-code-plan-global", "k3.1-pro", "high", 4, "opencode"),
        HunterSpec("mimo", "xiaomi-token-plan-sgp", "mimo-v2.6-pro", "default", 4, "opencode"),
        HunterSpec("codex", "openai", "gpt-daybreak-blue-latest", "xhigh", 4, "codex-exec"),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        args.command not in {"chat", "cache", "doctor"}
        and not getattr(args, "headless", False)
        and (not sys.stdin.isatty() or not sys.stdout.isatty())
    ):
        print("swarmerz requires an interactive terminal", file=sys.stderr)
        return 2
    try:
        if args.command == "doctor":
            if args.overrides and args.config is None:
                raise SwarmerzConfigError("doctor --set requires --config")
            config = None
            if args.config is not None:
                config_path = _resolve_config_path(args.config)
                config = load_config(str(config_path), args.overrides)
            checks = collect_diagnostics(config)
            print_diagnostics(checks, json_output=args.json_output)
            return diagnostics_exit_code(checks)
        if args.command in {"run", "analyze"} and getattr(args, "headless", False):
            return asyncio.run(_run_headless(args))
        if getattr(args, "headless_json", False):
            raise SwarmerzConfigError("--json requires --headless")
        if args.command == "cache":
            config_path = _resolve_config_path(args.config)
            config = load_config(str(config_path), args.overrides)
            storage = StorageManager(config.storage)
            if args.action == "status":
                result = storage.status()
            elif args.action == "prune":
                result = storage.prune()
            else:
                result = storage.clear()
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            elif args.action == "status":
                print(f"Cache root: {result['root']}")
                print(f"Shared cache: {format_bytes(int(result['shared_bytes']))}")
                print(f"Retained scratch: {format_bytes(int(result['scratch_bytes']))}")
                print(f"Total: {format_bytes(int(result['total_bytes']))}")
                print(f"Active runs: {len(result['active_runs'])}")
            else:
                print(
                    f"Swarmerz cache {args.action}: reclaimed "
                    f"{format_bytes(int(result['deleted_bytes']))}"
                )
            return 0
        if args.command == "chat":
            config_path = _resolve_config_path(args.config)
            config = load_config(str(config_path), args.overrides)
            return chat(
                config,
                args.session,
                args.round_id,
                resume=args.resume,
                print_command=args.print_command,
            )
        startup_warning = clipboard_startup_warning()
        if args.command == "demo":
            specs = _demo_specs()
            state = MissionState(
                title="DEMO / FIRST CONTACT",
                instruction="Demonstrate the Swarmerz operations deck.",
                specs=specs,
            )

            def backend_factory(**callbacks: object) -> DemoBackend:
                return DemoBackend(specs, speed=args.speed, **callbacks)
        else:
            config_path = _resolve_config_path(args.config)
            if args.command == "run":
                startup = asyncio.run(StartupApp(
                    discover_missions(config_path, args.overrides),
                    warning=startup_warning,
                ).run())
                if startup is None:
                    print("swarmerz: startup cancelled; no model work started")
                    return 0
                if startup.mode == "load":
                    mission = startup.mission
                    if mission is None:
                        raise ValueError("archive selection did not identify a mission")
                    state = MissionState(
                        title=f"{mission.session_id} / ARCHIVE",
                        instruction=mission.instruction,
                        specs=mission.specs,
                    )
                    state.started_at = mission.started_at
                    state.finished_at = mission.completed_at or mission.started_at
                    state.view = "synthesis"

                    def backend_factory(**callbacks: object) -> HistoricalBackend:
                        return HistoricalBackend(
                            config_path=config_path,
                            overrides=args.overrides,
                            mission=mission,
                            **callbacks,
                        )
                else:
                    specs = load_hunter_specs(config_path, args.overrides)
                    request = asyncio.run(LaunchpadApp(
                        config_path=config_path,
                        specs=specs,
                        context=args.context,
                        session=args.session,
                        instruction=_read_instruction(args),
                    ).run())
                    if request is None:
                        print("swarmerz: launch cancelled; no model work started")
                        return 0
                    instruction = request.instruction
                    session = request.session
                    context = request.context
                    state = MissionState(
                        title=f"{session} / RUN",
                        instruction=instruction,
                        specs=specs,
                    )

                    def backend_factory(**callbacks: object) -> SwarmerzBackend:
                        return SwarmerzBackend(
                            config_path=config_path,
                            instruction=instruction,
                            context=context,
                            session=session,
                            round_id=args.round_id,
                            overrides=args.overrides,
                            action="run",
                            **callbacks,
                        )
            else:
                specs = load_hunter_specs(config_path, args.overrides)
                instruction = ""
                session = args.session
                context = None
                state = MissionState(
                    title=f"{session} / ANALYZE",
                    instruction=instruction,
                    specs=specs,
                )

                def backend_factory(**callbacks: object) -> SwarmerzBackend:
                    return SwarmerzBackend(
                        config_path=config_path,
                        instruction=instruction,
                        context=context,
                        session=session,
                        round_id=args.round_id,
                        overrides=args.overrides,
                        action="analyze",
                        adopt_late_answers=args.adopt_late_answers,
                        **callbacks,
                    )
        if startup_warning:
            state.notice = startup_warning
        asyncio.run(SwarmerzApp(state, backend_factory).run())
        if state.review_command:
            print("Judge follow-up (open in another terminal tab):")
            print(state.review_command)
        return 0 if state.phase.value in {"complete", "degraded"} else 1
    except (SwarmerzConfigError, ValueError, OSError) as exc:
        print(f"swarmerz: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # Prompt Toolkit normally handles keys. This is only for a terminal that
        # forcibly tears down input before the deck can protect an active run.
        print("swarmerz: terminal interrupted", file=sys.stderr)
        return 130


async def _run_headless(args: argparse.Namespace) -> int:
    config_path = _resolve_config_path(args.config)
    config = load_config(str(config_path), args.overrides)
    instruction = _read_instruction(args) if args.command == "run" else ""
    if args.command == "run" and not instruction:
        raise SwarmerzConfigError(
            "headless run requires --instruction or --instruction-file"
        )
    json_output = bool(getattr(args, "headless_json", False))

    def progress(message: str) -> None:
        print(message, file=sys.stderr if json_output else sys.stdout, flush=True)

    runner = AuditRunner(
        config,
        instruction or "",
        context_override=getattr(args, "context", None),
        session_id=args.session,
        round_id=args.round_id,
        progress=progress,
        interactive=False,
    )
    if args.command == "analyze":
        run_dir = await runner.analyze_existing(
            adopt_late_answers=args.adopt_late_answers
        )
    else:
        run_dir = await runner.run()
    manifest = read_json(run_dir / "manifest.json")
    payload = {
        "status": manifest.get("status", "unknown"),
        "degraded": bool(manifest.get("degraded", False)),
        "session_id": manifest.get("session_id", runner.session_id),
        "round_id": manifest.get("round_id", runner.round_id),
        "run_dir": str(run_dir),
        "report": str(run_dir / "final" / "report.md"),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Swarmerz {payload['status']}: {payload['report']}")
    return 0 if payload["status"] == "completed" else 1


def _resolve_config_path(
    explicit: Path | None,
    *,
    cwd: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    working = (cwd or Path.cwd()).resolve()
    if explicit is not None:
        candidate = explicit.expanduser()
        if not candidate.is_absolute():
            candidate = working / candidate
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise SwarmerzConfigError(f"cannot read configuration: {resolved}")
        return resolved

    checkout = (project_root or Path(__file__).resolve().parents[2]).resolve()
    candidates = (
        working / "swarmerz.toml",
        checkout / "swarmerz.toml",
    )
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    searched = ", ".join(str(path) for path in seen)
    raise SwarmerzConfigError(
        f"cannot find default swarmerz.toml; searched: {searched}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
