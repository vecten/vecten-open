"""The HubSpot deals connector of the "Renamed field" scenario, shared with the evaluation
harness: a custom connector that reads its upstream's API response from a file, so renaming
the field in that file reproduces the upstream's rename."""

from __future__ import annotations

import json
from pathlib import Path

from vecten_shared.config import ConnectorDecl, load_config, save_config

from ..fixture_project import render_fixture

CONNECTOR = '''"""HubSpot deals, read from the upstream's API response."""

import json
import os

import dlt


@dlt.source(name="hubspot")
def source():
    @dlt.resource(name="deals", write_disposition="replace", primary_key="id")
    def deals():
        with open(os.environ["HUBSPOT_FIXTURE_FILE"]) as response:
            for row in json.load(response):
                yield {"id": row["id"], "dealname": row["properties"][FIELD]}

    return deals
'''
DEALS = [("1", "Acme pilot"), ("2", "Beta trial"), ("3", "Gamma renewal")]


def upstream(path: Path, field: str) -> None:
    path.write_text(json.dumps([{"id": i, "properties": {field: name}} for i, name in DEALS]))


def write_connector(root: Path, field: str) -> None:
    package = root / "connectors" / "hubspot"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("")
    (package / "connector.py").write_text(CONNECTOR.replace("FIELD", repr(field)))


def hubspot_project(path: Path, domain: str | None = None) -> Path:
    root = render_fixture(path, "postgres", deploy=True, domain=domain)
    write_connector(root, "dealname")
    config = load_config(root)
    hubspot = ConnectorDecl(name="hubspot", source="custom", groups={"default": []})
    save_config(root, config.model_copy(update={"connectors": [*config.connectors, hubspot]}))
    return root
