import io
import json
import zipfile
from importlib.metadata import Distribution, EntryPoint

import pytest

from vecten_shared.consumer_skills import package_skill
from vecten_shared.output import Failure


def test_cli_packages_only_declared_files_from_a_listed_extension(invoke, tmp_path, monkeypatch):
    wheel = tmp_path / "example-1.0.dist-info"
    wheel.mkdir()
    (wheel / "METADATA").write_text("Name: example\nVersion: 1.0\n")
    skill = tmp_path / "content"
    skill.mkdir()
    (skill / "SKILL.md").write_text("Review the monthly orders.")
    (skill / "notes.md").write_text("Private author notes")
    (skill / "guide.md").write_text("Reference for this skill")
    (tmp_path / "order_example.py").write_text(
        "from pathlib import Path\n"
        "report = {'directory': Path(__file__).parent / 'content', 'files': ['guide.md']}\n"
    )
    point = EntryPoint(name="orders", value="order_example:report", group="vecten.consumer_skills")
    monkeypatch.setattr(
        "vecten_shared.extensions.metadata.entry_points",
        lambda *, group: [point._for(Distribution.at(wheel))],
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vecten.toml").write_text(
        'users=1\nextensions=["example==1.0"]\n[project]\nname="demo"\nscaffold_version="0.1.1"\n'
    )
    result = invoke("extension", "pack-skill", "orders", "--output", "orders.zip", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["path"] == "orders.zip"
    original = (tmp_path / "orders.zip").read_bytes()
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        assert archive.namelist() == ["orders/SKILL.md", "orders/guide.md"]
        assert archive.read("orders/SKILL.md") == b"Review the monthly orders."
    assert invoke("extension", "pack-skill", "orders", "--output", "orders.zip").exit_code == 4
    assert (tmp_path / "orders.zip").read_bytes() == original
    assert invoke("extension", "pack-skill", "orders", "--output", "again.zip").exit_code == 0
    assert (tmp_path / "again.zip").read_bytes() == original


@pytest.mark.parametrize(
    "member",
    ["../outside", "/outside", "links/file.md", "file-link", ".env", "missing", "SKILL.md"],
)
def test_package_rejects_invalid_or_duplicate_members(tmp_path, member):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("A skill")
    (root / "links").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "file.md").write_text("outside")
    (root / "file-link").symlink_to(tmp_path / "file.md")
    with pytest.raises(Failure, match="consumer skill"):
        package_skill("orders", {"directory": root, "files": [member]})
