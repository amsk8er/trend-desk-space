"""只读市场证据摘要 API。"""
from fastapi import APIRouter
from sqlmodel import Session

from backend.discipline.market_evidence import build_market_evidence
from backend.engine import engine


router = APIRouter(prefix="/api/discipline", tags=["discipline"])


@router.get("/market-evidence")
def market_evidence(trade_date: str | None = None):
    with Session(engine) as session:
        return build_market_evidence(session, trade_date=trade_date)
