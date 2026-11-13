import pytest

from vecten.deploy.tfvars import check_tfvars, env_tfvars_path, read_tfvars
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config, save_config
from vecten_shared.output import ExitCode, Failure

from .scaffold_matrix import DEPLOY, LOCAL

SECOND = "a second warehouse is a second deployment"


def render(tmp_path, warehouse="postgres", cloud="aws", **overrides):
    answers = {**LOCAL, "sources": ["hubspot"], "warehouse": warehouse, **DEPLOY, "cloud": cloud}
    root = tmp_path / "proj"
    render_project(root, ScaffoldParams.from_answers(answers, overrides))
    return root


def replace_line(root, env, key, line):
    path = env_tfvars_path(root, env)
    lines = path.read_text().splitlines()
    out = [line if ln.split("=")[0].strip() == key else ln for ln in lines]
    if line is None:
        out = [ln for ln in out if ln is not None]
    path.write_text("\n".join(out) + "\n")


def refusal(root, env="prod"):
    with pytest.raises(Failure) as exc:
        check_tfvars(root, load_config(root), env)
    return exc.value


@pytest.fixture
def project(tmp_path):
    return render(tmp_path)


def test_rendered_prod_tfvars_passes(project):
    values = check_tfvars(project, load_config(project), "prod")
    assert values["environment"] == "prod"
    assert values["components"] == ["platform", "mcp"]
    assert values["domain"] == "data.acme.com"


def test_env_tfvars_path(tmp_path):
    assert env_tfvars_path(tmp_path, "staging") == tmp_path / "deploy" / "envs" / "staging.tfvars"


def test_removed_domain_is_named_missing(project):
    replace_line(project, "prod", "domain", None)
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG
    assert failure.details["missing"] == ["domain"]
    assert "domain" in failure.message


def test_empty_value_counts_as_missing(project):
    replace_line(project, "prod", "dns_zone", 'dns_zone = ""')
    assert refusal(project).details["missing"] == ["dns_zone"]


def test_non_json_value_refused_with_its_line_number(project):
    path = env_tfvars_path(project, "prod")
    lines = path.read_text().splitlines()
    index = next(i for i, ln in enumerate(lines) if ln.startswith("region"))
    lines[index] = "region = eu-central-1"
    path.write_text("\n".join(lines) + "\n")
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG
    assert f"line {index + 1}" in failure.message


def test_line_without_equals_refused(tmp_path):
    path = tmp_path / "x.tfvars"
    path.write_text('# comment\n\nsize = "small"\nnot a pair\n')
    with pytest.raises(Failure) as exc:
        read_tfvars(path)
    assert exc.value.code == ExitCode.CONFIG and "line 4" in exc.value.message


def test_read_tfvars_values(tmp_path):
    path = tmp_path / "x.tfvars"
    path.write_text(
        "# leading comment\n"
        'name = "a # not a comment"  # trailing comment\n'
        'list = ["platform", "mcp"]\n'
        "count = 3\n"
        "flag = true\n"
        'overrides = {"platform": "t3.large"}\n'
    )
    assert read_tfvars(path) == {
        "name": "a # not a comment",
        "list": ["platform", "mcp"],
        "count": 3,
        "flag": True,
        "overrides": {"platform": "t3.large"},
    }


def test_duplicate_key_refused(tmp_path):
    path = tmp_path / "x.tfvars"
    path.write_text('size = "small"\nsize = "large"\n')
    with pytest.raises(Failure) as exc:
        read_tfvars(path)
    assert "line 2" in exc.value.message and "size" in exc.value.message


def test_environment_must_equal_the_file_stem(project):
    replace_line(project, "prod", "environment", 'environment = "staging"')
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG
    assert "'staging'" in failure.message and "'prod'" in failure.message


def test_profile_differing_from_vecten_toml_names_both(project):
    replace_line(project, "prod", "profile", 'profile = "minimal"')
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG
    assert "'minimal'" in failure.message and "'standard'" in failure.message


def test_components_differing_from_vecten_toml_names_both(project):
    replace_line(project, "prod", "components", 'components = ["mcp"]')
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG
    assert "['mcp']" in failure.message and "['platform', 'mcp']" in failure.message


def test_components_order_does_not_matter(project):
    replace_line(project, "prod", "components", 'components = ["mcp", "platform"]')
    check_tfvars(project, load_config(project), "prod")


def test_project_name_differing_from_vecten_toml_refused(project):
    replace_line(project, "prod", "project_name", 'project_name = "other"')
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG and "'other'" in failure.message


