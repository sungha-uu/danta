from __future__ import annotations

import json
from pathlib import Path

import pytest

from danta.domain.mandate import EntryMandate
from danta.services.command_store import CommandStatus, FileCommandStore
from danta.services.runtime_lock import (
    RuntimeAlreadyRunningError,
    RuntimeInstanceLock,
)


def _mandate() -> EntryMandate:
    return EntryMandate.model_validate(
        {
            "report_data_as_of": "2026-07-30T09:00:00+09:00",
            "window_days": 14,
            "authority": "ENTRY_APPROVAL",
            "execution_mode": "USE_LOCKED_ACTIVE_MODE",
            "capital_scope": "KIS_ORDERABLE_CASH",
            "allocation_policy": "USER_DEFINED_ORDERABLE_CASH_PERCENT",
            "total_allocation_pct": "100.0",
            "unallocated_cash_pct": "0.0",
            "selected_symbol_count": 1,
            "entry_trigger": "LAST_PRICE_LTE_TARGET",
            "validity_policy": "UNTIL_FILLED_OR_USER_CANCELLED",
            "partial_fill_policy": (
                "PROTECT_FILLED_CANCEL_REMAINDER_ON_SAFETY_DETERIORATION"
            ),
            "duplicate_guard": "INTERNAL_ON_INGEST",
            "hard_stop_pct": "-7.0",
            "profit_policy": "ACTIVE_VERSIONED_LOCAL_ENGINE",
            "selections": [
                {
                    "rank": 9,
                    "symbol": "000660",
                    "name": "SK하이닉스",
                    "entry_target_price_krw": 1_325_000,
                    "entry_price_source": "USER_EDITED",
                    "allocation_pct": "100.0",
                    "ai_grade": "에이전트 추천",
                    "box_low": "1246000",
                    "box_high": "2305000",
                }
            ],
            "request": "paper entry",
        }
    )


def test_command_is_atomically_accepted_and_archived(tmp_path: Path) -> None:
    store = FileCommandStore(tmp_path / "commands")
    mandate = _mandate()

    submitted = store.submit(mandate)
    accepted = store.accept_next()

    assert submitted.exists() is False
    assert accepted is not None
    assert accepted.mandate.command_id == mandate.command_id
    assert len(list(store.active.glob("*.json"))) == 1

    archived = store.archive_active(
        mandate.command_id,
        status=CommandStatus.COMPLETED,
        reason="FLAT",
    )

    assert archived.exists()
    assert store.load_active() is None
    payload = json.loads(archived.read_text(encoding="utf-8"))
    assert payload["terminal_reason"] == "FLAT"


def test_duplicate_command_is_idempotent(tmp_path: Path) -> None:
    store = FileCommandStore(tmp_path / "commands")
    mandate = _mandate()

    first = store.submit(mandate)
    second = store.submit(mandate)

    assert first == second
    assert len(list(store.inbox.glob("*.json"))) == 1


def test_second_runtime_instance_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    first = RuntimeInstanceLock(path)
    second = RuntimeInstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(RuntimeAlreadyRunningError):
            second.acquire()
    finally:
        first.release()


def test_stale_runtime_lock_is_reclaimed(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    path.write_text('{"pid": 2147483647}', encoding="utf-8")

    lock = RuntimeInstanceLock(path)
    lock.acquire()
    lock.release()

    assert not path.exists()
