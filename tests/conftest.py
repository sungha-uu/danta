from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from danta.config import clear_settings_cache


@pytest.fixture(autouse=True)
def reset_settings_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("DANTA_CONFIG", raising=False)
    monkeypatch.delenv("DANTA_DATABASE_URL", raising=False)
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def paper_credentials_file(tmp_path: Path) -> Path:
    path = tmp_path / "paper.json"
    path.write_text(
        json.dumps(
            {
                "environment": "paper",
                "app_key": "test-app-key",
                "app_secret": "test-app-secret",
                "account_no": "12345678",
                "product_code": "01",
                "hts_id": "test-user",
            }
        ),
        encoding="utf-8",
    )
    return path

