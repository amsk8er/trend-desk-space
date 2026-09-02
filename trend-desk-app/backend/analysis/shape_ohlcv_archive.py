"""Kova 形态研究 OHLCV 的不可变本地档案 writer。

该模块不包含 provider、数据库或交易服务入口。调用方必须先完成采集和标准化，
writer 只负责重新校验证据合同、计算哈希并以 create-if-absent 语义落盘。
"""
from __future__ import annotations

from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4


CONTRACT = "kova_shape_ohlcv_archive_v1"
MARKETS = {"a_share", "us_h6"}
SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


class ArchiveValidationError(ValueError):
    """档案内容不满足 v1 证据合同。"""


class ArchiveCollisionError(RuntimeError):
    """同一不可变路径已经存在不同内容。"""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def observation_key_hash(*, market: str, as_of: str, identity: dict[str, Any]) -> str:
    _validate_market_date(market, as_of)
    if not isinstance(identity, dict) or not identity:
        raise ArchiveValidationError("observation identity must be a non-empty object")
    return sha256({
        "contract": CONTRACT,
        "market": market,
        "as_of": as_of,
        "observation_identity": identity,
    }).removeprefix("sha256:")


def _validate_market_date(market: str, as_of: str) -> None:
    if market not in MARKETS:
        raise ArchiveValidationError(f"unsupported archive market: {market}")
    try:
        parsed = date.fromisoformat(as_of)
    except (TypeError, ValueError) as exc:
        raise ArchiveValidationError("archive as_of must be an ISO date") from exc
    if parsed.isoformat() != as_of:
        raise ArchiveValidationError("archive as_of must be canonical YYYY-MM-DD")


def _positive_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return numeric > 0 and numeric not in {float("inf"), float("-inf")}


def _validate_bar(bar: dict[str, Any], *, as_of: str) -> str:
    if not isinstance(bar, dict):
        raise ArchiveValidationError("normalized bar must be an object")
    session = str(bar.get("date") or bar.get("session") or "")
    try:
        parsed = date.fromisoformat(session)
    except ValueError as exc:
        raise ArchiveValidationError("normalized bar date is invalid") from exc
    if session > as_of:
        raise ArchiveValidationError("future bar is not allowed")
    for field in ("open", "high", "low", "close", "volume"):
        if not _positive_number(bar.get(field)):
            raise ArchiveValidationError(f"normalized bar {field} must be finite and positive")
    opened = float(bar["open"])
    closed = float(bar["close"])
    high = float(bar["high"])
    low = float(bar["low"])
    if low > min(opened, closed) or high < max(opened, closed) or low > high:
        raise ArchiveValidationError("normalized bar OHLC relationship is invalid")
    return parsed.isoformat()


def _validate_complete_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ArchiveValidationError("archive payload must be an object")
    if payload.get("contract") != CONTRACT or payload.get("status") != "complete":
        raise ArchiveValidationError("complete archive contract or status is invalid")
    market = str(payload.get("market") or "")
    observation = payload.get("observation")
    if not isinstance(observation, dict):
        raise ArchiveValidationError("archive observation must be an object")
    as_of = str(observation.get("as_of") or "")
    identity = observation.get("identity")
    _validate_market_date(market, as_of)
    digest = observation_key_hash(market=market, as_of=as_of, identity=identity)

    source = payload.get("source_contract")
    if not isinstance(source, dict) or not str(source.get("cohort") or ""):
        raise ArchiveValidationError("source cohort is required")
    if not str(source.get("provider") or ""):
        raise ArchiveValidationError("source provider is required")
    validation = payload.get("validation")
    if not isinstance(validation, dict):
        raise ArchiveValidationError("validation evidence is required")
    required_flags = {
        "future_data_used": False,
        "duplicate_timestamps": False,
        "single_source_cohort": True,
        "session_complete": True,
    }
    for field, expected in required_flags.items():
        if validation.get(field) is not expected:
            raise ArchiveValidationError(f"validation flag {field} must be {expected}")

    raw = payload.get("raw_evidence")
    if not isinstance(raw, dict) or not raw:
        raise ArchiveValidationError("raw evidence is required")
    if not str(raw.get("raw_payload_hash") or "").startswith("sha256:"):
        raise ArchiveValidationError("raw payload hash is required")

    normalized = payload.get("normalized")
    if not isinstance(normalized, dict):
        raise ArchiveValidationError("normalized evidence is required")
    bars = normalized.get("bars")
    if not isinstance(bars, list) or len(bars) != 60:
        raise ArchiveValidationError("complete archive requires exactly 60 normalized sessions")
    sessions = [_validate_bar(bar, as_of=as_of) for bar in bars]
    if sessions != sorted(sessions) or len(set(sessions)) != len(sessions):
        raise ArchiveValidationError("normalized sessions must be unique and ascending")
    if sessions[-1] != as_of:
        raise ArchiveValidationError("normalized window must end exactly at as_of")
    if normalized.get("bar_count") != 60:
        raise ArchiveValidationError("normalized bar_count must equal 60")
    if normalized.get("first_session") != sessions[0] or normalized.get("last_session") != sessions[-1]:
        raise ArchiveValidationError("normalized session bounds do not match bars")
    actual_bars_hash = sha256(bars)
    declared_bars_hash = normalized.get("normalized_bars_hash")
    if declared_bars_hash != actual_bars_hash:
        raise ArchiveValidationError("normalized bars hash mismatch")

    if market == "a_share":
        rows = raw.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ArchiveValidationError("A-share archive requires raw rows")
        if source.get("cohort") == "a_share_tushare_raw_adj_v1" and any(
            not isinstance(row, dict) or not _positive_number(row.get("adj_factor"))
            for row in rows
        ):
            raise ArchiveValidationError(
                "Tushare cohort requires object rows with a positive adj_factor",
            )
    else:
        if source.get("instrument_semantic") != "bitget_rtoken_usdt_proxy":
            raise ArchiveValidationError("US H6 archive must disclose rToken proxy semantics")
        if not isinstance(raw.get("daily_1d"), list) or not isinstance(raw.get("hourly_1h"), list):
            raise ArchiveValidationError("US H6 archive requires raw 1D and 1H evidence")

    candidate_hash = observation.get("candidate_snapshot_hash")
    if not str(candidate_hash or "").startswith("sha256:"):
        raise ArchiveValidationError("candidate snapshot hash is required")
    query = payload.get("query")
    if not isinstance(query, dict) or not str(query.get("request_spec_hash") or "").startswith("sha256:"):
        raise ArchiveValidationError("request spec hash is required")
    algorithm = payload.get("algorithm")
    if not isinstance(algorithm, dict) or not algorithm.get("normalizer_version"):
        raise ArchiveValidationError("normalizer version is required")

    enriched = dict(payload)
    enriched["observation_key_hash"] = digest
    enriched.pop("artifact_hash", None)
    enriched["artifact_hash"] = sha256(enriched)
    return enriched


