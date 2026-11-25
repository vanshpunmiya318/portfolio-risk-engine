
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request, Path as ApiPath, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from starlette.datastructures import MutableHeaders
from starlette.types import Message, Receive, Scope, Send

from riskengine.api.analysis_models import PerformanceResponse, RiskResponse
from riskengine.api.allocation_models import AllocationResponse
from riskengine.api.transaction_models import TransactionsResponse
from riskengine.risk.transaction_history import HistoryDataConflict, get_transactions_history
from riskengine.api.report_models import ReportResponse
from riskengine.risk.report_service import get_report_response
from riskengine.risk.report_exports import report_json, report_markdown, report_filename
from riskengine.risk.allocation_service import get_allocation_response
from riskengine.risk.analysis_service import AnalysisInputError, AnalysisPortfolioNotFound
from riskengine.risk.performance_service import (
    get_performance_response, validate_performance_request, PerformanceDataConflict,
    get_risk_response, validate_risk_request,
)
from riskengine.api.holdings_models import HoldingsResponse
from riskengine.api.local_access import LocalAccess, SESSION_PATH, deny, load_local_access
from riskengine.risk.holdings_service import (
    get_holdings_summary, PortfolioNotFound, HoldingsInputError, HoldingsDataConflict,
)

from riskengine.db.application_writes import ApplicationWrites
from riskengine.db.connection import get_read_only_connection
from riskengine.db.portfolios import MissingHistoricalPriceError, get_total_portfolio_equity
from riskengine.db.transactions import get_net_cash_flows, has_portfolio_currency_conflict

ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("uvicorn.error")


def get_connection(db_path: Path, *, read_only: Literal[True]) -> sqlite3.Connection:
    """Keep the API's existing connection entry point restricted to reads."""
    if read_only is not True:
        raise ValueError("Use the application write boundary for writable connections.")
    return get_read_only_connection(db_path)


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
    previous_access = getattr(application.state, "local_access", None)
    previous_writes = getattr(application.state, "database_writes", None)
    access = load_local_access()
    selection = select_database()
    application.state.local_access = access
    application.state.database = selection
    application.state.database_writes = ApplicationWrites(
        selection.path, selection.mode, protected=isinstance(access, LocalAccess),
        data_root=os.environ.get("RISKENGINE_WRITE_DATA_ROOT"),
    )
    logger.info(
        "Portfolio API module=%s database=%s mode=%s access=read-only",
        Path(__file__).resolve(), selection.path, selection.mode,
    )
    try:
        yield
    finally:
        application.state.local_access = previous_access
        application.state.database_writes = previous_writes


class PortfolioAPI(FastAPI):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return

        path = scope["path"]
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path):]
        access = getattr(self.state, "local_access", None)
        if access is None:
            await deny(503, "Local API is not initialized. Start with dev.cmd.")(scope, receive, send)
            return
        if access is not False:
            local_request = Request(scope)
            rejection = access.guard(local_request, path)
            if rejection is not None:
                await rejection(scope, receive, send)
                return
        elif scope["method"] not in {"GET", "HEAD"}:
            await deny(403, "Legacy API mode is read-only.")(scope, receive, send)
            return
        cache_prefixes = ("/api/",) if access is not False else ("/api/portfolio/", "/api/portfolios/")
        if path != SESSION_PATH and not (path + "/").startswith(cache_prefixes):
            await super().__call__(scope, receive, send)
            return

        # Wrap the ASGI send outside FastAPI's error middleware so generated 500s are covered.
        successful = False

        async def send_no_store(message: Message) -> None:
            nonlocal successful
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
                successful = message["status"] == 200
            await send(message)
            if access is not False and successful and message["type"] == "http.response.body" and not message.get("more_body"):
                access.confirm_browser(local_request, path)

        await super().__call__(scope, receive, send_no_store)


