# backend/pipeline/nodes/import_.py
import shutil
from datetime import datetime
from pathlib import Path
from sqlmodel import Session
from backend.db import Batch, OcrJob
from backend.pipeline.state import init_pipeline_state

class BatchLockError(RuntimeError):
    pass

def import_batch(s: Session, *, source_dir: Path, data_root: Path, date: str) -> str:
    inbox = data_root/"inbox"; inbox.mkdir(parents=True, exist_ok=True)
    lock = inbox/".batch.lock"
    if lock.exists():
        raise BatchLockError(f"batch in progress: {lock.read_text()}")
    batch_id = f"batch_{date.replace('-', '')}_{datetime.now().strftime('%H%M')}"
    lock.write_text(batch_id)
    try:
        dest = inbox/batch_id; dest.mkdir(parents=True)
        pngs = sorted(p for p in source_dir.glob("*.png"))
        s.add(Batch(batch_id=batch_id, date=date, status="running",
                    source_dir=str(source_dir))); s.commit()
        init_pipeline_state(s, batch_id=batch_id)
        for i, p in enumerate(pngs):
            target = dest/p.name; shutil.copy2(p, target)
            s.add(OcrJob(batch_id=batch_id, image_path=str(target), image_index=i, status="todo"))
        s.commit()
        from backend.pipeline.state import transition, NodeStatus
        transition(s, batch_id=batch_id, node="import", to=NodeStatus.DONE)
        return batch_id
    finally:
        if lock.exists():
            lock.unlink()
