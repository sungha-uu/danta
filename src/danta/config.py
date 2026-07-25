from __future__ import annotations

import json
import os
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class TradingEnvironment(StrEnum):
    PAPER = "paper"
    PROD = "prod"


class AppSettings(BaseModel):
    app_name: str = "danta"
    environment: TradingEnvironment = TradingEnvironment.PAPER
    database_url: str = "sqlite+aiosqlite:///./data/danta-paper.db"
    kis_credentials_path: Path = Path(".secrets/kis/paper.json")
    smtp_config_path: Path = Path(
        ".secrets/imported_financial_statement_analysis/email_config.json"
    )
    krx_credentials_path: Path = Path(
        ".secrets/imported_financial_statement_analysis/key.txt"
    )
    smtp_enabled: bool = True
    log_level: str = "INFO"
    buy_requires_user_approval: bool = True
    unattended_auto_buy_enabled: bool = False
    auto_stop_sell_enabled: bool = True
    stop_loss_pct: Decimal = Decimal("7.0")
    stop_sell_requires_confirmation: bool = False
    paper_order_execution_enabled: bool = False
    real_order_execution_enabled: bool = False

    @model_validator(mode="after")
    def enforce_immutable_safety_policy(self) -> AppSettings:
        errors: list[str] = []
        if not self.buy_requires_user_approval:
            errors.append("buy_requires_user_approval must be true")
        if self.unattended_auto_buy_enabled:
            errors.append("unattended_auto_buy_enabled must be false")
        if not self.auto_stop_sell_enabled:
            errors.append("auto_stop_sell_enabled must be true")
        if self.stop_loss_pct != Decimal("7.0"):
            errors.append("stop_loss_pct must be exactly 7.0")
        if self.stop_sell_requires_confirmation:
            errors.append("stop_sell_requires_confirmation must be false")
        if self.environment is TradingEnvironment.PROD and self.real_order_execution_enabled:
            errors.append("real order execution is locked during Phase 0")
        if errors:
            raise ValueError("; ".join(errors))
        return self


class KisCredentials(BaseModel):
    environment: TradingEnvironment
    app_key: SecretStr
    app_secret: SecretStr
    account_no: str = Field(pattern=r"^\d{8}$")
    product_code: str = Field(default="01", pattern=r"^\d{2}$")
    hts_id: SecretStr

    @field_validator("app_key", "app_secret", "hts_id")
    @classmethod
    def reject_blank_secrets(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"JSON configuration must be an object: {path}")
        return cast(dict[str, object], value)
    except FileNotFoundError as exc:
        raise ValueError(f"configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON configuration: {path}") from exc


@lru_cache(maxsize=1)
def load_settings() -> AppSettings:
    path = Path(os.getenv("DANTA_CONFIG", "config/app.json"))
    if not path.exists():
        path = Path("config/app.example.json")
    data = _read_json(path)
    if database_url := os.getenv("DANTA_DATABASE_URL"):
        data["database_url"] = database_url
    return AppSettings.model_validate(data)


def load_kis_credentials(settings: AppSettings) -> KisCredentials:
    credentials = KisCredentials.model_validate(_read_json(settings.kis_credentials_path))
    if credentials.environment is not settings.environment:
        raise ValueError(
            "KIS credential environment does not match application environment: "
            f"{credentials.environment} != {settings.environment}"
        )
    return credentials


class SmtpConfig(BaseModel):
    smtp_server: str
    smtp_port: int = Field(ge=1, le=65535)
    use_ssl: bool = False
    sender: str
    password: SecretStr
    recipients: list[str]

    @field_validator("smtp_server", "sender")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("password")
    @classmethod
    def reject_blank_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("recipients", mode="before")
    @classmethod
    def normalize_recipients(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(part).strip() for part in value if str(part).strip()]
        raise ValueError("recipients must be a string or list")

    @field_validator("recipients")
    @classmethod
    def require_recipients(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one recipient is required")
        return value


def load_smtp_config(settings: AppSettings) -> SmtpConfig:
    if not settings.smtp_enabled:
        raise ValueError("SMTP notifications are disabled")
    return SmtpConfig.model_validate(_read_json(settings.smtp_config_path))


def load_krx_environment(settings: AppSettings) -> None:
    try:
        lines = settings.krx_credentials_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValueError(
            f"KRX credential file not found: {settings.krx_credentials_path}"
        ) from exc
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    krx_id = values.get("KRX_DATA_ID", "")
    krx_password = values.get("KRX_DATA_PW", values.get("KRX_DATA_PASSWORD", ""))
    if not krx_id or not krx_password:
        raise ValueError("KRX_DATA_ID and KRX_DATA_PW must be configured")
    os.environ["KRX_ID"] = krx_id
    os.environ["KRX_PW"] = krx_password


def clear_settings_cache() -> None:
    load_settings.cache_clear()
