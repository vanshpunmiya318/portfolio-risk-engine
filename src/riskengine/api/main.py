
import logging
import os
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request, Path as ApiPath, Query
from fastapi.encoders import jsonable_encoder

from riskengine.api.analysis_models import PerformanceResponse
from riskengine.risk.analysis_service import AnalysisInputError, AnalysisPortfolioNotFound
from riskengine.risk.performance_service import (
    get_performance_response, validate_performance_request, PerformanceDataConflict,
)
from riskengine.api.holdings_models import HoldingsResponse
from riskengine.risk.holdings_service import (
    get_holdings_summary, PortfolioNotFound, HoldingsInputError, HoldingsDataConflict,
)

from riskengine.db.connection import get_connection
from riskengine.db.portfolios import get_total_portfolio_equity
from riskengine.db.transactions import get_net_cash_flows

ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class DatabaseSelection:
    path: Path
    mode: str


def select_database() -> DatabaseSelection:
    if os.environ.get("RISKENGINE_DEMO") == "1":
        return DatabaseSelection(
            (ROOT / "data" / "demo_portfolio.db").resolve(), "demo"
        )
    override = os.environ.get("RISKENGINE_DB")
    return DatabaseSelection(
        Path(
            override if override is not None
            else ROOT / "data" / "historical_portfolio.db"
        ).expanduser().resolve(),
        "override" if override is not None else "historical",
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    selection = select_database()
    application.state.database = selection
    logger.info(
        "Portfolio API module=%s database=%s mode=%s access=read-only",
        Path(__file__).resolve(), selection.path, selection.mode,
    )
    yield


app = FastAPI(
    title="Portfolio Risk Engine API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/portfolios/{portfolio_id}/holdings", response_model=HoldingsResponse)
def portfolio_holdings(
    request: Request,
    portfolio_id: Annotated[int, ApiPath(gt=0, le=9223372036854775807)],
    as_of_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    max_price_age_days: Annotated[int, Query(
        ge=0, description="Maximum price age in calendar days; 0 requires an exact-date price.",
    )] = 0,
):
    if as_of_date is not None:
        try:
            date.fromisoformat(as_of_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="as_of_date must be a valid YYYY-MM-DD date")
    db_path = request.app.state.database.path
    if not db_path.is_file():
        raise HTTPException(status_code=404, detail="Portfolio database not found")
    try:
        with closing(get_connection(db_path, read_only=True)) as connection:
            connection.execute("BEGIN")  # One consistent read snapshot for every calculation.
            return get_holdings_summary(connection, portfolio_id, as_of_date, max_price_age_days)
    except PortfolioNotFound as error:
        raise HTTPException(status_code=404, detail=str(error))
    except HoldingsInputError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except HoldingsDataConflict as error:
        raise HTTPException(status_code=409, detail=str(error))


@app.get("/api/portfolio/summary")
def portfolio_summary(request: Request):
    db_path = request.app.state.database.path

    if not db_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Portfolio database not found",
        )

    with get_connection(db_path, read_only=True) as connection:
        portfolio = connection.execute(
            "SELECT id, name, currency FROM portfolios ORDER BY id LIMIT 1"
        ).fetchone()

        if portfolio is None:
            raise HTTPException(
                status_code=404,
                detail="No portfolio found",
            )

        portfolio_id, name, currency = portfolio

        as_of_date = connection.execute(
            "SELECT MAX(date) FROM prices"
        ).fetchone()[0]

        if as_of_date is None:
            raise HTTPException(
                status_code=404,
                detail="No market prices available",
            )

        equity = float(
            get_total_portfolio_equity(
                connection, portfolio_id, as_of_date
            )
        )

        contributions = float(
            get_net_cash_flows(
                connection, portfolio_id, as_of_date
            )
        )

        return {
            "portfolio_id": portfolio_id,
            "name": name,
            "currency": currency,
            "as_of_date": as_of_date,
            "total_equity": equity,
            "net_contributions": contributions,
            "profit_loss": equity - contributions,
        }


@app.get("/api/portfolios/{portfolio_id}/performance", response_model=PerformanceResponse)
def portfolio_performance(
    request: Request,
    portfolio_id: Annotated[int, ApiPath(gt=0, le=9223372036854775807)],
    start_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    end_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    benchmark: Annotated[str, Query(min_length=1)] = "SPY",
):
    try:
        validate_performance_request(start_date, end_date, benchmark)
    except AnalysisInputError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    db_path = request.app.state.database.path
    if not db_path.is_file():
        raise HTTPException(status_code=404, detail="Portfolio database not found")
    try:
        with closing(get_connection(db_path, read_only=True)) as connection:
            connection.execute("BEGIN")
            return get_performance_response(connection, portfolio_id, start_date, end_date, benchmark)
    except AnalysisPortfolioNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AnalysisInputError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PerformanceDataConflict as error:
        raise HTTPException(status_code=409, detail=jsonable_encoder([asdict(issue) for issue in error.issues])) from error
