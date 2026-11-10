import pytest

from vecten.scaffold.params import ScaffoldParams
from vecten_shared.output import ExitCode, Failure

LOCAL = {"name": "acme", "users": 8, "sources": ["hubspot"]}
DEPLOY = {
    "cloud": "aws",
    "domain": "data.acme.com",
    "dns_zone": "acme.com",
    "github_repository": "acme/data",
}


def test_defaults_from_local_answers():
    p = ScaffoldParams.from_answers(LOCAL, {})
    assert p.warehouse == "postgres" and p.size == "small" and p.components == ["platform", "mcp"]
    assert p.profile == "standard" and p.render_deploy is False and p.region is None


def test_region_defaults_per_cloud():
    p = ScaffoldParams.from_answers({**LOCAL, **DEPLOY}, {})
    assert p.region == "eu-central-1" and p.render_deploy is True
    q = ScaffoldParams.from_answers({**LOCAL, **DEPLOY, "cloud": "scaleway"}, {})
    assert q.region == "fr-par"


def test_size_derived_and_overridable():
    assert ScaffoldParams.from_answers({**LOCAL, "users": 30}, {}).size == "medium"
    assert ScaffoldParams.from_answers({**LOCAL, "users": 30}, {"size": "large"}).size == "large"


def test_missing_local_answers_named_together():
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers({}, {})
    assert exc.value.code == ExitCode.USAGE
    assert exc.value.details["missing"] == ["name", "users", "sources or warehouse_url"]


def test_empty_string_answers_count_as_unset():
    p = ScaffoldParams.from_answers({**LOCAL, "cloud": "", "region": "", "domain": ""}, {})
    assert p.cloud is None and p.region is None and p.render_deploy is False
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers({"name": "", "users": 8, "sources": ["hubspot"]}, {})
    assert exc.value.details["missing"] == ["name"]


def test_existing_warehouse_forces_mcp_only():
    p = ScaffoldParams.from_answers(
        {
            "name": "acme",
            "users": 5,
            "has_warehouse": "yes",
            "warehouse_url": "postgres://h:5432/dw",
        },
        {},
    )
    assert p.components == ["mcp"] and p.has_warehouse is True and p.warehouse == "postgres"
    assert p.warehouse_locator.host == "h" and p.warehouse_locator.existing is True


def test_existing_warehouse_refuses_sources():
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers(
            {
                "name": "acme",
                "users": 5,
                "has_warehouse": "yes",
                "warehouse_url": "postgres://h/dw",
                "sources": ["hubspot"],
            },
            {},
        )
    assert "platform" in exc.value.message


def test_existing_warehouse_requires_url():
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers({"name": "acme", "users": 5, "has_warehouse": "yes"}, {})
    assert "warehouse_url" in exc.value.message


def test_existing_warehouse_without_url_named_with_other_missing():
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers({"has_warehouse": "yes"}, {})
    assert exc.value.code == ExitCode.USAGE
    assert exc.value.details["missing"] == ["name", "users", "warehouse_url"]


def test_warehouse_url_alone_infers_existing_warehouse():
    p = ScaffoldParams.from_answers(
        {"name": "acme", "users": 5, "warehouse_url": "snowflake://ab12345/analytics"}, {}
    )
    assert p.has_warehouse is True and p.components == ["mcp"] and p.warehouse == "snowflake"


def test_unrecognised_has_warehouse_answer_refused():
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers({**LOCAL, "has_warehouse": "y"}, {})
    assert exc.value.code == ExitCode.USAGE
    assert "yes, no" in exc.value.message


def test_warehouse_url_with_has_warehouse_no_refused():
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers(
            {**LOCAL, "has_warehouse": "no", "warehouse_url": "postgres://h/dw"}, {}
        )
    assert exc.value.code == ExitCode.USAGE
    assert "has_warehouse yes" in exc.value.message


def test_non_numeric_users_refused():
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers({**LOCAL, "users": "abc"}, {})
    assert exc.value.code == ExitCode.USAGE
    assert exc.value.message == "users must be a positive integer"


def test_project_name_must_be_a_slug():
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers({**LOCAL, "name": "Acme Analytics"}, {})
    assert exc.value.code == ExitCode.USAGE
    assert "lowercase letters, digits, hyphens or underscores" in exc.value.message
    assert ScaffoldParams.from_answers({**LOCAL, "name": "acme-data_2"}, {}).project_name


@pytest.mark.parametrize(
    "field,bad,allowed",
    [
        ("cloud", "digitalocean", "aws, scaleway"),
        ("warehouse", "bigquery", "postgres, snowflake"),
        ("components", ["bi"], "platform, mcp"),
        ("profile", "huge", "standard, minimal"),
    ],
)
def test_unsupported_values_list_allowed(field, bad, allowed):
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers({**LOCAL, **DEPLOY, field: bad}, {})
    assert allowed in exc.value.message


def test_copier_data_has_only_scaffold_parameters_and_derived():
    data = ScaffoldParams.from_answers({**LOCAL, **DEPLOY}, {}).copier_data()
    assert set(data) == {
        "cloud",
        "region",
        "warehouse",
        "project_name",
        "domain",
        "dns_zone",
        "github_repository",
        "snowflake_account",
        "users",
        "sources",
        "size",
        "components",
        "profile",
        "has_warehouse",
        "warehouse_url",
        "render_deploy",
        "vecten_version",
        "airflow_image",
        "postgres_major",
        "terraform_module_ref",
        "terraform_engine",
        "terraform_snowflake_provider",
        "terraform_scaleway_provider",
        "github_runner",
        "ruff_version",
        "action_checkout",
        "action_setup_uv",
        "action_setup_terraform",
        "action_configure_aws_credentials",
        "convention_version",
        "ossie_spec",
        "warehouse_host",
        "warehouse_port",
        "warehouse_database",
        "warehouse_account",
        "warehouse_admin_user",
    }


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("region", "eu-central-1\nx"),
        ("domain", "data.acme.com (our subdomain)"),
        ("dns_zone", "acme.com;"),
        ("github_repository", "-acme/data"),
    ],
)
def test_deploy_answers_validated_at_scaffold(key, bad):
    with pytest.raises(Failure) as exc:
        ScaffoldParams.from_answers({**LOCAL, **DEPLOY, key: bad}, {})
    assert exc.value.code == ExitCode.USAGE and key in exc.value.message
