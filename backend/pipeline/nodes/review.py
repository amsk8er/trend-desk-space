# backend/pipeline/nodes/review.py
from sqlmodel import Session, select
from backend.db import OcrJob, OcrRow
from backend.ocr.review import row_anomalies


def review_summary(s: Session, batch_id: str) -> dict:
    """默认通过模型:统计各 review_status 计数 + 需重点审查的异常行数。
    异常 = row_anomalies 命中(截断 / 缺关键字段 / 整图低置信度),含被 reject 的行不计异常。"""
    pairs = s.exec(
        select(OcrRow, OcrJob).join(OcrJob).where(OcrJob.batch_id == batch_id)
    ).all()
    out = {"approved": 0, "pending": 0, "rejected": 0, "anomaly": 0, "total": len(pairs)}
    for r, job in pairs:
        out[r.review_status] = out.get(r.review_status, 0) + 1
        if r.review_status != "rejected" and row_anomalies(r, job):
            out["anomaly"] += 1
    return out


def run_all_can_proceed(s: Session, batch_id: str) -> tuple[bool, str]:
    """默认通过模型:OCR 行默认可用,校对绝不硬阻塞(恒 can_proceed=True)。
    pending = 未审但默认进下游;只对异常行(截断/缺字段/低置信度)给软提示,提醒重点审查。"""
    sm = review_summary(s, batch_id)
    if sm["anomaly"] > 0:
        return True, (f"{sm['anomaly']} 行待重点审查(异常:截断/缺关键字段/低置信度)—— "
                      f"默认全部通过,发现读错再驳回 (rejected={sm['rejected']})")
    return True, f"无异常行,{sm['total']} 行默认通过 (rejected={sm['rejected']})"
