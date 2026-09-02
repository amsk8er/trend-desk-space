"""完整结果集的稳定服务端排序与不透明分页游标。"""
from __future__ import annotations

import base64
import binascii
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
import hashlib
import hmac
import json
from typing import Any, Iterable

from .contracts import FIELD_AVAILABLE, SORT_SPECS, UsRightSideError


TEMPERATURE_ORDER = {name: rank for rank, name in enumerate(("冻", "寒", "凉", "平", "温", "热", "沸"))}
PHASE_ORDER = {name: rank for rank, name in enumerate(("谷雨", "立夏", "夏至", "小暑", "大暑"))}


def _state(row: dict, key: str) -> str:
    states = row.get("field_states") or row.get("field_states_json") or {}
    explicit = states.get(key)
    if explicit:
        return str(explicit)
    return FIELD_AVAILABLE if row.get(key) is not None else "not_returned"


def _text(value: Any) -> str:
    return str(value or "").casefold()


def _number(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _category(row: dict, *, value_key: str, state_key: str | None, kind: str) -> int:
    state = _state(row, state_key or value_key)
    value = row.get(value_key)
    if state != FIELD_AVAILABLE or value is None:
        return 2
    if kind == "temperature" and str(value) not in TEMPERATURE_ORDER:
        return 1
    if kind == "phase" and str(value) not in PHASE_ORDER:
        return 1
    return 0


def _compare_values(left: Any, right: Any, *, kind: str) -> int:
    if kind == "number":
        a, b = _number(left), _number(right)
    elif kind == "temperature":
        a, b = TEMPERATURE_ORDER.get(str(left), -1), TEMPERATURE_ORDER.get(str(right), -1)
    elif kind == "phase":
        a, b = PHASE_ORDER.get(str(left), -1), PHASE_ORDER.get(str(right), -1)
    else:
        a, b = _text(left), _text(right)
    return (a > b) - (a < b)


def sort_assets(rows: Iterable[dict], *, sort_by: str, sort_dir: str) -> list[dict]:
    spec = SORT_SPECS.get(sort_by)
    if spec is None:
        raise UsRightSideError("invalid_sort_field", f"不支持排序字段：{sort_by}", status_code=422)
    if sort_dir not in {"asc", "desc"}:
        raise UsRightSideError("invalid_sort_direction", "排序方向必须是 asc 或 desc", status_code=422)

    def compare(left: dict, right: dict) -> int:
        left_category = _category(
            left, value_key=spec.value_key, state_key=spec.state_key, kind=spec.kind)
        right_category = _category(
            right, value_key=spec.value_key, state_key=spec.state_key, kind=spec.kind)
        if left_category != right_category:
            return -1 if left_category < right_category else 1
        if left_category < 2:
            result = _compare_values(left.get(spec.value_key), right.get(spec.value_key), kind=spec.kind)
            if result and sort_dir == "desc":
                result *= -1
            if result:
                return result
        # 未知/空值的细分状态不改变“空值永远在末尾”，只提供稳定性。
        if left_category == 2:
            left_state = _state(left, spec.state_key or spec.value_key)
            right_state = _state(right, spec.state_key or spec.value_key)
            if left_state != right_state:
                return -1 if left_state < right_state else 1
        left_symbol, right_symbol = _text(left.get("ticker_symbol")), _text(right.get("ticker_symbol"))
        if left_symbol != right_symbol:
            return -1 if left_symbol < right_symbol else 1
        left_id, right_id = int(left.get("tm_id") or 0), int(right.get("tm_id") or 0)
        return (left_id > right_id) - (left_id < right_id)

    return sorted((dict(row) for row in rows), key=cmp_to_key(compare))


def filters_hash(filters: dict[str, Any]) -> str:
    normalized = {key: value for key, value in sorted(filters.items()) if value not in (None, "", [], ())}
    return hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def cursor_binding(*, run_id: str, filters_digest: str, sort_by: str, sort_dir: str) -> str:
    return hashlib.sha256(
        f"{run_id}|{filters_digest}|{sort_by}|{sort_dir}".encode()
    ).hexdigest()


def cursor_anchor(row: dict, *, sort_by: str) -> dict:
    spec = SORT_SPECS[sort_by]
    category = _category(
        row, value_key=spec.value_key, state_key=spec.state_key, kind=spec.kind)
    raw = row.get(spec.value_key)
    if spec.kind == "number" and raw is not None:
        normalized: str | None = format(_number(raw), "f")
    elif spec.kind == "text":
        normalized = _text(raw)
    else:
        normalized = str(raw) if raw is not None else None
    return {
        "empty_bucket": category,
        "sort_value": normalized,
        "field_state": _state(row, spec.state_key or spec.value_key),
        "ticker_symbol": str(row.get("ticker_symbol") or "").upper(),
        "tm_id": int(row.get("tm_id") or 0),
    }


def _signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def encode_cursor(*, anchor: dict, binding: str, secret: str) -> str:
    body = json.dumps(
        {"v": 1, "binding": binding, "anchor": anchor},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    payload = json.dumps(
        {"body": base64.urlsafe_b64encode(body).decode().rstrip("="),
         "sig": _signature(body, secret)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str | None, *, binding: str, secret: str) -> dict | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        envelope = json.loads(base64.urlsafe_b64decode(padded).decode())
        encoded_body = str(envelope["body"])
        body = base64.urlsafe_b64decode(encoded_body + "=" * (-len(encoded_body) % 4))
        if not hmac.compare_digest(str(envelope["sig"]), _signature(body, secret)):
            raise ValueError
        payload = json.loads(body.decode())
        anchor = payload["anchor"]
        if payload.get("v") != 1 or payload["binding"] != binding or not isinstance(anchor, dict):
            raise ValueError
        required = {"empty_bucket", "sort_value", "field_state", "ticker_symbol", "tm_id"}
        if set(anchor) != required:
            raise ValueError
        anchor["empty_bucket"] = int(anchor["empty_bucket"])
        anchor["tm_id"] = int(anchor["tm_id"])
        return anchor
    except (ValueError, TypeError, KeyError, json.JSONDecodeError,
            UnicodeDecodeError, binascii.Error) as exc:
        raise UsRightSideError(
            "invalid_sort_cursor", "分页游标与当前运行、筛选或排序不匹配", status_code=422
        ) from exc
