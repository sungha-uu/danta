from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from danta.adapters.dart.financials import (
    DartFinancialDataError,
    OpenDartFinancialClient,
)
from danta.dashboard.models import DashboardReport
from danta.domain.fundamentals import (
    FundamentalSnapshot,
    FundamentalSnapshotBatch,
    financial_risk_flags,
    health_status_for,
    safe_ratio,
)

REPORT_NAMES = {
    "11011": "사업보고서",
    "11012": "반기보고서",
    "11013": "1분기보고서",
    "11014": "3분기보고서",
}
ACCOUNT_ALIASES = {
    "total_assets": {"자산총계"},
    "current_assets": {"유동자산"},
    "total_liabilities": {"부채총계"},
    "current_liabilities": {"유동부채"},
    "total_equity": {"자본총계"},
    "revenue": {"매출액", "영업수익", "수익(매출액)", "보험영업수익"},
    "operating_income": {"영업이익", "영업이익(손실)"},
    "net_income": {
        "당기순이익",
        "당기순이익(손실)",
        "연결당기순이익",
        "분기순이익",
        "반기순이익",
    },
}
INCOME_FIELDS = {"revenue", "operating_income", "net_income"}


def report_candidates_for(as_of: date) -> tuple[tuple[int, str], ...]:
    """Return newest expected report first, then safe filing fallbacks."""
    if (as_of.month, as_of.day) >= (11, 15):
        primary = (as_of.year, "11014")
    elif (as_of.month, as_of.day) >= (8, 15):
        primary = (as_of.year, "11012")
    elif (as_of.month, as_of.day) >= (5, 16):
        primary = (as_of.year, "11013")
    elif (as_of.month, as_of.day) >= (4, 1):
        primary = (as_of.year - 1, "11011")
    else:
        primary = (as_of.year - 1, "11014")
    fallbacks = (
        (as_of.year, "11012"),
        (as_of.year, "11013"),
        (as_of.year - 1, "11011"),
        (as_of.year - 1, "11014"),
    )
    return tuple(dict.fromkeys((primary, *fallbacks)))


def _amount(value: object) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text == "-":
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _canonical_account(name: str) -> str | None:
    normalized = name.replace(" ", "").strip()
    for canonical, aliases in ACCOUNT_ALIASES.items():
        if normalized in {alias.replace(" ", "") for alias in aliases}:
            return canonical
    return None


def _statement_values(rows: Sequence[dict[str, Any]]) -> tuple[str, dict[str, Decimal]]:
    by_statement: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for row in rows:
        statement_type = str(row.get("fs_div", "")).strip()
        if statement_type not in {"CFS", "OFS"}:
            continue
        canonical = _canonical_account(str(row.get("account_nm", "")))
        if canonical is None:
            continue
        amount_key = (
            "thstrm_add_amount"
            if canonical in INCOME_FIELDS
            and str(row.get("thstrm_add_amount", "")).strip()
            else "thstrm_amount"
        )
        value = _amount(row.get(amount_key))
        if value is not None and canonical not in by_statement[statement_type]:
            by_statement[statement_type][canonical] = value
    if not by_statement:
        return "CFS", {}
    statement_type = max(
        by_statement,
        key=lambda item: (len(by_statement[item]), item == "CFS"),
    )
    return statement_type, by_statement[statement_type]


def _snapshot_from_rows(
    *,
    symbol: str,
    name: str,
    corp_code: str,
    business_year: int,
    report_code: str,
    rows: Sequence[dict[str, Any]],
    fetched_at: datetime,
    as_of_date: date,
) -> FundamentalSnapshot:
    statement_type, values = _statement_values(rows)
    total_assets = values.get("total_assets")
    current_assets = values.get("current_assets")
    total_liabilities = values.get("total_liabilities")
    current_liabilities = values.get("current_liabilities")
    total_equity = values.get("total_equity")
    revenue = values.get("revenue")
    operating_income = values.get("operating_income")
    net_income = values.get("net_income")
    debt_ratio = safe_ratio(total_liabilities, total_equity)
    current_ratio = safe_ratio(current_assets, current_liabilities)
    operating_margin = safe_ratio(operating_income, revenue)
    net_margin = safe_ratio(net_income, revenue)
    flags = financial_risk_flags(
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        total_equity=total_equity,
        revenue=revenue,
        operating_income=operating_income,
        net_income=net_income,
        debt_ratio_pct=debt_ratio,
        current_ratio_pct=current_ratio,
    )
    receipt = next(
        (
            str(row.get("rcept_no", "")).strip()
            for row in rows
            if str(row.get("rcept_no", "")).strip()
        ),
        None,
    )
    currency = next(
        (
            str(row.get("currency", "")).strip()
            for row in rows
            if str(row.get("currency", "")).strip()
        ),
        "KRW",
    )
    return FundamentalSnapshot(
        symbol=symbol,
        name=name,
        corp_code=corp_code,
        as_of_date=as_of_date,
        fetched_at=fetched_at,
        business_year=business_year,
        report_code=report_code,  # type: ignore[arg-type]
        report_name=REPORT_NAMES[report_code],
        receipt_no=receipt,
        statement_type=statement_type,  # type: ignore[arg-type]
        currency=currency,
        total_assets=total_assets,
        current_assets=current_assets,
        total_liabilities=total_liabilities,
        current_liabilities=current_liabilities,
        total_equity=total_equity,
        revenue=revenue,
        operating_income=operating_income,
        net_income=net_income,
        debt_ratio_pct=debt_ratio,
        current_ratio_pct=current_ratio,
        operating_margin_pct=operating_margin,
        net_margin_pct=net_margin,
        risk_flags=flags,
        health_status=health_status_for(flags),
    )


