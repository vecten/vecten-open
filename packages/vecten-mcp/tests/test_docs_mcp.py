import re
from importlib.metadata import entry_points
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def test_mcp_hub_lists_every_tool():
    names = {ep.name for ep in entry_points(group="vecten.mcp.tools")}
    table = (REPO / "docs" / "mcp.md").read_text().split("\n## Tools\n")[1].split("\n## ")[0]
    assert names, "the vecten-mcp distribution registers no tools"
    assert set(re.findall(r"`([a-z_-]+)`", table)) >= names
