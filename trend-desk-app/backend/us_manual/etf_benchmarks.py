"""H6 美国 ETF 的 SEC 身份、权威基准证据和跟踪指数去重。

SEC ticker 文件只负责 CIK/series/class 身份。跟踪指数必须来自发行商官方
资料或精确关联的 SEC 法定文件；方向、杠杆、策略和汇率对冲仅作观察字段。
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time as time_module
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from pypdf import PdfReader
from sqlmodel import Session

from backend import config
from backend.db import UsEtfBenchmarkEvidence, UsEtfBenchmarkReview, UsEtfIdentity
from backend.us_manual import repository
from backend.us_manual.contracts import (
    UsManualError,
    canonical_json,
    decimal_text,
    serialize,
    sha256,
    utc_now,
)


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
SEC_FORMS = {"497", "497K", "485APOS", "485BPOS", "N-1A", "N-1A/A", "S-6", "S-6/A"}
PARSER_VERSION = "us-etf-benchmark-v2-index-only"
SEC_MIN_REQUEST_INTERVAL_SECONDS = 0.125
SEC_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SEC_JSON_MEDIA_TYPES = {"application/json", "text/json"}
SEC_TEXT_MEDIA_TYPES = {
    "text/html", "text/plain", "application/xhtml+xml", "text/xml", "application/xml",
}
ISSUER_HTML_MEDIA_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
ISSUER_PDF_MEDIA_TYPES = {"application/pdf"}
ISSUER_ALLOWED_HOSTS = {
    "www.ssga.com",
    "www.ishares.com",
    "workplace.vanguard.com",
    "kraneshares.com",
    "www.kraneshares.com",
}
ISSUER_SOURCES: dict[str, tuple[dict[str, str], ...]] = {
    "SPY": ({
        "source_type": "issuer_product_page",
        "url": "https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy",
    },),
    "IVV": ({
        "source_type": "issuer_fact_sheet",
        "url": "https://www.ishares.com/us/literature/fact-sheet/ivv-ishares-core-s-p-500-etf-fund-fact-sheet-en-us.pdf",
    },),
    "VOO": ({
        "source_type": "issuer_fact_sheet",
        "url": "https://workplace.vanguard.com/assets/corp/fund_communications/pdf_publish/us-products/fact-sheet/F0968.pdf",
    },),
    "KWEB": ({
        "source_type": "issuer_fact_sheet",
        "url": "https://kraneshares.com/resources/factsheet/kweb_factsheet.pdf",
    },),
}
ISSUER_USER_AGENT = "TrendDesk/1.0 ETF benchmark evidence"
ISSUER_MAX_PDF_PAGES = 160
ISSUER_MAX_EXTRACTED_TEXT_CHARS = 2_000_000
_SEC_REQUEST_LOCK = threading.Lock()
_SEC_LAST_REQUEST_AT = 0.0
FINGERPRINT_FIELDS = ("benchmark_family_id",)


def _headers() -> dict[str, str]:
    # secrets.env is loaded during FastAPI lifespan, after backend.config was
    # imported. Read these values at request time so a local/private setting
    # actually takes effect without copying the contact email into source.
    agent = os.getenv("US_MANUAL_SEC_USER_AGENT", "").strip()
    if not agent and os.getenv(
        "US_MANUAL_SEC_USE_NOTIFICATION_EMAIL", "false",
    ).strip().lower() == "true":
        contact = os.getenv("TREND_EMAIL_TO", config.EMAIL_TO).strip()
        if contact and "@" in contact:
            agent = f"TrendDesk/1.0 {contact}"
    if not agent or "@" not in agent:
        raise UsManualError(
            "sec_user_agent_not_configured",
            "SEC 自动访问需要配置含联系邮箱的 US_MANUAL_SEC_USER_AGENT，"
            "或明确启用 US_MANUAL_SEC_USE_NOTIFICATION_EMAIL",
            503,
        )
    return {"User-Agent": agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json,text/html"}


def _pace_sec_request() -> None:
    """Keep the whole process below SEC's published 10 requests/second ceiling."""
    global _SEC_LAST_REQUEST_AT
    with _SEC_REQUEST_LOCK:
        now = time_module.monotonic()
        wait = SEC_MIN_REQUEST_INTERVAL_SECONDS - (now - _SEC_LAST_REQUEST_AT)
        if wait > 0:
            time_module.sleep(wait)
        _SEC_LAST_REQUEST_AT = time_module.monotonic()


def _validate_sec_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "sec.gov" or host.endswith(".sec.gov")):
        raise UsManualError("etf_source_not_allowed", "自动法定文件抓取只允许 SEC HTTPS 地址", 422)


def _validate_issuer_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ISSUER_ALLOWED_HOSTS:
        raise UsManualError("etf_issuer_source_not_allowed", "发行商自动抓取地址不在受控官方域名列表", 422)


def _validated_response_bytes(
    response: httpx.Response, *, allowed_media_types: set[str], source_label: str,
) -> bytes:
    _validate_sec_url(str(response.url))
    raw_length = response.headers.get("content-length")
    if raw_length:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise UsManualError("sec_contract_error", f"{source_label} Content-Length 无效", 503) from exc
        if declared_length > SEC_MAX_RESPONSE_BYTES:
            raise UsManualError("sec_response_too_large", f"{source_label} 超过 8MB 安全上限", 409)
    content = response.content
    if len(content) > SEC_MAX_RESPONSE_BYTES:
        raise UsManualError("sec_response_too_large", f"{source_label} 超过 8MB 安全上限", 409)
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type not in allowed_media_types:
        raise UsManualError(
            "sec_mime_unsupported",
            f"{source_label} Content-Type 不受支持：{media_type or 'missing'}",
            409,
        )
    return content


