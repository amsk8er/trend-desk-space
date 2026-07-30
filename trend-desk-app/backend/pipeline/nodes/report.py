# backend/pipeline/nodes/report.py
from sqlmodel import Session, select
from backend import config
from backend.db import Batch, Manifest, ExitListItem, Position, TrendBrief
from backend.llm import get_client
from backend.pipeline.state import transition, NodeStatus
from backend.analysis.trend_facts import build_trend_facts
from backend.analysis.trend_brief import facts_hash, generate_brief


def compose_report(s: Session, *, batch_id: str, brief_md: str | None = None) -> dict:
    transition(s, batch_id=batch_id, node="report", to=NodeStatus.RUNNING)
    b = s.get(Batch, batch_id)
    # latest-wins：节点重跑会 append 新 Manifest，取最新那条（manifest_id 自增，
    # 比 created_at 更稳，避免同秒并列）。对齐 read.py::read_b_filter 的取最新语义。
    prescreen = next((m for m in s.exec(select(Manifest).where(Manifest.batch_id == batch_id, Manifest.stage == "prescreen").order_by(Manifest.manifest_id.desc())).all()), None)
    bfilter = next((m for m in s.exec(select(Manifest).where(Manifest.batch_id == batch_id, Manifest.stage == "b_filter").order_by(Manifest.manifest_id.desc())).all()), None)
    exits = s.exec(select(ExitListItem).where(ExitListItem.batch_id == batch_id)).all()
    positions = s.exec(select(Position).where(Position.batch_id == batch_id)).all()
    lines = [f"# A 股日报 · {b.date}", ""]
    if brief_md:
        lines += ["## 趋势研判（AI）", "", brief_md, ""]
    lines.append("## 1. 市场全景")
    lines.append(f"- 持仓 {len(positions)} 只")
    lines.append("")
    if prescreen:
        cands = prescreen.manifest_json.get("candidates", [])
        lines.append(f"## 2. 今日新增温转热（{len(cands)}）")
        lines.append("| 标的 | 温度 | 强度 |")
        lines.append("|---|---|---|")
        for c in cands:
            strength = c.get("strength")
            lines.append(f"| {c.get('name')} | {c.get('status') or '—'} | "
                         f"{strength if strength is not None else '—'} |")
        lines.append("")
    lines.append(f"## 3. 出局警告（{len(exits)}）")
    for e in exits:
        lines.append(f"- {e.reason} → 建议 {e.action}")
    lines.append("")
    if bfilter:
        wl = bfilter.white_list or []
        watch = bfilter.manifest_json.get("watch_list", [])
        rj = bfilter.rejected or []
        lines.append("## 4. 执行卡过滤结论")
        lines.append(f"### 白名单（{len(wl)}）")
        for w in wl:
            sz = w.get("sizing") or {}
            if sz.get("position_amount"):
                lines.append(
                    f"- {w.get('name')} ✓ 建议仓位 {sz['position_amount']:.0f}元"
                    f"({sz['position_ratio']:.0%}) · 止损 {sz.get('stop_price')}"
                    f"({sz.get('stop_label')}) · 距离{sz.get('distance_pct') or 0:.1%}"
                    f" · 单笔最大亏 {sz.get('max_loss') or 0:.0f}元")
            else:
                lines.append(f"- {w.get('name')} ✓")
        if watch:
            lines.append(f"### 观察池（{len(watch)}）")
            for w in watch:
                lines.append(f"- {w.get('name')} ⏸ {w.get('watch_reason', '')}")
        lines.append(f"### 拒绝（{len(rj)}）")
        for r in rj:
            reasons = "; ".join(rr.get("reason", "") for rr in r.get("reasons", []))
            lines.append(f"- {r.get('name')} ✗ {reasons}")
    md = "\n".join(lines)
    payload = {"markdown": md, "batch_id": batch_id, "date": b.date}
    transition(s, batch_id=batch_id, node="report", to=NodeStatus.DONE)
    return payload


def _latest_brief(s: Session, batch_id: str, backend: str | None = None) -> TrendBrief | None:
    """取本批最新研判。backend 给定则只匹配该后端的缓存（同事实不同后端 → 各自缓存独立，
    切换后端时旧 brief 不会错误复用）。None → 任意后端（兼容未指定 backend 的调用方）。"""
    q = select(TrendBrief).where(TrendBrief.batch_id == batch_id)
    if backend:
        q = q.where(TrendBrief.backend == backend)
    return next((tb for tb in s.exec(
        q.order_by(TrendBrief.id.desc())).all()), None)


async def compose_report_with_brief(engine, *, batch_id: str, client_factory=get_client,
                                 backend: str | None = None) -> dict:
    """日报 orchestrator：机械段（compose_report）+ 顶部 LLM 趋势研判（自动+缓存）。

    缓存：事实包不变则复用已存研判，不调 LLM。缓存按 (batch, backend, facts_hash) 隔离：
    切换后端（如 claude_cli ↔ codex_cli）时旧 brief 不复用、强制重生成。
    降级：LLM 不可用/抛错 → 机械日报照常出，附「未生成」提示，节点永不 fail。
    不在 LLM await 期间持有 DB 连接（对齐 api_push）。
    """
    if not config.TREND_BRIEF_ENABLED:
        with Session(engine) as s:
            return compose_report(s, batch_id=batch_id)

    with Session(engine) as s:
        facts = build_trend_facts(s, batch_id=batch_id, lookback=config.TREND_BRIEF_LOOKBACK)
        h = facts_hash(facts)
        cached = _latest_brief(s, batch_id, backend=backend)
        if cached is not None and cached.facts_hash == h:
            return compose_report(s, batch_id=batch_id, brief_md=cached.markdown)

    # cache miss：session 已关闭，再调 LLM（可能耗时数秒）
    try:
        brief_md = await generate_brief(facts, client=client_factory(),
                                        model=config.TREND_BRIEF_MODEL)
        with Session(engine) as s:
            s.add(TrendBrief(batch_id=batch_id, facts_hash=h,
                             markdown=brief_md, model=config.TREND_BRIEF_MODEL,
                             backend=backend))
            s.commit()
            return compose_report(s, batch_id=batch_id, brief_md=brief_md)
    except Exception:   # noqa: BLE001 — 降级优先，LLM 失败绝不拖垮日报
        with Session(engine) as s:
            return compose_report(s, batch_id=batch_id, brief_md="_（趋势研判本次未生成）_")
