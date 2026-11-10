import pytest
import yaml

import vecten.intake.answers as answers_module
from vecten.intake.answers import ANSWER_KEYS, read_answers, validate_answer, write_answer
from vecten_shared.output import ExitCode, Failure


def test_keys_cover_the_intake():
    assert set(ANSWER_KEYS) == {
        "has_warehouse",
        "sources",
        "warehouse",
        "warehouse_url",
        "users",
        "name",
        "cloud",
        "region",
        "domain",
        "dns_zone",
        "github_repository",
        "snowflake_account",
    }
    assert ANSWER_KEYS["has_warehouse"].question == "Do you already have a data warehouse?"
    assert ANSWER_KEYS["cloud"].scope == "deploy" and ANSWER_KEYS["users"].scope == "local"


def test_write_and_read(tmp_path):
    write_answer(tmp_path, "cloud", "aws")
    write_answer(tmp_path, "sources", "hubspot,stripe")
    write_answer(tmp_path, "users", "30")
    assert yaml.safe_load((tmp_path / "vecten-answers.yml").read_text()) == {
        "cloud": "aws",
        "sources": ["hubspot", "stripe"],
        "users": 30,
    }
    assert read_answers(tmp_path)["users"] == 30


def test_sources_none_means_empty_list(tmp_path):
    write_answer(tmp_path, "sources", "none")
    assert read_answers(tmp_path)["sources"] == []


def test_invalid_choice_lists_allowed_and_leaves_file_unchanged(tmp_path):
    write_answer(tmp_path, "cloud", "aws")
    before = (tmp_path / "vecten-answers.yml").read_text()
    with pytest.raises(Failure) as exc:
        write_answer(tmp_path, "cloud", "digitalocean")
    assert exc.value.code == ExitCode.USAGE
    assert "aws" in exc.value.message and "scaleway" in exc.value.message
    assert (tmp_path / "vecten-answers.yml").read_text() == before


def test_unknown_key_refused(tmp_path):
    with pytest.raises(Failure) as exc:
        write_answer(tmp_path, "colour", "blue")
    assert exc.value.code == ExitCode.USAGE and "has_warehouse" in exc.value.message
    assert "access.<connector>" in exc.value.message


def test_access_key_points_at_the_declaration_and_writes_nothing(tmp_path):
    with pytest.raises(Failure) as exc:
        write_answer(tmp_path, "access.hubspot", "sales")
    assert exc.value.code == ExitCode.USAGE and "vecten.toml" in exc.value.message
    assert not (tmp_path / "vecten-answers.yml").exists()


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "2.5"])
def test_users_must_be_positive_integer(raw):
    with pytest.raises(Failure) as exc:
        validate_answer("users", raw)
    assert exc.value.code == ExitCode.USAGE and "users" in exc.value.message


def test_warehouse_url_password_refused_at_answer():
    with pytest.raises(Failure) as exc:
        validate_answer("warehouse_url", "postgres://a:b@h/d")
    assert "secret set WAREHOUSE_ADMIN_PASSWORD" in exc.value.message


def test_unparseable_answers_file_is_config_error(tmp_path):
    (tmp_path / "vecten-answers.yml").write_text("cloud: [unclosed\n")
    with pytest.raises(Failure) as exc:
        read_answers(tmp_path)
    assert exc.value.code == ExitCode.CONFIG and "vecten-answers.yml" in exc.value.message


def test_answers_file_written_atomically_as_utf8(tmp_path, monkeypatch):
    calls = []
    real = answers_module.write_text_atomic

    def spy(path, text):
        calls.append(path)
        real(path, text)

    monkeypatch.setattr(answers_module, "write_text_atomic", spy)
    write_answer(tmp_path, "name", "Café Données")
    assert calls == [tmp_path / "vecten-answers.yml"]
    assert read_answers(tmp_path)["name"] == "Café Données"


@pytest.mark.parametrize("raw", ["", "   ", " , "])
def test_sources_empty_value_refused(raw):
    with pytest.raises(Failure) as exc:
        validate_answer("sources", raw)
    assert exc.value.code == ExitCode.USAGE and "sources" in exc.value.message


@pytest.mark.parametrize("raw", ["none", "None", " NONE ", "start empty", "Start Empty"])
def test_sources_none_and_start_empty_case_insensitive(raw):
    assert validate_answer("sources", raw) == []


@pytest.mark.parametrize("raw", ["none,hubspot", "hubspot, None", "start empty,stripe"])
def test_sources_none_mixed_with_entries_refused(raw):
    with pytest.raises(Failure) as exc:
        validate_answer("sources", raw)
    assert exc.value.code == ExitCode.USAGE


