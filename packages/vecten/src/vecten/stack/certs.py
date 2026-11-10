"""Self-signed certificates for the local hostnames NGINX serves."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from vecten_shared.records import write_text_atomic


def ensure_self_signed(stack_dir: Path, hostnames: list[str]) -> Path:
    certs = stack_dir / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    for hostname in hostnames:
        host = hostname.split(":", 1)[0]
        crt, key = certs / f"{host}.crt", certs / f"{host}.key"
        if crt.is_file() and key.is_file():
            continue
        private = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(private.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(private, hashes.SHA256())
        )
        pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        write_text_atomic(key, pem, mode=0o600)
        crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return certs