def _get_json(url: str) -> dict[str, Any]:
    _validate_sec_url(url)
    try:
        with httpx.Client(timeout=20, headers=_headers(), follow_redirects=True) as client:
            _pace_sec_request()
            response = client.get(url)
            response.raise_for_status()
            content = _validated_response_bytes(
                response, allowed_media_types=SEC_JSON_MEDIA_TYPES, source_label="SEC JSON",
            )
            payload = json.loads(content)
    except httpx.HTTPError as exc:
        raise UsManualError("sec_unavailable", "SEC EDGAR 数据暂不可用", 503) from exc
    except ValueError as exc:
        raise UsManualError("sec_contract_error", "SEC EDGAR 返回不是 JSON", 503) from exc
    if not isinstance(payload, dict):
        raise UsManualError("sec_contract_error", "SEC EDGAR JSON 根节点不是对象", 503)
    return payload


def _get_text(url: str) -> str:
    _validate_sec_url(url)
    try:
        with httpx.Client(timeout=25, headers=_headers(), follow_redirects=True) as client:
            _pace_sec_request()
            response = client.get(url)
            response.raise_for_status()
            _validated_response_bytes(
                response, allowed_media_types=SEC_TEXT_MEDIA_TYPES, source_label="SEC 法定文件",
            )
            return response.text
    except httpx.HTTPError as exc:
        raise UsManualError("sec_filing_unavailable", "SEC 法定文件暂不可用", 503) from exc


