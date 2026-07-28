from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from danta.adapters.kis.client import KisApiError, KisClient
from danta.adapters.krx.client import KrxDataError, PykrxMarketDataClient
from danta.config import (
    load_dart_api_key,
    load_kis_credentials,
    load_krx_environment,
    load_settings,
    load_smtp_config,
)
from danta.dashboard.builder import build_dashboard, load_dashboard_report
from danta.dashboard.demo import demo_report
from danta.dashboard.models import DashboardReport
from danta.domain.mandate import parse_entry_mandate
from danta.services.active_box_walk_forward import run_active_box_walk_forward
from danta.services.ai_review import apply_ai_review, load_ai_review
from danta.services.assurance import build_assurance_report, write_assurance_report
from danta.services.candidate_report import CandidateReportError, build_quant_report
from danta.services.candidate_validation import (
    CandidateValidationError,
    validate_candidate_quotes,
)
from danta.services.close_prefetch import run_close_prefetch
from danta.services.context_review import PublicContextCollector, build_context_review
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
from danta.services.notifier import NotificationError, SmtpNotifier
from danta.services.paper_broker_campaign import PaperBrokerCampaign
from danta.services.paper_trading_application import PaperTradingApplication
from danta.services.policy_registry import load_policy_registry
from danta.services.provider_doctor import KisProviderDoctor


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
        default=Path("data/candidate_intraday_public_report.json"),
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
    paper = subparsers.add_parser(
        "paper-trade",
        help="run the approved KIS paper execution runtime",
    )
    paper.add_argument("--mandate", type=Path, required=True)
    paper.add_argument(
        "--policies",
        type=Path,
        default=Path("config/trading_policies.paper.json"),
    )
    paper.add_argument(
        "--execute",
        action="store_true",
        help="required second gate; without it only validates inputs",
    )
    assurance = subparsers.add_parser(
        "assure",
        help="write the machine-readable paper readiness report",
    )
    assurance.add_argument(
        "--policies",
        type=Path,
        default=Path("config/trading_policies.paper.json"),
    )
    assurance.add_argument(
        "--output",
        type=Path,
        default=Path("data/assurance/latest.json"),
    )
    cycle = subparsers.add_parser(
        "daily-cycle",
        help="run KOSPI 200, official max-30 review, and dashboard cycle",
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
    cycle.add_argument(
        "--dashboard-output", type=Path, default=Path("dashboard/dist")
    )
    cycle.add_argument(
        "--context-cache", type=Path, default=Path("data/public-context")
    )
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
    campaign = subparsers.add_parser(
        "paper-campaign",
        help="run the isolated one-share Samsung/SK hynix paper lifecycle campaign",
    )
    campaign.add_argument("--execute", action="store_true")
    campaign.add_argument("--monitor-seconds", type=int, default=30)
    campaign.add_argument("--start-discount", type=Decimal, default=Decimal("0"))
    campaign.add_argument("--end-discount", type=Decimal, default=Decimal("0.5"))
    campaign.add_argument(
        "--output",
        type=Path,
        default=Path("data/paper-campaign/latest.jsonl"),
    )
    return parser


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
        raise SystemExit(0 if assurance_report.ready_for_new_paper_entries else 12)
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
    if args.command == "paper-campaign":
        if not args.execute:
            print("paper campaign validated no action; pass --execute to submit paper orders")
            return
        try:
            if (
                args.start_discount < 0
                or args.end_discount > Decimal("0.5")
                or args.start_discount > args.end_discount
                or args.start_discount % Decimal("0.1") != 0
                or args.end_discount % Decimal("0.1") != 0
            ):
                raise ValueError(
                    "paper campaign discounts must be 0.1% steps between 0% and 0.5%"
                )
            settings = load_settings()
            campaign = PaperBrokerCampaign(
                settings=settings,
                credentials=load_kis_credentials(settings),
                policies=load_policy_registry(
                    Path("config/trading_policies.paper.json")
                ),
                output=args.output,
                monitor_seconds=args.monitor_seconds,
            )
            results = asyncio.run(
                campaign.run(
                    symbols=("000660", "005930"),
                    discounts=tuple(
                        Decimal(index) / Decimal("10")
                        for index in range(
                            int(args.start_discount * 10),
                            int(args.end_discount * 10) + 1,
                        )
                    ),
                )
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
            PermissionError,
            KisApiError,
        ) as exc:
            print(f"paper campaign stopped safely: {exc}", file=sys.stderr)
            raise SystemExit(14) from None
        print(
            f"paper campaign completed: {len(results)} steps, "
            f"{sum(item.status == 'ROUND_TRIP_FILLED' for item in results)} fills"
        )
        return
    if args.command == "paper-trade":
        try:
            settings = load_settings()
            mandate = parse_entry_mandate(args.mandate.read_text(encoding="utf-8"))
            policies = load_policy_registry(args.policies)
            credentials = load_kis_credentials(settings)
            if not args.execute:
                print(
                    "paper runtime inputs are valid; no order was sent. "
                    "Pass --execute to start the long-running runtime."
                )
                return
            application = PaperTradingApplication(
                settings=settings,
                credentials=credentials,
                mandate=mandate,
                policies=policies,
            )
            asyncio.run(application.run())
        except (OSError, ValueError, ValidationError, PermissionError, KisApiError) as exc:
            print(f"paper runtime refused to start: {exc}", file=sys.stderr)
            raise SystemExit(10) from None
        return
    if args.command == "doctor":
        try:
            raise SystemExit(asyncio.run(_doctor(args.live, args.symbol)))
        except (ValueError, ValidationError):
            print(
                "KIS 설정을 검증하지 못했습니다. "
                ".secrets/kis/paper.json의 빈 값을 확인하세요.",
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
                            report.candidates,
                            key=lambda candidate: (
                                candidate.windows["14"].rank or 999
                            ),
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
            f"context-reviewed report built: {target} "
            f"({len(review.candidates)} candidates)",
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
        print(
            f"real KOSPI report built: {target} "
            f"(data as of {report.data_as_of.isoformat()})"
        )
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
                                "average_trading_value": str(
                                    item.average_trading_value
                                ),
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
