"""AWS for the vecten CLI: the runtime provider plus bootstrap, provisioning through Terraform,
writes to any secret and shipping.

Importing this module registers `AwsProvider` under `aws`, replacing the runtime class that
`vecten_shared` fills the slot with. CLI paths import it inside the function that needs it,
so importing the CLI loads no boto3.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from vecten.deploy.aws import clients
from vecten.deploy.aws.bootstrap import (
    caller_account,
    ensure_boundary_policy,
    ensure_deploy_role,
    ensure_oidc_provider,
    ensure_plan_role,
    ensure_state_bucket,
    login_failure,
)
from vecten.deploy.aws.clients import aws_errors, aws_failure, aws_reason, error_code
from vecten.deploy.aws.ship import host_for, remember_secret, ship_host, ship_order
from vecten.deploy.aws.snapshots import final_snapshots
from vecten.deploy.aws.tunnel import free_port, get_tunnel
from vecten.deploy.bundle import build_host_bundle, head_commit, prepare_bundle_metadata
from vecten.deploy.control import (
    DESTROYING_VAR,
    CloudBootstrap,
    apply_environment,
    apply_update_only,
    destroy_environment,
    plan_environment,
    provision_environment,
)
from vecten.deploy.github_env import (
    canonical_repository,
    oidc_subject_prefixes,
    repository_facts,
    repository_ids,
)
from vecten.deploy.guards import require_bootstrapped
from vecten.deploy.names import (
    deploy_role_name,
    plan_role_name,
    rds_identifier,
    state_bucket_name,
)
from vecten.deploy.outputs import (
    outputs_key,
    outputs_text,
    parse_stored_outputs,
    save_outputs,
    write_outputs,
)
from vecten.deploy.snowflake import snowflake_root
from vecten.deploy.terraform import (
    PlannedRoot,
    TerraformRoot,
    TerraformRunner,
    get_terraform,
    require_terraform,
    s3_backend,
    state_key,
)
from vecten.deploy.tfvars import env_tfvars_path, read_tfvars, root_var_file
from vecten.github import gh as gh_mod
from vecten_shared.config import VectenConfig, load_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import provider_factory, register_provider, secret_id
from vecten_shared.provider.aws import AwsRuntimeProvider
from vecten_shared.provider.environment import (
    EnvironmentOutputs,
    PostgresOutput,
    environment_file,
)

CLOUD_ROOT = "aws"
# The GitHub environment variables the rendered workflows read.
ROLE_VARIABLE = "VECTEN_AWS_ROLE_ARN"
REGION_VARIABLE = "VECTEN_AWS_REGION"
STATE_BUCKET_VARIABLE = "VECTEN_STATE_BUCKET"
PLAN_ROLE_VARIABLE_PREFIX = "VECTEN_AWS_PLAN_ROLE_ARN_"
# What `destroying` changes in the cloud root: deletion protection on the instances, and
# force_destroy on the artifacts bucket, which deleting a non-empty bucket needs in state.
POSTGRES_MODULE = "module.environment.module.postgres"
ARTIFACTS_BUCKET = "module.environment.aws_s3_bucket.artifacts"
# RDS's predefined role holding every privilege its master user can pass on.
ADMIN_GRANTS = ("rds_superuser",)
_POSTGRES_KEY = re.compile(r'module\.environment\.module\.postgres\["([^"]+)"\]')
_INSTANCE_GONE = "DBInstanceNotFound"
_MISSING_KEY = frozenset({"NoSuchKey", "404"})


def plan_role_variable(env: str) -> str:
    """The repository-level variable holding `env`'s plan role ARN.

    The plan job declares no environment, so it reads repository-level variables only, and
    one name per environment keeps a second bootstrap from overwriting the first's. GitHub
    variable names hold letters, digits and underscores; environment names add hyphens.
    """
    return f"{PLAN_ROLE_VARIABLE_PREFIX}{env.upper().replace('-', '_')}"


def postgres_keys(state: list[str]) -> set[str]:
    """The instance keys (`state`, `warehouse`) of the Postgres module's resources in `state`."""
    return {m.group(1) for address in state if (m := _POSTGRES_KEY.match(address))}


