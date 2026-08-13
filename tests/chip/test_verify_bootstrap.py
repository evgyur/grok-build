from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "chip" / "verify_bootstrap.py"
REQUIRED = [
    "Cargo.lock",
    "LICENSE",
    "SOURCE_REV",
    "THIRD-PARTY-NOTICES",
    "crates/codegen/xai-grok-tools/THIRD_PARTY_NOTICES.md",
    "rust-toolchain.toml",
]
COMMIT = "a" * 40
TREE = "b" * 40
SOURCE_REV = "c" * 40
BUILD_CONTAINER = "docker.io/library/rust@sha256:365468470075493dc4583f47387001854321c5a8583ea9604b297e67f01c5a4f"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chip_verify_bootstrap", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BootstrapVerifierTests(unittest.TestCase):
    def fixture(self, root: Path) -> dict[str, object]:
        preserved: dict[str, object] = {}
        for relative in REQUIRED:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = (SOURCE_REV + "\n").encode() if relative == "SOURCE_REV" else f"fixture:{relative}\n".encode()
            path.write_bytes(content)
            preserved[relative] = {"sha256": sha256(content).hexdigest(), "bytes": len(content)}
        manifest: dict[str, object] = {
            "schema": 1,
            "distribution": "chip",
            "status": "source-bootstrap-not-released",
            "repository": "https://github.com/evgyur/grok-build",
            "upstream": {
                "repository": "https://github.com/xai-org/grok-build",
                "public_commit": COMMIT,
                "public_tree": TREE,
                "source_rev": SOURCE_REV,
                "fetched_at": "2026-08-13T00:00:00Z",
                "commit_verified_by_github": False,
            },
            "source_contract": {
                "package": "xai-grok-pager-bin",
                "package_version": "1.0.3",
                "rust_toolchain": "1.94.0",
                "build_container": BUILD_CONTAINER,
                "license": "Apache-2.0",
                "behavioral_patches": 0,
            },
            "preserved_files": preserved,
        }
        provenance = root / ".chip" / "provenance.json"
        provenance.parent.mkdir(parents=True)
        provenance.write_text(json.dumps(manifest))
        return manifest

    def configured_module(self, root: Path) -> ModuleType:
        module = load_module()
        setattr(module, "ROOT", root)
        setattr(module, "PROVENANCE", root / ".chip" / "provenance.json")

        def fake_git(*args: str) -> str:
            if args == ("rev-parse", "HEAD"):
                return "d" * 40
            if args == ("rev-parse", f"{COMMIT}^{{tree}}"):
                return TREE
            if args[:3] == ("merge-base", "--is-ancestor", COMMIT):
                return ""
            if args == ("diff", "--name-only", f"{COMMIT}..{'d' * 40}"):
                return "\n".join(
                    [
                        ".chip/provenance.json",
                        ".github/workflows/chip-bootstrap.yml",
                        "NOTICE-CHIP.md",
                        "README.md",
                        "SECURITY-CHIP.md",
                        "scripts/chip/verify_bootstrap.py",
                        "tests/chip/test_verify_bootstrap.py",
                    ]
                )
            raise AssertionError(args)

        setattr(module, "git", fake_git)
        return module

    def test_valid_fixture_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            result = self.configured_module(root).verify()
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["behavioral_patches"], 0)

    def test_tampered_preserved_file_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            root.joinpath("LICENSE").write_text("tampered\n")
            with self.assertRaisesRegex(RuntimeError, "preserved file mismatch"):
                self.configured_module(root).verify()

    def test_wrong_package_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.fixture(root)
            source_contract = manifest["source_contract"]
            assert isinstance(source_contract, dict)
            source_contract["package"] = "wrong-package"
            root.joinpath(".chip", "provenance.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, "package contract mismatch"):
                self.configured_module(root).verify()

    def test_wrong_package_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.fixture(root)
            source_contract = manifest["source_contract"]
            assert isinstance(source_contract, dict)
            source_contract["package_version"] = "9.9.9"
            root.joinpath(".chip", "provenance.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, "package version contract mismatch"):
                self.configured_module(root).verify()

    def test_wrong_rust_toolchain_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.fixture(root)
            source_contract = manifest["source_contract"]
            assert isinstance(source_contract, dict)
            source_contract["rust_toolchain"] = "nightly"
            root.joinpath(".chip", "provenance.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, "Rust toolchain contract mismatch"):
                self.configured_module(root).verify()

    def test_wrong_build_container_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.fixture(root)
            source_contract = manifest["source_contract"]
            assert isinstance(source_contract, dict)
            source_contract["build_container"] = "docker.io/library/rust:latest"
            root.joinpath(".chip", "provenance.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, "build container contract mismatch"):
                self.configured_module(root).verify()

    def test_boolean_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.fixture(root)
            manifest["schema"] = True
            root.joinpath(".chip", "provenance.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(RuntimeError, "unsupported provenance schema"):
                self.configured_module(root).verify()


if __name__ == "__main__":
    unittest.main()
