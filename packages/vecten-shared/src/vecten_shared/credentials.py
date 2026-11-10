"""A rotation envelope keeps both login candidates durable across a process interruption."""

import json


def password_candidates(value):
    if value.startswith('{"current":'):
        data = json.loads(value)
        values = [data["current"], data["pending"]]
        if not all(isinstance(item, str) and item for item in values):
            raise ValueError("invalid credential rotation envelope")
        return list(dict.fromkeys(values))
    return [value]
