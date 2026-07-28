from __future__ import annotations

import asyncio
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import HttpUrl

from danta.dashboard.models import DashboardReport, WindowMetrics
from danta.services.ai_review import (
    AiCandidateReview,
    AiNewsReview,
    AiReviewBatch,
    AiWindowReview,
)

POSITIVE_WORDS = (
    "호실적",
    "상향",
    "수주",
    "계약",
    "흑자",
    "성장",
    "회복",
    "증가",
    "강세",
    "급등",
    "반등",
)
NEGATIVE_WORDS = (
    "적자",
    "하향",
    "소송",
    "제재",
    "감소",
    "부진",
    "악화",
    "급락",
    "하락",
    "신저가",
    "손실",
)
BULLISH_DISCUSSION_WORDS = ("상승", "반등", "급등", "상한가", "매수", "호재")
BEARISH_DISCUSSION_WORDS = ("하락", "급락", "폭락", "손절", "매도", "악재")
TOPIC_WORDS = (
    "외인",
    "외국인",
    "기관",
    "실적",
    "반도체",
    "전쟁",
    "공매도",
    "배당",
    "수주",
)


@dataclass(frozen=True, slots=True)
class CollectedNews:
    title: str
    source: str
    published_at: str
    url: str
    sentiment: str


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    code: str
    name: str
    fetched_at: str
    news_status: str
    disclosure_status: str
    discussion_status: str
    news: tuple[CollectedNews, ...]
    discussion_titles: tuple[str, ...]
    discussion_url: str


class _NaverBoardParser(HTMLParser):
    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.titles: list[str] = []
        self._in_title_cell = False
        self._in_link = False
        self._parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "td" and "title" in classes:
            self._in_title_cell = True
        elif tag == "a" and self._in_title_cell and len(self.titles) < self.limit:
            self._in_link = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            title = _clean_text("".join(self._parts))
            if title:
                self.titles.append(title[:160])
            self._in_link = False
            self._parts = []
        elif tag == "td" and self._in_title_cell:
            self._in_title_cell = False


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _sentiment(text: str) -> str:
    positive = sum(word in text for word in POSITIVE_WORDS)
    negative = sum(word in text for word in NEGATIVE_WORDS)
    if positive > negative:
        return "POSITIVE"
    if negative > positive:
        return "NEGATIVE"
    return "NEUTRAL"


