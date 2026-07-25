from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from danta.adapters.kis.client import KisApiError
from danta.config import load_kis_credentials, load_settings
from danta.dashboard.builder import build_dashboard, load_dashboard_report
from danta.dashboard.demo import demo_report
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


if __name__ == "__main__":
    main()
