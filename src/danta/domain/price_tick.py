from __future__ import annotations


def kospi_tick_size(price: int) -> int:
    if price <= 0:
        raise ValueError("price must be positive")
    if price < 1_000:
        return 1
    if price < 5_000:
        return 5
    if price < 10_000:
        return 10
    if price < 50_000:
        return 50
    if price < 100_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def floor_kospi_price(price: int) -> int:
    tick = kospi_tick_size(price)
    return price // tick * tick