def _published_at(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.isoformat()
    except (TypeError, ValueError):
        return datetime.now(UTC).isoformat()


class PublicContextCollector:
    def __init__(
        self,
        cache_root: Path,
        *,
        dart_api_key: str | None = None,
        concurrency: int = 5,
        timeout_seconds: float = 12,
    ) -> None:
        self.cache_root = cache_root
        self.dart_api_key = dart_api_key
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds

    async def collect(
        self,
        candidates: list[tuple[str, str]],
        *,
        refresh: bool = False,
    ) -> dict[str, ContextSnapshot]:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        semaphore = asyncio.Semaphore(self.concurrency)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/128 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9",
        }
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=self.timeout_seconds,
        ) as client:
            corp_codes = await self._load_dart_corp_codes(client)
            results = await asyncio.gather(
                *(
                    self._collect_one(
                        client,
                        semaphore,
                        code,
                        name,
                        corp_codes.get(code),
                        refresh=refresh,
                    )
                    for code, name in candidates
                )
            )
        return {item.code: item for item in results}

    async def _collect_one(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        code: str,
        name: str,
        corp_code: str | None,
        *,
        refresh: bool,
    ) -> ContextSnapshot:
        cache_path = self.cache_root / f"{code}.json"
        if cache_path.exists() and not refresh:
            try:
                body = json.loads(cache_path.read_text(encoding="utf-8"))
                return ContextSnapshot(
                    **{
                        **body,
                        "news": tuple(CollectedNews(**item) for item in body["news"]),
                        "discussion_titles": tuple(body["discussion_titles"]),
                        "disclosure_status": body.get(
                            "disclosure_status",
                            "NOT_CONFIGURED",
                        ),
                    }
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        async with semaphore:
            news_result, disclosure_result, discussion_result = await asyncio.gather(
                self._fetch_news(client, name),
                self._fetch_disclosures(client, corp_code),
                self._fetch_discussion(client, code),
                return_exceptions=True,
            )
        if isinstance(news_result, BaseException):
            news_status = "FAILED"
            news: tuple[CollectedNews, ...] = ()
        else:
            news_status = "READY"
            news = news_result
        if isinstance(disclosure_result, BaseException):
            disclosure_status = "FAILED"
            disclosures: tuple[CollectedNews, ...] = ()
        else:
            disclosure_status = (
                "READY" if self.dart_api_key and corp_code else "NOT_CONFIGURED"
            )
            disclosures = disclosure_result
        combined_news = tuple(
            sorted(
                [*news, *disclosures],
                key=lambda item: item.published_at,
                reverse=True,
            )[:5]
        )
        if isinstance(discussion_result, BaseException):
            discussion_status = "FAILED"
            titles: tuple[str, ...] = ()
        else:
            discussion_status = "READY"
            titles = discussion_result
        snapshot = ContextSnapshot(
            code=code,
            name=name,
            fetched_at=datetime.now(UTC).isoformat(),
            news_status=news_status,
            disclosure_status=disclosure_status,
            discussion_status=discussion_status,
            news=combined_news,
            discussion_titles=titles,
            discussion_url=f"https://finance.naver.com/item/board.naver?code={code}",
        )
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(snapshot), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(cache_path)
        return snapshot

    async def _load_dart_corp_codes(
        self,
        client: httpx.AsyncClient,
    ) -> dict[str, str]:
        if not self.dart_api_key:
            return {}
        cache_path = self.cache_root / "dart-corp-codes.json"
        if cache_path.exists():
            try:
                body = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(body, dict) and body:
                    return {str(key): str(value) for key, value in body.items()}
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        response = await client.get(
            "https://opendart.fss.or.kr/api/corpCode.xml",
            params={"crtfc_key": self.dart_api_key},
        )
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            xml_name = next(
                name for name in archive.namelist() if name.lower().endswith(".xml")
            )
            root = ET.fromstring(archive.read(xml_name))
        mapping = {
            (item.findtext("stock_code") or "").strip(): (
                item.findtext("corp_code") or ""
            ).strip()
            for item in root.findall("list")
            if (item.findtext("stock_code") or "").strip()
        }
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(mapping, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(cache_path)
        return mapping

    async def _fetch_disclosures(
        self,
        client: httpx.AsyncClient,
        corp_code: str | None,
    ) -> tuple[CollectedNews, ...]:
        if not self.dart_api_key or not corp_code:
            return ()
        end = datetime.now(UTC).date()
        start = end - timedelta(days=30)
        response = await client.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={
                "crtfc_key": self.dart_api_key,
                "corp_code": corp_code,
                "bgn_de": start.strftime("%Y%m%d"),
                "end_de": end.strftime("%Y%m%d"),
                "sort": "date",
                "sort_mth": "desc",
                "page_count": "5",
            },
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") == "013":
            return ()
        if body.get("status") != "000":
            raise ValueError(
                f"DART disclosure request failed: {body.get('status')} "
                f"{body.get('message')}"
            )
        result: list[CollectedNews] = []
        for item in body.get("list", [])[:5]:
            receipt = str(item.get("rcept_no", "")).strip()
            title = _clean_text(str(item.get("report_nm", "")))
            filed = str(item.get("rcept_dt", "")).strip()
            if not receipt or not title:
                continue
            published = datetime.strptime(filed, "%Y%m%d").replace(
                tzinfo=UTC
            ).isoformat()
            result.append(
                CollectedNews(
                    title=f"[공시] {title}"[:240],
                    source="Open DART",
                    published_at=published,
                    url=(
                        "https://dart.fss.or.kr/dsaf001/main.do"
                        f"?rcpNo={receipt}"
                    ),
                    sentiment=_sentiment(title),
                )
            )
        return tuple(result)

    async def _fetch_news(
        self,
        client: httpx.AsyncClient,
        name: str,
    ) -> tuple[CollectedNews, ...]:
        query = quote(f'"{name}" 주가 when:7d')
        url = (
            "https://news.google.com/rss/search"
            f"?q={query}&hl=ko&gl=KR&ceid=KR:ko"
        )
        response = await client.get(url)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        articles: list[CollectedNews] = []
        seen: set[str] = set()
        for item in root.findall("./channel/item"):
            raw_title = _clean_text(item.findtext("title") or "")
            source = _clean_text(item.findtext("source") or "Google News")
            title = re.sub(rf"\s*-\s*{re.escape(source)}$", "", raw_title).strip()
            link = _clean_text(item.findtext("link") or "")
            if not title or not link or title in seen:
                continue
            seen.add(title)
            articles.append(
                CollectedNews(
                    title=title[:240],
                    source=source[:80],
                    published_at=_published_at(item.findtext("pubDate") or ""),
                    url=link,
                    sentiment=_sentiment(title),
                )
            )
            if len(articles) >= 5:
                break
        return tuple(articles)

    async def _fetch_discussion(
        self,
        client: httpx.AsyncClient,
        code: str,
    ) -> tuple[str, ...]:
        url = f"https://finance.naver.com/item/board.naver?code={code}"
        response = await client.get(url)
        response.raise_for_status()
        parser = _NaverBoardParser(limit=10)
        # Naver Finance currently declares and serves the board as UTF-8.
        # Decode explicitly instead of relying on a historical EUC-KR assumption.
        parser.feed(response.content.decode("utf-8", errors="replace"))
        return tuple(parser.titles)


def _discussion_summary(snapshot: ContextSnapshot) -> str:
    titles = snapshot.discussion_titles
    if snapshot.discussion_status == "FAILED":
        return "종목토론 수집 실패 · 비신뢰 참고 신호 미반영"
    if not titles:
        return "최근 종목토론 제목 없음 · 비신뢰 참고 신호"
    bullish = sum(any(word in title for word in BULLISH_DISCUSSION_WORDS) for title in titles)
    bearish = sum(any(word in title for word in BEARISH_DISCUSSION_WORDS) for title in titles)
    neutral = len(titles) - bullish - bearish
    topics = [
        word
        for word in TOPIC_WORDS
        if sum(word in title for title in titles) >= 2
    ][:3]
    topic_text = f" · 반복 화제: {', '.join(topics)}" if topics else ""
    return (
        f"최근 제목 {len(titles)}건: 상승 기대 {bullish} · "
        f"하락 우려 {bearish} · 기타 {neutral}{topic_text} · 비신뢰 참고용"
    )


def _context_status(
    snapshot: ContextSnapshot,
) -> Literal["READY", "PARTIAL", "FAILED"]:
    statuses = {
        snapshot.news_status,
        snapshot.disclosure_status,
        snapshot.discussion_status,
    }
    statuses.discard("NOT_CONFIGURED")
    if statuses == {"READY"}:
        return "READY"
    if statuses == {"FAILED"}:
        return "FAILED"
    return "PARTIAL"


def _grade(score: Decimal) -> str:
    if score >= Decimal("75"):
        return "STRONG_RECOMMEND"
    if score >= Decimal("60"):
        return "RECOMMEND"
    if score >= Decimal("45"):
        return "NOT_RECOMMEND"
    return "STRONG_NOT_RECOMMEND"


def _qualified_grade(
    score: Decimal,
    *,
    candidate_price: Decimal,
    metrics: WindowMetrics,
    expired_spike_reversion: bool,
) -> str:
    current_10pct_threshold = (
        Decimal("1") / Decimal("1.10") - Decimal("1")
    ) * Decimal("100")
    qualified = (
        (metrics.target_reach_count or 0) >= 1
        and metrics.position_pct is not None
        and metrics.position_pct <= Decimal("35")
        and metrics.current_vs_window_high_pct is not None
        and metrics.current_vs_window_high_pct <= current_10pct_threshold
        and metrics.target_price_10pct is not None
        and metrics.target_price_10pct > candidate_price
        and not expired_spike_reversion
    )
    if qualified:
        return _grade(score)
    return "NOT_RECOMMEND" if score >= Decimal("45") else "STRONG_NOT_RECOMMEND"


EXPIRED_SPIKE_RISK = "21일 원시세 복귀형 급등 소멸"
FINANCIAL_RISK_LABELS = {
    "REQUIRED_ACCOUNTS_MISSING": "필수 재무계정 결측",
    "NEGATIVE_EQUITY": "자본잠식 또는 음의 자본",
    "OPERATING_LOSS": "최근 누적 영업손실",
    "NET_LOSS": "최근 누적 순손실",
    "HIGH_DEBT_RATIO": "부채비율 300% 이상",
    "LOW_CURRENT_RATIO": "유동비율 70% 미만",
}


def _percentile(values: list[Decimal], ratio: Decimal) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return Decimal("0")
    index = int((Decimal(len(ordered) - 1) * ratio).to_integral_value())
    return ordered[index]


def _is_expired_spike_reversion(metrics: WindowMetrics) -> bool:
    if (
        metrics.days != 21
        or metrics.structure_status != "READY"
        or len(metrics.chart_bars) < 21
    ):
        return False
    bars = metrics.chart_bars
    first_close = bars[0].close
    peak_index = max(range(len(bars)), key=lambda index: bars[index].high)
    peak_excursion = (bars[peak_index].high / first_close - Decimal("1")) * Decimal(
        "100"
    )
    dates = sorted({bar.trading_date for bar in bars})
    third_size = max(1, len(dates) // 3)
    first_dates = set(dates[:third_size])
    last_dates = set(dates[-third_size:])
    first_upper = _percentile(
        [bar.high for bar in bars if bar.trading_date in first_dates],
        Decimal("0.80"),
    )
    last_upper = _percentile(
        [bar.high for bar in bars if bar.trading_date in last_dates],
        Decimal("0.80"),
    )
    return (
        abs(metrics.return_pct) <= Decimal("15")
        and peak_excursion >= Decimal("25")
        and Decimal(peak_index) / Decimal(len(bars) - 1) <= Decimal("0.50")
        and last_upper <= first_upper * Decimal("0.90")
    )


def _financial_context(candidate: object) -> tuple[Decimal, str, list[str]]:
    snapshot = getattr(candidate, "fundamentals", None)
    if snapshot is None:
        return Decimal("0"), "재무 스냅샷 없음", ["재무 스냅샷 미수집"]
    flags = list(snapshot.risk_flags)
    penalty = Decimal("0")
    if "NEGATIVE_EQUITY" in flags:
        penalty -= Decimal("20")
    if "OPERATING_LOSS" in flags:
        penalty -= Decimal("8")
    if "NET_LOSS" in flags:
        penalty -= Decimal("4")
    if "HIGH_DEBT_RATIO" in flags:
        penalty -= Decimal("4")
    if "LOW_CURRENT_RATIO" in flags:
        penalty -= Decimal("3")
    penalty = max(Decimal("-25"), penalty)
    report_text = (
        f"{snapshot.business_year}년 {snapshot.report_name} "
        f"{snapshot.statement_type}"
    )
    if not flags:
        return penalty, f"{report_text} 기준 치명 재무 플래그 없음", []
    labels = [FINANCIAL_RISK_LABELS.get(flag, flag) for flag in flags]
    return penalty, f"{report_text} · {', '.join(labels)}", labels


def build_context_review(
    report: DashboardReport,
    snapshots: dict[str, ContextSnapshot],
    *,
    reviewed_at: datetime,
) -> AiReviewBatch:
    reviews: list[AiCandidateReview] = []
    review_targets = sorted(
        report.candidates,
        key=lambda candidate: candidate.windows["14"].rank or 999,
    )
    for candidate in review_targets:
        snapshot = snapshots[candidate.code]
        financial_adjustment, financial_text, financial_risks = _financial_context(
            candidate
        )
        expired_spike_reversion = _is_expired_spike_reversion(
            candidate.windows["21"]
        )
        positive_news = sum(item.sentiment == "POSITIVE" for item in snapshot.news)
        negative_news = sum(item.sentiment == "NEGATIVE" for item in snapshot.news)
        news_adjustment = Decimal(
            max(-6, min(6, (positive_news - negative_news) * 3))
        )
        window_reviews: dict[str, AiWindowReview] = {}
        for key, metrics in candidate.windows.items():
            if metrics.structure_status != "READY" or metrics.quant_score is None:
                window_reviews[key] = AiWindowReview(
                    ai_grade="NOT_RECOMMEND",
                    ai_score=0,
                    ai_comment=(
                        f"{metrics.days}일 분봉 구조가 "
                        f"{metrics.structure_completed_days}/{metrics.days}거래일로 "
                        "미완료되어 에이전트 등급을 보류합니다."
                    ),
                    reasons=["뉴스·토론 컨텍스트만 수집됨"],
                    risks=["분봉 구조 미완료"],
                )
                continue
            flow_strength = metrics.flows.strength_pct
            flow_adjustment = (
                Decimal("8")
                if flow_strength >= Decimal("2")
                else Decimal("4")
                if flow_strength > 0
                else Decimal("-8")
                if flow_strength <= Decimal("-2")
                else Decimal("-4")
                if flow_strength < 0
                else Decimal("0")
            )
            score = (
                metrics.quant_score
                + flow_adjustment
                + news_adjustment
                + financial_adjustment
            )
            score = max(Decimal("0"), min(Decimal("100"), score))
            flow_text = (
                "외국인·기관 순유입"
                if flow_strength > 0
                else "외국인·기관 순유출"
                if flow_strength < 0
                else "외국인·기관 중립"
            )
            opportunity_text = "하락 모양과 현재 위치는 1차 반복 상승 순위에 섞지 않았습니다."
            if expired_spike_reversion:
                opportunity_text = (
                    "21일 급등 뒤 상단 가격대가 낮아지고 시작 가격대로 "
                    "복귀한 위험 이력은 별도 참고합니다."
                )
            gate_text = (
                f"6% 이상 비중복 상승 {metrics.up_swing_count or 0}회, "
                "평균 상승폭 "
                f"{(metrics.average_up_swing_pct or Decimal('0')).quantize(Decimal('0.1'))}%, "
                "평균 6% 도달 "
                + (
                    f"{metrics.average_time_to_6pct_hours.quantize(Decimal('0.1'))}시간"
                    if metrics.average_time_to_6pct_hours is not None
                    else "표본 없음"
                )
            )
            news_text = (
                f"최신 뉴스 {len(snapshot.news)}건 중 긍정 {positive_news}·"
                f"부정 {negative_news}"
                if snapshot.news_status == "READY"
                else "최신 뉴스 수집 실패"
            )
            window_reviews[key] = AiWindowReview(
                ai_grade=_qualified_grade(
                    score,
                    candidate_price=candidate.current_price,
                    metrics=metrics,
                    expired_spike_reversion=expired_spike_reversion,
                ),  # type: ignore[arg-type]
                ai_score=int(score.quantize(Decimal("1"))),
                ai_comment=(
                    f"{metrics.days}일 기준 {gate_text}, {flow_text} 강도 "
                    f"{flow_strength.quantize(Decimal('0.1'))}%, {news_text}. "
                    f"{financial_text}. "
                    f"{opportunity_text}"
                ),
                reasons=[
                    gate_text,
                    f"{flow_text} {flow_strength.quantize(Decimal('0.1'))}%",
                    news_text,
                    financial_text,
                ],
                risks=(
                    [
                        "토론은 비신뢰 참고 신호",
                        "뉴스 제목 감성은 사건 사실·가격 반영 여부 추가 확인 필요",
                        *(
                            [EXPIRED_SPIKE_RISK]
                            if expired_spike_reversion
                            else []
                        ),
                        *financial_risks,
                    ][:5]
                ),
            )
        reviews.append(
            AiCandidateReview(
                code=candidate.code,
                discussion_summary=_discussion_summary(snapshot),
                discussion_titles=list(snapshot.discussion_titles),
                discussion_url=HttpUrl(snapshot.discussion_url),
                context_status=_context_status(snapshot),
                windows=window_reviews,  # type: ignore[arg-type]
                news=[
                    AiNewsReview(
                        title=item.title,
                        source=item.source,
                        published_at=datetime.fromisoformat(item.published_at),
                        url=HttpUrl(item.url),
                        sentiment=item.sentiment,  # type: ignore[arg-type]
                    )
                    for item in snapshot.news
                ],
            )
        )
    return AiReviewBatch(
        model_id="agent-context-review-v8-official-all-flow-news-dart-fundamental",
        prompt_version="official-all-flow-news-dart-fundamental-v8-20260728",
        report_data_as_of=report.data_as_of,
        reviewed_at=reviewed_at,
        candidates=reviews,
    )
