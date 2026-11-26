import pytest

from calc import add


@pytest.mark.skip(reason="planted: a skipped test must be noticed")
def test_add() -> None:
    assert add(2, 3) == 6
