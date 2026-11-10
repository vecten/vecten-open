"""Read reviewed gap fixes from the configured GitHub repository while shipping."""

import json
import re
import sys
from urllib.parse import urlencode

from vecten.github.gh import get_gh
from vecten_shared.osi.evals import load_cases
from vecten_shared.output import ExitCode, Failure


def _read(gh, root, endpoint, *, paginate=False):
    try:
        result = gh.run(["api", endpoint, *(["--paginate", "--slurp"] if paginate else [])], root)
    except OSError:
        raise Failure(ExitCode.PREREQUISITE, "could not run the GitHub CLI `gh`") from None
    if result.returncode:
        raise Failure(ExitCode.TOOL_FAILED, "could not read GitHub gap verification metadata")
    try:
        return json.loads(result.stdout)
    except TypeError, ValueError:
        raise Failure(ExitCode.TOOL_FAILED, "invalid GitHub gap verification metadata") from None


def _pages(gh, root, endpoint):
    try:
        pages = _read(gh, root, endpoint, paginate=True)
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            raise ValueError
        return [row for page in pages for row in page if isinstance(row, dict)]
    except TypeError, ValueError:
        raise Failure(ExitCode.TOOL_FAILED, "invalid GitHub gap verification metadata") from None


def _mentions(value, gap):
    return isinstance(value, str) and bool(
        re.search(r"(?<![A-Za-z0-9-])" + re.escape(gap) + r"(?![A-Za-z0-9-])", value)
    )


def _note(line):
    # stdout carries the command's one result document, so ship notes go to stderr.
    print(line, file=sys.stderr, flush=True)


def feedback_manifest(root, config, commit, lineage, *, gh=None, note=_note):
    manifest = {
        "commit": commit,
        "ancestors": lineage["ancestors"],
        "resolutions": [],
        "unreviewed": [],
    }
    path = root / "evals" / "semantic.yaml"
    if not path.is_file():
        return manifest
    gaps = {}
    for case in load_cases(path):
        if case.gap:
            gaps.setdefault(case.gap, set()).update(case.request.get("metrics", []))
    if not gaps:
        return manifest
    repository = config.deploy.github_repository
    gh = gh or get_gh()
    canonical = _read(gh, root, f"repos/{repository}").get("full_name", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", canonical):
        raise Failure(ExitCode.TOOL_FAILED, "invalid GitHub repository identity")
    repository = canonical
    commits = {commit, *lineage["ancestors"]}
    numbers = set()
    for gap in gaps:
        query = urlencode(
            {"q": f"repo:{repository} is:pr is:merged in:body {json.dumps(gap)}", "per_page": 100}
        )
        pages = _read(gh, root, f"search/issues?{query}", paginate=True)
        for page in pages:
            if page.get("incomplete_results") or page.get("total_count", 0) > 1000:
                raise Failure(ExitCode.TOOL_FAILED, "incomplete GitHub gap search; retry shipping")
            numbers.update(
                item["number"] for item in page.get("items", []) if type(item.get("number")) is int
            )
    pulls = [_read(gh, root, f"repos/{repository}/pulls/{number}") for number in sorted(numbers)]
    unreviewed = {}
    for pull in sorted(pulls, key=lambda p: p.get("merged_at") or "", reverse=True):
        if (
            not pull.get("merged_at")
            or pull.get("merge_commit_sha") not in commits
            or pull.get("base", {}).get("repo", {}).get("full_name", "").casefold()
            != repository.casefold()
        ):
            continue
        matched = [
            gap
            for gap in gaps
            if _mentions(pull.get("body"), gap) and _mentions(pull.get("head", {}).get("ref"), gap)
        ]
        if not matched:
            continue
        number = pull["number"]
        if type(number) is not int or number < 1:
            continue
        author = pull.get("user", {}).get("login")
        reviews = _pages(gh, root, f"repos/{repository}/pulls/{number}/reviews?per_page=100")
        latest = {}
        for review in reviews:
            if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                latest[review.get("user", {}).get("login")] = review
        reviewers = sorted(
            login
            for login, review in latest.items()
            if isinstance(login, str)
            and login != author
            and review.get("state") == "APPROVED"
            and review.get("commit_id") == pull.get("head", {}).get("sha")
            and review.get("submitted_at", "") <= pull["merged_at"]
        )
        if not author or not reviewers:
            for gap in matched:
                unreviewed.setdefault(
                    gap,
                    {
                        "gap": gap,
                        "pull_request": f"https://github.com/{repository}/pull/{number}",
                        "merge_commit": pull["merge_commit_sha"],
                        "author": author,
                        "merged_at": pull["merged_at"],
                    },
                )
            continue
        for gap in matched:
            manifest["resolutions"].append(
                {
                    "gap": gap,
                    "pull_request": f"https://github.com/{repository}/pull/{number}",
                    "merge_commit": pull["merge_commit_sha"],
                    "author": author,
                    "reviewers": reviewers,
                    "merged_at": pull["merged_at"],
                    "metrics": sorted(gaps.pop(gap)),
                }
            )
    # A gap that a reviewed pull request resolves has left `gaps`; an unreviewed merge of it
    # changes nothing.
    for gap, entry in unreviewed.items():
        if gap in gaps and entry["author"]:
            manifest["unreviewed"].append({**entry, "metrics": sorted(gaps[gap])})
            note(
                f"gap {gap}: {entry['pull_request']} merged without an approving review; "
                "it resolves the gap only once an admin confirms it in the panel"
            )
    return manifest
