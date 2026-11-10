"""A rendered project with one `demo` connector, raw rows and models on every dbt layer."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_platform.dbt import runner
from vecten_platform.dbt.local_warehouse import LocalTarget, local_target
from vecten_platform.dbt.raw_replay import replay_fixtures
from vecten_shared.config import ConnectorDecl, load_config, save_config
from vecten_shared.osi.generate import dataset_database

from ..scaffold_matrix import DEPLOY

OVERLAY = Path(__file__).parent / "overlay"

RAW: dict[str, list[dict]] = {
    "company": [
        {
            "id": 1,
            "name": "Acme GmbH",
            "domain": "HTTPS://www.Acme.com/about",
            "email": " Sales@Acme.com ",
            "country": "DE",
            "created_at": "2026-01-05T09:00:00+00:00",
        },
        {
            "id": 2,
            "name": "Beta Ltd",
            "domain": "beta.co.uk",
            "email": "hello@beta.co.uk",
            "country": "GB",
            "created_at": "2026-01-06T09:00:00+00:00",
        },
        {
            "id": 3,
            "name": "Gamma SAS",
            "domain": "gamma.fr",
            "email": "",
            "country": "FR",
            "created_at": "2026-01-07T09:00:00+00:00",
        },
    ],
    "deal": [
        {
            "id": 10,
            "company_id": 1,
            "title": "Acme pilot",
            "stage": "won",
            "amount": 1200.5,
            "created_at": "2026-02-01T10:00:00+00:00",
            "close_date": "2026-02-20",
            "is_won": True,
        },
        {
            "id": 11,
            "company_id": 1,
            "title": "Acme rollout",
            "stage": "open",
            "amount": 8000.0,
            "created_at": "2026-03-01T10:00:00+00:00",
            "close_date": None,
            "is_won": False,
        },
        {
            "id": 12,
            "company_id": 2,
            "title": "Beta trial",
            "stage": "lost",
            "amount": 300.0,
            "created_at": "2026-02-10T10:00:00+00:00",
            "close_date": "2026-02-28",
            "is_won": False,
        },
        {
            "id": 13,
            "company_id": 3,
            "title": "Gamma renewal",
            "stage": "won",
            "amount": 4500.0,
            "created_at": "2026-03-15T10:00:00+00:00",
            "close_date": "2026-03-31",
            "is_won": True,
        },
    ],
}

CONNECTOR_PY = '''"""The demo connector exists for its fixtures; the local build replays them."""


def source():
    raise RuntimeError("fixture connector; replay only")
'''


def install_fixture_project(project_root: Path) -> None:
    shutil.copytree(OVERLAY, project_root, dirs_exist_ok=True)
    connector_dir = project_root / "connectors" / "demo"
    fixtures = connector_dir / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    for name, rows in RAW.items():
        (fixtures / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (connector_dir / "__init__.py").write_text("")
    (connector_dir / "connector.py").write_text(CONNECTOR_PY)
    config = load_config(project_root)
    # The overlay's sources name the Postgres build database; dbt refuses a source in any
    # other database than the one it builds into.
    datasets = project_root / "osi" / "datasets.json"
    datasets.write_text(
        datasets.read_text().replace('"warehouse.', f'"{dataset_database(config)}.')
    )
    demo = ConnectorDecl(name="demo", source="custom", groups={"default": []})
    save_config(project_root, config.model_copy(update={"connectors": [*config.connectors, demo]}))


def render_fixture(
    tmp_path: Path,
    warehouse: str,
    *,
    deploy: bool = False,
    domain: str | None = None,
    account: str | None = "ACME-PROD",
) -> Path:
    """Render the fixture project; `deploy` adds the AWS deploy layer and the MCP component,
    under `domain`, which is also its DNS zone, when one is given, and on Snowflake the
    `account` the platform builds in."""
    root = tmp_path / "proj"
    answers = {"name": "acme", "users": 3, "sources": ["demo"], "warehouse": warehouse}
    components = ["platform"]
    if deploy:
        answers.update(DEPLOY, cloud="aws")
        if warehouse == "snowflake" and account:
            answers["snowflake_account"] = account
        if domain:
            answers.update(domain=domain, dns_zone=domain)
        components = ["platform", "mcp"]
    params = ScaffoldParams.from_answers(answers, {"components": components})
    render_project(root, params)
    install_fixture_project(root)
    return root


def build_fixture(
    root: Path,
    *,
    docs: bool = False,
    validate: bool = False,
    inside: Callable[[LocalTarget], None] | None = None,
) -> list[runner.DbtRun]:
    """Replay the raw rows and run the two-step build, returning the runs without judging them.

    dbt builds against an empty Ossie directory, as `dbt-build-local` does. After a green build,
    `docs` appends a `docs generate` run, `validate` appends a `dbt parse` over the project's
    `osi/`, and `inside(target)` runs while the warehouse is still up so it can read the built
    relations.
    """
    config = load_config(root)
    dbt_dir = root / "dbt"
    workdir = root / ".vecten" / "dbt-local"
    with local_target(config, workdir) as target:
        replay_fixtures(target, root / "connectors", config)
        parse_env = runner.base_env(dbt_dir, target.env)
        empty_osi = workdir / "osi-empty"
        empty_osi.mkdir(parents=True, exist_ok=True)
        env = {**parse_env, runner.OSI_PATH_VAR: str(empty_osi)}
        runner.resolve_dependencies(
            project_dir=dbt_dir, config=config, env=env, in_process=target.in_process
        )
        runs = runner.run_build(project_dir=dbt_dir, env=env, in_process=target.in_process)
        steps = [(["docs", "generate"], env)] if docs else []
        steps += [(["parse"], parse_env)] if validate else []
        for args, step_env in steps:
            if all(r.returncode == 0 for r in runs):
                runs.append(
                    runner.dbt_step(
                        args, project_dir=dbt_dir, env=step_env, in_process=target.in_process
                    )
                )
        if inside is not None and all(r.returncode == 0 for r in runs):
            inside(target)
    return runs
