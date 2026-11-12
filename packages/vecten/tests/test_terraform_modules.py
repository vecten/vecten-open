"""The published Terraform modules: `terraform test` over mocked providers, and static checks.

The `terraform` tests run the binary with providers from the registry (or the plugin cache);
the rest read the module files and need no binary.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from vecten.deploy.outputs import OUTPUT_NAMES
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.versions import VERSIONS

from .scaffold_matrix import MATRIX, label

REPO = Path(__file__).resolve().parents[3]
TERRAFORM = REPO / "terraform"
AWS = TERRAFORM / "aws"
SCALEWAY = TERRAFORM / "scaleway"
SNOWFLAKE = TERRAFORM / "snowflake"
MODULE_ROOTS = ["aws", "scaleway", "snowflake"]
AGENT = REPO / "packages" / "vecten" / "src" / "vecten" / "deploy" / "vm" / "apply.py"
OPTIONAL_VARIABLES = {
    "platform_size",
    "root_volume_size_gb",
    "instance_type_overrides",
    "database_public_cidrs",
    "log_line_prefix",
    "destroying",
}
LOCAL = {"name": "acme", "users": 8, "sources": ["hubspot"]}
DEPLOY = {
    "domain": "data.acme.com",
    "dns_zone": "acme.com",
    "github_repository": "acme/data",
    "cloud": "aws",
}


def _terraform_env() -> dict[str, str]:
    env = dict(os.environ)
    cache = Path(env.get("TF_PLUGIN_CACHE_DIR") or Path.home() / ".terraform.d" / "plugin-cache")
    # Terraform's plugin cache breaks under concurrent installs, so each xdist worker has its own.
    if worker := os.environ.get("PYTEST_XDIST_WORKER"):
        cache = cache / worker
        cache.mkdir(parents=True, exist_ok=True)
    env["TF_PLUGIN_CACHE_DIR"] = str(cache)
    env["TF_IN_AUTOMATION"] = "1"
    env["CHECKPOINT_DISABLE"] = "1"
    return env


def _hcl_files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in {".tf", ".hcl", ".tftpl"}
        and ".terraform" not in p.parts
        and p.name != ".terraform.lock.hcl"
    )


def _block_arguments(text: str, header: str) -> set[str]:
    """Argument names at the top level of the block that `header`, ending in `{`, opens."""
    body = text[text.index(header) + len(header) :]
    depth = 1
    names: set[str] = set()
    for line in body.splitlines():
        if depth == 1:
            match = re.match(r"\s*(\w+)\s*=", line)
            if match:
                names.add(match.group(1))
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break
    return names


def _declared_variables(path: Path) -> set[str]:
    return set(re.findall(r'^variable "(\w+)"', path.read_text(encoding="utf-8"), re.M))


def _blocks(text: str, header: str) -> list[str]:
    """The bodies of every top-level block whose opening line starts with `header`."""
    bodies = []
    for match in re.finditer(rf"^{re.escape(header)}[^\n]*\{{\n", text, re.M):
        depth, end = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        bodies.append(text[match.end() : end - 1])
    return bodies


def _init_and_test(directory: Path, *, validate: bool = False) -> None:
    env = _terraform_env()
    steps = [["init", "-backend=false", "-input=false"], *([["validate"]] if validate else [])]
    for args in (*steps, ["test"]):
        done = subprocess.run(
            ["terraform", f"-chdir={directory}", *args, "-no-color"],
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )
        assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.terraform
@pytest.mark.parametrize("root", MODULE_ROOTS)
def test_terraform_test_passes(root):
    if shutil.which("terraform") is None:
        pytest.skip("terraform is not installed")
    _init_and_test(TERRAFORM / root)


@pytest.mark.terraform
@pytest.mark.parametrize(
    ("test_file", "run", "prefix"),
    [
        ("upgrade.tftest.hcl", "the_upgrade_forgets_them_without_revoking", ""),
        (
            "upgrade_child.tftest.hcl",
            "the_upgrade_forgets_them_inside_the_child_module",
            "module.snowflake.",
        ),
    ],
    ids=["root", "child"],
)
def test_snowflake_upgrade_forgets_the_analyst_grants_without_revoking(
    tmp_path, test_file, run, prefix
):
    """`terraform test` cannot assert on planned actions, so the upgrade file's verbose plan is
    read here: every earlier ANALYST grant leaves state, and nothing is destroyed, with the
    module as the root and as a thin root's child."""
    if shutil.which("terraform") is None:
        pytest.skip("terraform is not installed")
    module = tmp_path / "snowflake"
    shutil.copytree(SNOWFLAKE, module, ignore=shutil.ignore_patterns(".terraform*"))
    env = _terraform_env()
    steps = [
        ["init", "-backend=false", "-input=false"],
        ["test", f"-filter=tests/{test_file}", "-verbose"],
    ]
    for args in steps:
        done = subprocess.run(
            ["terraform", f"-chdir={module}", *args, "-no-color"],
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )
        assert done.returncode == 0, done.stdout + done.stderr
    plan = done.stdout.split(f'run "{run}"', 1)[1]
    kept = r"# (\S+) will no longer be managed by Terraform, but will not be destroyed"
    forgotten = set(re.findall(kept, plan))
    assert forgotten == {
        f'{prefix}snowflake_grant_privileges_to_account_role.analyst_schema["CORE"]',
        f'{prefix}snowflake_grant_privileges_to_account_role.analyst_schema["MARTS"]',
        *(
            f'{prefix}snowflake_grant_privileges_to_account_role.analyst_future["{schema}/{kind}"]'
            for schema in ("CORE", "MARTS")
            for kind in ("TABLES", "VIEWS")
        ),
    }
    assert re.search(r"Plan: \d+ to add, 0 to change, 0 to destroy\.", plan), plan
    assert "will be destroyed" not in plan


