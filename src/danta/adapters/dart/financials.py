from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx


class DartFinancialDataError(RuntimeError):
    """Raised when Open DART returns an invalid or failed financial response."""


class OpenDartFinancialClient:
    BASE_URL = "https://opendart.fss.or.kr/api"
    MAX_COMPANIES_PER_REQUEST = 100

    def __init__(
        self,
        api_key: str,
        *,
        corp_code_cache_path: Path,
        timeout_seconds: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DART API key must not be blank")
        self.api_key = api_key
        self.corp_code_cache_path = corp_code_cache_path
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def load_corp_code_map(self, *, refresh: bool = False) -> dict[str, str]:
        if self.corp_code_cache_path.exists() and not refresh:
            try:
                body = json.loads(
                    self.corp_code_cache_path.read_text(encoding="utf-8")
                )
                if isinstance(body, dict) and body:
                    return {
                        str(symbol): str(corp_code)
                        for symbol, corp_code in body.items()
                        if str(symbol).strip() and str(corp_code).strip()
                    }
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        async with self._client() as client:
            response = await client.get(
                f"{self.BASE_URL}/corpCode.xml",
                params={"crtfc_key": self.api_key},
            )
            response.raise_for_status()
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                xml_name = next(
                    name
                    for name in archive.namelist()
                    if name.lower().endswith(".xml")
                )
                root = ET.fromstring(archive.read(xml_name))
        except (StopIteration, zipfile.BadZipFile, ET.ParseError) as exc:
            raise DartFinancialDataError("invalid DART corporation-code archive") from exc
        mapping = {
            (item.findtext("stock_code") or "").strip(): (
                item.findtext("corp_code") or ""
            ).strip()
            for item in root.findall("list")
            if (item.findtext("stock_code") or "").strip()
            and (item.findtext("corp_code") or "").strip()
        }
        self.corp_code_cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.corp_code_cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(mapping, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.corp_code_cache_path)
        return mapping

    async def fetch_major_accounts(
        self,
        corp_codes: Sequence[str],
        *,
        business_year: int,
        report_code: str,
    ) -> list[dict[str, Any]]:
        unique_codes = tuple(dict.fromkeys(code.strip() for code in corp_codes if code.strip()))
        rows: list[dict[str, Any]] = []
        async with self._client() as client:
            for start in range(0, len(unique_codes), self.MAX_COMPANIES_PER_REQUEST):
                chunk = unique_codes[start : start + self.MAX_COMPANIES_PER_REQUEST]
                response = await client.get(
                    f"{self.BASE_URL}/fnlttMultiAcnt.json",
                    params={
                        "crtfc_key": self.api_key,
                        "corp_code": ",".join(chunk),
                        "bsns_year": str(business_year),
                        "reprt_code": report_code,
                    },
                )
                response.raise_for_status()
                body = response.json()
                status = str(body.get("status", ""))
                if status == "013":
                    continue
                if status != "000":
                    raise DartFinancialDataError(
                        "DART major-account request failed: "
                        f"{status} {body.get('message', '')}".strip()
                    )
                items = body.get("list", [])
                if not isinstance(items, list):
                    raise DartFinancialDataError(
                        "DART major-account response list is invalid"
                    )
                rows.extend(item for item in items if isinstance(item, dict))
        return rows

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            transport=self.transport,
        )
