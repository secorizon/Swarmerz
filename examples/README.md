<!--
Author: Laurent Gaffié, Secorizon.com
SPDX-License-Identifier: GPL-3.0-only
This file is part of Swarmerz, a terminal operations deck for independent model swarms.
-->

# Swarmerz configuration examples

These files are templates, not model recommendations. Every uncommented `[[hunters]]` table is
launched, so copy an example and keep only the routes that match the deployment.

See the [configuration reference](../docs/configuration.md) for field contracts, path resolution,
credential handling, isolation modes, and storage policy.

- `local-ollama.toml` starts with two native OpenCode parents routed to an Ollama-compatible local
  or LAN endpoint. Replace both model names with exact values from `ollama list` and replace the
  analyzer command with the team's schema-aware local judge adapter.
- The product-root `swarmerz.toml` is the larger hosted example. It demonstrates a heterogeneous
  fleet and native Codex adjudication; it is not a required or preferred roster.
- Native Claude Code can be added as either a hunter or adjudicator with
  `harness = "claude-code"`; use the exact model available to the authenticated Claude account.

Run an example explicitly:

```bash
./bin/swarmerz run --config examples/local-ollama.toml
```

For automation, add `--headless` plus `--instruction` or `--instruction-file`. Review all endpoints,
credentials, context exclusions, capacity, and quorum before a real audit.