ROOT_TEST = """
mock_provider "aws" {
  override_data {
    target = module.environment.data.aws_availability_zones.available
    values = { names = ["eu-west-1a", "eu-west-1b"] }
  }
  override_data {
    target = module.environment.data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  override_data {
    target = module.environment.data.aws_region.current
    values = { region = "REGION", name = "REGION" }
  }
  override_data {
    target = module.environment.data.aws_ssm_parameter.al2023
    values = { value = "ami-0abc", insecure_value = "ami-0abc" }
  }
  override_data {
    target = module.environment.data.aws_route53_zone.this
    values = { zone_id = "Z123" }
  }
}

run "thin_root_plans_with_the_module_defaults" {
  command = plan

  assert {
    condition     = output.environment == "prod" && output.setup_host == "mcp"
    error_message = "the thin root re-exports the module outputs at the top level"
  }
  assert {
    condition     = output.secret_prefix == "vecten/acme/prod/"
    error_message = "the secret prefix comes through"
  }
  assert {
    condition     = keys(output.postgres) == WAREHOUSE_KEYS
    error_message = "the instances follow the warehouse"
  }
  assert {
    condition     = output.region == "REGION"
    error_message = "the region output is the provider's region"
  }
}
"""


@pytest.mark.terraform
@pytest.mark.parametrize("warehouse", ["postgres", "snowflake"])
def test_rendered_root_plans_against_the_module(tmp_path, warehouse):
    if shutil.which("terraform") is None:
        pytest.skip("terraform is not installed")
    root = _render(tmp_path, warehouse)
    main = root / "main.tf"
    main.write_text(re.sub(r'source = "[^"]+"', f'source = "{AWS}"', main.read_text()))
    (root / "backend.tf").unlink()
    keys = '["state"]' if warehouse == "snowflake" else '["state", "warehouse"]'
    tfvars = (root.parents[1] / "envs" / "prod.tfvars").read_text()
    region = re.search(r'^region\s*=\s*"([^"]+)"', tfvars, re.M)
    assert region
    (root / "terraform.tfvars").write_text(tfvars)
    test = ROOT_TEST.replace("WAREHOUSE_KEYS", keys).replace("REGION", region.group(1))
    (root / "tests").mkdir()
    (root / "tests" / "root.tftest.hcl").write_text(test)
    _init_and_test(root)


def test_outputs_match_the_shared_list():
    names = re.findall(r'^output "(\w+)"', (AWS / "outputs.tf").read_text(), re.M)
    assert len(names) == len(set(names))
    assert set(names) == set(OUTPUT_NAMES)


def test_module_holds_no_credential():
    forbidden = re.compile(
        r"random_password|aws_secretsmanager_secret_version|\bpassword\s*=|master_password"
    )
    files = _hcl_files(AWS)
    assert files
    for path in files:
        assert not forbidden.search(path.read_text(encoding="utf-8")), path
    postgres = (AWS / "modules" / "postgres" / "main.tf").read_text()
    assert re.search(r"manage_master_user_password\s*=\s*true", postgres)


