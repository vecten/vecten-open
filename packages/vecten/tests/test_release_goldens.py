"""A project without analytics apps renders exactly what release 0.1.1 rendered."""

from pathlib import Path

import pytest

from .release_render import golden_name, render

GOLDENS = Path(__file__).parent / "fixtures" / "release_goldens"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    return render(tmp_path_factory.mktemp("golden"))


def test_every_golden_is_rendered(rendered):
    assert {golden_name(n) for n in rendered} == {p.name for p in GOLDENS.iterdir()}


@pytest.mark.parametrize("golden", sorted(p.name for p in GOLDENS.iterdir()))
def test_a_project_without_apps_renders_the_release_bytes(rendered, golden):
    by_golden = {golden_name(n): text for n, text in rendered.items()}
    assert by_golden[golden] == (GOLDENS / golden).read_text()
