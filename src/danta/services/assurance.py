from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from danta.config import AppSettings, TradingEnvironment
from danta.services.policy_registry import TradingPolicyRegistry

CheckStatus = Literal["PASS", "WARN", "FAIL", "BLOCKED"]


@dataclass(frozen=True, slots=True)
class AssuranceCheck:
    code: str
    severity: str
    status: CheckStatus
    evidence: str


@dataclass(frozen=True, slots=True)
class AssuranceReport:
    generated_at: datetime
    checks: tuple[AssuranceCheck, ...]
    code_fingerprint: str

    @property
    def ready_for_new_paper_entries(self) -> bool:
        return not any(
            check.severity == "CRITICAL" and check.status != "PASS"
            for check in self.checks
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "ready_for_new_paper_entries": self.ready_for_new_paper_entries,
            "code_fingerprint": self.code_fingerprint,
            "checks": [
                {
                    "code": check.code,
                    "severity": check.severity,
                    "status": check.status,
                    "evidence": check.evidence,
                }
                for check in self.checks
            ],
        }


def build_assurance_report(
    settings: AppSettings,
    policies: TradingPolicyRegistry,
    *,
    project_root: Path,
) -> AssuranceReport:
    checks = (
        _check(
            "SAFETY_USER_APPROVAL",
            "CRITICAL",
            settings.buy_requires_user_approval,
            "buy_requires_user_approval=true is mandatory",
        ),
        _check(
            "SAFETY_HARD_STOP",
            "CRITICAL",
            settings.auto_stop_sell_enabled
            and str(settings.stop_loss_pct) == "7.0"
            and not settings.stop_sell_requires_confirmation,
            "automatic -7% stop must remain enabled without confirmation",
        ),
        _check(
            "PRODUCTION_ORDER_LOCK",
            "CRITICAL",
            not settings.real_order_execution_enabled,
            "real_order_execution_enabled must remain false before promotion",
        ),
        _check(
            "PAPER_ENVIRONMENT",
            "CRITICAL",
            settings.environment is TradingEnvironment.PAPER,
            f"active environment={settings.environment.value}",
        ),
        _check(
            "ENTRY_POLICY_APPROVED",
            "CRITICAL",
            policies.entry.approved_for_paper,
            policies.entry.version,
        ),
        _check(
            "EXIT_POLICY_APPROVED",
            "CRITICAL",
            policies.exit.approved_for_paper,
            policies.exit.version,
        ),
        _check(
            "PAPER_EXECUTION_GATE",
            "CRITICAL",
            settings.paper_order_execution_enabled,
            "paper_order_execution_enabled is the config-side order gate",
        ),
        _check(
            "KIS_SECRET_FILE",
            "CRITICAL",
            settings.kis_credentials_path.is_file(),
            str(settings.kis_credentials_path),
        ),
        _check(
            "DATABASE_MIGRATION_DEFINITION",
            "CRITICAL",
            (project_root / "migrations/versions/0002_execution_runtime.py").is_file(),
            "execution runtime migration definition exists",
        ),
        _database_revision_check(settings, project_root),
        _check(
            "SMTP_CONFIGURATION",
            "WARN",
            (not settings.smtp_enabled) or settings.smtp_config_path.is_file(),
            "SMTP is independent from the protection path",
        ),
        _check(
            "RUNTIME_TESTS_PRESENT",
            "CRITICAL",
            (project_root / "tests/test_trading_runtime.py").is_file()
            and (project_root / "tests/test_exit_engine.py").is_file(),
            "runtime and hard-stop regression suites are present",
        ),
    )
    return AssuranceReport(
        generated_at=datetime.now(UTC),
        checks=checks,
        code_fingerprint=_fingerprint(project_root / "src/danta"),
    )


def write_assurance_report(report: AssuranceReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _check(
    code: str, severity: str, passed: bool, evidence: str
) -> AssuranceCheck:
    return AssuranceCheck(code, severity, "PASS" if passed else "BLOCKED", evidence)


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _database_revision_check(
    settings: AppSettings, project_root: Path
) -> AssuranceCheck:
    prefix = "sqlite+aiosqlite:///"
    if not settings.database_url.startswith(prefix):
        return AssuranceCheck(
            "DATABASE_SCHEMA_APPLIED",
            "CRITICAL",
            "BLOCKED",
            "non-SQLite revision must be verified by the deployment migration job",
        )
    database = Path(settings.database_url.removeprefix(prefix))
    if not database.is_absolute():
        database = project_root / database
    if not database.is_file():
        return AssuranceCheck(
            "DATABASE_SCHEMA_APPLIED",
            "CRITICAL",
            "BLOCKED",
            f"database does not exist: {database}",
        )
    try:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
    except sqlite3.Error as exc:
        return AssuranceCheck(
            "DATABASE_SCHEMA_APPLIED",
            "CRITICAL",
            "BLOCKED",
            f"cannot read alembic revision: {type(exc).__name__}",
        )
    revision = str(row[0]) if row else "missing"
    return AssuranceCheck(
        "DATABASE_SCHEMA_APPLIED",
        "CRITICAL",
        "PASS" if revision == "0002_execution_runtime" else "BLOCKED",
        f"alembic revision={revision}",
    )
