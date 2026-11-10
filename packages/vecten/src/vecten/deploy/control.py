"""The cloud-neutral control plane: what `bootstrap`, `deploy`, `ship`, `destroy` and
`user add-admin` ask of a cloud, and the Terraform sequence they run over it.

Importing this module loads no cloud SDK: `get_control_plane` imports a cloud's module only
when that cloud is asked for, so the CLI can import it at any time.
"""

from __future__ import annotations

import importlib
import json
import os
import tempfile
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from vecten.deploy.guards import require_bootstrapped
from vecten.deploy.snowflake import has_snowflake_root
from vecten.deploy.terraform import (
    PlannedRoot,
    TerraformRoot,
    TerraformRunner,
    apply_root,
    init_backend,
    plan_root,
    terraform_apply,
    terraform_destroy,
    terraform_outputs,
    terraform_plan,
    terraform_plan_changes,
    terraform_state_list,
)
from vecten_shared.output import ExitCode, Failure

if TYPE_CHECKING:
    from vecten_shared.config import VectenConfig
    from vecten_shared.provider.environment import EnvironmentOutputs, PostgresOutput

SUPPORTED_CLOUDS: tuple[str, ...] = ("aws", "scaleway")
# The module whose import registers a cloud's control-plane class as that cloud's provider.
_CLI_PROVIDER_MODULES = {"aws": "vecten.deploy.aws", "scaleway": "vecten.deploy.scaleway"}
# Plan actions that change nothing in the cloud.
NO_CHANGE = frozenset({"no-op", "read"})
# The cloud root's variable that lifts deletion protection and lets the bucket be emptied.
DESTROYING_VAR = "destroying"
RUNBOOK = "docs/runbook.md"
# `module:Class` of a control plane that replaces every cloud, as the mocked provider the
# scenarios and the scripted-human evaluation run against does.
CONTROL_PLANE_VAR = "VECTEN_CONTROL_PLANE"


def changed_addresses(
    changes: list[dict[str, Any]], actions: set[str], types: tuple[str, ...] | None = None
) -> list[str]:
    """The addresses among a plan's `resource_changes` whose actions include one of `actions`,
    limited to resources of `types` when given. A replacement plans both `delete` and
    `create`, so either catches it."""
    return sorted(
        c.get("address", "?")
        for c in changes
        if actions & set(c.get("change", {}).get("actions", []))
        and (types is None or c.get("type") in types)
    )


def refuse_guarded_changes(
    plane: ControlPlane, runner: TerraformRunner, planned: PlannedRoot
) -> None:
    """Refuse a cloud-root plan that deletes or replaces a resource of `plane.guarded_resources`,
    before anything is applied."""
    root = planned.root
    changes = terraform_plan_changes(
        runner, root.root_dir, planned.plan_file, env=root.env, secrets=root.secrets
    )
    refused = changed_addresses(changes, {"delete"}, plane.guarded_resources)
    if refused:
        raise Failure(
            ExitCode.REFUSED,
            f"the plan would delete or replace {', '.join(refused)}, losing its data; nothing "
            "was applied. A database is replaced only deliberately: by hand, after a "
            f"snapshot, as {RUNBOOK} describes. Revert the change that causes it, or follow "
            "that procedure",
            details={"addresses": refused},
        )


@dataclass(frozen=True)
class CloudBootstrap:
    """What a cloud's part of `bootstrap` prepared, for the cloud-neutral rest to publish.

    `environment_variables` go to the GitHub environment and `repository_variables` to the
    repository; `environment_secrets` and `repository_secrets` likewise, as GitHub secrets,
    and are the only fields holding secret values. `report` holds the cloud's own result
    fields, placed in `bootstrap`'s result after `state_bucket`; none of them is a secret.
    `cloud_ids`, when set, replaces the environment's recorded `cloud_ids` fact.
    `repository_use` names what relies on GitHub's spelling of the repository; None when
    nothing does, and then a casing difference earns no note.
    """

    state_bucket: str
    environment_variables: dict[str, str]
    repository_variables: dict[str, str]
    report: dict[str, Any] = field(default_factory=dict)
    environment_secrets: dict[str, str] = field(default_factory=dict, repr=False)
    repository_secrets: dict[str, str] = field(default_factory=dict, repr=False)
    cloud_ids: dict[str, str] | None = None
    repository_use: str | None = "OIDC trust"


