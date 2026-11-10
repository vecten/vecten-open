"""RSA key pairs for Snowflake key-pair sign-in, in a form every secrets store holds.

The local `.env` is read by python-dotenv and Docker Compose and holds no newline, so a key
generated on the laptop is kept as its PKCS#8 body on one line, which is also how
`secret set --from-local` copies it to an environment. A key the platform generated in a
cloud store is PEM. Both forms load. `cryptography` is imported where it is used: only the
CLI and the MCP, which both install it, call this module.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from vecten_shared.output import ExitCode, Failure


def generate_one_line() -> str:
    """A new 2048-bit RSA private key, as its PKCS#8 body on one line."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return base64.b64encode(der).decode("ascii")


def load_private_key(value: str, name: str) -> Any:
    """The RSA private key in secret `name`, held as PEM or as its one-line body."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    text = value.strip()
    try:
        if text.startswith("-----"):
            key = serialization.load_pem_private_key(text.encode("utf-8"), password=None)
        else:
            key = serialization.load_der_private_key(
                base64.b64decode(text, validate=True), password=None
            )
    except (ValueError, TypeError, UnsupportedAlgorithm, binascii.Error) as exc:
        # The exception text can quote the key material, so only its type is named.
        raise Failure(
            ExitCode.CONFIG,
            f"{name} is not an unencrypted RSA private key ({type(exc).__name__}); delete it "
            "so the next run generates a new key pair",
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise Failure(
            ExitCode.CONFIG,
            f"{name} is not an RSA key, which Snowflake key-pair sign-in needs; delete it so "
            "the next run generates a new key pair",
        )
    return key


def public_body(key: Any) -> str:
    """The public half of `key` on one line, without PEM header and trailer, as Snowflake's
    `RSA_PUBLIC_KEY` takes it."""
    from cryptography.hazmat.primitives import serialization

    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode("ascii")
