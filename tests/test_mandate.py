from __future__ import annotations

from decimal import Decimal

import pytest

from danta.domain.mandate import parse_entry_mandate, plan_entries

MANDATE = """DANTA ENTRY_MANDATE
report_data_as_of: 2026-07-26T03:29:06+09:00
window_days: 14
authority: ENTRY_APPROVAL
execution_mode: USE_LOCKED_ACTIVE_MODE
capital_scope: KIS_ORDERABLE_CASH
allocation_policy: USER_DEFINED_ORDERABLE_CASH_PERCENT
total_allocation_pct: 100.0
unallocated_cash_pct: 0.0
selected_symbol_count: 3
entry_trigger: LAST_PRICE_LTE_TARGET
validity_policy: UNTIL_FILLED_OR_USER_CANCELLED
partial_fill_policy: PROTECT_FILLED_CANCEL_REMAINDER_ON_SAFETY_DETERIORATION
duplicate_guard: INTERNAL_ON_INGEST
hard_stop_pct: -7.0
profit_policy: ACTIVE_VERSIONED_LOCAL_ENGINE
selections:
- rank: 1
  symbol: 005930
  name: 삼성전자
  entry_target_price_krw: 242467
  entry_price_source: BOX_LOW_AUTO
  allocation_pct: 40.0
  ai_grade: 적극 추천
  box_low: 242467.29
  box_high: 264165.89
- rank: 2
  symbol: 000660
  name: SK하이닉스
  entry_target_price_krw: 1729646
  entry_price_source: BOX_LOW_AUTO
  allocation_pct: 40.0
  ai_grade: 적극 추천
  box_low: 1729645.68
  box_high: 1907071.62
- rank: 3
  symbol: 005380
  name: 현대차
  entry_target_price_krw: 401874
  entry_price_source: BOX_LOW_AUTO
  allocation_pct: 20.0
  ai_grade: 적극 추천
  box_low: 401874.08
  box_high: 447736.97
request: 승인문을 검증하고 현재 잠긴 계좌 모드에서 목표가 도달 시 자동매수를 위임한다.
"""


def test_dashboard_mandate_preserves_symbols_and_builds_stable_command_id() -> None:
    first = parse_entry_mandate(MANDATE)
    second = parse_entry_mandate(MANDATE)

    assert [item.symbol for item in first.selections] == ["005930", "000660", "005380"]
    assert first.command_id == second.command_id
    assert first.command_id == "entry-10ad79b713269c120e4919fc866384fc"
    assert all(
        item.selection_basis == "QUANTITATIVE_OPPORTUNITY"
        for item in first.selections
    )
    assert first.hard_stop_pct == Decimal("-7.0")


def test_plan_uses_user_percentages_of_orderable_cash() -> None:
    mandate = parse_entry_mandate(MANDATE)

    plans = plan_entries(mandate, orderable_cash=10_000_000)

    assert [plan.allocated_cash for plan in plans] == [4_000_000, 4_000_000, 2_000_000]
    assert [plan.quantity for plan in plans] == [16, 2, 4]
    assert len({plan.idempotency_key for plan in plans}) == 3


def test_allocation_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="total_allocation_pct"):
        parse_entry_mandate(
            MANDATE.replace(
                "total_allocation_pct: 100.0",
                "total_allocation_pct: 99.0",
            )
        )


def test_zero_share_plan_rejects_entire_mandate() -> None:
    mandate = parse_entry_mandate(MANDATE)

    with pytest.raises(ValueError, match="cannot buy one share"):
        plan_entries(mandate, orderable_cash=100_000)
