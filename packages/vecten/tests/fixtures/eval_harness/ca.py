"""A certificate authority of the harness's own, so the stub hostnames answer over trusted TLS.

The authority lives in its directory and is reused on every start, so a trust granted to it
once (the keychain, a browser) survives restarts. Nothing here touches a system trust store.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NAME = "Vecten eval harness CA"
# Clients refuse leaf certificates valid for longer than 398 days.
LEAF_DAYS = 365
CA_DAYS = 3650
# The root may be trusted system-wide, so it can only vouch for the evaluation's own names.
PERMITTED = [
    x509.DNSName("acme.test"),
    x509.DNSName("demo.test"),
    x509.DNSName("localhost"),
    x509.IPAddress(ipaddress.ip_network("127.0.0.0/8")),
    x509.IPAddress(ipaddress.ip_network("::1/128")),
]


def _constrained(path: Path) -> bool:
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    try:
        cert.extensions.get_extension_for_class(x509.NameConstraints)
    except x509.ExtensionNotFound:
        return False
    return True


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_bytes(data)


@dataclass(frozen=True)
class Authority:
    directory: Path

    @property
    def cert_path(self) -> Path:
        return self.directory / "ca.pem"

    @property
    def key_path(self) -> Path:
        return self.directory / "ca-key.pem"

    @classmethod
    def create(cls, directory: Path) -> Authority:
        """The authority in `directory`, generated on first use and when the one there may
        vouch for names outside the evaluation's."""
        authority = cls(Path(directory))
        if (
            authority.cert_path.is_file()
            and authority.key_path.is_file()
            and _constrained(authority.cert_path)
        ):
            return authority
        key = _key()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NAME)])
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .add_extension(x509.NameConstraints(PERMITTED, None), critical=True)
            .sign(key, hashes.SHA256())
        )
        _write_private(authority.key_path, _pem_key(key))
        authority.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return authority

    def issue(self, names: list[str], directory: Path) -> tuple[Path, Path]:
        """A server certificate for `names` signed by this authority: (cert, key) paths."""
        ca_key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        key = _key()
        alt: list[x509.GeneralName] = []
        for name in names:
            try:
                alt.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                alt.append(x509.DNSName(name))
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()), False
            )
            .sign(ca_key, hashes.SHA256())
        )
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        cert_path, key_path = directory / f"{names[0]}.pem", directory / f"{names[0]}-key.pem"
        cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
            + ca_cert.public_bytes(serialization.Encoding.PEM)
        )
        _write_private(key_path, _pem_key(key))
        return cert_path, key_path

    def bundle(self, path: Path) -> Path:
        """The public roots plus this authority, for variables such as SSL_CERT_FILE that
        replace a client's roots rather than add to them."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        roots = Path(certifi.where()).read_text().rstrip("\n")
        path.write_text(f"{roots}\n\n# {NAME}\n{self.cert_path.read_text()}")
        return path
