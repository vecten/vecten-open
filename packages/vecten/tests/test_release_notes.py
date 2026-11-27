import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/release_notes.py"
CHANGELOG = REPO / "CHANGELOG.md"

SAMPLE = """\
# Changelog

Preamble.

## [Unreleased]

- Not released yet.

## [0.2.0] - 2026-12-01

### Added

- A feature whose line is
  wrapped, see [the guide](docs/guide.md#setup) and
  [the issue](https://github.com/vecten/vecten-open/issues/1).
- A [heading link](#added) and a [root link](/LICENSE).

  A second paragraph of the item,
  wrapped too.

### Fixed

- A fix.

## [0.1.0] - 2026-11-09

The first
release.

[Unreleased]: https://github.com/vecten/vecten-open/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/vecten/vecten-open/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/vecten/vecten-open/releases/tag/v0.1.0
"""


def release_notes():
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def notes(version, changelog=SAMPLE):
    return release_notes().notes(changelog, version)


def test_the_notes_are_the_version_s_section_without_its_heading():
    body = notes("0.2.0").split("\n---\n")[0]
    assert body.startswith("### Added\n")
    assert "## [0.2.0]" not in body
    assert "- A fix." in body
    assert "Not released yet" not in body and "The first" not in body


def test_the_last_section_stops_at_the_link_references():
    body = notes("0.1.0").split("\n---\n")[0]
    assert body.strip() == "The first release."


def test_a_path_in_the_tree_links_to_the_file_at_the_tag():
    text = notes("0.2.0")
    assert "[the guide](https://github.com/vecten/vecten-open/blob/v0.2.0/docs/guide.md#setup)" in (
        text
    )
    assert "[the issue](https://github.com/vecten/vecten-open/issues/1)" in text
    assert "[heading link](#added)" in text
    assert "[root link](/LICENSE)" in text


def test_hard_wrapped_lines_are_joined_and_blocks_kept_apart():
    lines = notes("0.2.0").splitlines()
    assert lines[2].startswith("- A feature whose line is wrapped, see [the guide](")
    assert lines[2].endswith("[the issue](https://github.com/vecten/vecten-open/issues/1).")
    assert lines[3].startswith("- A [heading link]")
    assert "  A second paragraph of the item, wrapped too." in lines


def test_a_line_after_a_closing_fence_is_not_joined_to_it():
    changelog = (
        "## [0.2.0] - 2026-12-01\n\n- Run this:\n\n  ```sh\n  vecten up\n  ```\n"
        "  Then sign in,\n  as before.\n\n### Fixed\n\n- A fix.\n"
    )
    lines = notes("0.2.0", changelog).splitlines()
    assert lines[2:8] == [
        "  ```sh",
        "  vecten up",
        "  ```",
        "  Then sign in, as before.",
        "",
        "### Fixed",
    ]


def test_the_footer_names_the_install_the_images_and_the_compare_link():
    footer = notes("0.2.0").split("\n---\n")[1]
    assert footer.strip().splitlines() == [
        "**Install:** `uv tool install vecten==0.2.0`",
        "",
        "**Images:** `ghcr.io/vecten/vecten-airflow:0.2.0`, `ghcr.io/vecten/vecten-mcp:0.2.0`",
        "",
        "**Full changes:** https://github.com/vecten/vecten-open/compare/v0.1.0...v0.2.0",
    ]


def test_a_first_release_has_no_compare_link():
    assert "Full changes" not in notes("0.1.0")


@pytest.mark.parametrize(
    "version, reason",
    [
        ("0.2.0rc1", "candidate"),
        ("0.3.0", "no section for 0.3.0"),
        ("Unreleased", "not X.Y.Z"),
    ],
)
def test_a_version_without_a_release_section_is_refused(version, reason):
    with pytest.raises(ValueError, match=reason):
        notes(version)


def test_an_empty_section_is_refused():
    with pytest.raises(ValueError, match="empty"):
        notes("0.2.0", "## [0.2.0] - 2026-12-01\n\n## [0.1.0] - 2026-11-09\n\n- x\n")


def test_the_cli_prints_the_notes_or_refuses(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE)
    ok = subprocess.run(
        ["python3", str(SCRIPT), "0.2.0", str(changelog)], capture_output=True, text=True
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout == notes("0.2.0")
    refused = subprocess.run(
        ["python3", str(SCRIPT), "0.2.0rc1", str(changelog)], capture_output=True, text=True
    )
    assert refused.returncode == 1
    assert refused.stdout == ""
    assert refused.stderr.startswith("refused: ")


def test_every_released_version_in_the_changelog_renders():
    changelog = CHANGELOG.read_text()
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
    assert "0.1.0" in versions
    for version in versions:
        text = notes(version, changelog)
        assert f"vecten=={version}" in text
        assert "](docs/" not in text, version


def test_the_images_named_are_the_ones_the_workflow_pushes():
    from vecten.stack.up import PUBLISHED_REGISTRY

    module = release_notes()
    assert module.REGISTRY == PUBLISHED_REGISTRY
    workflow = yaml.safe_load((REPO / ".github/workflows/publish.yml").read_text())
    pushed = [
        step["with"]["tags"].rsplit(":", 1)[0]
        for step in workflow["jobs"]["images"]["steps"]
        if step.get("uses", "").startswith("docker/build-push-action@")
    ]
    assert pushed == [f"{module.REGISTRY}/{image}" for image in module.IMAGES]
