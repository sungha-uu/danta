from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from pydantic import HttpUrl

from danta.dashboard.models import (
    AiGrade,
    CandidateView,
    DashboardReport,
    FlowBreakdown,
    NewsItem,
    WindowMetrics,
)

KST = timezone(timedelta(hours=9))

STOCKS = [
    ("005930", "삼성전자", "전기전자", 259000),
    ("000660", "SK하이닉스", "전기전자", 1836000),
    ("005380", "현대차", "운수장비", 425000),
    ("012450", "한화에어로스페이스", "운수장비", 956000),
    ("034020", "두산에너빌리티", "기계", 66800),
    ("006400", "삼성SDI", "전기전자", 401000),
    ("035420", "NAVER", "서비스업", 264000),
    ("051910", "LG화학", "화학", 426000),
    ("105560", "KB금융", "금융업", 129000),
    ("055550", "신한지주", "금융업", 82400),
    ("068270", "셀트리온", "의약품", 218500),
    ("028260", "삼성물산", "유통업", 231000),
    ("086790", "하나금융지주", "금융업", 97800),
    ("015760", "한국전력", "전기가스", 36350),
    ("032830", "삼성생명", "보험", 168000),
    ("009150", "삼성전기", "전기전자", 224000),
    ("010140", "삼성중공업", "운수장비", 28600),
    ("096770", "SK이노베이션", "화학", 142000),
    ("017670", "SK텔레콤", "통신업", 70800),
    ("003550", "LG", "서비스업", 99600),
    ("018260", "삼성에스디에스", "서비스업", 191000),
    ("066570", "LG전자", "전기전자", 118000),
    ("000270", "기아", "운수장비", 136500),
    ("033780", "KT&G", "제조업", 142000),
    ("010130", "고려아연", "철강금속", 1320000),
    ("024110", "기업은행", "은행", 21400),
    ("047050", "포스코인터내셔널", "유통업", 64900),
    ("011200", "HMM", "운수창고", 27800),
    ("090430", "아모레퍼시픽", "화학", 154000),
    ("316140", "우리금융지주", "금융업", 26700),
]


def _decimal(value: float) -> Decimal:
    return Decimal(f"{value:.2f}")


def _grade(rank: int) -> AiGrade:
    if rank <= 6:
        return "STRONG_RECOMMEND"
    if rank <= 14:
        return "RECOMMEND"
    if rank <= 23:
        return "NOT_RECOMMEND"
    return "STRONG_NOT_RECOMMEND"


def _series(base: int, index: int) -> list[Decimal]:
    amplitude = 0.035 + (index % 6) * 0.007
    drift = ((index % 5) - 2) * 0.0009
    values: list[Decimal] = []
    for day in range(21):
        wave = math.sin((day + index * 0.7) * 0.82) * amplitude
        micro = math.sin((day + index) * 2.1) * 0.006
        values.append(_decimal(base * (1 + wave + micro + drift * day)))
    return values


def _traversal_count(closes: list[Decimal], low: Decimal, high: Decimal) -> int:
    """Count alternating lower/upper zone contacts inside one price window."""
    width = high - low
    lower_ceiling = low + width * Decimal("0.20")
    upper_floor = high - width * Decimal("0.20")
    previous_zone: Literal["LOW", "HIGH"] | None = None
    traversals = 0
    for close in closes:
        zone: Literal["LOW", "HIGH"] | None = None
        if close <= lower_ceiling:
            zone = "LOW"
        elif close >= upper_floor:
            zone = "HIGH"
        if zone is None:
            continue
        if previous_zone is not None and zone != previous_zone:
            traversals += 1
        previous_zone = zone
    return traversals // 2


