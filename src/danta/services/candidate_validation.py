from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from danta.dashboard.models import DashboardReport
from danta.ports.broker import Quote


class QuoteClient(Protocol):
    async def current_price(self, symbol: str) -> Quote: ...


class CandidateValidationError(RuntimeError):
    """Raised when selected KRX candidates disagree materially with KIS."""


async def validate_candidate_quotes(
    report: DashboardReport,
    client: QuoteClient,
    *,
    maximum_difference_pct: Decimal = Decimal("2.0"),
) -> DashboardReport:
    differences: list[str] = []
    updated_candidates = []
    for candidate in report.candidates:
        quote = await client.current_price(candidate.code)
        krx_price = candidate.current_price
        difference_pct = abs(Decimal(quote.price) / krx_price - Decimal("1")) * Decimal(
            "100"
        )
        if difference_pct > maximum_difference_pct:
            differences.append(
                f"{candidate.code} KRX={krx_price} KIS={quote.price} "
                f"diff={difference_pct.quantize(Decimal('0.01'))}%"
            )
        updated_candidates.append(
            candidate.model_copy(update={"current_price": Decimal(quote.price)})
        )
    if differences:
        sample = "; ".join(differences[:3])
        raise CandidateValidationError(
            f"{len(differences)} candidates exceeded the KRX/KIS price tolerance: {sample}"
        )
    return report.model_copy(
        update={
            "calculation_version": f"{report.calculation_version}+kis-quote-verified",
            "candidates": updated_candidates,
        }
    )
