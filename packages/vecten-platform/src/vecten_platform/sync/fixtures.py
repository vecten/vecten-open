"""Where a connector's fixtures live; stdlib only so the CLI can ask without importing dlt."""

from __future__ import annotations

from pathlib import Path


def fixture_files(fixtures_dir: Path) -> list[Path]:
    if not fixtures_dir.is_dir():
        return []
    return sorted(fixtures_dir.glob("*.jsonl"))


def input_sample_files(fixtures_dir: Path) -> list[Path]:
    """Recorded upstream inputs, which replay checks even when the connector kept no rows."""
    return sorted((fixtures_dir / "inputs").glob("*.json"))


def check_fixtures(fixtures_dir: Path, groups: dict[str, list[str]]) -> dict[str, int]:
    """Check the declared object names and one JSON object per nonblank line, entirely offline."""
    import json

    from vecten_shared.output import ExitCode, Failure

    files = fixture_files(fixtures_dir)
    if not files:
        raise Failure(ExitCode.CONFIG, f"no JSON Lines fixtures under {fixtures_dir}")
    names = {path.stem for path in files}
    declared = {name for objects in groups.values() for name in objects}
    missing = declared - names
    if missing:
        raise Failure(
            ExitCode.CONFIG, f"{fixtures_dir}: missing fixtures for {', '.join(sorted(missing))}"
        )
    # An empty group discovers resources at runtime, so other filenames may be legitimate.
    unknown = names - declared
    if groups and all(groups.values()) and unknown:
        raise Failure(
            ExitCode.CONFIG, f"{fixtures_dir}: undeclared objects {', '.join(sorted(unknown))}"
        )
    counts = {}
    for path in files:
        count = 0
        with path.open("rb") as lines:
            for number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line.decode("utf-8"))
                except ValueError:
                    raise Failure(ExitCode.CONFIG, f"{path}:{number}: invalid JSON") from None
                if not isinstance(row, dict):
                    raise Failure(ExitCode.CONFIG, f"{path}:{number}: expected a JSON object")
                count += 1
        if not count:
            raise Failure(ExitCode.CONFIG, f"{path}: expected at least one JSON object")
        counts[path.stem] = count
    return counts
