from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from danta.domain.approval import BuyApproval, OrderType


def _approval(now: datetime) -> BuyApproval:
    return BuyApproval(
        approval_id="approval-1",
        symbol="005930",
        max_amount_krw=3_000_000,
        order_type=OrderType.MARKET,
        expires_at=now + timedelta(minutes=10),
        max_holding_days=3,
        max_acceptable_price=100_000,
    )


def test_valid_approval_passes() -> None:
    now = datetime.now(UTC)
    _approval(now).validate_for_order(
        now=now,
        symbol="005930",
        expected_amount_krw=2_000_000,
        current_price=90_000,
        is_watched=True,
        already_used=False,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "000660"},
        {"expected_amount_krw": 3_000_001},
        {"current_price": 100_001},
        {"is_watched": False},
        {"already_used": True},
    ],
)
def test_approval_scope_violation_is_rejected(overrides: dict[str, object]) -> None:
    now = datetime.now(UTC)
    arguments: dict[str, object] = {
        "now": now,
        "symbol": "005930",
        "expected_amount_krw": 2_000_000,
        "current_price": 90_000,
        "is_watched": True,
        "already_used": False,
    }
    arguments.update(overrides)
    with pytest.raises(ValueError):
        _approval(now).validate_for_order(**arguments)  # type: ignore[arg-type]

