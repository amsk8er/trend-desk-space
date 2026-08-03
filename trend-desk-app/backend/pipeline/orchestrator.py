"""RUN ALL 编排：一键顺序跑机械计算节点（聚合 → 初筛 → B筛 → 出局 → 日报）。

机械节点 = 纯计算、无人工输入，逐个手点很反人类（实盘反馈）。人工节点
（导入/OCR/校对/持仓/温度页/推送）仍由用户驱动——它们要上传截图、审查或确认。
本编排基于**现有数据**跑机械节点，让用户在数据就绪后一键推进到日报。

校对（review）因 default-pass 不阻塞下游（消费非 rejected 行），故不在机械链里。
某个节点抛错只记入 failed 并继续，不挡住后面的节点（如没持仓时出局检查照样空跑）。
"""
from sqlmodel import Session
from backend.pipeline.nodes.aggregate import run_aggregate
from backend.pipeline.nodes.prescreen import run_prescreen
from backend.pipeline.nodes.b_filter import run_b_filter
from backend.pipeline.nodes.exit_check import run_exit_check
from backend.pipeline.nodes.report import compose_report

MECHANICAL = [
    ("aggregate", run_aggregate),
    ("prescreen", run_prescreen),
    ("b_filter", run_b_filter),
    ("exit_check", run_exit_check),
    ("report", compose_report),
]


def run_auto(s: Session, *, batch_id: str) -> dict:
    """顺序跑机械节点；返回 {ran, failed}。某节点失败不阻断其余。"""
    ran: list[str] = []
    failed: list[dict] = []
    for name, fn in MECHANICAL:
        try:
            fn(s, batch_id=batch_id)
            ran.append(name)
        except Exception as e:  # noqa: BLE001 — 一个节点失败不该挡住其余机械节点
            failed.append({"node": name, "error": str(e)})
    return {"ran": ran, "failed": failed}