def lift_targets(state: list[str], gone: Iterable[str] = ()) -> list[str]:
    """The addresses in `state` that `destroying` changes: every resource of the Postgres
    instances and the artifacts bucket. Empty when a partial destroy removed them all.

    State still lists an instance deleted outside Terraform, and a refreshed plan would
    create it again, so every resource of a key in `gone` is left out.
    """
    dropped = tuple(f'{POSTGRES_MODULE}["{key}"]' for key in gone)
    return [
        address
        for address in state
        if (
            address == ARTIFACTS_BUCKET
            or address.startswith((f"{POSTGRES_MODULE}[", f"{POSTGRES_MODULE}."))
        )
        and not (dropped and address.startswith(dropped))
    ]


class AwsProvider(AwsRuntimeProvider):
    """The AWS control plane (`vecten.deploy.control.ControlPlane`) and the CLI's provider."""

    cloud = CLOUD_ROOT
    cloud_only_plans_refresh = True
    admin_grants = ADMIN_GRANTS
    # RDS deletion protection, which the module sets in prod, refuses a database deletion.
    guarded_resources: tuple[str, ...] = ()

    def _client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = clients.client(service, self.region())
        return self._clients[service]

    def client(self, service: str) -> Any:
        return self._client(service)

    def region(self) -> str:
        # Secrets are set before the first deploy writes the outputs, so the committed tfvars
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

    def get_secret(self, name: str) -> str:
        value = super().get_secret(name)
        remember_secret(value)
        return value

    def account_id(self) -> str:
        """The account the human's login acts in; refuses without a usable login."""
        try:
            sts = self.client("sts")
        except aws_errors() as exc:
            raise (login_failure(exc) or aws_failure("STS client", exc)) from None
        return caller_account(sts)

    def check_login(self) -> str:
        return self.account_id()

    def prepare(
        self,
        env: str,
        repository: str,
        account: str,
        branch: str,
        *,
        subject_prefixes: Sequence[str],
    ) -> dict:
        """Create or update the state bucket, the OIDC provider, the host roles' boundary, the
        deploy role and the plan role. `repository` is GitHub's spelling, `branch` its default
        branch and `subject_prefixes` the forms of the repository the OIDC subjects name.

        Returns `state_bucket`, `role_arn`, `plan_role_arn` and `created`, the kinds of
        resource this run created, in creation order.
        """
        self._check_env(env)
        project = self.config().project.name
        bucket = state_bucket_name(project, env, account)
        created: list[str] = []
        if ensure_state_bucket(self.client("s3"), bucket, self.region()):
            created.append("state_bucket")
        iam = self.client("iam")
        oidc = ensure_oidc_provider(iam, created=created)
        ensure_boundary_policy(iam, account, project, env, created=created)
        role = ensure_deploy_role(
            iam,
            deploy_role_name(project, env),
            oidc,
            repository,
            env,
            project,
            subject_prefixes=subject_prefixes,
            created=created,
        )
        plan_role = ensure_plan_role(
            iam,
            plan_role_name(project, env),
            oidc,
            repository,
            branch,
            bucket,
            env,
            project,
            subject_prefixes=subject_prefixes,
            created=created,
        )
        return {
            "state_bucket": bucket,
            "role_arn": role,
            "plan_role_arn": plan_role,
            "created": created,
        }

    def bootstrap_env(
        self,
        env: str,
        repository: str,
        branch: str,
        account: str,
        *,
        subject_prefixes: Sequence[str],
    ) -> CloudBootstrap:
        """`prepare`, publishing the deploy role, region and state bucket to the GitHub
        environment and the plan role to the repository."""
        prepared = self.prepare(env, repository, account, branch, subject_prefixes=subject_prefixes)
        return CloudBootstrap(
            state_bucket=prepared["state_bucket"],
            environment_variables={
                ROLE_VARIABLE: prepared["role_arn"],
                REGION_VARIABLE: self.region(),
                STATE_BUCKET_VARIABLE: prepared["state_bucket"],
            },
            repository_variables={plan_role_variable(env): prepared["plan_role_arn"]},
            report={
                "role_arn": prepared["role_arn"],
                "plan_role_arn": prepared["plan_role_arn"],
                "created": prepared["created"],
            },
        )

    def bootstrap(self, env: str, repository: str) -> None:
        gh = gh_mod.get_gh()
        name, branch = repository_facts(gh, self.project_root, repository)
        canonical = canonical_repository(repository, name)
        prefixes = oidc_subject_prefixes(
            canonical, *repository_ids(gh, self.project_root, canonical)
        )
        self.prepare(env, canonical, self.account_id(), branch, subject_prefixes=prefixes)

    def terraform_roots(
        self, env: str, *, cloud_only: bool = False, read_only: bool = False
    ) -> list[TerraformRoot]:
        """The roots `deploy` plans, in apply order: the cloud root, then any Snowflake root.

        `cloud_only` leaves the Snowflake root out, reading no Snowflake secret: the
        pull-request plan's role cannot read deploy-scope secrets. `read_only` writes no
        secret while resolving the roots, for a destroy. Everything that can refuse runs
        here, before the first Terraform call.
        """
        self._check_env(env)
        config = self.config()
        root_dir = self.project_root / "deploy" / "terraform" / CLOUD_ROOT
        if not (root_dir / "main.tf").is_file():
            raise Failure(
                ExitCode.CONFIG,
                f"deploy/terraform/{CLOUD_ROOT}/main.tf is missing; run `vecten init --deploy`",
            )
        roots = [
            TerraformRoot(CLOUD_ROOT, root_dir, state_key(env, CLOUD_ROOT), root_var_file(env))
        ]
        if cloud_only:
            return roots
        snowflake = snowflake_root(self.project_root, config, env, self, read_only=read_only)
        if snowflake is not None:
            roots.append(snowflake)
        return roots

    def backend_config(self, env: str, root: TerraformRoot) -> dict[str, str]:
        return s3_backend(require_bootstrapped(self.config(), env), root.key, self.region())

    def terraform_env(self) -> dict[str, str]:
        # Terraform inherits the AWS login: the job's OIDC role in Actions, the human's
        # profile on the laptop.
        return {}

    def plan(
        self, env: str, terraform: TerraformRunner, out_dir: Path, *, cloud_only: bool = False
    ) -> list[PlannedRoot]:
        """Init every root against the environment's state bucket and save its plan."""
        return plan_environment(self, env, terraform, out_dir, cloud_only=cloud_only)

    def apply(
        self, env: str, terraform: TerraformRunner, plans: list[PlannedRoot]
    ) -> EnvironmentOutputs:
        """Apply the saved plans in order and publish the cloud root's outputs."""
        return apply_environment(self, env, terraform, plans)

    def outputs_location(self) -> str:
        return f"s3://{require_bootstrapped(self.config(), self.env)}/{outputs_key(self.env)}"

    def store_outputs(self, raw: dict[str, Any]) -> EnvironmentOutputs:
        bucket = require_bootstrapped(self.config(), self.env)
        outputs = write_outputs(self.project_root, self.env, raw)
        # `ship` reads the hosts through `outputs()`, which must not answer from an earlier
        # read of the file this apply replaced.
        self._outputs = outputs
        try:
            self.client("s3").put_object(
                Bucket=bucket,
                Key=outputs_key(self.env),
                Body=outputs_text(outputs).encode("utf-8"),
                ContentType="application/json",
            )
        except aws_errors() as exc:
            raise aws_failure("S3 PutObject", exc) from None
        return outputs

    def stored_outputs(self) -> EnvironmentOutputs | None:
        bucket = require_bootstrapped(self.config(), self.env)
        key = outputs_key(self.env)
        try:
            body = self.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        except aws_errors() as exc:
            if error_code(exc) in _MISSING_KEY:
                return None
            raise aws_failure("S3 GetObject", exc) from None
        outputs = parse_stored_outputs(body, f"s3://{bucket}/{key}", self.env)
        save_outputs(self.project_root, self.env, outputs)
        self._outputs = outputs
        return outputs

    def provision(self, env: str, terraform: TerraformRunner | None = None) -> dict:
        """Plan and apply every root in two steps, then publish the outputs.

        Returns `roots`, the names applied in order, and `outputs`, the environment's
        outputs as written to the project and the state bucket.
        """
        runner = terraform or get_terraform()
        require_terraform(runner)
        return provision_environment(self, env, runner)

    def destroy(
        self,
        env: str,
        *,
        delete_data: bool = False,
        terraform: TerraformRunner | None = None,
        outputs: EnvironmentOutputs | None = None,
        notify: Callable[[str], None] | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Snapshot, lift deletion protection and destroy every root, as
        `vecten.deploy.control.destroy_environment` describes."""
        self._check_env(env)
        runner = terraform or get_terraform()
        require_terraform(runner)
        return destroy_environment(
            self, env, runner, delete_data=delete_data, outputs=outputs, notify=notify, now=now
        )

    def final_snapshots(
        self,
        outputs: EnvironmentOutputs,
        now: datetime,
        *,
        on_created: Callable[[str], None],
        absent: list[str],
        expires: dict[str, str],
    ) -> list[str]:
        return final_snapshots(
            self.client("rds"),
            self.config().project.name,
            self.env,
            outputs,
            now,
            on_created=on_created,
            absent=absent,
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
        """Add `destroying=true` to `root` and apply it to the instances and the bucket that
        still exist, turning deletion protection off and letting the bucket be emptied."""
        root = replace(root, extra_vars=(*root.extra_vars, (DESTROYING_VAR, "true")))
        gone = self._gone_instances(postgres_keys(state) - checked, absent)
        gone |= {k for k in checked if self._rds_identifier(k) in absent}
        targets = lift_targets(state, gone)
        if targets:
            apply_update_only(terraform, root, targets)
        return root

    def _rds_identifier(self, key: str) -> str:
        return rds_identifier(self.config().project.name, self.env, key)

    def _gone_instances(self, keys: set[str], absent: list[str]) -> set[str]:
        """The keys among `keys` whose RDS instance no longer exists; their identifiers are
        appended to `absent`."""
        gone: set[str] = set()
        for key in sorted(keys):
            identifier = self._rds_identifier(key)
            try:
                self.client("rds").describe_db_instances(DBInstanceIdentifier=identifier)
            except aws_errors() as exc:
                if error_code(exc) != _INSTANCE_GONE:
                    raise aws_failure(f"RDS DescribeDBInstances of {identifier}", exc) from None
                gone.add(key)
                if identifier not in absent:
                    absent.append(identifier)
        return gone

    def forget_outputs(self) -> list[str]:
        """Remove the environment's outputs from the state bucket and the project, so `ship`
        finds no hosts to reach; returns what could not be removed and never raises."""
        left: list[str] = []
        key = outputs_key(self.env)
        try:
            state_bucket = require_bootstrapped(self.config(), self.env)
            self.client("s3").delete_object(Bucket=state_bucket, Key=key)
        except Failure as failure:
            left.append(f"the stored {key} ({failure.message})")
        except aws_errors() as exc:
            left.append(f"s3://{state_bucket}/{key} (S3 DeleteObject failed: {aws_reason(exc)})")
        path = environment_file(self.project_root, self.env)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            left.append(f"{path} ({type(exc).__name__})")
        self._outputs = None
        return left

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
        shipped: list[str] = []
        metadata = prepare_bundle_metadata(
            project_root,
            config,
            commit,
            with_mcp=any("mcp" in host.components for host in outputs.hosts.values()),
        )
        with tempfile.TemporaryDirectory(prefix="vecten-bundles-") as tmp:
            for host in ship_order(outputs):
                bundle = build_host_bundle(
                    project_root, config, env, outputs, host, commit, Path(tmp), metadata=metadata
                )
                self.ship_host(host, bundle, commit)
                shipped.append(host)
        return shipped

    def master_credential(self, instance: PostgresOutput) -> tuple[str, str]:
        """The user name and password in `instance`'s RDS-managed master secret."""
        try:
            secrets = self.client("secretsmanager")
            raw = secrets.get_secret_value(SecretId=instance.master_secret)["SecretString"]
        except aws_errors() as exc:
            raise aws_failure("Secrets Manager GetSecretValue of the master secret", exc) from None
        try:
            credential = json.loads(raw)
            return credential["username"], credential["password"]
        except ValueError, KeyError, TypeError:
            raise Failure(
                ExitCode.TOOL_FAILED,
                "the warehouse instance's master secret is not username/password",
            ) from None

    def check_refresh_safe(self) -> None:
        # No AWS resource holds a value Terraform must not read.
        return None

    def before_cloud_apply(self) -> None:
        # No AWS resource holds a value Terraform must not read.
        return None

    def after_cloud_apply(self, outputs: EnvironmentOutputs) -> None:
        # Instance roles give the hosts their identity, and the module removes what it drops.
        return None

    def require_admin_access(self) -> None:
        # The SSM tunnel through the setup host needs nothing a project file opts into.
        return None

    def secret_id(self, name: str) -> str:
        return secret_id(self.config().project.name, self.env, name)

    def read_object(self, key: str) -> bytes | None:
        bucket = self.outputs().bucket
        try:
            return self.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        except aws_errors() as exc:
            if error_code(exc) in _MISSING_KEY:
                return None
            raise aws_failure(f"S3 GetObject of {key}", exc) from None

    def read_state_object(self, key: str) -> bytes | None:
        bucket = require_bootstrapped(self.config(), self.env)
        try:
            return self.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        except aws_errors() as exc:
            if error_code(exc) in _MISSING_KEY:
                return None
            raise aws_failure(f"S3 GetObject of {key}", exc) from None

    @contextmanager
    def admin_connection(
        self, outputs: EnvironmentOutputs, instance: PostgresOutput
    ) -> Iterator[dict[str, Any]]:
        """An SSM tunnel from a free local port through the setup host to `instance`."""
        host = outputs.hosts.get(outputs.setup_host)
        if host is None:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"environment {self.env!r} has no setup host in its outputs; "
                f"run `vecten deploy --env {self.env}`",
            )
        local_port = free_port()
        with get_tunnel().open(
            host.instance_id, instance.host, instance.port, local_port, self.region()
        ):
            # At 127.0.0.1 no RDS certificate name can match; SSM picks the far end instead.
            yield {"host": "127.0.0.1", "port": local_port, "sslmode": "require"}

    def ship(self, env: str, component: str, project_dir: Path) -> None:
        """Ship the content of `project_dir` at its HEAD to the host carrying `component`."""
        self._check_env(env)
        outputs = self.outputs()
        host = host_for(outputs, component)
        if host is None:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"environment {env!r} has no host carrying {component}; "
                f"run `vecten deploy --env {env}`",
            )
        commit = head_commit(gh_mod.get_git(), project_dir)
        config = load_config(project_dir)
        with tempfile.TemporaryDirectory(prefix="vecten-bundle-") as tmp:
            bundle = build_host_bundle(project_dir, config, env, outputs, host, commit, Path(tmp))
            self.ship_host(host, bundle, commit)

    def ship_host(self, host: str, bundle: Path, commit: str) -> None:
        ship_host(self, host, bundle, commit)


def register() -> None:
    """Hold the `aws` slot unless something other than the runtime class already holds it."""
    current = provider_factory("aws")
    if current is None or current is AwsRuntimeProvider:
        register_provider("aws", AwsProvider)


register()
