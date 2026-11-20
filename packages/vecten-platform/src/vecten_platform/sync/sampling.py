"""Bound the source iterator before dlt's incremental and user transforms."""

from collections.abc import Iterator

from dlt.extract.items_transform import LimitItem


class RecordingLimit(LimitItem):
    # dlt inserts later maps by affinity. Trim the page before redaction and recording.
    placement_affinity = -1.0

    def __init__(self, limit: int):
        super().__init__(max_items=limit, max_time=None, count_rows=True)

    def __call__(self, item, meta=None):
        if isinstance(item, list):
            item = item[: max(0, self.max_items - self.count)]
        return super().__call__(item, meta)


def _bounded_transform(transform, budget):
    def run(item, meta=None):
        if budget.exhausted:
            return
        output = transform(item, meta=meta)
        if not isinstance(output, Iterator):
            yield output
            return
        try:
            while not budget.exhausted:
                try:
                    value = next(output)
                except StopIteration:
                    return
                yield value
        finally:
            if close := getattr(output, "close", None):
                close()

    return run


def bound_recording(resource, limit: int) -> None:
    # A dependent REST endpoint is a pipe whose generator runs once per parent page.
    # Limit every ancestor and stop advancing the child iterator once its sample is full.
    pipe = resource._pipe
    while pipe is not None:
        budget = RecordingLimit(limit)
        if pipe.has_parent:
            pipe.replace_gen(_bounded_transform(pipe.gen, budget))
        pipe.insert_step(budget, pipe.steps.index(pipe.gen) + 1)
        pipe = pipe.parent
