"""美股手工执行台的持久化边界。

这里不包含行情、规则或成交判断；它只保存不可变证据和可追溯状态。这样 A 股
纪律台账不会被新功能的 Decimal/散股语义影响。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from backend.db import (
    UsAllocationPreview,
    UsAllocationPreviewItem,
    UsAccountOcrBatch,
    UsAccountOcrPosition,
    UsCandidateSnapshot,
    UsDailyRun,
    UsEtfBenchmarkEvidence,
    UsEtfBenchmarkReview,
    UsEtfIdentity,
    UsExitDecision,
    UsHoldingSignalSnapshot,
    UsManualAccountSnapshot,
    UsManualExecution,
    UsManualPlan,
    UsManualPlanItem,
    UsPositionLot,
    UsRiskAnchor,
    UsMarketEnvironmentSnapshot,
    UsSizingPreviewSnapshot,
    UsStopReview,
    UsStopSuggestion,
    UsUniverseArchive,
    UsWindDataCache,
    UsWindRequestAudit,
)
from backend.us_manual.contracts import (
    US_MANUAL_H2_SCOPE,
    US_MANUAL_H3_SCOPE,
    US_MANUAL_H4_SCOPE,
    US_MANUAL_H5_SCOPE,
    US_MANUAL_LEGACY_SCOPE,
    US_MANUAL_SCOPE,
    UsManualError,
    serialize,
)


US_MANUAL_COST_SCOPES = (
    US_MANUAL_SCOPE,
    US_MANUAL_H5_SCOPE,
    US_MANUAL_H4_SCOPE,
    US_MANUAL_H3_SCOPE,
    US_MANUAL_H2_SCOPE,
    US_MANUAL_LEGACY_SCOPE,
)


def latest_universe_archive(session: Session) -> UsUniverseArchive | None:
    return session.exec(
        select(UsUniverseArchive).order_by(UsUniverseArchive.created_at.desc()).limit(1)
    ).first()


def get_universe_archive(session: Session, archive_id: str) -> UsUniverseArchive:
    row = session.get(UsUniverseArchive, archive_id)
    if row is None:
        raise UsManualError("universe_archive_missing", "未找到美股覆盖归档", 404)
    return row


def save_universe_archive(session: Session, row: UsUniverseArchive) -> UsUniverseArchive:
    existing = session.get(UsUniverseArchive, row.archive_id)
    if existing is not None:
        return existing
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_run(session: Session, run_id: str) -> UsDailyRun:
    row = session.get(UsDailyRun, run_id)
    if row is None:
        raise UsManualError("run_not_found", "未找到美股扫描记录", 404)
    return row


def cached_run(session: Session, *, as_of_date: str, scope: str,
               base_fields_hash: str) -> UsDailyRun | None:
    return session.exec(select(UsDailyRun).where(
        UsDailyRun.as_of_date == as_of_date,
        UsDailyRun.scope == scope,
        UsDailyRun.base_fields_hash == base_fields_hash,
    )).first()


def latest_run(session: Session, *, as_of_date: str | None = None,
               scope: str = US_MANUAL_SCOPE) -> UsDailyRun | None:
    statement = select(UsDailyRun).where(UsDailyRun.scope == scope)
    if as_of_date:
        statement = statement.where(UsDailyRun.as_of_date == as_of_date)
    return session.exec(statement.order_by(
        UsDailyRun.created_at.desc(), UsDailyRun.as_of_date.desc(),
    ).limit(1)).first()


def estimated_cost_for_date(session: Session, *, as_of_date: str,
                            exclude_run_id: str | None = None) -> Decimal:
    statement = select(UsDailyRun).where(
        UsDailyRun.as_of_date == as_of_date,
        UsDailyRun.scope.in_(US_MANUAL_COST_SCOPES),
    )
    if exclude_run_id:
        statement = statement.where(UsDailyRun.run_id != exclude_run_id)
    rows = session.exec(statement).all()
    return sum((row.estimated_total_cost_cny or Decimal("0") for row in rows), Decimal("0"))


def cost_totals_for_date(session: Session, *, as_of_date: str) -> dict[str, Decimal | None]:
    """Return same-data-day Trend Animals totals across H1-H6 without hiding an unknown bill."""
    rows = session.exec(select(UsDailyRun).where(
        UsDailyRun.as_of_date == as_of_date,
        UsDailyRun.scope.in_(US_MANUAL_COST_SCOPES),
    )).all()
    estimated = sum(
        (row.estimated_total_cost_cny or Decimal("0") for row in rows),
        Decimal("0"),
    )
    billable = [row for row in rows if (row.estimated_total_cost_cny or Decimal("0")) > 0]
    actual = (
        sum((row.actual_total_cost_cny or Decimal("0") for row in billable), Decimal("0"))
        if all(row.actual_total_cost_cny is not None for row in billable)
        else None
    )
    return {"estimated_total_cny": estimated, "actual_total_cny": actual}


def acquire_run_lease(session: Session, *, run_id: str, owner: str,
                      now: datetime, expires_at: datetime) -> bool:
    """Atomically acquire or renew a short run lease."""
    result = session.exec(
        update(UsDailyRun).where(
            UsDailyRun.run_id == run_id,
            or_(
                UsDailyRun.lease_owner.is_(None),
                UsDailyRun.lease_owner == owner,
                UsDailyRun.lease_expires_at.is_(None),
                UsDailyRun.lease_expires_at <= now,
            ),
        ).values(lease_owner=owner, lease_expires_at=expires_at)
    )
    session.commit()
    return bool(result.rowcount)


def release_run_lease(session: Session, *, run_id: str, owner: str) -> None:
    session.exec(
        update(UsDailyRun).where(
            UsDailyRun.run_id == run_id,
            UsDailyRun.lease_owner == owner,
        ).values(lease_owner=None, lease_expires_at=None)
    )
    session.commit()


def save_run(session: Session, row: UsDailyRun) -> UsDailyRun:
    """保存一个 run；并发预检撞上唯一缓存键时返回赢者。"""
    try:
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    except IntegrityError:
        session.rollback()
        winner = cached_run(
            session, as_of_date=row.as_of_date, scope=row.scope,
            base_fields_hash=row.base_fields_hash,
        )
        if winner is None:
            raise
        return winner


def update_run(session: Session, run: UsDailyRun, **fields: Any) -> UsDailyRun:
    for key, value in fields.items():
        setattr(run, key, value)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def replace_candidates(session: Session, *, run: UsDailyRun,
                       candidates: Iterable[UsCandidateSnapshot]) -> list[UsCandidateSnapshot]:
    """基础扫描只在还未成功的 run 中写候选，防止成功证据被重跑覆盖。"""
    if run.status in {"ready", "ready_degraded", "ready_cached"}:
        return list_candidates(session, run.run_id)
    old = session.exec(select(UsCandidateSnapshot).where(UsCandidateSnapshot.run_id == run.run_id)).all()
    for row in old:
        session.delete(row)
    rows = list(candidates)
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


def list_candidates(session: Session, run_id: str) -> list[UsCandidateSnapshot]:
    rows = list(session.exec(select(UsCandidateSnapshot).where(
        UsCandidateSnapshot.run_id == run_id,
    )).all())
    return sorted(rows, key=lambda row: (
        0 if row.asset_type in {"stock", "etf"} and row.screen_status == "ready" else 1,
        row.observation_rank if row.observation_rank is not None else 1_000_000,
        row.rank if row.rank is not None else 1_000_000,
        row.ticker_symbol,
        row.tm_id,
    ))


def get_candidate(session: Session, candidate_id: int) -> UsCandidateSnapshot:
    row = session.get(UsCandidateSnapshot, candidate_id)
    if row is None:
        raise UsManualError("candidate_not_found", "未找到美股候选", 404)
    return row


def save_candidates(session: Session, rows: Iterable[UsCandidateSnapshot]) -> list[UsCandidateSnapshot]:
    saved = list(rows)
    session.add_all(saved)
    session.commit()
    for row in saved:
        session.refresh(row)
    return saved


def environment_for_run(session: Session, run_id: str) -> UsMarketEnvironmentSnapshot | None:
    return session.exec(select(UsMarketEnvironmentSnapshot).where(
        UsMarketEnvironmentSnapshot.run_id == run_id,
    ).limit(1)).first()


def get_environment(session: Session, environment_id: str) -> UsMarketEnvironmentSnapshot:
    row = session.get(UsMarketEnvironmentSnapshot, environment_id)
    if row is None:
        raise UsManualError("market_environment_missing", "未找到美股整体温度环境快照", 404)
    return row


def save_environment(session: Session, row: UsMarketEnvironmentSnapshot) -> UsMarketEnvironmentSnapshot:
    existing = environment_for_run(session, row.run_id)
    if existing is not None:
        return existing
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def latest_etf_identity(session: Session, ticker_symbol: str) -> UsEtfIdentity | None:
    return session.exec(select(UsEtfIdentity).where(
        UsEtfIdentity.ticker_symbol == ticker_symbol.upper(),
        UsEtfIdentity.is_active.is_(True),
    ).order_by(UsEtfIdentity.version.desc(), UsEtfIdentity.retrieved_at.desc()).limit(1)).first()


def get_etf_identity(session: Session, identity_id: str) -> UsEtfIdentity:
    row = session.get(UsEtfIdentity, identity_id)
    if row is None:
        raise UsManualError("etf_identity_missing", "未找到美国 ETF 的 SEC 身份证据", 404)
    return row


def save_etf_identity(session: Session, row: UsEtfIdentity) -> UsEtfIdentity:
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def latest_etf_benchmark(session: Session, ticker_symbol: str) -> UsEtfBenchmarkEvidence | None:
    return session.exec(select(UsEtfBenchmarkEvidence).where(
        UsEtfBenchmarkEvidence.ticker_symbol == ticker_symbol.upper(),
        UsEtfBenchmarkEvidence.is_active.is_(True),
    ).order_by(
        UsEtfBenchmarkEvidence.version.desc(), UsEtfBenchmarkEvidence.retrieved_at.desc(),
    ).limit(1)).first()


def get_etf_benchmark(session: Session, evidence_id: str) -> UsEtfBenchmarkEvidence:
    row = session.get(UsEtfBenchmarkEvidence, evidence_id)
    if row is None:
        raise UsManualError("etf_benchmark_missing", "未找到美国 ETF 基准证据", 404)
    return row


def save_etf_benchmark(session: Session, row: UsEtfBenchmarkEvidence) -> UsEtfBenchmarkEvidence:
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def list_etf_benchmarks(session: Session) -> list[UsEtfBenchmarkEvidence]:
    return list(session.exec(select(UsEtfBenchmarkEvidence).order_by(
        UsEtfBenchmarkEvidence.ticker_symbol, UsEtfBenchmarkEvidence.version.desc(),
    )).all())


def etf_review_by_idempotency(session: Session, key: str) -> UsEtfBenchmarkReview | None:
    return session.exec(select(UsEtfBenchmarkReview).where(
        UsEtfBenchmarkReview.idempotency_key == key,
    ).limit(1)).first()


def save_etf_review(session: Session, row: UsEtfBenchmarkReview) -> UsEtfBenchmarkReview:
    replay = etf_review_by_idempotency(session, row.idempotency_key)
    if replay is not None:
        return replay
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def latest_risk_anchor(session: Session, candidate_id: int) -> UsRiskAnchor | None:
    return session.exec(select(UsRiskAnchor).where(
        UsRiskAnchor.candidate_id == candidate_id,
    ).order_by(UsRiskAnchor.created_at.desc(), UsRiskAnchor.anchor_id.desc()).limit(1)).first()


def risk_anchor_by_input(session: Session, *, candidate_id: int, input_hash: str) -> UsRiskAnchor | None:
    return session.exec(select(UsRiskAnchor).where(
        UsRiskAnchor.candidate_id == candidate_id,
        UsRiskAnchor.input_hash == input_hash,
    ).limit(1)).first()


def get_risk_anchor(session: Session, anchor_id: str) -> UsRiskAnchor:
    row = session.get(UsRiskAnchor, anchor_id)
    if row is None:
        raise UsManualError("risk_anchor_missing", "未找到前期重要低点风险锚点", 404)
    return row


def save_risk_anchor(session: Session, row: UsRiskAnchor) -> UsRiskAnchor:
    existing = risk_anchor_by_input(session, candidate_id=row.candidate_id, input_hash=row.input_hash)
    if existing is not None:
        return existing
    try:
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    except IntegrityError:
        session.rollback()
        existing = risk_anchor_by_input(session, candidate_id=row.candidate_id, input_hash=row.input_hash)
        if existing is None:
            raise
        return existing


def allocation_by_input(session: Session, input_hash: str) -> UsAllocationPreview | None:
    return session.exec(select(UsAllocationPreview).where(
        UsAllocationPreview.input_hash == input_hash,
    ).limit(1)).first()


def get_allocation_preview(session: Session, preview_id: str) -> UsAllocationPreview:
    row = session.get(UsAllocationPreview, preview_id)
    if row is None:
        raise UsManualError("allocation_preview_missing", "未找到不可变推荐分配预览", 404)
    return row


def allocation_items(session: Session, preview_id: str) -> list[UsAllocationPreviewItem]:
    return list(session.exec(select(UsAllocationPreviewItem).where(
        UsAllocationPreviewItem.allocation_preview_id == preview_id,
    ).order_by(UsAllocationPreviewItem.priority, UsAllocationPreviewItem.allocation_item_id)).all())


def get_allocation_item(session: Session, item_id: int) -> UsAllocationPreviewItem:
    row = session.get(UsAllocationPreviewItem, item_id)
    if row is None:
        raise UsManualError("allocation_item_missing", "未找到推荐分配项", 404)
    return row


def save_allocation_preview(
    session: Session,
    preview: UsAllocationPreview,
    items: Iterable[UsAllocationPreviewItem],
) -> tuple[UsAllocationPreview, list[UsAllocationPreviewItem]]:
    existing = allocation_by_input(session, preview.input_hash)
    if existing is not None:
        return existing, allocation_items(session, existing.allocation_preview_id)
    materialized = list(items)
    try:
        session.add(preview)
        session.add_all(materialized)
        session.commit()
        session.refresh(preview)
        for row in materialized:
            session.refresh(row)
        return preview, materialized
    except IntegrityError:
        session.rollback()
        existing = allocation_by_input(session, preview.input_hash)
        if existing is None:
            raise
        return existing, allocation_items(session, existing.allocation_preview_id)


def holding_snapshot_by_contract(
    session: Session, *, as_of_date: str, lot_id: int, contract_hash: str,
) -> UsHoldingSignalSnapshot | None:
    return session.exec(select(UsHoldingSignalSnapshot).where(
        UsHoldingSignalSnapshot.as_of_date == as_of_date,
        UsHoldingSignalSnapshot.lot_id == lot_id,
        UsHoldingSignalSnapshot.contract_hash == contract_hash,
    ).limit(1)).first()


def save_holding_snapshot(session: Session, row: UsHoldingSignalSnapshot) -> UsHoldingSignalSnapshot:
    existing = holding_snapshot_by_contract(
        session, as_of_date=row.as_of_date, lot_id=row.lot_id, contract_hash=row.contract_hash,
    )
    if existing is not None:
        return existing
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def exit_decision_by_signal(
    session: Session, *, lot_id: int, as_of_date: str, signal_hash: str,
) -> UsExitDecision | None:
    return session.exec(select(UsExitDecision).where(
        UsExitDecision.lot_id == lot_id,
        UsExitDecision.as_of_date == as_of_date,
        UsExitDecision.signal_hash == signal_hash,
    ).limit(1)).first()


def exit_decision_for_snapshot(session: Session, snapshot_id: str) -> UsExitDecision | None:
    return session.exec(select(UsExitDecision).where(
        UsExitDecision.snapshot_id == snapshot_id,
    ).limit(1)).first()


def get_exit_decision(session: Session, decision_id: str) -> UsExitDecision:
    row = session.get(UsExitDecision, decision_id)
    if row is None:
        raise UsManualError("exit_decision_missing", "未找到美股趋势退出决定", 404)
    return row


def save_exit_decision(session: Session, row: UsExitDecision) -> UsExitDecision:
    existing = exit_decision_for_snapshot(session, row.snapshot_id)
    if existing is not None:
        return existing
    existing = exit_decision_by_signal(
        session, lot_id=row.lot_id, as_of_date=row.as_of_date, signal_hash=row.signal_hash,
    )
    if existing is not None:
        return existing
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = exit_decision_for_snapshot(session, row.snapshot_id)
        if existing is None:
            existing = exit_decision_by_signal(
                session, lot_id=row.lot_id, as_of_date=row.as_of_date, signal_hash=row.signal_hash,
            )
        if existing is not None:
            return existing
        raise
    session.refresh(row)
    return row


def latest_exit_decisions(session: Session, *, as_of_date: str | None = None) -> list[UsExitDecision]:
    statement = select(UsExitDecision)
    if as_of_date:
        statement = statement.where(UsExitDecision.as_of_date == as_of_date)
    return list(session.exec(statement.order_by(
        UsExitDecision.as_of_date.desc(), UsExitDecision.priority, UsExitDecision.lot_id,
    )).all())


def get_wind_cache(session: Session, cache_key: str) -> UsWindDataCache | None:
    return session.get(UsWindDataCache, cache_key)


def save_wind_cache(session: Session, row: UsWindDataCache) -> UsWindDataCache:
    existing = session.get(UsWindDataCache, row.cache_key)
    if existing is not None:
        return existing
    try:
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    except IntegrityError:
        session.rollback()
        existing = session.get(UsWindDataCache, row.cache_key)
        if existing is None:
            raise
        return existing


def save_wind_audit(session: Session, row: UsWindRequestAudit) -> UsWindRequestAudit:
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def latest_stop_suggestion(session: Session, candidate_id: int) -> UsStopSuggestion | None:
    return session.exec(select(UsStopSuggestion).where(
        UsStopSuggestion.candidate_id == candidate_id,
    ).order_by(UsStopSuggestion.created_at.desc(), UsStopSuggestion.suggestion_id.desc()).limit(1)).first()


def stop_suggestion_by_input(session: Session, *, candidate_id: int,
                             input_hash: str) -> UsStopSuggestion | None:
    return session.exec(select(UsStopSuggestion).where(
        UsStopSuggestion.candidate_id == candidate_id,
        UsStopSuggestion.input_hash == input_hash,
    )).first()


def get_stop_suggestion(session: Session, suggestion_id: str) -> UsStopSuggestion:
    row = session.get(UsStopSuggestion, suggestion_id)
    if row is None:
        raise UsManualError("stop_suggestion_not_found", "未找到双源止损建议", 404)
    return row


def save_stop_suggestion(session: Session, row: UsStopSuggestion) -> UsStopSuggestion:
    try:
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    except IntegrityError:
        session.rollback()
        existing = stop_suggestion_by_input(
            session, candidate_id=row.candidate_id, input_hash=row.input_hash,
        )
        if existing is None:
            raise
        return existing


def stop_review_for_suggestion(session: Session, suggestion_id: str) -> UsStopReview | None:
    return session.exec(select(UsStopReview).where(
        UsStopReview.suggestion_id == suggestion_id,
    ).order_by(UsStopReview.created_at, UsStopReview.review_id).limit(1)).first()


def stop_review_by_idempotency(session: Session, idempotency_key: str) -> UsStopReview | None:
    return session.exec(select(UsStopReview).where(
        UsStopReview.idempotency_key == idempotency_key,
    )).first()


def get_stop_review(session: Session, review_id: str) -> UsStopReview:
    row = session.get(UsStopReview, review_id)
    if row is None:
        raise UsManualError("stop_review_not_found", "未找到止损人工复核记录", 404)
    return row


def save_stop_review(session: Session, row: UsStopReview) -> UsStopReview:
    try:
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    except IntegrityError:
        session.rollback()
        replay = stop_review_by_idempotency(session, row.idempotency_key)
        if replay is not None:
            if replay.suggestion_id != row.suggestion_id:
                raise UsManualError(
                    "idempotency_conflict",
                    "该幂等键已用于另一条止损建议",
                    409,
                )
            return replay
        if stop_review_for_suggestion(session, row.suggestion_id) is not None:
            raise UsManualError(
                "stop_review_already_resolved",
                "该建议已经人工定稿，不能覆盖",
                409,
            )
        raise


def get_sizing_preview(session: Session, sizing_preview_id: str) -> UsSizingPreviewSnapshot:
    row = session.get(UsSizingPreviewSnapshot, sizing_preview_id)
    if row is None:
        raise UsManualError("sizing_preview_not_found", "未找到不可变仓位预览", 404)
    return row


def save_sizing_preview(session: Session, row: UsSizingPreviewSnapshot) -> UsSizingPreviewSnapshot:
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def current_account_snapshot(session: Session) -> UsManualAccountSnapshot | None:
    return session.exec(select(UsManualAccountSnapshot).order_by(
        UsManualAccountSnapshot.confirmed_at.desc(), UsManualAccountSnapshot.snapshot_id.desc(),
    ).limit(1)).first()


def save_account_snapshot(session: Session, row: UsManualAccountSnapshot) -> UsManualAccountSnapshot:
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_account_ocr_batch(session: Session, batch_id: str) -> UsAccountOcrBatch:
    row = session.get(UsAccountOcrBatch, batch_id)
    if row is None:
        raise UsManualError("account_ocr_batch_not_found", "未找到美股账户截图识别批次", 404)
    return row


def save_account_ocr_batch(session: Session, row: UsAccountOcrBatch) -> UsAccountOcrBatch:
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def latest_account_ocr_batch(session: Session) -> UsAccountOcrBatch | None:
    return session.exec(select(UsAccountOcrBatch).order_by(
        UsAccountOcrBatch.created_at.desc(), UsAccountOcrBatch.batch_id.desc(),
    ).limit(1)).first()


def latest_confirmed_account_ocr_snapshot(session: Session) -> UsManualAccountSnapshot | None:
    return session.exec(select(UsManualAccountSnapshot).where(
        UsManualAccountSnapshot.source == "bitget_ocr_confirmed",
    ).order_by(
        UsManualAccountSnapshot.confirmed_at.desc(),
        UsManualAccountSnapshot.snapshot_id.desc(),
    ).limit(1)).first()


def account_ocr_rows(session: Session, batch_id: str) -> list[UsAccountOcrPosition]:
    return list(session.exec(select(UsAccountOcrPosition).where(
        UsAccountOcrPosition.batch_id == batch_id,
    ).order_by(UsAccountOcrPosition.row_id)).all())


def replace_account_ocr_rows(
    session: Session, *, batch_id: str, rows: Iterable[UsAccountOcrPosition],
) -> list[UsAccountOcrPosition]:
    for old in account_ocr_rows(session, batch_id):
        session.delete(old)
    materialized = list(rows)
    session.add_all(materialized)
    session.commit()
    for row in materialized:
        session.refresh(row)
    return materialized


def get_plan(session: Session, plan_id: str) -> UsManualPlan:
    row = session.get(UsManualPlan, plan_id)
    if row is None:
        raise UsManualError("plan_not_found", "未找到美股手工清单", 404)
    return row


def plan_items(session: Session, plan_id: str) -> list[UsManualPlanItem]:
    return list(session.exec(select(UsManualPlanItem).where(
        UsManualPlanItem.plan_id == plan_id,
    ).order_by(UsManualPlanItem.priority, UsManualPlanItem.item_id)).all())


def plan_items_for_candidate(session: Session, candidate_id: int) -> list[UsManualPlanItem]:
    """Return every immutable plan link for a candidate, newest plan first."""
    return list(session.exec(
        select(UsManualPlanItem)
        .join(UsManualPlan, UsManualPlan.plan_id == UsManualPlanItem.plan_id)
        .where(UsManualPlanItem.candidate_id == candidate_id)
        .order_by(UsManualPlan.created_at.desc(), UsManualPlanItem.item_id.desc())
    ).all())


def get_plan_item(session: Session, item_id: int) -> UsManualPlanItem:
    row = session.get(UsManualPlanItem, item_id)
    if row is None:
        raise UsManualError("plan_item_not_found", "未找到美股计划项", 404)
    return row


def create_plan(session: Session, plan: UsManualPlan,
                items: Iterable[UsManualPlanItem]) -> tuple[UsManualPlan, list[UsManualPlanItem]]:
    session.add(plan)
    materialized = list(items)
    session.add_all(materialized)
    session.commit()
    session.refresh(plan)
    for item in materialized:
        session.refresh(item)
    return plan, materialized


def save_plan(session: Session, plan: UsManualPlan) -> UsManualPlan:
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan


def list_plans(session: Session, *, limit: int = 50) -> list[UsManualPlan]:
    return list(session.exec(select(UsManualPlan).order_by(
        UsManualPlan.created_at.desc(),
    ).limit(max(1, min(limit, 100)))).all())


def confirmed_executions(session: Session) -> list[UsManualExecution]:
    return list(session.exec(select(UsManualExecution).where(
        UsManualExecution.confirmed.is_(True),
    ).order_by(UsManualExecution.execution_id)).all())


def execution_by_idempotency(session: Session, idempotency_key: str) -> UsManualExecution | None:
    return session.exec(select(UsManualExecution).where(
        UsManualExecution.idempotency_key == idempotency_key,
    )).first()


def get_execution(session: Session, execution_id: int) -> UsManualExecution:
    row = session.get(UsManualExecution, execution_id)
    if row is None:
        raise UsManualError("execution_not_found", "未找到人工成交", 404)
    return row


def save_execution(session: Session, row: UsManualExecution) -> UsManualExecution:
    try:
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    except IntegrityError:
        session.rollback()
        existing = execution_by_idempotency(session, row.idempotency_key)
        if existing is None:
            raise
        return existing


def open_lots(session: Session, *, ticker_symbol: str | None = None) -> list[UsPositionLot]:
    statement = select(UsPositionLot).where(UsPositionLot.status == "open")
    if ticker_symbol:
        statement = statement.where(UsPositionLot.ticker_symbol == ticker_symbol)
    return list(session.exec(statement.order_by(UsPositionLot.opened_on_data_date, UsPositionLot.lot_id)).all())


def get_lot(session: Session, lot_id: int) -> UsPositionLot:
    row = session.get(UsPositionLot, lot_id)
    if row is None:
        raise UsManualError("position_not_found", "未找到本地美股持仓", 404)
    return row


def model_payload(row: Any) -> dict[str, Any]:
    return serialize(row.model_dump())


def monetary_sum(values: Iterable[Decimal | None]) -> Decimal:
    return sum((value or Decimal("0") for value in values), Decimal("0"))


def mark_plan_execution_status(session: Session, plan: UsManualPlan) -> UsManualPlan:
    """根据已确认的计划项数量派生计划状态，不改写锁定的风控字段。"""
    items = plan_items(session, plan.plan_id)
    if not items:
        return plan
    states = {item.status for item in items}
    if states == {"completed"}:
        plan.status = "completed"
    elif states & {"partially_executed", "completed"}:
        plan.status = "partially_executed"
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan
