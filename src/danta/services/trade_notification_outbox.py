from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class TradeNotificationKind(StrEnum):
    BUY = "BUY"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True)
class TradeNotification:
    intent_key: str
    correlation_id: str
    kind: TradeNotificationKind
    name: str
    price: int
    quantity: int | None
    return_pct: Decimal | None
    cause: str | None
    created_at: datetime


class TradeNotificationOutbox:
    """Private at-least-once outbox for mandatory trade-fill emails."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pending = root / "pending"
        self.sent = root / "sent"
        self.pending.mkdir(parents=True, exist_ok=True)
        self.sent.mkdir(parents=True, exist_ok=True)

    def enqueue_buy(
        self,
        *,
        intent_key: str,
        correlation_id: str,
        name: str,
        price: int,
        quantity: int,
    ) -> bool:
        return self._enqueue(
            TradeNotification(
                intent_key=intent_key,
                correlation_id=correlation_id,
                kind=TradeNotificationKind.BUY,
                name=name,
                price=price,
                quantity=quantity,
                return_pct=None,
                cause=None,
                created_at=datetime.now(UTC),
            )
        )

    def enqueue_exit(
        self,
        *,
        intent_key: str,
        correlation_id: str,
        name: str,
        price: int,
        return_pct: Decimal,
        cause: str,
    ) -> bool:
        return self._enqueue(
            TradeNotification(
                intent_key=intent_key,
                correlation_id=correlation_id,
                kind=TradeNotificationKind.EXIT,
                name=name,
                price=price,
                quantity=None,
                return_pct=return_pct,
                cause=cause,
                created_at=datetime.now(UTC),
            )
        )

    def load_pending(self) -> list[TradeNotification]:
        return [self._read(path) for path in sorted(self.pending.glob("*.json"))]

    def mark_sent(self, *, kind: TradeNotificationKind, intent_key: str) -> None:
        pending = self.pending / self._filename(kind, intent_key)
        sent = self.sent / self._filename(kind, intent_key)
        if sent.exists():
            return
        if not pending.exists():
            raise FileNotFoundError(f"pending trade notification not found: {intent_key}")
        os.replace(pending, sent)

    def _enqueue(self, item: TradeNotification) -> bool:
        filename = self._filename(item.kind, item.intent_key)
        pending = self.pending / filename
        sent = self.sent / filename
        if pending.exists() or sent.exists():
            return False
        payload = asdict(item)
        payload["kind"] = item.kind.value
        payload["return_pct"] = None if item.return_pct is None else str(item.return_pct)
        payload["created_at"] = item.created_at.isoformat()
        self._atomic_json(pending, payload)
        return True

    def _read(self, path: Path) -> TradeNotification:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return TradeNotification(
            intent_key=str(raw["intent_key"]),
            correlation_id=str(raw["correlation_id"]),
            kind=TradeNotificationKind(str(raw["kind"])),
            name=str(raw["name"]),
            price=int(raw["price"]),
            quantity=None if raw["quantity"] is None else int(raw["quantity"]),
            return_pct=(None if raw["return_pct"] is None else Decimal(str(raw["return_pct"]))),
            cause=None if raw["cause"] is None else str(raw["cause"]),
            created_at=datetime.fromisoformat(str(raw["created_at"])),
        )

    @staticmethod
    def _filename(kind: TradeNotificationKind, intent_key: str) -> str:
        digest = hashlib.sha256(f"{kind.value}:{intent_key}".encode()).hexdigest()
        return f"{kind.value.lower()}-{digest}.json"

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
