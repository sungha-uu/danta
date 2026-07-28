import pytest

from danta.domain.price_tick import floor_kospi_price, kospi_tick_size


@pytest.mark.parametrize(
    ("price", "tick"),
    [(999, 1), (1000, 5), (5000, 10), (10000, 50), (50000, 100), (100000, 500), (500000, 1000)],
)
def test_kospi_tick_boundaries(price: int, tick: int) -> None:
    assert kospi_tick_size(price) == tick


def test_floor_price_uses_valid_tick() -> None:
    assert floor_kospi_price(221_278) == 221_000
    assert floor_kospi_price(1_565_433) == 1_565_000
