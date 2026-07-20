"""Aggregate a batch's OCR rows by category, deduped by code — the trend-desk
equivalent of Hermes `by_market/{cat}_unique.json`. Read-only projection; the
raw OcrRows are untouched.
"""
from sqlmodel import Session, select
from backend.db import OcrJob, OcrRow
from backend.ocr.review import _dedupe_by_code, is_truncated as _is_truncated
from backend.ocr.sectors import resolve_sectors

def _project(r: OcrRow, resolved: dict[int, str | None], category: str) -> dict:
    """投影成聚合展示行。强度按来源页拆两字段(语义不同,不可混):
    A股页 → strength_a_share(A股内排名);A股组合/温转热页 → strength_intraday(页内排名)。
    价格/市值/日成交额/tags 从 raw_fields 提到顶层做列。"""
    rf = r.raw_fields or {}
    is_intraday = "组合" in (category or "")
    sector = resolved.get(r.row_id, r.sector)
    return {
        "row_id": r.row_id,
        "code": r.code,
        "name": r.name,
        "sector": sector,
        "market": r.market,
        "row_type": r.row_type,
        "temperature_status": r.temperature_status,
        "strength_a_share": None if is_intraday else r.strength,
        "strength_intraday": r.strength if is_intraday else None,
        "right_side_days": r.right_side_days,
        "right_side_gain_pct": r.right_side_gain_pct,
        "jieqi": r.jieqi,
        "tags": rf.get("tags") or [],
        "price": rf.get("price"),
        "market_cap_yi": rf.get("market_cap_yi", rf.get("market_cap")),
        "turnover_yi": rf.get("turnover_yi", rf.get("turnover") or rf.get("daily_turnover")),
        "review_status": r.review_status,
        "raw_fields": rf,
    }


def aggregate_by_category(s: Session, batch_id: str) -> dict:
    """Group all rows of a batch by OcrJob.category, dedup by code within each
    category (richest row wins, via review._dedupe_by_code), and report
    before/after counts (mirrors Hermes row_count_before/after_dedup).

    清洗:① 丢弃截断/无身份行(_is_truncated);② 板块缺失时按 code 跨截图回填。"""
    pairs = s.exec(
        select(OcrRow, OcrJob).join(OcrJob, OcrRow.job_id == OcrJob.job_id)
        .where(OcrJob.batch_id == batch_id)
    ).all()

    # 使用 resolve_sectors 做跨截图位置继承（比仅 code 回填更完整）
    resolved = resolve_sectors(pairs)

    by_cat: dict[str, list[OcrRow]] = {}
    dropped: dict[str, int] = {}
    for row, job in pairs:
        cat = job.category or row.market or "未分类"
        if _is_truncated(row):
            dropped[cat] = dropped.get(cat, 0) + 1
            continue
        by_cat.setdefault(cat, []).append(row)

    categories = []
    for cat in sorted(by_cat):
        rows = by_cat[cat]
        deduped = _dedupe_by_code(rows)
        categories.append({
            "category": cat,
            "row_count_before_dedup": len(rows),
            "row_count_after_dedup": len(deduped),
            "dropped_truncated": dropped.get(cat, 0),
            "rows": [_project(r, resolved, cat) for r in deduped],
        })
    return {"batch_id": batch_id, "categories": categories}
