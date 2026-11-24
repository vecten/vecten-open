import io
import json
import os
import subprocess
import sys
import zipfile
from importlib.metadata import Distribution, EntryPoint
from pathlib import Path

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
    assert (tmp_path / "orders.zip").stat().st_mode & 0o777 == (
        skill / "SKILL.md"
    ).stat().st_mode & 0o777
    original = (tmp_path / "orders.zip").read_bytes()
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        assert archive.namelist() == ["orders/SKILL.md", "orders/guide.md"]
        assert archive.read("orders/SKILL.md") == b"Review the monthly orders."
    assert invoke("extension", "pack-skill", "orders", "--output", "orders.zip").exit_code == 4
    assert (tmp_path / "orders.zip").read_bytes() == original
    assert invoke("extension", "pack-skill", "orders", "--output", "again.zip").exit_code == 0
    assert (tmp_path / "again.zip").read_bytes() == original


def test_mcp_image_check_packages_the_installed_extension(tmp_path):
    import vecten_shared.consumer_skills as packer

    dist = tmp_path / "example-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: example\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text("[vecten.consumer_skills]\norders = example:orders\n")
    (tmp_path / "example.py").write_text(
        "from pathlib import Path\norders = {'directory': Path(__file__).parent}\n"
    )
    skill = tmp_path / "SKILL.md"
    skill.write_text("Summarize orders.")
    config = tmp_path / "consumer-skills.json"
    config.write_text(
        json.dumps(
            {
                "users": 1,
                "project": {"name": "demo", "scaffold_version": "0.1.2"},
                "extensions": ["example==1.0"],
                "consumer_skills": {"orders": {"groups": ["sales"]}},
            }
        )
    )
    env = dict(os.environ, PYTHONPATH=str(Path(packer.__file__).resolve().parents[1]))
    command = [sys.executable, "-m", packer.__name__, str(config)]
    subprocess.run(command, cwd=tmp_path, env=env, check=True)
    skill.unlink()
    bad = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert bad.returncode and "SKILL.md must be a regular file" in bad.stderr


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
