# Chip fork notice

This repository is planned as a public fork of [`xai-org/grok-build`](https://github.com/xai-org/grok-build).

The upstream first-party source remains licensed under the Apache License, Version 2.0. Third-party and vendored source remains under its original licenses. The upstream `LICENSE`, `THIRD-PARTY-NOTICES`, crate-local notices, and vendored notices must be preserved.

## Bootstrap identity

- Upstream repository: `https://github.com/xai-org/grok-build`
- Upstream public commit: `e5fd4816d43260c15ba785f103990c1ed6cea230`
- Upstream tree: `25eefa9bdb3a4748cc065be3fa8200d04bc54493`
- Upstream monorepo source: `SOURCE_REV=ea094a8c369475f97c85540d01730baec0dce5d6`
- Upstream package: `xai-grok-pager-bin 1.0.3`
- Bootstrap behavioral patches: **none**

The machine-readable baseline is `.chip/provenance.json` and is verified by `scripts/chip/verify_bootstrap.py`.

## Fork policy

The fork exists to expose a small, stable worker contract for the separate [`evgyur/chip-grok`](https://github.com/evgyur/chip-grok) Hermes adapter. It must not silently replace the official `grok` binary or enable new upstream capabilities without review.

Fork-only patches are intentionally bounded:

- no more than six focused commits above the accepted upstream revision;
- no broad TUI or runtime rewrite;
- preserve upstream provenance and attribution;
- prefer deleting a fork patch when upstream ships an equivalent capability;
- upstream sync occurs through reviewable branches/PRs and never auto-activates a binary.

## Security and support

This fork is not maintained by SpaceXAI. Security issues specific to fork changes should be reported privately to the fork owner. Upstream vulnerabilities should follow upstream's `SECURITY.md` process.

No credentials, provider keys, private endpoints, local configuration, or runtime receipts belong in this repository.
