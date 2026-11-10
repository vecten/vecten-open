import json
from types import SimpleNamespace

import pytest

from vecten_mcp.rotation import PasswordRotation, password_candidates
from vecten_mcp.state import StateStore


class Store:
    def __init__(self):
        self.value = "old-password"
        self.fail_finalize = False

    def get_secret(self, name):
        return self.value

    def put_secret(self, name, value):
        if self.fail_finalize and not value.startswith("{"):
            raise OSError("store temporarily unavailable")
        self.value = value


def fixture():
    provider = Store()
    warehouse = SimpleNamespace(password="old-password", closed=0)

    def connect(password):
        if password != warehouse.password:
            raise ValueError("authentication failed")

        class Connection:
            def execute(self, query):
                # The final literal in the safely composed ALTER ROLE query is the new value.
                warehouse.password = query._obj[-1]._obj

            def close(self):
                warehouse.closed += 1

        return Connection()

    state = StateStore("sqlite://")
    return provider, warehouse, PasswordRotation(state, provider, connect, clock=lambda: 100)


def test_rotation_persists_new_password_and_leaves_active_connections_alone():
    provider, warehouse, rotation = fixture()
    rotation.rotate()
    assert provider.value == warehouse.password != "old-password"
    assert warehouse.closed == 1
    assert rotation.state.get("rotation", "impersonator")["at"] == 100


def test_interruption_after_database_change_keeps_both_candidates_recoverable():
    provider, warehouse, rotation = fixture()
    provider.fail_finalize = True
    with pytest.raises(OSError):
        rotation.rotate()
    pending = json.loads(provider.value)
    assert pending["current"] == "old-password"
    assert warehouse.password in password_candidates(provider.value)
    provider.fail_finalize = False
    rotation.rotate()
    assert provider.value == warehouse.password