def test_module_constraints_follow_versions():
    engine = f'required_version = "~> {VERSIONS.terraform}.0"'
    provider = f'version = "~> {VERSIONS.terraform_aws_provider}.0"'
    for versions in [AWS / "versions.tf", *sorted((AWS / "modules").glob("*/versions.tf"))]:
        text = versions.read_text()
        assert engine in text, versions
        assert provider in text, versions
        assert 'source  = "hashicorp/aws"' in text, versions


@pytest.mark.parametrize(
    ("module", "source", "minor"),
    [
        (AWS, "hashicorp/aws", VERSIONS.terraform_aws_provider),
        (SCALEWAY, "scaleway/scaleway", VERSIONS.terraform_scaleway_provider),
        (SCALEWAY, "hashicorp/random", VERSIONS.terraform_random_provider),
        (SNOWFLAKE, "snowflakedb/snowflake", VERSIONS.terraform_snowflake_provider),
    ],
)
def test_lock_file_pins_the_provider_minor(module, source, minor):
    lock = (module / ".terraform.lock.hcl").read_text()
    pinned = re.search(
        rf'provider "registry.terraform.io/{source}" \{{\s*version\s*=\s*"([^"]+)"', lock
    )
    assert pinned, f"the lock file names {source}"
    assert pinned.group(1).startswith(f"{minor}.")
    # CI runs on linux and people on macOS, so the lock holds a hash for each platform.
    (block,) = _blocks(lock, f'provider "registry.terraform.io/{source}"')
    assert len(re.findall(r'"h1:', block)) >= 4


def test_snowflake_constraints_follow_versions():
    text = (SNOWFLAKE / "versions.tf").read_text()
    assert f'required_version = "~> {VERSIONS.terraform}.0"' in text
    assert f'version = "~> {VERSIONS.terraform_snowflake_provider}.0"' in text
    assert 'source  = "snowflakedb/snowflake"' in text


def test_snowflake_module_holds_no_credential_and_no_provider():
    forbidden = re.compile(r"\bpassword\s*=|\bprivate_key\s*=|^provider \"", re.M)
    files = _hcl_files(SNOWFLAKE)
    assert files
    for path in files:
        if path.suffix == ".tf":
            assert not forbidden.search(path.read_text(encoding="utf-8")), path
    main = (SNOWFLAKE / "main.tf").read_text()
    assert re.search(r"rsa_public_key\s*=", main)
    assert 'jsondecode(file("${path.module}/sizing.json"))' in main


def _render_snowflake(tmp_path: Path, components: list[str]) -> Path:
    answers = {**LOCAL, **DEPLOY, "warehouse": "snowflake"}
    target = tmp_path / "snowflake-root"
    render_project(target, ScaffoldParams.from_answers(answers, {"components": components}))
    return target / "deploy" / "terraform" / "snowflake"


SNOWFLAKE_ROOT_VARS = {"database_name", "warehouse_name", "warehouse_size", "loader_public_key"}


def test_rendered_snowflake_root_passes_every_module_variable(tmp_path):
    root = _render_snowflake(tmp_path, ["platform", "mcp"])
    module_variables = _declared_variables(SNOWFLAKE / "variables.tf")
    main = (root / "main.tf").read_text()
    passed = _block_arguments(main, 'module "snowflake" {') - {"source"}
    assert passed == module_variables
    root_variables = _declared_variables(root / "variables.tf")
    assert SNOWFLAKE_ROOT_VARS <= root_variables
    assert re.search(r"^\s*account\s*=\s*var\.snowflake_account\s*$", main, re.M)
    for name in passed - {"account"}:
        assert re.search(rf"^\s*{name}\s*=\s*var\.{name}\s*$", main, re.M), name


def test_rendered_snowflake_root_configures_the_provider_from_the_environment_only(tmp_path):
    root = _render_snowflake(tmp_path, ["platform"])
    main = (root / "main.tf").read_text()
    # Credentials come from the environment only; the block holds the one setting no
    # environment variable can carry.
    assert _block_arguments(main, 'provider "snowflake" {') == {"preview_features_enabled"}
    assert 'preview_features_enabled = ["snowflake_current_role_datasource"]' in main
    assert 'source  = "snowflakedb/snowflake"' in main
    assert f'version = "~> {VERSIONS.terraform_snowflake_provider}.0"' in main
    # deploy sets these with -var on every plan; a tfvars entry would go stale when the key
    # is generated.
    tfvars = (root.parents[1] / "envs" / "prod.tfvars").read_text()
    for name in SNOWFLAKE_ROOT_VARS:
        assert name not in tfvars, name
        block = re.search(rf'variable "{name}" \{{(.*?)\}}', (root / "variables.tf").read_text())
        assert block and "default" not in block.group(1), name


