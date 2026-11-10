from dataclasses import dataclass
from typing import Protocol

from vecten_mcp.identity import warehouse_name


@dataclass(frozen=True)
class Account:
    name: str
    roles: tuple[str, ...] = ()
    last_login: str | None = None
    can_adopt: bool = False
    adoption_sql: str | None = None


class WarehouseAdapter(Protocol):
    def inspect_account(self, name: str) -> Account | None: ...
    def create_account(self, name: str, kind: str) -> None: ...
    def drop_account(self, name: str) -> None: ...
    def open_membership(self, name: str) -> None: ...
    def close_membership(self, name: str) -> None: ...
    def connect_principal(self, principal: dict, roles: set[str], expires_at: float): ...
    def check_scope(self, registry: set[str]) -> None: ...


def check_account(name, kind=None):
    warehouse_name(
        name, set(), override=name, kind=kind or ("service" if name.startswith("svc_") else "human")
    )
