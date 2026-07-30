from __future__ import annotations

from email.message import EmailMessage
from types import TracebackType

from danta.config import SmtpConfig
from danta.services.notifier import SmtpNotifier


class FakeSmtp:
    instances: list[FakeSmtp] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.login_user = ""
        self.login_password = ""
        self.message: EmailMessage | None = None
        self.started_tls = False
        self.__class__.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def starttls(self, **_kwargs: object) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.login_user = user
        self.login_password = password

    def send_message(self, message: EmailMessage) -> None:
        self.message = message


def _config(*, use_ssl: bool) -> SmtpConfig:
    return SmtpConfig.model_validate(
        {
            "smtp_server": "smtp.example.com",
            "smtp_port": 465 if use_ssl else 587,
            "use_ssl": use_ssl,
            "sender": "sender@example.com",
            "password": "app-password",
            "recipients": ["owner@example.com"],
        }
    )


def test_report_notification_uses_ssl_and_contains_public_url(monkeypatch: object) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    FakeSmtp.instances.clear()
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSmtp)

    receipt = SmtpNotifier(_config(use_ssl=True)).send_report_published(
        "https://example.github.io/danta_report/",
        is_demo=True,
    )

    client = FakeSmtp.instances[-1]
    assert receipt.recipient_count == 1
    assert client.login_password == "app-password"
    assert client.message is not None
    assert "DEMO" in client.message["Subject"]
    plain_body = client.message.get_body(preferencelist=("plain",))
    assert plain_body is not None
    assert "https://example.github.io/danta_report/" in plain_body.get_content()


def test_report_notification_uses_starttls(monkeypatch: object) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    FakeSmtp.instances.clear()
    monkeypatch.setattr("smtplib.SMTP", FakeSmtp)

    SmtpNotifier(_config(use_ssl=False)).send_report_published(
        "https://example.github.io/danta_report/",
        is_demo=False,
    )

    assert FakeSmtp.instances[-1].started_tls is True


def test_stage_notification_contains_stage_and_detail(monkeypatch: object) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    FakeSmtp.instances.clear()
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSmtp)

    SmtpNotifier(_config(use_ssl=True)).send_stage_completed(
        "https://example.github.io/danta_report/",
        stage="14일 분봉 수집 완료",
        detail="감사 풀 50종목의 14거래일 수집을 검증했습니다.",
    )

    message = FakeSmtp.instances[-1].message
    assert message is not None
    assert "14일 분봉 수집 완료" in message["Subject"]
    plain_body = message.get_body(preferencelist=("plain",))
    assert plain_body is not None
    assert "감사 풀 50종목" in plain_body.get_content()


def test_entry_price_notification_is_simple_and_exact(monkeypatch: object) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    FakeSmtp.instances.clear()
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSmtp)

    SmtpNotifier(_config(use_ssl=True)).send_entry_prices_determined(
        [("SK하이닉스", 1_450_000), ("삼성전자", 208_500)]
    )

    message = FakeSmtp.instances[-1].message
    assert message is not None
    assert message["Subject"] == "지정가격 산정완료."
    plain_body = message.get_body(preferencelist=("plain",))
    assert plain_body is not None
    assert plain_body.get_content().strip() == (
        "SK하이닉스 1,450,000원\n삼성전자 208,500원"
    )


def test_stop_loss_notification_contains_fill_and_return(monkeypatch: object) -> None:
    from decimal import Decimal

    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    FakeSmtp.instances.clear()
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSmtp)

    SmtpNotifier(_config(use_ssl=True)).send_stop_loss_completed(
        [
            ("SK하이닉스", 1_479_300, Decimal("-6.96")),
            ("삼성전자", 213_000, Decimal("-6.99")),
        ]
    )

    message = FakeSmtp.instances[-1].message
    assert message is not None
    assert message["Subject"] == "자동손절 체결완료."
    plain_body = message.get_body(preferencelist=("plain",))
    assert plain_body is not None
    assert plain_body.get_content().strip() == (
        "SK하이닉스 1,479,300원 -7.0%\n삼성전자 213,000원 -7.0%"
    )


def test_profit_exit_notification_contains_reason(monkeypatch: object) -> None:
    from decimal import Decimal

    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    FakeSmtp.instances.clear()
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSmtp)

    SmtpNotifier(_config(use_ssl=True)).send_exit_completed(
        [
            (
                "SK하이닉스",
                1_422_187,
                Decimal("4.495"),
                "ADAPTIVE_PROFIT_FLOOR",
            )
        ]
    )

    message = FakeSmtp.instances[-1].message
    assert message is not None
    assert message["Subject"] == "자동매도 체결완료."
    plain_body = message.get_body(preferencelist=("plain",))
    assert plain_body is not None
    assert plain_body.get_content().strip() == (
        "SK하이닉스 1,422,187원 4.5% 수익 보호"
    )
