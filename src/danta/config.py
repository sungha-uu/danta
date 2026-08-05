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
    krx_credentials_path: Path = Path(".secrets/imported_financial_statement_analysis/key.txt")
    smtp_enabled: bool = True
    log_level: str = "INFO"
    buy_requires_user_approval: bool = True
    unattended_auto_buy_enabled: bool = False
    paper_autonomous_campaign_path: Path = Path("private/paper_autonomous_campaign.json")
    paper_autonomous_kill_switch_path: Path = Path("private/PAPER_AUTONOMY_STOP")
    paper_autonomous_report_path: Path = Path("data/candidate_intraday_ai_report.json")
    paper_autonomous_poll_interval_seconds: int = Field(default=30, ge=5, le=300)
    autonomous_initial_capital_krw: int = Field(default=50_000_000, gt=0)
    paper_daily_close_enabled: bool = True
    paper_daily_close_root: Path = Path("data/paper-daily-close")
    auto_stop_sell_enabled: bool = True
    stop_loss_pct: Decimal = Decimal("7.0")
    stop_sell_requires_confirmation: bool = False
    paper_order_execution_enabled: bool = False
    real_order_execution_enabled: bool = False
    maximum_managed_symbols: int = Field(default=3, ge=1, le=20)
    order_poll_interval_seconds: Decimal = Field(default=Decimal("2.0"), ge=Decimal("1"))
    market_data_stale_seconds: int = Field(default=10, ge=2, le=60)
    fundamental_snapshot_path: Path = Path("data/fundamentals/latest.json")
    recommendation_performance_root: Path = Path(
        "data/recommendation-performance"
    )
    recommendation_round_trip_cost_bps: Decimal = Field(
        default=Decimal("35"),
        ge=0,
    )
    dart_corp_code_cache_path: Path = Path("data/public-context/dart-corp-codes.json")
    daily_run_root: Path = Path("data/daily-runs")
    dashboard_publish_repo: Path = Path("../danta_report")
    dashboard_public_url: str = "https://sungha-uu.github.io/danta_report/"
    daily_publish_enabled: bool = True
    daily_notify_enabled: bool = True
    market_wide_monitor_enabled: bool = True
    market_transition_email_enabled: bool = False
    market_wide_poll_interval_seconds: int = Field(default=30, ge=10, le=300)
    market_pages_publish_enabled: bool = True
    market_pages_publish_interval_seconds: int = Field(default=300, ge=300, le=3600)
    market_pages_git_push_enabled: bool = True
    market_dashboard_publish_repo: Path = Path("../danta_market_status")
    market_dashboard_public_url: str = "https://sungha-uu.github.io/danta_market_status/"
    market_entry_resume_required_path: Path = Path(
        "private/MARKET_ENTRY_RESUME_REQUIRED"
    )

    @model_validator(mode="after")
    def enforce_immutable_safety_policy(self) -> AppSettings:
        errors: list[str] = []
        if not self.buy_requires_user_approval:
            errors.append("buy_requires_user_approval must be true")
        # This legacy global switch remains locked. The only unattended path is
        # a validated, expiring PAPER_AUTONOMOUS_CAMPAIGN authorization file.
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
    if paper_execution := os.getenv("DANTA_PAPER_ORDER_EXECUTION_ENABLED"):
        data["paper_order_execution_enabled"] = paper_execution.lower() in {
            "1",
            "true",
            "yes",
        }
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
        raise ValueError(f"KRX credential file not found: {settings.krx_credentials_path}") from exc
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


def load_dart_api_key(settings: AppSettings) -> str:
    try:
        lines = settings.krx_credentials_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ValueError(
            f"provider credential file not found: {settings.krx_credentials_path}"
        ) from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() in {"DART_KEY", "DART_API_KEY"}:
            secret = value.strip().strip("\"'")
            if secret:
                return secret
    raise ValueError("DART_KEY must be configured")


def clear_settings_cache() -> None:
    load_settings.cache_clear()
