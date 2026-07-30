# backend/pipeline/state.py
from enum import StrEnum
from sqlmodel import Session
from backend.db import Batch

# 顺序 = 用户面流程：筛选链(初筛→B筛)连续，再持仓链(持仓→温度页→出局)。
NODES = ["import", "ocr", "review", "aggregate", "prescreen", "b_filter",
         "positions", "holding_temp", "exit_check", "report", "push"]

class NodeStatus(StrEnum):
    TODO = "todo"; RUNNING = "running"; DONE = "done"; FAILED = "failed"; SKIPPED = "skipped"

def init_pipeline_state(s: Session, batch_id: str) -> None:
    b = s.get(Batch, batch_id)
    b.pipeline_state = {n: NodeStatus.TODO for n in NODES}
    s.add(b); s.commit()

def get_state(s: Session, batch_id: str) -> dict[str, str]:
    b = s.get(Batch, batch_id)
    return dict(b.pipeline_state or {}) if b else {}

def transition(s: Session, *, batch_id: str, node: str, to: NodeStatus) -> None:
    assert node in NODES, f"unknown node {node}"
    b = s.get(Batch, batch_id)
    state = dict(b.pipeline_state or {})
    state[node] = to.value
    b.pipeline_state = state
    s.add(b); s.commit()
