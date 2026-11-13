"""Every item `next` reports, in priority order, from local files, run records and git."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from vecten.cli.context import CliContext
from vecten.intake.answers import read_answers
from vecten.next.areas import area_access_ask_items
from vecten.next.declared import declared_secret_items
from vecten.next.deploy import (
    deploy_gate_items,
    deploy_prerequisite_items,
    deploy_question_items,
    key_handover_items,
    key_renewal_items,
)
from vecten.next.existing_warehouse import existing_warehouse_items
from vecten.next.gitstate import read_git_state
from vecten.next.ingestion import ingestion_items
from vecten.next.intake import intake_items
from vecten.next.items import Item
from vecten.next.modelling import modelling_items
from vecten.next.post_deploy import post_deploy_items
from vecten.next.semantic import semantic_items
from vecten.next.stack import lock_items, stack_items
from vecten_shared.config import VectenConfig, load_config


def next_items(cwd: Path) -> list[Item]:
    root = CliContext(cwd, json=False).project_root()
    if root is None:
        return intake_items(cwd)
    config = load_config(root)
    # A key that expires stops CI or a VM whatever else is outstanding, so its rotation and
    # the deploy that hands a rotated key to its VM lead, for every environment that has one.
    now = datetime.now(UTC)
    keys = [
        item
        for env in _environments_with_facts(config)
        for item in key_renewal_items(config, env, now) + key_handover_items(config, env)
    ]
    return keys + _project_items(root, config)


def _environments_with_facts(config: VectenConfig) -> list[str]:
    """The order's environments first, in order, then the others by name."""
    order = config.promotion_order()
    recorded = set(config.environments)
    return [env for env in order if env in recorded] + sorted(recorded - set(order))


def _project_items(root: Path, config: VectenConfig) -> list[Item]:
    if config.has("platform"):
        local_loop = [
            *lock_items(root, config),
            *stack_items(root, config),
            *declared_secret_items(root, config),
            *ingestion_items(root, config),
            *area_access_ask_items(config),
            *modelling_items(root, config),
            *semantic_items(root, config),
        ]
    else:
        # The local MCP signs in as the provisioner, which only the administrator's SQL creates.
        local_loop = [
            *lock_items(root, config),
            *declared_secret_items(root, config),
            *ingestion_items(root, config),
            *area_access_ask_items(config),
            *modelling_items(root, config),
            *semantic_items(root, config),
            # The setup SQL is asked for the first environment of the order; which earlier run
            # counts for it depends on the warehouse (`_script_confirmed`).
            *existing_warehouse_items(root, config, config.promotion_order()[0]),
            *stack_items(root, config),
        ]
    # No deploy item is reported before the local loop is clear, and each later stage waits
    # for the one before it, so the gate is only offered once nothing else is outstanding.
    if local_loop:
        return local_loop
    questions = deploy_question_items(root, config, read_answers(root))
    if questions:
        return questions
    return _walk_order(root, config) if config.deploy is not None else []


def _walk_order(root: Path, config: VectenConfig) -> list[Item]:
    """The first environment of the order whose items are not all clear, with its items.

    A later environment ships what the one before it shipped, so it waits until the one
    before it is deployed and its first user, access, connection and webhook are settled.
    """
    git = read_git_state(root)
    for env in config.promotion_order():
        facts = config.environments.get(env)
        if facts is None or not facts.deployed_commit:
            prerequisites = deploy_prerequisite_items(root, config, env, git)
            return prerequisites or deploy_gate_items(config, env)
        items = post_deploy_items(root, config, env, git)
        if items:
            return items
    return []


def next_notes(cwd: Path) -> list[str]:
    """Facts worth knowing that ask for no action, so they never hold back a later stage.

    A declared service or route stays unbound when the project chooses not to run it.
    """
    from vecten_shared.extension_record import read_record
    from vecten_shared.services import unbound_parts

    root = CliContext(cwd, json=False).project_root()
    if root is None:
        return []
    config = load_config(root)
    return [
        f"{part} is declared but not bound in vecten.toml"
        for part in unbound_parts(config, read_record(root) or {})
    ]
