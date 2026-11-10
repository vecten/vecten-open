"""Scaleway for the vecten CLI: the runtime provider plus bootstrap, provisioning through
Terraform, writes to any secret, shipping by pull, final snapshots and admin access.

Importing this module registers `ScalewayProvider` under `scaleway`, replacing the runtime
class that `vecten_shared` fills the slot with. CLI paths import it inside the function that
needs it, so importing the CLI loads no HTTP client or boto3.

The control plane signs with one key: the human's on the laptop (`SCW_ACCESS_KEY` and
`SCW_SECRET_KEY`, or the scw configuration), the deploy key in CI (the same variables).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from vecten.deploy.control import DESTROYING_VAR, CloudBootstrap, apply_update_only
from vecten.deploy.guards import require_bootstrapped
from vecten.deploy.names import rds_identifier
from vecten.deploy.outputs import (
    outputs_key,
    outputs_text,
    parse_stored_outputs,
    save_outputs,
    write_outputs,
)
from vecten.deploy.scaleway import bootstrap, clients
from vecten.deploy.scaleway import ship as ship_mod
from vecten.deploy.scaleway import snapshots as snapshots_mod
from vecten.deploy.scaleway.bootstrap import find_secret, read_latest
from vecten.deploy.scaleway.rdb import (
    close_public_endpoints,
    find_instance,
    public_endpoints,
    set_user_password,
    wait_ready,
)
from vecten.deploy.snowflake import snowflake_root
from vecten.deploy.terraform import TerraformRoot, TerraformRunner, state_key
from vecten.deploy.tfvars import check_tfvars, env_tfvars_path, read_tfvars, root_var_file
from vecten_shared.config import VectenConfig
from vecten_shared.names import check_secret_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import provider_factory, register_provider, scope_for
from vecten_shared.provider.environment import (
    EnvironmentOutputs,
    PostgresOutput,
    environment_file,
)
from vecten_shared.provider.scaleway import (
    ScalewayRuntimeProvider,
    SecretManagerError,
    scaleway_resource_name,
    scaleway_secret_path,
)

CLOUD_ROOT = "scaleway"
# What `destroying` changes in the cloud root: force_destroy on the artifacts bucket, which
# deleting a non-empty bucket needs in state. RDB has no deletion protection to lift.
ARTIFACTS_BUCKET = "module.environment.scaleway_object_bucket.artifacts"
# They own every warehouse object; their creator, the instance's administrator, holds ADMIN.
ADMIN_GRANTS = ("warehouse_owner", "loader")
# The applications bootstrap records as `app_<key>`, which the module takes by key.
APPLICATION_PREFIX = "app_"
PUBLIC_CIDRS = "database_public_cidrs"
ADMIN_SECRET_SCOPE = "setup"


def admin_secret_name(key: str) -> str:
    """The secret holding the instance `key`'s administrator, which Terraform writes."""
    return f"SETUP_{key.upper()}_ADMIN_PASSWORD"


def instance_certificate(rdb: Any, instance_id: str, name: str) -> bytes:
    """The PEM certificate authority of an RDB instance.

    `GET /rdb/v1/regions/{region}/instances/{instance_id}/certificate` answers a file object,
    `{name, content_type, content}`, whose `content` is base64 (Scaleway SDK,
    `scaleway/rdb/v1/api.py` `get_instance_certificate`, `scaleway_core/bridge/scwfile.py`).
    """
    doc = rdb.request("GET", f"/instances/{quote(instance_id, safe='')}/certificate")
    try:
        pem = base64.b64decode(doc.get("content") or "", validate=True)
    except binascii.Error, ValueError, TypeError:
        pem = b""
    if b"-----BEGIN CERTIFICATE-----" not in pem:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"RDB answered no PEM certificate for database instance {name}",
        )
    return pem


def _secret_failure(operation: str, error: SecretManagerError) -> Failure:
    reason = f"HTTP {error.status}" + (f" {error.kind}" if error.kind else "")
    return Failure(ExitCode.TOOL_FAILED, f"Secret Manager {operation} failed: {reason}")


