from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from danta.dashboard.demo import demo_report
from danta.dashboard.models import ChartBar, DashboardReport
from danta.services.ai_review import apply_ai_review
from danta.services.context_review import (
    CollectedNews,
    ContextSnapshot,
    _is_expired_spike_reversion,
    _NaverBoardParser,
    _qualified_grade,
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
            disclosure_status="READY",
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
    assert (
        review.model_id
        == "agent-context-review-v9-repeat-continuation-challenger"
    )
    assert all(set(candidate.windows) == {"7", "14", "21"} for candidate in review.candidates)
    assert all(candidate.context_status == "READY" for candidate in review.candidates)
    assert all(len(candidate.news) == 1 for candidate in validated.candidates[:50])
    assert all(not candidate.news for candidate in validated.candidates[50:])
    assert all(
        candidate.windows["14"].ai_grade is None
        for candidate in validated.candidates[50:]
    )
    assert all(
        candidate.discussion_titles == ["반등 기대", "매수 의견"]
        for candidate in validated.candidates
    )
    assert all(candidate.discussion_url is not None for candidate in validated.candidates)


def test_expired_spike_reversion_requires_peak_and_falling_upper_regime() -> None:
    metrics = demo_report().candidates[0].windows["21"]
    decaying_bars = []
    stable_bars = []
    for day in range(1, 22):
        date = f"202607{day:02d}"
        decaying_high = (
            Decimal("150")
            if day <= 7
            else Decimal("125")
            if day <= 14
            else Decimal("108")
        )
        stable_high = Decimal("115")
        decaying_bars.append(
            ChartBar(
                trading_date=date,
                bucket="09",
                open=Decimal("100"),
                high=decaying_high,
                low=Decimal("98"),
                close=Decimal("102"),
                volume=Decimal("1000"),
            )
        )
        stable_bars.append(
            ChartBar(
                trading_date=date,
                bucket="09",
                open=Decimal("100"),
                high=stable_high,
                low=Decimal("98"),
                close=Decimal("102"),
                volume=Decimal("1000"),
            )
        )

    decaying = metrics.model_copy(
        update={"return_pct": Decimal("2"), "chart_bars": decaying_bars}
    )
    stable = metrics.model_copy(
        update={"return_pct": Decimal("2"), "chart_bars": stable_bars}
    )

    assert _is_expired_spike_reversion(decaying)
    assert not _is_expired_spike_reversion(stable)


def test_repeat_continuation_can_qualify_after_first_target_was_passed() -> None:
    metrics = demo_report().candidates[0].windows["14"].model_copy(
        update={
            "rank": 2,
            "position_pct": Decimal("51.4"),
            "current_vs_window_high_pct": Decimal("-20.6"),
            "average_up_swing_pct": Decimal("15.0"),
            "up_swing_count": 13,
            "average_time_to_6pct_hours": Decimal("1.4"),
            "volume_ratio": Decimal("2.05"),
            "target_reach_count": 2,
            "target_price_10pct": Decimal("179410"),
            "decline_shape": "OTHER",
        }
    )

    assert _qualified_grade(
        Decimal("92"),
        candidate_price=Decimal("225000"),
        metrics=metrics,
        expired_spike_reversion=False,
    ) == "STRONG_RECOMMEND"


def test_repeat_continuation_rejects_extended_or_structurally_broken_setup() -> None:
    base = demo_report().candidates[0].windows["14"].model_copy(
        update={
            "rank": 2,
            "position_pct": Decimal("76"),
            "current_vs_window_high_pct": Decimal("-7"),
            "average_up_swing_pct": Decimal("15"),
            "up_swing_count": 13,
            "average_time_to_6pct_hours": Decimal("1.4"),
            "volume_ratio": Decimal("2"),
            "target_reach_count": 2,
            "target_price_10pct": Decimal("179410"),
            "decline_shape": "STRUCTURAL_DECLINE",
        }
    )

    assert _qualified_grade(
        Decimal("92"),
        candidate_price=Decimal("225000"),
        metrics=base,
        expired_spike_reversion=False,
    ) == "NOT_RECOMMEND"
