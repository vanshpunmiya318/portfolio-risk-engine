
import os
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from riskengine.db.connection import get_connection
from riskengine.db.portfolios import add_portfolio
from riskengine.db.transactions import add_cash_flow, add_transaction
from riskengine.ingestion.tiingo_data import ingest_tiingo_prices
from riskengine.risk.full_report import generate_full_risk_report


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "historical_portfolio.db"
REPORT_PATH = ROOT / "reports" / "historical_portfolio_risk_report.md"
SCHEMA_DIR = ROOT / "src" / "riskengine" / "db" / "schema"

START_DATE = "2024-01-02"
END_DATE = "2026-10-23"
INITIAL_CAPITAL = 100_000.0

ETF_CONFIG = {
    "SPY": ("SPDR S&P 500 ETF Trust", "Equity", 0.30, "US Large-Cap Equity"),
    "QQQ": ("Invesco QQQ Trust", "Equity", 0.20, "US Growth Equity"),
    "IWM": ("iShares Russell 2000 ETF", "Equity", 0.15, "US Small-Cap Equity"),
    "GLD": ("SPDR Gold Shares", "Commodity ETF", 0.15, "Gold"),
    "TLT": ("iShares 20+ Year Treasury Bond ETF", "Bond ETF", 0.10, "US Treasuries"),
    "XLE": ("Energy Select Sector SPDR Fund", "Equity", 0.10, "Energy"),
}


def initialize_database(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as connection:
        for filename in (
            "instruments.sql",
            "prices.sql",
            "portfolios.sql",
        ):
            sql = (SCHEMA_DIR / filename).read_text(encoding="utf-8")
            connection.executescript(sql)


def ingest_market_data(db_path, token):
    for ticker, (name, asset_type, allocation, sector) in ETF_CONFIG.items():
        print(f"Ingesting {ticker}...")

        count = ingest_tiingo_prices(
            db_path=db_path,
            ticker=ticker,
            start_date=START_DATE,
            end_date=END_DATE,
            name=name,
            asset_type=asset_type,
            currency="USD",
            token=token,
            sector=sector,
        )

        print(f"  {count} price rows")


def find_common_purchase_date(connection):
    tickers = list(ETF_CONFIG)

    placeholders = ",".join("?" for _ in tickers)

    rows = connection.execute(
        f"""
        SELECT p.date, COUNT(DISTINCT i.ticker) AS ticker_count
        FROM prices p
        JOIN instruments i ON p.instrument_id = i.id
        WHERE i.ticker IN ({placeholders})
          AND p.date BETWEEN ? AND ?
        GROUP BY p.date
        HAVING COUNT(DISTINCT i.ticker) = ?
        ORDER BY p.date
        LIMIT 1
        """,
        (*tickers, START_DATE, END_DATE, len(tickers)),
    ).fetchall()

    if not rows:
        raise ValueError("No common purchase date for all six ETFs")

    return rows[0][0]


def build_portfolio(connection):
    purchase_date = find_common_purchase_date(connection)

    portfolio_id = add_portfolio(
        connection,
        "Historical Diversified ETF Portfolio",
        "USD",
    )

    add_cash_flow(
        connection,
        portfolio_id,
        purchase_date,
        "DEPOSIT",
        INITIAL_CAPITAL,
        "USD",
        "historical_demo",
    )

    purchases = []

    for ticker, (_, _, allocation, _) in ETF_CONFIG.items():
        instrument_id, close_price = connection.execute(
            """
            SELECT i.id, p.close
            FROM instruments i
            JOIN prices p ON p.instrument_id = i.id
            WHERE i.ticker = ?
              AND p.date = ?
            """,
            (ticker, purchase_date),
        ).fetchone()

        close_price = float(close_price)

        if close_price <= 0:
            raise ValueError(f"Invalid purchase price for {ticker}")

        budget = INITIAL_CAPITAL * allocation
        quantity = int(budget // close_price)

        if quantity < 1:
            raise ValueError(f"Insufficient budget to buy {ticker}")

        add_transaction(
            connection,
            portfolio_id,
            instrument_id,
            purchase_date,
            "BUY",
            quantity,
            close_price,
            "USD",
            "historical_demo",
        )

        purchases.append({
            "ticker": ticker,
            "quantity": quantity,
            "price": close_price,
            "cost": quantity * close_price,
        })

    total_cost = sum(p["cost"] for p in purchases)

    if total_cost > INITIAL_CAPITAL + 1e-8:
        raise ValueError("Purchases exceed initial capital")

    return portfolio_id, purchase_date, purchases


def main():
    token = os.environ.get("TIINGO_API_KEY")

    if not token:
        raise RuntimeError(
            "Set TIINGO_API_KEY before running the script"
        )

    if DB_PATH.exists():
        raise FileExistsError(
            f"Historical database already exists: {DB_PATH}. "
            "Refusing to overwrite existing data."
        )

    initialize_database(DB_PATH)
    ingest_market_data(DB_PATH, token)

    with get_connection(DB_PATH) as connection:
        with connection:
            portfolio_id, purchase_date, purchases = build_portfolio(
                connection
            )

        result = generate_full_risk_report(
            connection,
            portfolio_id,
            purchase_date,
            END_DATE,
            benchmark_ticker="SPY",
        )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(result["markdown"], encoding="utf-8")

    print(f"\nPortfolio ID: {portfolio_id}")
    print(f"Purchase date: {purchase_date}")

    for purchase in purchases:
        print(
            f"{purchase['ticker']}: "
            f"{purchase['quantity']} shares, "
            f"${purchase['cost']:,.2f}"
        )

    print(f"\nReport saved to: {REPORT_PATH}")


if __name__ == "__main__":
    main()