def _pdf_text(content: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if len(reader.pages) > ISSUER_MAX_PDF_PAGES:
            raise UsManualError("etf_issuer_pdf_too_many_pages", "发行商 PDF 页数超过安全上限", 409)
        chunks: list[str] = []
        total = 0
        for page in reader.pages:
            value = page.extract_text() or ""
            total += len(value)
            if total > ISSUER_MAX_EXTRACTED_TEXT_CHARS:
                raise UsManualError("etf_issuer_pdf_text_too_large", "发行商 PDF 提取文字超过安全上限", 409)
            chunks.append(value)
    except UsManualError:
        raise
    except Exception as exc:
        raise UsManualError("etf_issuer_pdf_invalid", "发行商 PDF 无法安全解析", 409) from exc
    text = "\n".join(chunks).strip()
    if not text:
        raise UsManualError("etf_issuer_pdf_empty", "发行商 PDF 没有可提取文字", 409)
    return text


def _get_issuer_document(source: dict[str, str]) -> dict[str, Any]:
    url = str(source.get("url") or "")
    source_type = str(source.get("source_type") or "")
    if source_type not in {"issuer_product_page", "issuer_fact_sheet", "issuer_prospectus"}:
        raise UsManualError("etf_issuer_source_type_invalid", "发行商来源类型不受支持", 422)
    _validate_issuer_url(url)
    try:
        with httpx.Client(
            timeout=25,
            headers={"User-Agent": ISSUER_USER_AGENT, "Accept": "text/html,application/pdf"},
            follow_redirects=True,
            max_redirects=3,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            _validate_issuer_url(str(response.url))
            raw_length = response.headers.get("content-length")
            if raw_length:
                try:
                    declared = int(raw_length)
                except ValueError as exc:
                    raise UsManualError("etf_issuer_contract_error", "发行商 Content-Length 无效", 503) from exc
                if declared > SEC_MAX_RESPONSE_BYTES:
                    raise UsManualError("etf_issuer_response_too_large", "发行商文档超过 8MB 安全上限", 409)
            content = response.content
            if len(content) > SEC_MAX_RESPONSE_BYTES:
                raise UsManualError("etf_issuer_response_too_large", "发行商文档超过 8MB 安全上限", 409)
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type in ISSUER_PDF_MEDIA_TYPES:
                text = _pdf_text(content)
            elif media_type in ISSUER_HTML_MEDIA_TYPES:
                text = response.text
            else:
                raise UsManualError(
                    "etf_issuer_mime_unsupported",
                    f"发行商文档 Content-Type 不受支持：{media_type or 'missing'}",
                    409,
                )
    except UsManualError:
        raise
    except httpx.HTTPError as exc:
        raise UsManualError("etf_issuer_unavailable", "发行商官方资料暂不可用", 503) from exc
    return {
        "source_type": source_type,
        "source_url": str(response.url),
        "requested_url": url,
        "media_type": media_type,
        "text": text,
        "raw": content,
        "source_effective_date": source.get("source_effective_date"),
    }


def _ticker_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    fields = payload.get("fields")
    rows: list[dict[str, Any]] = []
    if isinstance(data, list) and isinstance(fields, list):
        for raw in data:
            if isinstance(raw, list) and len(raw) == len(fields):
                rows.append(dict(zip((str(field) for field in fields), raw)))
    elif isinstance(data, list):
        rows = [dict(raw) for raw in data if isinstance(raw, dict)]
    elif all(isinstance(value, dict) for value in payload.values()):
        rows = [dict(value) for value in payload.values() if isinstance(value, dict)]
    if not rows:
        raise UsManualError("sec_contract_error", "SEC 基金 ticker 文件缺少可解析行", 503)
    return rows


def _pick(row: dict[str, Any], *names: str) -> Any:
    normalized = {re.sub(r"[^a-z0-9]", "", str(key).lower()): value for key, value in row.items()}
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in normalized and normalized[key] not in (None, ""):
            return normalized[key]
    return None


def resolve_sec_identity(payload: dict[str, Any], ticker_symbol: str) -> dict[str, Any]:
    ticker = ticker_symbol.strip().upper()
    matches = [row for row in _ticker_rows(payload) if str(_pick(row, "ticker", "symbol") or "").upper() == ticker]
    unique = {
        (
            str(_pick(row, "cik", "cik_str") or ""),
            str(_pick(row, "seriesId", "series_id") or ""),
            str(_pick(row, "classId", "class_id", "contractId") or ""),
        ): row
        for row in matches
    }
    if len(unique) != 1:
        raise UsManualError(
            "etf_identity_not_unique",
            f"SEC 中 {ticker} 的 CIK/series/class 必须唯一，实际 {len(unique)}",
            409,
        )
    (cik, series_id, class_id), raw = next(iter(unique.items()))
    if not cik or not series_id or not class_id:
        raise UsManualError("etf_identity_incomplete", f"SEC 中 {ticker} 身份字段不完整", 409)
    return {
        "ticker_symbol": ticker,
        "cik": cik.zfill(10),
        "series_id": series_id,
        "class_id": class_id,
        "class_ticker": str(_pick(raw, "ticker", "symbol") or ticker).upper(),
        "series_name": _pick(raw, "seriesName", "series_name"),
        "class_name": _pick(raw, "className", "class_name"),
        "legal_fund_name": _pick(raw, "name", "title", "companyName"),
        "raw": raw,
    }


def exposure_key(*, benchmark_family_id: str, strategy_type: str | None = None,
                 exposure_direction: str | None = None, leverage_multiplier: Any = None,
                 currency_hedge: str | None = None) -> str:
    """Return the execution-discipline key.

    H6 now admits and deduplicates an ETF once its tracking index is uniquely
    confirmed.  The legacy optional arguments stay accepted so older callers
    and immutable evidence can still be read, but they do not change the key.
    """
    del strategy_type, exposure_direction, leverage_multiplier, currency_hedge
    benchmark = benchmark_family_id.strip()
    if not benchmark or benchmark.lower() == "unknown":
        raise UsManualError("etf_benchmark_incomplete", "ETF 跟踪指数缺失或无效", 422)
    return sha256({"benchmark_family_id": benchmark})


def _plain_text(raw: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _canonical_benchmark(value: str) -> str:
    cleaned = re.sub(r"[®™©]", "", value)
    # A display-currency suffix does not change the underlying benchmark.  Do
    # not remove style, return variant, hedging, ESG, growth or equal-weight
    # words because those do change economic exposure.
    cleaned = re.sub(r"(?i)\s*\((?:USD|US\s*Dollars?)\)\s*$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;–—-")
    return cleaned


def parse_authoritative_benchmark(text: str, *, source_url: str) -> dict[str, Any]:
    """Require one tracking index; parse all other facts on a best-effort basis."""
    plain = _plain_text(text)
    if len(plain) < 200:
        raise UsManualError("etf_benchmark_source_empty", "权威资料正文不足，无法识别基准", 409)
    patterns = (
        r"(?i)(?:seeks|designed)\s+to\s+(?:track|replicate)[^.]{0,180}?(?:performance|results)\s+of\s+(?:the\s+)?([^.;]{3,140}?\bIndex)\b",
        r"(?i)(?:benchmark(?:\s+index)?|underlying\s+index)\s*(?:is|:|—|-)\s*([^.;]{3,140}?\bIndex)\b",
        r"(?i)\bETF\s+that\s+tracks\s+(?:the\s+)?([^.;]{3,140}?\bIndex)\b",
    )
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(_canonical_benchmark(match) for match in re.findall(pattern, plain))
    canonical = sorted({value.casefold(): value for value in matches}.values(), key=len)
    if len(canonical) != 1:
        raise UsManualError(
            "etf_benchmark_not_unique",
            f"权威资料中的基准指数必须唯一，实际识别 {len(canonical)} 个",
            409,
        )
    benchmark = canonical[0]
    family_id = "name::" + re.sub(r"[^a-z0-9]+", "-", benchmark.casefold()).strip("-")

    lower = plain.casefold()
    inverse = bool(re.search(r"\b(inverse|short)\b", lower))
    leveraged = bool(re.search(r"\b(leveraged|[23](?:\.0)?x|[23]\s+times)\b", lower))
    covered = "covered call" in lower
    buffered = bool(re.search(r"\b(buffer|defined outcome)\b", lower))
    active = "actively managed" in lower
    flags = [inverse or leveraged, covered, buffered, active]
    strategy = None
    if sum(bool(flag) for flag in flags) == 1:
        strategy = (
            "leveraged" if leveraged else "inverse" if inverse else "covered_call" if covered
            else "buffered" if buffered else "active"
        )
    elif not any(flags) and re.search(r"(?i)\b(?:track|replicate)s?\b", plain):
        strategy = "passive_index"
    direction = "inverse" if inverse else "long"
    leverage: Decimal | None = Decimal("1") if not (leveraged or inverse) else None
    leverage_matches = re.findall(r"(?i)\b([123](?:\.0+)?)\s*(?:x|times)\b", plain)
    if leveraged or inverse:
        values = {Decimal(value) for value in leverage_matches}
        leverage = next(iter(values)) if len(values) == 1 else None

    if re.search(r"(?i)\b(?:not|un)hedged\b|without\s+currency\s+hedg", plain):
        hedge = "none"
    elif re.search(r"(?i)\bcurrency[- ]hedged\b|hedges?\s+currency", plain):
        hedge = "hedged"
    elif re.search(r"(?i)\bU\.S\.\s+dollar\s+denominated\b", plain):
        hedge = "none"
    else:
        hedge = None

    key = exposure_key(
        benchmark_family_id=family_id,
        strategy_type=strategy,
        exposure_direction=direction,
        leverage_multiplier=leverage,
        currency_hedge=hedge,
    )
    return {
        "benchmark_name_raw": benchmark,
        "benchmark_canonical_name": benchmark,
        "benchmark_family_id": family_id,
        "strategy_type": strategy,
        "exposure_direction": direction,
        "leverage_multiplier": leverage,
        "currency_hedge": hedge,
        "exposure_key": key,
        "source_url": source_url,
        "matched_context_sha256": sha256({"benchmark": benchmark, "source": source_url}),
    }


def _recent_filings(payload: dict[str, Any]) -> list[dict[str, str]]:
    recent = ((payload.get("filings") or {}).get("recent") or {})
    if not isinstance(recent, dict):
        raise UsManualError("sec_contract_error", "SEC submissions 缺少 recent filings", 503)
    keys = ("accessionNumber", "filingDate", "form", "primaryDocument")
    columns = [recent.get(key) for key in keys]
    if any(not isinstance(column, list) for column in columns):
        raise UsManualError("sec_contract_error", "SEC submissions recent 字段不是数组", 503)
    return [dict(zip(keys, values)) for values in zip(*columns)]


def _filing_matches_identity(plain: str, *, ticker: str, series_id: str, class_id: str) -> bool:
    normalized = plain.casefold()
    def contains(token: str) -> bool:
        return bool(token) and bool(re.search(
            rf"(?<![a-z0-9]){re.escape(str(token).casefold())}(?![a-z0-9])",
            normalized,
        ))

    # A class ticker is already an exact public share-class identifier.  When
    # it is absent from a combined filing, require both SEC series and class
    # identifiers; a lone CIK-level token is not enough to bind benchmark text.
    return contains(ticker) or (contains(series_id) and contains(class_id))


class EtfBenchmarkService:
    def __init__(self, *, data_root: Path | None = None,
                 json_fetcher: Callable[[str], dict[str, Any]] = _get_json,
                 text_fetcher: Callable[[str], str] = _get_text,
                 issuer_fetcher: Callable[[dict[str, str]], dict[str, Any]] = _get_issuer_document,
                 issuer_sources: dict[str, tuple[dict[str, str], ...]] | None = None,
                 now_factory: Callable[[], datetime] | None = None):
        self.data_root = data_root or config.DATA
        self.json_fetcher = json_fetcher
        self.text_fetcher = text_fetcher
        self.issuer_fetcher = issuer_fetcher
        self.issuer_sources = ISSUER_SOURCES if issuer_sources is None else issuer_sources
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._ticker_payload_cache: dict[str, Any] | None = None

    @property
    def root(self) -> Path:
        return self.data_root / "research" / "sec" / "us_etf_benchmarks"

    def evidence_is_fresh(
        self, evidence: UsEtfBenchmarkEvidence | None, *, now: datetime | None = None,
    ) -> bool:
        checked_at = now or self.now_factory().astimezone(timezone.utc).replace(tzinfo=None)
        return bool(
            evidence is not None
            and evidence.status == "verified"
            and evidence.parser_version == PARSER_VERSION
            and evidence.benchmark_family_id
            and evidence.exposure_key
            and evidence.expires_at is not None
            and evidence.expires_at >= checked_at
        )

    def _archive(self, ticker: str, name: str, payload: Any) -> str:
        path = self.root / ticker / f"{name}-{sha256(payload)[:16]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
        return str(path)

    def _archive_text(self, ticker: str, name: str, payload: str) -> str:
        path = self.root / ticker / f"{name}-{sha256(payload)[:16]}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(payload, encoding="utf-8")
        return str(path)

    def _archive_bytes(self, ticker: str, name: str, payload: bytes, suffix: str) -> str:
        safe_suffix = suffix if suffix in {".html", ".pdf", ".txt"} else ".bin"
        path = self.root / ticker / f"{name}-{sha256(payload)[:16]}{safe_suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(payload)
        return str(path)

    def _issuer_evidence(self, ticker: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        parsed_rows: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for source in self.issuer_sources.get(ticker, ()):
            url = str(source.get("url") or "")
            try:
                document = self.issuer_fetcher(source)
                source_url = str(document.get("source_url") or url)
                _validate_issuer_url(source_url)
                raw = document.get("raw")
                text = document.get("text")
                if not isinstance(raw, bytes) or not isinstance(text, str):
                    raise UsManualError("etf_issuer_contract_error", "发行商抓取器返回结构无效", 503)
                if not _filing_matches_identity(text, ticker=ticker, series_id="", class_id=""):
                    raise UsManualError("etf_issuer_identity_mismatch", "发行商资料未精确出现 ETF ticker", 409)
                parsed = parse_authoritative_benchmark(text, source_url=source_url)
                media_type = str(document.get("media_type") or "")
                suffix = ".pdf" if media_type == "application/pdf" else ".html"
                parsed_rows.append({
                    "parsed": parsed,
                    "source_type": str(document.get("source_type") or source.get("source_type") or ""),
                    "source_url": source_url,
                    "requested_url": str(document.get("requested_url") or url),
                    "media_type": media_type,
                    "source_effective_date": document.get("source_effective_date"),
                    "content_sha256": sha256(raw),
                    "archive_path": self._archive_bytes(ticker, "issuer-source", raw, suffix),
                })
            except UsManualError as exc:
                errors.append({"url": url, "code": exc.code, "message": exc.message})
        return parsed_rows, errors

    def _identity(self, session: Session, ticker: str, ticker_payload: dict[str, Any]) -> UsEtfIdentity:
        resolved = resolve_sec_identity(ticker_payload, ticker)
        # Version this ticker's exact resolved identity, not the whole SEC
        # global file: an unrelated fund changing must not supersede every ETF.
        content_hash = sha256({key: value for key, value in resolved.items() if key != "raw"})
        source_hash = sha256(ticker_payload)
        current = repository.latest_etf_identity(session, ticker)
        if current is not None and current.content_sha256 == content_hash and current.status == "verified":
            return current
        if current is not None:
            current.is_active = False
            session.add(current)
        version = int(current.version) + 1 if current is not None else 1
        now = self.now_factory().astimezone(timezone.utc).replace(tzinfo=None)
        row = UsEtfIdentity(
            identity_id=f"us-etf-id-{ticker}-{uuid4().hex[:12]}",
            ticker_symbol=ticker,
            version=version,
            status="verified",
            cik=resolved["cik"], series_id=resolved["series_id"], class_id=resolved["class_id"],
            class_ticker=resolved["class_ticker"], series_name=resolved["series_name"],
            class_name=resolved["class_name"], legal_fund_name=resolved["legal_fund_name"],
            source_url=SEC_TICKERS_URL,
            content_sha256=content_hash,
            evidence_json={
                "resolved": resolved,
                "source_sha256": source_hash,
                "archive_path": self._archive(ticker, "identity-source", ticker_payload),
            },
            retrieved_at=now,
            expires_at=now + timedelta(days=config.US_MANUAL_ETF_EVIDENCE_MAX_AGE_DAYS),
        )
        return repository.save_etf_identity(session, row)

    def _ticker_payload(self) -> dict[str, Any]:
        if self._ticker_payload_cache is not None:
            return self._ticker_payload_cache
        index_path = self.root / "_shared" / "company-tickers-current.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            retrieved_at = datetime.fromisoformat(str(index["retrieved_at"]).replace("Z", "+00:00"))
            if retrieved_at.tzinfo is None:
                retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
            archive_path = Path(str(index["archive_path"])).resolve()
            archive_root = self.root.resolve()
            if (
                archive_path.is_relative_to(archive_root)
                and archive_path.is_file()
                and self.now_factory().astimezone(timezone.utc) - retrieved_at <= timedelta(days=31)
            ):
                payload = json.loads(archive_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and sha256(payload) == index.get("content_sha256"):
                    self._ticker_payload_cache = payload
                    return self._ticker_payload_cache
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            # A corrupt cache is never trusted; refresh from the authoritative
            # SEC URL and replace only the small local pointer after archiving.
            pass
        payload = self.json_fetcher(SEC_TICKERS_URL)
        archive_path = self._archive("_shared", "company-tickers", payload)
        index_path.parent.mkdir(parents=True, exist_ok=True)
        pointer = {
            "source_url": SEC_TICKERS_URL,
            "retrieved_at": self.now_factory().astimezone(timezone.utc).isoformat(),
            "content_sha256": sha256(payload),
            "archive_path": archive_path,
        }
        temporary = index_path.with_suffix(".tmp")
        temporary.write_text(canonical_json(pointer) + "\n", encoding="utf-8")
        temporary.replace(index_path)
        self._ticker_payload_cache = payload
        return self._ticker_payload_cache

    def refresh(self, session: Session, *, ticker_symbol: str, force: bool = False) -> dict[str, Any]:
        ticker = ticker_symbol.strip().upper()
        if not ticker or not re.fullmatch(r"[A-Z0-9.]{1,12}", ticker):
            raise UsManualError("etf_ticker_invalid", "美国 ETF ticker 格式无效", 422)
        now = self.now_factory().astimezone(timezone.utc).replace(tzinfo=None)
        cached = repository.latest_etf_benchmark(session, ticker)
        if (
            not force and cached is not None
            and cached.parser_version == PARSER_VERSION
            and cached.expires_at is not None
            and cached.expires_at >= now
        ):
            return self.payload(session, cached)
        ticker_payload = self._ticker_payload()
        identity = self._identity(session, ticker, ticker_payload)
        issuer_rows, issuer_errors = self._issuer_evidence(ticker)
        if issuer_rows:
            fingerprint_keys = {
                row["parsed"]["benchmark_family_id"]
                for row in issuer_rows
            }
            source_hash = sha256([
                {
                    "source_type": row["source_type"],
                    "source_url": row["source_url"],
                    "content_sha256": row["content_sha256"],
                    "parsed": serialize(row["parsed"]),
                }
                for row in issuer_rows
            ])
            current = repository.latest_etf_benchmark(session, ticker)
            source_conflict = len(fingerprint_keys) != 1
            proposed_issuer = issuer_rows[0]["parsed"] if not source_conflict else None
            mapping_changed = bool(
                proposed_issuer
                and current is not None
                and current.status == "verified"
                and current.benchmark_family_id
                and (
                    current.identity_id != identity.identity_id
                    or current.benchmark_family_id != proposed_issuer["benchmark_family_id"]
                )
            )
            status = (
                "verified"
                if proposed_issuer is not None and not mapping_changed
                else "benchmark_review_required"
            )
            if (
                current is not None
                and current.identity_id == identity.identity_id
                and current.status == status
                and current.parser_version == PARSER_VERSION
                and current.content_sha256 == source_hash
                and current.expires_at is not None
                and current.expires_at >= now
            ):
                return self.payload(session, current)
            version = int(current.version) + 1 if current is not None else 1
            selected_issuer = issuer_rows[0]
            parsed_issuer = proposed_issuer if status == "verified" else None
            evidence = UsEtfBenchmarkEvidence(
                evidence_id=f"us-etf-evidence-{ticker}-{uuid4().hex[:12]}",
                identity_id=identity.identity_id,
                ticker_symbol=ticker,
                version=version,
                status=status,
                benchmark_family_id=(parsed_issuer or {}).get("benchmark_family_id"),
                benchmark_name_raw=(parsed_issuer or {}).get("benchmark_name_raw"),
                benchmark_canonical_name=(parsed_issuer or {}).get("benchmark_canonical_name"),
                strategy_type=(parsed_issuer or {}).get("strategy_type"),
                exposure_direction=(parsed_issuer or {}).get("exposure_direction"),
                leverage_multiplier=(parsed_issuer or {}).get("leverage_multiplier"),
                currency_hedge=(parsed_issuer or {}).get("currency_hedge"),
                exposure_key=(parsed_issuer or {}).get("exposure_key"),
                source_type=(
                    selected_issuer["source_type"] if status == "verified"
                    else (
                        "issuer_mapping_changed"
                        if mapping_changed else "issuer_source_conflict"
                    )
                ),
                source_url=selected_issuer["source_url"],
                source_effective_date=selected_issuer.get("source_effective_date"),
                content_sha256=source_hash,
                parser_version=PARSER_VERSION,
                evidence_json={
                    "identity_id": identity.identity_id,
                    "issuer_sources": [
                        {
                            **{key: value for key, value in row.items() if key != "parsed"},
                            "parsed": serialize(row["parsed"]),
                        }
                        for row in issuer_rows
                    ],
                    "issuer_errors": issuer_errors,
                    "conflict": source_conflict,
                    "mapping_changed": mapping_changed,
                    "proposed_fingerprint": serialize(proposed_issuer),
                },
                supersedes_evidence_id=current.evidence_id if current else None,
                retrieved_at=now,
                expires_at=now + timedelta(days=config.US_MANUAL_ETF_EVIDENCE_MAX_AGE_DAYS),
            )
            if current is not None:
                current.is_active = False
                session.add(current)
            saved = repository.save_etf_benchmark(session, evidence)
            return self.payload(session, saved)
        submissions_url = SEC_SUBMISSIONS_URL.format(cik=str(identity.cik).zfill(10))
        submissions = self.json_fetcher(submissions_url)
        candidates = [row for row in _recent_filings(submissions) if row.get("form") in SEC_FORMS]
        errors: list[dict[str, str]] = list(issuer_errors)
        parsed: dict[str, Any] | None = None
        selected: dict[str, str] | None = None
        selected_text = ""
        cik_plain = str(identity.cik).lstrip("0") or "0"
        for filing in candidates[:30]:
            accession = str(filing.get("accessionNumber") or "")
            document = str(filing.get("primaryDocument") or "")
            if not accession or not document:
                continue
            url = SEC_ARCHIVE_URL.format(
                cik=cik_plain, accession=accession.replace("-", ""), document=document,
            )
            try:
                raw = self.text_fetcher(url)
                plain = _plain_text(raw)
                if not _filing_matches_identity(
                    plain,
                    ticker=ticker,
                    series_id=str(identity.series_id),
                    class_id=str(identity.class_id),
                ):
                    continue
                parsed = parse_authoritative_benchmark(raw, source_url=url)
                selected = filing
                selected_text = raw
                break
            except UsManualError as exc:
                errors.append({"url": url, "code": exc.code, "message": exc.message})
        current = repository.latest_etf_benchmark(session, ticker)
        version = int(current.version) + 1 if current is not None else 1
        if parsed is None or selected is None:
            status = "benchmark_review_required"
            content_hash = sha256({"submissions": submissions, "errors": errors})
            if (
                current is not None
                and current.identity_id == identity.identity_id
                and current.status == status
                and current.parser_version == PARSER_VERSION
                and current.content_sha256 == content_hash
                and current.expires_at is not None
                and current.expires_at >= now
            ):
                return self.payload(session, current)
            evidence = UsEtfBenchmarkEvidence(
                evidence_id=f"us-etf-evidence-{ticker}-{uuid4().hex[:12]}",
                identity_id=identity.identity_id, ticker_symbol=ticker, version=version,
                status=status, source_type="sec_filing_search", source_url=submissions_url,
                content_sha256=content_hash,
                parser_version=PARSER_VERSION,
                evidence_json={
                    "identity_id": identity.identity_id,
                    "candidate_filings": candidates[:30],
                    "errors": errors,
                    "archive_path": self._archive(ticker, "submissions", submissions),
                },
                supersedes_evidence_id=current.evidence_id if current else None,
                retrieved_at=now,
                expires_at=now + timedelta(days=config.US_MANUAL_ETF_EVIDENCE_MAX_AGE_DAYS),
            )
        else:
            accession = str(selected["accessionNumber"])
            content_hash = sha256(selected_text)
            mapping_changed = bool(
                current is not None
                and current.status == "verified"
                and current.benchmark_family_id
                and (
                    current.identity_id != identity.identity_id
                    or current.benchmark_family_id != parsed["benchmark_family_id"]
                )
            )
            if (
                current is not None
                and current.identity_id == identity.identity_id
                and current.status == (
                    "benchmark_review_required" if mapping_changed else "verified"
                )
                and current.parser_version == PARSER_VERSION
                and current.content_sha256 == content_hash
                and current.expires_at is not None
                and current.expires_at >= now
            ):
                return self.payload(session, current)
            evidence = UsEtfBenchmarkEvidence(
                evidence_id=f"us-etf-evidence-{ticker}-{uuid4().hex[:12]}",
                identity_id=identity.identity_id, ticker_symbol=ticker, version=version,
                status="benchmark_review_required" if mapping_changed else "verified",
                benchmark_family_id=None if mapping_changed else parsed["benchmark_family_id"],
                benchmark_name_raw=None if mapping_changed else parsed["benchmark_name_raw"],
                benchmark_canonical_name=None if mapping_changed else parsed["benchmark_canonical_name"],
                strategy_type=None if mapping_changed else parsed["strategy_type"],
                exposure_direction=None if mapping_changed else parsed["exposure_direction"],
                leverage_multiplier=None if mapping_changed else parsed["leverage_multiplier"],
                currency_hedge=None if mapping_changed else parsed["currency_hedge"],
                exposure_key=None if mapping_changed else parsed["exposure_key"],
                source_type="sec_mapping_changed" if mapping_changed else "sec_filing",
                source_url=parsed["source_url"], filing_accession_no=accession,
                source_effective_date=selected.get("filingDate"), content_sha256=content_hash,
                parser_version=PARSER_VERSION,
                evidence_json={
                    "identity_id": identity.identity_id,
                    "filing": selected,
                    # SQL JSON columns cannot persist Decimal directly.  Keep the
                    # evidence snapshot lossless and deterministic by using the
                    # same string-based Decimal encoding as the public API.
                    "parsed": serialize(parsed),
                    "mapping_changed": mapping_changed,
                    "proposed_fingerprint": serialize(parsed) if mapping_changed else None,
                    "archive_path": self._archive_text(ticker, "filing", selected_text),
                },
                supersedes_evidence_id=current.evidence_id if current else None,
                retrieved_at=now,
                expires_at=now + timedelta(days=config.US_MANUAL_ETF_EVIDENCE_MAX_AGE_DAYS),
            )
        if current is not None:
            current.is_active = False
            session.add(current)
        saved = repository.save_etf_benchmark(session, evidence)
        return self.payload(session, saved)

    def payload(self, session: Session, evidence: UsEtfBenchmarkEvidence) -> dict[str, Any]:
        result = repository.model_payload(evidence)
        result["identity"] = repository.model_payload(repository.get_etf_identity(session, evidence.identity_id))
        result["fingerprint"] = {
            "benchmark_family_id": evidence.benchmark_family_id,
            "strategy_type": evidence.strategy_type,
            "exposure_direction": evidence.exposure_direction,
            "leverage_multiplier": decimal_text(evidence.leverage_multiplier),
            "currency_hedge": evidence.currency_hedge,
            "exposure_key": evidence.exposure_key,
        }
        return result

    def census(
        self,
        session: Session,
        *,
        ticker_symbols: list[str],
        source_meta: dict[str, Any],
        refresh_missing: bool = False,
        batch_size: int = 5,
        start_after: str | None = None,
    ) -> dict[str, Any]:
        """Audit or advance the low-frequency archived ETF intersection.

        This never calls Trend Animals and is intentionally separate from the
        daily warm-to-hot state machine.  A batch is resumable: already fresh
        evidence is never downloaded again, while failed candidates remain
        explicit instead of being dropped from the census.
        """
        if isinstance(batch_size, bool) or batch_size < 1 or batch_size > 10:
            raise UsManualError("etf_census_batch_invalid", "ETF 普查每批必须为 1–10 只", 422)
        tickers = sorted({str(value).strip().upper() for value in ticker_symbols})
        if not tickers or any(not re.fullmatch(r"[A-Z0-9.]{1,12}", value) for value in tickers):
            raise UsManualError("etf_census_tickers_invalid", "ETF 普查 ticker 清单为空或格式无效", 422)
        cursor = str(start_after or "").strip().upper() or None
        if cursor is not None and not re.fullmatch(r"[A-Z0-9.]{1,12}", cursor):
            raise UsManualError("etf_census_cursor_invalid", "ETF 普查游标格式无效", 422)
        now = self.now_factory().astimezone(timezone.utc).replace(tzinfo=None)

        def state(ticker: str) -> str:
            evidence = repository.latest_etf_benchmark(session, ticker)
            if evidence is None:
                return "missing"
            if evidence.expires_at is None or evidence.expires_at < now:
                return "stale"
            return "verified" if self.evidence_is_fresh(evidence, now=now) else "blocked"

        before = {ticker: state(ticker) for ticker in tickers}
        refresh_targets = [ticker for ticker in tickers if before[ticker] in {"missing", "stale"}]
        eligible_targets = [
            ticker for ticker in refresh_targets
            if cursor is None or ticker > cursor
        ]
        batch_targets = eligible_targets[:batch_size]
        attempts: list[dict[str, Any]] = []
        if refresh_missing:
            for ticker in batch_targets:
                try:
                    result = self.refresh(
                        session,
                        ticker_symbol=ticker,
                        force=before[ticker] == "stale",
                    )
                    attempts.append({
                        "ticker_symbol": ticker,
                        "status": result["status"],
                        "evidence_id": result["evidence_id"],
                    })
                except UsManualError as exc:
                    attempts.append({"ticker_symbol": ticker, "status": "error", "error": exc.as_payload()})
        after = {ticker: state(ticker) for ticker in tickers}
        counts = {
            name: sum(1 for value in after.values() if value == name)
            for name in ("verified", "blocked", "stale", "missing")
        }
        pending = [ticker for ticker in tickers if after[ticker] in {"missing", "stale"}]
        pass_complete = bool(refresh_missing and len(eligible_targets) <= len(batch_targets))
        next_after = (
            batch_targets[-1]
            if refresh_missing and batch_targets and not pass_complete
            else None
        )
        report = {
            "schema_version": 1,
            "scope": "us-etf-benchmark-census",
            "source": serialize(source_meta),
            "ticker_count": len(tickers),
            "ticker_symbols": tickers,
            "refresh_missing": refresh_missing,
            "batch_size": batch_size,
            "start_after": cursor,
            "next_after": next_after,
            "pass_complete": pass_complete,
            "before": before,
            "attempts": attempts,
            "after": after,
            "counts": counts,
            "pending_tickers": pending,
            "complete": not pending,
            "manual_only": True,
            "trend_animals_paid_calls": 0,
            "generated_at": self.now_factory().astimezone(timezone.utc).isoformat(),
        }
        report["archive_path"] = self._archive(
            "_census", f"census-{str(source_meta.get('as_of_date') or 'unknown')}", report,
        )
        return report

    def review(
        self, session: Session, *, evidence_id: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Record a review without ever manufacturing a verified fingerprint."""
        evidence = repository.get_etf_benchmark(session, evidence_id)
        key = str(payload.get("idempotency_key") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        resolution = str(payload.get("resolution") or "").strip()
        if not key or len(key) > 160:
            raise UsManualError("idempotency_key_required", "ETF 复核需要长度不超过 160 的幂等键", 422)
        if not reason or len(reason) > 1000:
            raise UsManualError("etf_review_reason_required", "ETF 复核理由必须为 1–1000 字", 422)
        if resolution not in {"reject", "request_refresh", "confirm_authoritative_evidence"}:
            raise UsManualError(
                "etf_review_resolution_invalid",
                "ETF 复核只能驳回、请求刷新或确认已有权威证据",
                422,
            )
        if resolution == "confirm_authoritative_evidence" and evidence.status != "verified":
            raise UsManualError(
                "etf_review_cannot_verify_missing_source",
                "人工文字不能把缺失/冲突证据改成 verified；请先补充权威来源",
                409,
            )
        replay = repository.etf_review_by_idempotency(session, key)
        if replay is not None:
            if (
                replay.evidence_id != evidence.evidence_id
                or replay.resolution != resolution
                or replay.reason != reason
            ):
                raise UsManualError(
                    "idempotency_conflict",
                    "同一 ETF 复核幂等键已用于不同请求",
                    409,
                )
            return {
                "review": repository.model_payload(replay),
                "evidence": self.payload(session, evidence),
                "execution_status": (
                    "verified" if evidence.status == "verified"
                    else "blocked_pending_authoritative_source"
                ),
            }
        review = repository.save_etf_review(session, UsEtfBenchmarkReview(
            review_id=f"us-etf-review-{uuid4().hex[:16]}",
            evidence_id=evidence.evidence_id,
            idempotency_key=key,
            resolution=resolution,
            reason=reason,
            evidence_json={
                "benchmark_status_unchanged": evidence.status,
                "free_text_cannot_create_fingerprint": True,
            },
        ))
        return {
            "review": repository.model_payload(review),
            "evidence": self.payload(session, evidence),
            "execution_status": (
                "verified" if evidence.status == "verified" else "blocked_pending_authoritative_source"
            ),
        }

    def apply_candidates(self, session: Session, candidates: list[Any]) -> dict[str, int]:
        counts = {"verified": 0, "missing": 0, "stale": 0, "duplicate": 0}
        now = utc_now()
        eligible_etfs: list[Any] = []
        for row in candidates:
            if row.asset_type != "etf":
                row.benchmark_status = "not_applicable"
                continue
            evidence = repository.latest_etf_benchmark(session, row.ticker_symbol)
            if evidence is None:
                row.benchmark_status = "benchmark_review_required"
                row.screen_status = "observe"
                row.primary_reason = "etf_benchmark_missing"
                row.all_reasons = [*list(row.all_reasons or []), "etf_benchmark_missing"]
                counts["missing"] += 1
                continue
            identity = repository.get_etf_identity(session, evidence.identity_id)
            row.etf_identity_id = identity.identity_id
            row.etf_benchmark_evidence_id = evidence.evidence_id
            row.benchmark_family_id = evidence.benchmark_family_id
            row.exposure_key = evidence.exposure_key
            expired = evidence.expires_at is not None and evidence.expires_at < now
            contract_stale = evidence.parser_version != PARSER_VERSION
            if not self.evidence_is_fresh(evidence, now=now):
                row.benchmark_status = "stale" if expired or contract_stale else evidence.status
                row.screen_status = "observe"
                row.primary_reason = (
                    "etf_benchmark_stale"
                    if expired or contract_stale else "etf_benchmark_review_required"
                )
                row.all_reasons = [*list(row.all_reasons or []), row.primary_reason]
                counts["stale" if expired or contract_stale else "missing"] += 1
                continue
            row.benchmark_status = "verified"
            counts["verified"] += 1
            if row.screen_status == "ready":
                eligible_etfs.append(row)

        grouped: dict[str, list[Any]] = {}
        for row in eligible_etfs:
            grouped.setdefault(str(row.exposure_key), []).append(row)
        for same_exposure in grouped.values():
            same_exposure.sort(key=lambda row: (
                -(row.strength_local if row.strength_local is not None else Decimal("-999999")),
                -(row.amount_1d if row.amount_1d is not None else Decimal("-999999")),
                -(row.market_cap if row.market_cap is not None else Decimal("-999999")),
                row.ticker_symbol,
                row.tm_id,
            ))
            winner = same_exposure[0]
            for duplicate in same_exposure[1:]:
                duplicate.screen_status = "observe"
                duplicate.primary_reason = "duplicate_etf_exposure"
                duplicate.all_reasons = [*list(duplicate.all_reasons or []), "duplicate_etf_exposure"]
                duplicate.raw_fields = {
                    **dict(duplicate.raw_fields or {}),
                    "duplicate_etf_exposure": {
                        "winner": winner.ticker_symbol,
                        "exposure_key": duplicate.exposure_key,
                        "evidence_id": duplicate.etf_benchmark_evidence_id,
                    },
                }
                counts["duplicate"] += 1
        repository.save_candidates(session, candidates)
        return counts
