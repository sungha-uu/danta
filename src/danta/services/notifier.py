from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from danta.config import SmtpConfig


class NotificationError(RuntimeError):
    """Raised when an external notification could not be delivered."""


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
        message.set_content(
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
        message.set_content(
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