@pytest.mark.parametrize("raw", ["+5", "1_000", "٣", " 7x", "1e3"])
def test_users_refuses_non_ascii_digit_forms(raw):
    with pytest.raises(Failure) as exc:
        validate_answer("users", raw)
    assert exc.value.code == ExitCode.USAGE


def test_users_accepts_surrounding_whitespace():
    assert validate_answer("users", " 12 ") == 12


def test_warehouse_url_is_stripped():
    assert validate_answer("warehouse_url", "  postgres://admin@db.example.com/analytics \n") == (
        "postgres://admin@db.example.com/analytics"
    )


@pytest.mark.parametrize("content", ["0\n", "false\n", '""\n', "[]\n", "- a\n", "just text\n"])
def test_non_mapping_answers_file_is_config_error(tmp_path, content):
    (tmp_path / "vecten-answers.yml").write_text(content)
    with pytest.raises(Failure) as exc:
        read_answers(tmp_path)
    assert exc.value.code == ExitCode.CONFIG


@pytest.mark.parametrize("content", ["", "\n", "# only a comment\n"])
def test_empty_answers_file_reads_as_no_answers(tmp_path, content):
    (tmp_path / "vecten-answers.yml").write_text(content)
    assert read_answers(tmp_path) == {}


BAD_DEPLOY_ANSWERS = [
    ("region", "eu-central-1\nfoo"),
    ("region", "eu-central-1;rm"),
    ("region", 'eu"1'),
    ("region", "x"),
    ("domain", "data.acme.com (our subdomain)"),
    ("domain", "data.acme.com\nevil: 1"),
    ("domain", "acme"),
    ("domain", "-data.acme.com"),
    ("domain", "Data.Acme.com"),
    ("domain", "a" * 64 + ".com"),
    ("dns_zone", "acme.com;"),
    ("github_repository", "-acme/data"),
    ("github_repository", "acme/data;x"),
    ("github_repository", "acme"),
    ("github_repository", "acme/data\nx"),
]


@pytest.mark.parametrize(("key", "raw"), BAD_DEPLOY_ANSWERS)
def test_deploy_answers_refuse_values_outside_their_rule(key, raw):
    with pytest.raises(Failure) as exc:
        validate_answer(key, raw)
    assert exc.value.code == ExitCode.USAGE and exc.value.message.startswith(f"{key} must be")


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("region", "eu-central-1"),
        ("region", "fr-par"),
        ("domain", "data.acme.com"),
        ("domain", "x1.data-eu.acme.co.uk"),
        ("dns_zone", "acme.com"),
        ("github_repository", "acme/data"),
        ("github_repository", "Acme-Corp/data.platform_v2"),
    ],
)
def test_deploy_answers_accept_valid_values(key, raw):
    assert validate_answer(key, f" {raw} ") == raw


@pytest.mark.parametrize("raw", ["xy12345.eu-central-1", "ACME", "-PROD", "acme prod"])
def test_snowflake_account_must_be_organization_account(raw):
    with pytest.raises(Failure) as caught:
        validate_answer("snowflake_account", raw)
    assert caught.value.code == ExitCode.USAGE
    assert "ORGANIZATION-ACCOUNT" in caught.value.message


def test_snowflake_account_is_a_deploy_answer():
    assert validate_answer("snowflake_account", " ACME-PROD ") == "ACME-PROD"
    assert ANSWER_KEYS["snowflake_account"].scope == "deploy"


def test_sources_refuse_free_text():
    with pytest.raises(Failure) as exc:
        validate_answer("sources", "demo, our CRM at https://api.demo.test/v1")
    assert exc.value.code == ExitCode.USAGE
    assert "our CRM at https://api.demo.test/v1" in exc.value.message
    assert "vecten answer sources hubspot,billing_db" in exc.value.message


@pytest.mark.parametrize(
    ("raw", "names"),
    [("hubspot,billing_db", ["hubspot", "billing_db"]), ("start empty", []), ("none", [])],
)
def test_sources_take_connector_names(raw, names):
    assert validate_answer("sources", raw) == names


@pytest.mark.parametrize(
    ("raw", "names"),
    [
        ("HubSpot , Billing_DB", ["hubspot", "billing_db"]),
        ("Start Empty", []),
        ("hubspot,HubSpot", ["hubspot"]),
    ],
)
def test_sources_are_normalised(raw, names):
    assert validate_answer("sources", raw) == names


def test_an_empty_marker_with_names_is_refused():
    with pytest.raises(Failure) as exc:
        validate_answer("sources", "none, hubspot")
    assert exc.value.code == ExitCode.USAGE
