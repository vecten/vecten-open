import pytest
from sqlalchemy.exc import IntegrityError

from vecten_mcp.identity import Identity
from vecten_mcp.state import StateStore


def test_state_persists_and_default_group_maps_to_analyst(tmp_path):
    url = f"sqlite:///{tmp_path / 'state.db'}"
    store = StateStore(url)
    principal = store.create_principal(
        Identity("password", "jane@example.com", "jane@example.com", "Jane Doe"),
        "jane_doe",
    )
    assert store.groups_for(principal["id"]) == {"default"}
    store.close()
    again = StateStore(url)
    assert again.principal(principal["id"])["warehouse_name"] == "jane_doe"
    assert again.group("default")["warehouse_role"] == "analyst"
    again.close()


def test_principal_unique_identity_and_warehouse_binding():
    store = StateStore("sqlite://")
    identity = Identity("issuer", "subject", "jane@example.com", "Jane")
    store.create_principal(identity, "jane")
    with pytest.raises(IntegrityError):
        store.create_principal(identity, "different")
    with pytest.raises(IntegrityError):
        store.create_principal(Identity("issuer", "other", "other@example.com", "Other"), "jane")
    assert len(store.principals()) == 1


def test_oauth_record_consumption_is_single_use_and_persistent(tmp_path):
    store = StateStore(f"sqlite:///{tmp_path / 'state.db'}")
    store.put("codes", "hash-only", {"principal": "p"}, expires_at=100)
    assert store.take("codes", "hash-only", now=99) == {"principal": "p"}
    assert store.take("codes", "hash-only", now=99) is None
    store.put("codes", "expired", {"principal": "p"}, expires_at=100)
    assert store.take("codes", "expired", now=100) is None


def test_invitation_binding_requires_verified_identity_and_preserves_admin():
    store = StateStore("sqlite://")
    p = store.invite("jane@example.com", "Jane", "jane", admin=True)
    assert p["issuer"] is None
    identity = Identity("https://idp", "123", "jane@example.com", "Jane")
    bound = store.bind_identity(identity)
    assert bound["id"] == p["id"] and bound["admin"]
    with pytest.raises(ValueError, match="bound"):
        store.bind_identity(Identity("https://other", "123", "jane@example.com", "Jane"))


def test_failed_transaction_rolls_back():
    store = StateStore("sqlite://")
    with pytest.raises(RuntimeError), store.transaction() as conn:
        store.put("settings", "x", {"value": 1}, conn=conn)
        raise RuntimeError("abort")
    assert store.get("settings", "x") is None
