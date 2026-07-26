from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from danta.adapters.kis.client import KisApiError, KisClient
from danta.adapters.krx.client import KrxDataError, PykrxMarketDataClient
from danta.config import (
    load_kis_credentials,
    load_krx_environment,
    load_settings,
    load_smtp_config,
)
from danta.dashboard.builder import build_dashboard, load_dashboard_report
from danta.dashboard.demo import demo_report
from danta.dashboard.models import DashboardReport
from danta.services.candidate_report import CandidateReportError, build_quant_report
from danta.services.candidate_validation import (
    CandidateValidationError,
    validate_candidate_quotes,
)
from danta.services.intraday_report import (
    MinuteBarStore,
    backfill_minute_bars,
    balanced_prefilter,
    build_intraday_report,
    screening_pool,
    screening_pool_audit,
)
from danta.services.notifier import NotificationError, SmtpNotifier
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
        help="minute coverage target; 14/21 use the 50-symbol audit pool",
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
            prefiltered = balanced_prefilter(dataset)
            if len(prefiltered) < 30:
                raise CandidateReportError(
                    f"balanced prefilter returned only {len(prefiltered)} symbols"
                )
            snapshot = Path("data/prefilter-balanced-v1.json")
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot_temporary = snapshot.with_suffix(".tmp")
            snapshot_temporary.write_text(
                json.dumps(
                    {
                        "version": "prefilter-balanced-v1",
                        "data_as_of": dataset.trading_dates[-1].isoformat(),
                        "count": len(prefiltered),
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
                            for item in prefiltered
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            snapshot_temporary.replace(snapshot)
            collection_candidates = prefiltered
            if args.window_days > 7:
                collection_candidates = screening_pool(
                    prefiltered,
                    MinuteBarStore(args.data_root),
                    dataset.trading_dates,
                    limit=50,
                )
                audit_snapshot = Path("data/filter-audit-pool-v1.json")
                audit_temporary = audit_snapshot.with_suffix(".tmp")
                audit_entries = screening_pool_audit(
                    prefiltered,
                    MinuteBarStore(args.data_root),
                    dataset.trading_dates,
                    limit=50,
                )
                audit_temporary.write_text(
                    json.dumps(
                        {
                            "version": "filter-audit-pool-v1",
                            "data_as_of": dataset.trading_dates[-1].isoformat(),
                            "window_days": args.window_days,
                            "count": len(collection_candidates),
                            "candidates": [
                                {
                                    **asdict(item),
                                    "score": str(item.score),
                                    "position_pct": str(item.position_pct),
                                    "lower_trend_pct": str(item.lower_trend_pct),
                                }
                                for item in audit_entries
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                audit_temporary.replace(audit_snapshot)
            print(
                f"collection universe: {len(collection_candidates)} symbols; "
                f"starting resumable {args.window_days}-day minute backfill",
                flush=True,
            )
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
            report = build_intraday_report(
                dataset,
                prefiltered,
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
