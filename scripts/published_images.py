#!/usr/bin/env python3
"""Decide whether a version's base images still need building, so no published tag is pushed over.

Usage: published_images.py <registry> <version>. Writes build=true to $GITHUB_OUTPUT when
neither image exists and build=false when both do; exits non-zero, writing nothing, when only
one exists or the registry cannot answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable

NAMES = ("vecten-airflow", "vecten-mcp")

Inspect = Callable[[str], tuple[int, str]]


def refs(registry: str, version: str) -> list[str]:
    return [f"{registry.rstrip('/')}/{name}:{version}" for name in NAMES]


def missing(ref: str, returncode: int, stderr: str) -> bool:
    """Whether the registry answered that `ref` does not exist; raises on any other failure."""
    if returncode == 0:
        return False
    # buildx reports a tag the registry does not hold as "<ref>: not found", the registry's
    # error code as "manifest unknown"; any other failure leaves the question open.
    if f"{ref}: not found" in stderr or "manifest unknown" in stderr.lower():
        return True
    raise ValueError(f"inspecting {ref} failed: {stderr.strip()[-2000:]}")


def needs_build(registry: str, version: str, inspect: Inspect) -> bool:
    found = {ref: not missing(ref, *inspect(ref)) for ref in refs(registry, version)}
    if all(found.values()):
        return False
    if not any(found.values()):
        return True
    present = [ref for ref, exists in found.items() if exists]
    absent = [ref for ref, exists in found.items() if not exists]
    raise ValueError(
        f"{', '.join(present)} exists but {', '.join(absent)} does not; nothing is pushed, "
        "and a maintainer decides how to complete the version"
    )


def docker_inspect(ref: str) -> tuple[int, str]:
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref], capture_output=True, text=True
    )
    return result.returncode, result.stderr


def main(argv: list[str], inspect: Inspect = docker_inspect) -> int:
    if len(argv) != 2:
        print("usage: published_images.py <registry> <version>", file=sys.stderr)
        return 2
    registry, version = argv
    try:
        build = needs_build(registry, version, inspect)
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    if build:
        print(f"no image exists for {version}; building and pushing both")
    else:
        print(f"both images exist for {version}; skipping the build and checking them only")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"build={'true' if build else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