@runtime_checkable
class ControlPlane(Protocol):
    """One cloud as the deploy commands use it, bound to a project and an environment.

    Every method that can refuse without changing the cloud does so before its first write.
    No method puts a secret value into a return value, a message or Terraform's arguments.
    """

    cloud: str
    """The cloud's name, which is also its Terraform root's name under deploy/terraform/."""
    cloud_only_plans_refresh: bool
    """Whether a `--cloud-only` plan, the pull-request plan run with the plan identity,
    refreshes. False where that identity cannot read every resource without also being able
    to change one; such a plan compares the configuration with the stored state."""
    project_root: Path
    env: str

    admin_grants: tuple[str, ...]
    """The roles `user add-admin` grants an admin login on the warehouse instance; each one
    the instance's administrator can grant."""

    guarded_resources: tuple[str, ...]
    """Resource types an apply of the cloud root refuses to delete or replace. Empty where the
    cloud's own deletion protection already stops Terraform from deleting a database."""

    def config(self) -> VectenConfig:
        """The project's vecten.toml, read once."""
        ...

    def check_login(self) -> str:
        """The account the human's or the CI job's login acts in; refuses with exit 3 when
        there is no usable login. Makes no write."""
        ...

    def bootstrap_env(
        self,
        env: str,
        repository: str,
        branch: str,
        account: str,
        *,
        subject_prefixes: Sequence[str],
    ) -> CloudBootstrap:
        """Create or update the cloud side of `env`: the state store and the identities CI
        deploys and plans with. `repository` is GitHub's spelling, `branch` the default
        branch, `account` what `check_login` returned and `subject_prefixes` the forms of the
        repository a GitHub OIDC `sub` claim takes. Idempotent."""
        ...

    def terraform_roots(
        self, env: str, *, cloud_only: bool = False, read_only: bool = False
    ) -> list[TerraformRoot]:
        """The roots `deploy` plans, in apply order: the cloud root, then any Snowflake root.

        `cloud_only` leaves the Snowflake root out and reads no Snowflake secret.
        `read_only` writes no secret while resolving the roots. Everything that can refuse
        runs here, before the first Terraform call.
        """
        ...

    def backend_config(self, env: str, root: TerraformRoot) -> dict[str, str]:
        """The `-backend-config` settings that point `root` at its state, in argument order."""
        ...

    def terraform_env(self) -> dict[str, str]:
        """Environment variables every Terraform call needs beyond the inherited ones; each
        value is masked in Terraform's failure output, so it holds credentials only."""
        ...

    def outputs(self) -> EnvironmentOutputs:
        """The outputs in the project's environment file; refuses when it is missing."""
        ...

    def store_outputs(self, raw: dict[str, Any]) -> EnvironmentOutputs:
        """Validate the cloud root's `terraform output -json` and write the outputs to the
        project and to the environment's state store, where `ship` and `destroy` read them."""
        ...

    def stored_outputs(self) -> EnvironmentOutputs | None:
        """The outputs in the state store, also written into the project; None when the
        store holds none. A malformed object or another environment's refuses with exit 5."""
        ...

    def outputs_location(self) -> str:
        """Where `stored_outputs` reads, as messages name it."""
        ...

    def forget_outputs(self) -> list[str]:
        """Remove the outputs from the state store and the project, so `ship` finds no hosts;
        returns a line for each copy that could not be removed and never raises."""
        ...

    def check_refresh_safe(self) -> None:
        """Refuse with exit 4 when a refresh would read a credential into Terraform's plan or
        state. Reads only and changes nothing; `plan_environment` calls it once before a
        refreshing plan."""
        ...

    def before_cloud_apply(self) -> None:
        """Bring the cloud in line before `deploy`'s first Terraform call, whose refresh
        writes what it reads into state; makes no change on a first deploy."""
        ...

    def after_cloud_apply(self, outputs: EnvironmentOutputs) -> None:
        """Bring the cloud in line with what Terraform cannot do, right after the cloud root
        applied and its outputs were stored, before any other root or ship; raises when a
        host cannot be readied for shipping."""
        ...

    def ship_hosts(
        self,
        project_root: Path,
        config: VectenConfig,
        env: str,
        outputs: EnvironmentOutputs,
        commit: str,
    ) -> list[str]:
        """Build and ship one bundle per host at `commit`, setup host first, and return the
        hosts shipped in order; stops at the first failure, which raises."""
        ...

    def final_snapshots(
        self,
        outputs: EnvironmentOutputs,
        now: datetime,
        *,
        on_created: Callable[[str], None],
        absent: list[str],
        expires: dict[str, str],
    ) -> list[str]:
        """Snapshot every Postgres instance in `outputs` and wait for each; returns the
        snapshot names. `on_created` hears each name as it is taken; an instance that no
        longer exists is appended to `absent` by its cloud name and skipped. A cloud that
        deletes snapshots on a date of its own records it in `expires`, by snapshot name."""
        ...

    def lift_protection(
        self,
        terraform: TerraformRunner,
        root: TerraformRoot,
        state: list[str],
        *,
        checked: set[str],
        absent: list[str],
    ) -> TerraformRoot:
        """Make the cloud root destroyable and return the root `terraform destroy` runs with.

        Called once, for the cloud root, after its backend is initialised and with its
        non-empty `state`. `checked` names the Postgres keys `final_snapshots` already asked
        the cloud about; `absent` holds the instances it found gone. A cloud without
        deletion protection returns `root` unchanged.
        """
        ...

    def require_admin_access(self) -> None:
        """Refuse, from the project's files alone, when the environment offers the laptop no
        way to its databases; makes no cloud call."""
        ...

    def master_credential(self, instance: PostgresOutput) -> tuple[str, str]:
        """The user name and password of `instance`'s administrator, read with the human's
        login."""
        ...

    def admin_connection(
        self, outputs: EnvironmentOutputs, instance: PostgresOutput
    ) -> AbstractContextManager[dict[str, Any]]:
        """Open the way from the laptop to `instance` and yield the psycopg settings that
        reach it: `host`, `port` and `sslmode`. The way closes when the block ends."""
        ...

    def read_object(self, key: str) -> bytes | None:
        """The object `key` in the environment's bucket, read with the human's login; None
        when it does not exist. `vecten issues` reads task logs and dbt results this way."""
        ...

    def read_state_object(self, key: str) -> bytes | None:
        """The object `key` in the environment's state bucket, read with the human's login;
        None when it does not exist."""
        ...

    def secret_id(self, name: str) -> str:
        """The cloud's identifier of the secret `name`, as messages print it."""
        ...

    def put_secret(self, name: str, value: str) -> None: ...

    def get_secret(self, name: str) -> str: ...

    def list_secrets(self) -> list[str]: ...


