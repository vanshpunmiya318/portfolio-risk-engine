
from pathlib import Path
import subprocess
import sys

import os
from pathlib import Path
import subprocess
import sys

from riskengine.risk.attribution import calculate_performance_attribution

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from riskengine.risk.holdings import load_portfolio_holdings
from riskengine.risk.portfolio_returns import get_portfolio_daily_returns
from riskengine.risk.dashboard_metrics import calculate_live_risk_metrics
from riskengine.db.connection import get_connection

from riskengine.db.portfolios import (
    get_total_portfolio_equity,
    get_portfolio_equity_curve,
)
from riskengine.db.transactions import get_net_cash_flows

from riskengine.db.transactions import (
    add_transaction,
    get_positions,
    get_cash_balance,
    validate_transactions,
    validate_cash_balances,
)

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
DEMO_MODE = os.environ.get("RISKENGINE_DEMO", "0") == "1"

DATABASE = (
    ROOT / "data" / "demo_portfolio.db"
    if DEMO_MODE
    else Path(
        os.environ.get(
            "RISKENGINE_DB",
            str(ROOT / "data" / "historical_portfolio.db"),
        )
    ).expanduser().resolve()
)


if DEMO_MODE and not DATABASE.exists():
    from scripts.build_demo_db import build

    build()

