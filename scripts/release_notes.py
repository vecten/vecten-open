#!/usr/bin/env python3
"""Print the GitHub Release notes for a published version, from its changelog section.

Usage: release_notes.py <version> [changelog]. Prints the `## [<version>]` section of
CHANGELOG.md, without its heading, followed by the install line, the image references and the
compare link; refuses a version that is not X.Y.Z or that the changelog holds no entries for.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/vecten/vecten-open"
REGISTRY = "ghcr.io/vecten"
IMAGES = ("vecten-airflow", "vecten-mcp")
# A link target with a scheme, an in-page anchor or a root-relative path resolves the same on a
# Release page as in the repository; any other target is a path in the tree.
ABSOLUTE = re.compile(r"[a-z][a-z0-9+.-]*:|#|/", re.I)
INLINE_LINK = re.compile(r"\]\(([^)\s]+)\)")
# A line opening a block of its own, which the line before it never continues.
BLOCK_START = re.compile(r"\s*([-*+]|\d+\.)\s|#|>|\||```|\[[^\]]+\]:")
# A line whose block ends with it, which the line after it never continues.
WHOLE_LINE = re.compile(r"\s*(#|```|\||\[[^\]]+\]:)")


def section(changelog: str, version: str) -> str:
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)",
        changelog,
        re.M | re.S,
    )
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    body = match.group(1).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md's section for {version} is empty")
    return body


def absolute_links(text: str, version: str) -> str:
    def rewrite(match: re.Match[str]) -> str:
        target = match.group(1)
        if ABSOLUTE.match(target):
            return match.group(0)
        return f"]({REPOSITORY}/blob/v{version}/{target})"

    return INLINE_LINK.sub(rewrite, text)


def unwrap(text: str) -> str:
    """Join hard-wrapped lines: a Release page renders each newline as a line break."""
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        opens_fence = line.lstrip().startswith("```")
        joins = (
            not fenced
            and not opens_fence
            and line.strip()
            and lines
            and lines[-1].strip()
            and not WHOLE_LINE.match(lines[-1])
            and not BLOCK_START.match(line)
        )
        if joins:
            lines[-1] = f"{lines[-1].rstrip()} {line.strip()}"
        else:
            lines.append(line)
        if opens_fence:
            fenced = not fenced
    return "\n".join(lines)


def compare_link(changelog: str, version: str) -> str | None:
    match = re.search(rf"^\[{re.escape(version)}\]: (\S+)$", changelog, re.M)
    if match is None or "/compare/" not in match.group(1):
        return None
    return match.group(1)


def notes(changelog: str, version: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"version {version} is not X.Y.Z; a candidate gets no Release page")
    body = unwrap(absolute_links(section(changelog, version), version))
    images = ", ".join(f"`{REGISTRY}/{image}:{version}`" for image in IMAGES)
    footer = [
        f"**Install:** `uv tool install vecten=={version}`",
        f"**Images:** {images}",
    ]
    link = compare_link(changelog, version)
    if link is not None:
        footer.append(f"**Full changes:** {link}")
    return body + "\n\n---\n\n" + "\n\n".join(footer) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print("usage: release_notes.py <version> [changelog]", file=sys.stderr)
        return 2
    path = Path(argv[1]) if len(argv) == 2 else ROOT / "CHANGELOG.md"
    try:
        text = notes(path.read_text(), argv[0])
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
