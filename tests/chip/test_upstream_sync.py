from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = ROOT / "scripts" / "chip" / "upstream_sync.py"
PR_SCRIPT = ROOT / "scripts" / "chip" / "upstream_sync_pr.py"
WORKFLOW = ROOT / ".github" / "workflows" / "chip-upstream-sync.yml"


def load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def bare_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--git-dir", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def write(repository: Path, relative: str, content: str) -> None:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class LocalRepositories:
    def __init__(self, root: Path) -> None:
        self.upstream = root / "upstream"
        self.fork = root / "fork"
        self.upstream.mkdir()
        git(self.upstream, "init", "-q")
        git(self.upstream, "config", "user.name", "Fixture")
        git(self.upstream, "config", "user.email", "fixture@example.invalid")

        self.source_revs = [character * 40 for character in "abc"]
        initial = {
            "Cargo.lock": "lock-v1\n",
            "LICENSE": "Apache-2.0 fixture\n",
            "NOTICE": "notice-v1\n",
            "README.md": "shared baseline\n",
            "SOURCE_REV": self.source_revs[0] + "\n",
            "rust-toolchain.toml": "[toolchain]\nchannel = \"1.90.0\"\n",
            "crates/codegen/worker-core/Cargo.toml": "[package]\nname = \"worker-core\"\nversion = \"0.1.0\"\n",
            "crates/codegen/worker-core/src/lib.rs": "pub fn worker() {}\n",
        }
        for relative, content in initial.items():
            write(self.upstream, relative, content)
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "upstream one")
        self.old = git(self.upstream, "rev-parse", "HEAD")
        self.old_tree = git(self.upstream, "rev-parse", "HEAD^{tree}")

        write(self.upstream, "Cargo.lock", "lock-v2\n")
        write(self.upstream, "SOURCE_REV", self.source_revs[1] + "\n")
        write(self.upstream, "crates/codegen/xai-grok-hooks/src/lib.rs", "pub fn hooks() {}\n")
        write(self.upstream, "crates/codegen/xai-grok-plugin-marketplace/src/lib.rs", "pub fn plugins() {}\n")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "upstream two")
        self.candidate_one = git(self.upstream, "rev-parse", "HEAD")

        write(self.upstream, "LICENSE", "Apache-2.0 fixture, revised\n")
        write(self.upstream, "NOTICE", "notice-v2\n")
        write(self.upstream, "README.md", "upstream revised\n")
        write(self.upstream, "SOURCE_REV", self.source_revs[2] + "\n")
        write(self.upstream, "rust-toolchain.toml", "[toolchain]\nchannel = \"1.91.0\"\n")
        write(self.upstream, "crates/codegen/xai-grok-mcp/src/lib.rs", "pub fn mcp() {}\n")
        write(self.upstream, "crates/codegen/xai-grok-sandbox/src/lib.rs", "pub fn sandbox() {}\n")
        write(self.upstream, "crates/codegen/xai-grok-shell/src/headless/background_task.rs", "pub fn task() {}\n")
        write(self.upstream, "crates/codegen/xai-grok-shell/src/subagent.rs", "pub fn subagent() {}\n")
        write(self.upstream, "crates/codegen/xai-grok-config/src/lib.rs", "pub fn config() {}\n")
        write(self.upstream, "crates/codegen/xai-grok-pager/docs/user-guide/worker.md", "worker docs\n")
        git(self.upstream, "add", ".")
        git(self.upstream, "commit", "-qm", "upstream three")
        self.candidate_two = git(self.upstream, "rev-parse", "HEAD")

        git(root, "clone", "-q", str(self.upstream), str(self.fork))
        git(self.fork, "config", "user.name", "Fixture")
        git(self.fork, "config", "user.email", "fixture@example.invalid")
        git(self.fork, "checkout", "-q", self.old)
        provenance = {
            "schema": 1,
            "distribution": "chip",
            "upstream": {
                "repository": "https://github.com/xai-org/grok-build",
                "public_commit": self.old,
                "public_tree": self.old_tree,
                "source_rev": self.source_revs[0],
            },
        }
        write(self.fork, ".chip/provenance.json", json.dumps(provenance) + "\n")
        write(self.fork, "README.md", "fork policy\n")
        write(self.fork, "NOTICE-CHIP.md", "public fork policy\n")
        git(self.fork, "add", ".")
        git(self.fork, "commit", "-qm", "fork policy")


class UpstreamSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sync = load_module(SYNC_SCRIPT, "chip_upstream_sync")

    def test_offline_dry_run_covers_two_historical_upstream_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = LocalRepositories(Path(temporary))
            accepted_before = git(fixture.fork, "status", "--porcelain=v1")
            candidate_before = git(fixture.upstream, "status", "--porcelain=v1")

            first = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_one)
            second = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_two)

            self.assertEqual(first["candidate"]["public_commit"], fixture.candidate_one)
            self.assertEqual(second["candidate"]["public_commit"], fixture.candidate_two)
            self.assertEqual(first["candidate"]["source_rev"], fixture.source_revs[1])
            self.assertEqual(second["candidate"]["source_rev"], fixture.source_revs[2])
            self.assertEqual(accepted_before, git(fixture.fork, "status", "--porcelain=v1"))
            self.assertEqual(candidate_before, git(fixture.upstream, "status", "--porcelain=v1"))

    def test_report_is_stable_and_covers_required_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = LocalRepositories(Path(temporary))
            first = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_two)
            second = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_two)
            first_json = self.sync.render_json(first)
            second_json = self.sync.render_json(second)
            first_markdown = self.sync.render_markdown(first)
            second_markdown = self.sync.render_markdown(second)

            self.assertEqual(first_json, second_json)
            self.assertEqual(first_markdown, second_markdown)
            self.assertEqual(first["schema"], 1)
            self.assertEqual(first["accepted"]["public_commit"], fixture.old)
            self.assertIn("crates/codegen/xai-grok-mcp", first["changed_crates"])
            self.assertTrue(first["critical_deltas"]["cargo_lock"])
            self.assertTrue(first["critical_deltas"]["rust_toolchain"])
            self.assertTrue(first["critical_deltas"]["licenses"])
            self.assertTrue(first["critical_deltas"]["notices"])
            for area in (
                "worker_docs",
                "headless",
                "subagents",
                "hooks",
                "mcp",
                "plugins",
                "sandbox",
                "background_tasks",
            ):
                self.assertIn(area, first["feature_deltas"])
            self.assertTrue(first["cli_config_relevant_paths"])
            self.assertEqual(first["patch_replay"]["status"], "conflict")
            self.assertEqual(
                first["patch_replay"]["commits"],
                git(fixture.fork, "rev-list", "--reverse", f"{fixture.old}..HEAD").splitlines(),
            )
            self.assertIn("README.md", first["patch_replay"]["conflicting_paths"])
            self.assertEqual(len(first["feature_harvest_checklist"]), 9)
            self.assertNotIn(str(fixture.upstream), first_json)
            self.assertNotIn(str(fixture.fork), first_markdown)

    def test_unchanged_cli_is_silent_and_removes_stale_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = LocalRepositories(Path(temporary))
            output_json = Path(temporary) / "report.json"
            output_markdown = Path(temporary) / "report.md"
            output_json.write_text("stale")
            output_markdown.write_text("stale")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = self.sync.main(
                    [
                        "--accepted-repo",
                        str(fixture.fork),
                        "--candidate-repo",
                        str(fixture.upstream),
                        "--candidate-ref",
                        fixture.old,
                        "--json",
                        str(output_json),
                        "--markdown",
                        str(output_markdown),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertFalse(output_json.exists())
            self.assertFalse(output_markdown.exists())

    def test_patch_replay_reports_clean_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = LocalRepositories(Path(temporary))
            report = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_one)
            self.assertEqual(report["patch_replay"]["status"], "clean")
            self.assertEqual(report["patch_replay"]["conflicting_paths"], [])
            self.assertEqual(report["patch_replay"]["commit_count"], len(report["patch_replay"]["commits"]))
            self.assertGreater(report["patch_replay"]["commit_count"], 0)


class BranchPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sync = load_module(SYNC_SCRIPT, "chip_upstream_sync_branch")
        self.pr = load_module(PR_SCRIPT, "chip_upstream_sync_pr_branch")

    def test_branch_name_and_replayed_patch_ancestry_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRepositories(root)
            report = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_one)
            remote = root / "remote.git"
            git(root, "init", "--bare", "-q", str(remote))

            result = self.pr.prepare_branch(fixture.fork, fixture.upstream, report, str(remote), None)

            branch = f"sync/upstream-{fixture.candidate_one[:12]}"
            self.assertEqual(result["branch"], branch)
            tip = bare_git(remote, "rev-parse", f"refs/heads/{branch}")
            self.assertEqual(result["tip"], tip)
            self.assertEqual(
                len(bare_git(remote, "rev-list", f"{fixture.candidate_one}..{tip}").splitlines()),
                len(result["patches"]),
            )
            self.assertEqual(
                bare_git(remote, "show", f"{tip}:NOTICE-CHIP.md"),
                "public fork policy",
            )
            self.assertEqual(bare_git(remote, "show", f"{tip}:README.md"), "fork policy")

    def test_same_candidate_update_uses_lease_and_stable_tip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRepositories(root)
            report = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_one)
            remote = root / "remote.git"
            git(root, "init", "--bare", "-q", str(remote))
            first = self.pr.prepare_branch(fixture.fork, fixture.upstream, report, str(remote), None)
            second = self.pr.prepare_branch(
                fixture.fork, fixture.upstream, report, str(remote), first["tip"]
            )
            self.assertEqual(first["branch"], second["branch"])
            self.assertEqual(first["tip"], second["tip"])

    def test_stale_remote_lease_rejects_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRepositories(root)
            report = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_one)
            remote = root / "remote.git"
            git(root, "init", "--bare", "-q", str(remote))
            self.pr.prepare_branch(fixture.fork, fixture.upstream, report, str(remote), None)
            with self.assertRaisesRegex(self.pr.PreparationError, "lease"):
                self.pr.prepare_branch(
                    fixture.fork, fixture.upstream, report, str(remote), "f" * 40
                )

    def test_conflict_report_causes_no_push(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRepositories(root)
            report = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_two)
            self.assertEqual(report["patch_replay"]["status"], "conflict")
            report["patch_replay"]["status"] = "clean"
            remote = root / "remote.git"
            git(root, "init", "--bare", "-q", str(remote))
            with self.assertRaisesRegex(self.pr.PreparationError, "replay conflict"):
                self.pr.prepare_branch(fixture.fork, fixture.upstream, report, str(remote), None)
            self.assertEqual(bare_git(remote, "for-each-ref", "--format=%(refname)"), "")

    def test_tampered_patch_order_causes_no_push(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRepositories(root)
            write(fixture.fork, "NOTICE-CHIP.md", "public fork policy v2\n")
            git(fixture.fork, "add", ".")
            git(fixture.fork, "commit", "-qm", "fork policy two")
            report = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_one)
            report["patch_replay"]["commits"].reverse()
            remote = root / "remote.git"
            git(root, "init", "--bare", "-q", str(remote))
            with self.assertRaisesRegex(self.pr.PreparationError, "patch list"):
                self.pr.prepare_branch(fixture.fork, fixture.upstream, report, str(remote), None)
            self.assertEqual(bare_git(remote, "for-each-ref", "--format=%(refname)"), "")

    def test_invalid_branch_inputs_are_rejected(self) -> None:
        for candidate in ("1" * 39, "G" * 40, "1" * 40 + "/main"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(self.pr.PreparationError):
                    self.pr.branch_for_candidate(candidate)

    def test_candidate_without_accepted_ancestry_causes_no_push(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = LocalRepositories(root)
            report = self.sync.build_report(fixture.fork, fixture.upstream, fixture.candidate_one)
            unrelated = root / "unrelated"
            unrelated.mkdir()
            git(unrelated, "init", "-q")
            git(unrelated, "config", "user.name", "Fixture")
            git(unrelated, "config", "user.email", "fixture@example.invalid")
            write(unrelated, "SOURCE_REV", "d" * 40 + "\n")
            git(unrelated, "add", ".")
            git(unrelated, "commit", "-qm", "unrelated")
            report["candidate"]["public_commit"] = git(unrelated, "rev-parse", "HEAD")
            remote = root / "remote.git"
            git(root, "init", "--bare", "-q", str(remote))
            with self.assertRaisesRegex(self.pr.PreparationError, "descendant"):
                self.pr.prepare_branch(fixture.fork, unrelated, report, str(remote), None)
            self.assertEqual(bare_git(remote, "for-each-ref", "--format=%(refname)"), "")


class PullRequestPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pr = load_module(PR_SCRIPT, "chip_upstream_sync_pr")
        self.report = {
            "candidate": {"public_commit": "1" * 40},
            "accepted": {"public_commit": "2" * 40},
        }
        self.markdown = "# Upstream sync report\n"

    def test_existing_candidate_pr_is_stably_updated_without_merge(self) -> None:
        head = "evgyur:sync/upstream-111111111111"
        existing = [
            {
                "number": 9,
                "title": "sync/upstream-111111111111",
                "head": {"label": head},
                "base": {"ref": "main"},
            },
            {
                "number": 12,
                "title": "renamed by reviewer",
                "body": "<!-- chip-controlled-upstream-sync -->",
                "head": {"label": head},
                "base": {"ref": "main"},
            },
            {
                "number": 13,
                "title": "sync/upstream-000000000000",
                "head": {"label": "evgyur:sync/upstream-000000000000"},
                "base": {"ref": "main"},
            },
        ]
        first = self.pr.plan_pull_request(self.report, self.markdown, existing, "main", head)
        second = self.pr.plan_pull_request(self.report, self.markdown, existing, "main", head)
        self.assertEqual(first, second)
        self.assertEqual([item["operation"] for item in first], ["close", "update"])
        self.assertEqual(first[-1]["number"], 9)
        self.assertEqual(first[-1]["title"], "sync/upstream-111111111111")
        self.assertNotIn("merge", [item["operation"] for item in first])
        self.assertNotIn(13, [item.get("number") for item in first])

    def test_missing_sync_pr_creates_exactly_one_from_fork_branch(self) -> None:
        head = "evgyur:sync/upstream-111111111111"
        plan = self.pr.plan_pull_request(self.report, self.markdown, [], "main", head)
        creates = [item for item in plan if item["operation"] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["head"], head)
        self.assertFalse(creates[0]["maintainer_can_modify"])

    def test_pr_planner_rejects_head_that_is_not_exact_candidate_branch(self) -> None:
        with self.assertRaisesRegex(self.pr.PreparationError, "head"):
            self.pr.plan_pull_request(self.report, self.markdown, [], "main", "xai-org:main")

    def test_workflow_has_gated_permissions_schedule_and_no_build_or_merge(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("cron: '17 */6 * * *'", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertNotIn("contents: read", workflow)
        self.assertRegex(workflow, r"uses: actions/checkout@[0-9a-f]{40}")
        self.assertIn("--candidate-repo \"$RUNNER_TEMP/candidate\"", workflow)
        self.assertIn("--fork-owner evgyur", workflow)
        self.assertIn('--repository "$GITHUB_REPOSITORY"', workflow)
        self.assertNotIn("--head xai-org:main", workflow)
        self.assertNotIn("xai-org:main", workflow)
        lowered = workflow.lower()
        self.assertNotIn("cargo build", lowered)
        self.assertNotIn("cargo install", lowered)
        self.assertNotIn("gh pr merge", lowered)
        self.assertNotIn("auto-merge", lowered)
        self.assertNotIn("release", lowered)


if __name__ == "__main__":
    unittest.main()
