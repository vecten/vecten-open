import json
import re
import subprocess
import sys
import tomllib

import pytest
import yaml
from copier import run_copy

from vecten.deploy.outputs import OUTPUT_NAMES
from vecten.deploy.snowflake import DEPLOY_ROLE_PRIVILEGES
from vecten.knowhow import load_skill, skill_names
from vecten.knowhow.mcp_servers import mcp_servers_document
from vecten.knowhow.render import vendor_skills
from vecten.scaffold.baseline import BASELINE
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import DEPLOY_LAYER_PATHS, render_project, template_root
from vecten.stack.compose import LOCAL_AIRFLOW_API_PORT
from vecten_shared.config import load_config
from vecten_shared.osi.generate import render_document
from vecten_shared.versions import VERSIONS

from .scaffold_matrix import DEPLOY, LOCAL, MATRIX, label

COPY_GRANTS = (
    "  # A rebuilt relation keeps the grants of the one it replaces, in the same statement.\n"
    "  +copy_grants: true\n"
)
SKILL_REFERENCE = re.compile(r"skills/([^/\s`]+)/SKILL\.md")
BASE_PATHS = [
    "orchestration/docker-compose.override.yml",
    "osi/datasets.json",
    "osi/metrics.json",
    "evals/semantic.yaml",
    "skills/README.md",
    "docs/schema-convention.md",
    "docs/runbook.md",
    ".github/workflows/ci.yml",
    "CLAUDE.md",
    "vecten.toml",
    ".copier-answers.yml",
    ".gitignore",
]
CONVENTION_SECTIONS = [
    "Schemas",
    "Model names",
    "Layers",
    "Restricted areas",
    "Descriptions and keys",
    "Identity macros",
    "Time",
    "Ossie documents",
    "What a change to this convention means",
]
WORKFLOWS = ["terraform-plan.yml", "ship.yml", "terraform-apply.yml", "deploy-platform.yml"]
PLATFORM_PATHS = [
    "connectors/__init__.py",
    "orchestration/dags/.gitkeep",
    "orchestration/dags/dbt_build.py",
    "dbt/dbt_project.yml",
    "dbt/profiles.yml",
    "dbt/macros/generate_schema_name.sql",
    "dbt/tests/.gitkeep",
]


