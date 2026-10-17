from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import math
from pathlib import Path
import re
import time
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4
from zipfile import BadZipFile, ZipFile


class FinamReports:
    def __init__(self, broker, output_dir: str | Path):
        self.broker = broker
        self.output_dir = Path(output_dir) / "broker_reports"
        self.timeout = float(broker.settings.get("report_timeout_seconds", 120))
        self.poll_seconds = float(broker.settings.get("report_poll_seconds", 2))
        self.report_dates = {}
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("Finam: report_timeout_seconds должен быть положительным числом")
        if not math.isfinite(self.poll_seconds) or self.poll_seconds <= 0:
            raise ValueError("Finam: report_poll_seconds должен быть положительным числом")

    @staticmethod
    def _account_id(account_id: str) -> str:
        raw = str(account_id)
        if not re.fullmatch(r"[0-9]+", raw) or not 0 < int(raw) <= 9223372036854775807:
            raise ValueError("Finam Reports API требует положительный числовой account_id в диапазоне int64")
        return str(int(raw))

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime должен быть timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _trusted_url(url: str) -> str:
        if not isinstance(url, str) or not url or any(ord(character) < 32 for character in url):
            raise RuntimeError("Finam: некорректная ссылка на брокерский отчёт")
        try:
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").lower()
            valid = (
                parsed.scheme == "https"
                and (hostname == "finam.ru" or hostname.endswith(".finam.ru"))
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
            )
        except ValueError as error:
            raise RuntimeError("Finam: некорректная ссылка на брокерский отчёт") from error
        if not valid:
            raise RuntimeError("Finam: загрузка отчёта разрешена только по HTTPS с доменов finam.ru")
        return url

    def _wait(self, report_id: str, account_id: str) -> dict:
        deadline = time.monotonic() + self.timeout
        path = f"report/{quote(report_id, safe='')}/info"
        while True:
            if time.monotonic() >= deadline:
                raise RuntimeError("Finam: время ожидания брокерского отчёта истекло; повтори запрос позже")
            payload = self.broker._request("GET", path)
            info = payload.get("info")
            if not isinstance(info, dict):
                raise RuntimeError("Finam: в ответе отсутствует статус брокерского отчёта")
            if info.get("report_id", report_id) != report_id:
                raise RuntimeError("Finam: получен статус другого брокерского отчёта")
            if str(info.get("account_id", account_id)) != account_id:
                raise RuntimeError("Finam: брокерский отчёт относится к другому счёту")
            status = info.get("status", "NOT_FOUND")
            status = {0: "NOT_FOUND", 1: "PENDING", 2: "IN_PROGRESS", 3: "SUCCESS", 4: "ERROR"}.get(status, status)
            if status == "SUCCESS":
                return info
            if status == "ERROR":
                raise RuntimeError("Finam: генерация брокерского отчёта завершилась ошибкой")
            if status == "NOT_FOUND":
                raise RuntimeError("Finam: созданный брокерский отчёт не найден")
            if status not in {"PENDING", "IN_PROGRESS"}:
                raise RuntimeError("Finam: API вернул неизвестный статус брокерского отчёта")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Finam: время ожидания брокерского отчёта истекло; повтори запрос позже")
            time.sleep(min(self.poll_seconds, remaining, 2))

    @staticmethod
    def _extension(content: bytes, headers: dict[str, str]) -> str:
        if content.startswith((b"PK\x03\x04", b"PK\x05\x06")):
            try:
                with ZipFile(BytesIO(content)) as archive:
                    names = set(archive.namelist())
                if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
                    return ".xlsx"
            except BadZipFile:
                return ".zip"
            return ".zip"
        if content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return ".xls"
        if content.startswith(b"%PDF-"):
            return ".pdf"
        stripped = content.lstrip(b"\xef\xbb\xbf \r\n\t")
        lowered = stripped[:100].lower()
        if lowered.startswith((b"<!doctype html", b"<html")):
            return ".html"
        if stripped.startswith(b"<"):
            return ".xml"
        if stripped.startswith((b"{", b"[")):
            return ".json"
        content_type = headers.get("content-type", "").lower()
        disposition = headers.get("content-disposition", "").lower()
        if "text/csv" in content_type or re.search(r"\.csv(?:[\"';\s]|$)", disposition):
            return ".csv"
        if "tab-separated-values" in content_type or re.search(r"\.tsv(?:[\"';\s]|$)", disposition):
            return ".tsv"
        return ".bin"

    def _download(self, url: str) -> tuple[bytes, str]:
        current = self._trusted_url(url)
        redirects = 0
        refreshed = False
        while True:
            if not self.broker._jwt:
                self.broker._authenticate()
            pace = getattr(self.broker, "_pace", None)
            if pace is not None:
                pace()
            try:
                response = self.broker.session.get(
                    current,
                    headers={"Authorization": f"Bearer {self.broker._jwt}"},
                    timeout=self.broker.timeout,
                    follow_redirects=False,
                )
            except Exception:
                raise RuntimeError("Finam: не удалось скачать брокерский отчёт") from None
            if response.status_code == 401 and not refreshed and self.broker._secret:
                self.broker._authenticate()
                refreshed = True
                continue
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            if response.status_code in {301, 302, 303, 307, 308}:
                redirects += 1
                if redirects > 5:
                    raise RuntimeError("Finam: слишком много перенаправлений при загрузке брокерского отчёта")
                location = headers.get("location")
                if not location:
                    raise RuntimeError("Finam: перенаправление отчёта не содержит адреса")
                current = self._trusted_url(urljoin(current, location))
                continue
            if response.status_code != 200:
                raise RuntimeError(f"Finam: загрузка брокерского отчёта завершилась HTTP {response.status_code}")
            content = response.content
            if not isinstance(content, bytes) or not content:
                raise RuntimeError("Finam: скачан пустой брокерский отчёт")
            return content, self._extension(content, headers)

    def download(
        self,
        account_id: str,
        date_from: datetime,
        date_to_exclusive: datetime,
        *,
        chunk_days: int = 92,
    ) -> list[Path]:
        account_id = self._account_id(account_id)
        start = self._utc(date_from)
        end = self._utc(date_to_exclusive)
        if start > end:
            raise ValueError("Начало периода должно быть раньше окончания")
        if isinstance(chunk_days, bool) or not isinstance(chunk_days, int) or not 1 <= chunk_days <= 92:
            raise ValueError("Finam: размер периода отчёта должен быть от 1 до 92 дней")
        paths = []
        while start < end:
            if chunk_days == 1:
                stop = min((start.astimezone(date_from.tzinfo) + timedelta(days=1)).astimezone(timezone.utc), end)
            else:
                stop = min(start + timedelta(days=chunk_days), end)
            report_end = stop - timedelta(microseconds=1) if chunk_days == 1 else stop
            created = self.broker._request("POST", "report", json={
                "date_range": {"date_begin": self._iso(start), "date_end": self._iso(report_end)},
                "report_form": "REPORT_FORM_LONG",
                "account_id": account_id,
            })
            report_id = created.get("report_id")
            if not isinstance(report_id, str) or not report_id.strip():
                raise RuntimeError("Finam: API не вернул идентификатор брокерского отчёта")
            info = self._wait(report_id, account_id)
            url = info.get("url")
            if isinstance(url, dict):
                url = url.get("value")
            content, extension = self._download(url)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            path = self.output_dir / f"finam_{account_id}_{start:%Y%m%d}_{stop:%Y%m%d}_{uuid4().hex}{extension}"
            with path.open("xb") as target:
                target.write(content)
            paths.append(path)
            self.report_dates[path] = start.astimezone(date_from.tzinfo).date()
            start = stop
        return paths