def _named_factory() -> Callable[[Path, str], ControlPlane] | None:
    """The class `VECTEN_CONTROL_PLANE` names, or None when it is unset."""
    value = os.environ.get(CONTROL_PLANE_VAR)
    if not value:
        return None
    module_name, sep, attribute = value.partition(":")
    try:
        if not sep or not module_name or not attribute:
            raise ValueError(value)
        return getattr(importlib.import_module(module_name), attribute)
    except ImportError, AttributeError, ValueError:
        raise Failure(
            ExitCode.USAGE,
            f"{CONTROL_PLANE_VAR}={value!r} names no importable class; use module:Class",
        ) from None


def get_control_plane(cloud: str, project_root: Path, env: str) -> ControlPlane:
    """The control plane of `cloud` for `env`; refuses a cloud this release cannot deploy."""
    named = _named_factory()
    if named is not None:
        return named(project_root, env)
    if cloud == "aws":
        from vecten.deploy.aws import AwsProvider

        return AwsProvider(project_root, env)
    if cloud == "scaleway":
        from vecten.deploy.scaleway import ScalewayProvider

        return ScalewayProvider(project_root, env)
    raise Failure(
        ExitCode.CONFIG,
        f"unknown cloud {cloud!r}; one of: {', '.join(repr(c) for c in SUPPORTED_CLOUDS)}",
    )


def register_cli_provider(cloud: str) -> None:
    """Make `cloud`'s control-plane class the provider `get_provider` returns for it.

    The runtime classes refuse most secret writes; `secret set --env` writes any name.
    """
    named = _named_factory()
    if named is not None:
        from vecten_shared.provider import register_provider

        register_provider(cloud, named)
        return
    module = _CLI_PROVIDER_MODULES.get(cloud)
    if module is not None:
        importlib.import_module(module).register()


