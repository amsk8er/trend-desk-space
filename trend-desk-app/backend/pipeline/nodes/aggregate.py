"""Node ④ 聚合 — dedup OCR rows by code per category into the canonical unique
list (trend-desk's by_market). Persists a Manifest(stage="aggregate") so it's a
real pipeline node with status + provenance; downstream 初筛 consumes it.
"""
from sqlmodel import Session
from backend.db import Manifest
from backend.ocr.aggregate import aggregate_by_category
from backend.pipeline.state import transition, NodeStatus


def run_aggregate(s: Session, *, batch_id: str) -> dict:
    transition(s, batch_id=batch_id, node="aggregate", to=NodeStatus.RUNNING)
    transition(s, batch_id=batch_id, node="review", to=NodeStatus.DONE)
    result = aggregate_by_category(s, batch_id)
    s.add(Manifest(batch_id=batch_id, stage="aggregate", manifest_json=result))
    s.commit()
    transition(s, batch_id=batch_id, node="aggregate", to=NodeStatus.DONE)
    return result