SNOWFLAKE_ROOT_TEST = """
mock_provider "snowflake" {}

variables {
  database_name     = "ACME"
  warehouse_name    = "ACME_PROD_WH"
  warehouse_size    = "XSMALL"
  loader_public_key = "-----BEGIN PUBLIC KEY-----\\nMIIBIjANBgkqhkiG9w0B\\n-----END PUBLIC KEY-----"
}

run "thin_root_plans_against_the_module" {
  command = plan

  assert {
    condition     = module.snowflake.database == "ACME"
    error_message = "the thin root hands the database name to the module"
  }
  assert {
    condition     = module.snowflake.warehouse == "ACME_PROD_WH"
    error_message = "the thin root hands the warehouse name to the module"
  }
  assert {
    condition     = toset(module.snowflake.roles) == toset(["ANALYST", "LOADER", "PROVISIONER"])
    error_message = "the components come through from tfvars"
  }
}
"""


@pytest.mark.terraform
def test_rendered_snowflake_root_plans_against_the_module(tmp_path):
    if shutil.which("terraform") is None:
        pytest.skip("terraform is not installed")
    root = _render_snowflake(tmp_path, ["platform", "mcp"])
    main = root / "main.tf"
    main.write_text(re.sub(r'source = "[^"]+"', f'source = "{SNOWFLAKE}"', main.read_text()))
    (root / "backend.tf").unlink()
    tfvars = (root.parents[1] / "envs" / "prod.tfvars").read_text()
    tfvars = re.sub(
        r"^snowflake_account\s*=.*$", 'snowflake_account = "ACME-PROD"', tfvars, flags=re.M
    )
    (root / "terraform.tfvars").write_text(tfvars)
    (root / "tests").mkdir()
    (root / "tests" / "root.tftest.hcl").write_text(SNOWFLAKE_ROOT_TEST)
    _init_and_test(root)


def test_user_data_pins_follow_versions():
    locals_tf = (AWS / "locals.tf").read_text()
    compose = re.search(r'compose_plugin_version\s*=\s*"([^"]+)"', locals_tf)
    certbot = re.search(r'certbot_image\s*=\s*"([^"]+)"', locals_tf)
    buildx = re.search(r'buildx_plugin_version\s*=\s*"([^"]+)"', locals_tf)
    assert compose and compose.group(1) == VERSIONS.docker_compose_plugin
    assert buildx and buildx.group(1) == VERSIONS.docker_buildx_plugin
    assert certbot and certbot.group(1) == VERSIONS.certbot_image
    template = (AWS / "modules" / "host" / "user_data.sh.tftpl").read_text()
    assert "${compose_plugin_version}" in template
    assert "${buildx_plugin_version}" in template
    assert "${certbot_image}" in template


def test_module_decodes_the_sizing_slice():
    assert 'jsondecode(file("${path.module}/sizing.json"))' in (AWS / "locals.tf").read_text()


def _render(tmp_path: Path, warehouse: str) -> Path:
    answers = {**LOCAL, **DEPLOY, "warehouse": warehouse}
    params = ScaffoldParams.from_answers(answers, {"components": ["platform", "mcp"]})
    target = tmp_path / warehouse
    render_project(target, params)
    return target / "deploy" / "terraform" / "aws"


@pytest.mark.parametrize("warehouse", ["postgres", "snowflake"])
def test_rendered_root_passes_every_module_variable(tmp_path, warehouse):
    root = _render(tmp_path, warehouse)
    module_variables = _declared_variables(AWS / "variables.tf")
    assert OPTIONAL_VARIABLES <= module_variables
    main = (root / "main.tf").read_text()
    passed = _block_arguments(main, 'module "environment" {') - {"source"}
    expected = (
        module_variables if warehouse == "snowflake" else module_variables - {"snowflake_account"}
    )
    assert passed == expected
    root_variables = _declared_variables(root / "variables.tf")
    assert passed <= root_variables
    for name in passed:
        assert re.search(rf"^\s*{name}\s*=\s*var\.{name}\s*$", main, re.M), name


