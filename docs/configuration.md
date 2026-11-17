<!--
Author: Laurent Gaffié, Secorizon.com
SPDX-License-Identifier: GPL-3.0-only
This file is part of Swarmerz, a terminal operations deck for independent model swarms.
-->

# Configuration reference

Swarmerz reads a TOML deployment profile. Release artifacts intentionally provide no operational
profile: each team must review its own models, providers, credentials, storage paths, fan-out,
timeouts, and quorum before launching work.

The source repository retains two examples for adaptation:

- `swarmerz.toml`: a larger heterogeneous hosted example.
- `examples/local-ollama.toml`: a small local-first example.

They are templates, not model recommendations, and they are not included in installable artifacts.

## Profile discovery and paths

Pass a profile explicitly for reproducible operation:

```bash
swarmerz doctor --config /absolute/path/to/team-swarmerz.toml
swarmerz run --config /absolute/path/to/team-swarmerz.toml
```

Without `--config`, Swarmerz looks for `swarmerz.toml` in the working directory and then, when run
from a checkout, at the repository root. Relative `output_root`, context, cache, and prompt paths are
resolved from the selected profile's directory, not from the launch directory.

## Root settings

| Setting | Default | Contract |
|---|---:|---|
| `schema_version` | `1` | Must be exactly `1`. |
| `output_root` | `./swarm-runs` | Durable sessions, rounds, logs, evidence, and final reports. |
| `max_parallel_hunters` | Hunter count | Must be at least the hunter count. It is a capacity declaration, not a scheduler limit. |
| `minimum_answers` | Hunter count | Must be between `2` and the hunter count. It controls availability, not voting or truth. |
| `opencode_artifact_grace_seconds` | `3600` | Grace period associated with late OpenCode artifacts. May be zero. |
| `opencode_worker_timeout_seconds` | Artifact grace value | Positive worker-observation timeout. |
| `opencode_worker_poll_seconds` | `30` | Positive worker polling interval. |
| `opencode_answer_quiet_seconds` | `180` | Quiet period used when deciding whether an answer is stable. |
| `opencode_orphan_quiet_seconds` | `600` | Quiet period used by orphan recovery. |
| `opencode_finalize_timeout_seconds` | `900` | Timeout for recovery consolidation. |
| `opencode_finalize_answer_quiet_seconds` | `60` | Quiet period for a recovery answer. |
| `codex_transport_retries` | `2` | Transport retries; accepted range is `0`–`10`. |
| `codex_retry_delay_seconds` | `10` | Delay between transport retries; may be zero. |

## Context settings

The optional `[context]` table controls the default evidence tree.

| Setting | Default | Contract |
|---|---:|---|
| `path` | None | Directory to freeze when no launch override is supplied. |
| `allow_symlinks` | `false` | Symlinks are skipped unless explicitly allowed. |
| `exclude` | `[]` | File or directory name patterns. Entries cannot contain path separators. |

Hidden files, credentials, repository metadata, build outputs, and private test data are not
automatically safe to disclose. Review exclusions before every external-provider audit.

## Hunter engines

Configure between 2 and 26 `[[hunters]]` tables. Hunter IDs must be unique, start with a letter or
digit, and contain only letters, digits, dots, underscores, or hyphens.

| Field | Required | Meaning |
|---|---|---|
| `id` | Yes | Stable hunter identity used in artifacts and steering. |
| `harness` | Yes | `opencode`, `codex`, `codex-exec`, `cline`, or `command`. |
| `harness_version` | Recommended | Exact harness pin; `unversioned` and `latest` disable an exact pin. |
| `provider` | Yes in practice | Model route/provider identifier recorded for overlap analysis. |
| `model` | Yes | Requested model identifier. |
| `model_snapshot` | No | Exact snapshot used instead of the display model when supplied. |
| `reasoning_effort` | Yes | Requested effort from the declared supported set. |
| `supported_efforts` | Usually | Required for OpenCode and command routes; Codex and Cline have built-in capability rules. |
| `timeout_seconds` | No | Parent timeout; default `14400`. Must be positive. |
| `workers` | Yes for hunters | Parent-requested worker count. |
| `subagents` | No | Alternate harness fan-out declaration. Effective fan-out is `max(workers, subagents)`. |
| `worker_timeout_seconds` | No | Optional positive timeout for a worker. |
| `answer_path` | No | Defaults to `.swarm/answer.json`; must stay inside the workspace. |
| `extra_args` | No | Additional harness arguments. Codex hunters cannot use `--ephemeral`. |
| `pass_env` | No | Names of selected host environment variables passed to the engine. |
| `env` | No | Literal scalar environment values. Do not place credentials here. |
| `command` | Command adapter only | Argument vector for an internal adapter. |
| `ollama_base_url` | Ollama route only | HTTP(S) URL ending in `/v1`, without embedded credentials, query, or fragment. |

Every hunter must configure an effective fan-out of at least two. Swarmerz asks the parent to create
that many focused workstreams and records the available harness evidence; it does not claim that a
session count proves semantic independence.

