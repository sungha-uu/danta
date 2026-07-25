from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from danta.config import (
    AppSettings,
    SmtpConfig,
    TradingEnvironment,
    load_kis_credentials,
    load_krx_environment,
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


def test_smtp_config_normalizes_recipients_without_exposing_password() -> None:
    config = SmtpConfig.model_validate(
        {
            "smtp_server": "smtp.example.com",
            "smtp_port": 465,
            "use_ssl": True,
            "sender": "sender@example.com",
            "password": "secret-password",
            "recipients": "first@example.com; second@example.com",
        }
    )

    assert config.recipients == ["first@example.com", "second@example.com"]
    assert "secret-password" not in repr(config)


def test_krx_credentials_are_loaded_into_expected_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "key.txt"
    credentials.write_text(
        "KRX_DATA_ID=test-id\nKRX_DATA_PW=test-password\n",
        encoding="utf-8",
    )
    settings = AppSettings(krx_credentials_path=credentials)
    monkeypatch.delenv("KRX_ID", raising=False)
    monkeypatch.delenv("KRX_PW", raising=False)

    load_krx_environment(settings)

    assert os.environ["KRX_ID"] == "test-id"
    assert os.environ["KRX_PW"] == "test-password"
