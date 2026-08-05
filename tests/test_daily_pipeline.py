from datetime import datetime
from zoneinfo import ZoneInfo

from danta.services.daily_pipeline import _latest_completed_dataset_date

KST = ZoneInfo("Asia/Seoul")


def test_intraday_pipeline_uses_previous_calendar_date() -> None:
    assert _latest_completed_dataset_date(
        datetime(2026, 7, 31, 11, 45, tzinfo=KST)
    ).isoformat() == "2026-07-30"


def test_post_close_pipeline_can_use_current_calendar_date() -> None:
    assert _latest_completed_dataset_date(
        datetime(2026, 7, 31, 16, 0, tzinfo=KST)
    ).isoformat() == "2026-07-31"