def environment_roots(
    plane: ControlPlane, env: str, *, cloud_only: bool = False, read_only: bool = False
) -> list[TerraformRoot]:
    """`plane`'s roots with its `terraform_env` added to each root's environment and secrets."""
    roots = plane.terraform_roots(env, cloud_only=cloud_only, read_only=read_only)
    extra = plane.terraform_env()
    if not extra:
        return roots
    return [
        replace(r, env={**extra, **(r.env or {})}, secrets=(*r.secrets, *extra.values()))
        for r in roots
    ]


def plan_environment(
    plane: ControlPlane,
    env: str,
    terraform: TerraformRunner,
    out_dir: Path,
    *,
    cloud_only: bool = False,
) -> list[PlannedRoot]:
    """Init every root against the environment's state and save its plan into `out_dir`.

    A refreshing plan copies what it reads into the saved plan, so `check_refresh_safe`
    runs before the first root is planned whenever the plans refresh.
    """
    require_bootstrapped(plane.config(), env)
    roots = environment_roots(plane, env, cloud_only=cloud_only)
    refresh = plane.cloud_only_plans_refresh or not cloud_only
    if refresh:
        plane.check_refresh_safe()
    return [
        plan_root(
            terraform,
            root,
            backend=plane.backend_config(env, root),
            out_dir=out_dir,
            refresh=refresh,
        )
        for root in roots
    ]


def apply_environment(
    plane: ControlPlane, env: str, terraform: TerraformRunner, plans: list[PlannedRoot]
) -> EnvironmentOutputs:
    """Apply the saved plans in order and store the cloud root's outputs.

    The outputs are stored right after the cloud root applies, so a later root's failure
    still leaves `ship` the outputs of the infrastructure that exists; `after_cloud_apply`
    follows at once, so what the cloud root left undone is done in the same run.
    """
    require_bootstrapped(plane.config(), env)
    if plane.guarded_resources:
        for planned in plans:
            if planned.root.name == plane.cloud:
                refuse_guarded_changes(plane, terraform, planned)
    outputs: EnvironmentOutputs | None = None
    for planned in plans:
        apply_root(terraform, planned)
        if planned.root.name == plane.cloud:
            root = planned.root
            raw = terraform_outputs(terraform, root.root_dir, env=root.env, secrets=root.secrets)
            outputs = plane.store_outputs(raw)
            plane.after_cloud_apply(outputs)
    if outputs is None:
        raise Failure(ExitCode.INTERNAL, f"no {plane.cloud} root was among the plans applied")
    return outputs


def apply_update_only(runner: TerraformRunner, root: TerraformRoot, targets: list[str]) -> None:
    """Plan and apply `root` limited to `targets`, refusing any plan that would create or
    delete.

    `targets` are state addresses, so local edits elsewhere in the root never reach the
    environment and nothing already gone is planned back; the update-only check catches
    a create or replace the targets' own configuration would still cause.
    """
    with tempfile.TemporaryDirectory(prefix="vecten-destroy-") as tmp:
        plan_file = Path(tmp) / f"{root.name}.tfplan"
        terraform_plan(
            runner,
            root.root_dir,
            root.var_file,
            plan_file,
            root.extra_vars,
            env=root.env,
            secrets=root.secrets,
            targets=targets,
        )
        changes = terraform_plan_changes(
            runner, root.root_dir, plan_file, env=root.env, secrets=root.secrets
        )
        refused = changed_addresses(changes, {"create", "delete"})
        if refused:
            raise Failure(
                ExitCode.REFUSED,
                "preparing the environment for destroy would create or delete "
                f"{', '.join(refused)}; nothing was applied. Make the environment match "
                "its committed deploy/ files first",
                details={"addresses": refused},
            )
        updates = [c for c in changes if set(c.get("change", {}).get("actions", [])) - NO_CHANGE]
        if updates:
            terraform_apply(runner, root.root_dir, plan_file, env=root.env, secrets=root.secrets)


def provision_environment(plane: ControlPlane, env: str, terraform: TerraformRunner) -> dict:
    """Plan and apply every root in two steps, then store the outputs.

    `before_cloud_apply` runs first, so the refresh check in `plan_environment` finds the
    cloud already in line. The plan-only path never calls it, because the plan identity may
    change nothing in the cloud; there the check refuses instead. Returns `roots`, the names
    applied in order, and `outputs`, the environment's outputs as written to the project and
    the state store.
    """
    plane.before_cloud_apply()
    with tempfile.TemporaryDirectory(prefix="vecten-plan-") as tmp:
        plans = plan_environment(plane, env, terraform, Path(tmp))
        outputs = apply_environment(plane, env, terraform, plans)
    return {"roots": [p.root.name for p in plans], "outputs": outputs.model_dump()}


