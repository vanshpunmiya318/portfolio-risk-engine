
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

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
