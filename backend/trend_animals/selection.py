"""趋势动物“温转热”组合 → API 行 → 聚合 → 初筛的完整流水线。"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select, delete

from backend.db import Batch, Manifest, OcrJob, OcrRow
from backend.pipeline.nodes.aggregate import run_aggregate
from backend.pipeline.nodes.prescreen import run_prescreen
from backend.pipeline.state import init_pipeline_state, transition, NodeStatus
from backend.trend_animals.billing import (
    component_pricing, endpoint_fixed_cost, ensure_budget,
    estimate_component_cost, estimate_snapshot_cost,
)
from backend.trend_animals.errors import TrendAnimalsError
from backend.trend_animals.mapper import candidate_row
from backend.trend_animals.service import (
    finish_audit, ledger_delta, ledger_mark, new_audit, require_asset_date,
)


COMBOS = {
    "温转热(A股)": "A股组合",
    "温转热(ETF基金个股)": "ETF基金组合",
}
COUNT_FIELDS = ["tmId", "tickerName", "asOfDate", "constituentCount"]
CORE_FIELDS = [
    "tmId", "tickerName", "tickerSymbol", "asset", "asOfDate",
    "industryTmId", "industryName", "marketCap", "amount1d",
    "trendTemperatureCurr", "daysSinceTrendEntry",
]
SECTOR_FIELDS = [
    "tmId", "tickerName", "tickerSymbol", "asset", "asOfDate",
    "trendTemperatureCurr",
]
ENRICH_FIELDS = [
    "tmId", "tickerName", "tickerSymbol", "asOfDate",
    "trendPhaseCurr", "trendStrengthLocalCurr", "trendStrengthLocalChange",
]
CORE_IDENTITY_FIELDS = ("tmId", "tickerName", "tickerSymbol", "asOfDate")


def _find_combos(search_rows: list[dict], expected_date: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for name in COMBOS:
        matches = [row for row in search_rows if row.get("tickerName") == name]
        if len(matches) != 1:
            raise TrendAnimalsError(
                "api_contract_error", f"组合 {name} 搜索结果应唯一，实际 {len(matches)}")
        if matches[0].get("asOfDate") != expected_date:
            raise TrendAnimalsError(
                "data_stale", f"组合 {name} 日期 {matches[0].get('asOfDate')}，期望 {expected_date}")
        found[name] = matches[0]
    return found


def _selection_estimate_from_counts(*, docs: list[dict], billing: list[dict],
                                    counts: dict[str, int],
                                    basic_counts: dict[str, int] | None = None,
                                    unique_basic_count: int | None = None) -> dict:
    # 官方文档未定义 constituentCount 与 all_basic=1 返回行数必须相等。
    # 估算阶段尚不知道实际成分行数，先用 constituentCount 占位，取得返回行后再二次校验预算。
    basic_counts = basic_counts or counts
    total = unique_basic_count if unique_basic_count is not None else sum(counts.values())
    fixed_search = endpoint_fixed_cost(docs, "searchTicker")
    base, normal_row, combo_row = component_pricing(docs)
    count_cost = estimate_snapshot_cost(COUNT_FIELDS, len(counts), billing)
    component_cost = sum(estimate_component_cost(
        count, combo=True, base_cost=base, normal_row_cost=normal_row,
        combo_row_cost=combo_row) for count in basic_counts.values())
    core_cost = estimate_snapshot_cost(CORE_FIELDS, total, billing)
    # 成分展开前不知道唯一行业数；按“每只一个行业 + A股总览”保守估算。
    sector_cost = estimate_snapshot_cost(SECTOR_FIELDS, total + 1, billing)
    enrich_cost = estimate_snapshot_cost(ENRICH_FIELDS, total, billing)
    estimated = round(fixed_search + count_cost + component_cost + core_cost
                      + sector_cost + enrich_cost, 6)
    return {
        "estimated_cost": estimated,
        "estimate_breakdown": {
            "search": fixed_search, "count_snapshot": count_cost,
            "components": component_cost, "core_snapshot": core_cost,
            "sector_snapshot_worst_case": sector_cost,
            "candidate_enrichment_worst_case": enrich_cost,
        },
    }


def estimate_selection(client, *, expected_date: str) -> dict:
    docs = client.get_api_doc_intro()
    try:
        client.get_change_log()
    except TrendAnimalsError:
        pass
    statuses = client.get_update_status()
    a_status = require_asset_date(statuses, "A股", expected_date)
    require_asset_date(statuses, "ETF基金", expected_date)
    billing = client.get_snapshot_billing()
    search_rows = client.search_ticker("温转热")
    combos = _find_combos(search_rows, expected_date)
    combo_tm_ids = [int(combos[name]["tmId"]) for name in COMBOS]
    count_rows = client.get_snapshot(combo_tm_ids, COUNT_FIELDS)
    by_tm = {int(row["tmId"]): row for row in count_rows if row.get("tmId") is not None}
    counts: dict[str, int] = {}
    for name, combo in combos.items():
        tm_id = int(combo["tmId"])
        row = by_tm.get(tm_id)
        count = row.get("constituentCount") if row else None
        if not isinstance(count, (int, float)) or int(count) < 0:
            raise TrendAnimalsError("api_contract_error", f"组合 {name} 缺少 constituentCount")
        counts[name] = int(count)
    estimate = _selection_estimate_from_counts(docs=docs, billing=billing, counts=counts)
    return {
        "docs": docs, "statuses": statuses, "billing": billing, "combos": combos,
        "counts": counts, "market_tm_id": int(a_status["tmId"]),
        "as_of_date": expected_date, **estimate,
    }


def _create_batch(s: Session, *, date: str, batch_id: str | None) -> Batch:
    if batch_id:
        batch = s.get(Batch, batch_id)
        if batch is None:
            raise TrendAnimalsError("unknown_batch", f"批次不存在：{batch_id}")
        if batch.date != date:
            raise TrendAnimalsError(
                "data_stale", f"批次日期 {batch.date} 与 API 目标日期 {date} 不一致")
        if not batch.pipeline_state:
            init_pipeline_state(s, batch_id=batch.batch_id)
        return batch
    stem = f"batch_api_{date.replace('-', '')}_{datetime.now().strftime('%H%M%S')}"
    candidate = stem
    suffix = 1
    while s.get(Batch, candidate) is not None:
        candidate = f"{stem}_{suffix}"; suffix += 1
    batch = Batch(batch_id=candidate, date=date, status="running", source_dir=None)
    s.add(batch); s.commit()
    init_pipeline_state(s, batch_id=batch.batch_id)
    transition(s, batch_id=batch.batch_id, node="import", to=NodeStatus.SKIPPED)
    return batch


def _clear_api_jobs(s: Session, batch_id: str) -> None:
    jobs = s.exec(select(OcrJob).where(
        OcrJob.batch_id == batch_id, OcrJob.backend == "trend_api")).all()
    ids = [job.job_id for job in jobs if job.job_id is not None]
    if ids:
        s.exec(delete(OcrRow).where(OcrRow.job_id.in_(ids)))
        s.exec(delete(OcrJob).where(OcrJob.job_id.in_(ids)))
        s.flush()


def _add_job(s: Session, *, batch_id: str, category: str, source: str,
             raw_rows: list[dict]) -> OcrJob:
    job = OcrJob(
        batch_id=batch_id, image_path=f"trend-api://{source}", image_index=-1,
        model="trend-animals-api", backend="trend_api", status="done", category=category,
        raw_json={"meta": {"market": "A股", "category": category,
                           "source": "trend_api", "confidence": 1.0},
                  "rows": raw_rows},
    )
    s.add(job); s.flush()
    return job


def _persist_api_rows(s: Session, *, batch_id: str, components: dict[str, list[dict]],
                      core_rows: list[dict], sector_rows: list[dict],
                      market_tm_id: int) -> dict[str, OcrRow]:
    _clear_api_jobs(s, batch_id)
    core_by_tm = {int(row["tmId"]): row for row in core_rows if row.get("tmId") is not None}
    source_names: dict[int, list[str]] = {}
    jobs: dict[str, OcrJob] = {}
    for combo_name, rows in components.items():
        jobs[combo_name] = _add_job(
            s, batch_id=batch_id, category=COMBOS[combo_name],
            source=f"component/{combo_name}", raw_rows=rows)
        for row in rows:
            source_names.setdefault(int(row["tmId"]), []).append(combo_name)

    persisted_by_code: dict[str, OcrRow] = {}
    for tm_id, combos_for_tm in source_names.items():
        raw = core_by_tm[tm_id]
        # 同一品种若同时属于两个组合，以 A股组合 job 优先；raw_fields 仍保留全部来源。
        primary = next((name for name in COMBOS if name in combos_for_tm), combos_for_tm[0])
        mapped = candidate_row(raw, combo_name=primary)
        mapped["raw_fields"]["source_combos"] = combos_for_tm
        row = OcrRow(job_id=jobs[primary].job_id, review_status="pending", **mapped)
        s.add(row); s.flush()
        if row.code:
            persisted_by_code[row.code] = row

    sector_job = _add_job(
        s, batch_id=batch_id, category="A股", source="sectors", raw_rows=sector_rows)
    industry_name_by_tm: dict[int, str] = {}
    for raw in core_rows:
        tm_id = raw.get("industryTmId")
        name = raw.get("industryName")
        if tm_id is not None and name:
            industry_name_by_tm.setdefault(int(tm_id), str(name))
    for raw in sector_rows:
        tm_id = int(raw["tmId"])
        if tm_id == market_tm_id:
            s.add(OcrRow(
                job_id=sector_job.job_id, row_type="overview", market="A股", code=None,
                name="A股", temperature_status=raw.get("trendTemperatureCurr"),
                strength=None, review_status="pending",
                raw_fields={"data_source": "trend_api", "tm_id": tm_id,
                            "as_of_date": raw.get("asOfDate")},
            ))
            continue
        s.add(OcrRow(
            job_id=sector_job.job_id, row_type="sector", market="A股", code=None,
            name=industry_name_by_tm.get(tm_id) or raw.get("tickerName"),
            temperature_status=raw.get("trendTemperatureCurr"), strength=None,
            review_status="pending",
            raw_fields={"data_source": "trend_api", "tm_id": tm_id,
                        "as_of_date": raw.get("asOfDate")},
        ))
    s.commit()
    transition(s, batch_id=batch_id, node="ocr", to=NodeStatus.DONE)
    transition(s, batch_id=batch_id, node="review", to=NodeStatus.DONE)
    return persisted_by_code


def _enrich_candidates(s: Session, *, client, manifest: dict,
                       persisted_by_code: dict[str, OcrRow]) -> tuple[int, list[str]]:
    candidate_codes = [row.get("code") for row in manifest.get("candidates", []) if row.get("code")]
    tm_ids = []
    for code in candidate_codes:
        row = persisted_by_code.get(code)
        tm_id = (row.raw_fields or {}).get("tm_id") if row else None
        if tm_id is not None:
            tm_ids.append(int(tm_id))
    if not tm_ids:
        return 0, []
    snapshots = client.get_snapshot(tm_ids, ENRICH_FIELDS)
    by_tm = {int(row["tmId"]): row for row in snapshots if row.get("tmId") is not None}
    missing = sorted(set(tm_ids) - set(by_tm))
    for code in candidate_codes:
        row = persisted_by_code.get(code)
        tm_id = (row.raw_fields or {}).get("tm_id") if row else None
        raw = by_tm.get(int(tm_id)) if tm_id is not None else None
        if row is None or raw is None:
            continue
        exact = raw.get("trendStrengthLocalCurr")
        row.strength = round(float(exact)) if isinstance(exact, (int, float)) else None
        row.jieqi = raw.get("trendPhaseCurr")
        fields = dict(row.raw_fields or {})
        fields["trend_strength_exact"] = exact
        fields["trend_strength_change_raw"] = raw.get("trendStrengthLocalChange")
        row.raw_fields = fields
        s.add(row)
    s.commit()
    return len(tm_ids) - len(missing), [str(v) for v in missing]


def _persist_component_warnings(s: Session, *, batch_id: str, manifest: dict,
                                warnings: list[dict]) -> dict:
    """把 API 计数字段差异写入最终初筛 Manifest，刷新页面后仍可审计。"""
    if not warnings:
        return manifest
    updated = dict(manifest)
    updated["api_component_count_warnings"] = warnings
    latest = s.exec(select(Manifest).where(
        Manifest.batch_id == batch_id, Manifest.stage == "prescreen"
    ).order_by(Manifest.manifest_id.desc())).first()
    if latest is None:
        raise TrendAnimalsError("api_contract_error", "初筛完成但未找到 Manifest")
    latest.manifest_json = updated
    s.add(latest); s.commit()
    return updated


def run_selection_pipeline(s: Session, *, client, date: str,
                           batch_id: str | None = None,
                           approved_budget: float | None = None,
                           etf_min_aum_yi: float | None = None,
                           etf_min_turnover_yi: float | None = None,
                           min_market_cap_yi: float | None = None,
                           min_turnover_yi: float | None = None) -> dict:
    audit = new_audit(s, scope="selection", batch_id=batch_id)
    created_batch: Batch | None = None
    try:
        before = ledger_mark(client)
        estimate = estimate_selection(client, expected_date=date)
        ensure_budget(estimate["estimated_cost"], approved_budget)
        created_batch = _create_batch(s, date=date, batch_id=batch_id)
        audit.batch_id = created_batch.batch_id; s.add(audit); s.commit()
        components: dict[str, list[dict]] = {}
        basic_counts: dict[str, int] = {}
        component_count_warnings: list[dict] = []
        for name, combo in estimate["combos"].items():
            tm_id = int(combo["tmId"])
            rows = client.get_components(tm_id, all_basic=True)
            if estimate["counts"][name] > 0 and not rows:
                raise TrendAnimalsError(
                    "missing_required_fields", f"{name} 全部子级基础品种返回为空")
            returned_tm_ids = [row.get("tmId") for row in rows]
            if any(tm_id is None for tm_id in returned_tm_ids):
                raise TrendAnimalsError("missing_required_fields", f"{name} 成分存在缺少 tmId 的行")
            if len(set(returned_tm_ids)) != len(returned_tm_ids):
                raise TrendAnimalsError("api_contract_error", f"{name} 成分返回重复 tmId")
            if any(row.get("asOfDate") != date for row in rows):
                raise TrendAnimalsError("data_stale", f"{name} 成分包含非 {date} 数据")
            if len(rows) != estimate["counts"][name]:
                component_count_warnings.append({
                    "combo": name,
                    "constituent_count": estimate["counts"][name],
                    "returned_basic_count": len(rows),
                    "note": "官方文档未说明 constituentCount 必须等于全部子级品种行数",
                })
            components[name] = rows
            basic_counts[name] = len(rows)

        tm_ids = sorted({int(row["tmId"]) for rows in components.values() for row in rows})
        realized_estimate = _selection_estimate_from_counts(
            docs=estimate["docs"], billing=estimate["billing"], counts=estimate["counts"],
            basic_counts=basic_counts, unique_basic_count=len(tm_ids))
        # 直接/展开成分调用已经发生；在更贵的核心与趋势快照前按实际展开行数再校验一次。
        ensure_budget(realized_estimate["estimated_cost"], approved_budget)
        estimate.update(realized_estimate)
        core_rows = client.get_snapshot(tm_ids, CORE_FIELDS)
        core_by_tm = {int(row["tmId"]): row for row in core_rows if row.get("tmId") is not None}
        missing_tm = sorted(set(tm_ids) - set(core_by_tm))
        if missing_tm:
            raise TrendAnimalsError("missing_required_fields", f"核心快照缺少 tmId：{missing_tm}")
        for tm_id in tm_ids:
            raw = core_by_tm[tm_id]
            missing = [field for field in CORE_IDENTITY_FIELDS if raw.get(field) is None]
            if missing:
                raise TrendAnimalsError(
                    "missing_required_fields", f"tmId={tm_id} 核心字段缺失：{missing}")
            if raw.get("asOfDate") != date:
                raise TrendAnimalsError("data_stale", f"tmId={tm_id} 快照日期非 {date}")

        industry_tm_ids = sorted({int(row["industryTmId"]) for row in core_rows
                                  if row.get("asset") == "A股" and row.get("industryTmId") is not None})
        sector_tm_ids = sorted(set(industry_tm_ids + [estimate["market_tm_id"]]))
        sector_rows = client.get_snapshot(sector_tm_ids, SECTOR_FIELDS)
        sector_returned = {int(row["tmId"]) for row in sector_rows if row.get("tmId") is not None}
        if set(sector_tm_ids) - sector_returned:
            raise TrendAnimalsError(
                "missing_required_fields",
                f"板块快照缺少 tmId：{sorted(set(sector_tm_ids) - sector_returned)}")
        if any(row.get("asOfDate") != date for row in sector_rows):
            raise TrendAnimalsError("data_stale", "板块/大盘快照包含非目标日期")

        persisted = _persist_api_rows(
            s, batch_id=created_batch.batch_id, components=components,
            core_rows=core_rows, sector_rows=sector_rows,
            market_tm_id=estimate["market_tm_id"])
        run_aggregate(s, batch_id=created_batch.batch_id)
        manifest = run_prescreen(
            s, batch_id=created_batch.batch_id,
            etf_min_aum_yi=etf_min_aum_yi,
            etf_min_turnover_yi=etf_min_turnover_yi,
            min_market_cap_yi=min_market_cap_yi,
            min_turnover_yi=min_turnover_yi,
        )

        enrichment_warning = None
        try:
            enriched, missing_enrichment = _enrich_candidates(
                s, client=client, manifest=manifest, persisted_by_code=persisted)
            if enriched:
                run_aggregate(s, batch_id=created_batch.batch_id)
                manifest = run_prescreen(
                    s, batch_id=created_batch.batch_id,
                    etf_min_aum_yi=etf_min_aum_yi,
                    etf_min_turnover_yi=etf_min_turnover_yi,
                    min_market_cap_yi=min_market_cap_yi,
                    min_turnover_yi=min_turnover_yi,
                )
            if missing_enrichment:
                enrichment_warning = f"候选补充快照缺少 tmId：{missing_enrichment}"
        except TrendAnimalsError as exc:
            # 节气/强度是初筛后的展示增强；核心 M1-M4 已完成时不回滚整条选股链。
            enrichment_warning = f"候选补充字段失败：{exc.message}"

        manifest = _persist_component_warnings(
            s, batch_id=created_batch.batch_id, manifest=manifest,
            warnings=component_count_warnings)

        actual = ledger_delta(client, before)
        details = {
            "counts": estimate["counts"], "unique_components": len(tm_ids),
            "basic_counts": basic_counts,
            "component_count_warnings": component_count_warnings,
            "sectors": len(industry_tm_ids),
            "candidates": len(manifest.get("candidates", [])),
            "rejected": len(manifest.get("rejected", [])),
            "estimate_breakdown": estimate["estimate_breakdown"],
            "enrichment_warning": enrichment_warning,
        }
        finish_audit(
            s, audit, status="done", as_of_date=date, tm_count=len(tm_ids),
            requested_fields=CORE_FIELDS + SECTOR_FIELDS + ENRICH_FIELDS,
            estimated_cost=estimate["estimated_cost"], actual_cost=actual,
            details=details,
        )
        return {
            "ok": True, "batch_id": created_batch.batch_id, "as_of_date": date,
            "estimated_cost": estimate["estimated_cost"], "actual_cost": actual,
            "component_counts": estimate["counts"], "unique_components": len(tm_ids),
            "basic_component_counts": basic_counts,
            "component_count_warnings": component_count_warnings,
            "sector_count": len(industry_tm_ids),
            "candidates": manifest.get("candidates", []),
            "rejected": manifest.get("rejected", []),
            "market": manifest.get("market"),
            "enrichment_warning": enrichment_warning,
        }
    except TrendAnimalsError as error:
        s.rollback()
        finish_audit(
            s, audit, status="blocked" if error.code in {
                "data_stale", "confirmation_required", "not_configured",
                "component_count_mismatch",
            } else "failed", error=error,
            details={"created_batch_id": created_batch.batch_id if created_batch else None},
        )
        raise