def held_state_lock(plane: ControlPlane, env: str, roots: Sequence[TerraformRoot]) -> None:
    """Refuse while another run holds a root's state lock, read from its `<key>.tflock`.

    `destroy` snapshots before Terraform waits on the lock, so a second destroy would take a
    full snapshot per instance before failing on it. A body that is not Terraform's lock
    record still means the lock object exists.
    """
    for root in roots:
        key = f"{root.key}.tflock"
        body = plane.read_state_object(key)
        if body is None:
            continue
        try:
            record = json.loads(body)
        except ValueError:
            record = None
        if not isinstance(record, dict):
            record = {}
        who, operation, created = (
            str(record.get(field) or "unknown") for field in ("Who", "Operation", "Created")
        )
        lock = f"s3://{plane.backend_config(env, root)['bucket']}/{key}"
        raise Failure(
            ExitCode.REFUSED,
            f"another run holds the state lock of the {root.name} root since {created} "
            f"({who}, {operation}); nothing was snapshotted or destroyed. Wait for that run "
            f"to finish and run destroy again; if no run is active, the lock is stale: "
            f"delete {lock}",
            details={"lock": lock, "who": who, "operation": operation, "created": created},
        )


def destroy_environment(
    plane: ControlPlane,
    env: str,
    terraform: TerraformRunner,
    *,
    delete_data: bool,
    outputs: EnvironmentOutputs | None,
    notify: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> dict:
    """Take a final snapshot of every instance in `outputs`, lift deletion protection on the
    cloud root, then destroy every root in reverse apply order.

    Without `delete_data` a Snowflake root is left standing, with its database. A root
    whose state is already empty is skipped. The caller has confirmed the environment's
    name. Returns `snapshots`, `snapshot_expiry` (snapshot name to the date the cloud deletes
    it, empty where it keeps snapshots), `instances_absent`, `destroyed` and
    `already_destroyed` (root names in destroy order), `data_deleted` and `snowflake`
    (`kept`, `destroyed`, or None without a Snowflake root).
    """
    config = plane.config()
    require_bootstrapped(config, env)
    # Roots are resolved first: a Snowflake root refuses here for missing deploy secrets,
    # before a snapshot or a Terraform call.
    roots = environment_roots(plane, env, cloud_only=not delete_data, read_only=True)
    held_state_lock(plane, env, roots)
    say = notify or (lambda line: None)
    snapshots: list[str] = []
    absent: list[str] = []
    expires: dict[str, str] = {}
    if not delete_data:
        if outputs is None:
            raise Failure(ExitCode.INTERNAL, "destroy keeps the data only with the outputs")
        snapshots = plane.final_snapshots(
            outputs,
            now or datetime.now(UTC),
            on_created=lambda snapshot: say(f"final snapshot {snapshot}"),
            absent=absent,
            expires=expires,
        )
        for instance in absent:
            say(f"no snapshot of {instance}: the instance no longer exists")
    destroyed: list[str] = []
    already: list[str] = []
    for root in reversed(roots):
        init_backend(
            terraform,
            root.root_dir,
            plane.backend_config(env, root),
            env=root.env,
            secrets=root.secrets,
        )
        state = terraform_state_list(terraform, root.root_dir, env=root.env, secrets=root.secrets)
        if not state:
            already.append(root.name)
            continue
        if root.name == plane.cloud:
            # The snapshot step already asked the cloud about every instance in the outputs.
            checked = set(outputs.postgres) if outputs is not None and not delete_data else set()
            root = plane.lift_protection(terraform, root, state, checked=checked, absent=absent)
        terraform_destroy(
            terraform,
            root.root_dir,
            root.var_file,
            root.extra_vars,
            env=root.env,
            secrets=root.secrets,
        )
        destroyed.append(root.name)
    snowflake = None
    if has_snowflake_root(config):
        snowflake = "destroyed" if delete_data else "kept"
    return {
        "snapshots": snapshots,
        "snapshot_expiry": expires,
        "instances_absent": absent,
        "destroyed": destroyed,
        "already_destroyed": already,
        "data_deleted": delete_data,
        "snowflake": snowflake,
    }
