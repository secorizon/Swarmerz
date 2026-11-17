<!--
Author: Laurent Gaffié, Secorizon.com
SPDX-License-Identifier: GPL-3.0-only
This file is part of Swarmerz, a terminal operations deck for independent model swarms.
-->

# Security and trust model

Swarmerz improves evidence handling, independence, and failure visibility for multi-model audits. It
does not turn model output into a security proof or provide an operating-system sandbox.

## What Swarmerz enforces

- Configuration fails closed for unknown harnesses, invalid efforts, unsafe paths, duplicate hunter
  IDs, impossible quorum, invalid isolation settings, and analyzer identity blinding disabled.
- Context is frozen once, hashed, and made unwritable. Every role receives an inode-independent
  copy-on-write or copied view. The canonical digest is rechecked before and after adjudication;
  a mismatch fails the round. Symlinks are skipped unless explicitly allowed.
- Structured answers are validated against packaged JSON schemas before sealing.
- Seals record SHA-256 integrity metadata and make staged files read-only.
- The primary analyzer receives a freshly shuffled blind bundle and captured evidence from opaque
  scratch staging rather than the identified round tree. The private answer map is written only
  after that analyzer exits.
- Source-inspection references must resolve to regular context files. Executed reproductions require
  nonempty commands and at least one regular captured artifact; accepted references are preserved
  with SHA-256 metadata in `reproduction-seal.json`.
- Missing hunters, partial fan-out, late answers, permission rejection, and degraded coverage remain
  explicit in durable artifacts.
- Optional secondary verification must use a different provider or effective model.
- Cache deletion is restricted to a marked dedicated cache root and never targets durable runs.
- On Linux with a usable user systemd manager, harness trees receive configurable memory, swap, and
  task ceilings in scopes separate from the Swarmerz controller.
- Release artifacts are checked for operational profiles, tests, shell scripts, credential-like
  filenames, private-key headers, and common provider-token signatures.

## What Swarmerz does not claim

- **Workspace separation is not hostile-process isolation.** All processes still run under the same
  operating-system user unless the deployment adds containers, VMs, or separate accounts.
- **Opaque staging is not a filesystem namespace.** It removes identified sibling artifacts from
  the judge's normal workspace and command inputs, but a deliberately hostile same-user process can
  still search other host paths. Use a separate identity, container, or VM for that threat model.
- **Resource ceilings are damage containment, not a sandbox.** Automatic mode degrades visibly to
  process-group cleanup where Linux user scopes are unavailable. Shared OpenCode mode has one
  resource envelope for its shared service rather than one envelope per hunter.
  Swarmerz supplies the login's DBus address and XDG runtime directory only to the
  `systemd-run` launcher when needed, then removes those additions before the harness starts.
- **Read-only files are not external attestation.** Their owner can change permissions. Canonical
  context changes are detected at protocol boundaries, but seals still provide no transparency log,
  TPM measurement, or independent witness.
- **Blind labels do not erase writing style.** A model can identify itself, cite a provider-specific
  resource, or reveal substrate through its prose.
- **Worker counts do not prove independent reasoning.** Native child sessions and custom ledgers show
  observable fan-out, not semantic diversity.
- **Quorum is not consensus truth.** It permits analysis with available answers; it does not validate
  a claim by vote.
- **An empty report is not a clean bill of health.** Correlated models can share blind spots.
- **Detection lift is not benchmarked by this repository.** Multiple substrates are intended to
  broaden the search, but no bundled corpus establishes recall, precision, or a guaranteed gain from
  adding another model.

Use a hardened execution boundary when audited code may be malicious or when model tools can execute
untrusted build steps.

## Finding provenance

The analyzer must classify each validated finding:

| Provenance | Meaning |
|---|---|
| `independent_discovery` | The finding was derived independently from the target evidence. |
| `prior_artifact_corroboration` | The answer corroborated a claim already present in supplied material. |
| `derived` | The finding was derived from an accepted chain or other documented reasoning path. |

It must also state the strongest validation method actually performed:

| Validation method | Meaning |
|---|---|
| `executed_reproduction` | Nonempty commands were recorded and at least one cited regular captured artifact was resolved, preserved, and hashed. |
| `source_inspection` | Cited regular source files and relevant logic were directly inspected. |
| `reasoning_only` | The conclusion remains analytical and was not directly reproduced or inspected strongly enough for a higher class. |

Multiple hunters following the same clue demonstrate coverage of that clue, not multiple independent
discoveries. Review provenance together with validation method, evidence references, provider/model
overlap, and reproduction records.

## Credential handling

Never put secrets directly in `swarmerz.toml`, documentation, prompts, or a frozen target tree.
Prefer authenticated harness sessions or pass only reviewed variable names through `pass_env`.
Environment inheritance is disabled by default; literal `env` values can become part of operational
state and should not contain credentials.

An Ollama endpoint URL cannot contain embedded username/password data. Per-hunter OpenCode isolation
uses private state roots and therefore does not silently reuse the user's global OpenCode auth store.
For hosted providers, Swarmerz queries the private server's integration metadata and forwards only
the provider-declared variables that are present in the launch environment. It does not read or copy
the global OpenCode database. The temporary private HOME and database are removed when the hunter
stops, including handled startup failures.

Environment credentials are still visible to processes running as the same user and may count
against the same provider billing and rate limits. Per-hunter state separation is not a secret
boundary against hostile model-driven commands; use separate OS identities, containers, or VMs for
that threat model.

Before freezing a repository, exclude credentials, local environment files, private keys, cloud
profiles, signing material, and unrelated proprietary data. Hidden files receive no automatic trust.

## Provider and data boundary

Frozen context, prompts, and generated evidence are sent to the providers and command adapters named
by the deployment profile. A local model may keep source within the team's infrastructure; a hosted
route may not. Mixed fleets intentionally cross multiple provider boundaries.

Review every route for data residency, retention, account policy, cost, rate limits, and model-tool
permissions before launch.

## Recommended acceptance standard

Treat the final report as a high-quality lead and evidence record, then conduct the security team's
normal human review. For a finding intended to support disclosure or remediation, prefer:

- `independent_discovery` or a clearly documented derived chain.
- `source_inspection` or `executed_reproduction`.
- Evidence paths and reproduction artifacts that can be independently checked.
- Confirmation from a different provider/model substrate when the consequence warrants it.
- A manifest showing acceptable hunter and worker coverage.

The reviewer/judge completes the software workflow. Human acceptance remains an organizational
decision outside Swarmerz.
