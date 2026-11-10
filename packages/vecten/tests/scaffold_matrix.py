from itertools import product

from vecten.scaffold.params import ScaffoldParams

LOCAL = {"name": "acme", "users": 8}
DEPLOY = {"domain": "data.acme.com", "dns_zone": "acme.com", "github_repository": "acme/data"}


def build_matrix() -> list[ScaffoldParams]:
    out = []
    for cloud, warehouse, components, profile in product(
        ("aws", "scaleway", None),
        ("postgres", "snowflake"),
        (["platform", "mcp"], ["mcp"], ["platform"]),
        ("standard", "minimal"),
    ):
        answers = {**LOCAL, "sources": ["hubspot"], "warehouse": warehouse}
        if cloud:
            answers.update(DEPLOY, cloud=cloud)
        overrides = {"components": components, "profile": profile}
        out.append(ScaffoldParams.from_answers(answers, overrides))
    for url in (
        "postgres://dw.internal:5432/analytics",
        "snowflake://ab12345.eu-central-1/analytics",
    ):
        for cloud, profile in product(("aws", "scaleway", None), ("standard", "minimal")):
            answers = {**LOCAL, "has_warehouse": "yes", "warehouse_url": url}
            if cloud:
                answers.update(DEPLOY, cloud=cloud)
            out.append(ScaffoldParams.from_answers(answers, {"profile": profile}))
    return out


MATRIX = build_matrix()


def label(p: ScaffoldParams) -> str:
    existing = "-existing" if p.has_warehouse else ""
    return f"{p.cloud or 'local'}-{p.warehouse}-{'+'.join(p.components)}-{p.profile}{existing}"