app = PortfolioAPI(
    title="Portfolio Risk Engine API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get(SESSION_PATH)
def local_session(request: Request):
    access = getattr(request.app.state, "local_access", False)
    return {"generation": access.generation if access else "legacy-read-only"}


@app.get("/api/portfolios/{portfolio_id}/report", response_model=ReportResponse)
def portfolio_report(
    request: Request,
    portfolio_id: Annotated[int, ApiPath(gt=0, le=9223372036854775807)],
    start_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    end_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    benchmark: Annotated[str, Query(min_length=1)] = "SPY",
    confidence: Annotated[float, Query(gt=0, lt=1, allow_inf_nan=False)] = 0.95,
):
    return _compose_report(request, portfolio_id, start_date, end_date, benchmark, confidence)


@app.get("/api/portfolios/{portfolio_id}/report/export")
def export_portfolio_report(
    request: Request,
    portfolio_id: Annotated[int, ApiPath(gt=0, le=9223372036854775807)],
    format: Annotated[Literal["json", "markdown"], Query()],
    start_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    end_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    benchmark: Annotated[str, Query(min_length=1)] = "SPY",
    confidence: Annotated[float, Query(gt=0, lt=1, allow_inf_nan=False)] = 0.95,
):
    report = _compose_report(request, portfolio_id, start_date, end_date, benchmark, confidence)
    content = report_json(report) if format == "json" else report_markdown(report)
    return Response(content=content,
                    media_type="application/json" if format == "json" else "text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{report_filename(report, format)}"'})


def _compose_report(request, portfolio_id, start_date, end_date, benchmark, confidence):
    """Shared validation/error mapping and single snapshot, without internal HTTP."""
    try:
        validate_risk_request(start_date, end_date, benchmark, confidence)
        db_path = request.app.state.database.path
        if not db_path.is_file():
            raise HTTPException(status_code=404, detail="Portfolio database not found")
        with closing(get_connection(db_path, read_only=True)) as connection:
            connection.execute("BEGIN")
            return get_report_response(connection, portfolio_id, start_date, end_date, benchmark, confidence)
    except (AnalysisPortfolioNotFound, PortfolioNotFound) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (AnalysisInputError, HoldingsInputError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PerformanceDataConflict as error:
        raise HTTPException(status_code=409, detail=jsonable_encoder([asdict(issue) for issue in error.issues])) from error
    except HoldingsDataConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/ready")
def readiness_check(request: Request):
    try:
        with closing(get_connection(request.app.state.database.path, read_only=True)) as connection:
            connection.execute("BEGIN")
            # Resolve columns used by the serving API. Empty tables are valid.
            for query in (
                "SELECT id, name, currency FROM portfolios LIMIT 1",
                "SELECT id, ticker, name, currency, sector, primary_source FROM instruments LIMIT 1",
                "SELECT id, portfolio_id, instrument_id, date, transaction_type, quantity, price, currency, source "
                "FROM transactions LIMIT 1",
                "SELECT id, portfolio_id, date, flow_type, amount, currency, source FROM cash_flows LIMIT 1",
                "SELECT instrument_id, date, open, high, low, close, adjusted_close, volume, source, fetched_at "
                "FROM prices LIMIT 1",
                "SELECT id, instrument_id, ex_date, action_type, ratio FROM corporate_actions LIMIT 1",
                "SELECT instrument_id, start_date, end_date, source FROM corporate_action_coverage LIMIT 1",
            ):
                connection.execute(query).fetchone()
    except (OSError, sqlite3.Error) as error:
        raise HTTPException(status_code=503, detail="Configured database is not ready") from error
    return {"status": "ready"}


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
    return _portfolio_summary(request)


@app.get("/api/portfolios/{portfolio_id}/summary")
def portfolio_summary_by_id(
    request: Request,
    portfolio_id: Annotated[int, ApiPath(gt=0, le=9223372036854775807)],
):
    return _portfolio_summary(request, portfolio_id)


def _portfolio_summary(request: Request, portfolio_id: int | None = None):
    """Share the existing Overview calculations; only portfolio selection differs."""
    db_path = request.app.state.database.path

    if not db_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Portfolio database not found",
        )

    with closing(get_connection(db_path, read_only=True)) as connection:
        connection.execute("BEGIN")
        portfolio = (
            connection.execute("SELECT id, name, currency FROM portfolios ORDER BY id LIMIT 1")
            if portfolio_id is None else
            connection.execute("SELECT id, name, currency FROM portfolios WHERE id = ?", (portfolio_id,))
        ).fetchone()

        if portfolio is None:
            raise HTTPException(
                status_code=404,
                detail="No portfolio found" if portfolio_id is None else f"Portfolio {portfolio_id} not found",
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

        if has_portfolio_currency_conflict(connection, portfolio_id, as_of_date, currency):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Overview totals are unavailable because a trade, instrument, or cash flow "
                    f"uses a currency other than {currency}. No currency conversion was performed."
                ),
            )

        try:
            equity = float(get_total_portfolio_equity(connection, portfolio_id, as_of_date))
        except MissingHistoricalPriceError as error:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Overview totals are unavailable because a required historical security price "
                    f"is missing on or before {as_of_date}."
                ),
            ) from error

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


@app.get("/api/portfolios/{portfolio_id}/risk", response_model=RiskResponse)
def portfolio_risk(
    request: Request,
    portfolio_id: Annotated[int, ApiPath(gt=0, le=9223372036854775807)],
    start_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    end_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    benchmark: Annotated[str, Query(min_length=1)] = "SPY",
    confidence: Annotated[float, Query(gt=0, lt=1, allow_inf_nan=False)] = 0.95,
):
    try:
        validate_risk_request(start_date, end_date, benchmark, confidence)
    except AnalysisInputError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    db_path = request.app.state.database.path
    if not db_path.is_file():
        raise HTTPException(status_code=404, detail="Portfolio database not found")
    try:
        with closing(get_connection(db_path, read_only=True)) as connection:
            connection.execute("BEGIN")
            return get_risk_response(connection, portfolio_id, start_date, end_date, benchmark, confidence)
    except AnalysisPortfolioNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except AnalysisInputError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PerformanceDataConflict as error:
        raise HTTPException(status_code=409, detail=jsonable_encoder([asdict(issue) for issue in error.issues])) from error


@app.get("/api/portfolios/{portfolio_id}/allocation", response_model=AllocationResponse)
def portfolio_allocation(
    request: Request,
    portfolio_id: Annotated[int, ApiPath(gt=0, le=9223372036854775807)],
    as_of_date: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
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
            connection.execute("BEGIN")
            return get_allocation_response(connection, portfolio_id, as_of_date)
    except PortfolioNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except HoldingsInputError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except HoldingsDataConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/portfolios/{portfolio_id}/transactions", response_model=TransactionsResponse)
def portfolio_transactions(
    request: Request,
    portfolio_id: Annotated[int, ApiPath(gt=0, le=9223372036854775807)],
):
    db_path = request.app.state.database.path
    if not db_path.is_file():
        raise HTTPException(status_code=404, detail="Portfolio database not found")
    try:
        with closing(get_connection(db_path, read_only=True)) as connection:
            connection.execute("BEGIN")
            return get_transactions_history(connection, portfolio_id)
    except PortfolioNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except HistoryDataConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