class ScalewayProvider(ScalewayRuntimeProvider):
    """The Scaleway control plane (`vecten.deploy.control.ControlPlane`)."""

    cloud = CLOUD_ROOT
    # The plan key reads nothing in the resources project, so a refresh would fail on every
    # resource there.
    cloud_only_plans_refresh = False
    admin_grants = ADMIN_GRANTS
    # `scaleway_rdb_instance` has no deletion protection, so the apply refuses instead.
    guarded_resources = ("scaleway_rdb_instance",)

    def __init__(self, project_root: Path, env: str):
        # `<prefix>-state` names an environment's state project, so an environment whose name
        # ends in `-state` would name its resources project after another one's state project.
        if env.endswith("-state"):
            raise Failure(
                ExitCode.USAGE,
                f"environment {env!r} ends in -state, which Scaleway reserves for state "
                "projects; choose another name",
            )
        super().__init__(project_root, env)
        self._operator: str | None = None
        self._organization: str | None = None
        self._storages: dict[str, Any] = {}
        self._known: set[str] = set()
        # The hosts `after_cloud_apply` handed a key to, which ship gives the first-ship window.
        self._keyed: frozenset[str] = frozenset()

    def region(self) -> str:
        # Bootstrap runs before the first deploy writes the outputs, so the committed tfvars
        # and then the deploy answers name the region until then.
        if environment_file(self.project_root, self.env).is_file():
            return self.outputs().region
        tfvars = env_tfvars_path(self.project_root, self.env)
        if tfvars.is_file():
            region = read_tfvars(tfvars).get("region")
            if isinstance(region, str) and region:
                return region
        deploy = self.config().deploy
        if deploy is None:
            raise Failure(ExitCode.REFUSED, "no deploy answers; run `vecten init --deploy` first")
        return deploy.region

    def may_write(self, name: str) -> bool:
        return True

    # --- what bootstrap recorded, and the key every call signs with ---------------------------

    def cloud_id(self, name: str) -> str:
        """The `cloud_ids` fact `name` that bootstrap recorded; refuses when it is missing."""
        facts = self.config().environments.get(self.env)
        found = facts.cloud_ids.get(name) if facts else None
        if not found:
            raise Failure(
                ExitCode.PREREQUISITE,
                f'vecten.toml: [environments.{self.env}] cloud_ids["{name}"] is not set; run '
                f"`vecten bootstrap --env {self.env}`",
            )
        return found

    def application_ids(self) -> dict[str, str]:
        facts = self.config().environments.get(self.env)
        ids = facts.cloud_ids if facts else {}
        return {
            key.removeprefix(APPLICATION_PREFIX): value
            for key, value in sorted(ids.items())
            if key.startswith(APPLICATION_PREFIX)
        }

    def storage(self, project: str) -> Any:
        """Object Storage acting in the project bootstrap recorded as `project` (`project`
        for the artifacts bucket, `state_project` for the state bucket)."""
        if project not in self._storages:
            key = clients.credentials(organization=False)
            self._storages[project] = clients.object_storage(
                self.region(), key.access_key, key.secret_key, self.cloud_id(project)
            )
        return self._storages[project]

    def remember(self, value: str) -> None:
        """Record a value read in this process so no reported line can carry it."""
        if value:
            self._known.add(value)

    def known_values(self) -> set[str]:
        return set(self._known)

    def check_login(self) -> str:
        """The organization the human's key acts in, after Scaleway accepted the key."""
        creds = clients.credentials()
        self._operator = bootstrap.operator_principal(clients.client("iam"), creds)
        self._organization = creds.organization_id
        return creds.organization_id

    def bootstrap_env(
        self,
        env: str,
        repository: str,
        branch: str,
        account: str,
        *,
        subject_prefixes: Sequence[str],
    ) -> CloudBootstrap:
        """The projects, state bucket, applications, policies and keys of `env`; the deploy
        key goes to the GitHub environment and the plan key to the repository, as secrets.
        Scaleway trusts no OIDC issuer, so CI holds keys and `subject_prefixes` go unused."""
        self._check_env(env)
        if self._operator is None or self._organization != account:
            self.check_login()
        assert self._operator is not None
        config = self.config()
        values = check_tfvars(self.project_root, config, env)
        target = bootstrap.Target(
            project=config.project.name,
            env=env,
            region=self.region(),
            organization=account,
            operator=self._operator,
            layout=bootstrap.Layout(
                components=tuple(str(c) for c in values["components"]),
                profile=str(values["profile"]),
                warehouse=str(values["warehouse"]),
            ),
            # The zone the module writes records into, which tfvars names.
            dns_zone=str(values["dns_zone"]),
        )
        facts = config.environments.get(env)
        prepared = bootstrap.prepare(target, dict(facts.cloud_ids) if facts else {})
        return CloudBootstrap(
            state_bucket=prepared.state_bucket,
            environment_variables=prepared.environment_variables,
            repository_variables={},
            report=prepared.report,
            environment_secrets=prepared.environment_secrets,
            repository_secrets=prepared.repository_secrets,
            cloud_ids=prepared.cloud_ids,
            repository_use=None,
        )

    # --- Terraform ---------------------------------------------------------------------------

    def terraform_roots(
        self, env: str, *, cloud_only: bool = False, read_only: bool = False
    ) -> list[TerraformRoot]:
        """The cloud root, then any Snowflake root, which shares the Scaleway state bucket.

        The cloud root takes the resources project and the applications bootstrap recorded
        as `-var`s: they are IDs, not secrets, and tfvars must not repeat them.
        """
        self._check_env(env)
        config = self.config()
        root_dir = self.project_root / "deploy" / "terraform" / CLOUD_ROOT
        if not (root_dir / "main.tf").is_file():
            raise Failure(
                ExitCode.CONFIG,
                f"deploy/terraform/{CLOUD_ROOT}/main.tf is missing; run `vecten init --deploy`",
            )
        project_id = self.cloud_id("project")
        applications = json.dumps(self.application_ids(), separators=(",", ":"))
        from vecten.deploy.scaleway.bootstrap import operator_principal_of

        facts = config.environments.get(env)
        operator = operator_principal_of(facts.cloud_ids) if facts else None
        roots = [
            TerraformRoot(
                CLOUD_ROOT,
                root_dir,
                state_key(env, CLOUD_ROOT),
                root_var_file(env),
                extra_vars=(
                    ("project_id", project_id),
                    ("application_ids", applications),
                    ("dns_project_id", self.cloud_id("dns_project")),
                    *((("operator_principal", operator),) if operator else ()),
                ),
            )
        ]
        if cloud_only:
            return roots
        snowflake = snowflake_root(self.project_root, config, env, self, read_only=read_only)
        if snowflake is not None:
            roots.append(snowflake)
        return roots

    def backend_config(self, env: str, root: TerraformRoot) -> dict[str, str]:
        # The endpoint, region and locking are fixed in the rendered backend block.
        return {"bucket": require_bootstrapped(self.config(), env), "key": root.key}

    def terraform_env(self) -> dict[str, str]:
        """The key for the provider, and for the S3 backend as `<access key>@<state project>`,
        since the state bucket lives in a project the key does not prefer."""
        key = clients.credentials(organization=False)
        return {
            "AWS_ACCESS_KEY_ID": f"{key.access_key}@{self.cloud_id('state_project')}",
            "AWS_SECRET_ACCESS_KEY": key.secret_key,
            "SCW_ACCESS_KEY": key.access_key,
            "SCW_SECRET_KEY": key.secret_key,
        }

    # --- outputs -----------------------------------------------------------------------------

    def outputs_location(self) -> str:
        return f"s3://{require_bootstrapped(self.config(), self.env)}/{outputs_key(self.env)}"

    def store_outputs(self, raw: dict[str, Any]) -> EnvironmentOutputs:
        bucket = require_bootstrapped(self.config(), self.env)
        outputs = write_outputs(self.project_root, self.env, raw)
        # `ship` reads the hosts through `outputs()`, which must not answer from an earlier
        # read of the file this apply replaced.
        self._outputs = outputs
        key = outputs_key(self.env)
        try:
            self.storage("state_project").put_object(
                Bucket=bucket,
                Key=key,
                Body=outputs_text(outputs).encode("utf-8"),
                ContentType="application/json",
            )
        except clients.s3_errors() as exc:
            raise clients.s3_failure(f"PutObject of {key}", exc) from None
        return outputs

    def stored_outputs(self) -> EnvironmentOutputs | None:
        bucket = require_bootstrapped(self.config(), self.env)
        key = outputs_key(self.env)
        try:
            body = self.storage("state_project").get_object(Bucket=bucket, Key=key)["Body"].read()
        except clients.s3_errors() as exc:
            if clients.s3_missing(exc):
                return None
            raise clients.s3_failure(f"GetObject of {key}", exc) from None
        outputs = parse_stored_outputs(body, f"s3://{bucket}/{key}", self.env)
        save_outputs(self.project_root, self.env, outputs)
        self._outputs = outputs
        return outputs

    def forget_outputs(self) -> list[str]:
        """Remove the environment's outputs from the state bucket and the project, so `ship`
        finds no hosts to reach; returns what could not be removed and never raises."""
        left: list[str] = []
        key = outputs_key(self.env)
        try:
            bucket = require_bootstrapped(self.config(), self.env)
            self.storage("state_project").delete_object(Bucket=bucket, Key=key)
        except Failure as failure:
            left.append(f"the stored {key} ({failure.message})")
        except clients.s3_errors() as exc:
            reason = clients.s3_status(exc)[0] or type(exc).__name__
            left.append(f"s3://{bucket}/{key} (S3 DeleteObject failed: {reason})")
        path = environment_file(self.project_root, self.env)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            left.append(f"{path} ({type(exc).__name__})")
        self._outputs = None
        return left

    # --- after the apply, and shipping --------------------------------------------------------

    def check_refresh_safe(self) -> None:
        """Refuse while a server of the environment holds a `vecten-key` user data entry:
        the provider reads every entry into the non-sensitive `user_data` attribute, so a
        refresh would copy the key into the saved plan. Deletes nothing, because a plan-only
        run may change nothing in the cloud."""
        hosts = ship_mod.servers_holding_keys(self)
        if hosts:
            raise Failure(
                ExitCode.REFUSED,
                f"the {ship_mod.KEY_ENTRY} user data entry remains on {', '.join(hosts)}, and a "
                "refreshing plan would copy it into the saved plan; "
                f"`vecten deploy --env {self.env}` deletes the entry before it plans",
                details={"hosts": hosts},
            )

    def before_cloud_apply(self) -> None:
        """Delete any `vecten-key` user data entry a cancelled deploy left on the
        environment's servers, before a refresh copies it into state."""
        ship_mod.clear_leftover_keys(self)

    def after_cloud_apply(self, outputs: EnvironmentOutputs) -> None:
        """Close the public database endpoints tfvars no longer asks for, set each instance's
        administrator to the password in its secret, then hand each host its key.

        The provider suppresses the diff when the `load_balancer` block goes away, so
        Terraform drops the endpoint's ACL and leaves the endpoint itself open to every
        address; it is deleted here in the same run.
        """
        values = check_tfvars(self.project_root, self.config(), self.env)
        if not values.get(PUBLIC_CIDRS):
            project = self.config().project.name
            names = [rds_identifier(project, self.env, key) for key in sorted(outputs.postgres)]
            rdb = clients.client("rdb", outputs.region)
            close_public_endpoints(rdb, self.cloud_id("project"), names)
        self.reset_admin_passwords(outputs)
        self._keyed = frozenset(ship_mod.place_vm_keys(self, outputs))

    def reset_admin_passwords(self, outputs: EnvironmentOutputs) -> None:
        """Set the administrator of every instance in `outputs` to the password its
        `SETUP_<KEY>_ADMIN_PASSWORD` secret holds, each instance ready before and after.

        The secret is the truth. The provider sends the instance a password only when
        `password_wo_version` changes, while every apply draws a new ephemeral one for the
        secret version, so an apply that stops between the two leaves them apart until this
        runs. Every secret is read before the first password is set, so a missing one
        refuses with no instance changed.
        """
        project = self.config().project.name
        project_id = self.cloud_id("project")
        path = scaleway_secret_path(project, self.env, ADMIN_SECRET_SCOPE)
        secrets = clients.client("secret-manager", outputs.region)
        credentials: dict[str, tuple[str, str]] = {}
        for key in sorted(outputs.postgres):
            secret = f"{path}/{admin_secret_name(key)}"
            sid = find_secret(secrets, project_id, path, admin_secret_name(key))
            raw = read_latest(secrets, sid) if sid else None
            if raw is None:
                raise Failure(
                    ExitCode.PREREQUISITE,
                    f"the admin secret {secret} holds no version; run "
                    f"`vecten deploy --env {self.env}` again, whose apply writes it",
                    details={"secret": secret},
                )
            self.remember(raw)
            try:
                doc = json.loads(raw)
                user, password = doc["username"], doc["password"]
            except ValueError, KeyError, TypeError:
                user = password = None
            if not isinstance(user, str) or not isinstance(password, str) or not password:
                raise Failure(
                    ExitCode.TOOL_FAILED,
                    f"the admin secret {secret} is not username/password JSON",
                    details={"secret": secret},
                )
            self.remember(password)
            credentials[key] = (user, password)
        rdb = clients.client("rdb", outputs.region)
        for key, (user, password) in credentials.items():
            name = rds_identifier(project, self.env, key)
            try:
                found = find_instance(rdb, project_id, name)
                if found is None:
                    raise Failure(
                        ExitCode.TOOL_FAILED,
                        f"database instance {name} is not in the environment's project, so "
                        "its admin password could not be set",
                        details={"instance": name},
                    )
                instance_id = str(found["id"])
                wait_ready(rdb, instance_id, name)
                set_user_password(rdb, instance_id, user, password)
                wait_ready(rdb, instance_id, name)
            except clients.ApiError as error:
                reason = f"HTTP {error.status}" + (f" {error.kind}" if error.kind else "")
                raise Failure(
                    ExitCode.TOOL_FAILED,
                    f"setting the admin password of database instance {name} failed: {reason}",
                    details={"instance": name},
                ) from None

    def ship_hosts(
        self,
        project_root: Path,
        config: VectenConfig,
        env: str,
        outputs: EnvironmentOutputs,
        commit: str,
    ) -> list[str]:
        """Build and ship one bundle per host, setup host first; stops at the first failure."""
        self._check_env(env)
        return ship_mod.ship_hosts(
            self, project_root, config, env, outputs, commit, keyed=self._keyed
        )

    # --- destroy -----------------------------------------------------------------------------

    def final_snapshots(
        self,
        outputs: EnvironmentOutputs,
        now: datetime,
        *,
        on_created: Callable[[str], None],
        absent: list[str],
        expires: dict[str, str],
    ) -> list[str]:
        return snapshots_mod.final_snapshots(
            clients.client("rdb", outputs.region),
            self.config().project.name,
            self.env,
            self.cloud_id("project"),
            outputs,
            now,
            on_created=on_created,
            absent=absent,
            expires=expires,
        )

    def lift_protection(
        self,
        terraform: TerraformRunner,
        root: TerraformRoot,
        state: list[str],
        *,
        checked: set[str],
        absent: list[str],
    ) -> TerraformRoot:
        """Add `destroying=true` to `root` and apply it to the artifacts bucket, so destroy
        may empty it; the databases carry no deletion protection, so the final snapshots are
        their only guard.

        Any `vecten-key` user data entry a cancelled deploy left is deleted first: the plan
        here and the destroy after it refresh, and a failed destroy keeps what it read in
        state.
        """
        ship_mod.clear_leftover_keys(self)
        root = replace(root, extra_vars=(*root.extra_vars, (DESTROYING_VAR, "true")))
        if ARTIFACTS_BUCKET in state:
            apply_update_only(terraform, root, [ARTIFACTS_BUCKET])
        return root

    # --- add-admin ---------------------------------------------------------------------------

    def require_admin_access(self) -> None:
        """Refuse unless tfvars opens the databases' public endpoint: Scaleway offers no SSH
        and no run-command API to tunnel through, and a private network admits no laptop."""
        values = check_tfvars(self.project_root, self.config(), self.env)
        if not values.get(PUBLIC_CIDRS):
            raise Failure(
                ExitCode.REFUSED,
                f"vecten user add-admin reaches the warehouse over its public endpoint, which "
                f"deploy/envs/{self.env}.tfvars does not open: set {PUBLIC_CIDRS} to the "
                f'addresses you connect from (for example ["203.0.113.7/32"]), run '
                f"`vecten deploy --env {self.env}`, then retry",
            )

    def master_credential(self, instance: PostgresOutput) -> tuple[str, str]:
        """The user name and password in the instance's `SETUP_<KEY>_ADMIN_PASSWORD` secret,
        which Terraform wrote and `master_secret` names by ID."""
        path = f"/secrets/{quote(instance.master_secret, safe='')}/versions/latest_enabled/access"
        try:
            doc = self._call("AccessSecretVersion", "GET", path)
        except SecretManagerError as error:
            raise _secret_failure("AccessSecretVersion of the admin secret", error) from None
        try:
            raw = base64.b64decode(doc.get("data") or "", validate=True).decode("utf-8")
            credential = json.loads(raw)
            user, password = credential["username"], credential["password"]
        except binascii.Error, ValueError, KeyError, TypeError:
            raise Failure(
                ExitCode.TOOL_FAILED,
                "the warehouse instance's admin secret is not username/password",
            ) from None
        self.remember(raw)
        self.remember(password)
        return user, password

    @contextmanager
    def admin_connection(
        self, outputs: EnvironmentOutputs, instance: PostgresOutput
    ) -> Iterator[dict[str, Any]]:
        """The instance's public endpoint, which RDB serves and the outputs do not name, with
        the server verified against the instance's own certificate authority.

        The endpoint crosses the internet, so an unverified server could ask for the master
        password. RDB hands out the instance's CA certificate; the certificate's names are not
        documented to cover the load-balancer address, so the chain is checked and the name
        is not (`verify-ca`). The certificate file lives only while the block runs.
        """
        key = next((k for k, v in outputs.postgres.items() if v == instance), None)
        if key is None:
            raise Failure(ExitCode.INTERNAL, "the admin instance is not among the outputs")
        name = rds_identifier(self.config().project.name, self.env, key)
        rdb = clients.client("rdb", outputs.region)
        found = find_instance(rdb, self.cloud_id("project"), name)
        endpoint = next(iter(public_endpoints(found)), None) if found else None
        host = (endpoint.get("ip") or endpoint.get("hostname")) if endpoint else None
        port = endpoint.get("port") if endpoint else None
        if found is None or not host or not isinstance(port, int):
            raise Failure(
                ExitCode.PREREQUISITE,
                f"database instance {name} has no public endpoint yet; run "
                f"`vecten deploy --env {self.env}`, which opens it to {PUBLIC_CIDRS}",
            )
        pem = instance_certificate(rdb, str(found["id"]), name)
        handle, path = tempfile.mkstemp(prefix="vecten-rdb-ca-", suffix=".pem")
        try:
            # mkstemp creates the file readable by its owner only.
            with os.fdopen(handle, "wb") as out:
                out.write(pem)
            yield {"host": host, "port": port, "sslmode": "verify-ca", "sslrootcert": path}
        finally:
            Path(path).unlink(missing_ok=True)

    # --- secrets -----------------------------------------------------------------------------

    def secret_id(self, name: str) -> str:
        return scaleway_resource_name(self.config().project.name, self.env, name)

    def read_object(self, key: str) -> bytes | None:
        bucket = self.outputs().bucket
        try:
            return self.storage("project").get_object(Bucket=bucket, Key=key)["Body"].read()
        except clients.s3_errors() as exc:
            if clients.s3_missing(exc):
                return None
            raise clients.s3_failure(f"GetObject of {key}", exc) from None

    def read_state_object(self, key: str) -> bytes | None:
        bucket = require_bootstrapped(self.config(), self.env)
        try:
            return self.storage("state_project").get_object(Bucket=bucket, Key=key)["Body"].read()
        except clients.s3_errors() as exc:
            if clients.s3_missing(exc):
                return None
            raise clients.s3_failure(f"GetObject of {key}", exc) from None

    def _call(
        self,
        operation: str,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # The control plane's key comes from the environment or the scw configuration, as for
        # every other call here; the runtime class reads SCW_SECRET_KEY alone.
        try:
            return clients.client("secret-manager", self.region()).request(
                method, path, json=payload, params=query
            )
        except clients.ApiError as error:
            raise SecretManagerError(error.status, error.kind) from None

    def get_secret(self, name: str) -> str:
        value = super().get_secret(name)
        self.remember(value)
        return value

    def put_secret(self, name: str, value: str) -> None:
        """Add `value` as the secret's newest version, creating the secret at its scope's
        path first when Terraform has not (a connector's secret, a `--from-local` copy)."""
        check_secret_name(name)
        if self._secret_id(name) is None:
            path = scaleway_secret_path(self.config().project.name, self.env, scope_for(name))
            payload = {
                "project_id": self._project_id(),
                "name": name,
                "path": path,
                "protected": False,
                "description": "Set by vecten secret set.",
                "tags": ["vecten"],
            }
            try:
                self._call("CreateSecret", "POST", "/secrets", payload=payload)
            except SecretManagerError as error:
                raise _secret_failure("CreateSecret", error) from None
        super().put_secret(name, value)


def register() -> None:
    """Hold the `scaleway` slot unless something other than the runtime class already holds
    it; code on the VMs never imports this module, so it keeps the runtime class's guard."""
    current = provider_factory("scaleway")
    if current is None or current is ScalewayRuntimeProvider:
        register_provider("scaleway", ScalewayProvider)


register()