def test_size_may_differ_but_must_be_a_tier(project):
    replace_line(project, "prod", "size", 'size = "large"')
    assert check_tfvars(project, load_config(project), "prod")["size"] == "large"
    replace_line(project, "prod", "size", 'size = "huge"')
    assert refusal(project).code == ExitCode.CONFIG


def test_snowflake_account_on_postgres_is_a_second_warehouse(project):
    path = env_tfvars_path(project, "prod")
    path.write_text(path.read_text() + 'snowflake_account = "ACME-PROD"\n')
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG and SECOND in failure.message


def test_warehouse_differing_from_vecten_toml_is_a_second_warehouse(project):
    replace_line(project, "prod", "warehouse", 'warehouse = "snowflake"')
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG and SECOND in failure.message
    assert "'snowflake'" in failure.message and "'postgres'" in failure.message


def set_account(root, account):
    config = load_config(root)
    config.warehouse.account = account
    save_config(root, config)


def test_snowflake_project_needs_its_account(tmp_path):
    root = render(tmp_path, warehouse="snowflake")
    failure = refusal(root)
    assert failure.details["missing"] == ["snowflake_account"]
    assert "vecten answer snowflake_account" in failure.message
    assert "vecten init --deploy" in failure.message
    replace_line(root, "prod", "snowflake_account", 'snowflake_account = "ACME-PROD"')
    set_account(root, "ACME-PROD")
    assert check_tfvars(root, load_config(root), "prod")["snowflake_account"] == "ACME-PROD"


def test_snowflake_account_must_match_vecten_toml(tmp_path):
    root = render(tmp_path, warehouse="snowflake")
    replace_line(root, "prod", "snowflake_account", 'snowflake_account = "ACME-PROD"')
    set_account(root, "ACME-OTHER")
    failure = refusal(root)
    assert failure.code == ExitCode.CONFIG
    assert "'ACME-PROD'" in failure.message and "'ACME-OTHER'" in failure.message
    assert "snowflake_account" in failure.message and "[warehouse] account" in failure.message


@pytest.mark.parametrize("account", [None, ""])
def test_snowflake_project_without_an_account_in_vecten_toml_is_refused(tmp_path, account):
    root = render(tmp_path, warehouse="snowflake")
    replace_line(root, "prod", "snowflake_account", 'snowflake_account = "ACME-PROD"')
    set_account(root, account)
    failure = refusal(root)
    assert failure.code == ExitCode.CONFIG
    assert "[warehouse] account" in failure.message and "'ACME-PROD'" in failure.message


def test_absent_env_file_refused_naming_it(project):
    failure = refusal(project, "nosuch")
    assert failure.code == ExitCode.REFUSED
    assert "deploy/envs/nosuch.tfvars" in failure.message


def test_local_env_refused_by_the_env_name_rule(project):
    failure = refusal(project, "local")
    assert failure.code == ExitCode.USAGE and "reserved" in failure.message


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "[1, NaN]"])
def test_non_finite_numbers_refused_with_line(tmp_path, value):
    path = tmp_path / "x.tfvars"
    path.write_text(f'size = "small"\ncount = {value}\n')
    with pytest.raises(Failure) as exc:
        read_tfvars(path)
    assert exc.value.code == ExitCode.CONFIG and "line 2" in exc.value.message


@pytest.mark.parametrize(
    "value",
    ['"${var.x}"', '"a%{ if true }b%{ endif }"', '["ok", "${x}"]', '{"k": "${x}"}'],
)
def test_hcl_template_sequences_refused_with_line(tmp_path, value):
    path = tmp_path / "x.tfvars"
    path.write_text(f"# c\nname = {value}\n")
    with pytest.raises(Failure) as exc:
        read_tfvars(path)
    assert exc.value.code == ExitCode.CONFIG and "line 2" in exc.value.message
    assert "template" in exc.value.message


def test_dollar_without_brace_is_plain_text(tmp_path):
    path = tmp_path / "x.tfvars"
    path.write_text('name = "costs $5 and 50%"\n')
    assert read_tfvars(path) == {"name": "costs $5 and 50%"}


def test_missing_prod_file_names_init(project):
    env_tfvars_path(project, "prod").unlink()
    failure = refusal(project)
    assert failure.code == ExitCode.REFUSED
    assert "deploy/envs/prod.tfvars" in failure.message
    assert "vecten init --deploy" in failure.message
    assert "copy" not in failure.message


def test_copied_staging_file_passes(project):
    text = env_tfvars_path(project, "prod").read_text()
    text = text.replace('environment       = "prod"', 'environment       = "staging"')
    env_tfvars_path(project, "staging").write_text(text)
    assert check_tfvars(project, load_config(project), "staging")["environment"] == "staging"


