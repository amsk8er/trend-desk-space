from sqlmodel import Session, select
from backend.db import Manifest
from backend.ocr.review import clean_approved_rows
from backend.filters.checks import ManifestCtx
from backend.filters.chain import run_b_phase, run_execution_filter, fails
from backend.pipeline.state import transition, NodeStatus
from backend.pipeline.nodes.prescreen import _to_instrument, _sector_status_map
from backend.risk.sizing import collect_stop_refs, size_position

# B 筛三向分流（纪律 v2）：
#   rejected   — 主筛选 M1-M6 或一致性 C5/C6 不过；
#   watch_list — 纪律合格但不宜立即开仓：X1 日涨幅>10% 不追（次日复核），
#                或止损参考缺失/过远（无法按风险开仓，只能观察仓）；
#   white_list — 全部通过，附按风险反推的仓位建议（sizing）。


def run_b_filter(s: Session, *, batch_id: str,
                 risk_pct: float | None = None,
                 fixed_stop_pct: float | None = None) -> dict:
    transition(s, batch_id=batch_id, node="b_filter", to=NodeStatus.RUNNING)
    all_rows = clean_approved_rows(s, batch_id=batch_id)
    sector_map = _sector_status_map(all_rows)
    rows = [r for r in all_rows if r.row_type == "instrument"]
    # 沿用初筛所用的可调阈值（最新一次 prescreen manifest 持久化的），否则重算 M1-M4
    # 落回 config 默认，会把只因放宽阈值才过初筛的票静默丢掉（小由 2026-06-24）。
    pre = next((m for m in s.exec(
        select(Manifest).where(Manifest.batch_id == batch_id, Manifest.stage == "prescreen")
        .order_by(Manifest.manifest_id.desc())).all()), None)
    th = (pre.manifest_json.get("thresholds") if pre else None) or {}
    instruments = [_to_instrument(r, sector_map,
                                  etf_min_aum_yi=th.get("etf_min_aum_yi"),
                                  etf_min_turnover_yi=th.get("etf_min_turnover_yi"),
                                  min_market_cap_yi=th.get("min_market_cap_yi"),
                                  min_turnover_yi=th.get("min_turnover_yi"))
                   for r in rows]
    rf_by_code = {r.code: (r.raw_fields or {}) for r in rows if r.code}
    entries: dict[str, list] = {}
    for inst in instruments:
        entries.setdefault(inst.code, []).append(inst)
    mctx = ManifestCtx(entries=entries)
    # 初筛口径 = M1-M4；B 筛新增 = M6(节气) + 一致性 C5/C6。（M5 右侧涨幅门已取消，2026-06-19）
    # 「拒绝详情」只列过了初筛、栽在 B 筛新增门的标的：M1-M4 不过的初筛已拒过、
    # 已在初筛报告里列明，不在此重复（小由 2026-06-17 要求）。
    PRESCREEN_CHECKS = {"M1", "M2", "M3", "M4"}
    white, watch, rejected = [], [], []
    for inst in instruments:
        rec = {"code": inst.code, "name": inst.name, "sector": inst.sector,
               "sector_status": inst.sector_status, "status": inst.temperature_status,
               "strength": inst.strength,
               "jieqi": inst.jieqi, "is_etf": inst.is_etf,
               "market_cap_yi": inst.market_cap_yi, "turnover_yi": inst.turnover_yi,
               "right_side_days": inst.right_side_days,
               "right_side_gain_pct": inst.right_side_gain_pct,
               "daily_change_pct": inst.daily_change_pct, "tags": inst.tags}
        failed = fails(run_b_phase(inst, mctx))
        if failed:
            failed_ids = [r.check_id for r in failed]
            if PRESCREEN_CHECKS.intersection(failed_ids):
                continue  # 初筛阶段就被拒，不在 B 筛重复列
            rec["rejected_by"] = failed_ids
            rec["reasons"] = [{"check_id": r.check_id, "reason": r.reason} for r in failed]
            rejected.append(rec)
            continue
        x = run_execution_filter(inst)
        if not x.ok:
            rec["watch_reason"] = x.reason
            watch.append(rec)
            continue
        rf = rf_by_code.get(inst.code, {})
        sizing = size_position(current_price=rf.get("price"),
                               refs=collect_stop_refs(rf),
                               risk_pct=risk_pct, fixed_stop_pct=fixed_stop_pct)
        rec["sizing"] = sizing.as_dict()
        if sizing.verdict == "open":
            white.append(rec)
        else:
            rec["watch_reason"] = sizing.reason
            watch.append(rec)
    manifest_json = {"stage": "b_filter", "white_list": white,
                     "watch_list": watch, "rejected": rejected}
    s.add(Manifest(batch_id=batch_id, stage="b_filter", manifest_json=manifest_json,
                   white_list=white, rejected=rejected))
    s.commit()
    transition(s, batch_id=batch_id, node="b_filter", to=NodeStatus.DONE)
    return manifest_json
