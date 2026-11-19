<!--
Author: Laurent Gaffié, Secorizon.com
SPDX-License-Identifier: GPL-3.0-only
This file is part of Swarmerz, a terminal operations deck for independent model swarms.
-->

# CLI reference

The installed entry point is `swarmerz`. From a source checkout, `./bin/swarmerz` invokes the same
Python CLI.

```text
swarmerz [--version] {run,analyze,chat,cache,doctor,models,config-models,demo} ...
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

Open or inspect a follow-up when the primary analyzer is native Codex or Claude Code.

```text
swarmerz chat [--config PATH] [--set PATH=VALUE]
              --session NAME --round-id ID [--resume] [--print-command]
```

The first call creates a readable conversation in the configured native judge harness, grounded in
the completed report. Use `--resume` for a later continuation. `--print-command` displays the
launch without starting it.

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

## `models`

Print the profile's configured hunters and judges, then query every installed native harness
without sending a model prompt. This command is read-only and is intended for diagnostics and
automation:

```text
swarmerz models [--config PATH] [--set PATH=VALUE] [--json]
```

OpenCode supplies its project-scoped live model catalog and the display names of connected API or
token-plan providers. Swarmerz retains only provider IDs, display names, and a connected marker from
`auth list`; connection objects and credential values are discarded.
Codex models come from its local ChatGPT account catalog and are marked stale after 24 hours.
Claude Code currently has no non-interactive command that lists the account's complete entitlement;
Swarmerz therefore reports its local default, local allowlist, aliases advertised by the installed
CLI, and the documented Fable, Haiku, Opus, and Sonnet aliases. These are selectable aliases rather
than a claim of account entitlement, which Claude Code validates at launch. Authentication
identities, tokens, email addresses, organization IDs, and raw command failures are never printed.

Every displayed native model has a canonical selector. `--json` returns the provider grouping,
catalog freshness, configured roles, reasoning capabilities, and selectors without terminal
formatting.

## `config-models`

Open the full-screen interactive model configurator:

```text
swarmerz config-models [--config PATH]
```

The catalog combines OpenCode's project catalog and connection markers, the Codex account catalog,
and Claude Code's locally configured choices. Configured native routes appear first in profile
order; unconfigured choices follow in Claude Code, Codex, then OpenCode order. Current native
hunters are pre-checked, and a current native Codex or Claude Code primary judge is marked `[J]`.
Command-adapter hunters cannot be reconstructed from a provider catalog, so the configurator
displays them in the review as preserved and never removes them.

| Key | Action |
|---|---|
| `Tab` / `Shift-Tab` | Cycle All, Claude, Codex, and OpenCode catalog filters. |
| `O` | Enter a local or LAN Ollama server URL and discover its installed models. |
| `Up` / `Down`, `Page Up` / `Page Down`, mouse wheel | Scroll the model catalog. |
| `Space` | Check or uncheck the highlighted model as a hunter. |
| `J` | Assign the highlighted Codex or Claude Code model as primary judge. |
| `R` | Reset the staged judge to the current profile. |
| `[` / `]` | Decrease or increase the worker count used for newly checked hunters. |
| `Enter` | Open the staged diff; from the diff, validate and apply it. |
| `Escape` | Return from review, or cancel from the catalog. |
| `Ctrl-Q` | Cancel without writing. |
| `F9` | Cycle the normal Swarmerz themes. |

The screen reflows at narrow terminal widths: the catalog takes the full width instead of squeezing
model IDs beside the detail pane. New hunters receive a unique derived ID, the selected global
worker count (four initially, adjustable from 2 through 32), a 14,400-second timeout, and a
conservative default effort (`default` for OpenCode, `high` for native Codex/Claude when
advertised). Existing checked hunter tables keep their exact settings.

Ollama discovery accepts a full URL such as `http://192.168.8.167:11434/v1` or a bare LAN host such
as `192.168.8.167`, which uses port `11434`. It contacts the server directly, without a configured
shell proxy, and reads at most 4 MiB from the read-only `/api/tags` inventory; it does not load a
model or send a prompt. The selected hunter keeps that exact endpoint in `ollama_base_url`, and
normal mission preflight verifies completion and tool capabilities before launch. URLs cannot
contain embedded credentials, queries, or fragments.

The review screen lists every addition, removal, judge replacement, and preserved custom hunter.
Swarmerz writes one same-directory temporary profile, parses it through the complete validator, and
atomically replaces the configured path only after validation succeeds. It reconciles
`max_parallel_hunters` and `minimum_answers` with the resulting fleet. Native OpenCode remains a
hunter harness; the interactive primary-judge choice is restricted to Codex and Claude Code. Use a
documented `command` adapter when another runtime must judge.

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
| `F8` | Open the judge follow-up view when available. |
| `F9` | Cycle themes. |
| `Tab` / `Shift-Tab` | Focus the next or previous hunter. |
| `Ctrl-Q` | Exit only after work reaches a terminal state. |

Drag without Shift for pane-local selection. Shift-drag bypasses the TUI and lets the terminal select
across the entire screen grid.

## Command bus

| Command | Action |
|---|---|
| Plain text or `/all MESSAGE` | Steer active parents and unfinished workers. |
| `/to ID MESSAGE` | Steer one hunter swarm, or resume a recoverable harness session. |
| `/wrap [ID]` | Ask one or all swarms to consolidate and finish. |
| `/status` | Show steering readiness. |
| `/focus ID` | Focus a hunter. |
| `/filter [TEXT]` | Filter the signal rail; omit text to clear. |
| `/view activity\|reports\|synthesis\|mission\|review\|help` | Change workspace view. |
| `/theme [NAME]` | List or choose a theme. |
| `/clear` | Clear the local signal rail only. |
| `/chat` | Open or resume a completed review when the judge uses native Codex or Claude Code. |
| `/quit` or `/exit` | Exit only after the mission is terminal. |

Steering is an instruction to a running agent, not a forced process termination or a guarantee of
immediate completion.

A failed native Codex or Claude Code hunter, or verified native OpenCode hunter, remains
`RECOVERABLE` only while another hunter is still open. A targeted `/to ID MESSAGE` starts a
follow-up on the same thread or session and in the same workspace. Broadcast commands exclude
recoverable hunters, so an operator must make the retry explicit. Active Claude Code print turns do
not expose live steering; the targeted path becomes available after a failed turn has yielded a
session ID. Failures that never established a safe resumable session are terminal; Cline and custom
command adapters do not currently expose this recovery path.

The deck displays Codex- and Claude Code-backed hunters by configured model name, such as
`DAYBREAK`, `GPT-5.6-SOL`, or `MYTHOS-5-2`. Commands continue to use the stable hunter ID shown in
brackets, such as `/to claude continue`.
