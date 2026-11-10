"""The deploy questions, prerequisites and gate `next` reports, from files, records and git."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from vecten.deploy.expiry import describe, expiring_keys, waiting_vm_keys
from vecten.deploy.snowflake import (
    DEPLOY_PRIVATE_KEY,
    DEPLOY_USER,
    has_snowflake_root,
    secret_set_command,
)
from vecten.intake.answers import ANSWER_KEYS, DECLINED, read_answers
from vecten.next.existing_warehouse import provisioner_secrets_item
from vecten.next.gitstate import GitState
from vecten.next.items import Item
from vecten.secrets import LAPTOP_ONLY, provisioner_secret_names, recorded_secret_names
from vecten.stack.env import STACK_MANAGED
from vecten_shared.config import EnvironmentFacts, VectenConfig
from vecten_shared.envfile import read_env_values
from vecten_shared.provider.local import LocalProvider

# `none` records that the human has no account yet; the question stays open and, asked again,
# carries the guides to show first. An agent acting on the item records it like any answer.
CLOUDS = ("aws", "scaleway", DECLINED)
NO_ACCOUNT_HINT = f"Answer {DECLINED} if you do not have one yet."
PUSH = Item("command", "Push the project's changes to GitHub", "vecten github push")
RENDER_DEPLOY = Item("command", "Render the deploy layer", "vecten init --deploy")
ACCOUNT_KEY = "snowflake_account"


def _questions(
    config: VectenConfig,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str], ...]:
    # The estimate carries the project's own shape, so its figures are the ones the deploy bills.
    estimate = (
        f"vecten estimate --size {config.size} --components {','.join(config.components)} "
        f"--warehouse {config.warehouse.kind}"
    )
    questions = (
        (("cloud",), CLOUDS, (estimate, "vecten guide cloud"), ""),
        (("domain", "dns_zone"), (), ("vecten guide domain",), NO_ACCOUNT_HINT),
        (("github_repository",), (), ("vecten guide github",), NO_ACCOUNT_HINT),
    )
    if _needs_account(config):
        questions += (((ACCOUNT_KEY,), (), (), ""),)
    return questions


def _needs_account(config: VectenConfig) -> bool:
    """Whether the Snowflake account the platform builds in is still unknown; an existing
    warehouse's locator names its own."""
    return has_snowflake_root(config) and not config.warehouse.account


def _open(answers: dict[str, Any], keys: tuple[str, ...]) -> tuple[bool, bool]:
    """Whether the question is still open, and whether its first key was declined."""
    declined = answers.get(keys[0]) == DECLINED
    return declined or any(answers.get(k) is None for k in keys), declined


def deploy_question_items(root: Path, config: VectenConfig, answers: dict[str, Any]) -> list[Item]:
    if config.deploy is not None:
        return []
    for keys, allowed, guides, hint in _questions(config):
        still_open, declined = _open(answers, keys)
        if still_open:
            question = ANSWER_KEYS[keys[0]].question
            show_first = guides if declined else ()
            return [
                Item(
                    "ask",
                    question,
                    f"{question} {hint}" if hint else question,
                    answer_keys=keys,
                    allowed=allowed,
                    show_first=show_first,
                )
            ]
    return [RENDER_DEPLOY]


def _undeployed(config: VectenConfig, env: str) -> EnvironmentFacts | None:
    """The facts of an environment whose deploy layer exists but has not deployed yet."""
    facts = config.environments.get(env) or EnvironmentFacts()
    if config.deploy is None or facts.deployed_commit:
        return None
    return facts


def _uncopied_secrets(root: Path, config: VectenConfig, env: str) -> list[str]:
    env_file = LocalProvider(root, inside_compose=False).env_file
    values = read_env_values(env_file)
    # `--from-local` refuses a bare or empty entry, so only names with a value can be copied.
    local = {name for name, value in values.items() if value}
    # The local stack's own credentials stay local: the cloud environment generates its own.
    local -= STACK_MANAGED | LAPTOP_ONLY
    if not config.has("platform"):
        local -= set(provisioner_secret_names(config))
    return sorted(local - recorded_secret_names(root, env))


def snowflake_deploy_secret_items(root: Path, config: VectenConfig, env: str) -> list[Item]:
    """The deploy user's credentials the Snowflake root signs in with, until recorded."""
    if not has_snowflake_root(config):
        return []
    present = recorded_secret_names(root, env)
    return [
        Item("command", f"{title} in {env}{hint}", secret_set_command(name, env))
        for name, title, hint in (
            (DEPLOY_USER, "Store the Snowflake deploy user's name", ""),
            (
                DEPLOY_PRIVATE_KEY,
                "Store the Snowflake deploy user's private key",
                ", a PEM file on standard input",
            ),
        )
        if name not in present
    ]


def deploy_prerequisite_items(
    root: Path, config: VectenConfig, env: str, git: GitState
) -> list[Item]:
    facts = _undeployed(config, env)
    if facts is None:
        return []
    items: list[Item] = []
    # `init --deploy` writes the answered account to the tfvars and vecten.toml, which
    # deploy refuses to run without.
    if _needs_account(config):
        if read_answers(root).get(ACCOUNT_KEY):
            items.append(RENDER_DEPLOY)
        else:
            question = ANSWER_KEYS[ACCOUNT_KEY].question
            items.append(Item("ask", question, question, answer_keys=(ACCOUNT_KEY,)))
    if not git.has_origin:
        items.append(Item("command", "Put the project on GitHub", "vecten github init"))
    if not facts.bootstrapped:
        title = f"Bootstrap the {env} environment"
        items.append(Item("command", title, f"vecten bootstrap --env {env}"))
    # `secret set --env` writes to the cloud secrets store, which bootstrap creates.
    provisioner = provisioner_secrets_item(config, env)
    if provisioner is not None:
        items.append(provisioner)
    items += snowflake_deploy_secret_items(root, config, env)
    for name in _uncopied_secrets(root, config, env):
        command = f"vecten secret set {name} --env {env} --from-local"
        items.append(Item("command", f"Copy {name} to {env}", command))
    if git.has_origin and (git.dirty or git.ahead):
        items.append(PUSH)
    return items


def deploy_gate_items(config: VectenConfig, env: str) -> list[Item]:
    if _undeployed(config, env) is None:
        return []
    title = f"Deploy to {env}? This creates resources your cloud provider bills you for."
    return [Item("ask", title, f"vecten deploy --env {env}", allowed=("yes", "no"))]


def key_handover_items(config: VectenConfig, env: str) -> list[Item]:
    """Deploy again once bootstrap rotated a Scaleway VM's key; deploy places the new key."""
    facts = config.environments.get(env)
    if config.deploy is None or config.deploy.cloud != "scaleway":
        return []
    if facts is None or not facts.deployed_commit:
        return []
    hosts = waiting_vm_keys(facts)
    if not hosts:
        return []
    title = (
        f"Hand the new keys of {', '.join(hosts)} to their VMs in {env}; until a deploy places "
        "them, the VMs sign with the old keys, which stop working when they expire"
    )
    return [Item("command", title, f"vecten deploy --env {env}")]


def key_renewal_items(config: VectenConfig, env: str, moment: datetime) -> list[Item]:
    """Bootstrap again once a key it made expires within 30 days; it rotates each such key."""
    facts = config.environments.get(env)
    if facts is None or not facts.bootstrapped:
        return []
    expiring = expiring_keys(facts, moment)
    if not expiring:
        return []
    title = f"Rotate the {env} keys that expire soon: {describe(expiring)}"
    return [Item("command", title, f"vecten bootstrap --env {env}")]
