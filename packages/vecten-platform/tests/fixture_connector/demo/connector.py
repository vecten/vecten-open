"""Test-only connector: two healthy objects and one that always fails."""

import dlt

from vecten_platform.sync import secret


@dlt.resource(name="good_a", write_disposition="append")
def good_a(limit=None, cursor=dlt.sources.incremental("id", initial_value=0)):  # noqa: B008
    yield from [{"id": 1, "name": "a1"}, {"id": 2, "name": "a2"}][:limit]


@dlt.resource(name="bad")
def bad():
    raise RuntimeError("upstream returned 500")
    yield


@dlt.resource(name="good_b", write_disposition="append")
def good_b():
    yield from [{"id": 10, "amount": 1.5}]


@dlt.source(name="demo")
def demo_source(token: str, limit: int | None = None):
    return [good_a(limit=limit), bad, good_b]


def source(limit: int | None = None):
    return demo_source(token=secret("DEMO_TOKEN"), limit=limit)
