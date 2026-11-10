import jwt
import pytest

from vecten_mcp.auth.keys import PUBLISH_AHEAD, ROTATE_AFTER, SigningKeys


class Secrets:
    def __init__(self):
        self.values = {}

    def list_secrets(self):
        return list(self.values)

    def get_secret(self, name):
        return self.values[name]

    def put_secret(self, name, value):
        self.values[name] = value


def test_signing_audience_lifetime_and_overlap_rotation():
    now = [100]
    provider = Secrets()
    keys = SigningKeys(provider, "https://mcp.example", clock=lambda: now[0])
    old = keys.sign("jane", "https://mcp.example/mcp", 10000)
    claims = keys.verify(old, "https://mcp.example/mcp")
    assert claims["exp"] == 3700
    with pytest.raises(ValueError):
        keys.verify(old, "https://other/mcp")
    assert list(provider.values) == ["MCP_SIGNING_KEY"]
    now[0] = 200
    keys.rotate()
    new = keys.sign("jane", "https://mcp.example/mcp", 900)
    assert jwt.get_unverified_header(old)["kid"] != jwt.get_unverified_header(new)["kid"]
    assert len(keys.jwks()["keys"]) == 2
    assert keys.verify(old, "https://mcp.example/mcp")["sub"] == "jane"
    assert "PRIVATE" not in str(keys.jwks()) and all("d" not in k for k in keys.jwks()["keys"])
    restored = SigningKeys(provider, "https://mcp.example", clock=lambda: now[0])
    assert restored.verify(new, "https://mcp.example/mcp")["sub"] == "jane"
    now[0] = 3801
    assert len(keys.jwks()["keys"]) == 1
    with pytest.raises(ValueError):
        keys.verify(old, "https://mcp.example/mcp")


def test_snowflake_token_for_one_role_is_scoped_to_it_with_bounded_lifetime():
    keys = SigningKeys(Secrets(), "https://mcp.example", clock=lambda: 100)
    token = keys.snowflake_token("JANE", {"ANALYST"}, 200, "https://account.snowflakecomputing.com")
    claims = keys.verify(token, "https://account.snowflakecomputing.com")
    assert claims["sub"] == "JANE" and claims["scope"] == "session:role:ANALYST"
    assert claims["exp"] == 200


def test_snowflake_token_for_several_roles_uses_the_any_role_scope():
    # Snowflake allows USE SECONDARY ROLES in an External OAuth session only under this scope.
    keys = SigningKeys(Secrets(), "https://mcp.example", clock=lambda: 100)
    token = keys.snowflake_token(
        "JANE", {"ANALYST", "FINANCE"}, 200, "https://account.snowflakecomputing.com"
    )
    claims = keys.verify(token, "https://account.snowflakecomputing.com")
    assert claims["scope"] == "session:role-any"


def test_empty_cloud_secret_shell_is_initialized_but_denials_are_not_swallowed():
    from vecten_shared.output import ExitCode, Failure

    class Shell(Secrets):
        def list_secrets(self):
            return ["MCP_SIGNING_KEY"]

        def get_secret(self, name):
            raise Failure(self.code, "unavailable")

    shell = Shell()
    shell.code = ExitCode.PREREQUISITE
    assert SigningKeys(shell, "https://mcp.example").jwks()["keys"]
    shell.code = ExitCode.REFUSED
    with pytest.raises(Failure):
        SigningKeys(shell, "https://mcp.example")


def _kids(keys):
    return [key["kid"] for key in keys.jwks()["keys"]]


def _signing_kid(keys):
    return jwt.get_unverified_header(keys.sign("jane", "https://mcp.example/mcp", 10**12))["kid"]


def test_maintenance_before_the_publish_window_changes_nothing():
    now = [0]
    provider = Secrets()
    keys = SigningKeys(provider, "https://mcp.example", clock=lambda: now[0])
    stored = provider.values["MCP_SIGNING_KEY"]
    now[0] = ROTATE_AFTER - PUBLISH_AHEAD - 1
    keys.maintain()
    assert provider.values["MCP_SIGNING_KEY"] == stored
    assert len(_kids(keys)) == 1


def test_the_next_key_is_published_a_window_before_it_signs():
    # Snowflake caches the JWKS per URL and fetches no key it has not seen, so a key must be
    # published long before a token it signs reaches Snowflake.
    now = [0]
    provider = Secrets()
    keys = SigningKeys(provider, "https://mcp.example", clock=lambda: now[0])
    current = _signing_kid(keys)
    now[0] = ROTATE_AFTER - PUBLISH_AHEAD
    keys.maintain()
    kids = _kids(keys)
    assert len(kids) == 2 and kids[0] == current
    assert _signing_kid(keys) == current
    upcoming = kids[1]
    restored = SigningKeys(provider, "https://mcp.example", clock=lambda: now[0])
    assert _kids(restored) == kids
    now[0] += PUBLISH_AHEAD - 1
    keys.maintain()
    assert _signing_kid(keys) == current
    now[0] += 1
    keys.maintain()
    assert _signing_kid(keys) == upcoming
    assert "PRIVATE" not in str(keys.jwks())


def test_the_key_that_takes_over_keeps_the_old_one_for_its_tokens():
    now = [0]
    keys = SigningKeys(Secrets(), "https://mcp.example", clock=lambda: now[0])
    now[0] = ROTATE_AFTER - PUBLISH_AHEAD
    keys.maintain()
    now[0] = ROTATE_AFTER - 10
    old = keys.sign("jane", "https://mcp.example/mcp", now[0] + 3600)
    now[0] = ROTATE_AFTER
    keys.maintain()
    assert keys.verify(old, "https://mcp.example/mcp")["sub"] == "jane"
    assert len(_kids(keys)) == 2
    now[0] = ROTATE_AFTER + 3600
    assert len(_kids(keys)) == 1
    # The new key's own successor waits a full cycle.
    keys.maintain()
    assert len(_kids(keys)) == 1


def test_a_ring_without_a_next_key_still_loads():
    import json

    provider = Secrets()
    SigningKeys(provider, "https://mcp.example", clock=lambda: 0)
    ring = json.loads(provider.values["MCP_SIGNING_KEY"])
    ring.pop("next", None)
    provider.values["MCP_SIGNING_KEY"] = json.dumps(ring)
    keys = SigningKeys(provider, "https://mcp.example", clock=lambda: 0)
    assert len(_kids(keys)) == 1
