"""Read-only US position calculator market-data endpoint."""
from fastapi import APIRouter, HTTPException

from backend.us_calculator import UsCalculatorError, fetch_us_market_snapshot


router = APIRouter(prefix="/api/us-calculator", tags=["us-calculator"])


@router.get("/market/{ticker}")
def market_snapshot(ticker: str):
    try:
        return fetch_us_market_snapshot(ticker)
    except UsCalculatorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_payload()) from exc