def test_missing_staging_file_says_to_change_environment_and_domain(project):
    failure = refusal(project, "staging")
    assert failure.code == ExitCode.REFUSED
    assert 'environment = "staging"' in failure.message
    assert "domain" in failure.message


def test_prod_domain_must_equal_the_vecten_toml_domain(project):
    replace_line(project, "prod", "domain", 'domain = "other.acme.com"')
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG
    assert "'other.acme.com'" in failure.message and "'data.acme.com'" in failure.message
    assert "[deploy] domain" in failure.message


def test_a_second_environment_may_use_its_own_domain(project):
    text = env_tfvars_path(project, "prod").read_text()
    text = text.replace('environment       = "prod"', 'environment       = "staging"')
    text = text.replace('"data.acme.com"', '"staging.acme.com"')
    env_tfvars_path(project, "staging").write_text(text)
    values = check_tfvars(project, load_config(project), "staging")
    assert values["domain"] == "staging.acme.com"


def test_scaleway_refuses_a_tfvars_region_other_than_the_rendered_one(tmp_path):
    root = render(tmp_path, cloud="scaleway")
    rendered = load_config(root).deploy.region
    other = "nl-ams" if rendered != "nl-ams" else "pl-waw"
    replace_line(root, "prod", "region", f'region = "{other}"')
    failure = refusal(root)
    assert failure.code == ExitCode.CONFIG
    assert repr(other) in failure.message and repr(rendered) in failure.message
    assert "[deploy] region" in failure.message


def test_scaleway_accepts_the_rendered_region(tmp_path):
    root = render(tmp_path, cloud="scaleway")
    assert (
        check_tfvars(root, load_config(root), "prod")["region"] == load_config(root).deploy.region
    )


def test_aws_keeps_its_tfvars_region_unchecked(project):
    replace_line(project, "prod", "region", 'region = "eu-west-3"')
    assert check_tfvars(project, load_config(project), "prod")["region"] == "eu-west-3"


def test_dags_paused_at_creation_is_optional_and_a_bool(project):
    assert "dags_paused_at_creation" not in check_tfvars(project, load_config(project), "prod")
    path = env_tfvars_path(project, "prod")
    rendered = path.read_text()
    path.write_text(rendered + "dags_paused_at_creation = true\n")
    assert check_tfvars(project, load_config(project), "prod")["dags_paused_at_creation"] is True


@pytest.mark.parametrize("value", ['"true"', "1", "[true]"])
def test_dags_paused_at_creation_refuses_anything_but_true_or_false(project, value):
    path = env_tfvars_path(project, "prod")
    path.write_text(path.read_text() + f"dags_paused_at_creation = {value}\n")
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG
    assert "dags_paused_at_creation" in failure.message and "true or false" in failure.message


def _staging(project, domain):
    text = env_tfvars_path(project, "prod").read_text()
    text = text.replace('environment       = "prod"', 'environment       = "staging"')
    text = text.replace('"data.acme.com"', f'"{domain}"')
    env_tfvars_path(project, "staging").write_text(text)


def _bind_apex(project):
    from .services_fixtures import with_services

    with_services(project, {"bot": {"hostname": "@", "routes": {"webhook": {"access": "public"}}}})


def test_the_apex_is_refused_where_an_environments_domain_is_its_zone(project):
    # vecten.toml's own domain sits below its zone; staging serves at the zone itself.
    _bind_apex(project)
    _staging(project, "acme.com")
    failure = refusal(project, "staging")
    assert failure.code == ExitCode.CONFIG
    assert "deploy/envs/staging.tfvars" in failure.message
    assert "services.acme.bot.hostname" in failure.message and "@" in failure.message


def test_an_apps_apex_is_refused_where_an_environments_domain_is_its_zone(project):
    from .services_fixtures import with_apps

    with_apps(project, {"overview": {"groups": ["default"], "hostname": "@"}})
    _staging(project, "acme.com")
    failure = refusal(project, "staging")
    assert failure.code == ExitCode.CONFIG
    assert "apps.acme.overview.hostname" in failure.message and "@" in failure.message


def test_the_apex_is_admitted_where_the_environments_domain_sits_below_its_zone(project):
    _bind_apex(project)
    _staging(project, "staging.acme.com")
    assert check_tfvars(project, load_config(project), "staging")["domain"] == "staging.acme.com"


def test_tfvars_may_not_set_service_hostnames(project):
    path = env_tfvars_path(project, "prod")
    path.write_text(path.read_text() + 'service_hostnames = {"bot": "platform"}\n')
    failure = refusal(project)
    assert failure.code == ExitCode.CONFIG
    assert "service_hostnames" in failure.message and "vecten.toml" in failure.message
