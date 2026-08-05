from __future__ import annotations

import sqlite3
from pathlib import Path

from danta.config import AppSettings
from danta.services.assurance import _database_revision_check


def test_assurance_accepts_current_runtime_recovery_revision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "paper.db"
    with sqlite3.connect(database) as connection:
        connection.execute("create table alembic_version (version_num text)")
        connection.execute(
            "insert into alembic_version values ('0004_runtime_recovery')"
        )
    settings = AppSettings(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
    )

    check = _database_revision_check(settings, tmp_path)

    assert check.status == "PASS"


def test_assurance_blocks_stale_schema_revision(tmp_path: Path) -> None:
    database = tmp_path / "paper.db"
    with sqlite3.connect(database) as connection:
        connection.execute("create table alembic_version (version_num text)")
        connection.execute(
            "insert into alembic_version values ('0003_market_wide_monitor')"
        )
    settings = AppSettings(
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
    )

    check = _database_revision_check(settings, tmp_path)

    assert check.status == "BLOCKED"