def _window(
    all_closes: list[Decimal],
    days: Literal[7, 14, 21],
    rank: int,
) -> WindowMetrics:
    closes = all_closes[-days:]
    low = min(closes)
    high = max(closes)
    center = (low + high) / Decimal("2")
    amplitude = (high - low) / center * Decimal("100")
    position = (closes[-1] - low) / (high - low) * Decimal("100")
    period_return = (closes[-1] / closes[0] - 1) * Decimal("100")
    scale = Decimal(days) / Decimal("7")
    foreign = Decimal(34 - rank) * scale
    institution = Decimal(23 - rank) * scale
    financial = Decimal((rank % 7) - 2) * scale
    pension = Decimal((rank % 5) - 1) * scale
    retail = -(foreign + institution) * Decimal("0.72")
    return WindowMetrics(
        days=days,
        rank=rank,
        box_low=low,
        box_high=high,
        amplitude_pct=amplitude.quantize(Decimal("0.01")),
        position_pct=position.quantize(Decimal("0.01")),
        return_pct=period_return.quantize(Decimal("0.01")),
        average_trading_value_billion=Decimal(38 + rank * 4 + days).quantize(Decimal("0.1")),
        volume_ratio=Decimal("1.85") - Decimal(rank) * Decimal("0.02"),
        traversal_count=_traversal_count(closes, low, high),
        breakdown_risk_pct=min(Decimal(8 + rank * 2), Decimal("78")),
        quant_score=Decimal(96 - rank * 1.25),
        ai_score=Decimal(94 - rank * 1.1 if rank <= 10 else 60),
        final_score=Decimal(95 - rank * 1.18),
        ai_grade=_grade(rank),
        ai_comment=(
            f"{days}일 반복 왕복과 외국인·기관 순유입이 함께 확인됩니다. "
            f"{days}일 박스 하단 재접근 시 거래대금 유지 여부를 우선 확인합니다."
            if rank <= 14
            else (
                f"{days}일 정량 조건은 통과했지만 "
                "수급 연속성 또는 박스 안정성 확인이 더 필요합니다."
            )
        ),
        reasons=[f"{days}일 왕복 구조", "거래대금 유지", "스마트머니 순유입"],
        risks=["시장 급락 동조", f"{days}일 박스 하단 거래량 동반 이탈"],
        invalidation=f"{days}일 하단 이탈 후 2개 봉 내 재진입 실패",
        closes=closes,
        flows=FlowBreakdown(
            retail=retail.quantize(Decimal("0.1")),
            foreign=foreign.quantize(Decimal("0.1")),
            institution=institution.quantize(Decimal("0.1")),
            financial_investment=financial.quantize(Decimal("0.1")),
            pension=pension.quantize(Decimal("0.1")),
            strength_pct=Decimal(3.8 - rank * 0.11).quantize(Decimal("0.01")),
        ),
    )


def demo_report() -> DashboardReport:
    now = datetime.now(KST).replace(microsecond=0)
    candidates: list[CandidateView] = []
    for index, (code, name, sector, base) in enumerate(STOCKS):
        rank = index + 1
        closes = _series(base, index)
        current = closes[-1]
        naver_url = HttpUrl(f"https://finance.naver.com/item/main.naver?code={code}")
        candidates.append(
            CandidateView(
                code=code,
                name=name,
                sector=sector,
                current_price=current,
                windows={
                    "7": _window(closes, 7, rank),
                    "14": _window(closes, 14, rank),
                    "21": _window(closes, 21, rank),
                },
                news=[
                    NewsItem(
                        title=f"[데모] {name} 최신 뉴스 수집기 연결 전 예시",
                        source="Danta Demo",
                        published_at=now - timedelta(hours=rank),
                        url=naver_url,
                        sentiment="NEUTRAL",
                    )
                ],
                discussion_summary=(
                    "데모 요약입니다. 실제 운영에서는 공개 게시물의 주장·소문을 "
                    "사실로 간주하지 않고 참고 신호로만 표시합니다."
                ),
                naver_url=naver_url,
            )
        )
    return DashboardReport(
        generated_at=now,
        data_as_of=now - timedelta(minutes=5),
        market_regime="DEMO · 중립",
        calculation_version="box-v0-demo",
        model_id="demo-model",
        prompt_version="candidate-review-v0-demo",
        is_demo=True,
        candidates=candidates,
    )
