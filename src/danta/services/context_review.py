from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import HttpUrl

from danta.dashboard.models import DashboardReport
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
        concurrency: int = 5,
        timeout_seconds: float = 12,
    ) -> None:
        self.cache_root = cache_root
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
            results = await asyncio.gather(
                *(
                    self._collect_one(
                        client,
                        semaphore,
                        code,
                        name,
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
                    }
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        async with semaphore:
            news_result, discussion_result = await asyncio.gather(
                self._fetch_news(client, name),
                self._fetch_discussion(client, code),
                return_exceptions=True,
            )
        if isinstance(news_result, BaseException):
            news_status = "FAILED"
            news: tuple[CollectedNews, ...] = ()
        else:
            news_status = "READY"
            news = news_result
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
            discussion_status=discussion_status,
            news=news,
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
    statuses = {snapshot.news_status, snapshot.discussion_status}
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


def build_context_review(
    report: DashboardReport,
    snapshots: dict[str, ContextSnapshot],
    *,
    reviewed_at: datetime,
) -> AiReviewBatch:
    reviews: list[AiCandidateReview] = []
    for candidate in report.candidates:
        snapshot = snapshots[candidate.code]
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
            decline_opportunity = (
                (metrics.lower_trend_pct or Decimal("0")) < Decimal("-2")
                and (metrics.position_pct or Decimal("100")) <= Decimal("35")
                and flow_strength > 0
            )
            score = metrics.quant_score + flow_adjustment + news_adjustment
            if decline_opportunity:
                score += Decimal("5")
            active = metrics.active_box
            active_reaches = (
                active.upper_reaches
                if active is not None
                else (metrics.target_reach_count or 0)
            )
            if (metrics.position_pct or Decimal("100")) > Decimal("50"):
                score = min(score, Decimal("44"))
            elif (metrics.position_pct or Decimal("100")) > Decimal("35"):
                score = min(score, Decimal("59"))
            if active_reaches < 1:
                score = min(score, Decimal("69"))
            if active is not None and active.confidence == "LOW":
                score = min(score, Decimal("74"))
            if (
                active is not None
                and candidate.current_price < active.structural_invalidation_price
            ):
                score = min(score, Decimal("44"))
            score = max(Decimal("0"), min(Decimal("100"), score))
            flow_text = (
                "외국인·기관 순유입"
                if flow_strength > 0
                else "외국인·기관 순유출"
                if flow_strength < 0
                else "외국인·기관 중립"
            )
            opportunity_text = (
                "하락 자체보다 하단 할인과 수급 유입을 기회 요인으로 평가했습니다."
                if decline_opportunity
                else "하락 추세는 단독 감점하지 않았습니다."
            )
            gate_text = (
                (
                    f"활성 하단→상단 재도달 {active.upper_reaches}회, "
                    f"손절 선행 {active.stop_first}회, "
                    f"후향 관측 재도달률 "
                    f"{active.success_rate_pct.quantize(Decimal('0.1'))}% "
                    f"(신뢰 {active.confidence})"
                )
                if active is not None
                else (
                    f"하단 후 +10% 도달 {metrics.target_reach_count}회"
                    if (metrics.target_reach_count or 0) > 0
                    else "하단 후 +10% 도달 이력 없음"
                )
            )
            news_text = (
                f"최신 뉴스 {len(snapshot.news)}건 중 긍정 {positive_news}·"
                f"부정 {negative_news}"
                if snapshot.news_status == "READY"
                else "최신 뉴스 수집 실패"
            )
            window_reviews[key] = AiWindowReview(
                ai_grade=_grade(score),  # type: ignore[arg-type]
                ai_score=int(score.quantize(Decimal("1"))),
                ai_comment=(
                    f"{metrics.days}일 기준 현재 위치 "
                    f"{(metrics.position_pct or Decimal('0')).quantize(Decimal('0.1'))}%, "
                    f"{gate_text}, {flow_text} 강도 "
                    f"{flow_strength.quantize(Decimal('0.1'))}%, {news_text}. "
                    f"{opportunity_text}"
                ),
                reasons=[
                    gate_text,
                    f"{flow_text} {flow_strength.quantize(Decimal('0.1'))}%",
                    news_text,
                ],
                risks=[
                    "토론은 비신뢰 참고 신호",
                    "뉴스 제목 감성은 사건 사실·가격 반영 여부 추가 확인 필요",
                    *(
                        ["바닥 안정 확인 전 진입 보류"]
                        if (metrics.lower_trend_pct or Decimal("0")) < Decimal("-2")
                        else []
                    ),
                ],
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
        model_id="agent-context-review-v2-active-box-flow-priority",
        prompt_version="active-box-lower-flow-news-discussion-v2-20260726",
        report_data_as_of=report.data_as_of,
        reviewed_at=reviewed_at,
        candidates=reviews,
    )
