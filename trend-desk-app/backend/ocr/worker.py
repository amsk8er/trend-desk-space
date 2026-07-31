import asyncio
import time
from sqlmodel import Session, select
from backend.db import OcrJob, OcrRow
from backend.ocr.parser import parse_ocr_json, is_bad_image
from backend.llm.base import LLMClient, LLMRequest


def _coerce_int(v):
    """Strength may arrive as int, "84", "84%", or null. Return an int or None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().rstrip("%").strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _coerce_float(v):
    """Gain pct may arrive as 1.6, "1.6", "+1.6%", or null. Return a float or None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("%").strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _strength_of(row: dict):
    """New prompt emits top-level `strength`; old data tucked it in raw_fields.strength."""
    if "strength" in row:
        return _coerce_int(row.get("strength"))
    return _coerce_int((row.get("raw_fields") or {}).get("strength"))


def _derive_category(meta: dict):
    """聚合类别：优先模型显式给的 meta.category；否则取面包屑中段
    （'金融资产 > A股 > 半导体' → 'A股'）；都没有则回退 meta.market。"""
    cat = meta.get("category")
    if cat:
        return cat
    bc = meta.get("breadcrumb")
    if bc:
        parts = [p.strip() for p in bc.split(">") if p.strip()]
        if len(parts) >= 2:
            return parts[1]
    return meta.get("market")

async def _process_one(job_id: int, engine, client: LLMClient, prompt: str, sem: asyncio.Semaphore,
                       timeout_s: int = 120, max_retries: int = 1):
    async with sem:
        with Session(engine) as s:
            job = s.get(OcrJob, job_id)
            if not job or job.status != "todo":
                return
            job.status = "running"; s.add(job); s.commit()
            image = job.image_path; model = job.model
        t0 = time.time()
        # retry on transient errors (timeouts under concurrency are the common case)
        last_err = None
        for attempt in range(max_retries + 1):
            with Session(engine) as s:
                job = s.get(OcrJob, job_id); job.attempts += 1; s.add(job); s.commit()
            try:
                resp = await client.complete(
                    LLMRequest(prompt=prompt, model=model, images=[image], timeout_s=timeout_s))
                last_err = None
                break
            except Exception as e:
                last_err = e
        if last_err is not None:
            with Session(engine) as s:
                job = s.get(OcrJob, job_id)
                job.status = "failed"
                job.partial_reason = (f"exception after {max_retries + 1} tries: "
                                      f"{type(last_err).__name__}: {last_err}")[:500]
                job.elapsed_ms = int((time.time() - t0) * 1000); s.add(job); s.commit()
            return
        payload = parse_ocr_json(resp.text)
        bad, reason = is_bad_image(payload)
        with Session(engine) as s:
            job = s.get(OcrJob, job_id)
            job.raw_json = payload; job.elapsed_ms = resp.elapsed_ms
            job.backend = resp.raw.get("served_by") or getattr(client, "name", None)
            job.category = _derive_category(payload.get("meta") or {})
            if bad:
                job.status = "skip"; job.partial_reason = reason
            else:
                job.status = "done"
                for r in payload["rows"]:
                    s.add(OcrRow(
                        job_id=job.job_id,
                        row_type=r.get("row_type", "instrument"),
                        market=payload["meta"].get("market", "A股"),
                        code=r.get("code"), name=r.get("name"),
                        # 行级 sector；单板块页行内省略时回退到页级 meta.sector_name
                        sector=r.get("sector") or payload["meta"].get("sector_name"),
                        # temperature (0-100 int) is the legacy phantom — no longer populated;
                        # heat lives in temperature_status, the number lives in strength.
                        temperature=None,
                        temperature_status=r.get("temperature_status"),
                        strength=_strength_of(r),
                        right_side_days=r.get("right_side_days"),
                        right_side_gain_pct=_coerce_float(r.get("right_side_gain_pct")),
                        jieqi=r.get("jieqi"),
                        raw_fields=r.get("raw_fields") or {},
                    ))
            s.add(job); s.commit()

async def run_batch(*, engine, batch_id: str, client: LLMClient, prompt: str, concurrency: int = 4,
                    timeout_s: int = 120, max_retries: int = 1) -> None:
    with Session(engine) as s:
        job_ids = [j.job_id for j in s.exec(select(OcrJob).where(OcrJob.batch_id == batch_id, OcrJob.status == "todo"))]
    sem = asyncio.Semaphore(concurrency)
    await asyncio.gather(*[_process_one(jid, engine, client, prompt, sem, timeout_s, max_retries)
                           for jid in job_ids])