@pytest.mark.parametrize("warehouse", ["postgres", "snowflake"])
def test_rendered_root_leaves_optional_defaults_to_the_module(tmp_path, warehouse):
    root = _render(tmp_path, warehouse)
    variables = (root / "variables.tf").read_text()
    for name in OPTIONAL_VARIABLES:
        block = re.search(rf'variable "{name}" \{{(.*?)\n\}}', variables, re.S)
        assert block, name
        assert re.search(r"default\s*=\s*null", block.group(1)), name
    module_variables = (AWS / "variables.tf").read_text()
    for name in OPTIONAL_VARIABLES:
        block = re.search(rf'variable "{name}" \{{(.*?)\n\}}', module_variables, re.S)
        assert block, name
        assert re.search(r"nullable\s*=\s*false", block.group(1)), name


@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
def test_rendered_root_declares_the_ship_only_setting_without_passing_it(tmp_path, cloud):
    # `ship` reads dags_paused_at_creation from the tfvars; the module has no such input.
    root = (_render if cloud == "aws" else _render_scaleway)(tmp_path, "postgres")
    (block,) = _blocks((root / "variables.tf").read_text(), 'variable "dags_paused_at_creation"')
    assert re.search(r"type\s*=\s*bool", block) and re.search(r"default\s*=\s*null", block)
    main = (root / "main.tf").read_text()
    assert "dags_paused_at_creation" not in main
    for module in (AWS, SCALEWAY):
        assert "dags_paused_at_creation" not in _declared_variables(module / "variables.tf")


def test_module_leaves_the_provider_to_the_thin_root(tmp_path):
    for path in _hcl_files(AWS):
        if path.suffix == ".tf":
            assert not re.search(r'^provider "', path.read_text(), re.M), path
    main = (_render(tmp_path, "postgres") / "main.tf").read_text()
    assert re.search(r'provider "aws" \{\s*region = var\.region\b', main)
    assert re.search(r'"vecten:project"\s*=\s*var\.project_name', main)
    assert re.search(r'"vecten:env"\s*=\s*var\.environment', main)


def test_rendered_root_reexports_the_module_outputs(tmp_path):
    root = _render(tmp_path, "postgres")
    text = (root / "outputs.tf").read_text()
    names = re.findall(r'^output "(\w+)"', text, re.M)
    assert names == list(OUTPUT_NAMES)
    for name in OUTPUT_NAMES:
        assert re.search(rf"value\s*=\s*module\.environment\.{name}\b", text), name


# Scaleway: the same contract as AWS, plus the inputs bootstrap's facts feed.

SCALEWAY_OPTIONAL_VARIABLES = (OPTIONAL_VARIABLES - {"log_line_prefix"}) | {
    "state_admin_password_version",
    "warehouse_admin_password_version",
}


def test_scaleway_sends_no_log_line_prefix(tmp_path):
    # Managed Databases list log_line_prefix in neither their settings nor their init
    # settings, so an instance carrying it fails at apply.
    assert "log_line_prefix" not in _declared_variables(SCALEWAY / "variables.tf")
    for path in _module_code(SCALEWAY):
        assert "log_line_prefix" not in path.read_text(encoding="utf-8"), path
    root = _render_scaleway(tmp_path, "postgres")
    assert "log_line_prefix" not in (root / "main.tf").read_text()
    assert "log_line_prefix" not in (root / "variables.tf").read_text()


SCALEWAY_DEPLOY_VARIABLES = {"project_id", "application_ids", "dns_project_id"}
# Set by deploy when bootstrap recorded it; a project bootstrapped before that has none.
SCALEWAY_DEPLOY_OPTIONAL_VARIABLES = {"operator_principal"}


def _render_scaleway(tmp_path: Path, warehouse: str) -> Path:
    answers = {**LOCAL, **DEPLOY, "cloud": "scaleway", "warehouse": warehouse}
    params = ScaffoldParams.from_answers(answers, {"components": ["platform", "mcp"]})
    target = tmp_path / f"scw-{warehouse}"
    render_project(target, params)
    return target / "deploy" / "terraform" / "scaleway"


def _module_code(root: Path) -> list[Path]:
    return [p for p in _hcl_files(root) if "tests" not in p.relative_to(root).parts]


def test_scaleway_outputs_match_the_shared_list():
    names = re.findall(r'^output "(\w+)"', (SCALEWAY / "outputs.tf").read_text(), re.M)
    assert names == list(OUTPUT_NAMES)


