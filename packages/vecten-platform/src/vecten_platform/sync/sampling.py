"""Bound the source iterator before dlt's incremental and user transforms."""

from dlt.extract.items_transform import LimitItem

from vecten_shared.output import ExitCode, Failure


class RecordingLimit(LimitItem):
    # dlt inserts later maps by affinity. Trim the page before redaction and recording.
    placement_affinity = -1.0

    def __init__(self, limit: int):
        super().__init__(max_items=limit, max_time=None, count_rows=True)

    def __call__(self, item, meta=None):
        if isinstance(item, list):
            item = item[: max(0, self.max_items - self.count)]
        return super().__call__(item, meta)


def bound_recording(resource, limit: int) -> None:
    if resource.is_transformer:
        raise Failure(
            ExitCode.REFUSED,
            f"{resource.name} transforms an upstream resource; use a synthetic fixture "
            "because the upstream pull cannot be bounded here",
        )
    # Keep any tighter limit installed by the connector. dlt's add_limit replaces it.
    resource.add_step(RecordingLimit(limit), insert_at=1)
