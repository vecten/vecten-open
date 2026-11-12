"""Every item `next` reports, in priority order, from local files, run records and git."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from vecten.cli.context import CliContext
from vecten.intake.answers import read_answers
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
from vecten_shared.config import EnvironmentFacts, VectenConfig, load_config

ENV = "prod"


def next_items(cwd: Path) -> list[Item]:
    root = CliContext(cwd, json=False).project_root()
    if root is None:
        return intake_items(cwd)
    config = load_config(root)
    # A key that expires stops CI or a VM whatever else is outstanding, so its rotation and
    # the deploy that hands a rotated key to its VM lead.
    keys = key_renewal_items(config, ENV, datetime.now(UTC)) + key_handover_items(config, ENV)
    return keys + _project_items(root, config)


def _project_items(root: Path, config: VectenConfig) -> list[Item]:
    if config.has("platform"):
        local_loop = [
            *lock_items(root, config),
            *stack_items(root, config),
            *declared_secret_items(root, config),
            *ingestion_items(root, config),
            *modelling_items(root, config),
            *semantic_items(root, config),
        ]
    else:
        # The local MCP signs in as the provisioner, which only the administrator's SQL creates.
        local_loop = [
            *lock_items(root, config),
            *declared_secret_items(root, config),
            *ingestion_items(root, config),
            *modelling_items(root, config),
            *semantic_items(root, config),
            *existing_warehouse_items(root, config, ENV),
            *stack_items(root, config),
        ]
    # No deploy item is reported before the local loop is clear, and each later stage waits
    # for the one before it, so the gate is only offered once nothing else is outstanding.
    if local_loop:
        return local_loop
    questions = deploy_question_items(root, config, read_answers(root))
    if questions:
        return questions
    deployed = bool((config.environments.get(ENV) or EnvironmentFacts()).deployed_commit)
    if config.deploy is not None and not deployed:
        prerequisites = deploy_prerequisite_items(root, config, ENV, read_git_state(root))
        if prerequisites:
            return prerequisites
        gate = deploy_gate_items(config, ENV)
        if gate:
            return gate
    # Only a deployed project has changes to push, so git is read for it alone.
    return post_deploy_items(root, config, ENV, read_git_state(root)) if deployed else []