@pytest.mark.parametrize("output", ["hosts", "postgres"])
def test_scaleway_outputs_carry_the_aws_fields(output):
    def fields(module: Path) -> list[str]:
        (block,) = _blocks((module / "outputs.tf").read_text(), f'output "{output}"')
        return re.findall(r"^\s+(\w+)\s+=", block.split("=>", 1)[1], re.M)

    assert fields(SCALEWAY) == fields(AWS)


def test_scaleway_module_holds_no_credential():
    forbidden = re.compile(
        r'\bpassword\s*=(?!=)|scaleway_iam_|secret_key|resource "random_password"|master_password'
    )
    files = _module_code(SCALEWAY)
    assert files
    for path in files:
        assert not forbidden.search(path.read_text(encoding="utf-8")), path
    code = "\n".join(p.read_text(encoding="utf-8") for p in files if p.suffix == ".tf")
    versions = _blocks(code, 'resource "scaleway_secret_version"')
    assert len(versions) == 1
    assert not re.search(r"^\s*data\s*=", versions[0], re.M)
    assert re.search(r"^\s*data_wo\s*=", versions[0], re.M)
    assert "ephemeral.random_password.admin[each.key].result" in versions[0]
    # A replaced instance takes a fresh password at create; the version must follow it.
    assert "replace_triggered_by = [terraform_data.admin_instance[each.key]]" in versions[0]
    (tracker,) = _blocks(code, 'resource "terraform_data" "admin_instance"')
    assert re.search(r"triggers_replace\s*=\s*module\.postgres\[each\.key\]\.id", tracker)
    assert re.search(r'^ephemeral "random_password" "admin"', code, re.M)
    (rdb,) = _blocks(code, 'resource "scaleway_rdb_instance"')
    assert re.search(r"^\s*password_wo\s*=\s*var\.admin_password\s*$", rdb, re.M)
    postgres_variables = (SCALEWAY / "modules" / "postgres" / "variables.tf").read_text()
    (admin_password,) = _blocks(postgres_variables, 'variable "admin_password"')
    assert re.search(r"ephemeral\s*=\s*true", admin_password)


def test_scaleway_module_constraints_follow_versions():
    engine = f'required_version = "~> {VERSIONS.terraform}.0"'
    scaleway = f'version = "~> {VERSIONS.terraform_scaleway_provider}.0"'
    random = f'version = "~> {VERSIONS.terraform_random_provider}.0"'
    root = (SCALEWAY / "versions.tf").read_text()
    assert random in root
    assert 'source  = "hashicorp/random"' in root
    modules = sorted((SCALEWAY / "modules").glob("*/versions.tf"))
    for versions in [SCALEWAY / "versions.tf", *modules]:
        text = versions.read_text()
        assert engine in text, versions
        assert scaleway in text, versions
        assert 'source  = "scaleway/scaleway"' in text, versions


def test_scaleway_user_data_pins_follow_versions():
    locals_tf = (SCALEWAY / "locals.tf").read_text()
    compose = re.search(r'compose_plugin_version\s*=\s*"([^"]+)"', locals_tf)
    buildx = re.search(r'buildx_plugin_version\s*=\s*"([^"]+)"', locals_tf)
    certbot = re.search(r'certbot_image\s*=\s*"([^"]+)"', locals_tf)
    assert compose and compose.group(1) == VERSIONS.docker_compose_plugin
    assert buildx and buildx.group(1) == VERSIONS.docker_buildx_plugin
    assert certbot and certbot.group(1) == VERSIONS.certbot_image
    template = (SCALEWAY / "modules" / "host" / "user_data.sh.tftpl").read_text()
    assert "${compose_plugin_version}" in template
    assert "${buildx_plugin_version}" in template
    assert "${certbot_image}" in template


def test_scaleway_engine_follows_the_postgres_major():
    main = (SCALEWAY / "modules" / "postgres" / "main.tf").read_text()
    assert f'engine        = "PostgreSQL-{VERSIONS.postgres_major}"' in main


def test_aws_engine_and_client_follow_the_postgres_major():
    major = VERSIONS.postgres_major
    main = (AWS / "modules" / "postgres" / "main.tf").read_text()
    assert f'engine_version = "{major}"' in main
    assert f'family = "postgres{major}"' in main
    assert f'"${{var.identifier}}-pg{major}"' in main
    user_data = (AWS / "modules" / "host" / "user_data.sh.tftpl").read_text()
    assert f"postgresql{major}" in user_data


def test_scaleway_module_decodes_the_sizing_slice():
    assert 'jsondecode(file("${path.module}/sizing.json"))' in (SCALEWAY / "locals.tf").read_text()


