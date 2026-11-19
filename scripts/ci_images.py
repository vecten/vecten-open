"""Print the image references the versions table names, for CI to pull before uv is set up.

Runs on the runner's own Python, so it reads the table directly and imports nothing of ours.
"""

import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TABLE = REPO / "packages" / "vecten-shared" / "src" / "vecten_shared" / "versions.toml"


def references(names: list[str]) -> list[str]:
    images = tomllib.loads(TABLE.read_text(encoding="utf-8"))["images"]
    images["postgres"] = f"postgres:{images['postgres_major']}"
    return [images[name] for name in names]


if __name__ == "__main__":
    print(" ".join(references(sys.argv[1:])))
