import pytest

from vecten_mcp.identity import Identity, warehouse_name


def test_warehouse_transliteration_collision_and_length():
    assert warehouse_name("Łukasz Żółć", set()) == "lukasz_zolc"
    assert warehouse_name("Jan Kowalski", {"jan_kowalski"}) == "jan_kowalski_2"
    name = warehouse_name("a" * 90, {"a" * 63})
    assert len(name.encode()) == 63 and name.endswith("_2")
    assert warehouse_name("Reporting", set(), kind="service") == "svc_reporting"


@pytest.mark.parametrize(
    "name",
    ["admin_jane", "svc_reporting", "loader", "provisioner", "impersonator", "postgres", "analyst"],
)
def test_human_reserved_names(name):
    with pytest.raises(ValueError, match="reserved"):
        warehouse_name("Jane", set(), override=name)


@pytest.mark.parametrize("name", ["x; DROP ROLE postgres", "", "1invalid", "a" * 64])
def test_invalid_explicit_name(name):
    with pytest.raises(ValueError):
        warehouse_name("Jane", set(), override=name)


def test_explicit_collision_refuses_instead_of_renaming():
    with pytest.raises(ValueError, match="bound"):
        warehouse_name("Jan", {"jan"}, override="jan")


def test_identity_normalizes_email_and_keeps_subject_opaque():
    identity = Identity("https://issuer.example", "SUBJECT", "Jane@EXAMPLE.COM", "Jane")
    assert identity.email == "jane@example.com"
    assert identity.subject == "SUBJECT"
