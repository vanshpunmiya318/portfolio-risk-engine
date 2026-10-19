import pandas as pd

from riskengine.db.prices import get_latest_prices
from riskengine.db.transactions import (
    get_positions,
    validate_transactions,
    get_cash_balance,
    get_net_cash_flows,
    get_cash_flow_on_date,
    get_cash_flow_dates,
)

from riskengine.db.prices import get_price_with_max_age

from riskengine.db.prices import get_portfolio_price_coverage

def normalise_portfolio_name(name):
    return name.strip()


def normalise_currency(currency):
    return currency.strip().upper()


def add_portfolio(connection, name, currency):
    cursor = connection.execute(
        """
        INSERT INTO portfolios (
            name,
            currency
        )
        VALUES (?, ?)
        """,
        (
            normalise_portfolio_name(name),
            normalise_currency(currency),
        ),
    )

    return cursor.lastrowid


def get_portfolio_id(connection, name):
    result = connection.execute(
        """
        SELECT id
        FROM portfolios
        WHERE LOWER(name) = LOWER(?)
        """,
        (normalise_portfolio_name(name),),
    ).fetchone()

    if result is None:
        return None

    return result[0]


def get_or_create_portfolio(connection, name, currency):
    portfolio_id = get_portfolio_id(
        connection,
        name,
    )

    if portfolio_id is not None:
        return portfolio_id

    return add_portfolio(
        connection,
        name,
        currency,
    )



def get_portfolio_value(
    connection,
    portfolio_id,
    as_of_date,
    max_age_days=None,
):
    """
    Value active portfolio holdings using the latest available prices.

    When max_age_days is None, preserve the existing behaviour
    without a maximum price-age restriction.

    When max_age_days is specified, reject prices older than
    the permitted number of calendar days.
    """
    if max_age_days is not None:
        if (
            not isinstance(max_age_days, int)
            or isinstance(max_age_days, bool)
        ):
            raise ValueError(
                "max_age_days must be an integer or None"
            )

        if max_age_days < 0:
            raise ValueError(
                "max_age_days must not be negative"
            )

    validate_transactions(
        connection,
        portfolio_id,
    )

    positions = get_positions(
        connection,
        portfolio_id,
        as_of_date,
    )

    positions = positions[
        positions["quantity"] > 0
    ].copy()

    if positions.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "quantity",
                "price_date",
                "adjusted_close",
                "market_value",
            ]
        )

    prices = get_latest_prices(
        connection,
        positions["ticker"].tolist(),
        as_of_date,
    )

    missing_tickers = set(
        positions["ticker"]
    ) - set(
        prices["ticker"]
    )

    if missing_tickers:
        raise ValueError(
            "No price available as of "
            f"{as_of_date} for: "
            f"{sorted(missing_tickers)}"
        )

    result = positions.merge(
        prices,
        on="ticker",
        how="left",
    )

    if max_age_days is not None:
        from datetime import date

        valuation_date = date.fromisoformat(
            as_of_date
        )

        result["price_age_days"] = (
    pd.Timestamp(valuation_date)
    - pd.to_datetime(result["date"])
        ).dt.days

        stale_tickers = result.loc[
            result["price_age_days"] > max_age_days,
            "ticker",
        ].tolist()

        if stale_tickers:
            raise ValueError(
                "Price exceeds maximum permitted age "
                f"on {as_of_date} for: "
                f"{sorted(stale_tickers)}"
            )
    result["market_value"] = (
    result["quantity"]
    * result["adjusted_close"]
    )

    result["date"] = pd.to_datetime(
    result["date"]
    )

    return result[
        [
            "ticker",
            "quantity",
            "date",
            "adjusted_close",
            "market_value",
        ]
    ].rename(
        columns={
            "date": "price_date",
        }
    )




def get_total_portfolio_value(
    connection,
    portfolio_id,
    as_of_date,
    max_age_days=None,
):
    portfolio_value = get_portfolio_value(
        connection,
        portfolio_id,
        as_of_date,
        max_age_days=max_age_days,
    )

    return portfolio_value["market_value"].sum()





