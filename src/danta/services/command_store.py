from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from danta.domain.mandate import EntryMandate, parse_entry_mandate


class CommandStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class StoredCommand:
    mandate: EntryMandate
    status: CommandStatus
    accepted_at: datetime
    source_path: Path


class FileCommandStore:
    """Private, atomic, single-active-command inbox.

    Files are an operator-facing durable handoff, not the broker source of truth.
    KIS account state and the SQL journal always win during reconciliation.
    """

    def __init__(self, root: Path, *, safety_poll_seconds: float = 5.0) -> None:
        if safety_poll_seconds <= 0:
            raise ValueError("safety_poll_seconds must be positive")
        self.root = root
        self.inbox = root / "inbox"
        self.active = root / "active"
        self.archive = root / "archive"
        self.runtime_state_path = root.parent / "runtime_state.json"
        self.safety_poll_seconds = safety_poll_seconds
        for path in (self.inbox, self.active, self.archive):
            path.mkdir(parents=True, exist_ok=True)

    def submit(self, mandate: EntryMandate) -> Path:
        """Atomically place a validated command in the inbox."""
        target = self.inbox / f"{mandate.command_id}.json"
        if target.exists():
            return target
        existing = self._find_command(mandate.command_id)
        if existing is not None:
            return existing
        self._atomic_json(
            target,
            {
                "schema_version": 1,
                "command_id": mandate.command_id,
                "status": "PENDING",
                "submitted_at": datetime.now(UTC).isoformat(),
                "mandate": mandate.model_dump(mode="json"),
            },
        )
        return target

    def submit_document(self, source: Path) -> Path:
        text = source.read_text(encoding="utf-8")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            mandate = parse_entry_mandate(text)
        else:
            payload = raw.get("mandate", raw) if isinstance(raw, dict) else raw
            mandate = EntryMandate.model_validate(payload)
        return self.submit(mandate)

    def load_active(self) -> StoredCommand | None:
        files = sorted(self.active.glob("*.json"))
        if len(files) > 1:
            raise RuntimeError("multiple active ENTRY_MANDATE files require quarantine")
        return self._read(files[0]) if files else None

    def accept_next(self) -> StoredCommand | None:
        current = self.load_active()
        if current is not None:
            return current
        for source in sorted(self.inbox.glob("*.json")):
            try:
                pending = self._read(source)
            except (OSError, ValueError, ValidationError, json.JSONDecodeError):
                self._archive_invalid(source)
                continue
            accepted_at = datetime.now(UTC)
            target = self.active / f"{pending.mandate.command_id}.json"
            self._atomic_json(
                target,
                self._envelope(
                    pending.mandate,
                    status=CommandStatus.ACTIVE,
                    accepted_at=accepted_at,
                ),
            )
            source.unlink(missing_ok=True)
            return StoredCommand(
                mandate=pending.mandate,
                status=CommandStatus.ACTIVE,
                accepted_at=accepted_at,
                source_path=target,
            )
        return None

    def archive_active(
        self,
        command_id: str,
        *,
        status: CommandStatus,
        reason: str,
    ) -> Path:
        if status is CommandStatus.ACTIVE:
            raise ValueError("terminal archive status is required")
        active = self.active / f"{command_id}.json"
        if not active.exists():
            existing = self._find_command(command_id)
            if existing is not None:
                return existing
            raise FileNotFoundError(f"active command not found: {command_id}")
        stored = self._read(active)
        target = self.archive / f"{command_id}.{status.value.lower()}.json"
        payload = self._envelope(
            stored.mandate,
            status=status,
            accepted_at=stored.accepted_at,
        )
        payload["completed_at"] = datetime.now(UTC).isoformat()
        payload["terminal_reason"] = reason
        self._atomic_json(target, payload)
        active.unlink()
        return target

    def write_runtime_state(self, state: dict[str, object]) -> None:
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            **state,
        }
        self._atomic_json(self.runtime_state_path, payload)

    async def wait_for_change(self, stop: asyncio.Event | None = None) -> None:
        """Low-cost safety poll.

        One five-second directory check is negligible on a laptop and avoids a
        correctness dependency on platform-specific file notification loss.
        """
        if stop is None:
            await asyncio.sleep(self.safety_poll_seconds)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=self.safety_poll_seconds)
        except TimeoutError:
            return

    def _read(self, path: Path) -> StoredCommand:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("command envelope must be an object")
        payload = raw.get("mandate", raw)
        mandate = EntryMandate.model_validate(payload)
        command_id = str(raw.get("command_id", mandate.command_id))
        if command_id != mandate.command_id:
            raise ValueError("command_id does not match mandate payload")
        status_text = str(raw.get("status", "ACTIVE"))
        status = (
            CommandStatus.ACTIVE
            if status_text == "PENDING"
            else CommandStatus(status_text)
        )
        accepted_text = raw.get("accepted_at") or raw.get("submitted_at")
        accepted_at = (
            datetime.fromisoformat(str(accepted_text))
            if accepted_text
            else datetime.now(UTC)
        )
        if accepted_at.tzinfo is None:
            raise ValueError("command timestamp must be timezone-aware")
        return StoredCommand(mandate, status, accepted_at, path)

    def _archive_invalid(self, source: Path) -> None:
        target = self.archive / f"{source.stem}.rejected-{uuid4().hex[:8]}.json"
        os.replace(source, target)

    def _find_command(self, command_id: str) -> Path | None:
        for folder in (self.active, self.archive):
            matches = list(folder.glob(f"{command_id}*.json"))
            if matches:
                return matches[0]
        return None

    @staticmethod
    def _envelope(
        mandate: EntryMandate,
        *,
        status: CommandStatus,
        accepted_at: datetime,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "command_id": mandate.command_id,
            "status": status.value,
            "accepted_at": accepted_at.isoformat(),
            "mandate": mandate.model_dump(mode="json"),
        }

    @staticmethod
    def _atomic_json(target: Path, payload: dict[str, object]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
