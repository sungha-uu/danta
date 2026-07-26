from __future__ import annotations

from datetime import UTC, datetime

from danta.dashboard.demo import demo_report
from danta.dashboard.models import DashboardReport
from danta.services.ai_review import apply_ai_review
from danta.services.context_review import (
    CollectedNews,
    ContextSnapshot,
    _NaverBoardParser,
    build_context_review,
)


def test_naver_board_parser_collects_only_discussion_titles() -> None:
    parser = _NaverBoardParser(limit=2)
    parser.feed(
        """
        <table>
          <tr><td class="title"><a href="/item/board_read.naver">첫 글</a></td></tr>
          <tr><td class="other"><a href="#">무시할 링크</a></td></tr>
          <tr><td class="title"><a href="/item/board_read.naver">둘째 글</a></td></tr>
          <tr><td class="title"><a href="/item/board_read.naver">셋째 글</a></td></tr>
        </table>
        """
    )

    assert parser.titles == ["첫 글", "둘째 글"]


def test_context_review_covers_all_candidates_and_applies_public_context() -> None:
    report = demo_report()
    now = datetime.now(UTC)
    snapshots = {
        candidate.code: ContextSnapshot(
            code=candidate.code,
            name=candidate.name,
            fetched_at=now.isoformat(),
            news_status="READY",
            discussion_status="READY",
            news=(
                CollectedNews(
                    title=f"{candidate.name} 신규 계약 체결",
                    source="테스트뉴스",
                    published_at=now.isoformat(),
                    url="https://example.com/news",
                    sentiment="POSITIVE",
                ),
            ),
            discussion_titles=("반등 기대", "매수 의견"),
            discussion_url=(
                "https://finance.naver.com/item/board.naver"
                f"?code={candidate.code}"
            ),
        )
        for candidate in report.candidates
    }

    review = build_context_review(report, snapshots, reviewed_at=now)
    reviewed_report = apply_ai_review(report, review)
    validated = DashboardReport.model_validate(reviewed_report.model_dump())

    assert len(review.candidates) == 50
    assert review.model_id == "agent-context-review-v2-active-box-flow-priority"
    assert all(set(candidate.windows) == {"7", "14", "21"} for candidate in review.candidates)
    assert all(candidate.context_status == "READY" for candidate in review.candidates)
    assert all(len(candidate.news) == 1 for candidate in validated.candidates)
    assert all(
        candidate.discussion_titles == ["반등 기대", "매수 의견"]
        for candidate in validated.candidates
    )
    assert all(candidate.discussion_url is not None for candidate in validated.candidates)
