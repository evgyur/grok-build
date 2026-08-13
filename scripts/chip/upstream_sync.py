#!/usr/bin/env python3
"""Deterministic, source-only upstream comparison and patch replay dry-run."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

HEX40 = re.compile(r"^[0-9a-f]{40}$")
FEATURE_PATTERNS = {
    "worker_docs": (r"docs?/user-guide|worker.*\.(md|txt)$|(^|/)README",),
    "headless": (r"headless|stdio|non.?interactive",),
    "subagents": (r"subagent",),
    "hooks": (r"(^|[-_/])hooks?([-_/\.]|$)",),
    "mcp": (r"(^|[-_/])mcp([-_/\.]|$)",),
    "plugins": (r"plugin",),
    "sandbox": (r"sandbox",),
    "background_tasks": (r"background[-_ /]?tasks?|long[-_ /]?running",),
}
CLI_CONFIG_PATTERNS = (
    r"(^|[-_/])(cli|config|args?|flags?)([-_/\.]|$)",
    r"xai-grok-(pager-bin|shell)/",
    r"docs?/user-guide/",
    r"(^|/)Cargo\.toml$",
)
LICENSE_PATH = re.compile(r"(^|/)(license|licence|copying)([-_.]|$)", re.IGNORECASE)
NOTICE_PATH = re.compile(r"(^|/)(notice|third[-_]party)", re.IGNORECASE)


class SyncError(RuntimeError):
    pass


def git(
    repository: Path,
    *args: str,
    check: bool = True,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=False,
        capture_output=True,
        text=True,
        env=None if environment is None else dict(environment),
    )
    if check and result.returncode != 0:
        raise SyncError(f"git operation failed: {args[0] if args else 'unknown'}")
    return result


def git_text(repository: Path, *args: str) -> str:
    return git(repository, *args).stdout.strip()


def require_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not HEX40.fullmatch(value):
        raise SyncError(f"invalid {label}")
    return value


def read_accepted_identity(repository: Path) -> dict[str, str]:
    try:
        manifest = json.loads((repository / ".chip" / "provenance.json").read_text(encoding="utf-8"))
        upstream = manifest["upstream"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SyncError("invalid accepted provenance") from exc
    if not isinstance(upstream, dict):
        raise SyncError("invalid accepted provenance")
    identity = {
        "public_commit": require_identity(upstream.get("public_commit"), "accepted public_commit"),
        "public_tree": require_identity(upstream.get("public_tree"), "accepted public_tree"),
        "source_rev": require_identity(upstream.get("source_rev"), "accepted source_rev"),
    }
    actual_tree = git_text(repository, "rev-parse", f"{identity['public_commit']}^{{tree}}")
    if actual_tree != identity["public_tree"]:
        raise SyncError("accepted public_tree does not match public_commit")
    actual_source_rev = git_text(repository, "show", f"{identity['public_commit']}:SOURCE_REV")
    if actual_source_rev != identity["source_rev"]:
        raise SyncError("accepted SOURCE_REV does not match provenance")
    return identity


def read_candidate_identity(repository: Path, reference: str) -> dict[str, str]:
    commit = git_text(repository, "rev-parse", "--verify", f"{reference}^{{commit}}")
    commit = require_identity(commit, "candidate public_commit")
    tree = require_identity(git_text(repository, "rev-parse", f"{commit}^{{tree}}"), "candidate public_tree")
    source_rev = require_identity(git_text(repository, "show", f"{commit}:SOURCE_REV"), "candidate SOURCE_REV")
    return {"public_commit": commit, "public_tree": tree, "source_rev": source_rev}


def changed_files(repository: Path, old: str, new: str) -> list[dict[str, str]]:
    output = git_text(repository, "diff", "--name-status", "--find-renames", old, new)
    records: list[dict[str, str]] = []
    for line in output.splitlines():
        fields = line.split("\t")
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) == 3:
            records.append({"status": status, "old_path": fields[1], "path": fields[2]})
        elif len(fields) == 2:
            records.append({"status": status, "path": fields[1]})
        else:
            raise SyncError("unexpected git diff record")
    return sorted(records, key=lambda record: (record["path"], record["status"], record.get("old_path", "")))


def crate_for_path(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "crates":
        return "/".join(parts[:3])
    if len(parts) >= 3 and parts[:2] == ["prod", "mc"]:
        return "/".join(parts[:3])
    if len(parts) >= 2 and parts[0] == "third_party":
        return "/".join(parts[:2])
    return None


def matching_paths(paths: list[str], patterns: tuple[str, ...], require_all: bool = False) -> list[str]:
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    predicate = all if require_all else any
    return [path for path in paths if predicate(pattern.search(path) is not None for pattern in compiled)]


def patch_replay(accepted_repository: Path, candidate_repository: Path, accepted: str, candidate: str) -> dict[str, object]:
    ancestor = git(accepted_repository, "merge-base", "--is-ancestor", accepted, "HEAD", check=False)
    if ancestor.returncode != 0:
        raise SyncError("accepted public_commit is not an ancestor of the fork")
    commits_text = git_text(accepted_repository, "rev-list", "--reverse", f"{accepted}..HEAD")
    commits = [line for line in commits_text.splitlines() if line]
    for commit in commits:
        require_identity(commit, "accepted patch commit")
    if not commits:
        return {
            "status": "no_patches",
            "commit_count": 0,
            "commits": [],
            "conflicting_paths": [],
        }

    with tempfile.TemporaryDirectory(prefix="chip-upstream-replay-") as temporary:
        replay = Path(temporary) / "replay"
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(accepted_repository), str(replay)],
            check=False,
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            raise SyncError("could not create independent patch replay repository")
        git(replay, "remote", "add", "candidate", str(candidate_repository))
        git(replay, "fetch", "--quiet", "--no-tags", "candidate", candidate)
        git(replay, "checkout", "--quiet", "--detach", candidate)
        for commit in commits:
            result = git(
                replay,
                "-c",
                "user.name=Chip upstream sync",
                "-c",
                "user.email=sync@example.invalid",
                "cherry-pick",
                "--no-gpg-sign",
                commit,
                check=False,
            )
            if result.returncode != 0:
                conflicts = sorted(
                    path
                    for path in git_text(replay, "diff", "--name-only", "--diff-filter=U").splitlines()
                    if path
                )
                git(replay, "cherry-pick", "--abort", check=False)
                return {
                    "status": "conflict",
                    "commit_count": len(commits),
                    "commits": commits,
                    "conflicting_paths": conflicts,
                }
    return {
        "status": "clean",
        "commit_count": len(commits),
        "commits": commits,
        "conflicting_paths": [],
    }


def checklist(feature_deltas: dict[str, list[str]], cli_paths: list[str]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for area in FEATURE_PATTERNS:
        paths = feature_deltas[area]
        entries.append(
            {
                "area": area,
                "changed": bool(paths),
                "paths": paths,
                "review": "harvest or supersede bounded fork patches" if paths else "confirm no relevant upstream delta",
            }
        )
    entries.append(
        {
            "area": "cli_config",
            "changed": bool(cli_paths),
            "paths": cli_paths,
            "review": "review worker-facing compatibility" if cli_paths else "confirm no worker-facing compatibility delta",
        }
    )
    return entries


def build_report(accepted_repository: Path, candidate_repository: Path, candidate_ref: str) -> dict[str, object]:
    accepted_repository = accepted_repository.resolve()
    candidate_repository = candidate_repository.resolve()
    accepted = read_accepted_identity(accepted_repository)
    candidate = read_candidate_identity(candidate_repository, candidate_ref)
    ancestry = git(
        candidate_repository,
        "merge-base",
        "--is-ancestor",
        accepted["public_commit"],
        candidate["public_commit"],
        check=False,
    )
    if ancestry.returncode != 0:
        raise SyncError("candidate is not a descendant of accepted upstream")

    files = changed_files(candidate_repository, accepted["public_commit"], candidate["public_commit"])
    paths = sorted({record["path"] for record in files} | {record["old_path"] for record in files if "old_path" in record})
    crates = sorted({crate for path in paths if (crate := crate_for_path(path)) is not None})
    critical = {
        "cargo_lock": [path for path in paths if path == "Cargo.lock" or path.endswith("/Cargo.lock")],
        "rust_toolchain": [path for path in paths if Path(path).name.startswith("rust-toolchain")],
        "licenses": [path for path in paths if LICENSE_PATH.search(path)],
        "notices": [path for path in paths if NOTICE_PATH.search(path)],
    }
    feature_deltas = {area: matching_paths(paths, patterns) for area, patterns in FEATURE_PATTERNS.items()}
    cli_paths = matching_paths(paths, CLI_CONFIG_PATTERNS)
    replay = patch_replay(
        accepted_repository,
        candidate_repository,
        accepted["public_commit"],
        candidate["public_commit"],
    )
    return {
        "schema": 1,
        "status": "unchanged" if accepted["public_commit"] == candidate["public_commit"] else "review_required",
        "accepted": accepted,
        "candidate": candidate,
        "changed_crates": crates,
        "changed_files": files,
        "critical_deltas": critical,
        "feature_deltas": feature_deltas,
        "cli_config_relevant_paths": cli_paths,
        "patch_replay": replay,
        "feature_harvest_checklist": checklist(feature_deltas, cli_paths),
        "controls": {
            "build_or_activation_performed": False,
            "auto_merge_allowed": False,
            "candidate_or_accepted_repository_modified": False,
        },
    }


def render_json(report: dict[str, object]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def markdown_paths(paths: list[str]) -> str:
    return "<br>".join(f"`{path}`" for path in paths) if paths else "None"


def render_markdown(report: dict[str, object]) -> str:
    accepted = report["accepted"]
    candidate = report["candidate"]
    critical = report["critical_deltas"]
    features = report["feature_deltas"]
    replay = report["patch_replay"]
    assert isinstance(accepted, dict) and isinstance(candidate, dict)
    assert isinstance(critical, dict) and isinstance(features, dict) and isinstance(replay, dict)
    lines = [
        "# Controlled upstream sync report",
        "",
        "This deterministic report compares the accepted public source identity with the candidate. It does not build, activate, merge, or release a binary.",
        "",
        "## Source identities",
        "",
        "| Identity | Accepted | Candidate |",
        "|---|---|---|",
    ]
    for key in ("public_commit", "public_tree", "source_rev"):
        lines.append(f"| `{key}` | `{accepted[key]}` | `{candidate[key]}` |")
    changed_files_value = report["changed_files"]
    changed_crates_value = report["changed_crates"]
    assert isinstance(changed_files_value, list) and isinstance(changed_crates_value, list)
    lines.extend(
        [
            "",
            "## Change summary",
            "",
            f"- Changed crates: **{len(changed_crates_value)}**",
            f"- Changed files: **{len(changed_files_value)}**",
            f"- Patch replay: **{replay['status']}** across **{replay['commit_count']}** fork commits",
            f"- Patch conflicts: {markdown_paths(replay['conflicting_paths'])}",
            "",
            "### Changed crates",
            "",
            markdown_paths(changed_crates_value),
            "",
            "### Changed files",
            "",
            "| Status | Path |",
            "|---|---|",
        ]
    )
    if changed_files_value:
        for record in changed_files_value:
            source = f" (from `{record['old_path']}`)" if "old_path" in record else ""
            lines.append(f"| `{record['status']}` | `{record['path']}`{source} |")
    else:
        lines.append("| — | None |")
    lines.extend(["", "## Critical source deltas", ""])
    for label, paths in critical.items():
        lines.append(f"- **{label}**: {markdown_paths(paths)}")
    lines.extend(["", "## Worker and runtime deltas", "", "| Area | Relevant paths |", "|---|---|"])
    for area, paths in features.items():
        lines.append(f"| `{area}` | {markdown_paths(paths)} |")
    cli_paths = report["cli_config_relevant_paths"]
    assert isinstance(cli_paths, list)
    lines.extend(
        [
            "",
            "### CLI and configuration relevant paths",
            "",
            markdown_paths(cli_paths),
            "",
            "## Feature-harvest checklist",
            "",
            "Reviewers must complete each item before accepting a candidate.",
            "",
        ]
    )
    checklist_value = report["feature_harvest_checklist"]
    assert isinstance(checklist_value, list)
    for entry in checklist_value:
        marker = " "
        delta = "changed" if entry["changed"] else "no detected delta"
        lines.append(f"- [{marker}] **{entry['area']}** — {delta}; {entry['review']}.")
    lines.extend(
        [
            "",
            "## Safety controls",
            "",
            "- [x] Comparison and patch replay were source-only dry-runs in temporary/local repositories.",
            "- [x] No binary was built, installed, activated, released, or auto-merged.",
            "- [ ] Human review and all separately gated validation are still required.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def parse_args(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted-repo", type=Path, default=Path("."))
    parser.add_argument("--candidate-repo", type=Path, required=True)
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    report = build_report(args.accepted_repo, args.candidate_repo, args.candidate_ref)
    if report["status"] == "unchanged":
        args.json.unlink(missing_ok=True)
        args.markdown.unlink(missing_ok=True)
        return 0
    write_report(args.json, render_json(report))
    write_report(args.markdown, render_markdown(report))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, SyncError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, sort_keys=True))
        raise SystemExit(1)
