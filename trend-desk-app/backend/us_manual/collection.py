"""H6 美股/ETF 日采集：退出优先、环境容量、ETF 证据与 EP3 风险锚点。

该模块是趋势动物付费数据的唯一编排边界。它不读取 Bitget 私有账户，不保存交易所
凭证，也不创建订单；自动化只到候选和仓位计算所需的只读参考价为止。
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlmodel import Session

from backend import config
from backend.db import UsCandidateSnapshot, UsDailyRun, UsMarketEnvironmentSnapshot
from backend.trend_animals.billing import (
    component_pricing,
    endpoint_fixed_cost,
    estimate_component_cost,
    estimate_snapshot_cost,
)
from backend.trend_animals.client import TrendAnimalsClient
from backend.trend_animals.errors import TrendAnimalsError
from backend.trend_animals.service import ledger_mark, ledger_rows_after, ledger_rows_cost
from backend.trend_animals.us_coverage import select_us_roots
from backend.trend_animals.us_xstock_pool import _bitget_stock_rows
from backend.us_manual import repository
from backend.us_manual.bitget_public import fetch_public_instruments, fetch_public_quote
from backend.us_manual.contracts import (
    BASE_SIGNAL_FIELDS,
    ETF_BASE_SIGNAL_FIELDS,
    ETF_ENRICHMENT_FIELDS,
    HOLDING_EXIT_FIELDS,
    MANUAL_ONLY_NOTICE,
    MARKET_ENVIRONMENT_FIELDS,
    US_COMBO_NAMES,
    US_MANUAL_RULES_VERSION,
    US_MANUAL_H4_SCOPE,
    US_MANUAL_H5_SCOPE,
    US_MANUAL_H3_SCOPE,
    US_MANUAL_SCOPE,
    UsManualError,
    as_int,
    canonical_json,
    decimal_text,
    serialize,
    sha256,
    utc_now,
    utc_now_text,
)
from backend.us_manual.enrichment import apply_enrichment, apply_gate_fields, resolve_enrichment_fields
from backend.us_manual.etf_benchmarks import EtfBenchmarkService
from backend.us_manual.exits import record_holding_signals
from backend.us_manual.risk_anchor import RiskAnchorService
from backend.us_manual.rules import environment_factor, rank_observation_candidates, rank_ready_candidates


COUNT_FIELDS = ("constituentCount",)
COMPONENT_BATCH_LIMIT = 300
READY_STATUSES = {"ready", "ready_degraded"}
WARM_OR_ABOVE = {"温", "热", "沸"}


class _BudgetStop(Exception):
    pass


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _parse_clock(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid US manual clock: {value}") from exc


def shanghai_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(ZoneInfo(config.US_MANUAL_TIMEZONE))


def collection_contract_hash(
    stock_quality_fields: list[str] | tuple[str, ...],
    etf_quality_fields: list[str] | tuple[str, ...],
) -> str:
    return sha256({
        "rules_version": US_MANUAL_RULES_VERSION,
        "combos": US_COMBO_NAMES,
        "component_depth": "direct",
        "getAllBasicComponentsFlag": 0,
        "gate_fields": {
            "stock": list(BASE_SIGNAL_FIELDS),
            "etf": list(ETF_BASE_SIGNAL_FIELDS),
        },
        "stock_gate": {
            "right_side_calendar_days": {"min": 1, "max_exclusive": 10},
            "industry_temperature": sorted(WARM_OR_ABOVE),
        },
        "quality_fields": {
            "stock": list(stock_quality_fields),
            "etf": list(etf_quality_fields),
        },
        "minimum_relative_strength": "90",
        "match": "bitget_exact_underlying_symbol_v1",
        "etf_mode": "trade_pool_without_industry_gate",
        "etf_plan_gate": "authoritative_tracking_index_only_v2",
        "etf_deduplication": ["benchmark_family_id"],
        "quote": "bitget_public_quote_v1",
        "holding_exit_fields": list(HOLDING_EXIT_FIELDS),
        "market_environment_fields": list(MARKET_ENVIRONMENT_FIELDS),
        "risk_anchor_mode": config.US_MANUAL_H6_MODE,
        "risk_anchor_contract": "bitget_rtoken_ep3_v1_not_exit_stop",
        "allocation": "environment_daily_capacity_20_tickers_50u_target_v1",
    })


def _next_retry(local_now: datetime) -> datetime | None:
    cutoff = _parse_clock(config.US_MANUAL_AUTO_CUTOFF)
    minute_step = max(1, int(config.US_MANUAL_RETRY_MINUTES))
    candidate = local_now.replace(second=0, microsecond=0)
    remainder = candidate.minute % minute_step
    candidate += timedelta(minutes=(minute_step - remainder) if remainder else minute_step)
    if candidate.time() > cutoff:
        return None
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def _normalize_symbol(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())


def _safe_rows(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise UsManualError("api_contract_error", f"{label}不是对象数组")
    return value


def _parse_timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsManualError("quote_contract_error", "Bitget 公开报价时间无效") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _batch(values: list[int], size: int = COMPONENT_BATCH_LIMIT) -> list[list[int]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _snapshot_cost(fields: list[str], row_count: int, billing: list[dict]) -> Decimal:
    return sum(
        (_decimal(estimate_snapshot_cost(fields, len(group), billing))
         for group in _batch(list(range(row_count)))),
        Decimal("0"),
    )


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    temp.replace(path)


def _read_stage(path: Path, request_hash: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise UsManualError("archive_corrupt", f"美股阶段归档无法读取：{path.name}") from exc
    if not isinstance(payload, dict) or payload.get("request_hash") != request_hash:
        return None
    return payload


def _response_row_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return sum(len(rows) for rows in value.values() if isinstance(rows, list))
    return 0


def _response_data_dates(value: Any) -> list[str]:
    dates: set[str] = set()
    rows: list[Any]
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        rows = [row for nested in value.values() if isinstance(nested, list) for row in nested]
    else:
        rows = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("asOfDate"), str):
            dates.add(row["asOfDate"])
    return sorted(dates)


def _stage_payload(*, request: dict[str, Any], response: Any,
                   estimated_cost: Decimal = Decimal("0"),
                   actual_cost: Decimal | None = Decimal("0")) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "rules_version": US_MANUAL_RULES_VERSION,
        "created_at_utc": utc_now_text(),
        "request": request,
        "request_hash": sha256(request),
        "estimated_cost_cny": decimal_text(estimated_cost),
        "actual_cost_cny": decimal_text(actual_cost),
        "returned_row_count": _response_row_count(response),
        "data_dates": _response_data_dates(response),
        "response_sha256": sha256(response),
        "response": response,
    }


def _actual_stage_costs(rows: list[dict] | None, *, stages: set[str],
                        quality_fields: list[str]) -> tuple[dict[str, Decimal], Decimal | None]:
    """Attribute one ledger delta to H6 stages without extra balance calls."""
    if rows is None:
        return {}, None
    costs = {name: Decimal("0") for name in stages}
    unattributed = Decimal("0")
    for row in rows:
        try:
            cost = _decimal(row.get("apiCost"))
        except Exception:
            continue
        api_name = str(row.get("ApiName") or "")
        params = str(row.get("params") or "")
        stage: str | None = None
        if api_name == "searchTicker":
            stage = "search"
        elif api_name == "getComponentTicker":
            stage = "components"
        elif api_name == "getTickerSnapshot":
            if "constituentCount" in params:
                stage = "count_snapshot"
            elif any(field in params for field in HOLDING_EXIT_FIELDS[1:]):
                stage = "holding_exit_snapshot"
            # ``trendStrengthLocalCurr`` / labels are also candidate quality
            # fields.  The root-only temperature field is the unambiguous
            # signature of the market-environment call.
            elif "trendTemperatureCurr" in params:
                stage = "market_environment_snapshot"
            elif any(field in params for field in quality_fields):
                stage = (
                    "quality_snapshot"
                    if any(marker in params for marker in ("industryTmId", "industryName", "industryTrend"))
                    else "etf_quality_snapshot"
                )
            elif "industryTrendTemperature" in params:
                stage = "gate_snapshot"
            elif "daysSinceTrendEntry" in params:
                stage = "etf_gate_snapshot"
        if stage in costs:
            costs[stage] += cost
        else:
            unattributed += cost
    return costs, unattributed


def _find_combos(rows: list[dict[str, Any]], *, as_of_date: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for name, asset in US_COMBO_NAMES.items():
        matches = [row for row in rows if row.get("tickerName") == name and row.get("asset") == asset]
        if len(matches) != 1:
            raise UsManualError(
                "combo_identity_invalid",
                f"{name} / {asset} 搜索结果必须唯一，实际 {len(matches)}",
            )
        row = matches[0]
        tm_id = as_int(row.get("tmId"))
        if tm_id is None or tm_id <= 0:
            raise UsManualError("combo_identity_invalid", f"{name} 缺少有效 tmId")
        if row.get("asOfDate") != as_of_date:
            raise UsManualError(
                "data_stale", f"{name} 数据日 {row.get('asOfDate') or '未知'}，期望 {as_of_date}",
            )
        found[name] = row
    return found


def _combo_counts(rows: list[dict[str, Any]], *, combos: dict[str, dict[str, Any]],
                  as_of_date: str) -> dict[str, int]:
    by_tm = {as_int(row.get("tmId")): row for row in rows}
    counts: dict[str, int] = {}
    for name, combo in combos.items():
        row = by_tm.get(as_int(combo.get("tmId")))
        count = None if row is None else row.get("constituentCount")
        if row is None or row.get("asOfDate") != as_of_date:
            raise UsManualError("data_stale", f"{name} 成分数缺失或数据日不一致")
        if isinstance(count, bool) or not isinstance(count, (int, float)) or int(count) < 0:
            raise UsManualError("missing_required_fields", f"{name} 缺少有效 constituentCount")
        counts[name] = int(count)
    return counts


def _component_members(rows_by_combo: dict[str, list[dict[str, Any]]], *,
                       as_of_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    members: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    seen_tm: set[int] = set()
    for combo_name, rows in rows_by_combo.items():
        root_asset = "美国ETF" if combo_name == "温转热(美国ETF)" else "美股"
        for index, row in enumerate(rows):
            tm_id = as_int(row.get("tmId"))
            if tm_id is None or tm_id <= 0:
                raise UsManualError("api_contract_error", f"{combo_name} 第 {index + 1} 行缺少有效 tmId")
            if tm_id in seen_tm:
                raise UsManualError("api_contract_error", f"温转热直接成分出现重复 tmId={tm_id}")
            seen_tm.add(tm_id)
            if row.get("asOfDate") != as_of_date:
                raise UsManualError("data_stale", f"{combo_name} 成分 tmId={tm_id} 数据日不一致")
            symbol = str(row.get("tickerSymbol") or "").strip().upper()
            enriched = {**row, "tickerSymbol": symbol, "root_asset": root_asset, "source_combo": combo_name}
            if not symbol:
                audit.append({"tmId": tm_id, "reason": "missing_ticker_symbol", "source_combo": combo_name})
                continue
            members.append(enriched)
    return members, audit


def strict_bitget_intersection(*, members: list[dict[str, Any]],
                               raw_products: list[dict[str, Any]]) -> dict[str, Any]:
    """Only one exact source symbol and one exact Bitget product may enter the pool."""
    source_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in members:
        source_by_symbol[str(row["tickerSymbol"]).upper()].append(row)
    parsed_products = [row for row in _bitget_stock_rows(raw_products) if row.get("quote_coin") == "USDT"]
    product_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    normalized_products: dict[str, list[str]] = defaultdict(list)
    for row in parsed_products:
        symbol = str(row.get("underlying_symbol") or "").upper()
        product_by_symbol[symbol].append(row)
        normalized_products[_normalize_symbol(symbol)].append(symbol)

    matched: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for symbol in sorted(source_by_symbol):
        sources = source_by_symbol[symbol]
        products = product_by_symbol.get(symbol, [])
        if len(sources) != 1:
            unresolved.append({"ticker_symbol": symbol, "reason": "duplicate_source_symbol",
                               "tm_ids": [row.get("tmId") for row in sources]})
            continue
        if len(products) != 1:
            normalized = sorted(set(normalized_products.get(_normalize_symbol(symbol), [])))
            reason = "duplicate_bitget_symbol" if len(products) > 1 else (
                "normalized_unverified" if normalized else "no_exact_bitget_product")
            unresolved.append({"ticker_symbol": symbol, "tmId": sources[0].get("tmId"),
                               "reason": reason, "normalized_candidates": normalized})
            continue
        matched.append({"source": sources[0], "venue": products[0]})

    source_symbols = set(source_by_symbol)
    unmatched_product_count = sum(
        len(products) for symbol, products in product_by_symbol.items() if symbol not in source_symbols)
    return {
        "matched": matched,
        "unresolved": unresolved,
        "public_reality_stock_products": len(parsed_products),
        "unmatched_public_product_count": unmatched_product_count,
        "raw_product_sha256": sha256(raw_products),
    }


def _validated_snapshot_map(rows: list[dict[str, Any]], *, wanted: set[int],
                            as_of_date: str) -> dict[int, dict[str, Any]]:
    by_tm: dict[int, dict[str, Any]] = {}
    for row in rows:
        tm_id = as_int(row.get("tmId"))
        if tm_id is None or tm_id not in wanted:
            raise UsManualError("api_contract_error", "快照返回了未请求或无效的 tmId")
        if tm_id in by_tm:
            raise UsManualError("api_contract_error", f"快照返回重复 tmId={tm_id}")
        if row.get("asOfDate") != as_of_date:
            raise UsManualError("data_stale", f"快照 tmId={tm_id} 数据日不一致")
        by_tm[tm_id] = row
    return by_tm


def _quality_complete(candidate: UsCandidateSnapshot) -> bool:
    if candidate.asset_type == "etf":
        return candidate.strength_local is not None
    return all(value is not None for value in (
        candidate.strength_local,
        candidate.amount_1d,
        candidate.market_cap,
        candidate.industry_tm_id,
        candidate.industry_name,
        candidate.industry_temperature_curr,
        candidate.industry_strength_local,
    ))


class UsH6Collector:
    def __init__(self, *, data_root: Path | None = None,
                 client_factory: Callable[[], TrendAnimalsClient] = TrendAnimalsClient,
                 instruments_fetcher: Callable[[], list[dict[str, Any]]] = fetch_public_instruments,
                 quote_fetcher: Callable[[str], dict[str, Any]] = fetch_public_quote,
                 risk_anchor_service_factory: Callable[[], Any] | None = None,
                 etf_service_factory: Callable[[], Any] | None = None,
                 stop_service_factory: Callable[[], Any] | None = None,
                 now_factory: Callable[[], datetime] | None = None):
        self.data_root = data_root or config.DATA
        self.client_factory = client_factory
        self.instruments_fetcher = instruments_fetcher
        self.quote_fetcher = quote_fetcher
        self.risk_anchor_service_factory = risk_anchor_service_factory or stop_service_factory
        self.etf_service_factory = etf_service_factory
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def _risk_anchor_service(self) -> Any:
        if self.risk_anchor_service_factory is not None:
            return self.risk_anchor_service_factory()
        return RiskAnchorService(
            data_root=self.data_root,
            quote_fetcher=self.quote_fetcher,
            now_factory=self.now_factory,
        )

    def _etf_service(self) -> Any:
        if self.etf_service_factory is not None:
            return self.etf_service_factory()
        return EtfBenchmarkService(data_root=self.data_root, now_factory=self.now_factory)

    @property
    def root(self) -> Path:
        return self.data_root / "research" / "trend_animals" / "us_manual_h6"

    @property
    def legacy_h5_root(self) -> Path:
        return self.data_root / "research" / "trend_animals" / "us_manual_h5"

    @property
    def legacy_h4_root(self) -> Path:
        return self.data_root / "research" / "trend_animals" / "us_manual_h4"

    @property
    def legacy_h3_root(self) -> Path:
        # Exact request hashes allow already-paid H3 stages to be read without
        # rewriting H3 manifests or raw evidence.
        return self.data_root / "research" / "trend_animals" / "us_manual_h3"

    def _run_payload(self, session: Session, run: UsDailyRun, *, cache_state: str | None = None) -> dict[str, Any]:
        payload = repository.model_payload(run)
        payload["funnel"] = serialize(run.funnel_json or {})
        anchor_service = self._risk_anchor_service()
        payload["candidates"] = [
            anchor_service.candidate_payload(session, row)
            for row in repository.list_candidates(session, run.run_id)
        ]
        payload["cache_state"] = cache_state or ("hit" if run.cache_hit else "miss")
        payload["notice"] = MANUAL_ONLY_NOTICE
        return payload

    def _reconcile_ready_costs(self, session: Session, run: UsDailyRun) -> UsDailyRun:
        """Do not count exact H3-H5 archive reuse as a new H6 charge."""
        breakdown = {
            key: _decimal(value)
            for key, value in (run.cost_breakdown_json or {}).items()
            if key in {
                "search", "count_snapshot", "components", "gate_snapshot", "etf_gate_snapshot",
                "quality_snapshot", "etf_quality_snapshot",
                "holding_exit_snapshot", "market_environment_snapshot",
            } and value not in (None, "")
        }
        if not breakdown:
            return run
        actual_breakdown: dict[str, Decimal] = {}
        actual_known = True
        for name in list(breakdown):
            current_path = self.root / run.as_of_date / f"{name}.json"
            legacy_path = next((path for path in (
                self.legacy_h5_root / run.as_of_date / f"{name}.json",
                self.legacy_h4_root / run.as_of_date / f"{name}.json",
                self.legacy_h3_root / run.as_of_date / f"{name}.json",
            ) if path.exists()), None)
            if not current_path.exists() and legacy_path is not None:
                breakdown[name] = Decimal("0")
                actual_breakdown[name] = Decimal("0")
                continue
            try:
                stage = json.loads(current_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                actual_known = False
                continue
            raw_actual = stage.get("actual_cost_cny") if isinstance(stage, dict) else None
            if raw_actual is None:
                actual_known = False
            else:
                actual_breakdown[name] = _decimal(raw_actual)
        estimated_total = sum(breakdown.values(), Decimal("0"))
        estimated_quality = sum((
            value for key, value in breakdown.items() if key.endswith("quality_snapshot")
        ), Decimal("0"))
        actual_total = sum(actual_breakdown.values(), Decimal("0")) if actual_known else None
        actual_quality = (
            sum((value for key, value in actual_breakdown.items()
                 if key.endswith("quality_snapshot")), Decimal("0"))
            if actual_known else None
        )
        run = repository.update_run(
            session, run,
            estimated_base_cost_cny=estimated_total - estimated_quality,
            estimated_enrichment_cost_cny=estimated_quality,
            estimated_total_cost_cny=estimated_total,
            actual_base_cost_cny=(actual_total - actual_quality
                                  if actual_total is not None and actual_quality is not None else None),
            actual_enrichment_cost_cny=actual_quality,
            actual_total_cost_cny=actual_total,
            cost_breakdown_json={key: decimal_text(value) for key, value in breakdown.items()},
        )
        manifest_path = (
            Path(run.raw_archive_path)
            if run.raw_archive_path else self.root / run.as_of_date / "manifests" / f"{run.run_id}.json"
        )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return run
        if isinstance(manifest, dict) and isinstance(manifest.get("cost"), dict):
            manifest["cost"]["estimated_breakdown_cny"] = {
                key: decimal_text(value) for key, value in breakdown.items()
            }
            manifest["cost"]["estimated_total_cny"] = decimal_text(estimated_total)
            manifest["cost"]["actual_total_cny"] = decimal_text(actual_total)
            manifest["cost"]["actual_breakdown_cny"] = {
                key: decimal_text(value) for key, value in actual_breakdown.items()
            }
            _write_json_atomic(manifest_path, manifest)
        return run

    def _archive_stage(self, *, as_of_date: str, name: str, request: dict[str, Any],
                       response: Any, cost: Decimal = Decimal("0"),
                       actual_cost: Decimal | None = Decimal("0")) -> dict[str, Any]:
        path = self.root / as_of_date / f"{name}.json"
        request_hash = sha256(request)
        cached = _read_stage(path, request_hash)
        if cached is not None:
            return cached
        payload = _stage_payload(
            request=request, response=response,
            estimated_cost=cost, actual_cost=actual_cost,
        )
        _write_json_atomic(path, payload)
        return payload

    def _annotate_stage_actual(self, *, as_of_date: str, name: str,
                               actual_cost: Decimal | None) -> None:
        path = self.root / as_of_date / f"{name}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        payload["rules_version"] = US_MANUAL_RULES_VERSION
        payload["actual_cost_cny"] = decimal_text(actual_cost)
        response = payload.get("response")
        payload.setdefault("returned_row_count", _response_row_count(response))
        payload.setdefault("data_dates", _response_data_dates(response))
        payload.setdefault("response_sha256", sha256(response))
        _write_json_atomic(path, payload)

    def _cached_stage(self, *, as_of_date: str, name: str,
                      request: dict[str, Any]) -> dict[str, Any] | None:
        request_hash = sha256(request)
        current = _read_stage(self.root / as_of_date / f"{name}.json", request_hash)
        if current is not None:
            return current
        legacy_h5 = _read_stage(self.legacy_h5_root / as_of_date / f"{name}.json", request_hash)
        if legacy_h5 is not None:
            return legacy_h5
        legacy_h4 = _read_stage(self.legacy_h4_root / as_of_date / f"{name}.json", request_hash)
        if legacy_h4 is not None:
            return legacy_h4
        return _read_stage(self.legacy_h3_root / as_of_date / f"{name}.json", request_hash)

    def _set_cost(self, session: Session, run: UsDailyRun, breakdown: dict[str, Decimal], *,
                  enrichment: bool = False) -> None:
        total = sum(breakdown.values(), Decimal("0"))
        quality = sum(
            (value for key, value in breakdown.items() if key.endswith("quality_snapshot")),
            Decimal("0"),
        )
        base = total - quality
        repository.update_run(
            session,
            run,
            estimated_base_cost_cny=base,
            estimated_enrichment_cost_cny=quality if enrichment or quality else None,
            estimated_total_cost_cny=total,
            cost_breakdown_json={key: decimal_text(value) for key, value in breakdown.items()},
        )

    def _ensure_budget(self, session: Session, run: UsDailyRun, breakdown: dict[str, Decimal],
                       *, stage: str, increment: Decimal) -> None:
        cap = run.daily_cost_cap_cny or _decimal(config.US_MANUAL_DAILY_AUTO_BUDGET)
        other = repository.estimated_cost_for_date(
            session, as_of_date=run.as_of_date, exclude_run_id=run.run_id)
        current = sum(breakdown.values(), Decimal("0"))
        projected = other + current + increment
        if projected <= cap:
            return
        run.estimated_total_cost_cny = current
        run.cost_breakdown_json = {
            **{key: decimal_text(value) for key, value in breakdown.items()},
            "blocked_next_stage": stage,
            "blocked_increment_cny": decimal_text(increment),
            "same_date_prior_estimate_cny": decimal_text(other),
            "projected_same_date_total_cny": decimal_text(projected),
        }
        if stage == "holding_exit_snapshot":
            run.status = "exit_data_blocked"
            run.exit_status = "blocked"
            run.error_code = "exit_data_blocked"
        else:
            run.status = "exit_ready_buy_blocked_budget"
            run.error_code = "daily_cost_cap_exceeded"
        run.error_message = f"下一阶段 {stage} 将使同一数据日预计累计费用超过 ¥{decimal_text(cap)}"
        session.add(run)
        session.commit()
        raise _BudgetStop

    def _paid_stage(self, session: Session, run: UsDailyRun, breakdown: dict[str, Decimal],
                    executed_stages: set[str], *,
                    name: str, request: dict[str, Any], estimated_cost: Decimal,
                    call: Callable[[], Any]) -> Any:
        cached = self._cached_stage(as_of_date=run.as_of_date, name=name, request=request)
        if cached is not None:
            # 同一 run 重试保留原预算；跨 mode/版本读取已付费归档时成本为 0，
            # 防止 off→shadow→active 因缓存证据被重复计入 ¥5 上限。
            prior = (run.cost_breakdown_json or {}).get(name)
            breakdown[name] = _decimal(prior) if prior not in (None, "") else Decimal("0")
            self._set_cost(session, run, breakdown, enrichment=name.endswith("quality_snapshot"))
            return cached.get("response")
        self._ensure_budget(session, run, breakdown, stage=name, increment=estimated_cost)
        response = call()
        self._archive_stage(
            as_of_date=run.as_of_date, name=name, request=request,
            response=response, cost=estimated_cost, actual_cost=None,
        )
        executed_stages.add(name)
        breakdown[name] = estimated_cost
        self._set_cost(session, run, breakdown, enrichment=name.endswith("quality_snapshot"))
        return response

    def _new_or_cached_run(self, session: Session, *, as_of_date: str, trigger: str,
                           stock_quality_fields: list[str], etf_quality_fields: list[str],
                           source_dates: dict[str, Any]) -> UsDailyRun:
        contract_hash = collection_contract_hash(stock_quality_fields, etf_quality_fields)
        all_quality_fields = list(dict.fromkeys([*stock_quality_fields, *etf_quality_fields]))
        existing = repository.cached_run(
            session, as_of_date=as_of_date, scope=US_MANUAL_SCOPE,
            base_fields_hash=contract_hash,
        )
        if existing is not None:
            return existing
        run = UsDailyRun(
            run_id=f"us-run-{as_of_date.replace('-', '')}-{uuid4().hex[:10]}",
            as_of_date=as_of_date,
            scope=US_MANUAL_SCOPE,
            status="pending",
            rules_version=US_MANUAL_RULES_VERSION,
            base_fields=list(BASE_SIGNAL_FIELDS),
            base_fields_hash=contract_hash,
            enrichment_fields=all_quality_fields,
            enrichment_fields_hash=sha256({
                "stock": stock_quality_fields,
                "etf": etf_quality_fields,
            }),
            daily_cost_cap_cny=_decimal(config.US_MANUAL_DAILY_AUTO_BUDGET),
            trigger=trigger,
            source_dates_json=source_dates,
        )
        return repository.save_run(session, run)

    def collect(self, session: Session, *, trigger: str = "manual",
                now: datetime | None = None) -> dict[str, Any]:
        local_now = shanghai_now(now or self.now_factory())
        client = self.client_factory()
        run: UsDailyRun | None = None
        owner = uuid4().hex
        before_ledger: set[tuple] = set()
        try:
            docs = _safe_rows(client.get_api_doc_intro(), label="实时接口文档")
            changes = _safe_rows(client.get_change_log(), label="变更记录")
            billing = _safe_rows(client.get_snapshot_billing(), label="字段计费表")
            statuses = _safe_rows(client.get_update_status(), label="更新状态")
            try:
                roots = select_us_roots(statuses)
            except TrendAnimalsError as exc:
                return {
                    "status": "waiting_update",
                    "state": "waiting_update",
                    "run_id": None,
                    "as_of_date": None,
                    "error": {"code": exc.code, "message": str(exc)},
                    "next_retry_at": serialize(_next_retry(local_now)),
                    "schedule": self.schedule_payload(local_now),
                    "paid_calls": 0,
                    "notice": MANUAL_ONLY_NOTICE,
                }
            as_of_date = str(roots[0]["asOfDate"])
            stock_quality_fields = resolve_enrichment_fields(billing)
            etf_quality_fields = resolve_enrichment_fields(billing, ETF_ENRICHMENT_FIELDS)
            quality_fields = list(dict.fromkeys([*stock_quality_fields, *etf_quality_fields]))
            current_latest = repository.latest_run(session, scope=US_MANUAL_SCOPE)
            h5_latest = repository.latest_run(session, scope=US_MANUAL_H5_SCOPE)
            h4_latest = repository.latest_run(session, scope=US_MANUAL_H4_SCOPE)
            h3_latest = repository.latest_run(session, scope=US_MANUAL_H3_SCOPE)
            latest = max(
                (row for row in (current_latest, h5_latest, h4_latest, h3_latest) if row is not None),
                key=lambda row: row.as_of_date,
                default=None,
            )
            if latest is not None and as_of_date < latest.as_of_date:
                return {
                    "status": "waiting_update",
                    "state": "waiting_update",
                    "run_id": None,
                    "as_of_date": as_of_date,
                    "error": {
                        "code": "data_date_regressed",
                        "message": f"趋势动物数据日 {as_of_date} 早于本地最新 {latest.as_of_date}",
                    },
                    "next_retry_at": serialize(_next_retry(local_now)),
                    "schedule": self.schedule_payload(local_now),
                    "paid_calls": 0,
                    "notice": MANUAL_ONLY_NOTICE,
                }
            source_dates = {
                "us_as_of_date": as_of_date,
                "us_etf_as_of_date": roots[1]["asOfDate"],
                "update_status": roots,
                "free_preflight_at": utc_now_text(),
                "docs_sha256": sha256(docs),
                "change_log_sha256": sha256(changes),
                "billing_sha256": sha256(billing),
            }
            run = self._new_or_cached_run(
                session, as_of_date=as_of_date, trigger=trigger,
                stock_quality_fields=stock_quality_fields,
                etf_quality_fields=etf_quality_fields,
                source_dates=source_dates,
            )
            preflight_request = {"as_of_date": as_of_date, "free": True}
            self._archive_stage(
                as_of_date=as_of_date,
                name="preflight",
                request=preflight_request,
                response={"docs": docs, "changes": changes, "billing": billing, "statuses": statuses},
            )
            # Bitget products are free public data.  Resolve and archive them before
            # any paid Trend Animals stage so a venue outage cannot waste API fees.
            product_request = {
                "endpoint": "bitget_public_instruments",
                "category": "SPOT",
                "retrieval_date": local_now.date().isoformat(),
            }
            product_cached = self._cached_stage(
                as_of_date=as_of_date, name="bitget_products", request=product_request,
            )
            if product_cached is None:
                raw_products = self.instruments_fetcher()
                self._archive_stage(
                    as_of_date=as_of_date,
                    name="bitget_products",
                    request=product_request,
                    response=raw_products,
                )
            else:
                raw_products = product_cached.get("response")
            products = _safe_rows(raw_products, label="Bitget 公共产品清单")
            if run.status in READY_STATUSES:
                run = self._reconcile_ready_costs(session, run)
                run.cache_hit = True
                session.add(run)
                session.commit()
                session.refresh(run)
                return self._run_payload(session, run, cache_state="hit")

            manual_start = _parse_clock(config.US_MANUAL_MANUAL_START)
            if trigger == "manual" and local_now.time() < manual_start:
                repository.update_run(
                    session, run, status="waiting_window", next_retry_at=None,
                    error_code="manual_window_not_open",
                    error_message=f"北京时间 {config.US_MANUAL_MANUAL_START} 前只完成免费更新检查",
                )
                return self._run_payload(session, run, cache_state="free_check_only")

            expires = utc_now() + timedelta(minutes=max(1, config.US_MANUAL_LEASE_MINUTES))
            if not repository.acquire_run_lease(
                session, run_id=run.run_id, owner=owner, now=utc_now(), expires_at=expires,
            ):
                session.refresh(run)
                return self._run_payload(session, run, cache_state="in_progress")

            run = repository.update_run(
                session, run,
                status="collecting",
                trigger=trigger,
                attempt_count=int(run.attempt_count or 0) + 1,
                next_retry_at=None,
                error_code=None,
                error_message=None,
            )
            breakdown = {
                key: _decimal(value)
                for key, value in (run.cost_breakdown_json or {}).items()
                if key in {
                    "search", "count_snapshot", "components", "gate_snapshot", "etf_gate_snapshot",
                    "quality_snapshot", "etf_quality_snapshot",
                    "holding_exit_snapshot", "market_environment_snapshot",
                }
                and value not in (None, "")
            }
            executed_stages: set[str] = set()
            before_ledger = ledger_mark(client)

            # H6 always evaluates existing H6 positions before considering new buys.
            # Legacy H1-H5 lots remain visible in the ledger but are never given fabricated
            # H6 signal evidence.
            open_h6_lots = [
                lot for lot in repository.open_lots(session)
                if lot.rules_version == US_MANUAL_RULES_VERSION
            ]
            missing_tm_lots = [int(lot.lot_id) for lot in open_h6_lots if lot.tm_id is None]
            if missing_tm_lots:
                repository.update_run(
                    session, run, exit_status="blocked",
                    error_code="holding_tm_id_missing",
                    error_message="H6 开放持仓缺少趋势动物 tmId，不能获取真实退出字段",
                )
                raise UsManualError(
                    "holding_tm_id_missing",
                    "H6 开放持仓缺少趋势动物 tmId，今日新增买入失败关闭",
                    409,
                    {"lot_ids": missing_tm_lots},
                )
            exit_decisions: list[Any] = []
            if open_h6_lots:
                holding_tm_ids = sorted({int(lot.tm_id) for lot in open_h6_lots if lot.tm_id is not None})
                holding_request = {
                    "endpoint": "getTickerSnapshot",
                    "tm_ids": holding_tm_ids,
                    "fields": list(HOLDING_EXIT_FIELDS),
                    "as_of_date": as_of_date,
                }

                def holding_call() -> list[dict[str, Any]]:
                    rows: list[dict[str, Any]] = []
                    for group in _batch(holding_tm_ids):
                        rows.extend(_safe_rows(
                            client.get_snapshot(group, list(HOLDING_EXIT_FIELDS)),
                            label="开放持仓退出快照",
                        ))
                    return rows

                holding_rows = self._paid_stage(
                    session, run, breakdown, executed_stages,
                    name="holding_exit_snapshot", request=holding_request,
                    estimated_cost=_snapshot_cost(
                        list(HOLDING_EXIT_FIELDS), len(holding_tm_ids), billing,
                    ),
                    call=holding_call,
                )
                holding_rows = _safe_rows(holding_rows, label="开放持仓退出快照")
                # Validate exact date/scope before persisting any exit decision.
                _validated_snapshot_map(
                    holding_rows, wanted=set(holding_tm_ids), as_of_date=as_of_date,
                )
                exit_decisions = record_holding_signals(
                    session,
                    run_id=run.run_id,
                    as_of_date=as_of_date,
                    rows=holding_rows,
                    quote_fetcher=self.quote_fetcher,
                )
                run = repository.update_run(session, run, exit_status="ready")
            else:
                run = repository.update_run(session, run, exit_status="no_holdings")

            # The US root temperature determines the aggregate daily new-buy capacity.
            # Unknown or unsupported values are archived as blocked (factor 0), while the
            # watchlist may still finish for research.
            market_tm_id = int(roots[0]["tmId"])
            market_request = {
                "endpoint": "getTickerSnapshot",
                "tm_ids": [market_tm_id],
                "fields": list(MARKET_ENVIRONMENT_FIELDS),
                "as_of_date": as_of_date,
            }
            market_rows = self._paid_stage(
                session, run, breakdown, executed_stages,
                name="market_environment_snapshot", request=market_request,
                estimated_cost=_decimal(estimate_snapshot_cost(
                    list(MARKET_ENVIRONMENT_FIELDS), 1, billing,
                )),
                call=lambda: client.get_snapshot([market_tm_id], list(MARKET_ENVIRONMENT_FIELDS)),
            )
            market_by_tm = _validated_snapshot_map(
                _safe_rows(market_rows, label="美股整体环境快照"),
                wanted={market_tm_id}, as_of_date=as_of_date,
            )
            market_raw = market_by_tm[market_tm_id]
            market_temperature = str(market_raw.get("trendTemperatureCurr") or "").strip()
            try:
                factor = environment_factor(market_temperature)
                environment_status = "ready"
            except UsManualError:
                factor = Decimal("0")
                environment_status = "blocked"
                market_temperature = market_temperature or "未知"
            environment = repository.save_environment(session, UsMarketEnvironmentSnapshot(
                environment_id=f"us-env-{run.run_id}-{uuid4().hex[:8]}",
                run_id=run.run_id,
                as_of_date=as_of_date,
                market_tm_id=market_tm_id,
                market_temperature=market_temperature,
                environment_factor=factor,
                status=environment_status,
                contract_hash=sha256({
                    "fields": MARKET_ENVIRONMENT_FIELDS,
                    "rules_version": US_MANUAL_RULES_VERSION,
                }),
                raw_json=serialize(market_raw),
                raw_sha256=sha256(market_raw),
            ))
            run = repository.update_run(session, run, environment_status=environment.status)

            search_request = {"endpoint": "searchTicker", "keyword": "温转热", "as_of_date": as_of_date}
            search_rows = self._paid_stage(
                session, run, breakdown, executed_stages,
                name="search", request=search_request,
                estimated_cost=_decimal(endpoint_fixed_cost(docs, "searchTicker")),
                call=lambda: client.search_ticker("温转热"),
            )
            combos = _find_combos(_safe_rows(search_rows, label="温转热搜索结果"), as_of_date=as_of_date)
            combo_ids = [int(combos[name]["tmId"]) for name in US_COMBO_NAMES]

            count_request = {
                "endpoint": "getTickerSnapshot", "tm_ids": combo_ids,
                "fields": list(COUNT_FIELDS), "as_of_date": as_of_date,
            }
            count_rows = self._paid_stage(
                session, run, breakdown, executed_stages,
                name="count_snapshot", request=count_request,
                estimated_cost=_decimal(estimate_snapshot_cost(list(COUNT_FIELDS), len(combo_ids), billing)),
                call=lambda: client.get_snapshot(combo_ids, list(COUNT_FIELDS)),
            )
            counts = _combo_counts(
                _safe_rows(count_rows, label="组合成分数快照"), combos=combos, as_of_date=as_of_date,
            )

            base_cost, normal_row_cost, combo_row_cost = component_pricing(docs)
            component_estimate = sum((
                _decimal(estimate_component_cost(
                    counts[name], combo=True, base_cost=base_cost,
                    normal_row_cost=normal_row_cost, combo_row_cost=combo_row_cost,
                )) for name in US_COMBO_NAMES
            ), Decimal("0"))
            component_request = {
                "endpoint": "getComponentTicker", "combos": {
                    name: {"tm_id": int(combos[name]["tmId"]), "getAllBasicComponentsFlag": 0}
                    for name in US_COMBO_NAMES
                }, "as_of_date": as_of_date,
            }

            def component_call() -> dict[str, list[dict[str, Any]]]:
                return {
                    name: client.get_components(int(combos[name]["tmId"]), all_basic=False)
                    for name in US_COMBO_NAMES
                }

            raw_components = self._paid_stage(
                session, run, breakdown, executed_stages,
                name="components", request=component_request,
                estimated_cost=component_estimate, call=component_call,
            )
            if not isinstance(raw_components, dict):
                raise UsManualError("api_contract_error", "温转热直接成分归档不是对象")
            rows_by_combo = {
                name: _safe_rows(raw_components.get(name), label=f"{name} 直接成分")
                for name in US_COMBO_NAMES
            }
            # constituentCount is only the best pre-call estimate.  Once the direct
            # response exists, carry its real row count into all subsequent budget gates.
            if "components" in executed_stages or breakdown.get("components", Decimal("0")) > 0:
                breakdown["components"] = sum((
                    _decimal(estimate_component_cost(
                        len(rows_by_combo[name]), combo=True, base_cost=base_cost,
                        normal_row_cost=normal_row_cost, combo_row_cost=combo_row_cost,
                    )) for name in US_COMBO_NAMES
                ), Decimal("0"))
            self._set_cost(session, run, breakdown)
            members, component_audit = _component_members(rows_by_combo, as_of_date=as_of_date)

            intersection = strict_bitget_intersection(members=members, raw_products=products)

            matched = intersection["matched"]
            matched_stocks = [item for item in matched if item["source"]["root_asset"] == "美股"]
            matched_etfs = [item for item in matched if item["source"]["root_asset"] == "美国ETF"]
            stock_gate_tm_ids = [int(row["source"]["tmId"]) for row in matched_stocks]
            etf_gate_tm_ids = [int(row["source"]["tmId"]) for row in matched_etfs]
            gate_request = {
                "endpoint": "getTickerSnapshot", "tm_ids": stock_gate_tm_ids,
                "fields": list(BASE_SIGNAL_FIELDS), "as_of_date": as_of_date,
            }

            def gate_call() -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = []
                for group in _batch(stock_gate_tm_ids):
                    rows.extend(_safe_rows(
                        client.get_snapshot(group, list(BASE_SIGNAL_FIELDS)),
                        label="个股门槛快照",
                    ))
                return rows

            gate_rows = self._paid_stage(
                session, run, breakdown, executed_stages,
                name="gate_snapshot", request=gate_request,
                estimated_cost=_snapshot_cost(list(BASE_SIGNAL_FIELDS), len(stock_gate_tm_ids), billing),
                call=gate_call,
            ) if stock_gate_tm_ids else []
            stock_gate_by_tm = _validated_snapshot_map(
                _safe_rows(gate_rows, label="个股门槛快照"),
                wanted=set(stock_gate_tm_ids), as_of_date=as_of_date,
            )
            etf_gate_request = {
                "endpoint": "getTickerSnapshot", "tm_ids": etf_gate_tm_ids,
                "fields": list(ETF_BASE_SIGNAL_FIELDS), "as_of_date": as_of_date,
            }

            def etf_gate_call() -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = []
                for group in _batch(etf_gate_tm_ids):
                    rows.extend(_safe_rows(
                        client.get_snapshot(group, list(ETF_BASE_SIGNAL_FIELDS)),
                        label="ETF 门槛快照",
                    ))
                return rows

            etf_gate_rows = self._paid_stage(
                session, run, breakdown, executed_stages,
                name="etf_gate_snapshot", request=etf_gate_request,
                estimated_cost=_snapshot_cost(
                    list(ETF_BASE_SIGNAL_FIELDS), len(etf_gate_tm_ids), billing,
                ),
                call=etf_gate_call,
            ) if etf_gate_tm_ids else []
            etf_gate_by_tm = _validated_snapshot_map(
                _safe_rows(etf_gate_rows, label="ETF 门槛快照"),
                wanted=set(etf_gate_tm_ids), as_of_date=as_of_date,
            )

            candidates: list[UsCandidateSnapshot] = []
            for item in matched:
                source = item["source"]
                venue = item["venue"]
                tm_id = int(source["tmId"])
                asset_type = "etf" if source["root_asset"] == "美国ETF" else "stock"
                gate_row = (
                    etf_gate_by_tm.get(tm_id) if asset_type == "etf" else stock_gate_by_tm.get(tm_id)
                )
                days = as_int((gate_row or {}).get("daysSinceTrendEntry"))
                within_age = days is not None and 1 <= days < 10
                temperature = str(
                    (gate_row or {}).get("industryTrendTemperatureCurr") or ""
                ).strip() or None
                temperature_ok = temperature in WARM_OR_ABOVE
                if gate_row is None:
                    screen_status = "data_incomplete"
                    reason = "gate_snapshot_missing"
                elif days is None:
                    screen_status = "data_incomplete"
                    reason = "right_side_days_missing"
                elif not within_age:
                    screen_status = "observe"
                    reason = "right_side_age_outside_1_9"
                elif asset_type == "stock" and temperature is None:
                    screen_status = "data_incomplete"
                    reason = "sector_temperature_missing"
                elif asset_type == "stock" and not temperature_ok:
                    screen_status = "observe"
                    reason = "sector_temperature_below_warm"
                else:
                    screen_status = "screened"
                    reason = "h4_gate_passed"
                candidate = UsCandidateSnapshot(
                    run_id=run.run_id,
                    environment_id=environment.environment_id,
                    tm_id=tm_id,
                    ticker_symbol=str(source.get("tickerSymbol") or "").upper(),
                    ticker_name=source.get("tickerName"),
                    asset_type=asset_type,
                    venue_instrument=venue.get("venue_instrument"),
                    venue_metadata_json=venue,
                    temperature_prev="温",
                    temperature_curr="热",
                    right_side_calendar_days=days,
                    right_side_age_bucket=("1-3" if days is not None and days <= 3 else
                                           "4-9" if within_age else "outside_1_9"),
                    warm_to_hot=True,
                    gate_passed=(within_age and (asset_type == "etf" or temperature_ok)),
                    screen_status=screen_status,
                    primary_reason=reason,
                    all_reasons=[reason],
                    raw_fields={
                        "membership": source,
                        "gate": gate_row,
                        "signal_evidence": "direct_member_of_warm_to_hot_combo",
                    },
                    raw_sha256=sha256({"source": source, "gate": gate_row, "venue": venue}),
                )
                if gate_row is not None:
                    apply_gate_fields(candidate, gate_row)
                candidates.append(candidate)
            repository.replace_candidates(session, run=run, candidates=candidates)
            candidates = repository.list_candidates(session, run.run_id)

            quality_candidates = [row for row in candidates if row.gate_passed]
            stock_quality_candidates = [row for row in quality_candidates if row.asset_type == "stock"]
            etf_quality_candidates = [row for row in quality_candidates if row.asset_type == "etf"]
            stock_quality_ids = [row.tm_id for row in stock_quality_candidates]
            etf_quality_ids = [row.tm_id for row in etf_quality_candidates]
            quality_request = {
                "endpoint": "getTickerSnapshot", "tm_ids": stock_quality_ids,
                "fields": stock_quality_fields, "as_of_date": as_of_date,
            }

            def quality_call() -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = []
                for group in _batch(stock_quality_ids):
                    rows.extend(_safe_rows(
                        client.get_snapshot(group, stock_quality_fields), label="个股质量快照",
                    ))
                return rows

            quality_rows = self._paid_stage(
                session, run, breakdown, executed_stages,
                name="quality_snapshot", request=quality_request,
                estimated_cost=_snapshot_cost(stock_quality_fields, len(stock_quality_ids), billing),
                call=quality_call,
            ) if stock_quality_ids else []
            stock_quality_by_tm = _validated_snapshot_map(
                _safe_rows(quality_rows, label="个股质量快照"),
                wanted=set(stock_quality_ids), as_of_date=as_of_date,
            )
            etf_quality_request = {
                "endpoint": "getTickerSnapshot", "tm_ids": etf_quality_ids,
                "fields": etf_quality_fields, "as_of_date": as_of_date,
            }

            def etf_quality_call() -> list[dict[str, Any]]:
                rows: list[dict[str, Any]] = []
                for group in _batch(etf_quality_ids):
                    rows.extend(_safe_rows(
                        client.get_snapshot(group, etf_quality_fields), label="ETF 质量快照",
                    ))
                return rows

            etf_quality_rows = self._paid_stage(
                session, run, breakdown, executed_stages,
                name="etf_quality_snapshot", request=etf_quality_request,
                estimated_cost=_snapshot_cost(etf_quality_fields, len(etf_quality_ids), billing),
                call=etf_quality_call,
            ) if etf_quality_ids else []
            etf_quality_by_tm = _validated_snapshot_map(
                _safe_rows(etf_quality_rows, label="ETF 质量快照"),
                wanted=set(etf_quality_ids), as_of_date=as_of_date,
            )
            for candidate in quality_candidates:
                raw = (
                    etf_quality_by_tm.get(candidate.tm_id)
                    if candidate.asset_type == "etf" else stock_quality_by_tm.get(candidate.tm_id)
                )
                if raw is None:
                    candidate.screen_status = "data_incomplete"
                    candidate.primary_reason = "quality_snapshot_missing"
                    candidate.all_reasons = ["quality_snapshot_missing"]
                else:
                    apply_enrichment(candidate, raw)
                    candidate.raw_sha256 = sha256(candidate.raw_fields)
            selected, _ = rank_ready_candidates(quality_candidates)
            quality_complete = sum(1 for candidate in quality_candidates if _quality_complete(candidate))
            rank_observation_candidates(selected)

            # ETF classification is an execution-safety gate, not a fuzzy name heuristic.
            # Missing or stale authoritative evidence is refreshed automatically as part of
            # this collection.  A uniquely confirmed tracking index is applied immediately to
            # the current signal day; optional strategy/exposure facts never block planning.
            etf_service = self._etf_service()
            etf_evidence_audit: list[dict[str, Any]] = []
            etfs_requiring_classification = [row for row in selected if row.asset_type == "etf"]
            for candidate in etfs_requiring_classification:
                evidence = repository.latest_etf_benchmark(session, candidate.ticker_symbol)
                fresh = (
                    etf_service.evidence_is_fresh(evidence)
                    if hasattr(etf_service, "evidence_is_fresh")
                    else (
                        evidence is not None
                        and evidence.status == "verified"
                        and evidence.expires_at is not None
                        and evidence.expires_at >= utc_now()
                        and bool(evidence.benchmark_family_id)
                    )
                )
                if fresh:
                    etf_evidence_audit.append({
                        "candidate_id": candidate.candidate_id,
                        "ticker_symbol": candidate.ticker_symbol,
                        "status": "cache_hit",
                        "evidence_id": evidence.evidence_id,
                    })
                    continue
                try:
                    refreshed = etf_service.refresh(
                        session, ticker_symbol=candidate.ticker_symbol,
                    )
                    etf_evidence_audit.append({
                        "candidate_id": candidate.candidate_id,
                        "ticker_symbol": candidate.ticker_symbol,
                        "status": "refreshed_and_applied",
                        "evidence": refreshed,
                    })
                except UsManualError as exc:
                    etf_evidence_audit.append({
                        "candidate_id": candidate.candidate_id,
                        "ticker_symbol": candidate.ticker_symbol,
                        "status": "blocked",
                        "error": exc.as_payload(),
                    })
            etf_counts = etf_service.apply_candidates(session, candidates)
            selected = [
                row for row in repository.list_candidates(session, run.run_id)
                if row.screen_status == "ready" and row.asset_type in {"stock", "etf"}
            ]
            rank_observation_candidates(selected)
            repository.save_candidates(session, candidates)
            quote_audit: list[dict[str, Any]] = []
            for candidate in selected:
                try:
                    quote = self.quote_fetcher(str(candidate.venue_instrument))
                    candidate.reference_price_usdt = _decimal(quote.get("reference_price"))
                    candidate.reference_price_at = _parse_timestamp(quote.get("quoted_at"))
                    candidate.reference_price_source = "bitget_public_quote"
                    candidate.quote_status = "available"
                    quote_audit.append({"candidate_id": candidate.candidate_id, "tmId": candidate.tm_id,
                                        "venue_instrument": candidate.venue_instrument, "quote": quote})
                except (UsManualError, ValueError) as exc:
                    candidate.reference_price_usdt = None
                    candidate.reference_price_at = None
                    candidate.reference_price_source = "bitget_public_quote"
                    candidate.quote_status = "quote_unavailable"
                    quote_audit.append({
                        "candidate_id": candidate.candidate_id,
                        "tmId": candidate.tm_id,
                        "venue_instrument": candidate.venue_instrument,
                        "error": {"code": getattr(exc, "code", "quote_contract_error"), "message": str(exc)},
                    })
            repository.save_candidates(session, candidates)

            # H6 no longer uses Wind or treats the structure point as a real stop.
            # Bitget rToken EP3 anchors are planning-only evidence and fail independently.
            anchor_service = self._risk_anchor_service()
            anchor_audit: list[dict[str, Any]] = []
            if anchor_service.mode != "off":
                for candidate in selected:
                    try:
                        anchor_audit.append(anchor_service.refresh(
                            session,
                            candidate_id=int(candidate.candidate_id),
                            allow_collecting=True,
                        ))
                    except Exception as exc:  # isolate one product/provider failure
                        anchor_audit.append({
                            "candidate_id": candidate.candidate_id,
                            "status": "blocked",
                            "error_code": "risk_anchor_internal_error",
                            "error_message": str(exc)[:500],
                        })
            latest_anchors = {
                row.candidate_id: repository.latest_risk_anchor(session, int(row.candidate_id))
                for row in selected
            }
            anchor_audit_by_candidate = {
                row.get("candidate_id"): row
                for row in anchor_audit
                if isinstance(row, dict) and row.get("candidate_id") is not None
            }
            anchor_statuses: dict[str, int] = defaultdict(int)
            plan_ready = 0
            for candidate in selected:
                anchor = latest_anchors.get(candidate.candidate_id)
                if anchor is None:
                    audit_status = (anchor_audit_by_candidate.get(candidate.candidate_id) or {}).get(
                        "status"
                    )
                    anchor_statuses["blocked" if audit_status == "blocked" else "pending"] += 1
                    continue
                anchor_statuses[anchor.status] += 1
                if (
                    anchor_service.mode == "active"
                    and anchor.status == "ready"
                    and candidate.quote_status == "available"
                    and environment.status == "ready"
                    and environment.environment_factor > 0
                    and run.exit_status in {"ready", "no_holdings"}
                ):
                    plan_ready += 1
            candidates = repository.list_candidates(session, run.run_id)
            quote_failures = sum(
                1 for row in candidates
                if row.screen_status == "ready" and row.quote_status != "available"
            )

            quote_request = {
                "source": "bitget_public_quote", "candidate_tm_ids": [row.tm_id for row in selected],
                "retrieved_at": utc_now_text(),
            }
            self._archive_stage(
                as_of_date=as_of_date, name="quotes", request=quote_request,
                response=quote_audit,
            )
            stock_members = sum(1 for row in members if row["root_asset"] == "美股")
            etf_members = sum(1 for row in members if row["root_asset"] == "美国ETF")
            stock_intersection = sum(1 for row in matched if row["source"]["root_asset"] == "美股")
            etf_intersection = sum(1 for row in matched if row["source"]["root_asset"] == "美国ETF")
            right_side = sum(
                1 for row in candidates
                if row.asset_type in {"stock", "etf"} and row.right_side_calendar_days is not None
                and 1 <= row.right_side_calendar_days < 10
            )
            sector_temperature_warm_plus = sum(
                1 for row in candidates
                if row.asset_type == "stock" and row.gate_passed
            )
            etf_signal_gate_passed = sum(
                1 for row in candidates if row.asset_type == "etf" and row.gate_passed
            )
            relative_strength_90_plus = sum(
                1 for row in quality_candidates
                if row.strength_local is not None and row.strength_local >= Decimal("90")
            )
            actual_ledger_rows = ledger_rows_after(client, before_ledger)
            actual_cost = ledger_rows_cost(actual_ledger_rows)
            actual_breakdown, actual_unattributed = _actual_stage_costs(
                actual_ledger_rows,
                stages=executed_stages,
                quality_fields=quality_fields,
            )
            for stage_name in executed_stages:
                self._annotate_stage_actual(
                    as_of_date=as_of_date,
                    name=stage_name,
                    actual_cost=actual_breakdown.get(stage_name),
                )
            complete_actual_attempt = executed_stages == {
                key for key, value in breakdown.items() if value > 0
            }
            actual_base = (
                sum((value for key, value in actual_breakdown.items()
                     if not key.endswith("quality_snapshot")), Decimal("0"))
                if actual_ledger_rows is not None and complete_actual_attempt else None
            )
            actual_quality = (
                sum((value for key, value in actual_breakdown.items()
                     if key.endswith("quality_snapshot")), Decimal("0"))
                if actual_ledger_rows is not None and complete_actual_attempt else None
            )
            anchor_failures = anchor_statuses.get("blocked", 0)
            etf_mapping_failures = etf_counts.get("missing", 0) + etf_counts.get("stale", 0)
            environment_blocked = environment.status != "ready"
            status = (
                "ready_degraded"
                if quote_failures or anchor_failures or etf_mapping_failures or environment_blocked
                else "ready"
            )
            funnel = {
                "warm_to_hot_stocks": stock_members,
                "warm_to_hot_etfs": etf_members,
                "bitget_stock_intersection": stock_intersection,
                "bitget_etf_intersection": etf_intersection,
                "right_side_within_window": right_side,
                "sector_temperature_warm_plus": sector_temperature_warm_plus,
                "etf_signal_gate_passed": etf_signal_gate_passed,
                "quality_complete": quality_complete,
                "relative_strength_90_plus": relative_strength_90_plus,
                "etf_benchmark_verified": etf_counts.get("verified", 0),
                "etf_benchmark_missing_or_stale": etf_mapping_failures,
                "etf_exact_exposure_duplicates": etf_counts.get("duplicate", 0),
                "market_temperature": environment.market_temperature,
                "environment_factor": decimal_text(environment.environment_factor),
                "open_h6_positions_checked": len(open_h6_lots),
                "exit_decisions": len(exit_decisions),
                "ready_for_plan": plan_ready,
                "quote_unavailable": quote_failures,
                "risk_anchor_pending": anchor_statuses.get("pending", 0),
                "risk_anchor_ready": anchor_statuses.get("ready", 0),
                "risk_anchor_blocked": anchor_failures,
                "unmatched_audit_count": len(intersection["unresolved"]) + len(component_audit),
            }
            manifest = {
                "schema_version": 2,
                "rules_version": US_MANUAL_RULES_VERSION,
                "scope": US_MANUAL_SCOPE,
                "run_id": run.run_id,
                "as_of_date": as_of_date,
                "completed_at_utc": utc_now_text(),
                "contract_hash": run.base_fields_hash,
                "requested_fields": {
                    "gate": {
                        "stock": list(BASE_SIGNAL_FIELDS),
                        "etf": list(ETF_BASE_SIGNAL_FIELDS),
                    },
                    "quality": {
                        "stock": stock_quality_fields,
                        "etf": etf_quality_fields,
                    },
                    "holding_exit": list(HOLDING_EXIT_FIELDS),
                    "market_environment": list(MARKET_ENVIRONMENT_FIELDS),
                    "forbidden": ["priceIndex", "Cl"],
                },
                "component_mode": {"getAllBasicComponentsFlag": 0, "direct_only": True},
                "component_counts": {
                    name: {"reported": counts[name], "returned_direct": len(rows_by_combo[name])}
                    for name in US_COMBO_NAMES
                },
                "cost": {
                    "cap_cny": decimal_text(run.daily_cost_cap_cny),
                    "estimated_breakdown_cny": {key: decimal_text(value) for key, value in breakdown.items()},
                    "estimated_total_cny": decimal_text(sum(breakdown.values(), Decimal("0"))),
                    "actual_total_cny": actual_cost if complete_actual_attempt else None,
                    "actual_current_attempt_cny": actual_cost,
                    "actual_breakdown_cny": {
                        key: decimal_text(value) for key, value in actual_breakdown.items()
                    },
                    "actual_unattributed_cny": decimal_text(actual_unattributed),
                },
                "bitget": {
                    "products_sha256": intersection["raw_product_sha256"],
                    "public_reality_stock_products": intersection["public_reality_stock_products"],
                    "strict_matching_only": True,
                    "unresolved": [*component_audit, *intersection["unresolved"]],
                },
                "funnel": funnel,
                "quote_audit": quote_audit,
                "market_environment": repository.model_payload(environment),
                "exit_decisions": [repository.model_payload(row) for row in exit_decisions],
                "etf_benchmark": {
                    "counts": etf_counts,
                    "audit": etf_evidence_audit,
                    "tracking_index_only": True,
                },
                "risk_anchor_mode": anchor_service.mode,
                "risk_anchor_audit": anchor_audit,
                "risk_anchor_is_exit_stop": False,
                "manual_only": True,
            }
            manifest_path = self.root / as_of_date / "manifests" / f"{run.run_id}.json"
            _write_json_atomic(manifest_path, manifest)
            run = repository.update_run(
                session, run,
                status=status,
                actual_total_cost_cny=(
                    _decimal(actual_cost)
                    if actual_cost is not None and complete_actual_attempt else None
                ),
                actual_base_cost_cny=actual_base,
                actual_enrichment_cost_cny=actual_quality,
                funnel_json=funnel,
                source_dates_json={**source_dates, "combo_counts": counts},
                exit_status=run.exit_status,
                environment_status=environment.status,
                etf_mapping_status=("degraded" if etf_mapping_failures else "ready"),
                universe_count=len(members),
                returned_count=len(stock_gate_by_tm) + len(etf_gate_by_tm),
                raw_archive_path=str(manifest_path),
                raw_sha256=sha256(manifest),
                completed_at=utc_now(),
                next_retry_at=None,
                error_code=("market_environment_blocked" if environment_blocked else
                            "etf_benchmark_blocked" if etf_mapping_failures else
                            "risk_anchor_blocked" if anchor_failures else
                            "quote_unavailable" if quote_failures else None),
                error_message=(
                    "美股整体温度缺失或不可识别，今日不开新仓"
                    if environment_blocked else
                    f"{etf_mapping_failures} 只 ETF 基准证据缺失或过期"
                    if etf_mapping_failures else
                    f"{anchor_failures} 只候选前期重要低点证据被阻断"
                    if anchor_failures else
                    f"{quote_failures} 只候选暂缺 Bitget 公开报价"
                    if quote_failures else None
                ),
            )
            return self._run_payload(session, run)
        except _BudgetStop:
            assert run is not None
            session.refresh(run)
            return self._run_payload(session, run, cache_state="cost_blocked")
        except TrendAnimalsError as exc:
            error = UsManualError(exc.code, str(exc), 503)
            if run is not None:
                repository.update_run(
                    session, run, status="failed", error_code=error.code,
                    error_message=error.message, next_retry_at=_next_retry(local_now),
                )
            raise error from exc
        except UsManualError as exc:
            if run is not None and run.status not in {
                "budget_blocked", "exit_data_blocked", "exit_ready_buy_blocked_budget",
            }:
                repository.update_run(
                    session, run, status="failed", error_code=exc.code,
                    error_message=exc.message, next_retry_at=_next_retry(local_now),
                )
            raise
        finally:
            if run is not None:
                try:
                    repository.release_run_lease(session, run_id=run.run_id, owner=owner)
                except Exception:
                    session.rollback()
            client.close()

    def refresh_quote(self, session: Session, *, candidate_id: int) -> dict[str, Any]:
        candidate = repository.get_candidate(session, candidate_id)
        run = repository.get_run(session, candidate.run_id)
        latest = repository.latest_run(session, scope=US_MANUAL_SCOPE)
        if latest is None or latest.run_id != run.run_id:
            raise UsManualError(
                "historical_candidate_read_only",
                "该候选不是当前活动 H6 候选；请从当前清单重新选择",
                409,
            )
        if (run.rules_version != US_MANUAL_RULES_VERSION
                or candidate.asset_type not in {"stock", "etf"}):
            raise UsManualError("candidate_not_executable", "只有当前 H6 个股或 ETF 候选可以刷新公开报价", 422)
        if candidate.screen_status != "ready" or not candidate.venue_instrument:
            raise UsManualError("candidate_not_plan_ready", "候选尚未通过 H6 筛选纪律", 422)
        quote = self.quote_fetcher(candidate.venue_instrument)
        candidate.reference_price_usdt = _decimal(quote.get("reference_price"))
        candidate.reference_price_at = _parse_timestamp(quote.get("quoted_at"))
        candidate.reference_price_source = "bitget_public_quote"
        candidate.quote_status = "available"
        repository.save_candidates(session, [candidate])
        candidates = repository.list_candidates(session, run.run_id)
        funnel = dict(run.funnel_json or {})
        funnel["quote_unavailable"] = sum(
            1 for row in candidates if row.screen_status == "ready" and row.quote_status != "available"
        )
        repository.update_run(session, run, funnel_json=funnel)
        session.refresh(candidate)
        return self._risk_anchor_service().candidate_payload(session, candidate)

    @staticmethod
    def schedule_payload(local_now: datetime | None = None) -> dict[str, Any]:
        now = local_now or shanghai_now()
        return {
            "timezone": config.US_MANUAL_TIMEZONE,
            "manual_full_collection_after": config.US_MANUAL_MANUAL_START,
            "automatic_slots": ["08:00", "08:30", "09:00", "09:30", "10:00"],
            "automatic_cutoff": config.US_MANUAL_AUTO_CUTOFF,
            "scheduler_enabled": bool(config.US_MANUAL_SCHEDULER_ENABLED),
            "now_local": now.replace(microsecond=0).isoformat(),
        }


# Compatibility for older internal imports. Historical classes do not change
# persisted H1-H5 rows; they only keep application wiring/tests import-safe.
UsH5Collector = UsH6Collector
UsH4Collector = UsH6Collector
UsH3Collector = UsH6Collector
