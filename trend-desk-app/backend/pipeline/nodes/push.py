# backend/pipeline/nodes/push.py
from pathlib import Path
from backend.notify.feishu import push_report


async def push_node(*, batch_id: str, report: dict, data_root: Path) -> str:
    local = data_root / "reports" / batch_id
    local.mkdir(parents=True, exist_ok=True)
    (local / "report.md").write_text(report["markdown"], encoding="utf-8")
    url = await push_report(title=f"A股日报-{report['date']}", markdown=report["markdown"])
    return url
