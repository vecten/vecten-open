"""The Snowflake root of a deploy: which projects have one, the deploy user's credentials it
authenticates with, and the loader's key pair whose public half it registers."""

from __future__ import annotations

from pathlib import Path

from vecten.deploy.sizing import snowflake_warehouse_size
from vecten.deploy.terraform import TerraformRoot, state_key
from vecten.deploy.tfvars import check_tfvars, root_var_file
from vecten_shared.config import SNOWFLAKE_ACCOUNT, EnvironmentFacts, VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import Provider, snowflake_names

SNOWFLAKE_ROOT = "snowflake"
DEPLOY_USER = "DEPLOY_SNOWFLAKE_USER"
DEPLOY_PRIVATE_KEY = "DEPLOY_SNOWFLAKE_PRIVATE_KEY"
DEPLOY_SECRETS = (DEPLOY_USER, DEPLOY_PRIVATE_KEY)
# The root sets future grants in CORE and MARTS, which a regular schema allows only to a role
# holding MANAGE GRANTS; the runbook names the same privileges.
DEPLOY_ROLE_PRIVILEGES = (
    "CREATE DATABASE, CREATE WAREHOUSE, CREATE ROLE, CREATE USER and MANAGE GRANTS"
)
LOADER_PRIVATE_KEY = "SNOWFLAKE_LOADER_PRIVATE_KEY"
PROVISIONER_PRIVATE_KEY = "MCP_PROVISIONER_PRIVATE_KEY"
JWT_AUTHENTICATOR = "SNOWFLAKE_JWT"
KEY_BITS = 2048
# The provider skips a missing config file, so no ~/.snowflake/config profile applies.
NO_CONFIG_FILE = "/nonexistent/vecten/snowflake-config"


def has_snowflake_root(config: VectenConfig) -> bool:
    # `init` renders deploy/terraform/snowflake under exactly this condition: the platform
    # builds the warehouse, so an MCP over an existing Snowflake account has no root.
    return config.warehouse.kind == "snowflake" and "platform" in config.components


def identity_pending(config: VectenConfig, env: str) -> bool:
    """Whether the MCP of a platform-built Snowflake still lacks its Snowflake identity.

    The MCP checks its scope as the provisioner user when it starts and refuses to serve
    without it, and only an ACCOUNTADMIN running `vecten mcp provisioner-script` creates it,
    after the first deploy has created the database the script grants on.
    """
    facts = config.environments.get(env) or EnvironmentFacts()
    return has_snowflake_root(config) and config.has("mcp") and not facts.provisioner_script_run


def identity_pending_note(config: VectenConfig, env: str) -> str:
    user = snowflake_names(config, env).provisioner_user
    return (
        f"the MCP signs in to Snowflake as {user}, which "
        f"`vecten mcp provisioner-script --env {env}` sets up, so no host was shipped: run it, "
        "have an ACCOUNTADMIN run its SQL, confirm with `--confirm-run`, then "
        "`vecten github push`, which ships"
    )


def split_account(account: str) -> tuple[str, str]:
    """The organisation and account names of an `ORGANIZATION-ACCOUNT` identifier."""
    match = SNOWFLAKE_ACCOUNT.fullmatch(account or "")
    if match is None:
        raise Failure(
            ExitCode.CONFIG,
            f"the Snowflake account {account!r} is not an ORGANIZATION-ACCOUNT identifier, "
            "for example ACME-PROD",
        )
    return match.group(1), match.group(2)


def secret_set_command(name: str, env: str) -> str:
    return f"vecten secret set {name} --env {env}"