def _safe_attempt_id(attempt_id: str) -> str:
    if not SAFE_ID.fullmatch(attempt_id):
        raise ArchiveValidationError("attempt_id contains unsafe characters")
    return attempt_id


def _write_immutable(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    content = canonical_json(payload) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ArchiveCollisionError(f"cannot verify existing archive: {path}") from exc
        if existing == content:
            return {"status": "reused_verified", "path": str(path), "artifact_hash": payload["artifact_hash"]}
        raise ArchiveCollisionError(f"immutable archive collision: {path}")

    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = path.read_text(encoding="utf-8")
            if existing != content:
                raise ArchiveCollisionError(f"immutable archive collision: {path}")
            return {"status": "reused_verified", "path": str(path), "artifact_hash": payload["artifact_hash"]}
        return {"status": "created", "path": str(path), "artifact_hash": payload["artifact_hash"]}
    finally:
        temporary.unlink(missing_ok=True)


def write_complete_observation(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """校验并写入成功观测；相同内容复用，不同内容碰撞失败。"""
    enriched = _validate_complete_payload(payload)
    market = enriched["market"]
    as_of = enriched["observation"]["as_of"]
    digest = enriched["observation_key_hash"]
    path = root / "observations" / market / as_of / f"{digest}.json"
    return _write_immutable(path, enriched)


def write_failed_attempt(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """写入不可变失败 attempt；失败永远不会占用成功观测路径。"""
    if not isinstance(payload, dict) or payload.get("contract") != CONTRACT:
        raise ArchiveValidationError("failed attempt contract is invalid")
    if payload.get("status") != "failed":
        raise ArchiveValidationError("failed attempt status must be failed")
    market = str(payload.get("market") or "")
    as_of = str(payload.get("as_of") or "")
    _validate_market_date(market, as_of)
    attempt_id = _safe_attempt_id(str(payload.get("attempt_id") or ""))
    if not str(payload.get("reason_code") or "") or not str(payload.get("stage") or ""):
        raise ArchiveValidationError("failed attempt requires reason_code and stage")
    enriched = dict(payload)
    enriched.pop("artifact_hash", None)
    enriched["artifact_hash"] = sha256(enriched)
    path = root / "attempts" / market / as_of / f"{attempt_id}.json"
    return _write_immutable(path, enriched)


def verified_observation_exists(
    root: Path,
    *,
    market: str,
    as_of: str,
    observation_hash: str,
) -> bool:
    """严格验证既有成功档案，供零网络 dry-run 判断是否可复用。"""
    _validate_market_date(market, as_of)
    if not re.fullmatch(r"[0-9a-f]{64}", observation_hash):
        return False
    path = root / "observations" / market / as_of / f"{observation_hash}.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        validated = _validate_complete_payload(payload)
    except (OSError, ValueError, TypeError):
        return False
    return (
        validated.get("observation_key_hash") == observation_hash
        and payload.get("artifact_hash") == validated.get("artifact_hash")
    )
