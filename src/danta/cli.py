from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from danta.adapters.kis.client import KisApiError, KisClient
from danta.adapters.krx.client import KrxDataError, PykrxMarketDataClient
from danta.config import (
    TradingEnvironment,
    load_dart_api_key,
    load_kis_credentials,
    load_krx_environment,
    load_settings,
    load_smtp_config,
)
from danta.dashboard.builder import build_dashboard, load_dashboard_report
from danta.dashboard.demo import demo_report
from danta.dashboard.models import DashboardReport
from danta.db.session import create_engine_and_session
from danta.services.active_box_walk_forward import run_active_box_walk_forward
from danta.services.ai_review import apply_ai_review, load_ai_review
from danta.services.assurance import build_assurance_report, write_assurance_report
from danta.services.autonomous_campaign import (
    AutonomousCandidatePreference,
    candidate_preference_path,
    create_campaign_authorization,
    load_campaign_authorization,
    read_candidate_preference,
    write_campaign_authorization,
    write_candidate_preference,
)
from danta.services.candidate_report import CandidateReportError, build_quant_report
from danta.services.candidate_validation import (
    CandidateValidationError,
    validate_candidate_quotes,
)
from danta.services.close_prefetch import run_close_prefetch
from danta.services.command_store import FileCommandStore
from danta.services.context_review import PublicContextCollector, build_context_review
from danta.services.daily_close import (
    DailyCloseError,
    DailyCloseResult,
    run_daily_close,
)
from danta.services.daily_operations import (
    DailyOperationError,
    run_scheduled_refresh,
)
from danta.services.daily_pipeline import run_daily_pipeline
from danta.services.intraday_report import (
    MinuteBarStore,
    backfill_minute_bars,
    build_intraday_report,
    market_cap_top_universe,
)
from danta.services.market_monitor_application import MarketMonitorApplication
from danta.services.notifier import NotificationError, SmtpNotifier
from danta.services.policy_registry import load_policy_registry
from danta.services.provider_doctor import KisProviderDoctor
from danta.services.public_dashboards import refresh_public_dashboards
from danta.services.recommendation_performance import (
    RecommendationPerformanceError,
    RecommendationPerformanceTracker,
)
from danta.services.runtime_lock import (
    RuntimeAlreadyRunningError,
    RuntimeInstanceLock,
)
from danta.services.runtime_repository import SqlRuntimeRepository
from danta.services.trading_application import TradingApplication


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="danta")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="validate KIS configuration and connectivity")
    doctor.add_argument("--live", action="store_true", help="call the KIS API")
    doctor.add_argument("--symbol", default="005930")

    server = subparsers.add_parser("serve", help="run the FastAPI development server")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8000)

    dashboard = subparsers.add_parser("dashboard", help="build the static candidate report")
    dashboard.add_argument("--input", type=Path, help="validated public report JSON")
    dashboard.add_argument("--output", type=Path, default=Path("dashboard/dist"))
    dashboard.add_argument("--demo", action="store_true", help="build with deterministic demo data")

    notify = subparsers.add_parser("notify-report", help="email a published report link")
    notify.add_argument("--url", required=True)
    notify.add_argument("--demo", action="store_true")

    subparsers.add_parser(
        "market-monitor",
        help="monitor KOSPI from 08:50 through 15:30 and publish market Pages",
    )
    notify.add_argument("--stage")
    notify.add_argument("--detail", default="")

    daily = subparsers.add_parser(
        "daily-report",
        help="collect KRX data and build the real KOSPI candidate report",
    )
    daily.add_argument(
        "--json-output",
        type=Path,
        default=Path("data/candidate_public_report.json"),
    )
    daily.add_argument(
        "--dashboard-output",
        type=Path,
        default=Path("dashboard/dist"),
    )
    daily.add_argument(
        "--skip-kis-validation",
        action="store_true",
        help="build an explicitly unverified offline report",
    )
    intraday = subparsers.add_parser(
        "intraday-report",
        help="backfill balanced KOSPI universe and build the real 60-minute report",
    )
    intraday.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/intraday/1m"),
    )
    intraday.add_argument(
        "--json-output",
        type=Path,
        default=Path("data/candidate_intraday_ai_report.json"),
    )
    intraday.add_argument(
        "--dashboard-output",
        type=Path,
        default=Path("dashboard/dist"),
    )
    intraday.add_argument(
        "--window-days",
        type=int,
        choices=(7, 14, 21),
        default=7,
        help="minute coverage target for the 200-symbol public ranking",
    )
    intraday.add_argument(
        "--skip-backfill",
        action="store_true",
        help="rebuild from stored minute bars without calling the KIS minute API",
    )
    walk_forward = subparsers.add_parser(
        "active-box-walk-forward",
        help="evaluate frozen active boxes on later stored minute bars",
    )
    walk_forward.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/intraday/1m"),
    )
    walk_forward.add_argument(
        "--output",
        type=Path,
        default=Path("data/experiments/active-box-walk-forward-v1.json"),
    )
    walk_forward.add_argument("--training-days", type=int, default=7)
    walk_forward.add_argument("--holding-days", type=int, default=5)
    walk_forward.add_argument(
        "--round-trip-cost-bps",
        type=Decimal,
        default=Decimal("35"),
    )
    ai_review = subparsers.add_parser(
        "apply-ai-review",
        help="apply a complete versioned AI review to a public report",
    )
    ai_review.add_argument("--input", type=Path, required=True)
    ai_review.add_argument("--review", type=Path, required=True)
    ai_review.add_argument("--output", type=Path, required=True)
    ai_review.add_argument(
        "--dashboard-output",
        type=Path,
        default=Path("dashboard/dist"),
    )
    context_review = subparsers.add_parser(
        "context-review",
        help="collect public context and review the fixed 14-day top 50",
    )
    context_review.add_argument("--input", type=Path, required=True)
    context_review.add_argument("--output", type=Path, required=True)
    context_review.add_argument(
        "--review-output",
        type=Path,
        default=Path("data/context-review-latest.json"),
    )
    context_review.add_argument(
        "--cache-root",
        type=Path,
        default=Path("data/public-context"),
    )
    context_review.add_argument(
        "--dashboard-output",
        type=Path,
        default=Path("dashboard/dist"),
    )
    context_review.add_argument("--refresh", action="store_true")
    runtime = subparsers.add_parser(
        "trading-runtime",
        help="run the mandate-independent KIS account runtime",
    )
    runtime.add_argument(
        "--policies",
        type=Path,
        default=Path("config/trading_policies.live.json"),
    )
    runtime.add_argument(
        "--command-root",
        type=Path,
        default=None,
    )
    runtime.add_argument(
        "--execute",
        action="store_true",
        help="required second gate; without it only validates configuration",
    )
    submit_mandate = subparsers.add_parser(
        "submit-entry-mandate",
        help="validate and atomically submit an ENTRY_MANDATE to the runtime inbox",
    )
    submit_mandate.add_argument("--input", type=Path, required=True)
    submit_mandate.add_argument(
        "--command-root",
        type=Path,
        default=Path("private/prod/commands"),
    )
    assurance = subparsers.add_parser(
        "assure",
        help="write the machine-readable active-account readiness report",
    )
    assurance.add_argument(
        "--policies",
        type=Path,
        default=Path("config/trading_policies.live.json"),
    )
    assurance.add_argument(
        "--output",
        type=Path,
        default=Path("data/assurance/latest.json"),
    )
    cycle = subparsers.add_parser(
        "daily-cycle",
        help="run KOSPI 200 orderable display and top-50 review cycle",
    )
    cycle.add_argument("--data-root", type=Path, default=Path("data/intraday/1m"))
    cycle.add_argument(
        "--report-output",
        type=Path,
        default=Path("data/candidate_intraday_public_report.json"),
    )
    cycle.add_argument(
        "--review-output",
        type=Path,
        default=Path("data/context-review-latest.json"),
    )
    cycle.add_argument("--dashboard-output", type=Path, default=Path("dashboard/dist"))
    cycle.add_argument("--context-cache", type=Path, default=Path("data/public-context"))
    cycle.add_argument("--use-context-cache", action="store_true")
    scheduled = subparsers.add_parser(
        "scheduled-refresh",
        help="run the idempotent 16:00 market-close refresh and Pages publish",
    )
    scheduled.add_argument("--force", action="store_true")
    scheduled.add_argument("--no-publish", action="store_true")
    scheduled.add_argument("--no-notify", action="store_true")
    prefetch = subparsers.add_parser(
        "close-prefetch",
        help="prefetch the completed regular-session minute bars after 15:30",
    )
    prefetch.add_argument("--force", action="store_true")
    autonomy = subparsers.add_parser(
        "autonomy",
        help="authorize, stop, resume, or inspect the active KIS autonomous campaign",
    )
    autonomy.add_argument(
        "action",
        choices=("authorize", "stop", "resume", "status", "prefer", "clear-preference"),
    )
    autonomy.add_argument("--days", type=int, default=90)
    autonomy.add_argument(
        "--symbols",
        nargs="+",
        help="one to three six-digit symbols to prioritize for one trading day",
    )
    autonomy.add_argument(
        "--trading-date",
        type=date.fromisoformat,
        help="preference date in YYYY-MM-DD; defaults to the next weekday",
    )
    autonomy.add_argument(
        "--require-include",
        action="store_true",
        help="include named READY symbols even when the AI grade is not approved",
    )
    autonomy.add_argument(
        "--execute",
        action="store_true",
        help="required for authorize and resume",
    )
    market_entry_gate = subparsers.add_parser(
        "market-entry-gate",
        help="inspect or acknowledge the latched market-wide new-entry stop",
    )
    market_entry_gate.add_argument("action", choices=("status", "resume"))
    market_entry_gate.add_argument(
        "--execute",
        action="store_true",
        help="required for resume after the operator reviews market conditions",
    )
    performance = subparsers.add_parser(
        "recommendation-performance",
        help="freeze and evaluate the fixed 14-day top-50 AI review cohort",
    )
    performance.add_argument(
        "--report",
        type=Path,
        default=Path("data/candidate_intraday_ai_report.json"),
    )
    performance.add_argument(
        "--data-root",
        type=Path,
        default=Path("data/intraday/1m"),
    )
    performance.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/recommendation-performance"),
    )
    performance.add_argument(
        "--round-trip-cost-bps",
        type=Decimal,
        default=Decimal("35"),
    )
    daily_close = subparsers.add_parser(
        "daily-close",
        help="email the active KIS autonomous account close summary",
    )
    daily_close.add_argument(
        "--force",
        action="store_true",
        help="allow manual execution outside the normal close-time gate",
    )
    public_dashboards = subparsers.add_parser(
        "public-dashboards",
        help="build sanitized operations and autonomous-performance Pages",
    )
    public_dashboards.add_argument(
        "--publish",
        action="store_true",
        help="commit and push both configured GitHub Pages repositories",
    )
    return parser


