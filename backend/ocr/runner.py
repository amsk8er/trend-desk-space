"""OCR rerun service: reset chosen jobs to `todo` and kick off a background run.

Used by both the HTTP `/run/ocr` endpoint and the chatbox `rerun_ocr` tool, so
"重跑" means the same thing everywhere: reset → background execute → watch via SSE.
"""
import asyncio
import logging
import os
from sqlmodel import Session, select
from backend import config
from backend.db import OcrJob, OcrRow
from backend.engine import engine
from backend.llm import get_client
from backend.llm.fallback import FallbackClient
from backend.pipeline.nodes.ocr import run_ocr_node
from backend.pipeline.state import get_state, transition, NodeStatus

log = logging.getLogger(__name__)

_PROMPT = config.ROOT / "prompts" / "ocr_a_market.md"

# in-flight background OCR tasks, keyed by batch_id, so a run can be cancelled.
_RUNNING: dict[str, asyncio.Task] = {}


def reset_jobs_for_rerun(s: Session, batch_id: str, indices: list[int] | None) -> list[int]:
    """Reset target OcrJobs to `todo` so the next run picks them up.

    indices given  → those image_index jobs (explicit user choice, even if done).
    indices None   → all not-yet-successful jobs (status in skip/failed).

    Resetting a `done` job MUST delete its OcrRows first, else _process_one
    (which only appends) would duplicate rows on the rerun. skip/failed have none.
    """
    q = select(OcrJob).where(OcrJob.batch_id == batch_id)
    if indices is not None:
        q = q.where(OcrJob.image_index.in_(indices))
    else:
        q = q.where(OcrJob.status.in_(("skip", "failed")))
    targets = s.exec(q).all()

    reset_ids: list[int] = []
    for j in targets:
        for r in s.exec(select(OcrRow).where(OcrRow.job_id == j.job_id)).all():
            s.delete(r)
        j.status = "todo"
        j.partial_reason = None
        s.add(j)
        reset_ids.append(j.job_id)
    s.commit()
    return reset_ids


def reset_running_jobs(s: Session, batch_id: str) -> list[int]:
    """Unstick jobs wedged in `running` → `todo` and unlock the ocr node.

    Used by force-cancel and as recovery after a server restart left stale
    `running` jobs (the in-flight task is gone, but the DB still says running →
    the schedule guard would refuse new runs). Returns the reset job ids.
    """
    stuck = s.exec(select(OcrJob).where(
        OcrJob.batch_id == batch_id, OcrJob.status == "running")).all()
    ids: list[int] = []
    for j in stuck:
        j.status = "todo"
        j.partial_reason = None
        s.add(j)
        ids.append(j.job_id)
    if get_state(s, batch_id).get("ocr") == "running":
        transition(s, batch_id=batch_id, node="ocr", to=NodeStatus.TODO)
    s.commit()
    return ids


def cancel_ocr_run(batch_id: str) -> dict:
    """Force-stop a (possibly stuck) OCR run: cancel the background task, then
    unstick running jobs + unlock the node so the UI can re-trigger. Works even
    with no live task (post-restart cleanup). MUST run on the event loop thread.
    """
    task = _RUNNING.pop(batch_id, None)
    cancelled = bool(task and not task.done())
    if cancelled:
        task.cancel()
    with Session(engine) as s:
        ids = reset_running_jobs(s, batch_id)
    return {"cancelled": cancelled, "reset": len(ids)}


def _count_todo(s: Session, batch_id: str) -> int:
    return len(s.exec(select(OcrJob).where(
        OcrJob.batch_id == batch_id, OcrJob.status == "todo")).all())


def _log_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        log.info("background OCR run cancelled")
    except Exception:  # noqa: BLE001
        log.exception("background OCR run failed")


def _build_client(backend: str | None):
    """Resolve the client for this run; wrap with fallback only if configured.

    OCR_FALLBACK_BACKEND is read at call time (NOT via config.py) because
    config module values freeze before app lifespan loads secrets.env.
    """
    client = get_client(backend)
    fb = os.getenv("OCR_FALLBACK_BACKEND", "").strip()
    if fb and fb != getattr(client, "name", None):
        return FallbackClient(client, get_client(fb))
    return client


def schedule_ocr_run(batch_id: str, indices: list[int] | None = None,
                     backend: str | None = None) -> int:
    """Reset targets, then schedule run_ocr_node as a background task. Returns the
    number of jobs queued (todo). Returns 0 without scheduling if OCR is already
    running (guard against stacking runs whose archival passes would race) or if
    there is nothing to do. MUST be called from within a running event loop.
    """
    with Session(engine) as s:
        if get_state(s, batch_id).get("ocr") == "running":
            return 0
        reset_jobs_for_rerun(s, batch_id, indices)
        queued = _count_todo(s, batch_id)
    if queued == 0:
        return 0
    prompt = _PROMPT.read_text(encoding="utf-8")
    task = asyncio.get_running_loop().create_task(
        run_ocr_node(engine=engine, batch_id=batch_id, data_root=config.DATA,
                     client=_build_client(backend), prompt=prompt, concurrency=4))
    _RUNNING[batch_id] = task

    def _done(t: asyncio.Task) -> None:
        if _RUNNING.get(batch_id) is t:
            _RUNNING.pop(batch_id, None)
        _log_task_result(t)

    task.add_done_callback(_done)
    return queued
