"""When the cloud keys `bootstrap` made expire, read from the facts it recorded.

Bootstrap records each key's expiry as `cloud_ids["expires_<owner>"]` (`deploy`, `plan` and
one per host) and rotates every key that expires within `RENEW_WITHIN`, so `next` and
`deploy` warn within that same window: one bootstrap run then clears the warning.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from vecten_shared.config import CLOUD_ID_EXPIRY_PREFIX, EnvironmentFacts

RENEW_WITHIN = timedelta(days=30)


def expiring_keys(facts: EnvironmentFacts | None, moment: datetime) -> dict[str, datetime]:
    """The keys among the recorded ones that expire within `RENEW_WITHIN` of `moment`, or
    already have, by owner."""
    if facts is None:
        return {}
    found: dict[str, datetime] = {}
    for key, value in sorted(facts.cloud_ids.items()):
        if not key.startswith(CLOUD_ID_EXPIRY_PREFIX):
            continue
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expires - moment <= RENEW_WITHIN:
            found[key.removeprefix(CLOUD_ID_EXPIRY_PREFIX)] = expires
    return found


def describe(expiring: dict[str, datetime]) -> str:
    return ", ".join(f"{owner} ({when:%Y-%m-%d})" for owner, when in expiring.items())


def renewal_warning(env: str, expiring: dict[str, datetime]) -> str:
    return (
        f"the {env} keys of {describe(expiring)} expire within {RENEW_WITHIN.days} days, "
        f"after which CI or the VMs stop reaching the cloud; run `vecten bootstrap --env {env}` "
        f"to rotate them, then `vecten deploy --env {env}` to hand each VM its new key"
    )


RETIRING_PREFIX = "retiring_"
# CI keys retire on their own; a VM keeps signing with its old key until a deploy hands it the
# new one, and the old key expires.
VM_KEY_OWNERS = ("platform", "mcp", "main")


def waiting_vm_keys(facts: EnvironmentFacts | None) -> list[str]:
    ids = facts.cloud_ids if facts is not None else {}
    return [host for host in VM_KEY_OWNERS if f"{RETIRING_PREFIX}{host}" in ids]
