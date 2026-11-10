from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from vecten_mcp.runtime_setup import provisioner_key
from vecten_shared.keypair import generate_one_line


def _store(value):
    return SimpleNamespace(get_secret=lambda name: value)


def test_the_provisioner_key_loads_from_its_one_line_laptop_form():
    # `secret set --from-local` copies the laptop's one-line key to the environment.
    assert isinstance(provisioner_key(_store(generate_one_line())), rsa.RSAPrivateKey)


def test_the_provisioner_key_loads_from_the_pem_the_platform_stores():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    loaded = provisioner_key(_store(pem))
    assert loaded.public_key().public_numbers() == key.public_key().public_numbers()
