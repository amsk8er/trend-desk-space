from datetime import datetime
from sqlmodel import Session, select
from backend.db import OcrJob, OcrRow

def approve(s: Session, row_ids: list[int], reason: str = "") -> None:
    for rid in row_ids:
        r = s.get(OcrRow, rid)
        if r:
            r.review_status = "approved"; r.review_reason = reason
            r.reviewed_at = datetime.utcnow(); s.add(r)
    s.commit()

def reject(s: Session, row_ids: list[int], reason: str = "") -> None:
    for rid in row_ids:
        r = s.get(OcrRow, rid)
        if r:
            r.review_status = "rejected"; r.review_reason = reason
            r.reviewed_at = datetime.utcnow(); s.add(r)
    s.commit()

def is_truncated(r: OcrRow) -> bool:
    """顶部滚动截断 / 缺关键字段的不完整行 —— 聚合与初筛都剔除,不计入唯一清单。
    判据:名称是「截断行」标记 / raw_fields.note 提到「截断」/ 既无 code 又无 name。"""
    name = (r.name or "").strip()
    if "截断" in name:
        return True
    if "截断" in ((r.raw_fields or {}).get("note") or ""):
        return True
    if not r.code and not name:
        return True
    return False


# 低置信度阈值:job.raw_json.meta.confidence 低于此值的整张图,其所有行都标异常。
# 与 parser.is_bad_image 的硬拒阈值(0.3,直接 skip 不入库)区分——这里是「入库了但要重点看」的软线。
ANOMALY_CONFIDENCE_FLOOR = 0.6


def _job_confidence(job: OcrJob | None) -> float | None:
    """OCR 整图置信度。识图模型在 raw_json.meta.confidence 里报(0-1),缺省 None。"""
    if job is None:
        return None
    meta = (job.raw_json or {}).get("meta") or {}
    conf = meta.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        return float(conf)
    return None


def row_anomalies(r: OcrRow, job: OcrJob | None = None) -> list[str]:
    """默认通过模型下的「异常审查」判据:返回这一行需要人工重点看的理由清单(空=干净)。
    个股行(instrument)缺关键字段 / 截断 / 整图低置信度 都算异常;板块/大盘行只查截断与置信度
    (它们本就没有市值·成交额·右侧天数这些个股字段)。"""
    reasons: list[str] = []
    if is_truncated(r):
        reasons.append("截断/不完整行")
    conf = _job_confidence(job)
    if conf is not None and conf < ANOMALY_CONFIDENCE_FLOOR:
        reasons.append(f"OCR 置信度低({conf})")
    if r.row_type == "instrument":
        rf = r.raw_fields or {}
        if rf.get("market_cap_yi") is None:
            reasons.append("缺市值")
        if rf.get("turnover_yi") is None:
            reasons.append("缺成交额")
        if r.right_side_days is None:
            reasons.append("缺右侧天数")
    return reasons


def _dedupe_by_code(rows: list[OcrRow]) -> list[OcrRow]:
    """同一标的(code)在多张截图重复出现时,只保留信息最全的一条。
    规则:优先留 strength 非空的;并列时留 raw_fields 更丰富的。
    code 为空的行(大盘/板块)不参与去重,原样保留。"""
    best: dict[str, OcrRow] = {}
    passthrough: list[OcrRow] = []
    for r in rows:
        if not r.code:
            passthrough.append(r); continue
        cur = best.get(r.code)
        if cur is None:
            best[r.code] = r; continue
        # prefer non-null strength, then richer raw_fields
        cur_score = (cur.strength is not None, len(cur.raw_fields or {}))
        new_score = (r.strength is not None, len(r.raw_fields or {}))
        if new_score > cur_score:
            best[r.code] = r
    return passthrough + list(best.values())


def clean_approved_rows(s: Session, batch_id: str) -> list[OcrRow]:
    """默认通过模型:下游节点(初筛/B 筛/持仓提醒)消费「非 rejected」行 ——
    即 approved + pending 都可用,pending = 未审但默认可用。只有 reject 才排除。
    sector / overview 行只要不被 reject 就保留,M1 板块温度回查据此生效。
    截断行(is_truncated)仍剔除;同 code 重复标的按信息完整度去重(原始 OcrRow 全保留)。"""
    stmt = select(OcrRow).join(OcrJob).where(
        OcrJob.batch_id == batch_id, OcrRow.review_status != "rejected",
    )
    rows = [r for r in s.exec(stmt) if not is_truncated(r)]
    return _dedupe_by_code(rows)

def pending_count(s: Session, batch_id: str) -> int:
    stmt = select(OcrRow).join(OcrJob).where(
        OcrJob.batch_id == batch_id, OcrRow.review_status == "pending",
    )
    return len(list(s.exec(stmt)))
