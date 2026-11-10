"""The MCP redeems a set-password link `vecten mcp admin-link` stored as MCP_ADMIN_LINK."""

import json
import secrets

import pytest

from vecten_mcp.auth.identity import PasswordBackend, digest
from vecten_mcp.auth.limits import RateLimit
from vecten_mcp.state import StateStore

ADMIN = "ada@acme.test"
PASSWORD = "long-fixture-password"


class Link:
    """The secrets store's MCP_ADMIN_LINK value, re-read on every call as the provider is."""

    def __init__(self):
        self.value = None
        self.reads = 0

    def issue(self, email=ADMIN, issued_at=1000.0):
        token = secrets.token_urlsafe(32)
        self.value = json.dumps({"token": token, "email": email, "issued_at": issued_at})
        return token

    def __call__(self):
        self.reads += 1
        return self.value


@pytest.fixture
def setup():
    store = StateStore("sqlite://")
    store.invite(ADMIN, ADMIN, "ada")
    now = [1000.0]
    link = Link()
    admins = [ADMIN]
    backend = PasswordBackend(store, clock=lambda: now[0], admin_link=link, admins=admins)
    return store, backend, link, now, admins


def test_a_valid_link_sets_the_password(setup):
    store, backend, link, now, _ = setup
    token = link.issue()
    now[0] += 60
    backend.set_password(token, PASSWORD)
    assert backend.authenticate(ADMIN, PASSWORD, "ip").email == ADMIN
    assert store.get("used_admin_links", digest(token)) == {"email": ADMIN}
    assert token not in json.dumps(store.scan("used_admin_links"))


def test_attempts_from_one_address_are_limited_before_the_secret_is_read(setup):
    _, backend, link, _, _ = setup
    link.issue()
    for _ in range(30):
        with pytest.raises(ValueError, match="invalid or expired"):
            backend.set_password("not-the-token", PASSWORD, address="203.0.113.9")
    reads = link.reads
    with pytest.raises(RateLimit):
        backend.set_password("not-the-token", PASSWORD, address="203.0.113.9")
    assert link.reads == reads


def test_a_link_works_once(setup):
    _, backend, link, _, _ = setup
    token = link.issue()
    backend.set_password(token, PASSWORD)
    with pytest.raises(ValueError, match="invalid or expired"):
        backend.set_password(token, "another-long-password")


def test_a_link_expires_after_fifteen_minutes(setup):
    _, backend, link, now, _ = setup
    token = link.issue(issued_at=1000.0)
    now[0] = 1000.0 + 901
    with pytest.raises(ValueError, match="invalid or expired"):
        backend.set_password(token, PASSWORD)


def test_a_link_for_an_email_removed_from_admins_is_refused(setup):
    _, backend, link, _, admins = setup
    token = link.issue()
    admins.clear()
    with pytest.raises(ValueError, match="invalid or expired"):
        backend.set_password(token, PASSWORD)


def test_a_newer_link_replaces_the_older(setup):
    _, backend, link, _, _ = setup
    older = link.issue()
    newer = link.issue()
    with pytest.raises(ValueError, match="invalid or expired"):
        backend.set_password(older, PASSWORD)
    backend.set_password(newer, PASSWORD)


def test_a_link_for_an_admin_not_yet_invited_invites_them(setup):
    store, backend, link, _, admins = setup
    admins.append("grace@acme.test")
    token = link.issue(email="grace@acme.test")
    backend.set_password(token, PASSWORD)
    principal = store.by_email("grace@acme.test")
    assert principal and principal["kind"] == "human" and principal["enabled"]
    assert backend.authenticate("grace@acme.test", PASSWORD, "ip").email == "grace@acme.test"


def test_malformed_or_absent_links_are_refused(setup):
    _, backend, link, _, _ = setup
    with pytest.raises(ValueError, match="invalid or expired"):
        backend.set_password("anything", PASSWORD)
    link.value = "not json"
    with pytest.raises(ValueError, match="invalid or expired"):
        backend.set_password("anything", PASSWORD)
    link.value = json.dumps({"token": "x"})
    with pytest.raises(ValueError, match="invalid or expired"):
        backend.set_password("x", PASSWORD)


def test_a_disabled_principal_cannot_redeem(setup):
    store, backend, link, _, _ = setup
    store.update_principal(store.by_email(ADMIN)["id"], enabled=False)
    token = link.issue()
    with pytest.raises(ValueError):
        backend.set_password(token, PASSWORD)
    assert store.get("passwords", ADMIN) is None


def test_the_link_is_read_on_every_attempt(setup):
    _, backend, link, _, _ = setup
    with pytest.raises(ValueError):
        backend.set_password("early", PASSWORD)
    token = link.issue()
    backend.set_password(token, PASSWORD)
    assert link.reads == 2


def test_a_startup_link_still_works_without_the_admin_link(setup):
    store, _, _, _, _ = setup
    backend = PasswordBackend(store, clock=lambda: 1000.0)
    token = backend.setup_link(ADMIN)
    backend.set_password(token, PASSWORD)
    assert backend.authenticate(ADMIN, PASSWORD, "ip").email == ADMIN


def test_runtime_reads_the_cloud_secret_on_every_call_and_never_locally():
    from vecten_mcp.runtime_setup import admin_link_reader
    from vecten_shared.output import ExitCode, Failure

    class Store:
        value = None

        def get_secret(self, name):
            assert name == "MCP_ADMIN_LINK"
            if self.value is None:
                raise Failure(ExitCode.PREREQUISITE, "not set")
            return self.value

    store = Store()
    assert admin_link_reader("local", store) is None
    read = admin_link_reader("aws", store)
    assert read() is None
    store.value = "first"
    assert read() == "first"
    store.value = "second"
    assert read() == "second"


def test_redeeming_the_admin_link_voids_the_admins_other_links_and_browser_sessions(setup):
    store, backend, link, _, _ = setup
    ada = store.by_email(ADMIN)
    older = backend.setup_link(ADMIN)
    store.put("browser_sessions", digest("cookie"), {"principal_id": ada["id"]})
    assert backend.set_password(link.issue(), PASSWORD) == ada["id"]
    assert store.get("browser_sessions", digest("cookie")) is None
    with pytest.raises(ValueError, match="invalid or expired"):
        backend.set_password(older, "an-attackers-password")