def deploy_credentials(provider: Provider, env: str, account: str) -> dict[str, str]:
    """The Snowflake provider's environment: the deploy user signs in by key pair.

    Refuses naming the `secret set` command of every deploy secret that is missing.
    """
    organisation, name = split_account(account)
    found: dict[str, str] = {}
    missing: list[str] = []
    for secret in DEPLOY_SECRETS:
        try:
            found[secret] = provider.get_secret(secret)
        except Failure as failure:
            if failure.code != ExitCode.PREREQUISITE:
                raise
            missing.append(secret)
    if missing:
        commands = " and ".join(f"`{secret_set_command(s, env)}`" for s in missing)
        raise Failure(
            ExitCode.PREREQUISITE,
            f"the Snowflake root signs in as the deploy user, whose credentials are not set in "
            f"{env}; run {commands} (the private key as a PEM file on standard input). The "
            f"user's default role holds {DEPLOY_ROLE_PRIVILEGES} on the account",
            details={"missing": missing},
        )
    return {
        "SNOWFLAKE_ORGANIZATION_NAME": organisation,
        "SNOWFLAKE_ACCOUNT_NAME": name,
        "SNOWFLAKE_USER": found[DEPLOY_USER],
        "SNOWFLAKE_AUTHENTICATOR": JWT_AUTHENTICATOR,
        "SNOWFLAKE_PRIVATE_KEY": found[DEPLOY_PRIVATE_KEY],
        "SNOWFLAKE_CONFIG_PATH": NO_CONFIG_FILE,
    }


def ensure_loader_key(provider: Provider, *, store: bool = True) -> str:
    """The PEM public key of the loader user.

    With `store` false nothing is written: an absent key is replaced by a throwaway one whose
    public half satisfies the root's variable, which a destroy reads but never applies.
    """
    return ensure_key_pair(provider, LOADER_PRIVATE_KEY, store=store)


def ensure_key_pair(provider: Provider, name: str, *, store: bool = True) -> str:
    """The PEM public key of the key pair whose private half the secrets store keeps as
    `name`, generating and storing the pair when it holds none. Only the public half ever
    leaves this function."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    present = name in provider.list_secrets()
    if present:
        try:
            key = serialization.load_pem_private_key(
                provider.get_secret(name).encode("utf-8"), password=None
            )
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            # The exception text can quote the key material, so only its type is named.
            raise Failure(
                ExitCode.CONFIG,
                f"{name} is not an unencrypted PEM private key "
                f"({type(exc).__name__}); delete it from the secrets store so the next "
                "run generates a new key pair",
            ) from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise Failure(
                ExitCode.CONFIG,
                f"{name} is not an RSA key, which Snowflake key-pair sign-in needs; "
                "delete it from the secrets store so the next run generates a new key pair",
            )
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    if store and not present:
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        provider.put_secret(name, pem.decode("ascii"))
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return public.decode("ascii")


def snowflake_root(
    project_root: Path,
    config: VectenConfig,
    env: str,
    secrets: Provider,
    *,
    read_only: bool = False,
) -> TerraformRoot | None:
    """The Snowflake root to plan after the cloud root, or None for a project without one.

    `secrets` is the environment's control-plane provider, which holds the deploy-scope
    Snowflake credentials the root's provider authenticates with and the loader's key.
    `read_only` writes no loader key, for a destroy.
    """
    if not has_snowflake_root(config):
        return None
    values = check_tfvars(project_root, config, env)
    account = str(values["snowflake_account"])
    try:
        split_account(account)
    except Failure as failure:
        raise Failure(failure.code, f"deploy/envs/{env}.tfvars: {failure.message}") from None
    credentials = deploy_credentials(secrets, env, account)
    names = snowflake_names(config, env)
    # The loader key is written only once every refusal has passed.
    public_key = ensure_loader_key(secrets, store=not read_only)
    return TerraformRoot(
        SNOWFLAKE_ROOT,
        project_root / "deploy" / "terraform" / SNOWFLAKE_ROOT,
        state_key(env, SNOWFLAKE_ROOT),
        root_var_file(env),
        extra_vars=(
            ("database_name", names.database),
            ("warehouse_name", names.warehouse),
            ("warehouse_size", snowflake_warehouse_size(str(values["size"]))),
            ("loader_public_key", public_key),
        ),
        env=credentials,
        secrets=(credentials["SNOWFLAKE_USER"], credentials["SNOWFLAKE_PRIVATE_KEY"]),
    )
