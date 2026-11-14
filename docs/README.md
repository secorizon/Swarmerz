<!--
Author: Laurent Gaffié, Secorizon.com
SPDX-License-Identifier: GPL-3.0-only
This file is part of Swarmerz, a terminal operations deck for independent model swarms.
-->

# Swarmerz documentation

This documentation describes the Swarmerz 0.1 release line. The root
[README](../README.md) is the product overview; the guides below are the operational reference.

- [Installation and platform support](installation.md)
- [Configuration reference](configuration.md)
- [CLI reference](cli-reference.md)
- [Operating missions and recovery](operations.md)
- [Security and trust model](security-model.md)

## Protocol at a glance

Swarmerz completes four software stages:

1. Freeze the selected evidence and record its integrity metadata.
2. Launch the configured independent hunter parents and their worker fan-out.
3. Validate, seal, and blind structured hunter answers; continue when the configured quorum is met.
4. Give the sealed reports to a fresh reviewer/judge, which emits the final report.

An optional pre-hunt baseline and an optional secondary machine verifier can be configured, but
neither changes the four-stage default workflow. Human review begins after Swarmerz emits its final
result; it is not an in-product approval stage.

## Documentation boundary

The repository contains documentation and example profiles for operators. Installable release
artifacts contain the Python runtime and required JSON schemas, but no operational profile, example
configuration, tests, shell launcher, or credentials. Every deployment must supply and review its
own `swarmerz.toml`.