def test_bootstrap_agent_copy_is_the_package_agent():
    # User data writes this copy so the watcher runs before the first bundle; the bundle then
    # ships the package's agent, so the two must not drift.
    copy = SCALEWAY / "modules" / "host" / "agent" / "apply.py"
    assert copy.read_bytes() == AGENT.read_bytes()
    main = (SCALEWAY / "modules" / "host" / "main.tf").read_text()
    assert 'base64gzip(file("${path.module}/agent/apply.py"))' in main


def test_scaleway_module_leaves_the_provider_to_the_thin_root(tmp_path):
    for path in _module_code(SCALEWAY):
        if path.suffix == ".tf":
            assert not re.search(r'^provider "', path.read_text(), re.M), path
    main = (_render_scaleway(tmp_path, "postgres") / "main.tf").read_text()
    # Credentials come from the environment only.
    assert _block_arguments(main, 'provider "scaleway" {') == {"region", "zone", "project_id"}
    assert re.search(r'^\s*zone\s*=\s*"\$\{var\.region\}-1"\s*$', main, re.M)
    assert 'source  = "scaleway/scaleway"' in main
    assert f'version = "~> {VERSIONS.terraform_scaleway_provider}.0"' in main


@pytest.mark.parametrize("warehouse", ["postgres", "snowflake"])
def test_rendered_scaleway_root_passes_every_module_variable(tmp_path, warehouse):
    root = _render_scaleway(tmp_path, warehouse)
    module_variables = _declared_variables(SCALEWAY / "variables.tf")
    assert (
        SCALEWAY_OPTIONAL_VARIABLES | SCALEWAY_DEPLOY_VARIABLES | SCALEWAY_DEPLOY_OPTIONAL_VARIABLES
        <= module_variables
    )
    main = (root / "main.tf").read_text()
    passed = _block_arguments(main, 'module "environment" {') - {"source"}
    expected = (
        module_variables if warehouse == "snowflake" else module_variables - {"snowflake_account"}
    )
    assert passed == expected
    assert passed <= _declared_variables(root / "variables.tf")
    for name in passed:
        assert re.search(rf"^\s*{name}\s*=\s*var\.{name}\s*$", main, re.M), name


def test_rendered_scaleway_root_leaves_optional_defaults_to_the_module(tmp_path):
    root = _render_scaleway(tmp_path, "postgres")
    variables = (root / "variables.tf").read_text()
    module_variables = (SCALEWAY / "variables.tf").read_text()
    for name in SCALEWAY_OPTIONAL_VARIABLES:
        (block,) = _blocks(variables, f'variable "{name}"')
        assert re.search(r"default\s*=\s*null", block), name
        (block,) = _blocks(module_variables, f'variable "{name}"')
        assert re.search(r"nullable\s*=\s*false", block), name


def test_rendered_scaleway_root_takes_the_bootstrap_ids_from_deploy_only(tmp_path):
    root = _render_scaleway(tmp_path, "postgres")
    variables = (root / "variables.tf").read_text()
    # deploy sets these with -var from vecten.toml facts; a missing one fails the plan rather
    # than falling back to a default.
    for name in sorted(SCALEWAY_DEPLOY_VARIABLES):
        line = re.search(rf'^variable "{name}" \{{([^\n]*)\}}$', variables, re.M)
        assert line and "default" not in line.group(1), name
    for name in sorted(SCALEWAY_DEPLOY_OPTIONAL_VARIABLES):
        (block,) = _blocks(variables, f'variable "{name}"')
        assert re.search(r"default\s*=\s*null", block), name
    tfvars = (root.parents[1] / "envs" / "prod.tfvars").read_text()
    for name in SCALEWAY_DEPLOY_VARIABLES | SCALEWAY_DEPLOY_OPTIONAL_VARIABLES:
        assert name not in tfvars, name


@pytest.mark.parametrize("root_name", ["scaleway", "snowflake"])
def test_rendered_scaleway_backends_point_at_object_storage(tmp_path, root_name):
    root = _render_scaleway(tmp_path, "snowflake").parent / root_name
    backend = (root / "backend.tf").read_text()
    assert _block_arguments(backend, 'backend "s3" {') == {
        "region",
        "endpoints",
        "use_path_style",
        "skip_credentials_validation",
        "skip_region_validation",
        "skip_requesting_account_id",
        "skip_metadata_api_check",
        "use_lockfile",
    }
    assert 'region                      = "fr-par"' in backend
    assert 'endpoints                   = { s3 = "https://s3.fr-par.scw.cloud" }' in backend
    for flag in ("use_path_style", "use_lockfile", "skip_metadata_api_check"):
        assert re.search(rf"^\s*{flag}\s*=\s*true$", backend, re.M), flag


