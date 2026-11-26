#!/usr/bin/env python3
"""Name what a pushed version tag publishes, or refuse a tag the tree or `main` does not back.

Usage: publish_target.py <tag>. Writes target, index_url, check_url, version and the images'
build arguments (airflow_image, airflow_version, mcp_base_image) to $GITHUB_OUTPUT; on a refusal
it writes nothing and exits non-zero.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "packages/vecten-shared/src/vecten_shared/versions.toml"
# Each target's upload endpoint and the simple index that lists what it already holds.
INDEXES = {
    "candidate": ("https://test.pypi.org/legacy/", "https://test.pypi.org/simple/"),
    "release": ("https://upload.pypi.org/legacy/", "https://pypi.org/simple/"),
}


def target(tag: str, version: str, on_main: bool) -> str:
    if tag != f"v{version}":
        raise ValueError(f"tag {tag} does not name the tree's version {version}")
    if re.fullmatch(r"\d+\.\d+\.\d+rc\d+", version):
        return "candidate"
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"version {version} is neither X.Y.Z nor X.Y.ZrcN")
    if not on_main:
        raise ValueError(f"release tag {tag} is not on main")
    return "release"


def image_args(table: dict) -> dict[str, str]:
    """The versions table's image values, derived as `vecten_shared.versions` derives them."""
    airflow_image = table["images"]["airflow"]
    return {
        "airflow_image": airflow_image,
        "airflow_version": airflow_image.rsplit(":", 1)[1].split("-")[0],
        "mcp_base_image": table["images"]["mcp_base"],
    }


def on_main() -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    # Exit 1 means "not an ancestor"; anything else means git could not answer.
    if result.returncode not in (0, 1):
        raise ValueError(f"git merge-base failed: {result.stderr.strip()}")
    return result.returncode == 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: publish_target.py <tag>", file=sys.stderr)
        return 2
    table = tomllib.loads(VERSIONS.read_text())
    version = table["vecten"]
    images = image_args(table)
    try:
        name = target(argv[0], version, on_main())
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    index_url, check_url = INDEXES[name]
    lines = [
        f"target={name}",
        f"index_url={index_url}",
        f"check_url={check_url}",
        f"version={version}",
        *(f"{name}={value}" for name, value in images.items()),
    ]
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("".join(f"{line}\n" for line in lines))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