def get_portfolio_weights(
    connection,
    portfolio_id,
    as_of_date,
    max_age_days=None,
):
    portfolio_value = get_portfolio_value(
        connection,
        portfolio_id,
        as_of_date,
        max_age_days=max_age_days,
    )

    if portfolio_value.empty:
        return portfolio_value.assign(
            weight=pd.Series(dtype=float)
        )

    total_value = portfolio_value["market_value"].sum()

    if total_value <= 0:
        raise ValueError(
            "Portfolio market value must be positive"
        )

    portfolio_value["weight"] = (
        portfolio_value["market_value"] / total_value
    )

    return portfolio_value




def get_portfolio_return(
    connection,
    portfolio_id,
    start_date,
    end_date,
    max_age_days=None,
):
    start_value = get_total_portfolio_value(
        connection,
        portfolio_id,
        start_date,
        max_age_days=max_age_days,
    )

    end_value = get_total_portfolio_value(
        connection,
        portfolio_id,
        end_date,
        max_age_days=max_age_days,
    )

    if start_value <= 0:
        raise ValueError(
            "Starting portfolio value must be positive"
        )

    return (end_value / start_value) - 1





def get_total_portfolio_equity(
    connection,
    portfolio_id,
    as_of_date,
    max_age_days=None,
):
    holdings_value = get_total_portfolio_value(
        connection,
        portfolio_id,
        as_of_date,
        max_age_days=max_age_days,
    )

    cash_balance = get_cash_balance(
        connection,
        portfolio_id,
        as_of_date,
    )

    return holdings_value + cash_balance



def get_cash_flow_adjusted_return(
    connection,
    portfolio_id,
    start_date,
    end_date,
    max_age_days=None,
):
    start_equity = get_total_portfolio_equity(
        connection,
        portfolio_id,
        start_date,
        max_age_days=max_age_days,
    )

    end_equity = get_total_portfolio_equity(
        connection,
        portfolio_id,
        end_date,
        max_age_days=max_age_days,
    )

    if start_equity <= 0:
        raise ValueError(
            "Starting portfolio equity must be positive"
        )

    net_external_flow = (
        get_net_cash_flows(
            connection,
            portfolio_id,
            end_date,
        )
        - get_net_cash_flows(
            connection,
            portfolio_id,
            start_date,
        )
    )

    investment_profit = (
        end_equity - start_equity - net_external_flow
    )

    return investment_profit / start_equity




def get_portfolio_subperiod_return(
    connection,
    portfolio_id,
    previous_date,
    current_date,
    max_age_days=0,
):
    if current_date <= previous_date:
        raise ValueError(
            "Current date must be after previous date"
        )

    cash_flow_dates = get_cash_flow_dates(
        connection,
        portfolio_id,
        previous_date,
        current_date,
    )

    intermediate_dates = [
        date for date in cash_flow_dates
        if date < current_date
    ]

    if intermediate_dates:
        raise ValueError(
            "Intermediate cash flows require separate return periods"
        )

    for valuation_date in [previous_date, current_date]:
        coverage = get_portfolio_price_coverage(
            connection,
            portfolio_id,
            valuation_date,
            max_age_days=max_age_days,
        )

        if not coverage["is_complete"]:
            raise ValueError(
                f"Incomplete price coverage on {valuation_date}: "
                f"missing instrument IDs "
                f"{coverage['missing_instrument_ids']}"
            )

    previous_equity = get_total_portfolio_equity(
        connection,
        portfolio_id,
        previous_date,
        max_age_days=max_age_days,
    )

    current_equity = get_total_portfolio_equity(
        connection,
        portfolio_id,
        current_date,
        max_age_days=max_age_days,
    )

    external_flow = get_cash_flow_on_date(
        connection,
        portfolio_id,
        current_date,
    )

    # External cash flows are treated as end-of-period.
    adjusted_ending_equity = current_equity - external_flow

    if previous_equity <= 0:
        raise ValueError(
            "Starting capital must be positive"
        )

    return (adjusted_ending_equity / previous_equity) - 1




