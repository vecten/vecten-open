"""What a project without apps renders: the stack files `up` writes and each host's bundle.

The goldens under fixtures/release_goldens/ are this module's output on release 0.1.1, before
analytics apps; regenerate them only for a change meant to alter a project without apps.
"""

import tarfile
from pathlib import Path
from unittest import mock

import yaml

# The laptop's Airflow runs as the invoking user's uid, which differs between machines.
LAPTOP_UID = 50000
BUNDLE_FILES = (
    ".vecten/ship/host.json",
    ".vecten/stack/docker-compose.yml",
    ".vecten/stack/nginx/conf.d/default.conf",
)


def render(tmp_path: Path) -> dict[str, str]:
    from vecten.deploy.bundle import build_host_bundle
    from vecten.stack.compose import generate_compose, local_hostnames
    from vecten.stack.nginx import generate_nginx_conf
    from vecten_shared.config import load_config
    from vecten_shared.provider.environment import EnvironmentOutputs

    from .deploy_fixtures import COMMIT, STANDARD_OUTPUTS, add_connector, render_aws

    root = render_aws(tmp_path / "proj")
    add_connector(root, "hubspot", secrets=("HUBSPOT_API_KEY",))
    (root / "dbt" / "target").mkdir()
    (root / "dbt" / "target" / "manifest.json").write_text("{}")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    config = load_config(root)
    outputs = EnvironmentOutputs.model_validate(STANDARD_OUTPUTS)
    found: dict[str, str] = {}
    for host in ("platform", "mcp"):
        bundle = build_host_bundle(root, config, "prod", outputs, host, COMMIT, tmp_path / host)
        with tarfile.open(bundle, "r:gz") as tar:
            for name in BUNDLE_FILES:
                found[f"{host}/{name}"] = tar.extractfile(name).read().decode("utf-8")
    stack = root / ".vecten" / "stack"
    stack.mkdir(parents=True, exist_ok=True)
    with mock.patch("os.getuid", return_value=LAPTOP_UID):
        doc = generate_compose(config, "local", ["platform", "mcp"], stack, project="golden")
    found["local/docker-compose.yml"] = yaml.safe_dump(doc, sort_keys=True)
    found["local/default.conf"] = generate_nginx_conf(
        ["platform", "mcp"], local_hostnames(), "local"
    )
    return {name: text.replace(str(root), "<project>") for name, text in found.items()}


def golden_name(name: str) -> str:
    return name.replace("/", "__")
