import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--shard",
        metavar="I/N",
        help="run only the I-th of N interleaved slices of the selected tests (I from 1)",
    )


def _shard(spec: str) -> tuple[int, int]:
    try:
        index, total = (int(part) for part in spec.split("/"))
    except ValueError:
        raise pytest.UsageError(f"--shard takes I/N, got {spec!r}") from None
    if not 1 <= index <= total:
        raise pytest.UsageError(f"--shard {spec}: I runs from 1 to N")
    return index, total


def _slice_of(items, total: int) -> list[int]:
    """Deal the tests out in turn, an `xdist_group` as one card: slow tests sit together in a
    few modules, and a group shares a module fixture that each slice would otherwise rebuild."""
    groups: dict[str, int] = {}
    slices = []
    dealt = 0
    for item in items:
        mark = item.get_closest_marker("xdist_group")
        group = mark and (mark.args[0] if mark.args else mark.kwargs.get("name", "default"))
        if group in groups:
            slices.append(groups[group])
            continue
        slices.append(dealt % total)
        if group:
            groups[group] = dealt % total
        dealt += 1
    return slices


# trylast: the slices divide the tests that survive -m and -k, so each slice gets its share of
# the run. Every xdist worker collects the same items, so each computes the same slice.
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    spec = config.getoption("shard")
    if not spec:
        return
    index, total = _shard(spec)
    slices = _slice_of(items, total)
    kept = [item for item, s in zip(items, slices, strict=True) if s == index - 1]
    dropped = [item for item, s in zip(items, slices, strict=True) if s != index - 1]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = kept


@pytest.fixture(autouse=True)
def _close_state_stores(monkeypatch):
    # A store's pooled sqlite connections otherwise reach the garbage collector open, and the
    # ResourceWarning Python raises then fails whichever later test is running at the time.
    # The platform's tests also run inside the Airflow image, where vecten_mcp is absent.
    try:
        from vecten_mcp.state import StateStore
    except ImportError:
        yield
        return

    opened = []
    init = StateStore.__init__

    def tracked(self, *args, **kwargs):
        opened.append(self)
        init(self, *args, **kwargs)

    monkeypatch.setattr(StateStore, "__init__", tracked)
    yield
    for store in opened:
        if hasattr(store, "engine"):
            store.close()
