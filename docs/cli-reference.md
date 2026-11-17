<!--
Author: Laurent Gaffié, Secorizon.com
SPDX-License-Identifier: GPL-3.0-only
This file is part of Swarmerz, a terminal operations deck for independent model swarms.
-->

# CLI reference

The installed entry point is `swarmerz`. From a source checkout, `./bin/swarmerz` invokes the same
Python CLI.

```text
swarmerz [--version] {run,analyze,chat,cache,doctor,demo} ...
```

Run `swarmerz COMMAND --help` for the parser's exact current options.

## Configuration options

The operational commands accept:

- `--config PATH`: select a deployment profile.
- `--set PATH=VALUE`: override one existing profile value; repeat as needed.

For installed deployments, prefer an explicit absolute `--config` path. No operational profile is
included in the release artifact.

## `run`

Start a new round or enter the stored-mission picker.

```text
swarmerz run [--config PATH] [--set PATH=VALUE]
             [--instruction TEXT | --instruction-file PATH]
             [--context DIRECTORY] [--session NAME] [--round-id ID]
             [--headless] [--json]
```

Without `--headless`, supplied values prefill the operations deck. The operator still crosses the
final launch boundary before model work begins. `--headless` requires an instruction and preserves
the same freeze, fan-out, sealing, quorum, and adjudication protocol. With `--json`, progress is sent
to standard error and one completion object is written to standard output.

## `analyze`

Retry primary adjudication for an existing round without rerunning hunters.

```text
swarmerz analyze [--config PATH] [--set PATH=VALUE]
                 --session NAME --round-id ID
                 [--adopt-late-answers] [--headless] [--json]
```

Existing seals are verified before use. `--adopt-late-answers` is deliberately opt-in and only
admits contract-valid answers that arrived after the original run recorded failure.

## `chat`

Open or inspect a follow-up when the primary analyzer is native Codex.

```text
swarmerz chat [--config PATH] [--set PATH=VALUE]
              --session NAME --round-id ID [--resume] [--print-command]
```

The first call creates a readable Codex conversation grounded in the completed report. Use
`--resume` for a later continuation. `--print-command` displays the launch without starting it.

## `cache`

Inspect or maintain disposable SSD cache data.

```text
swarmerz cache [--config PATH] [--set PATH=VALUE] [--json]
               {status,prune,clear}
```

- `status` reports shared cache, retained scratch, total size, and active runs.
- `prune` applies age and size policy.
- `clear` removes managed cache content only when no mission is active.

All deletion operations require Swarmerz's cache marker and refuse paths outside the configured
cache root. Durable session artifacts are outside this mechanism.

## `doctor`

Check the installed package and optional platform helpers. Supply a profile to also check its
required harnesses.

```text
swarmerz doctor [--config PATH] [--set PATH=VALUE] [--json]
```

`doctor --set` requires `--config`. Clipboard warnings and unavailable automatic resource
containment do not produce a failure exit status. A profile using `resources.mode = "required"`
does fail when the required platform helpers are absent; mission startup performs the definitive
user-systemd capability probe.

## `demo`

Exercise the real deck and event pipeline without contacting a model.

```text
swarmerz demo [--speed MULTIPLIER]
```

Demo does not require a deployment profile.

## Exit status

| Status | Meaning |
|---:|---|
| `0` | Command succeeded. Doctor may still have non-blocking warnings. |
| `1` | Mission or required doctor check failed. |
| `2` | Invalid arguments, missing configuration, or configuration error. |
| `130` | Terminal interruption reached the outer CLI handler. |

The manifest remains authoritative for mission status and degraded coverage; do not infer audit
quality from process status alone.

## Operations-deck keys

| Key | Action |
|---|---|
| `F1` | Open the command map. |
| `F2` / `F3` / `F4` / `F5` | Activity, reports, synthesis, and mission telemetry. |
| `F6` | Copy the workspace selection, or the full current workspace without a selection. |
| `F7` | Compatibility alias for `F6`. |
| `Ctrl-C` | Copy when the workspace selection has focus. |
| `F8` | Open the Codex follow-up view when available. |
| `F9` | Cycle themes. |
| `Tab` / `Shift-Tab` | Focus the next or previous hunter. |
| `Ctrl-Q` | Exit only after work reaches a terminal state. |

Drag without Shift for pane-local selection. Shift-drag bypasses the TUI and lets the terminal select
across the entire screen grid.

## Command bus

| Command | Action |
|---|---|
| Plain text or `/all MESSAGE` | Steer active parents and unfinished workers. |
| `/to ID MESSAGE` | Steer one hunter swarm. |
| `/wrap [ID]` | Ask one or all swarms to consolidate and finish. |
| `/status` | Show steering readiness. |
| `/focus ID` | Focus a hunter. |
| `/filter [TEXT]` | Filter the signal rail; omit text to clear. |
| `/view activity\|reports\|synthesis\|mission\|review\|help` | Change workspace view. |
| `/theme [NAME]` | List or choose a theme. |
| `/clear` | Clear the local signal rail only. |
| `/chat` | Open or resume a completed native-Codex review. |
| `/quit` or `/exit` | Exit only after the mission is terminal. |

Steering is an instruction to a running agent, not a forced process termination or a guarantee of
immediate completion.
