import asyncio
from decimal import Decimal

import pytest

from danta.services.capital_allocator import CapitalAllocator


async def test_concurrent_reservations_cannot_exceed_symbol_cap() -> None:
    allocator = CapitalAllocator()
    await allocator.register_mandate(
        mandate_id="m1",
        orderable_cash=1_000_000,
        allocations={"005930": Decimal("50"), "000660": Decimal("50")},
    )

    async def reserve(reservation_id: str) -> bool:
        try:
            await allocator.reserve(
                mandate_id="m1",
                symbol="005930",
                amount=300_000,
                reservation_id=reservation_id,
            )
            return True
        except ValueError:
            return False

    results = await asyncio.gather(reserve("r1"), reserve("r2"))
    assert sorted(results) == [False, True]
    assert await allocator.available_for_symbol("m1", "005930") == 200_000


async def test_reservation_is_idempotent_and_release_restores_capacity() -> None:
    allocator = CapitalAllocator()
    await allocator.register_mandate(
        mandate_id="m1",
        orderable_cash=1_000_000,
        allocations={"005930": Decimal("100")},
    )
    first = await allocator.reserve(
        mandate_id="m1", symbol="005930", amount=700_000, reservation_id="r1"
    )
    second = await allocator.reserve(
        mandate_id="m1", symbol="005930", amount=700_000, reservation_id="r1"
    )
    assert first == second
    assert await allocator.release("r1")
    assert await allocator.available_for_symbol("m1", "005930") == 1_000_000


async def test_changed_duplicate_reservation_is_rejected() -> None:
    allocator = CapitalAllocator()
    await allocator.register_mandate(
        mandate_id="m1",
        orderable_cash=1_000_000,
        allocations={"005930": Decimal("100")},
    )
    await allocator.reserve(
        mandate_id="m1", symbol="005930", amount=100_000, reservation_id="r1"
    )
    with pytest.raises(ValueError, match="different values"):
        await allocator.reserve(
            mandate_id="m1", symbol="005930", amount=200_000, reservation_id="r1"
        )


async def test_partial_fill_consumes_value_and_keeps_remainder_reserved() -> None:
    allocator = CapitalAllocator()
    await allocator.register_mandate(
        mandate_id="m1",
        orderable_cash=1_000_000,
        allocations={"005930": Decimal("100")},
    )
    await allocator.reserve(
        mandate_id="m1",
        symbol="005930",
        amount=900_000,
        reservation_id="r1",
    )
    await allocator.consume_partial("r1", amount=300_000)
    assert await allocator.available_for_symbol("m1", "005930") == 100_000
    assert await allocator.release("r1")
    assert await allocator.available_for_symbol("m1", "005930") == 700_000
