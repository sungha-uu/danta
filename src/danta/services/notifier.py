from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from decimal import Decimal
from email.message import EmailMessage
from html import escape

from danta.config import SmtpConfig
from danta.domain.market_wide import MarketWideRiskLevel


class NotificationError(RuntimeError):
    """Raised when an external notification could not be delivered."""


def _set_utf8_content(message: EmailMessage, body: str) -> None:
    """Encode Korean notification bodies without depending on host code pages."""
    message.set_content(body, charset="utf-8", cte="base64")


@dataclass(frozen=True)
class NotificationReceipt:
    recipient_count: int


class SmtpNotifier:
    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    def send_report_published(self, report_url: str, *, is_demo: bool) -> NotificationReceipt:
        label = "DEMO" if is_demo else "DAILY"
        data_notice = (
            "현재 리포트는 UI 검증용 데모 데이터입니다."
            if is_demo
            else "실제 수집 데이터로 생성된 일일 리포트입니다."
        )
        subject = f"[DANTA][{label}] GitHub Pages 리포트 배포 완료"
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = subject
        _set_utf8_content(message,
            "Danta 리포트가 GitHub Pages에 배포되었습니다.\n"
            f"{report_url}\n\n"
            + data_notice
        )
        message.add_alternative(
            "<html><body style=\"font-family:Arial,'Malgun Gothic',sans-serif;color:#172033\">"
            f"<h2>Danta {escape(label)} 리포트</h2>"
            "<p>GitHub Pages 배포가 완료되었습니다.</p>"
            f"<p><a href=\"{escape(report_url)}\">{escape(report_url)}</a></p>"
            f"<p>{data_notice}</p>"
            "<p style=\"font-size:12px;color:#657087\">계좌·주문·비밀정보는 포함하지 않습니다.</p>"
            "</body></html>",
            subtype="html",
        )

        self._deliver(message)

        return NotificationReceipt(recipient_count=len(self._config.recipients))

    def send_stage_completed(
        self,
        report_url: str,
        *,
        stage: str,
        detail: str,
    ) -> NotificationReceipt:
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = f"[DANTA][완료] {stage}"
        _set_utf8_content(message,
            f"{stage} 작업이 완료되었습니다.\n"
            f"{detail}\n\n"
            f"리포트: {report_url}\n"
        )
        message.add_alternative(
            "<html><body style=\"font-family:Arial,'Malgun Gothic',sans-serif;color:#172033\">"
            f"<h2>{escape(stage)}</h2>"
            f"<p>{escape(detail)}</p>"
            f"<p><a href=\"{escape(report_url)}\">리포트 열기</a></p>"
            "<p style=\"font-size:12px;color:#657087\">계좌·주문·비밀정보는 포함하지 않습니다.</p>"
            "</body></html>",
            subtype="html",
        )
        self._deliver(message)
        return NotificationReceipt(recipient_count=len(self._config.recipients))

    def send_entry_prices_determined(
        self,
        prices: list[tuple[str, int]],
    ) -> NotificationReceipt:
        if not prices:
            raise ValueError("at least one entry price is required")
        lines: list[str] = []
        for name, price in prices:
            if not name.strip():
                raise ValueError("stock name must not be blank")
            if price <= 0:
                raise ValueError("entry price must be positive")
            lines.append(f"{name.strip()} {price:,}원")
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = "지정가격 산정완료."
        _set_utf8_content(message, "\n".join(lines))
        self._deliver(message)
        return NotificationReceipt(recipient_count=len(self._config.recipients))

    def send_autonomous_selection_completed(
        self,
        selections: list[tuple[str, str, int, Decimal]],
    ) -> NotificationReceipt:
        if not selections:
            raise ValueError("at least one autonomous selection is required")
        lines: list[str] = []
        for name, grade, price, position_pct in selections:
            if not name.strip() or not grade.strip():
                raise ValueError("selection name and grade must not be blank")
            if price <= 0:
                raise ValueError("selection price must be positive")
            lines.append(
                f"{name.strip()} {grade.strip()} {price:,}원 "
                f"박스위치 {position_pct.quantize(Decimal('0.1'))}%"
            )
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = "자율 모의투자 종목 선정완료."
        _set_utf8_content(message, "\n".join(lines))
        self._deliver(message)
        return NotificationReceipt(recipient_count=len(self._config.recipients))

    def send_stop_loss_completed(
        self,
        trades: list[tuple[str, int, Decimal]],
    ) -> NotificationReceipt:
        if not trades:
            raise ValueError("at least one stop-loss trade is required")
        lines: list[str] = []
        for name, price, return_pct in trades:
            if not name.strip():
                raise ValueError("stock name must not be blank")
            if price <= 0:
                raise ValueError("sell price must be positive")
            lines.append(
                f"{name.strip()} {price:,}원 {return_pct.quantize(Decimal('0.1'))}%"
            )
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = "자동손절 체결완료."
        _set_utf8_content(message, "\n".join(lines))
        self._deliver(message)
        return NotificationReceipt(recipient_count=len(self._config.recipients))

    def send_buy_completed(
        self,
        trades: list[tuple[str, int, int]],
    ) -> NotificationReceipt:
        if not trades:
            raise ValueError("at least one completed buy is required")
        lines: list[str] = []
        for name, price, quantity in trades:
            if not name.strip():
                raise ValueError("stock name must not be blank")
            if price <= 0 or quantity <= 0:
                raise ValueError("buy fill price and quantity must be positive")
            lines.append(f"{name.strip()} {quantity:,}주 {price:,}원")
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = "자동매수 체결완료."
        _set_utf8_content(message, "\n".join(lines))
        self._deliver(message)
        return NotificationReceipt(recipient_count=len(self._config.recipients))

    def send_exit_completed(
        self,
        trades: list[tuple[str, int, Decimal, str]],
    ) -> NotificationReceipt:
        if not trades:
            raise ValueError("at least one completed exit trade is required")
        cause_labels = {
            "ADAPTIVE_PROFIT_FLOOR": "수익 보호",
            "PROFIT_TARGET": "익절",
            "TIME_EXIT": "시간 청산",
        }
        lines: list[str] = []
        for name, price, return_pct, cause in trades:
            if not name.strip():
                raise ValueError("stock name must not be blank")
            if price <= 0:
                raise ValueError("sell price must be positive")
            reason = cause_labels.get(cause, cause)
            lines.append(
                f"{name.strip()} {price:,}원 "
                f"{return_pct.quantize(Decimal('0.1'))}% {reason}"
            )
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = "자동매도 체결완료."
        _set_utf8_content(message, "\n".join(lines))
        self._deliver(message)
        return NotificationReceipt(recipient_count=len(self._config.recipients))

    def send_paper_daily_close(self, body: str) -> NotificationReceipt:
        if not body.strip():
            raise ValueError("paper daily close body must not be blank")
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = "# 자율 모의투자 현황"
        _set_utf8_content(message, body.strip())
        self._deliver(message)
        return NotificationReceipt(recipient_count=len(self._config.recipients))

    def send_market_risk_transition(
        self,
        *,
        previous: MarketWideRiskLevel | None,
        current: MarketWideRiskLevel,
        kospi_return_pct: Decimal,
        foreign_net_million: int,
        institution_net_million: int,
        pension_net_million: int,
        program_net_million: int,
        reasons: tuple[str, ...],
        dashboard_url: str,
    ) -> NotificationReceipt:
        labels = {
            MarketWideRiskLevel.NORMAL: "정상",
            MarketWideRiskLevel.CAUTION: "주의",
            MarketWideRiskLevel.RISK_OFF: "신규매수 중단",
            MarketWideRiskLevel.PANIC: "시장 비상",
        }
        before = "시작" if previous is None else labels[previous]
        after = labels[current]
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = ", ".join(self._config.recipients)
        message["Subject"] = f"[DANTA][시장위험] {before} → {after}"
        _set_utf8_content(message,
            f"시장 상태: {before} → {after}\n"
            f"KOSPI: {kospi_return_pct.quantize(Decimal('0.01'))}%\n"
            f"외국인: {foreign_net_million:,}백만원\n"
            f"기관: {institution_net_million:,}백만원\n"
            f"연기금 등: {pension_net_million:,}백만원\n"
            f"프로그램: {program_net_million:,}백만원\n"
            f"판정 근거: {', '.join(reasons)}\n"
            f"시장 현황판: {dashboard_url}\n"
        )
        self._deliver(message)
        return NotificationReceipt(recipient_count=len(self._config.recipients))

    def _deliver(self, message: EmailMessage) -> None:
        try:
            if self._config.use_ssl:
                with smtplib.SMTP_SSL(
                    self._config.smtp_server,
                    self._config.smtp_port,
                    timeout=60,
                    context=ssl.create_default_context(),
                ) as client:
                    self._send(client, message)
            else:
                with smtplib.SMTP(
                    self._config.smtp_server,
                    self._config.smtp_port,
                    timeout=60,
                ) as client:
                    client.starttls(context=ssl.create_default_context())
                    self._send(client, message)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotificationError("SMTP delivery failed") from exc

    def _send(self, client: smtplib.SMTP, message: EmailMessage) -> None:
        client.login(self._config.sender, self._config.password.get_secret_value())
        client.send_message(message)
