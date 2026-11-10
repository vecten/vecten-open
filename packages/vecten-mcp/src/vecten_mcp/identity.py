"""Login identities and warehouse names, independent of warehouse credentials."""

import re
from dataclasses import dataclass

from text_unidecode import unidecode

RESERVED = {
    "loader",
    "provisioner",
    "impersonator",
    "postgres",
    "analyst",
    "airflow",
    "mcp_state",
    "warehouse_owner",
}


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    email: str
    name: str

    def __post_init__(self):
        object.__setattr__(self, "email", self.email.strip().lower())
        if not all((self.issuer, self.subject, self.email, self.name)) or "@" not in self.email:
            raise ValueError("identity needs issuer, subject, email and display name")


def warehouse_name(
    display_name: str, occupied: set[str], override: str | None = None, kind: str = "human"
) -> str:
    if kind not in {"human", "service"}:
        raise ValueError("principal kind must be human or service")
    base = (
        override
        if override is not None
        else re.sub(r"[^a-z0-9]+", "_", unidecode(display_name).lower()).strip("_")[:63]
    )
    if kind == "service" and not base.startswith("svc_"):
        base = "svc_" + base
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", base):
        raise ValueError("warehouse name must be 1 to 63 lowercase ASCII identifier characters")
    if (
        base in RESERVED
        or base.startswith(("admin_", "pg_"))
        or (kind == "human" and base.startswith("svc_"))
    ):
        raise ValueError("warehouse name uses a reserved name or prefix")
    if override is not None and base in occupied:
        raise ValueError("warehouse name is bound to another principal")
    candidate, suffix = base, 2
    while candidate in occupied:
        tail = f"_{suffix}"
        candidate = base[: 63 - len(tail)] + tail
        suffix += 1
    return candidate
