from pathlib import Path

root = Path(__file__).parent
dbt, dags, osi, skill = (root / name for name in ("dbt", "dags", "osi", "skill"))
rows = []


def write_connector(target):
    (target / "connector.py").write_text("def source():\n    return []\n")


connector = {
    "kind": "custom",
    "write": write_connector,
    "groups": {"daily": {"objects": [], "schedule": "0 6 * * *", "timezone": "Europe/Warsaw"}},
}


def identity(credentials):
    return {"subject": "fixture"}


def provision(name):
    return name


def audit(row):
    rows.append(row)


def answer():
    return "fixture"


tool = {"groups": ["analyst"], "handler": answer}