def test_rendered_scaleway_root_reexports_the_module_outputs(tmp_path):
    text = (_render_scaleway(tmp_path, "postgres") / "outputs.tf").read_text()
    assert re.findall(r'^output "(\w+)"', text, re.M) == list(OUTPUT_NAMES)


SCALEWAY_ROOT_TEST = """
mock_provider "scaleway" {}

variables {
  project_id     = "1a2b3c4d-2222-4333-8444-555555555555"
  dns_project_id = "9a9b9c9d-2222-4333-8444-555555555555"
  application_ids = {
    deploy   = "aaaaaaaa-0000-4000-8000-000000000001"
    plan     = "aaaaaaaa-0000-4000-8000-000000000002"
    platform = "aaaaaaaa-0000-4000-8000-000000000003"
    mcp      = "aaaaaaaa-0000-4000-8000-000000000004"
  }
}

run "thin_root_plans_with_the_module_defaults" {
  command = plan

  assert {
    condition     = output.environment == "prod" && output.setup_host == "mcp"
    error_message = "the thin root re-exports the module outputs at the top level"
  }
  assert {
    condition     = output.secret_prefix == "/vecten/acme/prod/"
    error_message = "the secret prefix comes through"
  }
  assert {
    condition     = keys(output.postgres) == WAREHOUSE_KEYS
    error_message = "the instances follow the warehouse"
  }
  assert {
    condition     = output.region == "fr-par"
    error_message = "the region reaches the module"
  }
  assert {
    condition     = output.bucket == "vecten-acme-prod-artifacts-1a2b3c4d"
    error_message = "the project ID reaches the module"
  }
}
"""


@pytest.mark.terraform
@pytest.mark.parametrize("warehouse", ["postgres", "snowflake"])
def test_rendered_scaleway_root_validates_and_plans_against_the_module(tmp_path, warehouse):
    if shutil.which("terraform") is None:
        pytest.skip("terraform is not installed")
    root = _render_scaleway(tmp_path, warehouse)
    main = root / "main.tf"
    main.write_text(re.sub(r'source = "[^"]+"', f'source = "{SCALEWAY}"', main.read_text()))
    (root / "backend.tf").unlink()
    tfvars = (root.parents[1] / "envs" / "prod.tfvars").read_text()
    (root / "terraform.tfvars").write_text(tfvars)
    keys = '["state"]' if warehouse == "snowflake" else '["state", "warehouse"]'
    (root / "tests").mkdir()
    (root / "tests" / "root.tftest.hcl").write_text(
        SCALEWAY_ROOT_TEST.replace("WAREHOUSE_KEYS", keys)
    )
    _init_and_test(root, validate=True)


def _init_and_validate(directory: Path) -> None:
    env = _terraform_env()
    for args in (["init", "-backend=false", "-input=false"], ["validate"]):
        done = subprocess.run(
            ["terraform", f"-chdir={directory}", *args, "-no-color"],
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )
        assert done.returncode == 0, done.stdout + done.stderr


DEPLOY_FORMS = [p for p in MATRIX if p.render_deploy]
LOCAL_MODULES = {"aws": AWS, "scaleway": SCALEWAY, "snowflake": SNOWFLAKE}


@pytest.mark.terraform
@pytest.mark.parametrize("params", DEPLOY_FORMS, ids=label)
def test_every_cloud_render_validates(tmp_path, params):
    """Every root of a deployed form, its backend block included, against the modules in
    this checkout, since the release tag the roots name exists only once released."""
    if shutil.which("terraform") is None:
        pytest.skip("terraform is not installed")
    target = tmp_path / "proj"
    render_project(target, params)
    snowflake = params.warehouse == "snowflake" and "platform" in params.components
    roots = sorted((target / "deploy" / "terraform").iterdir())
    assert [r.name for r in roots] == [params.cloud, *(["snowflake"] if snowflake else [])]
    for root in roots:
        main = root / "main.tf"
        local = LOCAL_MODULES[root.name]
        main.write_text(re.sub(r'source = "git::[^"]+"', f'source = "{local}"', main.read_text()))
        assert (root / "backend.tf").is_file()
        _init_and_validate(root)