def _next_weekday(current: date) -> date:
    candidate = current + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


async def _doctor(live: bool, symbol: str) -> int:
    settings = load_settings()
    credentials = load_kis_credentials(settings)
    report = await KisProviderDoctor(settings, credentials).run(live=live, symbol=symbol)
    public_report = report.as_public_dict()
    output_path = Path("data/provider_capability_snapshot.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(public_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(output_path)
    print(json.dumps(public_report, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    args = _parser().parse_args()
    if args.command == "public-dashboards":
        try:
            public_dashboard_result = asyncio.run(
                refresh_public_dashboards(load_settings(), publish=args.publish)
            )
        except (OSError, ValueError, RuntimeError, KisApiError) as exc:
            print(f"public dashboard refresh failed: {exc}", file=sys.stderr)
            raise SystemExit(20) from None
        print(
            json.dumps(
                asdict(public_dashboard_result),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return
    if args.command == "market-entry-gate":
        settings = load_settings()
        latch = settings.market_entry_resume_required_path
        if args.action == "status":
            print(
                latch.read_text(encoding="utf-8")
                if latch.exists()
                else json.dumps(
                    {"resume_required": False},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if not args.execute:
            print("market entry resume requires --execute", file=sys.stderr)
            raise SystemExit(19)
        latch.unlink(missing_ok=True)
        print(
            "market-wide new-entry stop acknowledged; the live guard will "
            "re-latch it if risk remains"
        )
        return
    if args.command == "autonomy":
        try:
            settings = load_settings()
            credentials = load_kis_credentials(settings)
            if args.action == "authorize":
                if not args.execute:
                    raise PermissionError("authorize requires --execute")
                authorization = create_campaign_authorization(
                    now=datetime.now().astimezone(),
                    days=args.days,
                    environment=settings.environment,
                )
                write_campaign_authorization(
                    authorization,
                    settings.autonomous_campaign_path,
                )
                settings.autonomous_kill_switch_path.unlink(missing_ok=True)
                print(
                    json.dumps(
                        authorization.model_dump(mode="json"),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            if args.action == "prefer":
                if not args.execute:
                    raise PermissionError("candidate preference requires --execute")
                symbols = tuple(args.symbols or ())
                local_now = datetime.now().astimezone()
                trading_date = args.trading_date or _next_weekday(local_now.date())
                preference = AutonomousCandidatePreference(
                    trading_date=trading_date,
                    symbols=symbols,
                    selection_policy=(
                        "REQUIRE_INCLUDE" if args.require_include else "PRIORITIZE_ELIGIBLE"
                    ),
                    created_at=local_now,
                )
                write_candidate_preference(
                    preference,
                    candidate_preference_path(settings),
                )
                print(
                    json.dumps(
                        preference.model_dump(mode="json"),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            if args.action == "clear-preference":
                if not args.execute:
                    raise PermissionError("clearing candidate preference requires --execute")
                candidate_preference_path(settings).unlink(missing_ok=True)
                print("autonomous candidate preference cleared")
                return
            if args.action == "stop":
                settings.autonomous_kill_switch_path.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                settings.autonomous_kill_switch_path.write_text(
                    datetime.now().astimezone().isoformat(),
                    encoding="utf-8",
                )
                print("autonomous NEW ENTRIES stopped; position protection remains active")
                return
            if args.action == "resume":
                if not args.execute:
                    raise PermissionError("resume requires --execute")
                loaded_authorization = load_campaign_authorization(settings, credentials)
                if loaded_authorization is None or not loaded_authorization.permits_new_entries(
                    datetime.now().astimezone()
                ):
                    raise PermissionError("a valid non-expired campaign is required")
                settings.autonomous_kill_switch_path.unlink(missing_ok=True)
                print("autonomous new entries resumed")
                return
            loaded_authorization = load_campaign_authorization(settings, credentials)
            loaded_preference = read_candidate_preference(settings)
            print(
                json.dumps(
                    {
                        "authorization": (
                            None
                            if loaded_authorization is None
                            else loaded_authorization.model_dump(mode="json")
                        ),
                        "kill_switch": (settings.autonomous_kill_switch_path.exists()),
                        "candidate_preference": (
                            None
                            if loaded_preference is None
                            else loaded_preference.model_dump(mode="json")
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        except (OSError, ValueError, ValidationError, PermissionError) as exc:
            print(f"autonomy command rejected: {exc}", file=sys.stderr)
            raise SystemExit(18) from None
    if args.command == "market-monitor":
        try:
            asyncio.run(MarketMonitorApplication(load_settings()).run_session())
        except (OSError, RuntimeError, ValueError, KisApiError, NotificationError) as exc:
            print(f"market monitor failed: {exc}", file=sys.stderr)
            raise SystemExit(17) from None
        return
    if args.command == "close-prefetch":
        try:
            prefetch_result = asyncio.run(
                run_close_prefetch(
                    load_settings(),
                    force=args.force,
                    progress=lambda message: print(message, flush=True),
                )
            )
        except (
            ValueError,
            RuntimeError,
            KrxDataError,
            KisApiError,
            CandidateReportError,
        ) as exc:
            print(f"close prefetch failed: {exc}", file=sys.stderr)
            raise SystemExit(15) from None
        print(json.dumps(asdict(prefetch_result), ensure_ascii=False, indent=2))
        return
    if args.command == "scheduled-refresh":
        try:
            scheduled_result = asyncio.run(
                run_scheduled_refresh(
                    load_settings(),
                    force=args.force,
                    publish=not args.no_publish,
                    notify=not args.no_notify,
                    progress=lambda message: print(message, flush=True),
                )
            )
        except (
            DailyOperationError,
            ValueError,
            RuntimeError,
            KrxDataError,
            KisApiError,
            CandidateReportError,
            NotificationError,
        ) as exc:
            print(f"scheduled refresh failed: {exc}", file=sys.stderr)
            raise SystemExit(14) from None
        print(json.dumps(asdict(scheduled_result), ensure_ascii=False, indent=2))
        return
    if args.command == "assure":
        try:
            assurance_report = build_assurance_report(
                load_settings(),
                load_policy_registry(args.policies),
                project_root=Path.cwd(),
            )
            write_assurance_report(assurance_report, args.output)
        except (OSError, ValueError, ValidationError) as exc:
            print(f"assurance failed: {exc}", file=sys.stderr)
            raise SystemExit(11) from None
        print(json.dumps(assurance_report.as_dict(), ensure_ascii=False, indent=2))
        raise SystemExit(0 if assurance_report.ready_for_new_entries else 12)
    if args.command == "daily-cycle":
        try:
            daily_result = asyncio.run(
                run_daily_pipeline(
                    load_settings(),
                    data_root=args.data_root,
                    report_output=args.report_output,
                    review_output=args.review_output,
                    dashboard_output=args.dashboard_output,
                    context_cache_root=args.context_cache,
                    refresh_context=not args.use_context_cache,
                    progress=lambda message: print(message, flush=True),
                )
            )
        except (
            ValueError,
            RuntimeError,
            KrxDataError,
            KisApiError,
            CandidateReportError,
        ) as exc:
            print(f"daily cycle failed: {exc}", file=sys.stderr)
            raise SystemExit(13) from None
        print(
            f"daily cycle completed: {daily_result.candidate_count} candidates, "
            f"{daily_result.deep_review_count} context reviews, "
            f"{daily_result.dashboard_path}"
        )
        return
    if args.command == "submit-entry-mandate":
        try:
            target = FileCommandStore(args.command_root).submit_document(args.input)
        except (OSError, ValueError, ValidationError) as exc:
            print(f"mandate submission rejected: {exc}", file=sys.stderr)
            raise SystemExit(10) from None
        print(f"ENTRY_MANDATE submitted atomically: {target}")
        return
    if args.command == "trading-runtime":
        try:
            settings = load_settings()
            policies = load_policy_registry(args.policies)
            credentials = load_kis_credentials(settings)
            if not args.execute:
                print(
                    "account runtime configuration is valid; no order was sent. "
                    "Pass --execute to start recovery and monitoring."
                )
                return
            application = TradingApplication(
                settings=settings,
                credentials=credentials,
                mandate=None,
                policies=policies,
                command_root=args.command_root,
            )
            with RuntimeInstanceLock(Path("private/trading-runtime.lock")):
                asyncio.run(application.run())
        except (
            OSError,
            ValueError,
            ValidationError,
            PermissionError,
            KisApiError,
            RuntimeAlreadyRunningError,
        ) as exc:
            print(f"account runtime refused to start: {exc}", file=sys.stderr)
            raise SystemExit(10) from None
        return
    if args.command == "doctor":
        try:
            raise SystemExit(asyncio.run(_doctor(args.live, args.symbol)))
        except (ValueError, ValidationError):
            print(
                "KIS 설정을 검증하지 못했습니다. .secrets/kis/paper.json의 빈 값을 확인하세요.",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        except KisApiError as exc:
            print(f"KIS 연결 진단 실패: {exc}", file=sys.stderr)
            raise SystemExit(3) from None
    if args.command == "serve":
        uvicorn.run("danta.main:app", host=args.host, port=args.port, reload=False)
        return
    if args.command == "dashboard":
        if args.demo == (args.input is not None):
            print("dashboard requires exactly one of --demo or --input", file=sys.stderr)
            raise SystemExit(2)
        report = demo_report() if args.demo else load_dashboard_report(args.input)
        target = build_dashboard(report, args.output)
        print(f"dashboard built: {target}")
        return
    if args.command == "active-box-walk-forward":
        try:
            wf_report = run_active_box_walk_forward(
                MinuteBarStore(args.data_root),
                training_days=args.training_days,
                holding_days=args.holding_days,
                round_trip_cost_bps=args.round_trip_cost_bps,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    wf_report.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(args.output)
        except (ValueError, CandidateReportError) as exc:
            print(f"active box walk-forward failed: {exc}", file=sys.stderr)
            raise SystemExit(9) from None
        active, baseline = wf_report.summaries
        print(
            f"active box walk-forward built: {args.output} "
            f"(active {active.trades} trades, baseline {baseline.trades}, "
            f"status {wf_report.sample_status})",
            flush=True,
        )
        return
    if args.command == "recommendation-performance":
        try:
            result = RecommendationPerformanceTracker(
                args.output_root,
                round_trip_cost_bps=args.round_trip_cost_bps,
            ).update(
                load_dashboard_report(args.report),
                MinuteBarStore(args.data_root),
            )
        except (
            OSError,
            ValueError,
            ValidationError,
            RecommendationPerformanceError,
        ) as exc:
            print(
                f"recommendation performance failed: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(13) from None
        print(
            "recommendation performance updated: "
            f"{result.snapshot_count} snapshots, "
            f"{result.completed_outcome_count} completed outcomes, "
            f"status {result.recommendation_edge_status}",
            flush=True,
        )
        return
    if args.command == "daily-close":
        settings = load_settings()
        try:
            credentials = load_kis_credentials(settings)
            notifier = SmtpNotifier(load_smtp_config(settings))

            async def close_report() -> DailyCloseResult:
                engine, session_factory = create_engine_and_session(
                    settings.database_url
                )
                try:
                    internal_positions = await SqlRuntimeRepository(
                        session_factory
                    ).load_open_positions()
                    async with KisClient(
                        credentials,
                        token_cache_path=settings.kis_token_cache_path,
                    ) as client:
                        return await run_daily_close(
                            settings,
                            credentials,
                            client,
                            notifier,
                            internal_positions=internal_positions,
                            force=args.force,
                        )
                finally:
                    await engine.dispose()

            close_result = asyncio.run(close_report())
        except (
            OSError,
            ValueError,
            ValidationError,
            PermissionError,
            KisApiError,
            NotificationError,
            DailyCloseError,
        ) as exc:
            print(f"paper daily close failed: {exc}", file=sys.stderr)
            raise SystemExit(14) from None
        print(
            f"paper daily close: {close_result.status} "
            f"({close_result.trading_date}; {close_result.detail})",
            flush=True,
        )
        return
    if args.command == "notify-report":
        try:
            settings = load_settings()
            notifier = SmtpNotifier(load_smtp_config(settings))
            if args.stage:
                receipt = notifier.send_stage_completed(
                    args.url,
                    stage=args.stage,
                    detail=args.detail or "요청한 단계가 완료되었습니다.",
                )
            else:
                receipt = notifier.send_report_published(args.url, is_demo=args.demo)
        except (ValueError, ValidationError, NotificationError) as exc:
            print(f"report notification failed: {exc}", file=sys.stderr)
            raise SystemExit(4) from None
        print(f"report notification sent to {receipt.recipient_count} configured recipient(s)")
        return
    if args.command == "apply-ai-review":
        try:
            report = load_dashboard_report(args.input)
            reviewed = apply_ai_review(report, load_ai_review(args.review))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    reviewed.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(args.output)
            target = build_dashboard(reviewed, args.dashboard_output)
        except (ValueError, ValidationError) as exc:
            print(f"AI review failed: {exc}", file=sys.stderr)
            raise SystemExit(7) from None
        print(f"AI-reviewed report built: {target}")
        return
    if args.command == "context-review":
        try:
            report = load_dashboard_report(args.input)
            snapshots = asyncio.run(
                PublicContextCollector(
                    args.cache_root,
                    dart_api_key=load_dart_api_key(load_settings()),
                ).collect(
                    [
                        (item.code, item.name)
                        for item in sorted(
                            [
                                *report.candidates,
                                *report.extended_watchlist,
                            ],
                            key=lambda candidate: candidate.windows["14"].rank or 999,
                        )[:50]
                    ],
                    refresh=args.refresh,
                )
            )
            review = build_context_review(
                report,
                snapshots,
                reviewed_at=datetime.now().astimezone(),
            )
            reviewed = apply_ai_review(report, review)
            args.review_output.parent.mkdir(parents=True, exist_ok=True)
            review_temporary = args.review_output.with_suffix(".tmp")
            review_temporary.write_text(
                json.dumps(
                    review.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            review_temporary.replace(args.review_output)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    reviewed.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(args.output)
            target = build_dashboard(reviewed, args.dashboard_output)
        except (ValueError, ValidationError) as exc:
            print(f"context review failed: {exc}", file=sys.stderr)
            raise SystemExit(8) from None
        print(
            f"context-reviewed report built: {target} ({len(review.candidates)} candidates)",
            flush=True,
        )
        return
    if args.command == "daily-report":
        try:
            settings = load_settings()
            load_krx_environment(settings)
            dataset = PykrxMarketDataClient().collect()
            report = build_quant_report(dataset)
            if not args.skip_kis_validation:
                credentials = load_kis_credentials(settings)

                async def validate() -> DashboardReport:
                    async with KisClient(
                        credentials,
                        token_cache_path=Path("data/kis-token-cache.json"),
                    ) as client:
                        return await validate_candidate_quotes(report, client)

                report = asyncio.run(validate())
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.json_output.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    report.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(args.json_output)
            target = build_dashboard(report, args.dashboard_output)
        except (
            ValueError,
            KrxDataError,
            KisApiError,
            CandidateReportError,
            CandidateValidationError,
        ) as exc:
            print(f"daily report failed: {exc}", file=sys.stderr)
            raise SystemExit(5) from None
        print(f"real KOSPI report built: {target} (data as of {report.data_as_of.isoformat()})")
        return
    if args.command == "intraday-report":
        try:
            settings = load_settings()
            load_krx_environment(settings)
            print("collecting 21 KRX trading days...", flush=True)
            dataset = PykrxMarketDataClient().collect(required_days=21)
            universe = market_cap_top_universe(dataset, limit=200)
            snapshot = Path("data/market-cap-top-200-v1.json")
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot_temporary = snapshot.with_suffix(".tmp")
            snapshot_temporary.write_text(
                json.dumps(
                    {
                        "version": "market-cap-top-200-v1",
                        "data_as_of": dataset.trading_dates[-1].isoformat(),
                        "count": len(universe),
                        "symbols": [
                            {
                                "symbol": item.symbol,
                                "name": item.name,
                                "market_cap": str(item.market_cap),
                                "latest_price": str(item.latest_price),
                                "average_trading_value": str(item.average_trading_value),
                            }
                            for item in universe
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            snapshot_temporary.replace(snapshot)
            collection_candidates = universe
            print(
                f"collection universe: {len(collection_candidates)} symbols; "
                f"starting resumable {args.window_days}-day minute backfill",
                flush=True,
            )
            if not args.skip_backfill:
                credentials = load_kis_credentials(settings)

                async def collect_minutes() -> None:
                    async with KisClient(
                        credentials,
                        token_cache_path=Path("data/kis-token-cache.json"),
                    ) as client:
                        await backfill_minute_bars(
                            client,
                            MinuteBarStore(args.data_root),
                            collection_candidates,
                            dataset.trading_dates,
                            window_days=args.window_days,
                            progress=lambda message: print(message, flush=True),
                        )

                asyncio.run(collect_minutes())
            else:
                print("KIS minute backfill skipped; using stored bars", flush=True)
            report = build_intraday_report(
                dataset,
                universe,
                MinuteBarStore(args.data_root),
                strategy_status=(
                    "ACTIVE"
                    if (
                        settings.real_order_execution_enabled
                        if settings.environment is TradingEnvironment.PROD
                        else settings.paper_order_execution_enabled
                    )
                    else "RESEARCH_ONLY"
                ),
            )
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.json_output.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    report.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(args.json_output)
            target = build_dashboard(report, args.dashboard_output)
        except (ValueError, KrxDataError, KisApiError, CandidateReportError) as exc:
            print(f"intraday report failed: {exc}", file=sys.stderr)
            raise SystemExit(6) from None
        print(
            f"real intraday KOSPI report built: {target} "
            f"(data as of {report.data_as_of.isoformat()})",
            flush=True,
        )
        return


if __name__ == "__main__":
    main()
