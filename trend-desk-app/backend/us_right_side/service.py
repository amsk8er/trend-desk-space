"""美股右侧资产页用例编排。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from sqlmodel import Session, select

from backend import config
from backend.db import (
    UsRightSideAssetSnapshot, UsRightSideIndustrySnapshot, UsRightSideRun,
)
from backend.trend_animals.client import TrendAnimalsClient
from backend.trend_animals.service import ledger_mark, ledger_rows_after, ledger_rows_cost
from .contracts import (
    AGE_FIELDS, DEEP_FIELDS, INDUSTRY_FIELDS, RULES_VERSION, SCREEN_FIELDS, SCOPE,
    STANDARD_ASSET_FIELDS, STRENGTH_FIELDS, UsRightSideError, field_set_hash,
    sort_capabilities,
)
from .pipeline import (
    persist_age_batch, persist_deep_batch, persist_industry_batch, persist_screen_batch,
    persist_standard_batch, persist_strength_batch,
)
from .planning import remaining_plan, require_budget, require_preflight, snapshot_plan
from .repository import (
    archive_dir, asset_payload, assets_for_run, file_sha256, get_run, industries_for_run,
    latest_run, latest_universe_seed, payload_sha256, run_payload, us_stock_universe, write_json,
)
from .sorting import (
    cursor_anchor, cursor_binding, decode_cursor, encode_cursor, filters_hash, sort_assets,
)


def _decimal(value: float | str | Decimal | None) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _accumulated(existing: Decimal | None, added: Decimal | None) -> Decimal | None:
    if existing is None and added is None:
        return None
    return (existing or Decimal("0")) + (added or Decimal("0"))


def _observed_cost(client: Any, mark: Any) -> Decimal | None:
    try:
        return _decimal(ledger_rows_cost(ledger_rows_after(client, mark)))
    except Exception:
        return None


class UsRightSideService:
    def __init__(self, client_factory: Callable[[], Any] = TrendAnimalsClient):
        self.client_factory = client_factory

    @contextmanager
    def _client(self) -> Iterator[Any]:
        client = self.client_factory()
        try:
            yield client
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _completed_batches(run: UsRightSideRun, stage: str) -> set[int]:
        progress = dict((run.progress_json or {}).get(stage) or {})
        explicit = progress.get("completed_batch_indexes")
        if isinstance(explicit, list):
            return {int(value) for value in explicit}
        return set(range(int(progress.get("completed_batches") or 0)))

    @staticmethod
    def _progress(run: UsRightSideRun, stage: str, *, completed: set[int], total: int,
                  summaries: dict[str, dict] | None = None) -> None:
        previous = dict((run.progress_json or {}).get(stage) or {})
        run.progress_json = {
            **(run.progress_json or {}),
            stage: {
                **previous,
                "completed_batches": len(completed),
                "completed_batch_indexes": sorted(completed),
                "total_batches": total,
                **({"batch_summaries": summaries} if summaries is not None else {}),
            },
        }

    @contextmanager
    def _lease(self, session: Session, run: UsRightSideRun) -> Iterator[None]:
        now = datetime.utcnow()
        if run.lease_owner and run.lease_expires_at and run.lease_expires_at > now:
            raise UsRightSideError(
                "run_already_active", "该运行正在另一请求中执行，请稍后刷新",
                status_code=409,
            )
        owner = uuid4().hex
        run.lease_owner = owner
        run.lease_expires_at = now + timedelta(minutes=config.US_RIGHT_SIDE_LEASE_MINUTES)
        session.add(run); session.commit()
        try:
            yield
        finally:
            session.refresh(run)
            if run.lease_owner == owner:
                run.lease_owner = None
                run.lease_expires_at = None
                session.add(run); session.commit()

    def capabilities(self) -> dict:
        return {
            "enabled": config.US_RIGHT_SIDE_ENABLED,
            "scope": SCOPE,
            "rules_version": RULES_VERSION,
            "research_only": True,
            "automated_trading": False,
            "automatic_paid_refresh": False,
            "default_page_size": 40,
            "notice": "右侧状态是趋势事实，不是买入或仓位指令。",
        }

    def _enabled(self) -> None:
        if not config.US_RIGHT_SIDE_ENABLED:
            raise UsRightSideError("us_right_side_disabled", "美股右侧资产页已关闭", status_code=404)

    @staticmethod
    def _us_status(status_rows: list[dict]) -> dict:
        matches = [row for row in status_rows if isinstance(row, dict) and row.get("asset") == "美股"]
        if len(matches) != 1 or not matches[0].get("asOfDate"):
            raise UsRightSideError("api_contract_error", "更新状态中的美股根节点缺失或不唯一")
        return matches[0]

    def overview(self, session: Session) -> dict:
        self._enabled()
        run = latest_run(session)
        try:
            universe_path, seed = latest_universe_seed()
            universe = us_stock_universe(seed)
            universe_summary = {
                "available": True,
                "membership_as_of_date": seed.get("as_of_date"),
                "stock_count": len(universe),
                "path": str(universe_path),
                "sha256": file_sha256(universe_path),
            }
        except UsRightSideError as exc:
            universe_summary = {"available": False, "error": exc.as_payload()}
        return {
            "capabilities": self.capabilities(),
            "universe": universe_summary,
            "run": run_payload(run) if run else None,
            "sort_capabilities": sort_capabilities(
                age_ready=bool(run and (run.age_ready or run.standard_ready)),
                strength_ready=bool(run and (run.strength_ready or run.standard_ready)),
                standard_ready=bool(run and run.standard_ready),
                industry_ready=bool(run and run.industry_ready),
                deep_ready=bool(run and run.deep_ready),
            ),
        }

    def run_status(self, session: Session, run_id: str) -> dict:
        self._enabled()
        return run_payload(get_run(session, run_id))

    def universe_preflight(self) -> dict:
        self._enabled()
        try:
            path, seed = latest_universe_seed()
            current = {
                "available": True,
                "membership_as_of_date": seed.get("as_of_date"),
                "stock_count": len(us_stock_universe(seed)),
                "path": str(path),
                "sha256": file_sha256(path),
            }
        except UsRightSideError as exc:
            current = {"available": False, "error": exc.as_payload()}
        with self._client() as client:
            docs = client.get_api_doc_intro()
            changes = client.get_change_log()
            billing = client.get_snapshot_billing()
            status = self._us_status(client.get_update_status())
        component = next((row for row in docs if row.get("ApiName") == "getComponentTicker"), {})
        available_fields = sorted({
            str(row.get("columnName")) for row in billing
            if isinstance(row, dict) and row.get("columnName")
        })
        required_fields = sorted(set(
            SCREEN_FIELDS + AGE_FIELDS + STRENGTH_FIELDS + STANDARD_ASSET_FIELDS
            + INDUSTRY_FIELDS + DEEP_FIELDS))
        return {
            "current": current,
            "upstream": {
                "tm_id": status.get("tmId"),
                "as_of_date": status.get("asOfDate"),
                "update_dt": status.get("updateDt"),
            },
            "billing_model": component.get("billingModel"),
            "estimated_cost_cny": None,
            "requires_unbounded_confirmation": True,
            "note": "all_basic 展开行数无法在调用前由 constituentCount 可靠估算。",
            "free_evidence": {
                "api_doc_sha256": payload_sha256(docs),
                "change_log_sha256": payload_sha256(changes),
                "billing_sha256": payload_sha256(billing),
                "update_status_sha256": payload_sha256(status),
                "api_count": len(docs) if isinstance(docs, list) else None,
                "billing_field_count": len(available_fields),
                "latest_change": changes[0] if isinstance(changes, list) and changes else None,
                "missing_required_fields": sorted(set(required_fields) - set(available_fields)),
            },
        }

    def refresh_universe(self, *, confirm_unbounded_all_basic: bool,
                         approved_budget_cny: float | None) -> dict:
        self._enabled()
        if not confirm_unbounded_all_basic or approved_budget_cny is None or approved_budget_cny <= 0:
            raise UsRightSideError(
                "budget_confirmation_required",
                "刷新全量美股覆盖需要确认未知展开行数风险并填写预算",
                context={"estimated_cost_cny": None, "approved_budget_cny": approved_budget_cny},
            )
        with self._client() as client:
            status = self._us_status(client.get_update_status())
            root_tm_id = int(status["tmId"])
            as_of_date = str(status["asOfDate"])
            destination = config.DATA / "research" / "trend_animals" / "us_coverage" / as_of_date
            seed_path = destination / "current_universe_seed.json"
            if seed_path.exists():
                seed = json.loads(seed_path.read_text())
                return {
                    "cached": True,
                    "as_of_date": as_of_date,
                    "stock_count": len(us_stock_universe(seed)),
                    "path": str(seed_path),
                    "actual_cost_cny": "0",
                }
            before = ledger_mark(client)
            rows = client.get_components(root_tm_id, all_basic=True)
            actual = ledger_rows_cost(ledger_rows_after(client, before))
        if not isinstance(rows, list):
            raise UsRightSideError("api_contract_error", "美股覆盖返回不是数组")
        instruments = []
        seen: set[int] = set()
        excluded_other_dates = 0
        for raw in rows:
            if not isinstance(raw, dict) or raw.get("asset") != "美股":
                continue
            try:
                tm_id = int(raw.get("tmId"))
            except (TypeError, ValueError):
                continue
            if tm_id in seen:
                raise UsRightSideError("universe_invalid", f"覆盖返回重复 tmId={tm_id}")
            seen.add(tm_id)
            if raw.get("asOfDate") != as_of_date:
                excluded_other_dates += 1
                continue
            instruments.append({
                "root_asset": "美股", "tmId": tm_id,
                "tickerName": raw.get("tickerName"), "tickerSymbol": raw.get("tickerSymbol"),
                "asset": raw.get("asset"), "assetCategory": raw.get("assetCategory"),
                "currencyDefault": raw.get("currencyDefault"), "asOfDate": raw.get("asOfDate"),
            })
        seed = {
            "schema_version": 1,
            "scope": "trend_animals_us_current_universe_seed_v1",
            "as_of_date": as_of_date,
            "instrument_count": len(instruments),
            "unique_tm_ids": len(seen),
            "excluded_other_as_of_dates": {"美股": excluded_other_dates},
            "scope_note": "趋势动物同日美股覆盖身份种子，不是交易建议或券商可交易池。",
            "instruments": instruments,
        }
        raw_sha = write_json(destination / "components" / f"root_{root_tm_id}.json", rows)
        seed_sha = write_json(seed_path, seed)
        write_json(destination / "manifest.json", {
            "schema_version": 1, "status": "complete", "as_of_date": as_of_date,
            "root_tm_id": root_tm_id, "returned_rows": len(rows),
            "stock_count": len(instruments), "excluded_other_dates": excluded_other_dates,
            "raw_sha256": raw_sha, "seed_sha256": seed_sha,
            "actual_cost_cny": actual, "approved_budget_cny": approved_budget_cny,
        })
        return {
            "cached": False, "as_of_date": as_of_date, "stock_count": len(instruments),
            "path": str(seed_path), "actual_cost_cny": str(actual) if actual is not None else None,
        }

    def scan_preflight(self, session: Session) -> dict:
        self._enabled()
        universe_path, seed = latest_universe_seed()
        universe = us_stock_universe(seed)
        universe_hash = file_sha256(universe_path)
        with self._client() as client:
            billing = client.get_snapshot_billing()
            status = self._us_status(client.get_update_status())
        as_of_date = str(status["asOfDate"])
        membership_as_of_date = str(seed.get("as_of_date") or universe_path.parent.name)
        if membership_as_of_date != as_of_date:
            raise UsRightSideError(
                "universe_not_ready",
                "美股成员范围日期落后于趋势数据日，请先刷新覆盖范围",
                status_code=409,
                context={
                    "membership_as_of_date": membership_as_of_date,
                    "trend_as_of_date": as_of_date,
                },
            )
        plan = snapshot_plan([row["tmId"] for row in universe], SCREEN_FIELDS, billing)
        existing = session.exec(select(UsRightSideRun).where(
            UsRightSideRun.as_of_date == as_of_date,
            UsRightSideRun.scope == SCOPE,
            UsRightSideRun.universe_sha256 == universe_hash,
            UsRightSideRun.screen_fields_hash == field_set_hash(SCREEN_FIELDS),
        )).first()
        if existing is not None:
            plan = remaining_plan(plan, self._completed_batches(existing, "screen"))
            existing.cache_hit = existing.status in {
                "screen_ready", "awaiting_standard_budget", "standard_enriching",
                "standard_partial", "awaiting_age_budget", "age_enriching", "age_partial",
                "awaiting_strength_budget", "strength_enriching", "strength_partial",
                "awaiting_industry_budget", "industry_enriching", "ready",
            }
            session.add(existing); session.commit(); session.refresh(existing)
            return {**run_payload(existing), "preflight": plan}

        run = UsRightSideRun(
            run_id=f"usr-{as_of_date.replace('-', '')}-{uuid4().hex[:10]}",
            as_of_date=as_of_date,
            membership_as_of_date=membership_as_of_date,
            upstream_update_dt=str(status.get("updateDt") or "") or None,
            scope=SCOPE,
            rules_version=RULES_VERSION,
            status="awaiting_screen_budget",
            universe_path=str(universe_path),
            universe_sha256=universe_hash,
            screen_fields=list(SCREEN_FIELDS), screen_fields_hash=field_set_hash(SCREEN_FIELDS),
            age_fields=list(AGE_FIELDS), age_fields_hash=field_set_hash(AGE_FIELDS),
            strength_fields=list(STRENGTH_FIELDS),
            strength_fields_hash=field_set_hash(STRENGTH_FIELDS),
            standard_fields=list(STANDARD_ASSET_FIELDS),
            standard_fields_hash=field_set_hash(STANDARD_ASSET_FIELDS),
            industry_fields=list(INDUSTRY_FIELDS), industry_fields_hash=field_set_hash(INDUSTRY_FIELDS),
            deep_fields=list(DEEP_FIELDS), deep_fields_hash=field_set_hash(DEEP_FIELDS),
            estimated_screen_cost_cny=_decimal(plan["estimated_cost_cny"]),
            universe_count=len(universe),
            progress_json={"screen": {"completed_batches": 0, "total_batches": plan["batch_count"]}},
            cost_breakdown_json={"screen": plan},
        )
        session.add(run); session.commit(); session.refresh(run)
        return {**run_payload(run), "preflight": plan}

    def _load_run_universe(self, run: UsRightSideRun) -> tuple[list[dict], dict[int, dict]]:
        path = Path(run.universe_path)
        if not path.exists() or file_sha256(path) != run.universe_sha256:
            raise UsRightSideError("archive_hash_mismatch", "美股成员归档缺失或哈希不一致")
        seed = json.loads(path.read_text())
        universe = us_stock_universe(seed)
        return universe, {row["tmId"]: row for row in universe}

    def run_scan(self, session: Session, run_id: str, approved_budget_cny: float,
                 preflight_hash: str) -> dict:
        run = get_run(session, run_id)
        with self._lease(session, run):
            return self._run_scan(session, run_id, approved_budget_cny, preflight_hash)

    def _run_scan(self, session: Session, run_id: str, approved_budget_cny: float,
                  preflight_hash: str) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        if run.status not in {"awaiting_screen_budget", "screen_partial", "screening"}:
            return run_payload(run)
        universe, universe_by_tm = self._load_run_universe(run)
        with self._client() as client:
            billing = client.get_snapshot_billing()
            status = self._us_status(client.get_update_status())
            if status.get("asOfDate") != run.as_of_date:
                raise UsRightSideError("data_date_mismatch", "趋势动物美股数据日已经变化，请重新预检")
            full_plan = snapshot_plan([row["tmId"] for row in universe], SCREEN_FIELDS, billing)
            completed_indexes = self._completed_batches(run, "screen")
            plan = remaining_plan(full_plan, completed_indexes)
            require_preflight(plan, preflight_hash)
            require_budget(plan["estimated_cost_cny"], approved_budget_cny)
            run.status = "screening"
            run.approved_screen_budget_cny = _decimal(approved_budget_cny)
            run.estimated_screen_cost_cny = _decimal(plan["estimated_cost_cny"])
            session.add(run); session.commit()
            ledger_before = ledger_mark(client)
            destination = archive_dir(run.as_of_date, run.run_id)
            progress = dict((run.progress_json or {}).get("screen") or {})
            summaries = dict(progress.get("batch_summaries") or {})
            right_side = sum(int(item.get("right_side") or 0) for item in summaries.values())
            unknown = sum(int(item.get("unknown") or 0) for item in summaries.values())
            try:
                for part in plan["batches"]:
                    rows = client.get_snapshot(part["tm_ids"], list(SCREEN_FIELDS))
                    relative = f"screen_batches/{part['batch_index']:03d}.json"
                    raw_sha256 = write_json(destination / relative, rows)
                    summary = persist_screen_batch(
                        session, run_id=run.run_id, universe_by_tm=universe_by_tm,
                        tm_ids=part["tm_ids"], rows=rows, as_of_date=run.as_of_date,
                        raw_archive_ref=relative,
                    )
                    if summary["unknown"] == 0 and summary["returned"] == len(part["tm_ids"]):
                        completed_indexes.add(int(part["batch_index"]))
                    summaries[str(part["batch_index"])] = {**summary, "raw_sha256": raw_sha256}
                    right_side = sum(int(item.get("right_side") or 0) for item in summaries.values())
                    unknown = sum(int(item.get("unknown") or 0) for item in summaries.values())
                    run.scanned_count = sum(int(item.get("returned") or 0) for item in summaries.values())
                    run.right_side_count = right_side
                    run.unknown_count = unknown
                    self._progress(
                        run, "screen", completed=completed_indexes,
                        total=full_plan["total_batch_count"], summaries=summaries,
                    )
                    session.add(run); session.commit()
            except Exception as exc:
                run.actual_screen_cost_cny = _accumulated(
                    run.actual_screen_cost_cny, _observed_cost(client, ledger_before),
                )
                run.status = "screen_partial"
                run.error_code = getattr(exc, "code", "screen_failed")
                run.error_message = str(exc)
                session.add(run); session.commit()
                raise
            actual = ledger_rows_cost(ledger_rows_after(client, ledger_before))
        run.actual_screen_cost_cny = _accumulated(run.actual_screen_cost_cny, _decimal(actual))
        run.status = "screen_ready" if unknown == 0 else "screen_partial"
        run.error_code = None if unknown == 0 else "screen_incomplete"
        run.error_message = None if unknown == 0 else f"{unknown} 个品种右侧状态未知"
        manifest = {
            "run_id": run.run_id,
            "as_of_date": run.as_of_date,
            "membership_as_of_date": run.membership_as_of_date,
            "scope": SCOPE,
            "universe_count": len(universe),
            "right_side_count": right_side,
            "unknown_count": unknown,
            "screen_fields": list(SCREEN_FIELDS),
            "batches": [
                {"batch_index": int(index), **summary}
                for index, summary in sorted(summaries.items(), key=lambda item: int(item[0]))
            ],
            "estimated_cost_cny": plan["estimated_cost_cny"],
            "actual_cost_cny": actual,
        }
        destination = archive_dir(run.as_of_date, run.run_id)
        run.manifest_path = str(destination / "manifest.json")
        run.manifest_sha256 = write_json(destination / "manifest.json", manifest)
        if run.status == "screen_ready" and len(completed_indexes) == full_plan["total_batch_count"]:
            write_json(destination / "right_side_index.json", [
                {"tmId": asset.tm_id, "tickerSymbol": asset.ticker_symbol, "tickerName": asset.ticker_name}
                for asset in assets_for_run(session, run.run_id)
            ])
        run.updated_at = datetime.utcnow()
        session.add(run); session.commit(); session.refresh(run)
        return run_payload(run)

    def age_preflight(self, session: Session, run_id: str) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        if run.scanned_count != run.universe_count or run.unknown_count:
            raise UsRightSideError("screen_incomplete", "右侧扫描尚不完整，不能初筛右侧天数")
        assets = assets_for_run(session, run_id)
        with self._client() as client:
            billing = client.get_snapshot_billing()
        plan = remaining_plan(
            snapshot_plan([row.tm_id for row in assets], AGE_FIELDS, billing),
            self._completed_batches(run, "age"),
        )
        run.age_fields = list(AGE_FIELDS)
        run.age_fields_hash = field_set_hash(AGE_FIELDS)
        run.strength_fields = list(STRENGTH_FIELDS)
        run.strength_fields_hash = field_set_hash(STRENGTH_FIELDS)
        run.standard_fields = list(STANDARD_ASSET_FIELDS)
        run.standard_fields_hash = field_set_hash(STANDARD_ASSET_FIELDS)
        if run.strength_covered_count == 0 and run.standard_covered_count == 0:
            run.estimated_strength_cost_cny = None
            run.approved_strength_budget_cny = None
            run.estimated_standard_cost_cny = None
            run.approved_standard_budget_cny = None
            breakdown = dict(run.cost_breakdown_json or {})
            breakdown.pop("strength", None)
            breakdown.pop("standard", None)
            run.cost_breakdown_json = breakdown
        run.estimated_age_cost_cny = _decimal(plan["estimated_cost_cny"])
        run.status = "awaiting_age_budget" if not run.age_ready else run.status
        run.cost_breakdown_json = {**(run.cost_breakdown_json or {}), "age": plan}
        session.add(run); session.commit(); session.refresh(run)
        return {**run_payload(run), "preflight": plan}

    def run_age(self, session: Session, run_id: str, approved_budget_cny: float,
                preflight_hash: str) -> dict:
        run = get_run(session, run_id)
        with self._lease(session, run):
            return self._run_age(session, run_id, approved_budget_cny, preflight_hash)

    def _run_age(self, session: Session, run_id: str, approved_budget_cny: float,
                 preflight_hash: str) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        if run.age_ready:
            return run_payload(run)
        assets = assets_for_run(session, run_id)
        tm_ids = [row.tm_id for row in assets]
        with self._client() as client:
            billing = client.get_snapshot_billing()
            full_plan = snapshot_plan(tm_ids, AGE_FIELDS, billing)
            completed_indexes = self._completed_batches(run, "age")
            plan = remaining_plan(full_plan, completed_indexes)
            require_preflight(plan, preflight_hash)
            require_budget(plan["estimated_cost_cny"], approved_budget_cny)
            run.status = "age_enriching"
            run.approved_age_budget_cny = _decimal(approved_budget_cny)
            session.add(run); session.commit()
            before = ledger_mark(client)
            returned = run.age_covered_count
            destination = archive_dir(run.as_of_date, run.run_id)
            try:
                for part in plan["batches"]:
                    rows = client.get_snapshot(part["tm_ids"], list(AGE_FIELDS))
                    relative = f"age_batches/{part['batch_index']:03d}.json"
                    write_json(destination / relative, rows)
                    summary = persist_age_batch(
                        session, run_id=run.run_id, tm_ids=part["tm_ids"], rows=rows,
                        as_of_date=run.as_of_date, raw_archive_ref=relative,
                    )
                    if summary["returned"] == len(part["tm_ids"]):
                        completed_indexes.add(int(part["batch_index"]))
                    returned = sum(
                        len(full_plan["batches"][index]["tm_ids"]) for index in completed_indexes)
                    run.age_covered_count = returned
                    self._progress(run, "age", completed=completed_indexes,
                                   total=full_plan["total_batch_count"])
                    session.add(run); session.commit()
            except Exception as exc:
                run.actual_age_cost_cny = _accumulated(
                    run.actual_age_cost_cny, _observed_cost(client, before))
                run.status = "age_partial"
                run.error_code = getattr(exc, "code", "age_enrichment_failed")
                run.error_message = str(exc)
                session.add(run); session.commit()
                raise
            actual = ledger_rows_cost(ledger_rows_after(client, before))
        run.actual_age_cost_cny = _accumulated(run.actual_age_cost_cny, _decimal(actual))
        run.age_ready = len(completed_indexes) == full_plan["total_batch_count"]
        run.status = "awaiting_strength_budget" if run.age_ready else "age_partial"
        run.error_code = None if run.age_ready else "age_fields_incomplete"
        run.error_message = None if run.age_ready else f"右侧天数仅返回 {returned}/{len(tm_ids)}"
        run.updated_at = datetime.utcnow()
        session.add(run); session.commit(); session.refresh(run)
        return run_payload(run)

    @staticmethod
    def _age_window_targets(assets: list[UsRightSideAssetSnapshot],
                            max_days: int = 30) -> list[UsRightSideAssetSnapshot]:
        return [row for row in assets if row.days_since_trend_entry is not None
                and row.days_since_trend_entry <= max_days]

    @classmethod
    def _top_strength_targets(cls, assets: list[UsRightSideAssetSnapshot], *,
                              max_days: int = 30, top_n: int = 100) -> list[UsRightSideAssetSnapshot]:
        eligible = [row for row in cls._age_window_targets(assets, max_days)
                    if row.strength_local_curr is not None]
        return sorted(
            eligible,
            key=lambda row: (-float(row.strength_local_curr), row.ticker_symbol, row.tm_id),
        )[:top_n]

    def strength_preflight(self, session: Session, run_id: str, max_days: int = 30) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        if not run.age_ready:
            raise UsRightSideError("age_fields_incomplete", "请先补齐全部右侧资产的右侧天数")
        if run.status == "screen_partial":
            raise UsRightSideError("screen_incomplete", "右侧扫描尚不完整，不能增强")
        if max_days != 30:
            raise UsRightSideError("payload_invalid", "强度预筛固定使用最近 30 天", status_code=422)
        completed = self._completed_batches(run, "strength")
        assets = self._age_window_targets(assets_for_run(session, run_id), max_days)
        with self._client() as client:
            billing = client.get_snapshot_billing()
        plan = remaining_plan(
            snapshot_plan([row.tm_id for row in assets], STRENGTH_FIELDS, billing),
            completed,
        )
        run.strength_fields = list(STRENGTH_FIELDS)
        run.strength_fields_hash = field_set_hash(STRENGTH_FIELDS)
        run.strength_max_days = max_days
        run.strength_target_count = len(assets)
        run.estimated_strength_cost_cny = _decimal(plan["estimated_cost_cny"])
        if not completed and not run.strength_ready:
            run.standard_fields = list(STANDARD_ASSET_FIELDS)
            run.standard_fields_hash = field_set_hash(STANDARD_ASSET_FIELDS)
            run.standard_covered_count = 0
            run.standard_target_count = 0
            run.standard_max_days = max_days
            run.standard_top_n = 100
            run.estimated_standard_cost_cny = None
            run.approved_standard_budget_cny = None
            breakdown = dict(run.cost_breakdown_json or {})
            breakdown.pop("standard", None)
            run.cost_breakdown_json = breakdown
        run.status = "awaiting_strength_budget" if not run.strength_ready else run.status
        run.cost_breakdown_json = {**(run.cost_breakdown_json or {}), "strength": plan}
        session.add(run); session.commit(); session.refresh(run)
        return {**run_payload(run), "preflight": plan}

    def run_strength(self, session: Session, run_id: str, approved_budget_cny: float,
                     preflight_hash: str) -> dict:
        run = get_run(session, run_id)
        with self._lease(session, run):
            return self._run_strength(session, run_id, approved_budget_cny, preflight_hash)

    def _run_strength(self, session: Session, run_id: str, approved_budget_cny: float,
                      preflight_hash: str) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        if run.strength_ready:
            return run_payload(run)
        if not run.age_ready:
            raise UsRightSideError("age_fields_incomplete", "请先补齐全部右侧资产的右侧天数")
        assets = self._age_window_targets(assets_for_run(session, run_id), run.strength_max_days)
        tm_ids = [row.tm_id for row in assets]
        with self._client() as client:
            billing = client.get_snapshot_billing()
            full_plan = snapshot_plan(tm_ids, STRENGTH_FIELDS, billing)
            completed_indexes = self._completed_batches(run, "strength")
            plan = remaining_plan(full_plan, completed_indexes)
            require_preflight(plan, preflight_hash)
            require_budget(plan["estimated_cost_cny"], approved_budget_cny)
            run.status = "strength_enriching"
            run.approved_strength_budget_cny = _decimal(approved_budget_cny)
            session.add(run); session.commit()
            before = ledger_mark(client)
            returned = run.strength_covered_count
            destination = archive_dir(run.as_of_date, run.run_id)
            try:
                for part in plan["batches"]:
                    rows = client.get_snapshot(part["tm_ids"], list(STRENGTH_FIELDS))
                    relative = f"strength_batches/{part['batch_index']:03d}.json"
                    write_json(destination / relative, rows)
                    summary = persist_strength_batch(
                        session, run_id=run.run_id, tm_ids=part["tm_ids"], rows=rows,
                        as_of_date=run.as_of_date, raw_archive_ref=relative,
                    )
                    if summary["returned"] == len(part["tm_ids"]):
                        completed_indexes.add(int(part["batch_index"]))
                    returned = sum(
                        len(full_plan["batches"][index]["tm_ids"]) for index in completed_indexes)
                    run.strength_covered_count = returned
                    self._progress(run, "strength", completed=completed_indexes,
                                   total=full_plan["total_batch_count"])
                    session.add(run); session.commit()
            except Exception as exc:
                run.actual_strength_cost_cny = _accumulated(
                    run.actual_strength_cost_cny, _observed_cost(client, before))
                run.status = "strength_partial"
                run.error_code = getattr(exc, "code", "strength_enrichment_failed")
                run.error_message = str(exc)
                session.add(run); session.commit()
                raise
            actual = ledger_rows_cost(ledger_rows_after(client, before))
        run.actual_strength_cost_cny = _accumulated(run.actual_strength_cost_cny, _decimal(actual))
        run.strength_ready = len(completed_indexes) == full_plan["total_batch_count"]
        run.status = "awaiting_standard_budget" if run.strength_ready else "strength_partial"
        run.error_code = None if run.strength_ready else "strength_fields_incomplete"
        run.error_message = None if run.strength_ready else f"候选强度仅返回 {returned}/{len(tm_ids)}"
        run.updated_at = datetime.utcnow()
        session.add(run); session.commit(); session.refresh(run)
        return run_payload(run)

    def standard_preflight(self, session: Session, run_id: str,
                           top_n: int = 100) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        if not run.strength_ready:
            raise UsRightSideError("strength_fields_incomplete", "请先获取最近 30 天候选的本地强度")
        if top_n != 100:
            raise UsRightSideError("payload_invalid", "详情补齐固定使用强度 Top 100", status_code=422)
        completed = self._completed_batches(run, "standard")
        assets = self._top_strength_targets(
            assets_for_run(session, run_id), max_days=run.strength_max_days, top_n=top_n)
        with self._client() as client:
            billing = client.get_snapshot_billing()
        plan = remaining_plan(
            snapshot_plan([row.tm_id for row in assets], STANDARD_ASSET_FIELDS, billing),
            completed,
        )
        run.standard_fields = list(STANDARD_ASSET_FIELDS)
        run.standard_fields_hash = field_set_hash(STANDARD_ASSET_FIELDS)
        run.standard_max_days = run.strength_max_days
        run.standard_top_n = top_n
        run.standard_target_count = len(assets)
        run.estimated_standard_cost_cny = _decimal(plan["estimated_cost_cny"])
        run.status = "awaiting_standard_budget" if not run.standard_ready else run.status
        run.cost_breakdown_json = {**(run.cost_breakdown_json or {}), "standard": plan}
        session.add(run); session.commit(); session.refresh(run)
        return {**run_payload(run), "preflight": plan}

    def run_standard(self, session: Session, run_id: str, approved_budget_cny: float,
                     preflight_hash: str) -> dict:
        run = get_run(session, run_id)
        with self._lease(session, run):
            return self._run_standard(session, run_id, approved_budget_cny, preflight_hash)

    def _run_standard(self, session: Session, run_id: str, approved_budget_cny: float,
                      preflight_hash: str) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        if run.standard_ready:
            return run_payload(run)
        if not run.strength_ready:
            raise UsRightSideError("strength_fields_incomplete", "请先获取最近 30 天候选的本地强度")
        assets = self._top_strength_targets(
            assets_for_run(session, run_id), max_days=run.strength_max_days,
            top_n=run.standard_top_n)
        tm_ids = [row.tm_id for row in assets]
        with self._client() as client:
            billing = client.get_snapshot_billing()
            full_plan = snapshot_plan(tm_ids, STANDARD_ASSET_FIELDS, billing)
            completed_indexes = self._completed_batches(run, "standard")
            plan = remaining_plan(full_plan, completed_indexes)
            require_preflight(plan, preflight_hash)
            require_budget(plan["estimated_cost_cny"], approved_budget_cny)
            run.status = "standard_enriching"
            run.approved_standard_budget_cny = _decimal(approved_budget_cny)
            session.add(run); session.commit()
            before = ledger_mark(client)
            returned = run.standard_covered_count
            destination = archive_dir(run.as_of_date, run.run_id)
            try:
                for part in plan["batches"]:
                    rows = client.get_snapshot(part["tm_ids"], list(STANDARD_ASSET_FIELDS))
                    relative = f"standard_batches/{part['batch_index']:03d}.json"
                    write_json(destination / relative, rows)
                    summary = persist_standard_batch(
                        session, run_id=run.run_id, tm_ids=part["tm_ids"], rows=rows,
                        as_of_date=run.as_of_date, raw_archive_ref=relative,
                    )
                    if summary["returned"] == len(part["tm_ids"]):
                        completed_indexes.add(int(part["batch_index"]))
                    returned = sum(
                        len(full_plan["batches"][index]["tm_ids"]) for index in completed_indexes)
                    run.standard_covered_count = returned
                    self._progress(
                        run, "standard", completed=completed_indexes,
                        total=full_plan["total_batch_count"],
                    )
                    session.add(run); session.commit()
            except Exception as exc:
                run.actual_standard_cost_cny = _accumulated(
                    run.actual_standard_cost_cny, _observed_cost(client, before),
                )
                run.status = "standard_partial"
                run.error_code = getattr(exc, "code", "standard_enrichment_failed")
                run.error_message = str(exc)
                session.add(run); session.commit()
                raise
            actual = ledger_rows_cost(ledger_rows_after(client, before))
        run.actual_standard_cost_cny = _accumulated(run.actual_standard_cost_cny, _decimal(actual))
        run.standard_ready = len(completed_indexes) == full_plan["total_batch_count"]
        run.status = "awaiting_industry_budget" if run.standard_ready else "standard_partial"
        run.error_code = None if run.standard_ready else "standard_fields_incomplete"
        run.error_message = None if run.standard_ready else f"标准字段仅返回 {returned}/{len(tm_ids)}"
        run.updated_at = datetime.utcnow()
        session.add(run); session.commit(); session.refresh(run)
        return run_payload(run)

    @staticmethod
    def _industry_targets(assets: list[UsRightSideAssetSnapshot]) -> tuple[list[int], dict[int, str]]:
        names: dict[int, str] = {}
        for asset in assets:
            if asset.industry_tm_id is not None:
                names.setdefault(asset.industry_tm_id, asset.industry_name or "")
        return sorted(names), names

    def industry_preflight(self, session: Session, run_id: str) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        if not run.standard_ready:
            raise UsRightSideError("standard_fields_incomplete", "请先补齐全部标准字段")
        tm_ids, _ = self._industry_targets(assets_for_run(session, run_id))
        with self._client() as client:
            billing = client.get_snapshot_billing()
        plan = remaining_plan(
            snapshot_plan(tm_ids, INDUSTRY_FIELDS, billing),
            self._completed_batches(run, "industry"),
        )
        run.industry_count = len(tm_ids)
        run.estimated_industry_cost_cny = _decimal(plan["estimated_cost_cny"])
        run.status = "awaiting_industry_budget" if not run.industry_ready else run.status
        run.cost_breakdown_json = {**(run.cost_breakdown_json or {}), "industry": plan}
        session.add(run); session.commit(); session.refresh(run)
        return {**run_payload(run), "preflight": plan}

    def run_industry(self, session: Session, run_id: str, approved_budget_cny: float,
                     preflight_hash: str) -> dict:
        run = get_run(session, run_id)
        with self._lease(session, run):
            return self._run_industry(session, run_id, approved_budget_cny, preflight_hash)

    def _run_industry(self, session: Session, run_id: str, approved_budget_cny: float,
                      preflight_hash: str) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        if run.industry_ready:
            return run_payload(run)
        tm_ids, names = self._industry_targets(assets_for_run(session, run_id))
        with self._client() as client:
            billing = client.get_snapshot_billing()
            full_plan = snapshot_plan(tm_ids, INDUSTRY_FIELDS, billing)
            completed_indexes = self._completed_batches(run, "industry")
            plan = remaining_plan(full_plan, completed_indexes)
            require_preflight(plan, preflight_hash)
            require_budget(plan["estimated_cost_cny"], approved_budget_cny)
            run.status = "industry_enriching"
            run.approved_industry_budget_cny = _decimal(approved_budget_cny)
            session.add(run); session.commit()
            before = ledger_mark(client)
            returned = run.industry_covered_count
            destination = archive_dir(run.as_of_date, run.run_id)
            try:
                for part in plan["batches"]:
                    rows = client.get_snapshot(part["tm_ids"], list(INDUSTRY_FIELDS))
                    relative = f"industry_batches/{part['batch_index']:03d}.json"
                    write_json(destination / relative, rows)
                    summary = persist_industry_batch(
                        session, run_id=run.run_id, tm_ids=part["tm_ids"], rows=rows,
                        as_of_date=run.as_of_date, industry_names=names, raw_archive_ref=relative,
                    )
                    if summary["returned"] == len(part["tm_ids"]):
                        completed_indexes.add(int(part["batch_index"]))
                    returned = sum(
                        len(full_plan["batches"][index]["tm_ids"]) for index in completed_indexes)
                    run.industry_covered_count = returned
                    self._progress(
                        run, "industry", completed=completed_indexes,
                        total=full_plan["total_batch_count"],
                    )
                    session.add(run); session.commit()
            except Exception as exc:
                run.actual_industry_cost_cny = _accumulated(
                    run.actual_industry_cost_cny, _observed_cost(client, before),
                )
                run.status = "awaiting_industry_budget"
                run.error_code = getattr(exc, "code", "industry_enrichment_failed")
                run.error_message = str(exc)
                session.add(run); session.commit()
                raise
            actual = ledger_rows_cost(ledger_rows_after(client, before))
        run.actual_industry_cost_cny = _accumulated(run.actual_industry_cost_cny, _decimal(actual))
        run.industry_ready = len(completed_indexes) == full_plan["total_batch_count"]
        run.status = "ready" if run.industry_ready else "awaiting_industry_budget"
        run.error_code = None if run.industry_ready else "industry_environment_incomplete"
        run.error_message = None if run.industry_ready else f"行业环境仅返回 {returned}/{len(tm_ids)}"
        run.completed_at = datetime.utcnow() if run.industry_ready else None
        run.updated_at = datetime.utcnow()
        session.add(run); session.commit(); session.refresh(run)
        return run_payload(run)

    def deep_preflight(self, session: Session, run_id: str) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        assets = assets_for_run(session, run_id)
        with self._client() as client:
            billing = client.get_snapshot_billing()
        plan = remaining_plan(
            snapshot_plan([row.tm_id for row in assets], DEEP_FIELDS, billing),
            self._completed_batches(run, "deep"),
        )
        run.estimated_deep_cost_cny = _decimal(plan["estimated_cost_cny"])
        run.cost_breakdown_json = {**(run.cost_breakdown_json or {}), "deep": plan}
        session.add(run); session.commit(); session.refresh(run)
        return {**run_payload(run), "preflight": plan}

    def run_deep(self, session: Session, run_id: str, approved_budget_cny: float,
                 preflight_hash: str) -> dict:
        run = get_run(session, run_id)
        with self._lease(session, run):
            return self._run_deep(session, run_id, approved_budget_cny, preflight_hash)

    def _run_deep(self, session: Session, run_id: str, approved_budget_cny: float,
                  preflight_hash: str) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        assets = assets_for_run(session, run_id)
        tm_ids = [row.tm_id for row in assets]
        with self._client() as client:
            billing = client.get_snapshot_billing()
            full_plan = snapshot_plan(tm_ids, DEEP_FIELDS, billing)
            completed_indexes = self._completed_batches(run, "deep")
            plan = remaining_plan(full_plan, completed_indexes)
            require_preflight(plan, preflight_hash)
            require_budget(plan["estimated_cost_cny"], approved_budget_cny)
            before = ledger_mark(client)
            returned = run.deep_covered_count
            destination = archive_dir(run.as_of_date, run.run_id)
            for part in plan["batches"]:
                rows = client.get_snapshot(part["tm_ids"], list(DEEP_FIELDS))
                relative = f"deep_batches/{part['batch_index']:03d}.json"
                write_json(destination / relative, rows)
                summary = persist_deep_batch(
                    session, run_id=run.run_id, tm_ids=part["tm_ids"], rows=rows,
                    as_of_date=run.as_of_date, raw_archive_ref=relative,
                )
                if summary["returned"] == len(part["tm_ids"]):
                    completed_indexes.add(int(part["batch_index"]))
                returned = sum(
                    len(full_plan["batches"][index]["tm_ids"]) for index in completed_indexes)
                run.deep_covered_count = returned
                self._progress(
                    run, "deep", completed=completed_indexes,
                    total=full_plan["total_batch_count"],
                )
                session.add(run); session.commit()
            actual = ledger_rows_cost(ledger_rows_after(client, before))
        run.approved_deep_budget_cny = _decimal(approved_budget_cny)
        run.actual_deep_cost_cny = _accumulated(run.actual_deep_cost_cny, _decimal(actual))
        run.deep_covered_count = returned
        run.deep_ready = len(completed_indexes) == full_plan["total_batch_count"]
        session.add(run); session.commit(); session.refresh(run)
        return run_payload(run)

    def trend_plot(self, session: Session, run_id: str, tm_id: int) -> dict:
        self._enabled()
        get_run(session, run_id)
        asset = session.exec(select(UsRightSideAssetSnapshot).where(
            UsRightSideAssetSnapshot.run_id == run_id,
            UsRightSideAssetSnapshot.tm_id == tm_id,
        )).first()
        if asset is None:
            raise UsRightSideError("asset_not_found", "未找到右侧资产", status_code=404)
        destination = archive_dir(asset.as_of_date, run_id) / "plots" / f"{tm_id}.json"
        if destination.exists():
            payload = json.loads(destination.read_text())
            return {"tm_id": tm_id, "cached": True, "estimated_cost_cny": 0, "image": payload.get("image")}
        with self._client() as client:
            docs = client.get_api_doc_intro()
            row = next((item for item in docs if item.get("ApiName") == "getTickerTrendPlot"), None)
            if row is None:
                raise UsRightSideError("api_contract_error", "实时文档缺少趋势图接口")
            image = client.get_trend_plot(tm_id)
        write_json(destination, {"tm_id": tm_id, "image": image})
        return {"tm_id": tm_id, "cached": False, "estimated_cost_cny": 0.1, "image": image}

    def list_assets(self, session: Session, *, run_id: str | None = None,
                    query: str | None = None, temperature: str | None = None,
                    phase: str | None = None, industry: str | None = None,
                    days_min: int | None = None, days_max: int | None = None,
                    strength_min: float | None = None, risk_only: bool = False,
                    sort_by: str | None = None, sort_dir: str | None = None,
                    cursor: str | None = None, page_size: int = 40) -> dict:
        self._enabled()
        run = get_run(session, run_id) if run_id else latest_run(session)
        if run is None:
            raise UsRightSideError("run_not_found", "尚无美股右侧运行", status_code=404)
        if page_size < 1 or page_size > 100:
            raise UsRightSideError("invalid_page_size", "page_size 必须在 1–100", status_code=422)
        capabilities = sort_capabilities(
            age_ready=bool(run.age_ready or run.standard_ready),
            strength_ready=bool(run.strength_ready or run.standard_ready),
            standard_ready=run.standard_ready, industry_ready=run.industry_ready,
            deep_ready=run.deep_ready)
        active_sort = sort_by or ("strength_local_curr" if run.strength_ready else "ticker_symbol")
        if active_sort not in capabilities:
            raise UsRightSideError("invalid_sort_field", f"不支持排序字段：{active_sort}", status_code=422)
        if not capabilities[active_sort]["enabled"]:
            raise UsRightSideError(
                "sort_field_unavailable", capabilities[active_sort]["blocked_reason"], status_code=409,
                context={"sort_by": active_sort},
            )
        active_dir = sort_dir or capabilities[active_sort]["default_direction"]
        if active_dir not in {"asc", "desc"}:
            raise UsRightSideError("invalid_sort_direction", "排序方向必须是 asc 或 desc", status_code=422)

        industries = {row.industry_tm_id: row for row in industries_for_run(session, run.run_id)}
        rows = [asset_payload(asset, industries.get(asset.industry_tm_id))
                for asset in assets_for_run(session, run.run_id)]
        normalized_query = str(query or "").strip().casefold()
        filters = {
            "query": normalized_query, "temperature": temperature, "phase": phase,
            "industry": industry, "days_min": days_min, "days_max": days_max,
            "strength_min": strength_min, "risk_only": risk_only,
        }

        def keep(row: dict) -> bool:
            if normalized_query and normalized_query not in (
                f"{row.get('ticker_symbol') or ''} {row.get('ticker_name') or ''}".casefold()
            ):
                return False
            if temperature and row.get("temperature_curr") != temperature:
                return False
            if phase and row.get("phase_curr") != phase:
                return False
            if industry and row.get("industry_name") != industry:
                return False
            days = row.get("days_since_trend_entry")
            if days_min is not None and (days is None or int(days) < days_min):
                return False
            if days_max is not None and (days is None or int(days) > days_max):
                return False
            strength = row.get("strength_local_curr")
            if strength_min is not None and (strength is None or float(strength) < strength_min):
                return False
            if risk_only and not (row.get("risk_flag_count") or 0):
                return False
            return True

        filtered = [row for row in rows if keep(row)]
        ordered = sort_assets(filtered, sort_by=active_sort, sort_dir=active_dir)
        digest = filters_hash(filters)
        binding = cursor_binding(
            run_id=run.run_id, filters_digest=digest, sort_by=active_sort, sort_dir=active_dir)
        anchor = decode_cursor(
            cursor, binding=binding, secret=config.US_RIGHT_SIDE_CURSOR_SECRET)
        offset = 0
        if anchor is not None:
            for index, row in enumerate(ordered):
                if cursor_anchor(row, sort_by=active_sort) == anchor:
                    offset = index + 1
                    break
            else:
                raise UsRightSideError(
                    "invalid_sort_cursor",
                    "分页游标锚点已不存在，请回到第一页",
                    status_code=422,
                )
        page = ordered[offset:offset + page_size]
        for index, row in enumerate(page, start=offset + 1):
            row["rank"] = index
        next_offset = offset + len(page)
        next_cursor = (
            encode_cursor(
                anchor=cursor_anchor(page[-1], sort_by=active_sort), binding=binding,
                secret=config.US_RIGHT_SIDE_CURSOR_SECRET,
            )
            if page and next_offset < len(ordered) else None
        )
        return {
            "run": run_payload(run),
            "items": page,
            "total": len(ordered),
            "page_size": page_size,
            "offset": offset,
            "next_cursor": next_cursor,
            "sort": {"sort_by": active_sort, "sort_dir": active_dir},
            "sort_capabilities": capabilities,
            "filters": filters,
            "facets": {
                "industries": sorted({
                    str(row["industry_name"]) for row in rows if row.get("industry_name")
                }),
            },
        }

    def asset_detail(self, session: Session, *, run_id: str, tm_id: int) -> dict:
        self._enabled()
        run = get_run(session, run_id)
        asset = session.exec(select(UsRightSideAssetSnapshot).where(
            UsRightSideAssetSnapshot.run_id == run_id,
            UsRightSideAssetSnapshot.tm_id == tm_id,
        )).first()
        if asset is None:
            raise UsRightSideError("asset_not_found", "未找到右侧资产", status_code=404)
        industry = None
        if asset.industry_tm_id is not None:
            industry = session.exec(select(UsRightSideIndustrySnapshot).where(
                UsRightSideIndustrySnapshot.run_id == run_id,
                UsRightSideIndustrySnapshot.industry_tm_id == asset.industry_tm_id,
            )).first()
        return {"run": run_payload(run), "asset": asset_payload(asset, industry)}
