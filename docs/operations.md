<!--
Author: Laurent Gaffié, Secorizon.com
SPDX-License-Identifier: GPL-3.0-only
This file is part of Swarmerz, a terminal operations deck for independent model swarms.
-->

# Operating missions and recovery

Swarmerz is an audit protocol with a terminal control surface. The final software output is the
reviewer/judge's report; security-team review follows outside the product.

## Round lifecycle

1. **Freeze evidence.** A codebase context is copied once, hashed, made unwritable, and linked into
   isolated workspaces. Raw missions without local evidence skip this step.
2. **Optional baseline.** When `analyzer.baseline_first = true`, the primary analyzer records a
   pre-hunt assessment before it can see hunter answers. This is disabled by default.
3. **Independent hunts.** Every configured parent starts independently and manages its requested
   worker fan-out. Hunters do not receive other hunters' raw answers.
4. **Seal and quorum.** Contract-valid answers are staged, hashed, made read-only, and copied into a
   blind answer bundle. Missing or partial coverage remains visible.
5. **Reviewer/judge.** A fresh analyzer evaluates every sealed report, disagreements, factual risks,
   provenance, and validation method, then emits the final result.
6. **Optional secondary verification.** A distinct configured analyzer can confirm, reject, or mark
   each primary finding uncertain before carry-forward.
7. **Durable finalization.** Attributed answers, analysis, report, logs, coverage, and evidence remain
   under the round directory.

Quorum is an availability threshold. It does not turn agreement into proof, and it does not mean
that missing hunters found nothing.

## Starting a codebase mission

Use absolute paths for operational clarity:

```bash
swarmerz run \
  --config /absolute/path/to/team-swarmerz.toml \
  --context /absolute/path/to/target \
  --session protocol-audit \
  --instruction "Map trust boundaries and validate exploitable authorization failures."
```

The non-headless command opens startup selection and preflight. No model work begins until the final
command is submitted. `Ctrl-B` returns to the previous preflight stage; `Ctrl-Q` cancels before
launch.

## Sessions and later rounds

A session keeps one frozen context and a history of rounds. Reuse the session name only when the
evidence tree is unchanged. In later rounds, omit `--context` or provide the same original path.
Start a new session when the tree, target, trust assumptions, or desired hunter lineage changes.

Accepted findings—not other hunters' raw answers—can form the validated baseline for a later round.
This preserves useful continuity without turning the next hunt into transcript sharing.

Session names start with a letter or digit and may contain letters, digits, dots, underscores, and
hyphens.

## Headless operation

Headless mode is intended for controlled CI, replay, or unattended terminals:

```bash
swarmerz run --headless --json \
  --config /absolute/path/to/team-swarmerz.toml \
  --context /absolute/path/to/target \
  --session protocol-audit \
  --instruction-file /absolute/path/to/instruction.txt
```

Progress is written to standard error when JSON output is selected. Standard output contains the
status, degraded flag, session and round IDs, round directory, and final-report path.

## Durable artifacts

The configured `output_root` is the evidence store. Its core layout is:

```text
sessions/<session>/
├── session.json
├── input/context/
└── rounds/<round-id>/
    ├── instruction.txt
    ├── manifest.json
    ├── hunters/<id>/
    │   ├── answer.json
    │   ├── seal.json
    │   ├── result.json
    │   ├── worker-status.json
    │   ├── fanout-manifest.json
    │   ├── evidence-manifest.json
    │   ├── evidence/
    │   └── stdout.jsonl / stderr.log
    ├── baseline/baseline.json
    ├── analysis/
    │   ├── analysis.json
    │   ├── accepted-analysis.json
    │   ├── blind-answers.json
    │   └── answer-map.json
    ├── secondary-analysis/verification.json
    └── final/
        ├── answers.json
        ├── analysis.json
        └── report.md
```

Optional directories appear only when their features run. `manifest.json` is authoritative for
status, coverage, degradation, and lifecycle state. A draft or `partial-notes.md` is not a sealed
answer.

Successful workspaces are removable scratch after referenced evidence is staged and hashed.
Incomplete or failed workspaces can be retained for recovery according to storage policy.

## Cache versus evidence

The cache stores reproducible build products, package-manager caches, temporary files, and retained
failed-run scratch. It does not store the authoritative final report or sealed evidence.

```bash
swarmerz cache --config /absolute/path/to/team-swarmerz.toml status
swarmerz cache --config /absolute/path/to/team-swarmerz.toml prune
swarmerz cache --config /absolute/path/to/team-swarmerz.toml clear
```

`clear` refuses to operate without the expected cache marker or while a mission is active. It never
targets `output_root`.

## Recovery

### Primary adjudication failed

If enough hunters sealed answers but the primary analyzer failed, retry adjudication only:

```bash
swarmerz analyze \
  --config /absolute/path/to/team-swarmerz.toml \
  --session protocol-audit \
  --round-id ROUND_ID
```

This verifies existing seals and does not rerun hunters. Use `--adopt-late-answers` only after
reviewing a valid answer that arrived after failure; adoption remains explicitly recorded.

### Hunter is blocked

Inspect the signal rail, `worker-monitor.json`, harness stderr, and any unresolved permission
request. Swarmerz rejects unattended OpenCode permission escalation rather than silently granting
access. Use `/wrap ID` to request consolidation if useful partial work exists.

### Stored round is missing

Use the same profile and `output_root` override that created the round. The archive picker reads the
selected profile's durable root; it does not search the entire host.

### Clipboard copy is unavailable

Run `swarmerz doctor`. On Wayland, install `wl-clipboard`; on X11, install `xclip` or `xsel`. macOS
uses `pbcopy`. Missing clipboard integration never stops a mission.

## Reading a result

Start with these artifacts:

1. `manifest.json` for status, configured versus sealed coverage, and degraded state.
2. `final/report.md` for the judge's readable result.
3. `final/analysis.json` for attributed validated findings and unresolved questions.
4. `analysis/accepted-analysis.json` for the carry-forward set after optional secondary verification.
5. Each finding's evidence references, reproduction record, validation method, and provenance class.

Agreement count alone is not validation. Prefer findings supported by source inspection or executed
reproduction and inspect whether supposedly independent confirmation actually used a distinct
provider/model substrate.
