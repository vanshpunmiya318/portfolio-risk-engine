"""Opt-in legacy transaction submission; bundled datasets are always protected."""
import os
from contextlib import closing
from pathlib import Path

from riskengine.db.connection import get_connection
from riskengine.db.transactions import (
    add_transaction, get_positions, get_cash_balance,
    validate_transactions, validate_cash_balances,
)

ROOT = Path(__file__).resolve().parents[3]


def transaction_writes_allowed(database, demo_mode, *, environ=None):
    """Explicit separate database plus opt-in; resolve aliases and hard links."""
    env = os.environ if environ is None else environ
    if demo_mode or env.get("RISKENGINE_ALLOW_TRANSACTION_WRITES") != "1":
        return False
    configured = env.get("RISKENGINE_DB", "").strip()
    if not configured:
        return False
    try:
        target = Path(database).expanduser().resolve()
        if target != Path(configured).expanduser().resolve() or not target.is_file():
            return False
        for name in ("historical_portfolio.db", "demo_portfolio.db"):
            protected = (ROOT / "data" / name).resolve()
            if target == protected or (protected.exists() and target.samefile(protected)):
                return False
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def record_streamlit_transaction(database, demo_mode, portfolio_id, instrument_id,
                                 ticker, trade_date, transaction_type, quantity, price):
    # Enforce on submission, even if the form's disabled state is bypassed.
    if not transaction_writes_allowed(database, demo_mode):
        raise PermissionError("Transaction writes are disabled for this database.")
    trade_value = quantity * price
    with closing(get_connection(database)) as connection:
        price_exists = connection.execute(
            """
            SELECT 1 FROM prices
            WHERE instrument_id = ? AND date = ?
            LIMIT 1
            """,
            (instrument_id, trade_date),
        ).fetchone()
        if price_exists is None:
            raise ValueError(
                f"No {ticker} closing price for {trade_date}. "
                "Choose a date with market data."
            )

        positions = get_positions(
            connection,
            portfolio_id,
            trade_date,
        )

        current_shares = 0.0

        matching = positions.loc[
            positions["ticker"] == ticker,
            "quantity",
        ]

        if not matching.empty:
            current_shares = float(matching.iloc[0])

        available_cash = get_cash_balance(
            connection,
            portfolio_id,
            trade_date,
        )

        if (
            transaction_type == "SELL"
            and quantity > current_shares + 1e-9
        ):
            raise ValueError(
                f"Insufficient shares. "
                f"Available: {current_shares:g}"
            )

        if (
            transaction_type == "BUY"
            and trade_value > available_cash + 1e-9
        ):
            raise ValueError(
                f"Insufficient cash. "
                f"Available: ${available_cash:,.2f}"
            )

        # The transaction and validation are atomic:
        # failures roll back the new record.
        with connection:
            add_transaction(
                connection,
                portfolio_id,
                instrument_id,
                trade_date,
                transaction_type,
                int(quantity),
                float(price),
                "USD",
                "streamlit_dashboard",
            )

            validate_transactions(
                connection,
                portfolio_id,
            )
            validate_cash_balances(
                connection,
                portfolio_id,
            )