Reasoning efforts are `default`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`.
Only values explicitly supported by the selected harness/model combination are accepted.

## Primary analyzer

The `[analyzer]` table uses the common engine fields with these additional rules:

- Supported harnesses are native Codex (`codex` or `codex-exec`) and `command`.
- `blind_hunter_identity` must remain `true`.
- `baseline_first` defaults to `false`. When enabled, the analyzer runs once before hunter answers
  are disclosed and again after quorum for final adjudication.
- Analyzer fan-out is not subject to the hunters' minimum-two-worker rule.

The reviewer/judge is the final software stage. It evaluates the sealed reports and writes the final
report; human review occurs afterward.

## Secondary analyzer

`[secondary_analyzer]` is optional. When configured, it must have a distinct ID and use a different
provider or effective model from the primary analyzer. It assesses each primary finding as
`confirmed`, `rejected`, or `uncertain`; only confirmed findings enter the accepted carry-forward
set.

## Isolation settings

| Setting | Default | Meaning |
|---|---:|---|
| `isolation.mode` | `workspace` | Independent context views and opaque adjudication staging; still a same-user application boundary. |
| `isolation.inherit_environment` | `false` | Prevents unrestricted host-environment inheritance. |
| `isolation.opencode_server_mode` | `shared` | `shared` uses one private round service; `per_hunter` gives native OpenCode parents separate passwords and state roots. |

Both server modes run as the same operating-system user. Use containers or VMs when the audited code
or model-driven tools may be hostile.

## Resource-limit settings

The optional `[resources]` table bounds accidental process and memory explosions independently of
the model prompt.

| Setting | Default | Meaning |
|---|---:|---|
| `resources.mode` | `auto` | `auto` uses Linux user-systemd scopes when available, `required` fails startup without them, and `off` disables operating-system ceilings. |
| `resources.memory_max_gib` | `16` | Maximum resident memory charged to one harness scope. Zero disables this individual ceiling. |
| `resources.memory_swap_max_gib` | `2` | Maximum swap charged to one harness scope. Zero disables this individual ceiling. |
| `resources.tasks_max` | `512` | Maximum processes and threads in one harness scope. Zero disables this individual ceiling. |

```toml
[resources]
mode = "auto"
memory_max_gib = 16
memory_swap_max_gib = 2
tasks_max = 512
```

On Linux, Swarmerz verifies the user systemd manager before launching a harness. Each native Codex,
command-adapter, and adjudicator process tree receives its own transient scope. A shared OpenCode
service has one envelope covering all native OpenCode hunters attached to that service;
`opencode_server_mode = "per_hunter"` gives each service a separate envelope. The controller stays
outside these scopes, so a limit event can fail the affected harness without killing the deck.

In `auto` mode, macOS and Linux installations without a usable user-systemd manager continue with
process-group timeout cleanup and emit a warning. Use `required` when a deployment must refuse an
uncontained mission. An instruction that explicitly requests fuzzing overrides the default prompt
restriction, but it does not silently override operating-system ceilings. Raise the reviewed values
or set `mode = "off"` for an intentionally larger campaign.

## Storage settings

The `[storage]` table controls disposable build and dependency caches, not durable audit evidence.

| Setting | Default | Meaning |
|---|---:|---|
| `cache_root` | `$XDG_CACHE_HOME/swarmerz` or `~/.cache/swarmerz` | Dedicated marked cache directory. |
| `max_cache_gib` | `40` | Positive cache size limit. |
| `max_cache_age_days` | `30` | Positive age limit. |
| `failed_scratch_retention_days` | `7` | Retention for failed-run scratch. |
| `maintenance_interval_hours` | `24` | Automatic maintenance interval. |
| `cleanup_successful_workspaces` | `true` | Removes reproducible successful scratch after durable evidence staging. |

The cache root and `output_root` cannot contain one another. The cache root cannot be `/`, the home
directory, an unmarked non-empty directory, or a symlink. Cache maintenance never deletes
`swarm-runs`. Randomly named adjudication staging under the managed `opaque/` subtree follows the
same successful cleanup and failed-scratch retention policy.

## Credentials

Do not store API keys, session tokens, private keys, or passwords in the profile. Prefer a provider's
authenticated local session or list only the required variable names in `pass_env`. Unlisted host
variables are not inherited when environment inheritance is disabled.

Per-hunter OpenCode mode intentionally does not reuse the user's shared OpenCode auth store. Each
private server reports the environment-variable names supported by its selected provider. Swarmerz
forwards only matching non-empty values from the launch environment, then confirms that OpenCode
registered the environment connection. Custom provider variables can still be named explicitly in
`pass_env`. Swarmerz never imports, copies, or modifies the user's OpenCode credential database,
and it removes each hunter's temporary OpenCode HOME and database when that hunter stops. A provider
authenticated only through saved OAuth or token-plan state must use shared mode or a dedicated
environment credential.

## One-run overrides

Repeat `--set PATH=VALUE` for temporary changes. Array positions use numeric indexes, and values are
parsed as JSON when possible.

```bash
swarmerz doctor \
  --config /absolute/path/to/team-swarmerz.toml \
  --set minimum_answers=4 \
  --set hunters.0.timeout_seconds=7200
```

Overrides must target existing keys; they cannot create unknown settings. For repeatable production
runs, prefer a reviewed profile over a long override list.
