"""One identity lifecycle service for every administrative surface."""

from dataclasses import asdict
from threading import RLock
from uuid import uuid4

from sqlalchemy import delete

from vecten_mcp.identity import Identity, warehouse_name
from vecten_mcp.state import principals


class AdoptionRequired(ValueError):
    def __init__(self, account):
        self.account = account
        super().__init__(
            "warehouse account exists; confirm adoption after reviewing roles and last login"
        )

    def as_data(self):
        return {"adoption_required": True, "account": asdict(self.account)}


class Administration:
    def __init__(self, state, adapter, auth, access, *, pools=None):
        self.state, self.adapter, self.auth, self.access = state, adapter, auth, access
        self.pools = pools
        self.lock = RLock()

    def list_principals(self):
        return self.state.principals()

    def create_person(self, email, name, *, warehouse_override=None, adopt=False):
        Identity("invitation", email.lower(), email, name)
        return self._create(email.lower(), name, "human", warehouse_override, adopt)

    def create_service(self, name, *, adopt=False):
        principal = self._create(f"{uuid4().hex}@service.invalid", name, "service", None, adopt)
        return dict(principal, credentials=self.auth.service_credentials(principal["id"]))

    def _create(self, email, name, kind, override, adopt):
        with self.lock:
            pending = self.state.by_email(email)
            if pending and (pending["provisioned"] or pending["kind"] != kind):
                raise ValueError("principal already exists")
            occupied = {p["warehouse_name"] for p in self.state.principals()}
            target = (
                pending["warehouse_name"]
                if pending
                else warehouse_name(name, occupied, override=override, kind=kind)
            )
            if pending and override and override != target:
                raise ValueError("pending principal already has a reserved warehouse name")
            account = self.adapter.inspect_account(target)
            if account and (not adopt or not account.can_adopt):
                raise AdoptionRequired(account)
            created = False
            try:
                with self.state.transaction() as conn:
                    if pending:
                        principal = self.state.update_principal(
                            pending["id"], adopted=account is not None, provisioned=True, conn=conn
                        )
                    elif kind == "human":
                        principal = self.state.invite(
                            email,
                            name,
                            target,
                            adopted=account is not None,
                            provisioned=True,
                            conn=conn,
                        )
                    else:
                        principal = self.state.create_principal(
                            Identity("service", target, email, name),
                            target,
                            kind=kind,
                            adopted=account is not None,
                            provisioned=True,
                            conn=conn,
                        )
                    if account is None:
                        self.adapter.create_account(target, kind)
                        created = True
                    self.adapter.membership("analyst", target)
                    self.state.put(
                        "provisioner_registry",
                        target,
                        {"kind": kind, "adopted": account is not None},
                        conn=conn,
                    )
            except BaseException:
                if created:
                    self.adapter.drop_account(target)
                raise
            return principal

    def set_admin(self, principal_id, enabled):
        principal = self._require(principal_id)
        if principal["kind"] != "human":
            raise ValueError("service principals cannot be admins")
        return self.state.update_principal(principal_id, admin=bool(enabled))

    def _require(self, principal_id):
        principal = self.state.principal(principal_id)
        if not principal:
            raise ValueError("unknown principal")
        return principal

    def disable(self, principal_id):
        self._require(principal_id)
        self.auth.disable_principal(principal_id)
        if self.pools:
            self.pools.disable_principal(principal_id)
        return self.state.principal(principal_id)

    def delete_principal(self, principal_id):
        principal = self._require(principal_id)
        self.disable(principal_id)
        for group in self.state.groups_for(principal_id):
            role = self.state.group(group)["warehouse_role"]
            self.adapter.membership(role, principal["warehouse_name"], False)
        if principal["provisioned"] and not principal["adopted"]:
            self.adapter.drop_account(principal["warehouse_name"])
        with self.state.transaction() as conn:
            conn.execute(delete(principals).where(principals.c.id == principal_id))
            # Adoption's ADMIN grant can outlive the binding; retain its registry entry.
            if not principal["adopted"]:
                self.state.remove("provisioner_registry", principal["warehouse_name"], conn=conn)
            self.state.remove("passwords", principal["email"], conn=conn)
            for key, link in self.state.scan("password_links", conn=conn):
                if link.get("principal_id") == principal_id:
                    self.state.remove("password_links", key, conn=conn)
        return {"deleted": principal_id, "warehouse_account_retained": principal["adopted"]}
