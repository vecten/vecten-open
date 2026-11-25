#!/usr/bin/env python3
"""Close the issues a merged pull request declares it fixes once it lands on a release branch.

Usage: close_issues.py <owner/repo> <branch> <commit>... with GH_TOKEN set. GitHub closes them
itself only on a merge into the default branch. A declaration is a line of its own,
`Fixes #12` or `- Closes: #12, resolves #13`, at the start of a line or of a sentence; a
keyword inside a sentence, inline code, a code block, a quote or an HTML comment is a mention,
not a declaration. A number that names no issue is reported and
skipped; any other failure fails the run after every other issue has been tried.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
# After the first number the keyword is optional, as in `Closes #116 and #117`.
NUMBERS = rf"{KEYWORD}#\d+(?:\s*(?:,|\band\b)\s*(?:{KEYWORD})?#\d+|\s*;\s*{KEYWORD}#\d+)*(?!\w)"
# A declaration opens a line, after an optional bullet, or opens a sentence.
DECLARATION = re.compile(rf"(?:^ {{0,3}}(?:[-*+] +)?|(?<=[.!?]) +)({NUMBERS})", re.I)
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
# A quote, or four spaces of indent, which make an indented code block.
NOT_PROSE = re.compile(r" {0,3}>| {4}|\t")
INLINE_CODE = re.compile(r"(`+).*?\1")
COMMENT = re.compile(r"<!--.*?-->", re.S)


def declared(body: str | None) -> list[int]:
    found, fence = set(), None
    for line in COMMENT.sub("", body or "").splitlines():
        marker = FENCE.match(line)
        if marker:
            run = marker.group(1)
            # A fence closes only on the same character, at least as long as the one that opened it.
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = None
            continue
        if fence is not None or NOT_PROSE.match(line):
            continue
        for declaration in DECLARATION.finditer(INLINE_CODE.sub(" ", line)):
            found.update(int(number) for number in re.findall(r"#(\d+)", declaration.group(1)))
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


def main(repo: str, branch: str, commits: list[str]) -> int:
    failed = False
    for commit in commits:
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
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
