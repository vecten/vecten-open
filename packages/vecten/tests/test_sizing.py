import pytest

from vecten.intake.sizing import size_for_users


@pytest.mark.parametrize(
    "users,size",
    [(1, "small"), (10, "small"), (11, "medium"), (50, "medium"), (51, "large"), (500, "large")],
)
def test_thresholds(users, size):
    assert size_for_users(users) == size
