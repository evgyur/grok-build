# Fork security and update policy

This file applies only to changes maintained in `evgyur/grok-build`. The upstream reporting process remains in [`SECURITY.md`](SECURITY.md).

## Reporting a fork-specific vulnerability

Do not open a public issue containing exploit details, credentials, private endpoints, or affected-user data. Report fork-specific vulnerabilities privately to the repository owner through GitHub's private vulnerability reporting surface when it is enabled. If that surface is unavailable, use the maintainer contact shown on the repository owner's verified GitHub profile and disclose only enough metadata to establish a private channel.

Vulnerabilities that also affect unmodified upstream code should be reported through the upstream process in `SECURITY.md`.

## Supported state

No Chip fork binary is released or supported during Phase 0. The repository is source-only and `behavioral_patches` must remain `0` in `.chip/provenance.json`.

A future supported release must identify all of the following:

- exact upstream public commit and `SOURCE_REV`;
- exact fork commit and source-tree digest;
- pinned Rust toolchain and build-container digest;
- binary SHA-256 and test receipts;
- explicit prerelease or release status.

## Update boundary

Upstream discovery and fetch may be automated. Merge/rebase, behavioral patch replay, build, review, release, and runtime activation are separate gated actions.

The fork must never:

- auto-merge upstream into `main`;
- auto-promote a newly built binary;
- silently replace the official `grok` executable in global `PATH`;
- rely on Grok's official self-updater for a fork binary;
- commit credentials, provider endpoints, local configuration, receipts, or private paths.

A future fork binary must run with self-update disabled and be selected by the separate `chip-grok` adapter through an exact lock manifest. Runtime activation belongs to that adapter and is out of scope for this repository's Phase 0 baseline.

## Upstream sync procedure

1. Fetch `https://github.com/xai-org/grok-build` into the `upstream` remote.
2. Pin the new public commit, tree, `SOURCE_REV`, toolchain, lockfile, licenses, and notices.
3. Create a review branch; never update `main` directly from a scheduler.
4. Classify CLI, headless protocol, sandbox, updater, hooks, MCP, plugin, subagent, dependency, and license changes.
5. Replay only the bounded fork patch stack and delete patches superseded upstream.
6. Run provenance, source build, mock headless E2E, public hygiene, and compatibility checks.
7. Merge only after an exact-candidate review. A merged source candidate still does not activate a runtime binary.