st.set_page_config(
    page_title="Portfolio Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container {
        padding-top: 2rem;
        max-width: 1500px;
    }
    div[data-testid="stMetric"] {
        background: #151e2d;
        border: 1px solid #29364a;
        padding: 18px;
        border-radius: 12px;
    }
    div[data-testid="stMetricLabel"],
    div[data-testid="stMetricValue"] {
        color: #f1f5f9;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=60)
def load_live_benchmark(db_path, start_date, end_date, database_version):
    with get_connection(db_path) as connection:
        prices = pd.read_sql_query(
            """
            SELECT p.date, p.close
            FROM prices p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE i.ticker = 'SPY'
              AND p.date BETWEEN ? AND ?
            ORDER BY p.date
            """,
            connection,
            params=(start_date, end_date),
            parse_dates=["date"],
        )

    if prices.empty:
        raise ValueError("No SPY benchmark prices found")

    return prices


# Historical portfolio calculations use SQLite as the source of truth.
# The live portfolio equity curve is loaded after the active portfolio
# and latest available market date have been identified.

@st.cache_data(ttl=60)
def load_live_portfolio_curve(
    db_path, portfolio_id, start_date, end_date, database_version
):
    with get_connection(db_path) as connection:
        return get_portfolio_equity_curve(
            connection,
            portfolio_id,
            start_date,
            end_date,
            max_age_days=0,
        )


INITIAL_CAPITAL = 100_000.00
PORTFOLIO_START_DATE = "2024-01-02"
HOLDINGS = pd.DataFrame(
    [
        {"Ticker": "SPY", "Exposure": "US Large-Cap", "Initial Weight": 30},
        {"Ticker": "QQQ", "Exposure": "US Growth", "Initial Weight": 20},
        {"Ticker": "IWM", "Exposure": "US Small-Cap", "Initial Weight": 15},
        {"Ticker": "GLD", "Exposure": "Gold", "Initial Weight": 15},
        {"Ticker": "TLT", "Exposure": "US Treasuries", "Initial Weight": 10},
        {"Ticker": "XLE", "Exposure": "Energy", "Initial Weight": 10},
    ]
)

st.sidebar.title("Portfolio Intelligence")
st.sidebar.caption("Historical ETF Portfolio Analytics")
st.sidebar.divider()

page = st.sidebar.radio(
    "Navigation",
    [
    "Overview",
    "Holdings",
    "Transactions",
    "Performance",
    "Risk Analytics",
    "Portfolio Allocation",
    "Reports",
    ],
)

st.sidebar.divider()
st.sidebar.caption("Historical analysis | 2024–2026") 
st.sidebar.caption("Demonstration portfolio · Not live trading")


# Update market data from the existing incremental updater.
with st.sidebar.expander("Market Data", expanded=True):
    if DATABASE.is_file():
        with get_connection(DATABASE, read_only=DEMO_MODE) as status_connection:
            latest_by_ticker = pd.read_sql_query(
                """
                SELECT i.ticker, MAX(p.date) AS latest_date
                FROM instruments AS i
                LEFT JOIN prices AS p ON p.instrument_id = i.id
                WHERE i.ticker IN ('SPY', 'QQQ', 'IWM', 'GLD', 'TLT', 'XLE')
                GROUP BY i.ticker
                ORDER BY i.ticker
                """,
                status_connection,
            )
        valid_dates = latest_by_ticker["latest_date"].dropna()
        if not valid_dates.empty:
            # Show the oldest latest-date: the date covered by every ETF.
            common_date = valid_dates.min()
            st.caption(f"All-ETF price coverage through: {common_date}")
            with st.popover("ETF price dates"):
                st.dataframe(latest_by_ticker, hide_index=True)
        else:
            st.warning("No market prices found.")
    else:
        st.error("Market database not found.")

    updater_script = ROOT / "scripts" / "update_market_prices.py"
    if st.button(
        "Update Market Data",
        key="update_market_data",
        disabled=DEMO_MODE or not (DATABASE.is_file() and updater_script.is_file()),
        use_container_width=True,
    ):
        with st.spinner("Downloading and validating ETF prices..."):
            try:
                result = subprocess.run(
                    [sys.executable, str(updater_script), "--db", str(DATABASE)],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                st.error("Update timed out. Check the database before retrying.")
            except OSError as error:
                st.error(f"Could not start the updater: {error}")
            else:
                output = "\n".join(filter(None, [result.stdout, result.stderr]))
                if result.returncode == 0:
                    st.success("Market-data updater finished successfully.")
                    if output:
                        st.code(output, language="text")
                    st.cache_data.clear()
                    st.info("Price data refreshed. Click another page or rerun the app to see updated figures.")
                else:
                    st.error(f"Market-data update failed (exit code {result.returncode}).")
                    if output:
                        st.code(output, language="text")
    if not updater_script.is_file():
        st.caption("Missing scripts/update_market_prices.py")


with get_connection(DATABASE, read_only=DEMO_MODE) as connection:
    portfolio_row = connection.execute(
        "SELECT id FROM portfolios ORDER BY id LIMIT 1"
    ).fetchone()

    if portfolio_row is None:
        st.error("No portfolio found in the database.")
        st.stop()

    active_portfolio_id = int(portfolio_row[0])

    latest_date = connection.execute(
        "SELECT MAX(date) FROM prices"
    ).fetchone()[0]

    if latest_date is None:
        st.error("No market prices found.")
        st.stop()

    portfolio_value = float(
        get_total_portfolio_equity(
            connection,
            active_portfolio_id,
            latest_date,
        )
    )

    net_contributions = float(
        get_net_cash_flows(
            connection,
            active_portfolio_id,
            latest_date,
        )
    )

profit = portfolio_value - net_contributions

total_return = (
    profit / net_contributions * 100
    if net_contributions > 0
    else 0.0
)

benchmark_prices = load_live_benchmark(
    str(DATABASE),
    PORTFOLIO_START_DATE,
    latest_date,
    DATABASE.stat().st_mtime_ns,
)

# Match the original benchmark's starting investment date.
benchmark_start_date = PORTFOLIO_START_DATE

with get_connection(DATABASE, read_only=DEMO_MODE) as connection:
    benchmark_start_price = connection.execute(
        """
        SELECT p.close
        FROM prices p
        JOIN instruments i ON i.id = p.instrument_id
        WHERE i.ticker = 'SPY'
          AND p.date = ?
        """,
        (benchmark_start_date,),
    ).fetchone()

if benchmark_start_price is None:
    raise ValueError(
        f"Missing SPY starting price for {benchmark_start_date}"
    )

benchmark_prices["benchmark"] = (
    INITIAL_CAPITAL
    * benchmark_prices["close"]
    / float(benchmark_start_price[0])
)

benchmark_value = float(benchmark_prices["benchmark"].iloc[-1])
benchmark_return = (
    benchmark_value / INITIAL_CAPITAL - 1
) * 100

if page in {"Overview", "Performance", "Risk Analytics"}:
    live_curve = load_live_portfolio_curve(
    str(DATABASE),
    active_portfolio_id,
    PORTFOLIO_START_DATE,
    latest_date,
    DATABASE.stat().st_mtime_ns,
    )

    live_curve = live_curve.rename(
        columns={"total_equity": "portfolio"}
    )


    data = live_curve[["date", "portfolio"]].merge(
        benchmark_prices[["date", "benchmark"]],
        on="date",
        how="left",
    ).sort_values("date").reset_index(drop=True)


@st.cache_data(ttl=60)
def load_live_daily_returns(
    db_path,
    portfolio_id,
    dates,
    database_version,
):
    with get_connection(db_path) as connection:
        return get_portfolio_daily_returns(
            connection=connection,
            portfolio_id=portfolio_id,
            dates=pd.DatetimeIndex(pd.to_datetime(dates)),
            max_age_days=0,
        )



if page in {"Overview", "Performance", "Risk Analytics"}:
    portfolio_daily_returns = load_live_daily_returns(
        str(DATABASE),
        active_portfolio_id,
        tuple(data["date"].dt.strftime("%Y-%m-%d")),
        DATABASE.stat().st_mtime_ns,
    )

    live_risk = calculate_live_risk_metrics(
    data,
    portfolio_daily_returns,
    )

def performance_chart(frame, normalize=False):
    fig = go.Figure()

    if normalize:
        first = frame.iloc[0]
        portfolio = frame["portfolio"] / first["portfolio"] * 100
        benchmark = frame["benchmark"] / first["benchmark"] * 100
        axis_title = "Indexed Value (Start = 100)"
    else:
        portfolio = frame["portfolio"]
        benchmark = frame["benchmark"]
        axis_title = "Portfolio Value (USD)"

    fig.add_trace(
        go.Scatter(
            x=frame["date"],
            y=portfolio,
            name="Portfolio",
            mode="lines",
            line=dict(color="#38bdf8", width=2.5),
        )
    )

    fig.add_trace(
        go.Scatter(
            x=frame["date"],
            y=benchmark,
            name="SPY Benchmark",
            mode="lines",
            line=dict(color="#f59e0b", width=2),
        )
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0e1624",
        plot_bgcolor="#0e1624",
        height=440,
        margin=dict(l=20, r=20, t=25, b=20),
        hovermode="x unified",
        xaxis_title=None,
        yaxis_title=axis_title,
        legend=dict(orientation="h", y=1.12),
    )

    return fig


if page == "Overview":
    st.title("Portfolio Overview")
    st.caption("Historical portfolio performance and risk intelligence")

    a, b, c, d = st.columns(4)

    a.metric("Portfolio Value", f"${portfolio_value:,.2f}")
    b.metric("Total Profit / Loss", f"${profit:,.2f}")
    c.metric("Cumulative Return", f"{total_return:.2f}%")
    d.metric("SPY Benchmark Return", f"{benchmark_return:.2f}%")

    st.subheader("Portfolio Growth vs SPY")
    st.plotly_chart(
        performance_chart(data),
        use_container_width=True,
    )

    st.subheader("Risk Snapshot")
    a, b, c, d = st.columns(4)

    a.metric("Annualized Volatility", f"{live_risk['volatility']:.2f}%")
    b.metric("Maximum Drawdown", f"{live_risk['drawdown']:.2f}%")
    c.metric("Sharpe Ratio", f"{live_risk['sharpe']:.2f}")
    d.metric("Portfolio Beta", f"{live_risk['beta']:.2f}")


    st.info(
        "This is a historical demonstration portfolio. "
        "Performance uses raw closing prices and excludes "
        "dividend-inclusive total return adjustments."
    )



elif page == "Holdings":
    st.title("Portfolio Holdings")
    st.caption("Actual positions and valuations from the historical SQLite database")

    if not DATABASE.exists():
        st.error("Historical portfolio database not found.")
        st.stop()

    with get_connection(DATABASE, read_only=DEMO_MODE) as connection:
        portfolios = pd.read_sql_query(
            "SELECT id, name FROM portfolios ORDER BY name",
            connection,
        )

    selected_name = st.selectbox(
        "Select Portfolio",
        portfolios["name"].tolist(),
    )

    selected_id = int(
        portfolios.loc[
            portfolios["name"] == selected_name, "id"
        ].iloc[0]
    )

    holdings = load_portfolio_holdings(DATABASE, selected_id)

    if holdings.empty:
        st.warning("No open positions found.")
        st.stop()

    invested_value = holdings["market_value"].sum()
    net_invested = holdings["net_invested"].sum()
    net_position_gain = invested_value - net_invested

    a, b, c = st.columns(3)
    a.metric("Invested Holdings Value", f"${invested_value:,.2f}")
    b.metric("Net Transaction Investment", f"${net_invested:,.2f}")
    c.metric("Value Minus Net Investment", f"${net_position_gain:,.2f}")

    display = holdings[
        [
            "ticker",
            "name",
            "shares",
            "latest_price",
            "market_value",
            "weight_pct",
            "net_invested",
            "unrealized_pl",
        ]
    ].rename(
        columns={
            "ticker": "Ticker",
            "name": "Name",
            "shares": "Shares",
            "latest_price": "Latest Price",
            "market_value": "Market Value",
            "weight_pct": "Weight (%)",
            "net_invested": "Net Invested",
            "unrealized_pl": "Value Minus Net Invested",
        }
    )

    st.dataframe(
        display,
        hide_index=True,
        width="stretch",
        column_config={
            "Latest Price": st.column_config.NumberColumn(format="$%.2f"),
            "Market Value": st.column_config.NumberColumn(format="$%.2f"),
            "Weight (%)": st.column_config.NumberColumn(format="%.2f%%"),
            "Net Invested": st.column_config.NumberColumn(format="$%.2f"),
            "Value Minus Net Invested": st.column_config.NumberColumn(
                format="$%.2f"
            ),
        },
    )

    st.subheader("Current Holdings Allocation")

    fig = go.Figure(
        go.Pie(
            labels=holdings["ticker"],
            values=holdings["market_value"],
            hole=0.55,
            textinfo="label+percent",
        )
    )

    fig.update_layout(
        template="plotly_dark",
        height=430,
    )

    st.plotly_chart(fig, width="stretch")

    st.caption(
        "Read-only historical holdings. Values exclude portfolio cash. "
        "Value minus net investment is not tax-lot unrealized P/L."
    )



elif page == "Transactions":
    st.title("Transaction Management")
    st.caption("Review and record portfolio trades")

    if not DATABASE.exists():
        st.error("Portfolio database not found.")
        st.stop()

    with get_connection(DATABASE, read_only=DEMO_MODE) as connection:
        portfolios = pd.read_sql_query(
            "SELECT id, name FROM portfolios ORDER BY name",
            connection,
        )

    if portfolios.empty:
        st.warning("No portfolios found.")
        st.stop()

    selected_name = st.selectbox(
        "Select Portfolio",
        portfolios["name"].tolist(),
        key="transactions_portfolio",
    )

    portfolio_id = int(
        portfolios.loc[
            portfolios["name"] == selected_name, "id"
        ].iloc[0]
    )

    with get_connection(DATABASE, read_only=DEMO_MODE) as connection:
        transactions = pd.read_sql_query(
            """
            SELECT
                t.date AS Date,
                i.ticker AS Ticker,
                t.transaction_type AS Type,
                t.quantity AS Shares,
                t.price AS Price,
                t.quantity * t.price AS Amount
            FROM transactions t
            JOIN instruments i
                ON i.id = t.instrument_id
            WHERE t.portfolio_id = ?
            ORDER BY t.date DESC, t.id DESC
            """,
            connection,
            params=(portfolio_id,),
        )

        instruments = pd.read_sql_query(
            """
            SELECT id, ticker
            FROM instruments
            ORDER BY ticker
            """,
            connection,
        )

        latest_date = connection.execute(
            "SELECT MAX(date) FROM prices"
        ).fetchone()[0]

        first_funding_date = connection.execute(
            "SELECT MIN(date) FROM cash_flows WHERE portfolio_id = ?",
            (portfolio_id,),
        ).fetchone()[0]

        cash_balance = (
            get_cash_balance(connection, portfolio_id, latest_date)
            if latest_date else 0.0
        )

    a, b, c = st.columns(3)
    a.metric("Total Transactions", len(transactions))
    b.metric("Available Cash", f"${cash_balance:,.2f}")
    c.metric("Latest Price Date", latest_date or "N/A")

    st.subheader("Transaction History")

    st.dataframe(
        transactions,
        hide_index=True,
        width="stretch",
        column_config={
            "Price": st.column_config.NumberColumn(format="$%.2f"),
            "Amount": st.column_config.NumberColumn(format="$%.2f"),
        },
    )

    st.divider()
    st.subheader("Add Transaction")

    st.info(
        "This form records trades in the demonstration database. "
        "It does not place real market orders. "
        "Historical reports are not automatically regenerated."
    )

    if latest_date is None:
        st.warning("Price data is required before recording trades.")
        st.stop()

    with st.form(
    "add_transaction_form",
    clear_on_submit=False,
    enter_to_submit=False,
        ):
        ticker = st.selectbox(
            "Instrument",
            instruments["ticker"].tolist(),
        )

        transaction_type = st.radio(
            "Transaction Type",
            ["BUY", "SELL"],
            horizontal=True,
        )

        trade_date = st.date_input(
            "Trade Date",
            value=pd.Timestamp(latest_date).date(),
            min_value=pd.Timestamp(first_funding_date).date(),
            max_value=pd.Timestamp(latest_date).date(),
            help="Choose a historical market date after the portfolio was funded.",
        )

        quantity = st.number_input(
            "Shares",
            min_value=1,
            step=1,
            value=1,
        )

        price = st.number_input(
            "Execution Price (USD)",
            min_value=0.01,
            value=100.00,
            step=0.01,
            format="%.2f",
        )

        trade_value = quantity * price
        st.write(f"**Trade Value: ${trade_value:,.2f}**")

        submitted = st.form_submit_button(
            "Record Transaction",
            type="primary",
            disabled=DEMO_MODE,
        )

    if submitted and not DEMO_MODE:
        instrument_id = int(
            instruments.loc[
                instruments["ticker"] == ticker, "id"
            ].iloc[0]
        )

        try:
            with get_connection(DATABASE, read_only=DEMO_MODE) as connection:
                price_exists = connection.execute(
                    """
                    SELECT 1 FROM prices
                    WHERE instrument_id = ? AND date = ?
                    LIMIT 1
                    """,
                    (instrument_id, trade_date.isoformat()),
                ).fetchone()
                if price_exists is None:
                    raise ValueError(
                        f"No {ticker} closing price for {trade_date}. "
                        "Choose a date with market data."
                    )

                positions = get_positions(
                    connection,
                    portfolio_id,
                    trade_date.isoformat(),
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
                    trade_date.isoformat(),
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
                        trade_date.isoformat(),
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

            st.success(
                f"Recorded {transaction_type} "
                f"{quantity} {ticker} shares "
                f"at ${price:,.2f}."
            )
            st.rerun()

        except (ValueError, Exception) as error:
            st.error(f"Transaction not recorded: {error}")


elif page == "Performance":
    st.title("Performance Analytics")
    st.caption(
        "Date-range returns are calculated from cash-flow-adjusted daily "
        "portfolio returns and SPY closing-price returns."
    )

    date_range = st.date_input(
        "Select Date Range",
        value=(data["date"].min().date(), data["date"].max().date()),
        min_value=data["date"].min().date(),
        max_value=data["date"].max().date(),
    )

    if len(date_range) != 2:
        st.info("Select both a start and end date to calculate performance.")
        st.stop()

    start, end = date_range
    filtered = data.loc[
        (data["date"].dt.date >= start)
        & (data["date"].dt.date <= end)
    ].copy().sort_values("date")

    if len(filtered) < 3:
        st.warning("Choose a date range containing at least three trading-day observations.")
        st.stop()

    # Returns are indexed by their END date. Exclude the first selected
    # observation: it is the baseline, not a return earned within the period.
    first_date = pd.Timestamp(filtered["date"].iloc[0])
    last_date = pd.Timestamp(filtered["date"].iloc[-1])
    benchmark_daily_returns = pd.Series(
        data["benchmark"].to_numpy(dtype=float),
        index=pd.DatetimeIndex(data["date"]),
        name="SPY",
    ).pct_change().dropna()
    portfolio_returns = pd.Series(
        portfolio_daily_returns, dtype=float, name="Portfolio"
    ).sort_index()

    aligned_returns = pd.concat(
        [portfolio_returns, benchmark_daily_returns], axis=1, join="inner"
    ).dropna()
    aligned_returns = aligned_returns.loc[
        (aligned_returns.index > first_date)
        & (aligned_returns.index <= last_date)
    ].sort_index()

    if len(aligned_returns) < 2:
        st.warning("Not enough aligned trading-day returns for this date range.")
        st.stop()

    # Linked returns remove the effect of external deposits and withdrawals.
    linked_growth = (1.0 + aligned_returns).cumprod()
    baseline = pd.DataFrame(
        {"Portfolio": [1.0], "SPY": [1.0]},
        index=pd.DatetimeIndex([first_date]),
    )
    linked_growth = pd.concat([baseline, linked_growth]).sort_index()

    indexed = st.toggle("Normalize selected period to 100", value=True)
    if indexed:
        chart_frame = pd.DataFrame({
            "date": linked_growth.index,
            "portfolio": linked_growth["Portfolio"].to_numpy() * 100.0,
            "benchmark": linked_growth["SPY"].to_numpy() * 100.0,
        })
        chart_title = "Cash-flow-adjusted growth (start = 100)"
    else:
        # Dollar growth of an equal starting investment, NOT account equity.
        starting_value = float(filtered["portfolio"].iloc[0])
        chart_frame = pd.DataFrame({
            "date": linked_growth.index,
            "portfolio": linked_growth["Portfolio"].to_numpy() * starting_value,
            "benchmark": linked_growth["SPY"].to_numpy() * starting_value,
        })
        chart_title = "Equal-starting-value growth (USD)"

    st.subheader(chart_title)
    st.plotly_chart(performance_chart(chart_frame), width="stretch")
    st.caption(
        "Both lines start at the same value. The portfolio line links "
        "cash-flow-adjusted returns; the SPY line links price returns. "
        "The dollar view is hypothetical growth, not the actual account balance."
    )

    st.subheader("Selected-Period Performance Comparison")

    def performance_statistics(daily_returns):
        daily_returns = daily_returns.astype(float)
        growth = pd.concat(
            [pd.Series([1.0]), (1.0 + daily_returns).cumprod().reset_index(drop=True)],
            ignore_index=True,
        )
        cumulative = float(growth.iloc[-1] - 1.0)
        annualized = (
            (1.0 + cumulative) ** (252.0 / len(daily_returns)) - 1.0
            if cumulative > -1.0 else -1.0
        )
        volatility = float(daily_returns.std(ddof=1) * (252.0 ** 0.5))
        maximum_drawdown = float(-(growth / growth.cummax() - 1.0).min())
        daily_std = float(daily_returns.std(ddof=1))
        sharpe = (
            float(daily_returns.mean() / daily_std * (252.0 ** 0.5))
            if daily_std > 0 else float("nan")
        )
        return [
            f"{cumulative * 100:.2f}%",
            f"{annualized * 100:.2f}%",
            f"{volatility * 100:.2f}%",
            f"{maximum_drawdown * 100:.2f}%",
            f"{sharpe:.2f}",
        ]

    comparison = pd.DataFrame({
        "Metric": [
            "Cumulative Return", "Annualized Return", "Annualized Volatility",
            "Maximum Drawdown", "Sharpe Ratio",
        ],
        "Portfolio": performance_statistics(aligned_returns["Portfolio"]),
        "SPY": performance_statistics(aligned_returns["SPY"]),
    })
    st.dataframe(comparison, hide_index=True, width="stretch")
    st.caption(
        f"Measured from {first_date:%d %b %Y} through {last_date:%d %b %Y} "
        f"({len(aligned_returns)} aligned daily returns). "
        "Annualization uses 252 trading days; Sharpe assumes a 0% risk-free rate. "
        "The first selected observation is the starting baseline."
    )

elif page == "Risk Analytics":
    st.title("Risk Analytics")
    st.caption("Historical risk measures based on cash-flow-adjusted portfolio returns")

    a, b, c = st.columns(3)
    a.metric("Annualized Volatility", f"{live_risk['volatility']:.2f}%")
    b.metric("Maximum Drawdown", f"{live_risk['drawdown']:.2f}%")
    c.metric("Sharpe Ratio", f"{live_risk['sharpe']:.2f}")

    a, b, c = st.columns(3)
    a.metric("Historical VaR (95%)", f"{live_risk['var']:.2f}%")
    b.metric("Historical CVaR (95%)", f"{live_risk['cvar']:.2f}%")
    c.metric("Beta vs SPY", f"{live_risk['beta']:.2f}")

    st.subheader("Historical Drawdown Comparison")

    portfolio_growth = (1 + portfolio_daily_returns.sort_index()).cumprod()
    portfolio_growth = pd.concat([
        pd.Series([1.0], index=[pd.Timestamp(data["date"].iloc[0])]),
        portfolio_growth,
    ]).sort_index()
    portfolio_drawdown = (portfolio_growth / portfolio_growth.cummax() - 1) * 100

    benchmark_growth = data.set_index("date")["benchmark"]
    benchmark_drawdown = (benchmark_growth / benchmark_growth.cummax() - 1) * 100

    drawdowns = pd.DataFrame({
        "date": data["date"],
        "Portfolio": portfolio_drawdown.reindex(data["date"]).to_numpy(),
        "SPY": benchmark_drawdown.reindex(data["date"]).to_numpy(),
    })

    fig = go.Figure()
    for column, color in [("Portfolio", "#38bdf8"), ("SPY", "#f59e0b")]:
        fig.add_trace(go.Scatter(
            x=drawdowns["date"],
            y=drawdowns[column],
            name=column,
            mode="lines",
            line=dict(color=color, width=2),
        ))

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0e1624",
        plot_bgcolor="#0e1624",
        height=430,
        yaxis_title="Drawdown (%)",
        hovermode="x unified",
    )
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "Portfolio drawdowns use cash-flow-adjusted investment returns. "
        "SPY drawdowns use historical closing prices."
    )

elif page == "Portfolio Allocation":
    st.title("Portfolio Allocation")
    st.caption(
        "Compare original target allocations with current "
        "split-adjusted portfolio holdings."
    )

    current_holdings = load_portfolio_holdings(DATABASE, active_portfolio_id)
    if current_holdings.empty:
        st.warning("No active holdings found.")
        st.stop()

    targets = HOLDINGS[["Ticker", "Exposure", "Initial Weight"]].copy()
    actual = current_holdings[["ticker", "weight_pct", "market_value"]].rename(
        columns={
            "ticker": "Ticker",
            "weight_pct": "Current Weight",
            "market_value": "Market Value",
        }
    )
    allocation = targets.merge(actual, on="Ticker", how="outer")
    allocation["Initial Weight"] = allocation["Initial Weight"].fillna(0)
    allocation["Current Weight"] = allocation["Current Weight"].fillna(0)
    allocation["Market Value"] = allocation["Market Value"].fillna(0)
    allocation["Drift (pp)"] = (
        allocation["Current Weight"] - allocation["Initial Weight"]
    )

    st.subheader("Target vs Current Allocation")
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=allocation["Ticker"], y=allocation["Initial Weight"],
        name="Initial Target", marker_color="#64748b",
    ))
    fig.add_trace(go.Bar(
        x=allocation["Ticker"], y=allocation["Current Weight"],
        name="Current Weight", marker_color="#38bdf8",
    ))
    fig.update_layout(
        barmode="group",
        template="plotly_dark",
        paper_bgcolor="#0e1624",
        plot_bgcolor="#0e1624",
        height=430,
        xaxis_title="ETF",
        yaxis_title="Portfolio Weight (%)",
        legend=dict(orientation="h", y=1.12),
        margin=dict(l=20, r=20, t=40, b=20),
    )
    st.plotly_chart(fig, width="stretch")

    st.subheader("Allocation Breakdown")
    st.dataframe(
        allocation[[
            "Ticker", "Exposure", "Initial Weight", "Current Weight",
            "Drift (pp)", "Market Value",
        ]],
        hide_index=True,
        width="stretch",
        column_config={
            "Initial Weight": st.column_config.NumberColumn(format="%.2f%%"),
            "Current Weight": st.column_config.NumberColumn(format="%.2f%%"),
            "Drift (pp)": st.column_config.NumberColumn(format="%+.2f"),
            "Market Value": st.column_config.NumberColumn(format="$%.2f"),
        },
    )

    st.subheader("Current Portfolio Composition")
    fig = go.Figure(go.Pie(
        labels=current_holdings["ticker"],
        values=current_holdings["market_value"],
        hole=0.58,
        textinfo="label+percent",
    ))
    fig.update_layout(template="plotly_dark", height=400)
    st.plotly_chart(fig, width="stretch")

    st.subheader("Historical Allocation Drift")
    drift_path = REPORTS / "allocation_drift.png"
    if drift_path.exists():
        st.image(str(drift_path))
    st.caption(
        "Current weights use the latest database valuations. "
        "Weights exclude uninvested cash. Historical allocation "
        "drift is a previously generated report snapshot."
    )

