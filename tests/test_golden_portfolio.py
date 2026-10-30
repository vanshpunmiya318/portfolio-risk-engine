import pytest

from riskengine.db.instruments import add_instrument
from riskengine.db.prices import add_price
from riskengine.db.transactions import add_cash_flow
from riskengine.db.portfolios import get_portfolio_equity_curve
import pandas as pd

from riskengine.risk.metrics import (
    annualized_volatility,
    maximum_drawdown,
    historical_var,
    historical_cvar,
)
from riskengine.db.portfolios import (
    get_portfolio_subperiod_return,
    get_time_weighted_return,
    get_cash_flow_adjusted_return,
)

from riskengine.db.portfolios import (
    add_portfolio,
    get_total_portfolio_equity,
)
from riskengine.db.transactions import (
    add_transaction,
    get_cash_balance,
    get_positions,
)

EXPECTED_EQUITY = {
    "2026-01-05": 20000.00,
    "2026-01-06": 20140.00,
    "2026-01-07": 20205.00,
    "2026-01-08": 20238.00,
    "2026-01-09": 20292.00,
}

EXPECTED_CASH = {
    "2026-01-05": 17000.00,
    "2026-01-06": 17000.00,
    "2026-01-07": 17440.00,
    "2026-01-08": 17440.00,
    "2026-01-09": 17440.00,
}

EXPECTED_POSITIONS = {
    "2026-01-05": {"AAPL": 10, "MSFT": 5, "NVDA": 10},
    "2026-01-06": {"AAPL": 10, "MSFT": 5, "NVDA": 10},
    "2026-01-07": {"AAPL": 6, "MSFT": 5, "NVDA": 10},
    "2026-01-08": {"AAPL": 6, "MSFT": 5, "NVDA": 20},
    "2026-01-09": {"AAPL": 6, "MSFT": 5, "NVDA": 20},
}



from riskengine.db.schema.loader import create_schema


