from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from danta.config import (
    AppSettings,
    TradingEnvironment,
    load_kis_credentials,
)


def test_immutable_policy_defaults_are_safe() -> None:
    settings = AppSettings()
    assert settings.environment is TradingEnvironment.PAPER
    assert settings.buy_requires_user_approval is True
    assert settings.unattended_auto_buy_enabled is False
    assert settings.stop_loss_pct == Decimal("7.0")
    assert settings.real_order_execution_enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("buy_requires_user_approval", False),
        ("unattended_auto_buy_enabled", True),
        ("auto_stop_sell_enabled", False),
        ("stop_loss_pct", "6.9"),
        ("stop_sell_requires_confirmation", True),
    ],
)
def test_unsafe_policy_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate({field: value})


def test_credentials_environment_must_match(paper_credentials_file: Path) -> None:
    settings = AppSettings(
        environment="prod",
        kis_credentials_path=paper_credentials_file,
    )
    with pytest.raises(ValueError, match="does not match"):
        load_kis_credentials(settings)

