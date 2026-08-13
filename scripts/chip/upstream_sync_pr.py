#!/usr/bin/env python3
"""Create or update the single controlled upstream sync pull request."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

HEX40 = re.compile(r"^[0-9a-f]{40}$")
SYNC_BRANCH = re.compile(r"^sync/upstream-[0-9a-f]{12}$")
MARKER = "<!-- chip-controlled-upstream-sync -->"


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PreparationError(RuntimeError):
    pass


def require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not HEX40.fullmatch(value):
        raise PreparationError(f"invalid {label}")
    return value


def branch_for_candidate(candidate: str) -> str:
    candidate = require_sha(candidate, "candidate public_commit")
    branch = f"sync/upstream-{candidate[:12]}"
    if not SYNC_BRANCH.fullmatch(branch):
        raise PreparationError("invalid sync branch")
    return branch


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
        raise PreparationError(f"git operation failed: {args[0] if args else 'unknown'}")
    return result


def git_text(repository: Path, *args: str) -> str:
    return git(repository, *args).stdout.strip()


def patch_id(repository: Path, old: str, new: str) -> str:
    difference = git(repository, "diff", "--binary", old, new).stdout
    result = subprocess.run(
        ["git", "patch-id", "--stable"],
        input=difference,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise PreparationError("could not verify replayed patch content")
    return result.stdout.split()[0]


def validate_report(report: dict[str, Any]) -> tuple[str, str, list[str]]:
    candidate = require_sha(report.get("candidate", {}).get("public_commit"), "candidate public_commit")
    accepted = require_sha(report.get("accepted", {}).get("public_commit"), "accepted public_commit")
    replay = report.get("patch_replay")
    if not isinstance(replay, dict) or replay.get("status") != "clean":
        raise PreparationError("patch replay is not clean")
    raw_patches = replay.get("commits")
    if not isinstance(raw_patches, list):
        raise PreparationError("invalid accepted patch list")
    patches = [require_sha(commit, "accepted patch commit") for commit in raw_patches]
    if len(set(patches)) != len(patches) or replay.get("commit_count") != len(patches):
        raise PreparationError("invalid accepted patch list")
    return accepted, candidate, patches


def prepare_branch(
    accepted_repository: Path,
    candidate_repository: Path,
    report: dict[str, Any],
    remote_url: str,
    remote_old_sha: str | None,
    token: str | None = None,
) -> dict[str, Any]:
    accepted, candidate, patches = validate_report(report)
    branch = branch_for_candidate(candidate)
    if remote_old_sha is not None:
        require_sha(remote_old_sha, "remote branch old SHA")

    if git(candidate_repository, "merge-base", "--is-ancestor", accepted, candidate, check=False).returncode != 0:
        raise PreparationError("candidate is not a descendant of accepted upstream")
    expected_patches = git_text(accepted_repository, "rev-list", "--reverse", f"{accepted}..HEAD").splitlines()
    if patches != expected_patches:
        raise PreparationError("accepted patch list does not match fork history")

    with tempfile.TemporaryDirectory(prefix="chip-upstream-publish-") as temporary:
        prepared = Path(temporary) / "prepared"
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(accepted_repository), str(prepared)],
            check=False,
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            raise PreparationError("could not create independent branch repository")
        git(prepared, "remote", "add", "candidate", str(candidate_repository))
        git(prepared, "fetch", "--quiet", "--no-tags", "candidate", candidate)
        git(prepared, "checkout", "--quiet", "--detach", candidate)
        for commit in patches:
            environment = os.environ.copy()
            environment["GIT_COMMITTER_DATE"] = git_text(
                accepted_repository, "show", "-s", "--format=%cI", commit
            )
            result = git(
                prepared,
                "-c",
                "user.name=Chip upstream sync",
                "-c",
                "user.email=sync@example.invalid",
                "cherry-pick",
                "--no-gpg-sign",
                commit,
                check=False,
                environment=environment,
            )
            if result.returncode != 0:
                conflicts = sorted(
                    path
                    for path in git_text(prepared, "diff", "--name-only", "--diff-filter=U").splitlines()
                    if path
                )
                raise PreparationError(
                    "accepted patch replay conflict" + (f": {', '.join(conflicts)}" if conflicts else "")
                )
        tip = require_sha(git_text(prepared, "rev-parse", "HEAD"), "prepared branch tip")
        if git(prepared, "merge-base", "--is-ancestor", candidate, tip, check=False).returncode != 0:
            raise PreparationError("prepared branch does not descend from candidate")
        if len(git_text(prepared, "rev-list", f"{candidate}..{tip}").splitlines()) != len(patches):
            raise PreparationError("prepared branch patch count mismatch")
        if patches and patch_id(prepared, candidate, tip) != patch_id(
            accepted_repository, accepted, "HEAD"
        ):
            raise PreparationError("prepared branch content does not match accepted patches")

        git(prepared, "remote", "add", "publish", remote_url)
        fetched = git(prepared, "fetch", "--quiet", "--no-tags", "publish", branch, check=False)
        observed_result = git(prepared, "rev-parse", "--verify", "FETCH_HEAD", check=False)
        observed = observed_result.stdout.strip() if fetched.returncode == 0 else None
        observed = observed if observed is not None and HEX40.fullmatch(observed) else None
        if observed != remote_old_sha:
            raise PreparationError("remote branch does not match expected old SHA lease")
        lease = f"refs/heads/{branch}:{remote_old_sha or ''}"
        environment = os.environ.copy()
        if token is not None:
            askpass = Path(temporary) / "askpass.py"
            askpass.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "print('x-access-token' if 'username' in sys.argv[1].lower() else os.environ['CHIP_GITHUB_TOKEN'])\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            environment.update(
                {
                    "CHIP_GITHUB_TOKEN": token,
                    "GIT_ASKPASS": str(askpass),
                    "GIT_TERMINAL_PROMPT": "0",
                }
            )
        result = git(
            prepared,
            "push",
            "--porcelain",
            "--no-follow-tags",
            f"--force-with-lease={lease}",
            "publish",
            f"{tip}:refs/heads/{branch}",
            check=False,
            environment=environment,
        )
        if result.returncode != 0:
            raise PreparationError("branch push rejected by expected old SHA lease")
    return {"branch": branch, "tip": tip, "previous_tip": remote_old_sha, "patches": patches}


def plan_pull_request(
    report: dict[str, Any],
    markdown: str,
    open_pull_requests: list[dict[str, Any]],
    base: str,
    head: str,
) -> list[dict[str, Any]]:
    candidate = require_sha(report.get("candidate", {}).get("public_commit"), "candidate public_commit")
    accepted = require_sha(report.get("accepted", {}).get("public_commit"), "accepted public_commit")
    title = branch_for_candidate(candidate)
    if head != f"evgyur:{title}":
        raise PreparationError("pull request head does not match candidate branch")
    body = (
        f"{MARKER}\n\n{markdown.rstrip()}\n\n"
        "---\n"
        f"Accepted upstream: `{accepted}`  \n"
        f"Candidate upstream: `{candidate}`  \n\n"
        "This pull request is review-only. It must never be auto-merged and does not build or activate a binary.\n"
    )
    sync_prs = sorted(
        (
            pull
            for pull in open_pull_requests
            if (isinstance(pull.get("title"), str) and SYNC_BRANCH.fullmatch(pull["title"]))
            or (isinstance(pull.get("body"), str) and MARKER in pull["body"])
        ),
        key=lambda pull: int(pull["number"]),
    )
    matching_head = [pull for pull in sync_prs if pull.get("head", {}).get("label") == head]
    keeper = matching_head[0] if matching_head else None
    operations: list[dict[str, Any]] = []
    for duplicate in sync_prs:
        if duplicate is not keeper:
            operations.append({"operation": "close", "number": int(duplicate["number"])})
    if keeper is not None:
        operations.append(
            {
                "operation": "update",
                "number": int(keeper["number"]),
                "title": title,
                "body": body,
                "base": base,
            }
        )
    else:
        operations.append(
            {
                "operation": "create",
                "title": title,
                "body": body,
                "base": base,
                "head": head,
                "maintainer_can_modify": False,
            }
        )
    return operations


class GitHubApi:
    def __init__(self, repository: str, token: str) -> None:
        if not re.fullmatch(r"[^/]+/[^/]+", repository):
            raise ApiError("invalid GitHub repository")
        self.base_url = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "chip-controlled-upstream-sync",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=data,
            headers=self.headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ApiError(
                f"GitHub API {method} {endpoint} failed with HTTP {exc.code}", exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"GitHub API {method} {endpoint} failed") from exc
        return json.loads(body) if body else None


def read_remote_branch_sha(api: GitHubApi, branch: str) -> str | None:
    if not SYNC_BRANCH.fullmatch(branch):
        raise PreparationError("invalid sync branch")
    encoded = urllib.parse.quote(f"heads/{branch}", safe="")
    try:
        response = api.request("GET", f"/git/ref/{encoded}")
    except ApiError as error:
        if error.status_code == 404:
            return None
        raise
    try:
        return require_sha(response["object"]["sha"], "remote branch old SHA")
    except (KeyError, TypeError) as error:
        raise ApiError("invalid GitHub branch response") from error


def list_open_pull_requests(api: GitHubApi, base: str) -> list[dict[str, Any]]:
    pulls: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {"state": "open", "base": base, "per_page": "100", "page": str(page)}
        )
        response = api.request("GET", f"/pulls?{query}")
        if not isinstance(response, list):
            raise ApiError("invalid GitHub pull request response")
        pulls.extend(response)
        if len(response) < 100:
            return pulls
        page += 1


def apply_plan(api: GitHubApi, plan: list[dict[str, Any]]) -> None:
    for operation in plan:
        kind = operation["operation"]
        number = operation.get("number")
        payload = {key: value for key, value in operation.items() if key not in {"operation", "number"}}
        if kind == "create":
            api.request("POST", "/pulls", payload)
        elif kind == "update":
            api.request("PATCH", f"/pulls/{number}", payload)
        elif kind == "close":
            api.request("PATCH", f"/pulls/{number}", {"state": "closed"})
        else:
            raise ApiError("unsupported pull request operation")


def parse_args(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--accepted-repo", type=Path, default=Path("."))
    parser.add_argument("--candidate-repo", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--base", default="main")
    parser.add_argument("--fork-owner", default="evgyur")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ApiError("GITHUB_TOKEN is required")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    markdown = args.markdown.read_text(encoding="utf-8")
    _, candidate, _ = validate_report(report)
    branch = branch_for_candidate(candidate)
    if args.fork_owner != "evgyur" or args.repository != "evgyur/grok-build":
        raise PreparationError("unexpected fork repository")
    api = GitHubApi(args.repository, token)
    remote_old_sha = read_remote_branch_sha(api, branch)
    remote_url = f"https://github.com/{args.repository}.git"
    prepare_branch(
        args.accepted_repo,
        args.candidate_repo,
        report,
        remote_url,
        remote_old_sha,
        token,
    )
    head = f"{args.fork_owner}:{branch}"
    open_pull_requests = list_open_pull_requests(api, args.base)
    plan = plan_pull_request(report, markdown, open_pull_requests, args.base, head)
    apply_plan(api, plan)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiError, PreparationError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, sort_keys=True))
        raise SystemExit(1)