@pytest.fixture
def golden_db(tmp_path, db_connection):
    db_path = tmp_path / "golden_portfolio.db"

    create_schema(db_path)
    connection = db_connection(db_path)

    instruments = {}

    for ticker, name in [
        ("AAPL", "Apple"),
        ("MSFT", "Microsoft"),
        ("NVDA", "NVIDIA"),
    ]:
        instrument_id = add_instrument(
            connection,
            ticker=ticker,
            name=name,
            asset_type="stock",
            currency="USD",
            sector="Technology",
        )

        connection.execute(
            """
            UPDATE instruments
            SET primary_source = 'test'
            WHERE id = ?
            """,
            (instrument_id,),
        )

        instruments[ticker] = instrument_id

    connection.commit()

    daily_prices = {
        "AAPL": [100, 105, 110, 108, 112],
        "MSFT": [200, 198, 205, 210, 208],
        "NVDA": [100, 110, 108, 55, 57],
    }

    dates = [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
        "2026-01-08",
        "2026-01-09",
    ]

    for ticker, prices in daily_prices.items():
        for date, price in zip(dates, prices):
            add_price(
                connection,
                instrument_id=instruments[ticker],
                date=date,
                open_price=price,
                high=price,
                low=price,
                close=price,
                adjusted_close=price,
                volume=1000,
                source="test",
                fetched_at="2026-01-10T00:00:00",
            )

    connection.commit()

    portfolio_id = add_portfolio(
        connection,
        name="Golden Portfolio",
        currency="USD",
    )

    add_cash_flow(
        connection,
        portfolio_id=portfolio_id,
        date="2026-01-05",
        flow_type="DEPOSIT",
        amount=20000.00,
        currency="USD",
        source="test",
    )

    trades = [
        ("2026-01-05", "AAPL", "BUY", 10, 100),
        ("2026-01-05", "MSFT", "BUY", 5, 200),
        ("2026-01-05", "NVDA", "BUY", 10, 100),
        ("2026-01-07", "AAPL", "SELL", 4, 110),
    ]

    for date, ticker, transaction_type, quantity, price in trades:
        add_transaction(
            connection,
            portfolio_id=portfolio_id,
            instrument_id=instruments[ticker],
            date=date,
            transaction_type=transaction_type,
            quantity=quantity,
            price=price,
            currency="USD",
            source="test",
        )

    connection.execute(
        """
        INSERT INTO corporate_actions (
            instrument_id,
            ex_date,
            action_type,
            ratio,
            source,
            fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            instruments["NVDA"],
            "2026-01-08",
            "SPLIT",
            2.0,
            "test",
            "2026-01-10T00:00:00",
        ),
    )

    connection.commit()

    yield connection, instruments, portfolio_id



def test_golden_portfolio_positions(golden_db):
    connection, instruments, portfolio_id = golden_db

    for date, expected in EXPECTED_POSITIONS.items():
        positions = get_positions(
            connection,
            portfolio_id,
            as_of_date=date,
        )

        actual = {
            row["ticker"]: row["quantity"]
            for _, row in positions.iterrows()
            if row["quantity"] > 0
        }

        assert actual == expected




def test_golden_portfolio_cash(golden_db):
    connection, instruments, portfolio_id = golden_db

    for date, expected_cash in EXPECTED_CASH.items():
        actual_cash = get_cash_balance(
            connection,
            portfolio_id,
            as_of_date=date,
        )

        assert actual_cash == pytest.approx(
            expected_cash,
            abs=1e-10,
        )



def test_golden_portfolio_equity(golden_db):
    connection, instruments, portfolio_id = golden_db

    for date, expected_equity in EXPECTED_EQUITY.items():
        actual_equity = get_total_portfolio_equity(
            connection,
            portfolio_id,
            as_of_date=date,
        )

        assert actual_equity == pytest.approx(
            expected_equity,
            abs=1e-10,
        )



def test_golden_portfolio_daily_returns(golden_db):
    connection, instruments, portfolio_id = golden_db

    dates = list(EXPECTED_EQUITY)

    for previous_date, current_date in zip(
        dates[:-1],
        dates[1:],
    ):
        previous_equity = get_total_portfolio_equity(
            connection,
            portfolio_id,
            as_of_date=previous_date,
        )

        current_equity = get_total_portfolio_equity(
            connection,
            portfolio_id,
            as_of_date=current_date,
        )

        actual_return = (
            current_equity / previous_equity
        ) - 1

        expected_return = (
            EXPECTED_EQUITY[current_date]
            / EXPECTED_EQUITY[previous_date]
        ) - 1

        assert actual_return == pytest.approx(
            expected_return,
            abs=1e-10,
        )




def test_golden_portfolio_equity_curve(golden_db):
    connection, instruments, portfolio_id = golden_db

    curve = get_portfolio_equity_curve(
        connection,
        portfolio_id,
        start_date="2026-01-05",
        end_date="2026-01-09",
    )

    assert len(curve) == 5

    for _, row in curve.iterrows():
        date = str(row["date"])[:10]

        assert row["cash_balance"] == pytest.approx(
            EXPECTED_CASH[date],
            abs=1e-10,
        )

        assert row["total_equity"] == pytest.approx(
            EXPECTED_EQUITY[date],
            abs=1e-10,
        )

        assert row["holdings_value"] == pytest.approx(
            EXPECTED_EQUITY[date] - EXPECTED_CASH[date],
            abs=1e-10,
        )


def test_golden_portfolio_cumulative_return(golden_db):
    connection, instruments, portfolio_id = golden_db

    curve = get_portfolio_equity_curve(
        connection,
        portfolio_id,
        start_date="2026-01-05",
        end_date="2026-01-09",
    )

    initial_equity = float(curve.iloc[0]["total_equity"])
    final_equity = float(curve.iloc[-1]["total_equity"])

    cumulative_return = final_equity / initial_equity - 1

    assert cumulative_return == pytest.approx(
        0.0146,
        abs=1e-10,
    )




def test_golden_portfolio_subperiod_return(golden_db):
    connection, instruments, portfolio_id = golden_db

    actual_return = get_portfolio_subperiod_return(
        connection,
        portfolio_id,
        previous_date="2026-01-06",
        current_date="2026-01-07",
    )

    expected_return = (20205.00 / 20140.00) - 1

    assert actual_return == pytest.approx(
        expected_return,
        abs=1e-10,
    )


def test_golden_portfolio_time_weighted_return(golden_db):
    connection, instruments, portfolio_id = golden_db

    actual_return = get_time_weighted_return(
        connection,
        portfolio_id,
        start_date="2026-01-06",
        end_date="2026-01-09",
    )

    expected_return = (20292.00 / 20140.00) - 1

    assert actual_return == pytest.approx(
        expected_return,
        abs=1e-10,
    )


def test_golden_portfolio_cash_flow_adjusted_return(golden_db):
    connection, instruments, portfolio_id = golden_db

    actual_return = get_cash_flow_adjusted_return(
        connection,
        portfolio_id,
        start_date="2026-01-06",
        end_date="2026-01-09",
    )

    expected_return = (20292.00 / 20140.00) - 1

    assert actual_return == pytest.approx(
        expected_return,
        abs=1e-10,
    )



@pytest.fixture
def cash_flow_golden_db(golden_db):
    connection, instruments, _ = golden_db

    instrument_id = add_instrument(
        connection,
        ticker="TESTCF",
        name="Cash Flow Test Stock",
        asset_type="stock",
        currency="USD",
        sector="Technology",
    )

    connection.execute(
        """
        UPDATE instruments
        SET primary_source = 'test'
        WHERE id = ?
        """,
        (instrument_id,),
    )

    prices = {
        "2026-01-05": 100.00,
        "2026-01-06": 105.00,
        "2026-01-07": 105.00,
        "2026-01-08": 107.10,
    }

    for date, price in prices.items():
        add_price(
            connection,
            instrument_id=instrument_id,
            date=date,
            open_price=price,
            high=price,
            low=price,
            close=price,
            adjusted_close=price,
            volume=1000,
            source="test",
            fetched_at="2026-01-10T00:00:00",
        )

    portfolio_id = add_portfolio(
        connection,
        name="Golden Cash Flow Portfolio",
        currency="USD",
    )

    for date, amount in [
        ("2026-01-05", 10000.00),
        ("2026-01-07", 5250.00),
    ]:
        add_cash_flow(
            connection,
            portfolio_id=portfolio_id,
            date=date,
            flow_type="DEPOSIT",
            amount=amount,
            currency="USD",
            source="test",
        )

    for date, quantity, price in [
        ("2026-01-05", 100, 100.00),
        ("2026-01-07", 50, 105.00),
    ]:
        add_transaction(
            connection,
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            date=date,
            transaction_type="BUY",
            quantity=quantity,
            price=price,
            currency="USD",
            source="test",
        )

    connection.commit()

    return connection, portfolio_id




def test_golden_intermediate_deposit_equity(cash_flow_golden_db):
    connection, portfolio_id = cash_flow_golden_db

    expected_equity = {
        "2026-01-05": 10000.00,
        "2026-01-06": 10500.00,
        "2026-01-07": 15750.00,
        "2026-01-08": 16065.00,
    }

    for date, expected in expected_equity.items():
        actual = get_total_portfolio_equity(
            connection,
            portfolio_id,
            as_of_date=date,
        )

        assert actual == pytest.approx(
            expected,
            abs=1e-10,
        )


def test_golden_intermediate_deposit_twr(cash_flow_golden_db):
    connection, portfolio_id = cash_flow_golden_db

    actual = get_time_weighted_return(
        connection,
        portfolio_id,
        start_date="2026-01-05",
        end_date="2026-01-08",
    )

    assert actual == pytest.approx(
        0.071,
        abs=1e-10,
    )


def test_golden_intermediate_deposit_adjusted_return(
    cash_flow_golden_db,
):
    connection, portfolio_id = cash_flow_golden_db

    actual = get_cash_flow_adjusted_return(
        connection,
        portfolio_id,
        start_date="2026-01-05",
        end_date="2026-01-08",
    )

    assert actual == pytest.approx(
        0.0815,
        abs=1e-10,
    )




def test_golden_portfolio_risk_metrics(golden_db):
    connection, instruments, portfolio_id = golden_db

    curve = get_portfolio_equity_curve(
        connection,
        portfolio_id,
        start_date="2026-01-05",
        end_date="2026-01-09",
    )

    actual_returns = (
        curve.set_index("date")["total_equity"]
        .astype(float)
        .pct_change()
        .dropna()
    )

    expected_equity = pd.Series(
        [20000.0, 20140.0, 20205.0, 20238.0, 20292.0]
    )

    expected_returns = expected_equity.pct_change().dropna()

    assert actual_returns.to_numpy() == pytest.approx(
        expected_returns.to_numpy(),
        abs=1e-12,
    )

    assert annualized_volatility(
        actual_returns
    ) == pytest.approx(
        annualized_volatility(expected_returns),
        abs=1e-12,
    )

    assert maximum_drawdown(actual_returns) == pytest.approx(
        0.0,
        abs=1e-12,
    )


def test_golden_portfolio_historical_risk(golden_db):
    connection, instruments, portfolio_id = golden_db

    curve = get_portfolio_equity_curve(
        connection,
        portfolio_id,
        start_date="2026-01-05",
        end_date="2026-01-09",
    )

    returns = (
        curve.set_index("date")["total_equity"]
        .astype(float)
        .pct_change()
        .dropna()
    )

    expected_returns = pd.Series([
        20140 / 20000 - 1,
        20205 / 20140 - 1,
        20238 / 20205 - 1,
        20292 / 20238 - 1,
    ])

    assert historical_var(
        returns,
        confidence=0.95,
    ) == pytest.approx(
        historical_var(expected_returns, confidence=0.95),
        abs=1e-12,
    )

    assert historical_cvar(
        returns,
        confidence=0.95,
    ) == pytest.approx(
        historical_cvar(expected_returns, confidence=0.95),
        abs=1e-12,
    )