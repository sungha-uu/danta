from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from danta.adapters.kis.client import KisApiError, KisClient
from danta.config import AppSettings, load_kis_credentials, load_smtp_config
from danta.db.session import create_engine_and_session
from danta.domain.market_wide import MarketWideRiskLevel, MarketWideSnapshot
from danta.services.market_guard import MarketGuardDecision
from danta.services.market_wide_monitor import (
    MarketStatusPublisher,
    MarketWideCollector,
    MarketWideMonitor,
    is_market_risk_email_transition,
)
from danta.services.market_wide_repository import MarketWideRepository
from danta.services.notifier import NotificationError, SmtpNotifier

KST = ZoneInfo("Asia/Seoul")
SESSION_START = time(8, 50)
SESSION_END = time(15, 30)


class MarketMonitorApplication:
    """Standalone KOSPI monitor used independently of any entry mandate."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    async def run_session(self) -> None:
        engine, session_factory = create_engine_and_session(self.settings.database_url)
        async with engine.connect() as connection:
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        if revision != "0003_market_wide_monitor":
            await engine.dispose()
            raise RuntimeError("database schema is not current; run alembic upgrade head")
        repository = MarketWideRepository(session_factory)
        notifier = (
            SmtpNotifier(load_smtp_config(self.settings))
            if (
                self.settings.smtp_enabled
                and self.settings.market_transition_email_enabled
            )
            else None
        )
        publisher = MarketStatusPublisher(
            repository_path=self.settings.market_dashboard_publish_repo,
            git_push_enabled=self.settings.market_pages_git_push_enabled,
        )

        async def transition(
            snapshot: MarketWideSnapshot,
            decision: MarketGuardDecision,
            previous: MarketWideRiskLevel | None,
        ) -> None:
            if notifier is None or not is_market_risk_email_transition(
                previous, decision.level
            ):
                return
            try:
                await asyncio.to_thread(
                    notifier.send_market_risk_transition,
                    previous=previous,
                    current=decision.level,
                    kospi_return_pct=snapshot.kospi_return_pct,
                    foreign_net_million=snapshot.investor.foreign,
                    institution_net_million=snapshot.investor.institution,
                    pension_net_million=snapshot.investor.pension_fund_etc,
                    program_net_million=snapshot.program.total,
                    reasons=decision.reason_codes,
                    dashboard_url=self.settings.market_dashboard_public_url,
                )
            except NotificationError:
                return

        try:
            async with KisClient(
                load_kis_credentials(self.settings),
                token_cache_path=Path("data/kis-token-cache.json"),
            ) as client:
                monitor = MarketWideMonitor(
                    collector=MarketWideCollector(client),
                    repository=repository,
                    on_transition=transition,
                )
                next_publish_at = 0.0
                loop = asyncio.get_running_loop()
                while _within_market_session(datetime.now(KST)):
                    started = loop.time()
                    try:
                        snapshot, decision = await monitor.poll_once()
                    except KisApiError as exc:
                        print(
                            f"market monitor transient KIS error; retrying: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        await asyncio.sleep(
                            self.settings.market_wide_poll_interval_seconds
                        )
                        continue
                    if loop.time() >= next_publish_at:
                        try:
                            await publisher.publish(snapshot, decision)
                        except (OSError, RuntimeError) as exc:
                            print(
                                "market Pages publish failed; monitoring continues: "
                                f"{exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                        else:
                            next_publish_at = (
                                loop.time()
                                + self.settings.market_pages_publish_interval_seconds
                            )
                    elapsed = loop.time() - started
                    await asyncio.sleep(
                        max(
                            1.0,
                            self.settings.market_wide_poll_interval_seconds - elapsed,
                        )
                    )
        finally:
            await engine.dispose()


def _within_market_session(now: datetime) -> bool:
    return now.weekday() < 5 and SESSION_START <= now.time() <= SESSION_END


async def publish_market_once(settings: AppSettings) -> Path:
    """Collect and publish one sanitized snapshot for setup and recovery."""
    engine, session_factory = create_engine_and_session(settings.database_url)
    try:
        repository = MarketWideRepository(session_factory)
        async with KisClient(
            load_kis_credentials(settings),
            token_cache_path=Path("data/kis-token-cache.json"),
        ) as client:
            monitor = MarketWideMonitor(
                collector=MarketWideCollector(client),
                repository=repository,
            )
            snapshot, decision = await monitor.poll_once()
            return await MarketStatusPublisher(
                repository_path=settings.market_dashboard_publish_repo,
                git_push_enabled=False,
            ).publish(snapshot, decision)
    finally:
        await engine.dispose()