@pytest.mark.parametrize("params", MATRIX, ids=label)
def test_render_layout(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    for rel in BASE_PATHS:
        assert (target / rel).is_file(), rel
    for rel in PLATFORM_PATHS:
        assert (target / rel).exists() is ("platform" in params.components), rel
    assert not (target / "orchestration" / "docker-compose.yml").exists()
    assert not list(target.rglob("nginx*"))
    cfg = load_config(target)
    assert cfg.components == params.components and cfg.profile == params.profile
    assert cfg.warehouse.kind == params.warehouse and cfg.warehouse.existing is params.has_warehouse
    assert (cfg.deploy is not None) is params.render_deploy
    loc = params.warehouse_locator
    assert (cfg.warehouse.host, cfg.warehouse.port) == (loc.host, loc.port)
    assert (cfg.warehouse.database, cfg.warehouse.account) == (loc.database, loc.account)
    runbook = (target / "docs" / "runbook.md").read_text()
    replay_note = "never run `connector.py`" in runbook
    assert replay_note is ("platform" in params.components)
    scaleway_deploy = params.render_deploy and params.cloud == "scaleway"
    assert ("## Scaleway deployment" in runbook) is scaleway_deploy
    assert ("VECTEN_SCW_ACCESS_KEY" in runbook) is scaleway_deploy
    assert ("## dbt" in runbook) is ("platform" in params.components)
    aws_deploy = params.render_deploy and params.cloud == "aws"
    assert ("## AWS deployment" in runbook) is aws_deploy
    assert ("aws " in runbook) is aws_deploy
    assert ("### Protect the deploy environment" in runbook) is params.render_deploy
    assert ("### One queue per environment" in runbook) is params.render_deploy
    served = "the MCP reads only the `core` and `marts` schemas" in runbook
    assert served is (params.render_deploy and "platform" not in params.components)
    snowflake_root = params.warehouse == "snowflake" and "platform" in params.components
    assert ("### Snowflake" in runbook) is (params.render_deploy and snowflake_root)
    assert ("## Promotion order" in runbook) is params.render_deploy
    assert ("### What a promoted run reads from where" in runbook) is params.render_deploy
    own_account = "Set `snowflake_account` there to `dev`'s Snowflake account" in runbook
    assert own_account is (params.render_deploy and snowflake_root)
    assert "waits for Snowflake accounts per environment" not in runbook
    assert (DEPLOY_ROLE_PRIVILEGES in runbook) is (params.render_deploy and snowflake_root)
    # In a shared account another environment's integration reaches a deploy user without
    # a key-pair-only policy.
    shared = params.snowflake_shared_account
    deploy_policy = "SET AUTHENTICATION POLICY <database>.<schema>.DEPLOY_SIGN_IN" in runbook
    assert deploy_policy is (params.render_deploy and snowflake_root and shared)
    fixed_account = "One environment per Snowflake account" in runbook
    assert fixed_account is (params.render_deploy and snowflake_root and not shared)
    identity = "`vecten mcp provisioner-script --env <env>`, which stores" in runbook
    assert identity is (params.render_deploy and snowflake_root and "mcp" in params.components)
    managed_warehouse = params.warehouse == "postgres" and "platform" in params.components
    assert ("restore-db-instance-from-db-snapshot" in runbook) is (aws_deploy and managed_warehouse)
    assert ("scw rdb snapshot restore" in runbook) is (scaleway_deploy and managed_warehouse)
    # A snapshot-restored RDS instance has no managed master secret, which Terraform needs.
    assert ("--manage-master-user-password" in runbook) is (aws_deploy and managed_warehouse)
    # After a destroy the MCP's registry of people sits in an empty `state` under standard.
    restore_state = (
        managed_warehouse
        and params.render_deploy
        and "mcp" in params.components
        and params.profile == "standard"
    )
    flat = " ".join(runbook.split())
    assert ("restore `state` as well, from the final snapshot" in flat) is restore_state
    if aws_deploy and managed_warehouse:
        # The log prefix lives on the instance holding warehouse: `state` under minimal.
        holder = "state" if params.profile == "minimal" else "warehouse"
        assert f"reboot-db-instance --db-instance-identifier <prefix>-{holder}`" in runbook
        assert "-backend-config=encrypt=true" in runbook
    convention = (target / "docs" / "schema-convention.md").read_text()
    assert convention.startswith(f"# Schema convention, version {VERSIONS.schema_convention}\n")
    assert VERSIONS.schema_convention == 2
    assert f"Apache Ossie {VERSIONS.ossie_spec}" in convention
    assert len(convention.splitlines()) < 150
    headings = re.findall(r"^## (.+)$", convention, re.M)
    assert headings == CONVENTION_SECTIONS
    platform = "platform" in params.components
    assert ("CI runs `vecten dbt-build-local --check`" in convention) is platform
    claude = (target / "CLAUDE.md").read_text()
    assert ("dbt-build-local" in claude) is platform
    assert ("vecten mcp introspect" in claude) is not platform
    for dbt_advice in ("Layering:", "core reads the snapshots", "normalize_domain"):
        assert (dbt_advice in claude) is platform, dbt_advice


def test_quotes_in_warehouse_locator_keep_vecten_toml_valid(tmp_path):
    params = ScaffoldParams.from_answers(
        {"name": "acme", "users": 5, "warehouse_url": 'postgres://h/dw"x'}, {}
    )
    target = tmp_path / "proj"
    render_project(target, params)
    assert load_config(target).warehouse.database == 'dw"x'


@pytest.mark.parametrize(
    "url,user",
    [
        ("postgresql://mcp_admin@warehouse.acme.test:5432/analytics", "mcp_admin"),
        ("snowflake://VECTEN_ADMIN@acct/db", "VECTEN_ADMIN"),
        ("postgres://h/dw", None),
    ],
)
def test_the_locator_user_reaches_vecten_toml(tmp_path, url, user):
    params = ScaffoldParams.from_answers({"name": "acme", "users": 5, "warehouse_url": url}, {})
    target = tmp_path / "proj"
    render_project(target, params)
    assert load_config(target).warehouse.admin_user == user


def product_files(params) -> set[str]:
    """Every file outside skills/ that a project with these parameters is rendered with."""
    files = {*BASE_PATHS, "README.md", BASELINE} - {"skills/README.md"}
    if "platform" in params.components:
        files |= {*PLATFORM_PATHS, ".mcp.json", "dbt/snapshots/.gitkeep"}
        files |= {f"dbt/models/{layer}/.gitkeep" for layer in ("staging", "core", "marts")}
    if params.render_deploy:
        files |= set(DEPLOY_LAYER_PATHS(params.cloud, params.warehouse, params.components))
    return files


@pytest.mark.parametrize("params", MATRIX, ids=label)
def test_render_holds_only_the_product_files(tmp_path, params):
    target = tmp_path / "proj"
    rendered = {
        path.relative_to(target).as_posix()
        for path in render_project(target, params)
        if not path.relative_to(target).as_posix().startswith("skills/")
    }
    assert rendered == product_files(params)


def test_an_extra_rendered_file_is_caught(tmp_path):
    target = tmp_path / "proj"
    params = next(p for p in MATRIX if "platform" in p.components)
    render_project(target, params)
    (target / "connectors" / "extra_source.py").write_text("")
    rendered = {
        p.relative_to(target).as_posix()
        for p in target.rglob("*")
        if p.is_file() and not p.relative_to(target).as_posix().startswith("skills/")
    }
    assert rendered - product_files(params) == {"connectors/extra_source.py"}


@pytest.mark.parametrize("params", [p for p in MATRIX if "platform" in p.components][:4], ids=label)
def test_rendered_dbt_project_is_valid_yaml(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    for name in ("dbt_project.yml", "profiles.yml"):
        yaml.safe_load((target / "dbt" / name).read_text())


def test_rendered_project_is_not_tracking_generated_dbt_files(tmp_path):
    render_project(tmp_path / "proj", MATRIX[0])
    ignored = (tmp_path / "proj" / ".gitignore").read_text().splitlines()
    for line in ("dbt/packages.yml", "dbt/package-lock.yml", "dbt/dbt_packages/", "dbt/target/"):
        assert line in ignored
    assert "dbt/project-packages.yml" not in ignored
    assert not (tmp_path / "proj" / "dbt" / "packages.yml").exists()


def test_rendered_dbt_project_delegates_schema_names_to_the_convention(tmp_path):
    render_project(tmp_path / "proj", MATRIX[0])
    dbt_dir = tmp_path / "proj" / "dbt"
    project = yaml.safe_load((dbt_dir / "dbt_project.yml").read_text())
    assert project["require-dbt-version"] == [">=1.12.0", "<1.13.0"]
    assert project["test-paths"] == ["tests"]
    assert project["osi-paths"] == ["{{ env_var('VECTEN_OSI_PATH', '../osi') }}"]
    assert project["packages-install-path"] == (
        "{{ env_var('DBT_PACKAGES_INSTALL_PATH', 'dbt_packages') }}"
    )
    assert project["snapshots"]["acme"]["+schema"] == "snapshots"
    macro = (dbt_dir / "macros" / "generate_schema_name.sql").read_text()
    assert "vecten_convention.generate_schema_name(custom_schema_name, node)" in macro


def test_snowflake_models_keep_their_grants_across_rebuilds(tmp_path):
    texts = {}
    for warehouse in ("postgres", "snowflake"):
        params = ScaffoldParams.from_answers(
            {"name": "acme", "users": 3, "sources": ["hubspot"], "warehouse": warehouse}, {}
        )
        render_project(tmp_path / warehouse, params)
        texts[warehouse] = (tmp_path / warehouse / "dbt" / "dbt_project.yml").read_text()
    assert yaml.safe_load(texts["snowflake"])["models"]["+copy_grants"] is True
    # A Postgres project renders exactly as before; Snowflake adds the setting only.
    assert "copy_grants" not in texts["postgres"]
    assert texts["snowflake"] == texts["postgres"].replace(
        "\nmodels:\n", "\nmodels:\n" + COPY_GRANTS
    )


@pytest.mark.parametrize("warehouse", ["postgres", "snowflake"])
def test_rendered_profile_reads_threads_from_the_environment(tmp_path, warehouse):
    params = ScaffoldParams.from_answers(
        {"name": "acme", "users": 3, "sources": ["hubspot"], "warehouse": warehouse}, {}
    )
    render_project(tmp_path / "proj", params)
    output = yaml.safe_load((tmp_path / "proj" / "dbt" / "profiles.yml").read_text())["vecten"][
        "outputs"
    ]["local"]
    assert output["threads"] == "{{ env_var('DBT_THREADS', '4') | as_number }}"
    if warehouse == "postgres":
        assert "'warehouse'" in output["dbname"]
    else:
        assert output["account"] == "{{ env_var('SNOWFLAKE_ACCOUNT', '') }}"


def test_osi_shells_have_the_root_shape(tmp_path):
    render_project(tmp_path / "proj", MATRIX[0])
    for name, model in (("datasets.json", "acme"), ("metrics.json", "acme_metrics")):
        text = (tmp_path / "proj" / "osi" / name).read_text()
        doc = json.loads(text)
        assert text == render_document(doc), name
        assert doc["version"] == VERSIONS.ossie_spec
        assert doc["semantic_model"][0]["name"] == model
        assert doc["semantic_model"][0]["datasets"] == []


@pytest.mark.parametrize("params", MATRIX[:6], ids=label)
def test_rendered_python_lints(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    r = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(target)], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_copier_answers_hold_every_parameter(tmp_path):
    target = tmp_path / "proj"
    render_project(target, MATRIX[0])
    answers = yaml.safe_load((target / ".copier-answers.yml").read_text())
    assert answers["cloud"] == "aws" and answers["components"] == ["platform", "mcp"]
    scaffold_version = tomllib.loads((target / "vecten.toml").read_text())["project"][
        "scaffold_version"
    ]
    assert scaffold_version == answers["vecten_version"]


DEPLOY_SAMPLE = [
    p
    for p in MATRIX
    if p.render_deploy and p.profile == "standard" and p.components == ["platform", "mcp"]
]


@pytest.mark.parametrize("params", MATRIX, ids=label)
def test_deploy_layer_only_with_deploy_answers(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    assert (target / "deploy").exists() is params.render_deploy
    for wf in WORKFLOWS:
        assert (target / ".github" / "workflows" / wf).exists() is params.render_deploy, wf
    if not params.render_deploy:
        return
    expected = set(DEPLOY_LAYER_PATHS(params.cloud, params.warehouse, params.components))
    actual = {str(p.relative_to(target)) for p in (target / "deploy").rglob("*") if p.is_file()}
    actual |= {f".github/workflows/{wf}" for wf in WORKFLOWS}
    assert actual == expected
    other = {"aws", "scaleway"} - {params.cloud}
    for o in other:
        assert not (target / "deploy" / "terraform" / o).exists()
    snowflake_expected = params.warehouse == "snowflake" and "platform" in params.components
    assert (target / "deploy" / "terraform" / "snowflake").exists() is snowflake_expected
    for n in ("main", "variables", "backend"):
        assert (target / "deploy" / "terraform" / "snowflake" / f"{n}.tf").is_file() is (
            snowflake_expected
        ), n


SNOWFLAKE_ROOTS = [
    p
    for p in MATRIX
    if p.render_deploy and p.warehouse == "snowflake" and "platform" in p.components
]


@pytest.mark.parametrize("params", SNOWFLAKE_ROOTS, ids=label)
def test_snowflake_root_takes_the_provider_from_the_environment_and_names_by_var(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    root = target / "deploy" / "terraform" / "snowflake"
    main = (root / "main.tf").read_text()
    provider = re.search(r'^provider "snowflake" \{\n(.*?)^\}', main, re.M | re.S)
    assert provider
    assert provider.group(1).strip() == (
        'preview_features_enabled = ["snowflake_current_role_datasource", '
        '"snowflake_authentication_policy_resource", '
        '"snowflake_user_authentication_policy_attachment_resource"]'
    )
    assert f'version = "~> {VERSIONS.terraform_snowflake_provider}.0"' in main
    assert f"vecten-open.git//terraform/snowflake?ref={params.terraform_module_ref}" in main
    for name in (
        "database_name",
        "warehouse_name",
        "analyst_role_name",
        "loader_role_name",
        "loader_user_name",
        "provisioner_role_name",
        "warehouse_size",
        "loader_public_key",
        "loader_sign_in",
    ):
        assert re.search(rf"^\s*{name}\s*=\s*var\.{name}\s*$", main, re.M), name
        assert f'variable "{name}"' in (root / "variables.tf").read_text(), name
    assert 'backend "s3"' in (root / "backend.tf").read_text()


@pytest.mark.parametrize("params", [p for p in MATRIX if p.render_deploy], ids=label)
def test_prod_tfvars_filled_without_placeholders(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    text = (target / "deploy" / "envs" / "prod.tfvars").read_text()
    assert "<" not in text and "TODO" not in text and "CHANGEME" not in text
    for needle in (
        params.domain,
        params.dns_zone,
        params.region,
        params.github_repository,
        params.profile,
    ):
        assert needle in text
    for c in params.components:
        assert f'"{c}"' in text
    if params.warehouse == "snowflake":
        assert "snowflake_account" in text


@pytest.mark.parametrize("params", DEPLOY_SAMPLE, ids=label)
def test_thin_root_module_references_published_module_by_ref(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    main = (target / "deploy" / "terraform" / params.cloud / "main.tf").read_text()
    assert f"vecten-open.git//terraform/{params.cloud}?ref=v0.1.0" in main


@pytest.mark.parametrize("params", DEPLOY_SAMPLE, ids=label)
def test_thin_root_configures_aws_on_aws_only_and_reexports_outputs(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    root = target / "deploy" / "terraform" / params.cloud
    assert ('provider "aws"' in (root / "main.tf").read_text()) is (params.cloud == "aws")
    outputs = re.findall(r'^output "(\w+)"', (root / "outputs.tf").read_text(), re.M)
    assert outputs == list(OUTPUT_NAMES)


@pytest.mark.parametrize("params", DEPLOY_SAMPLE, ids=label)
def test_workflows_have_no_long_lived_key_and_apply_only_on_dispatch(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    wf = target / ".github" / "workflows"
    for name in WORKFLOWS:
        doc = yaml.safe_load((wf / name).read_text())
        text = (wf / name).read_text()
        assert "AWS_ACCESS_KEY_ID" not in text and "AWS_SECRET_ACCESS_KEY" not in text
        triggers = doc[True] if True in doc else doc["on"]
        if name in ("terraform-apply.yml", "deploy-platform.yml"):
            assert set(triggers) <= {"workflow_dispatch", "workflow_call"}, name
        if name == "ship.yml":
            assert triggers["push"]["branches"] == ["main"]
        if name == "terraform-plan.yml":
            assert "pull_request" in triggers and "push" in triggers


def test_region_with_a_quote_keeps_workflows_valid_yaml(tmp_path):
    # model_copy skips validation, so only the template's own quoting keeps the YAML valid.
    params = DEPLOY_SAMPLE[0].model_copy(update={"region": 'eu"central: 1'})
    target = tmp_path / "proj"
    # Render only the template: full project rendering vendors skills and validates
    # the config, which correctly rejects this deliberately invalid region.
    run_copy(
        str(template_root()),
        str(target),
        data=params.copier_data(),
        defaults=True,
        quiet=True,
        unsafe=True,
        vcs_ref=None,
    )
    wf = target / ".github" / "workflows"
    seen = 0
    for name in WORKFLOWS:
        doc = yaml.safe_load((wf / name).read_text())
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                if "aws-region" in step.get("with", {}):
                    assert step["with"]["aws-region"] == 'eu"central: 1'
                    seen += 1
    assert seen == 3


@pytest.mark.parametrize("params", [p for p in MATRIX if p.render_deploy], ids=label)
def test_terraform_constraint_and_workflow_engine_derive_from_versions(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    constraint = f'required_version = "~> {VERSIONS.terraform}.0"'
    roots = [params.cloud] + (
        ["snowflake"] if (target / "deploy/terraform/snowflake").is_dir() else []
    )
    for root in roots:
        assert constraint in (target / "deploy" / "terraform" / root / "main.tf").read_text(), root
    for name in ("terraform-plan.yml", "terraform-apply.yml"):
        doc = yaml.safe_load((target / ".github" / "workflows" / name).read_text())
        engines = [
            step["with"]["terraform_version"]
            for job in doc["jobs"].values()
            for step in job["steps"]
            if "terraform_version" in step.get("with", {})
        ]
        assert engines == [f"~{VERSIONS.terraform}.0"], name


@pytest.mark.parametrize("params", MATRIX[:2], ids=label)
def test_runbook_logs_command_matches_how_the_cli_runs_compose(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    runbook = (target / "docs" / "runbook.md").read_text()
    assert (
        "`docker compose --project-directory .vecten/stack --env-file .env "
        "-f .vecten/stack/docker-compose.yml logs`"
    ) in runbook


def test_aws_runbook_names_what_the_deploy_code_uses(tmp_path):
    from vecten.deploy.aws import (
        PLAN_ROLE_VARIABLE_PREFIX,
        REGION_VARIABLE,
        ROLE_VARIABLE,
        STATE_BUCKET_VARIABLE,
    )
    from vecten.deploy.aws.tunnel import DOCUMENT
    from vecten.deploy.run import RUNBOOK, RUNBOOK_PROTECT_SECTION

    params = next(
        p
        for p in MATRIX
        if p.cloud == "aws" and p.warehouse == "postgres" and p.components == ["platform", "mcp"]
    )
    root = tmp_path / "proj"
    render_project(root, params)
    runbook = (root / RUNBOOK).read_text()
    section = RUNBOOK_PROTECT_SECTION.strip('"')
    assert f"\n### {section}\n" in runbook
    for name in (ROLE_VARIABLE, REGION_VARIABLE, STATE_BUCKET_VARIABLE, DOCUMENT):
        assert name in runbook, name
    assert f"`{PLAN_ROLE_VARIABLE_PREFIX}<ENV>`" in runbook
    flat = " ".join(runbook.split())
    for phrase in (
        "vecten/acme/<env>/<scope>/<NAME>",
        "`MCP_*`",
        "`DEPLOY_*`",
        "`ADMIN_*`",
        "vecten user add-admin <firstname> --env <env>",
        "aws rds restore-db-instance-to-point-in-time",
        "terraform import",
        "--delete-data <env>",
        "database_public_cidrs",
        'log_line_prefix = "%t:%r:%u@%d:[%p]:"',
        "deploy-<env>",
        ".terraform.lock.hcl",
        "/run/vecten/",
    ):
        assert phrase in flat, phrase


@pytest.mark.parametrize("profile", ["standard", "minimal"])
def test_scaleway_runbook_names_what_the_deploy_code_uses(tmp_path, profile):
    from vecten.deploy.run import RUNBOOK, RUNBOOK_PROTECT_SECTION
    from vecten.deploy.scaleway import PUBLIC_CIDRS
    from vecten.deploy.scaleway.bootstrap import (
        ENV_ACCESS_SECRET,
        ENV_SECRET_SECRET,
        KEY_LIFETIME,
        PLAN_ACCESS_PREFIX,
        PLAN_SECRET_PREFIX,
        PROJECT_VARIABLE,
        REGION_VARIABLE,
        ROTATE_WITHIN,
        STATE_BUCKET_VARIABLE,
        STATE_PROJECT_VARIABLE,
        host_key_name,
        project_marker,
    )
    from vecten.deploy.scaleway.ship import (
        FIRST_SHIP_WINDOW,
        KEY_ACK_WINDOW,
        KEY_ENTRY,
        SHIP_WINDOW,
        pointer_key,
        status_key,
    )
    from vecten.deploy.vm.apply import KEY_FILE_NAME, STATE_DIR, WATCH_CONFIG

    params = next(
        p
        for p in MATRIX
        if p.cloud == "scaleway"
        and p.warehouse == "postgres"
        and p.components == ["platform", "mcp"]
        and p.profile == profile
    )
    root = tmp_path / "proj"
    render_project(root, params)
    runbook = (root / RUNBOOK).read_text()
    assert f"\n### {RUNBOOK_PROTECT_SECTION.strip(chr(34))}\n" in runbook
    flat = " ".join(runbook.split())
    holder = "state" if profile == "minimal" else "warehouse"
    for phrase in (
        ENV_ACCESS_SECRET,
        ENV_SECRET_SECRET,
        f"`{PLAN_ACCESS_PREFIX}<ENV>`",
        f"`{PLAN_SECRET_PREFIX}<ENV>`",
        PROJECT_VARIABLE,
        STATE_PROJECT_VARIABLE,
        REGION_VARIABLE,
        STATE_BUCKET_VARIABLE,
        f"`{project_marker('<role>', '<project>', '<env>')}`",
        f"{KEY_LIFETIME.days} days",
        f"{ROTATE_WITHIN.days} days",
        f"`{host_key_name('<host>')}`",
        f"`{KEY_ENTRY}`",
        f"`{STATE_DIR / KEY_FILE_NAME}`",
        f"`{WATCH_CONFIG}`",
        f"`{pointer_key('<host>')}`",
        f"`{status_key('<first component>', '<commit>')}`",
        f"{KEY_ACK_WINDOW // 60} minutes",
        f"{FIRST_SHIP_WINDOW // 60} minutes",
        f"{SHIP_WINDOW // 60} minutes",
        PUBLIC_CIDRS,
        'cloud_ids["dns_project"]',
        "-var dns_project_id=<dns project id>",
        "ALTER ROLE loader PASSWORD",
        "ALTER ROLE impersonator PASSWORD",
        "never as replaced",
        "All Applications",
        "/vecten/acme/<env>/<scope>/<NAME>",
        "`SETUP_*`",
        f"SETUP_{holder.upper()}_ADMIN_PASSWORD",
        f"{holder}_admin_password_version",
        'module.environment.module.postgres["warehouse"].scaleway_rdb_instance.this',
        "<env>/scaleway.tfstate",
        "verify-ca",
        "vecten user add-admin <firstname> --env <env>",
        "--delete-data <env>",
        "deploy-<env>",
        ".terraform.lock.hcl",
        "scw init",
        # What the #26 restore needed: the snapshot's 365-day limit, the allow-list, the
        # private endpoint before the import, the ship that waits for the reset passwords.
        "365 days",
        "scw rdb snapshot create instance-id=<restored instance id>",
        "scw rdb acl set <your address>/32",
        "private-network.enable-ipam=true",
        "password authentication failed",
        "Then run `vecten deploy --env <env>` again",
        "scw rdb log download <instance id>",
    ):
        assert phrase in flat, phrase
    assert "with no expiry" not in flat
    # The tfvars need no hand edit: bootstrap finds the zone's project itself.
    assert 'dns_project_id = "' not in flat
    # With the MCP a restore after a destroy brings `state` back too, under either profile, and
    # with it Airflow's metadata.
    for phrase in (
        "ALTER ROLE airflow PASSWORD",
        "ALTER ROLE mcp_state PASSWORD",
        "generated a new `AIRFLOW_FERNET_KEY`",
        "connections and variables",
    ):
        assert phrase in flat, phrase
    assert ("And on the restored `state` instance" in flat) is (profile == "standard")


def test_scaleway_runbook_restores_state_without_the_mcp_only_under_minimal(tmp_path):
    from vecten.deploy.run import RUNBOOK

    for profile in ("standard", "minimal"):
        params = next(
            p
            for p in MATRIX
            if p.cloud == "scaleway"
            and p.warehouse == "postgres"
            and p.components == ["platform"]
            and p.profile == profile
        )
        root = tmp_path / profile
        render_project(root, params)
        flat = " ".join((root / RUNBOOK).read_text().split())
        minimal = profile == "minimal"
        assert ("ALTER ROLE airflow PASSWORD" in flat) is minimal
        assert ("generated a new `AIRFLOW_FERNET_KEY`" in flat) is minimal


def test_runbook_and_claude_md_describe_connectors(tmp_path):
    root = tmp_path / "proj"
    params = ScaffoldParams.from_answers({"name": "acme", "users": 3, "sources": ["hubspot"]}, {})
    render_project(root, params)
    runbook = (root / "docs" / "runbook.md").read_text()
    assert "## Connectors" in runbook
    for command in ("vecten connector run", "--record", "vecten raw schema", "vecten secret set"):
        assert command in runbook
    claude = (root / "CLAUDE.md").read_text()
    assert "connectors/<source>/connector.py" in claude and "secret(" in claude
    ci = (root / ".github" / "workflows" / "ci.yml").read_text()
    assert "pytest connectors" in ci


def test_runbook_says_record_reloads_and_fixtures_are_real_data(tmp_path):
    root = tmp_path / "proj"
    params = ScaffoldParams.from_answers({"name": "acme", "users": 3, "sources": ["hubspot"]}, {})
    render_project(root, params)
    runbook = " ".join((root / "docs" / "runbook.md").read_text().split())
    assert (
        "`--record` reloads the object from the start so the fixture is complete; run it on the "
        "first sync or whenever the fixture should be refreshed."
    ) in runbook
    for phrase in ("real records", "up to 200 per object", "committed", "personal data"):
        assert phrase in runbook, phrase


def test_mcp_only_project_has_no_connector_docs_or_ci_step(tmp_path):
    root = tmp_path / "proj"
    params = ScaffoldParams.from_answers(
        {"name": "acme", "users": 3, "warehouse_url": "postgres://dw.internal:5432/analytics"}, {}
    )
    render_project(root, params)
    assert params.components == ["mcp"]
    assert not (root / "connectors").exists()
    assert "## Connectors" not in (root / "docs" / "runbook.md").read_text()
    assert "## Connectors" not in (root / "CLAUDE.md").read_text()
    assert "pytest connectors" not in (root / ".github" / "workflows" / "ci.yml").read_text()


@pytest.mark.parametrize("params", MATRIX, ids=label)
def test_ci_validates_the_semantic_model_and_checks_the_build(tmp_path, params):
    root = tmp_path / "proj"
    render_project(root, params)
    ci = (root / ".github" / "workflows" / "ci.yml").read_text()
    pin = f'uvx --with "vecten=={params.vecten_version}"'
    assert f"{pin} vecten mcp validate" in ci
    assert (f"{pin} vecten dbt-build-local --check" in ci) == ("platform" in params.components)
    assert ("docker info" in ci) == ("platform" in params.components)
    # An unpinned ruff takes each new release's default rules into the project's CI.
    assert f"- run: uvx ruff@{VERSIONS.ruff} check ." in ci
    yaml.safe_load(ci)


@pytest.mark.parametrize("components", [["platform", "mcp"], ["platform"], ["mcp"]])
def test_dbt_dag_ships_with_the_platform_and_compiles(tmp_path, components):
    target = tmp_path / "proj"
    params = next(p for p in MATRIX if p.components == components)
    render_project(target, params)
    dag = target / "orchestration" / "dags" / "dbt_build.py"
    assert dag.is_file() is ("platform" in components)
    if dag.is_file():
        source = dag.read_text()
        compile(source, str(dag), "exec")
        assert "import vecten." not in source and "from vecten." not in source
        assert "build_dbt_dag(dbt_assets(load_config(" in source


@pytest.mark.parametrize("params", MATRIX, ids=label)
def test_vendored_skills_and_mcp_json_are_the_package_files(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    written = vendor_skills(target)
    for name in skill_names():
        vendored = target / "skills" / name / "SKILL.md"
        assert vendored.read_text(encoding="utf-8") == load_skill(name), name
        assert vendored in written
        assert not (target / "skills" / name / "evals").exists()
    mcp = target / ".mcp.json"
    if "platform" in params.components:
        assert (
            mcp.read_text(encoding="utf-8") == json.dumps(mcp_servers_document(), indent=2) + "\n"
        )
        assert mcp in written
        assert ".mcp.json" not in (target / ".gitignore").read_text().splitlines()
    else:
        assert not mcp.exists() and mcp not in written


def test_vendored_skills_reference_only_vendored_paths(tmp_path):
    target = tmp_path / "proj"
    render_project(target, MATRIX[0])
    vendor_skills(target)
    known = set(skill_names())
    for path in (target / "skills").glob("*/SKILL.md"):
        named = set(SKILL_REFERENCE.findall(path.read_text(encoding="utf-8")))
        assert named <= known, (path.parent.name, named - known)


# The `connector` and `model` skills act on the platform component's connectors and dbt project.
PLATFORM_SKILLS = {"connector", "model"}
MCP_SERVER_VARIABLES = ("AIRFLOW_API_URL", "AIRFLOW_USERNAME", "AIRFLOW_PASSWORD", "DBT_PATH")


@pytest.mark.parametrize("params", MATRIX, ids=label)
def test_claude_md_lists_the_vendored_skills_and_the_loop(tmp_path, params):
    target = tmp_path / "proj"
    render_project(target, params)
    vendor_skills(target)
    claude = (target / "CLAUDE.md").read_text()
    skills = claude.split("\n## Skills\n")[1].split("\n## ")[0]
    flat = " ".join(skills.split())
    platform = "platform" in params.components
    expected = {n for n in skill_names() if platform or n not in PLATFORM_SKILLS}
    named = set(SKILL_REFERENCE.findall(skills))
    assert named == expected, named ^ expected
    for name in named:
        assert (target / "skills" / name / "SKILL.md").is_file(), name
    assert "`vecten next --json`" in flat
    assert "`skills/guide/SKILL.md` is the entry" in flat
    assert ("`.mcp.json`" in flat) is platform
    assert "do not use them" not in flat
    for variable in MCP_SERVER_VARIABLES:
        assert (f"`{variable}`" in flat) is platform, variable
    assert (f"`http://127.0.0.1:{LOCAL_AIRFLOW_API_PORT}`" in flat) is platform
    assert "`vecten issues --json`" in flat


@pytest.mark.parametrize("name", ["osi/metrics.json", "evals/semantic.yaml"])
def test_rerender_preserves_owner_files_and_pristine_baseline(tmp_path, name):
    render_project(tmp_path, MATRIX[0])
    original = (tmp_path / name).read_text()
    (tmp_path / name).write_text("owner contents\n")
    render_project(tmp_path, MATRIX[0])
    assert (tmp_path / name).read_text() == "owner contents\n"
    assert json.loads((tmp_path / BASELINE).read_text())["files"][name] == original


def _snowflake_runbook(tmp_path, shared):
    answers = {**LOCAL, **DEPLOY, "sources": ["hubspot"], "warehouse": "snowflake", "cloud": "aws"}
    params = ScaffoldParams.from_answers(
        answers, {"components": ["platform", "mcp"], "snowflake_shared_account": shared}
    )
    render_project(tmp_path / "p", params)
    runbook = (tmp_path / "p" / "docs" / "runbook.md").read_text()
    return runbook, (tmp_path / "p" / ".copier-answers.yml").read_text()


def test_the_runbook_states_a_shared_accounts_names_policies_and_trust(tmp_path):
    runbook, answers = _snowflake_runbook(tmp_path, shared=True)
    section = runbook[runbook.index("### Snowflake") : runbook.index("## First sign-in")]
    for text in (
        "`_<BASE>_<ENV>`",
        "`ANALYST_<BASE>_<ENV>`",
        "`PROVISIONER_<BASE>_<ENV>`",
        "`EXTERNAL_OAUTH_<BASE>_<ENV>`",
        "`<BASE>_<ENV>.VECTEN_ACCESS.MCP_SIGN_IN`",
        "SET AUTHENTICATION POLICY <database>.<schema>.DEPLOY_SIGN_IN",
        "its own deploy user and role",
        "by ownership, never by a name pattern",
        "Moving an environment that has fixed names into a shared account is a new deployment",
        "create `prod` first",
        "live checks have yet to confirm",
    ):
        assert text in section, text
    for fixed in ("`VECTEN_PROVISIONER`", "One environment per Snowflake account"):
        assert fixed not in section, fixed
    # The layout is vecten.toml's; the answers file stays the same in both layouts.
    assert "snowflake_shared_account" not in answers
    assert _snowflake_runbook(tmp_path / "fixed", shared=False)[1] == answers.replace(
        str(tmp_path / "p"), str(tmp_path / "fixed" / "p")
    )


def test_the_runbook_states_that_fixed_names_hold_an_account_alone(tmp_path):
    runbook, _ = _snowflake_runbook(tmp_path, shared=False)
    section = runbook[runbook.index("### Snowflake") : runbook.index("## First sign-in")]
    assert "One environment per Snowflake account" in section
    assert "`VECTEN_PROVISIONER`" in section and "`ANALYST`" in section
    assert "_<BASE>_<ENV>" not in section
