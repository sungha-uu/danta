from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field, model_validator

from danta.domain.entry import EntryPolicy
from danta.domain.risk import ExitPolicy


class EntryPolicyConfig(BaseModel):
    version: str
    approved_for_paper: bool = False
    max_snapshot_age_seconds: int = Field(gt=0)
    sell_pressure_block: Decimal = Field(ge=0, le=1)
    stabilization_required: Decimal = Field(ge=0, le=1)
    buy_recovery_required: Decimal = Field(ge=0, le=1)
    max_spread_bps: Decimal = Field(gt=0)

    def to_domain(self) -> EntryPolicy:
        return EntryPolicy(
            version=self.version,
            approved=self.approved_for_paper,
            max_snapshot_age_seconds=self.max_snapshot_age_seconds,
            sell_pressure_block=self.sell_pressure_block,
            stabilization_required=self.stabilization_required,
            buy_recovery_required=self.buy_recovery_required,
            max_spread_bps=self.max_spread_bps,
        )


class ExitPolicyConfig(BaseModel):
    version: str
    approved_for_paper: bool = False
    early_loss_pct: Decimal
    strong_loss_pct: Decimal
    early_defense_score: Decimal = Field(ge=0, le=1)
    strong_sell_pressure: Decimal = Field(ge=0, le=1)
    panic_market_stress: Decimal = Field(ge=0, le=1)
    profit_arm_pct: Decimal = Field(ge=0)
    profit_giveback_pct: Decimal = Field(gt=0)
    profit_weakness_score: Decimal = Field(ge=0, le=1)
    max_holding_minutes: int = Field(gt=0)

    def to_domain(self) -> ExitPolicy:
        return ExitPolicy(
            version=self.version,
            approved=self.approved_for_paper,
            early_loss_pct=self.early_loss_pct,
            strong_loss_pct=self.strong_loss_pct,
            early_defense_score=self.early_defense_score,
            strong_sell_pressure=self.strong_sell_pressure,
            panic_market_stress=self.panic_market_stress,
            profit_arm_pct=self.profit_arm_pct,
            profit_giveback_pct=self.profit_giveback_pct,
            profit_weakness_score=self.profit_weakness_score,
            max_holding_minutes=self.max_holding_minutes,
        )


class TradingPolicyRegistry(BaseModel):
    schema_version: str
    entry: EntryPolicyConfig
    exit: ExitPolicyConfig

    @model_validator(mode="after")
    def require_unique_versions(self) -> TradingPolicyRegistry:
        if not self.schema_version.strip():
            raise ValueError("schema_version is required")
        if self.entry.version == self.exit.version:
            raise ValueError("entry and exit policy versions must be distinct")
        return self


def load_policy_registry(path: Path) -> TradingPolicyRegistry:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"trading policy file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid trading policy JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("trading policy JSON must be an object")
    return TradingPolicyRegistry.model_validate(cast(dict[str, object], raw))
