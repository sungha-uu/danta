from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, field_validator, model_validator


class EntrySelection(BaseModel):
    rank: int = Field(ge=1, le=200)
    symbol: str = Field(pattern=r"^[0-9A-Z]{6}$")
    name: str = Field(min_length=1, max_length=80)
    entry_target_price_krw: int = Field(gt=0)
    entry_price_source: Literal[
        "BOX_LOW_AUTO",
        "USER_EDITED",
        "PAPER_AUTONOMOUS_REPORT_PRICE",
    ]
    allocation_pct: Decimal = Field(gt=0, le=100)
    ai_grade: str = Field(min_length=1, max_length=40)
    box_low: Decimal = Field(gt=0)
    box_high: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_box(self) -> EntrySelection:
        if self.box_high <= self.box_low:
            raise ValueError("box_high must be greater than box_low")
        if self.allocation_pct != self.allocation_pct.quantize(Decimal("0.1")):
            raise ValueError("allocation_pct must have at most one decimal place")
        return self


class EntryMandate(BaseModel):
    report_data_as_of: datetime
    window_days: Literal[7, 14, 21]
    authority: Literal["ENTRY_APPROVAL"]
    execution_mode: Literal["USE_LOCKED_ACTIVE_MODE"]
    capital_scope: Literal["KIS_ORDERABLE_CASH"]
    allocation_policy: Literal["USER_DEFINED_ORDERABLE_CASH_PERCENT"]
    total_allocation_pct: Decimal = Field(gt=0, le=100)
    unallocated_cash_pct: Decimal = Field(ge=0, lt=100)
    selected_symbol_count: int = Field(ge=1, le=3)
    entry_trigger: Literal["LAST_PRICE_LTE_TARGET"]
    validity_policy: Literal["UNTIL_FILLED_OR_USER_CANCELLED"]
    partial_fill_policy: Literal["PROTECT_FILLED_CANCEL_REMAINDER_ON_SAFETY_DETERIORATION"]
    duplicate_guard: Literal["INTERNAL_ON_INGEST"]
    hard_stop_pct: Decimal
    profit_policy: Literal["ACTIVE_VERSIONED_LOCAL_ENGINE"]
    selections: list[EntrySelection] = Field(min_length=1, max_length=3)
    request: str = Field(min_length=1, max_length=240)

    @field_validator("window_days", mode="before")
    @classmethod
    def parse_window_days(cls, value: object) -> int:
        return int(str(value))

    @model_validator(mode="after")
    def validate_invariants(self) -> EntryMandate:
        errors: list[str] = []
        if self.report_data_as_of.tzinfo is None:
            errors.append("report_data_as_of must include a timezone")
        if self.hard_stop_pct != Decimal("-7.0"):
            errors.append("hard_stop_pct must be exactly -7.0")
        if len(self.selections) != self.selected_symbol_count:
            errors.append("selected_symbol_count does not match selections")
        symbols = [selection.symbol for selection in self.selections]
        if len(symbols) != len(set(symbols)):
            errors.append("selection symbols must be unique")
        calculated_total = sum(
            (selection.allocation_pct for selection in self.selections),
            Decimal("0"),
        )
        if calculated_total != self.total_allocation_pct:
            errors.append("total_allocation_pct does not match selections")
        if self.unallocated_cash_pct != Decimal("100.0") - calculated_total:
            errors.append("unallocated_cash_pct does not match allocation total")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def command_id(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"entry-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class PlannedEntry:
    symbol: str
    target_price: int
    allocation_pct: Decimal
    allocated_cash: int
    quantity: int
    idempotency_key: str


def parse_entry_mandate(value: str) -> EntryMandate:
    if len(value.encode("utf-8")) > 20_000:
        raise ValueError("ENTRY_MANDATE exceeds the maximum document size")
    first_line, separator, body = value.partition("\n")
    if first_line.strip() != "DANTA ENTRY_MANDATE" or not separator:
        raise ValueError("ENTRY_MANDATE header is missing")
    loaded = yaml.load(body, Loader=yaml.BaseLoader)
    if not isinstance(loaded, dict):
        raise ValueError("ENTRY_MANDATE body must be a mapping")
    return EntryMandate.model_validate(cast(dict[str, object], loaded))


def plan_entries(mandate: EntryMandate, *, orderable_cash: int) -> list[PlannedEntry]:
    if orderable_cash <= 0:
        raise ValueError("orderable_cash must be positive")
    plans: list[PlannedEntry] = []
    for selection in mandate.selections:
        allocated_cash = int(Decimal(orderable_cash) * selection.allocation_pct / Decimal("100"))
        quantity = allocated_cash // selection.entry_target_price_krw
        if quantity <= 0:
            raise ValueError(
                f"allocation for {selection.symbol} cannot buy one share at the target price"
            )
        plans.append(
            PlannedEntry(
                symbol=selection.symbol,
                target_price=selection.entry_target_price_krw,
                allocation_pct=selection.allocation_pct,
                allocated_cash=allocated_cash,
                quantity=quantity,
                idempotency_key=f"{mandate.command_id}:{selection.symbol}:BUY",
            )
        )
    return plans
