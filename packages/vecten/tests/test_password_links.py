import json

import pytest

from vecten.password_links import (
    added,
    admin_link_issued,
    dropped,
    issued,
    owed,
    read_links,
)
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import RECORDS_DIR


def test_added_people_are_owed_a_link(tmp_path):
    added(tmp_path, "prod", "p-grace", "grace@acme.test")
    assert owed(tmp_path, "prod") == [
        {"id": "p-grace", "email": "grace@acme.test", "issued": False}
    ]
    issued(tmp_path, "prod", "p-grace")
    assert owed(tmp_path, "prod") == []


def test_an_admin_link_covers_the_same_address_in_any_case(tmp_path):
    admin_link_issued(tmp_path, "prod", "ada@acme.test")
    added(tmp_path, "prod", "p-ada", "Ada@Acme.test")
    assert owed(tmp_path, "prod") == []
    assert read_links(tmp_path, "prod") == [
        {"id": "p-ada", "email": "ada@acme.test", "issued": True}
    ]


def test_a_second_add_replaces_the_entry(tmp_path):
    added(tmp_path, "prod", "p-1", "grace@acme.test")
    added(tmp_path, "prod", "p-2", " grace@acme.test ")
    assert read_links(tmp_path, "prod") == [
        {"id": "p-2", "email": "grace@acme.test", "issued": False}
    ]


def test_admin_link_keeps_the_id_it_had(tmp_path):
    added(tmp_path, "prod", "p-ada", "ada@acme.test")
    admin_link_issued(tmp_path, "prod", "ada@acme.test")
    assert read_links(tmp_path, "prod") == [
        {"id": "p-ada", "email": "ada@acme.test", "issued": True}
    ]


def test_dropped_and_unknown_ids(tmp_path):
    added(tmp_path, "prod", "p-grace", "grace@acme.test")
    before = read_links(tmp_path, "prod")
    issued(tmp_path, "prod", "p-nobody")
    dropped(tmp_path, "prod", "p-nobody")
    assert read_links(tmp_path, "prod") == before
    dropped(tmp_path, "prod", "p-grace")
    assert read_links(tmp_path, "prod") == []


def test_records_are_per_environment(tmp_path):
    added(tmp_path, "prod", "p-grace", "grace@acme.test")
    assert owed(tmp_path, "staging") == []
    assert read_links(tmp_path, "staging") == []


def test_a_re_added_person_with_a_new_id_is_owed_a_link(tmp_path):
    added(tmp_path, "prod", "p-1", "grace@acme.test")
    issued(tmp_path, "prod", "p-1")
    added(tmp_path, "prod", "p-2", "grace@acme.test")
    assert owed(tmp_path, "prod") == [{"id": "p-2", "email": "grace@acme.test", "issued": False}]


def test_a_re_added_person_with_the_same_id_keeps_the_issued_link(tmp_path):
    added(tmp_path, "prod", "p-1", "grace@acme.test")
    issued(tmp_path, "prod", "p-1")
    added(tmp_path, "prod", "p-1", "grace@acme.test")
    assert owed(tmp_path, "prod") == []


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"links": None},
        {"links": [{"email": "a@b.test", "issued": True}]},
        {"links": ["x"]},
        [],
    ],
)
def test_a_record_of_the_wrong_shape_is_a_corrupt_run_record(tmp_path, record):
    path = tmp_path / RECORDS_DIR / "password-links" / "prod.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    with pytest.raises(Failure) as caught:
        read_links(tmp_path, "prod")
    assert caught.value.code == ExitCode.CONFIG
    assert "corrupt run record" in caught.value.message
