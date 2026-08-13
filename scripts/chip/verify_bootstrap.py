#!/usr/bin/env python3
"""Fail-closed verifier for the source-only Chip fork bootstrap."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROVENANCE = ROOT / ".chip" / "provenance.json"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class VerificationError(RuntimeError):
    pass


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise VerificationError(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be an object")
    return value


def verify() -> dict[str, object]:
    raw = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    manifest = require_object(raw, "manifest")
    if type(manifest.get("schema")) is not int or manifest["schema"] != 1:
        raise VerificationError("unsupported provenance schema")
    if manifest.get("distribution") != "chip":
        raise VerificationError("unexpected distribution")
    if manifest.get("status") != "source-bootstrap-not-released":
        raise VerificationError("unexpected bootstrap status")

    if manifest.get("repository") != "https://github.com/evgyur/grok-build":
        raise VerificationError("fork repository contract mismatch")
    upstream = require_object(manifest.get("upstream"), "upstream")
    if upstream.get("repository") != "https://github.com/xai-org/grok-build":
        raise VerificationError("upstream repository contract mismatch")
    public_commit = upstream.get("public_commit")
    public_tree = upstream.get("public_tree")
    source_rev = upstream.get("source_rev")
    if not all(isinstance(value, str) and HEX40.fullmatch(value) for value in (public_commit, public_tree, source_rev)):
        raise VerificationError("invalid upstream identity")
    if type(upstream.get("commit_verified_by_github")) is not bool:
        raise VerificationError("commit verification flag must be boolean")

    head = git("rev-parse", "HEAD")
    base_tree = git("rev-parse", f"{public_commit}^{{tree}}")
    git("merge-base", "--is-ancestor", str(public_commit), head)
    if base_tree != public_tree:
        raise VerificationError("bootstrap upstream tree mismatch")
    allowed_bootstrap_paths = {
        ".chip/provenance.json",
        ".github/workflows/chip-bootstrap.yml",
        "NOTICE-CHIP.md",
        "scripts/chip/verify_bootstrap.py",
        "tests/chip/test_verify_bootstrap.py",
    }
    changed_paths = {
        item
        for item in git("diff", "--name-only", f"{public_commit}..{head}").splitlines()
        if item
    }
    if changed_paths != allowed_bootstrap_paths:
        raise VerificationError("bootstrap path set mismatch")
    if (ROOT / "SOURCE_REV").read_text(encoding="utf-8").strip() != source_rev:
        raise VerificationError("SOURCE_REV mismatch")

    source_contract = require_object(manifest.get("source_contract"), "source_contract")
    if source_contract.get("license") != "Apache-2.0":
        raise VerificationError("license contract mismatch")
    if type(source_contract.get("behavioral_patches")) is not int or source_contract["behavioral_patches"] != 0:
        raise VerificationError("bootstrap must contain zero behavioral patches")

    preserved = require_object(manifest.get("preserved_files"), "preserved_files")
    required = {
        "Cargo.lock",
        "LICENSE",
        "SOURCE_REV",
        "THIRD-PARTY-NOTICES",
        "crates/codegen/xai-grok-tools/THIRD_PARTY_NOTICES.md",
        "rust-toolchain.toml",
    }
    if set(preserved) != required:
        raise VerificationError("preserved file set mismatch")
    for relative, untyped_record in preserved.items():
        record = require_object(untyped_record, f"preserved_files.{relative}")
        expected_hash = record.get("sha256")
        expected_bytes = record.get("bytes")
        if not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash):
            raise VerificationError(f"invalid hash for {relative}")
        if type(expected_bytes) is not int or expected_bytes < 0:
            raise VerificationError(f"invalid byte count for {relative}")
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise VerificationError(f"missing or unsafe preserved file: {relative}")
        if path.stat().st_size != expected_bytes or sha256_file(path) != expected_hash:
            raise VerificationError(f"preserved file mismatch: {relative}")

    return {
        "status": "verified",
        "upstream_commit": public_commit,
        "upstream_tree": public_tree,
        "source_rev": source_rev,
        "behavioral_patches": 0,
        "preserved_files": len(required),
    }


def main() -> int:
    try:
        print(json.dumps(verify(), sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, VerificationError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