def get_time_weighted_return(
    connection,
    portfolio_id,
    start_date,
    end_date,
    max_age_days=0,
):
    if end_date <= start_date:
        raise ValueError(
            "End date must be after start date"
        )

    cash_flow_dates = get_cash_flow_dates(
        connection,
        portfolio_id,
        start_date,
        end_date,
    )

    period_dates = [
        start_date,
        *cash_flow_dates,
    ]

    if period_dates[-1] != end_date:
        period_dates.append(end_date)

    compounded_growth = 1.0

    for previous_date, current_date in zip(
        period_dates[:-1],
        period_dates[1:],
    ):
        period_return = get_portfolio_subperiod_return(
            connection,
            portfolio_id,
            previous_date,
            current_date,
            max_age_days=max_age_days,
        )

        compounded_growth *= 1 + period_return

    return compounded_growth - 1


        
    """
    Calculate a linked portfolio return across cash-flow subperiods.

    Performance measurement uses an end-of-period external
    cash-flow adjustment:

        return = (ending_equity - external_flow) / beginning_equity - 1

    Subperiod returns are geometrically compounded.

    This convention is separate from transaction cash accounting,
    where cash flows affect balances on their recorded dates.

    Limitations:
    - Uses date-level closing valuations rather than intraday values.
    - Does not establish valuations immediately before actual flows.
    - Assumes external flows occur at the end of each measurement
      subperiod for performance attribution.
    - Not yet a fully validated production-grade TWR implementation.
    """





def get_portfolio_equity_curve(
    connection,
    portfolio_id,
    start_date,
    end_date,
    max_age_days=0,
):
    """
    Calculate historical portfolio equity on dates where
    portfolio instruments have recorded market prices.

    Equity = historical holdings market value + cash balance.

    Includes the requested start and end dates.

    max_age_days=0 requires exact-date prices.
    """

    from datetime import date

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    if end < start:
        raise ValueError(
            "End date must not be before start date"
        )

    if (
        not isinstance(max_age_days, int)
        or isinstance(max_age_days, bool)
    ):
        raise ValueError(
            "max_age_days must be an integer"
        )

    if max_age_days < 0:
        raise ValueError(
            "max_age_days must not be negative"
        )

    price_rows = connection.execute(
        """
        SELECT DISTINCT p.date
        FROM prices AS p
        JOIN transactions AS t
            ON t.instrument_id = p.instrument_id
        WHERE t.portfolio_id = ?
          AND t.date <= ?
          AND p.date >= ?
          AND p.date <= ?
        ORDER BY p.date
        """,
        (
            portfolio_id,
            end_date,
            start_date,
            end_date,
        ),
    ).fetchall()

    transaction_rows = connection.execute(
        """
        SELECT DISTINCT date
        FROM transactions
        WHERE portfolio_id = ?
          AND date >= ?
          AND date <= ?
        ORDER BY date
        """,
        (
            portfolio_id,
            start_date,
            end_date,
        ),
    ).fetchall()

    cash_flow_rows = connection.execute(
        """
        SELECT DISTINCT date
        FROM cash_flows
        WHERE portfolio_id = ?
          AND date >= ?
          AND date <= ?
        ORDER BY date
        """,
        (
            portfolio_id,
            start_date,
            end_date,
        ),
    ).fetchall()

    valuation_dates = sorted(
        {
            start_date,
            end_date,
            *[row[0] for row in price_rows],
            *[row[0] for row in transaction_rows],
            *[row[0] for row in cash_flow_rows],
        }
    )






    records = []

    for valuation_date in valuation_dates:
        coverage = get_portfolio_price_coverage(
            connection,
            portfolio_id,
            valuation_date,
            max_age_days=max_age_days,
        )

        if not coverage["is_complete"]:
            raise ValueError(
                f"Incomplete price coverage on {valuation_date}: "
                f"missing instrument IDs "
                f"{coverage['missing_instrument_ids']}"
            )

        positions = get_positions(
            connection,
            portfolio_id,
            valuation_date,
        )

        active_holdings = positions[
            positions["quantity"] > 0
        ]

        holdings_value = get_total_portfolio_value(
            connection,
            portfolio_id,
            valuation_date,
            max_age_days=max_age_days,
        )

        cash_balance = get_cash_balance(
            connection,
            portfolio_id,
            valuation_date,
        )

        records.append(
            {
                "date": pd.Timestamp(valuation_date),
                "holdings_value": float(holdings_value),
                "cash_balance": float(cash_balance),
                "total_equity": float(
                    holdings_value + cash_balance
                ),
                "active_holdings": len(active_holdings),
            }
        )

    return pd.DataFrame(
        records,
        columns=[
            "date",
            "holdings_value",
            "cash_balance",
            "total_equity",
            "active_holdings",
        ],
    )