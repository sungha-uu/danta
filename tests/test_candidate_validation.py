from __future__ import annotations

from decimal import Decimal

import pytest

from danta.dashboard.demo import demo_report
from danta.ports.broker import Quote
from danta.services.candidate_validation import (
    CandidateValidationError,
    validate_candidate_quotes,
)


class FakeQuoteClient:
    def __init__(self, *, multiplier: Decimal) -> None:
        self.multiplier = multiplier

    async def current_price(self, symbol: str) -> Quote:
        report = demo_report()
        price = next(
            candidate.current_price for candidate in report.candidates if candidate.code == symbol
        )
        return Quote(
            symbol=symbol,
            price=int(price * self.multiplier),
            change_rate=None,
            raw_timestamp=None,
        )


@pytest.mark.asyncio
async def test_candidate_quotes_add_verified_calculation_version() -> None:
    report = demo_report()

    verified = await validate_candidate_quotes(
        report,
        FakeQuoteClient(multiplier=Decimal("1")),
    )

    assert verified.calculation_version.endswith("+kis-quote-verified")


@pytest.mark.asyncio
async def test_candidate_quote_mismatch_blocks_report() -> None:
    report = demo_report()

    with pytest.raises(CandidateValidationError, match="50 candidates"):
        await validate_candidate_quotes(
            report,
            FakeQuoteClient(multiplier=Decimal("1.10")),
        )