async def refresh_fundamental_snapshots(
    client: OpenDartFinancialClient,
    universe: Sequence[tuple[str, str]],
    *,
    output_path: Path,
    as_of: date,
    progress: Callable[[str], None] | None = None,
) -> FundamentalSnapshotBatch:
    emit = progress if progress is not None else lambda _message: None
    requested = tuple(dict.fromkeys(symbol for symbol, _name in universe))
    names = {symbol: name for symbol, name in universe}
    candidates = report_candidates_for(as_of)
    target_year, target_code = candidates[0]
    existing = load_fundamental_batch(output_path)
    existing_map = existing.by_symbol() if existing is not None else {}
    complete = {
        symbol: snapshot
        for symbol, snapshot in existing_map.items()
        if symbol in requested
        and snapshot.business_year == target_year
        and snapshot.report_code == target_code
    }
    if len(complete) == len(requested):
        return existing  # type: ignore[return-value]

    corp_codes = await client.load_corp_code_map()
    snapshots = dict(complete)
    unavailable = {
        symbol for symbol in requested if symbol not in corp_codes
    }
    unresolved = set(requested) - set(snapshots) - unavailable
    provider_errors: list[str] = []
    fetched_at = datetime.now(UTC)
    for business_year, report_code in candidates:
        if not unresolved:
            break
        requested_corp_codes = [corp_codes[symbol] for symbol in sorted(unresolved)]
        emit(
            f"collecting DART financials {business_year}/{report_code} "
            f"for {len(requested_corp_codes)} symbols"
        )
        try:
            rows = await client.fetch_major_accounts(
                requested_corp_codes,
                business_year=business_year,
                report_code=report_code,
            )
        except (DartFinancialDataError, httpx.HTTPError) as exc:
            provider_errors.append(
                f"{business_year}/{report_code}: {type(exc).__name__}: {exc}"
            )
            continue
        rows_by_corp: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            corp_code = str(row.get("corp_code", "")).strip()
            if corp_code:
                rows_by_corp[corp_code].append(row)
        resolved_now: set[str] = set()
        for symbol in sorted(unresolved):
            corp_code = corp_codes[symbol]
            symbol_rows = rows_by_corp.get(corp_code, [])
            if not symbol_rows:
                continue
            snapshots[symbol] = _snapshot_from_rows(
                symbol=symbol,
                name=names[symbol],
                corp_code=corp_code,
                business_year=business_year,
                report_code=report_code,
                rows=symbol_rows,
                fetched_at=fetched_at,
                as_of_date=as_of,
            )
            resolved_now.add(symbol)
        unresolved -= resolved_now
    for symbol in tuple(unresolved):
        if symbol in existing_map:
            snapshots[symbol] = existing_map[symbol]
            unresolved.remove(symbol)
    unavailable.update(unresolved)
    batch = FundamentalSnapshotBatch(
        generated_at=fetched_at,
        target_business_year=target_year,
        target_report_code=target_code,  # type: ignore[arg-type]
        requested_symbols=requested,
        snapshots=tuple(
            snapshots[symbol] for symbol in requested if symbol in snapshots
        ),
        unavailable_symbols=tuple(sorted(unavailable)),
        provider_errors=tuple(provider_errors),
    )
    _write_batch(output_path, batch)
    return batch


def attach_fundamentals(
    report: DashboardReport,
    batch: FundamentalSnapshotBatch,
) -> DashboardReport:
    snapshots = batch.by_symbol()
    return report.model_copy(
        update={
            "candidates": [
                candidate.model_copy(
                    update={"fundamentals": snapshots.get(candidate.code)}
                )
                for candidate in report.candidates
            ],
            "extended_watchlist": [
                candidate.model_copy(
                    update={"fundamentals": snapshots.get(candidate.code)}
                )
                for candidate in report.extended_watchlist
            ],
        }
    )


def load_fundamental_batch(path: Path) -> FundamentalSnapshotBatch | None:
    if not path.exists():
        return None
    try:
        return FundamentalSnapshotBatch.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None


def _write_batch(path: Path, batch: FundamentalSnapshotBatch) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(batch.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
