from vecten_shared.config import Size

THRESHOLDS: tuple[tuple[int, Size], ...] = ((10, "small"), (50, "medium"))


def size_for_users(users: int) -> Size:
    for ceiling, size in THRESHOLDS:
        if users <= ceiling:
            return size
    return "large"
