# backend/pipeline/nodes/ocr.py
import shutil
from pathlib import Path
from sqlmodel import Session, select
from backend.db import OcrJob, Batch
from backend.ocr.worker import run_batch
from backend.pipeline.state import transition, NodeStatus
from backend.pipeline.archive_originals import archive_source_images
from backend import config


async def run_ocr_node(*, engine, batch_id: str, data_root: Path, client, prompt: str, concurrency: int = 4,
                       timeout_s: int = 120, max_retries: int = 1, screenshots_archive: Path | None = None):
    with Session(engine) as s:
        transition(s, batch_id=batch_id, node="ocr", to=NodeStatus.RUNNING)
    await run_batch(engine=engine, batch_id=batch_id, client=client, prompt=prompt,
                    concurrency=concurrency, timeout_s=timeout_s, max_retries=max_retries)
    with Session(engine) as s:
        jobs = s.exec(select(OcrJob).where(OcrJob.batch_id == batch_id)).all()
        archive = data_root/"archive"/batch_id; archive.mkdir(parents=True, exist_ok=True)
        failed = data_root/"failed"/batch_id; failed.mkdir(parents=True, exist_ok=True)
        done_names: list[str] = []
        for j in jobs:
            src = Path(j.image_path)
            if not src.exists():
                continue
            if j.status == "done":
                # done_names 取 job 文件名；import_batch 用原名 copy 到内部 inbox，
                # 故与 iCloud source_dir 里的原图同名，archive_source_images 据此回查。
                done_names.append(src.name)
            dest = (archive if j.status == "done" else failed) / src.name
            shutil.move(str(src), str(dest)); j.image_path = str(dest); s.add(j)
        # 成功识别的 iCloud 原图 → iCloud 归档（按批次号子目录）；失败的留 inbox 等重试。
        batch = s.get(Batch, batch_id)
        if batch and batch.source_dir and done_names:
            archive_root = Path(screenshots_archive) if screenshots_archive else config.SCREENSHOTS_ARCHIVE
            archive_source_images(source_dir=Path(batch.source_dir), archive_root=archive_root,
                                  batch_id=batch_id, filenames=done_names)
        s.commit()
        transition(s, batch_id=batch_id, node="ocr", to=NodeStatus.DONE)
