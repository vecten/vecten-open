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

    def connect_app(self, apps, key, *, adopt=False, rotate=True):
        """Creates or rotates the principal of bound container app `key` (`<extension>.<app>`):
        named `principal_name(app)`, marked as the app's, a member of exactly its bound groups,
        with new client credentials that replace any earlier ones. Without `rotate`, an
        existing principal's groups follow the binding and its credentials stay."""
        from vecten_mcp.apps import APP_PRINCIPALS, principal_name

        extension, _, name = key.partition(".")
        app = apps.apps.get(name) if apps else None
        if app is None or app.key != key:
            raise ValueError(
                f"{key} is not bound in the shipped vecten.toml; bind it in "
                f"[apps.{extension or '<extension>'}.{name or '<app>'}] and ship"
            )
        if app.kind != "container":
            raise ValueError(
                f"{key} is a {app.kind} app; only a container app reads as an app principal"
            )
        missing = sorted(group for group in set(app.groups) if not self.state.group(group))
        if missing:
            raise ValueError(
                f"group {missing[0]} that {key} is bound to does not exist; create it first"
            )
        target = principal_name(app.name)
        with self.lock:
            found = next(
                (p for p in self.state.principals() if p["warehouse_name"] == target), None
            )
            if found is not None and found["kind"] != "service":
                raise ValueError(f"{target} belongs to a person, not a service principal")
            if found is not None and not found["enabled"]:
                raise ValueError(f"{target} is disabled; delete it, then connect {key} again")
            marker = None
            if found is not None:
                marker = self.state.get(APP_PRINCIPALS, found["id"])
                if marker is not None and marker != {"extension": app.extension, "app": app.name}:
                    other = (
                        f"{marker.get('extension')}.{marker.get('app')}"
                        if isinstance(marker, dict)
                        else "another app"
                    )
                    raise ValueError(
                        f"{target} is the principal of {other}; delete it before connecting {key}"
                    )
                if marker is None and not adopt:
                    raise ValueError(
                        f"{target} exists but is not marked as {key}'s principal; review who "
                        "uses it, then repeat with --adopt: its groups become "
                        f"{', '.join(sorted(app.groups))} and its credentials are replaced"
                    )
            if found is None and not rotate:
                raise ValueError(
                    f"{key} has no principal yet; connect it without --groups-only first"
                )
            if found is not None and not rotate and marker is None:
                raise ValueError(
                    f"{target} is not marked as {key}'s principal; connect it with --adopt, "
                    "which replaces its credentials"
                )
            if adopt and (found is None or marker is None):
                held = self.state.groups_for(found["id"]) if found else set()
                self._adoptable_for(app, target, held)
            # The principal holds no client credentials until the last step, so nobody can
            # sign in as it before it is marked and its groups are set.
            principal = found or self._create(
                f"{uuid4().hex}@service.invalid", f"App {key}", "service", target, adopt
            )
            apps.mark(principal["id"], app.extension, app.name)
            held, bound = self.state.groups_for(principal["id"]), set(app.groups)
            for group in sorted(bound - held):
                self.access.membership(group, principal["id"], True)
            for group in sorted(held - bound):
                self.access.membership(group, principal["id"], False)
            credentials = self.auth.replace_service_credentials(principal["id"]) if rotate else None
        return {
            "app": key,
            "created": found is None,
            "principal": {
                "id": principal["id"],
                "name": principal["name"],
                "warehouse_name": target,
            },
            "groups": sorted(bound),
            "credentials": credentials,
        }

    def _adoptable_for(self, app, target, held=()):
        """An app principal reads as exactly its bound groups' roles, so an existing warehouse
        account holding any other role is not adopted as one. The roles of `held`, the MCP
        groups an existing principal is in, are revoked by the connection and so count as
        managed."""
        account = self.adapter.inspect_account(target)
        if account is None:
            return
        groups = {*app.groups, *held, "default"}
        # PUBLIC is every Snowflake user's; `default` is the group a new principal joins first
        # and leaves when the app is not bound to it.
        managed = {"public"} | {
            self.state.group(group)["warehouse_role"].casefold() for group in groups
        }
        extra = sorted(role for role in account.roles if role.casefold() not in managed)
        if extra:
            raise ValueError(
                f"warehouse account {target} holds {', '.join(extra)}, beyond the roles of "
                f"{app.key}'s groups; revoke them before adopting it as the app's principal"
            )

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
        held = self.access.held_roles(principal_id) if self.access else None
        if held is None:
            held = {
                self.state.group(g)["warehouse_role"] for g in self.state.groups_for(principal_id)
            }
        for role in sorted(held):
            self.adapter.membership(role, principal["warehouse_name"], False)
        if principal["provisioned"] and not principal["adopted"]:
            self.adapter.drop_account(principal["warehouse_name"])
        with self.state.transaction() as conn:
            conn.execute(delete(principals).where(principals.c.id == principal_id))
            # Adoption's ADMIN grant can outlive the binding; retain its registry entry.
            if not principal["adopted"]:
                self.state.remove("provisioner_registry", principal["warehouse_name"], conn=conn)
            self.state.remove("passwords", principal["email"], conn=conn)
            self.state.remove("app_principals", principal_id, conn=conn)
            self.state.remove("app_roles", principal_id, conn=conn)
            for key, link in self.state.scan("password_links", conn=conn):
                if link.get("principal_id") == principal_id:
                    self.state.remove("password_links", key, conn=conn)
        return {"deleted": principal_id, "warehouse_account_retained": principal["adopted"]}
