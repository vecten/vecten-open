import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from vecten_shared.keypair import generate_one_line, load_private_key, public_body
from vecten_shared.output import ExitCode, Failure


def _pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_a_generated_key_is_one_line_the_local_env_stores_bare():
    from vecten_shared.provider.local import _BARE_VALUE

    value = generate_one_line()
    assert "\n" not in value and "-----" not in value
    assert _BARE_VALUE.fullmatch(value)
    assert isinstance(load_private_key(value, "K"), rsa.RSAPrivateKey)


def test_a_pem_key_loads_as_before():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    loaded = load_private_key(_pem(key), "K")
    assert loaded.public_key().public_numbers() == key.public_key().public_numbers()


def test_the_one_line_form_is_the_pem_body():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    body = "".join(line for line in _pem(key).splitlines() if not line.startswith("-----"))
    loaded = load_private_key(body, "K")
    assert loaded.public_key().public_numbers() == key.public_key().public_numbers()


def test_the_public_body_is_what_snowflake_takes():
    key = load_private_key(generate_one_line(), "K")
    body = public_body(key)
    assert "\n" not in body and "-----" not in body
    der = serialization.load_der_public_key(__import__("base64").b64decode(body))
    assert der.public_numbers() == key.public_key().public_numbers()


@pytest.mark.parametrize("value", ["not a key", "QUJD"])
def test_a_value_that_is_no_key_names_the_secret_and_not_the_value(value):
    with pytest.raises(Failure) as caught:
        load_private_key(value, "MCP_PROVISIONER_PRIVATE_KEY")
    assert caught.value.code == ExitCode.CONFIG
    assert "MCP_PROVISIONER_PRIVATE_KEY" in caught.value.message
    assert value not in caught.value.message