elif page == "Reports":
    st.title("Reports & Research")
    st.write(
        "Explore the historical portfolio risk report, "
        "benchmark comparison, and performance attribution."
    )

    with get_connection(DATABASE, read_only=DEMO_MODE) as connection:
        attribution = calculate_performance_attribution(
            connection,
            active_portfolio_id,
            latest_date,
        )

    report_lines = [
        "# Portfolio Performance Attribution Report",
        "",
        f"**Portfolio ID:** {active_portfolio_id}",
        f"**As-of date:** {latest_date}",
        "",
        "## Realized and Unrealized Price Gains",
        "",
        "| ETF | Realized (USD) | Unrealized (USD) | Total (USD) |",
        "|---|---:|---:|---:|",
    ]

    for _, item in attribution.iterrows():
        report_lines.append(
            f"| {item['ticker']} "
            f"| {item['realized_gain_usd']:,.2f} "
            f"| {item['unrealized_gain_usd']:,.2f} "
            f"| {item['gain_usd']:,.2f} |"
        )

    report_lines.extend([
        "",
        f"**Total price gain/loss: ${attribution['gain_usd'].sum():,.2f}**",
        "",
        "Method: Average-cost accounting. Includes realized and "
        "unrealized price gains. Excludes dividends, fees, taxes "
        "and currency effects.",
    ])

    st.download_button(
        "Download Performance Attribution Report",
        data="\n".join(report_lines),
        file_name="performance_attribution_report.md",
        mime="text/markdown",
    )

    st.subheader("Performance Attribution")


    if attribution.empty:
        st.info("No transactions available for attribution.")
    else:
        fig = go.Figure()

        fig.add_trace(
            go.Bar(
                x=attribution["ticker"],
                y=attribution["realized_gain_usd"],
                name="Realized Gain/Loss",
            )
        )

        fig.add_trace(
            go.Bar(
                x=attribution["ticker"],
                y=attribution["unrealized_gain_usd"],
                name="Unrealized Gain/Loss",
            )
        )

        fig.update_layout(
            template="plotly_dark",
            barmode="relative",
            height=430,
            xaxis_title="ETF",
            yaxis_title="Gain / Loss (USD)",
        )

        st.plotly_chart(fig, use_container_width=True)

        st.dataframe(
            attribution[
                [
                    "ticker",
                    "realized_gain_usd",
                    "unrealized_gain_usd",
                    "gain_usd",
                ]
            ],
            hide_index=True,
            use_container_width=True,
        )

        st.metric(
            "Total Price Gain / Loss",
            f"${attribution['gain_usd'].sum():,.2f}",
        )

    st.caption(
        "Attribution uses average-cost accounting and includes "
        "realized and unrealized price gains. Excludes dividends, "
        "fees, taxes and currency effects."
    )

    
    st.caption(
        "Historical analysis for educational and portfolio "
        "demonstration purposes. Not investment advice."
    )
