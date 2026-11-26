#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["markdown-it-py==4.2.0"]
# ///
"""Close the issues a merged pull request declares it fixes once it lands on a release branch.

Usage: close_issues.py <owner/repo> <branch> <before> <after> with GH_TOKEN set, for a push that
moved <branch> from <before> to <after>. GitHub closes them itself only on a merge into the
default branch.

A declaration is a closing keyword and an issue number, `Fixes #12` or `Closes: #12, resolves
#13`, that opens a line or a sentence of a paragraph. The body is parsed as CommonMark first, so
a keyword in a quote, a code block, a code span or HTML is a mention, not a declaration.

A number that names no issue is reported and skipped; any other failure fails the run after
every other issue has been tried.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from markdown_it import MarkdownIt

API = "https://api.github.com"
KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
# After the first number the keyword is optional, as in `Closes #116 and #117`.
NUMBERS = rf"{KEYWORD}#\d+(?:\s*(?:,|\band\b)\s*(?:{KEYWORD})?#\d+|\s*;\s*{KEYWORD}#\d+)*(?!\w)"
DECLARATION = re.compile(rf"(?:^\s*|(?<=[.!?]) +)({NUMBERS})", re.I)
# Stands in for a code span or inline HTML: neither a word nor the end of a sentence.
OPAQUE = "\x00"
# Longer than any first-parent run one push lands on a protected release branch.
MAX_LANDED = 100


def prose(inline) -> list[str]:
    """The lines of a paragraph as text, with code spans, inline HTML and links made opaque.

    A link's label says nothing about where it points: `[#123](https://github.com/other/repo/
    issues/123)` names another repository's issue, so a link never declares a fix here.
    """
    parts, linked = [], 0
    for child in inline.children or []:
        if child.type == "link_open":
            linked += 1
            parts.append(OPAQUE)
        elif child.type == "link_close":
            linked -= 1
        elif linked:
            continue
        elif child.type == "text":
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif child.type in ("code_inline", "html_inline"):
            parts.append(OPAQUE)
    return "".join(parts).split("\n")


def declared(body: str | None) -> list[int]:
    found, quoted, paragraph = set(), 0, False
    for token in MarkdownIt("commonmark").parse(body or ""):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        elif token.type == "inline" and paragraph and not quoted:
            for line in prose(token):
                for declaration in DECLARATION.finditer(line):
                    found.update(int(n) for n in re.findall(r"#(\d+)", declaration.group(1)))
        paragraph = token.type == "paragraph_open"
    return sorted(found)


def call(method: str, path: str, payload: dict | None = None):
    request = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def landed(repo: str, before: str, after: str) -> list[str]:
    """The commits the push put on the branch's first-parent line, oldest first.

    A merge commit brings the pull request's own commits along; only the merge itself landed on
    the branch. A new branch landed nothing, and a deleted one has no commits left to read.
    """
    if set(before) == {"0"} or set(after) == {"0"}:
        return []
    commits, sha = [], after
    while sha != before:
        if len(commits) == MAX_LANDED:
            raise RuntimeError(f"{before} is not within {MAX_LANDED} first parents of {after}")
        commits.append(sha)
        parents = call("GET", f"/repos/{repo}/commits/{sha}")["parents"]
        if not parents:
            raise RuntimeError(f"{before} is not a first parent of {after}")
        sha = parents[0]["sha"]
    return commits[::-1]


def merged_pulls(repo: str, branch: str, commit: str, attempts: int = 3, pause: float = 5):
    # The list also holds open pull requests that carry the commit, so only the one this commit
    # merged counts. A push that bypassed review has none; the retries cover a lagging index.
    for attempt in range(attempts):
        pulls = [
            pull
            for pull in call("GET", f"/repos/{repo}/commits/{commit}/pulls")
            if pull["merged_at"]
            and pull["base"]["ref"] == branch
            and pull["merge_commit_sha"] == commit
        ]
        if pulls or attempt == attempts - 1:
            return pulls
        time.sleep(pause)
    return []


def close(repo: str, issue: int, pull: int, branch: str) -> None:
    try:
        found = call("GET", f"/repos/{repo}/issues/{issue}")
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 410):
            raise
        exc.close()
        print(f"::warning::#{pull} declares it fixes #{issue}, which is not an issue here")
        return
    if "pull_request" in found or found["state"] != "open":
        return
    call(
        "POST",
        f"/repos/{repo}/issues/{issue}/comments",
        {"body": f"Fixed by #{pull}, merged into {branch}."},
    )
    call("PATCH", f"/repos/{repo}/issues/{issue}", {"state": "closed", "state_reason": "completed"})
    print(f"closed #{issue}, fixed by #{pull}")


def main(repo: str, branch: str, before: str, after: str) -> int:
    failed = False
    for commit in landed(repo, before, after):
        for pull in merged_pulls(repo, branch, commit):
            for issue in declared(pull["body"]):
                try:
                    close(repo, issue, pull["number"], branch)
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    if isinstance(exc, urllib.error.HTTPError):
                        exc.close()
                    print(f"::error::closing #{issue} for #{pull['number']} failed: {exc}")
                    failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
