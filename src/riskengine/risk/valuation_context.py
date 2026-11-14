"""Request-local historical records for one uninterrupted caller-owned snapshot.

No connection lifecycle operations, SQL adapters or cross-request caches. SQLite
still computes split factors and cash sums; shared legacy kernels do accounting.
"""
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import json
from math import isfinite
import sqlite3

import numpy as np
from numpy.typing import NDArray
import pandas as pd

from riskengine.db.corporate_actions import validate_coverage_intervals
from riskengine.db.instruments import normalise_ticker
from riskengine.db.portfolios import value_joined_positions, value_positions
from riskengine.db.transactions import positions_from_records, validate_transaction_records


_MAX_EXACT_JOIN_ROWS = 100_000


@dataclass(frozen=True)
class _HoldingsInputs:
    quantity: NDArray[np.float64]
    price: NDArray[np.float64]
    coverage_tickers: tuple[str, ...]


def _select_holdings_arrays(quantity, close, adjusted_close, sources):
    """One source-selection implementation for bulk and fallback preparation."""
    quantity = quantity.copy()
    price = adjusted_close.copy()
    tiingo = sources == "tiingo"
    price[tiingo] = close[tiingo]
    quantity.flags.writeable = False
    price.flags.writeable = False
    return quantity, price, tiingo


class _LazyExactJoin(Mapping):
    """Stable row ranges; legacy DataFrames are parsed only when requested."""

    def __init__(self, joined, ranges):
        self._joined = joined
        self._ranges = ranges
        self._frames = {}

    def __iter__(self):
        return iter(self._ranges)

    def __len__(self):
        return len(self._ranges)

    def __contains__(self, day):
        return day in self._ranges

    def __getitem__(self, day):
        if day not in self._frames:
            start, stop = self._ranges[day]
            frame = self._joined.iloc[start:stop].copy().reset_index(drop=True)
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
            self._frames[day] = frame
        return self._frames[day]


class ValuationContextError(RuntimeError):
    """Programming/snapshot misuse, rather than a market-data classification."""


class PreparedValuationContext:
    """Private-to-preparation state; use only inside its lexical with scope.

    All earlier transactions are retained through the inclusive final boundary.
    The connection must have no write history: total_changes cannot detect a
    rollback of changes made before preparation. Callers with writes use legacy
    readers instead. Scope ownership forbids commit/rollback/restart inside it.
    """

    def __init__(self, connection: sqlite3.Connection, portfolio_id: int, boundaries):
        if not connection.in_transaction or connection.total_changes:
            raise ValuationContextError("Context requires a fresh caller-owned read transaction")
        self._connection = connection
        self._portfolio_id = portfolio_id
        self._boundaries = tuple(day.isoformat() if isinstance(day, date) else day for day in boundaries)
        if not self._boundaries or tuple(sorted(set(self._boundaries))) != self._boundaries:
            raise ValuationContextError("Context requires ordered unique valuation boundaries")
        self._cutoff = self._boundaries[-1]
        self._active = False
        self._used = False
        self._version = connection.execute("PRAGMA data_version").fetchone()[0]
        self._transactions = tuple(connection.execute(
            "SELECT t.id,t.instrument_id,i.ticker,t.date,t.transaction_type,t.quantity,t.price,t.currency "
            "FROM transactions t JOIN instruments i ON i.id=t.instrument_id "
            "WHERE t.portfolio_id=? AND t.date<=? ORDER BY t.date,t.id",
            (portfolio_id, self._cutoff),
        ).fetchall())
        self._flows = tuple(connection.execute(
            "SELECT id,date,flow_type,amount,currency FROM cash_flows "
            "WHERE portfolio_id=? AND date<=? ORDER BY id", (portfolio_id, self._cutoff),
        ).fetchall())
        ids = sorted({row[1] for row in self._transactions})
        # JSON parameters avoid SQLite's variable limit on large portfolios.
        encoded_ids = json.dumps(ids)
        instruments = connection.execute(
            "SELECT id,ticker,currency FROM instruments WHERE id IN (SELECT value FROM json_each(?))",
            (encoded_ids,),
        ).fetchall()
        self._instruments = {row[1]: (row[0], row[2]) for row in instruments}
        self._splits = tuple(connection.execute(
            "SELECT id,instrument_id,ex_date,ratio FROM corporate_actions "
            "WHERE instrument_id IN (SELECT value FROM json_each(?)) AND action_type='SPLIT' "
            "AND ex_date<=? ORDER BY ex_date,id", (encoded_ids, self._cutoff),
        ).fetchall())
        self._coverage = tuple(connection.execute(
            "SELECT instrument_id,start_date,end_date FROM corporate_action_coverage "
            "WHERE instrument_id IN (SELECT value FROM json_each(?)) AND source='tiingo' "
            "ORDER BY start_date,end_date", (encoded_ids,),
        ).fetchall())
        price_rows = connection.execute(
            "SELECT i.ticker,p.date,p.close,p.adjusted_close,p.source FROM prices p "
            "JOIN instruments i ON i.id=p.instrument_id "
            "WHERE i.id IN (SELECT value FROM json_each(?)) AND p.date<=? ORDER BY i.ticker",
            (encoded_ids, self._cutoff),
        ).fetchall()
        self._prices = {}
        for row in price_rows:
            self._prices.setdefault(row[0], []).append(row)
        self._event_dates = tuple(sorted({r[3] for r in self._transactions} | {r[2] for r in self._splits}))
        epochs = sorted({self._epoch(day) for day in self._boundaries})
        # Identical SQLite EXP/SUM/LN predicates to get_positions(). Do not use
        # running share multiplication, products, or calendar-day inference.
        factors = connection.execute(
            "SELECT t.instrument_id,t.date,d.value,COALESCE((SELECT EXP(SUM(LN(ratio))) "
            "FROM corporate_actions WHERE instrument_id=t.instrument_id AND action_type='SPLIT' "
            "AND ex_date>t.date AND ex_date<=d.value),1.0) "
            "FROM transactions t CROSS JOIN json_each(?) d WHERE t.portfolio_id=? "
            "AND t.date<=d.value AND t.date<=? ORDER BY t.date,t.id,d.value",
            (json.dumps(epochs), portfolio_id, self._cutoff),
        ).fetchall()
        self._factors = {(instrument, day, epoch): factor for instrument, day, epoch, factor in factors}
        # Preserve two separate SQLite aggregates and their scan order. A Python
        # running balance or regrouped SUM can change floating-point results.
        sums = connection.execute(
            "SELECT d.value,(SELECT COALESCE(SUM(CASE WHEN flow_type='DEPOSIT' THEN amount "
            "WHEN flow_type='WITHDRAWAL' THEN -amount END),0) FROM cash_flows "
            "WHERE portfolio_id=? AND date<=d.value),(SELECT COALESCE(SUM(CASE "
            "WHEN transaction_type='BUY' THEN -quantity*price WHEN transaction_type='SELL' "
            "THEN quantity*price END),0) FROM transactions WHERE portfolio_id=? AND date<=d.value) "
            "FROM json_each(?) d", (portfolio_id, portfolio_id, json.dumps(self._boundaries)),
        ).fetchall()
        self._cash = {day: (external, external + trade) for day, external, trade in sums}
        self._positions = {}
        self._validation = {}
        self._exact_join = None
        self._holdings_inputs = {}

    def __enter__(self):
        if self._used:
            raise ValuationContextError("Context scope cannot be reused")
        self._used = True
        self._active = True
        try:
            self._check(self._connection, self._portfolio_id, self._cutoff)
        except Exception:
            self._active = False
            raise
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is None:
                self._check(self._connection, self._portfolio_id, self._cutoff)
                if self._connection.execute("PRAGMA data_version").fetchone()[0] != self._version:
                    raise ValuationContextError("Context read snapshot changed")
        finally:
            self._active = False
            self._positions.clear()
            self._validation.clear()
            self._exact_join = None
            self._holdings_inputs.clear()

    def _check(self, connection, portfolio_id, day):
        if (not self._active or connection is not self._connection or portfolio_id != self._portfolio_id
                or day not in self._boundaries):
            raise ValuationContextError("Context scope, portfolio or valuation boundary mismatch")
        try:
            valid = (connection.in_transaction and not connection.total_changes
                     and connection.execute("PRAGMA data_version").fetchone()[0] == self._version)
        except sqlite3.ProgrammingError:
            valid = False
        if not valid:
            raise ValuationContextError("Context read transaction is no longer valid")

    def _epoch(self, day):
        index = bisect_right(self._event_dates, day) - 1
        return self._event_dates[index] if index >= 0 else day

    def positions(self, connection, portfolio_id, day):
        self._check(connection, portfolio_id, day)
        return self._position_state(day).copy()

    def _position_state(self, day):
        epoch = self._epoch(day)
        if epoch not in self._positions:
            records = [(r[1], r[2], r[3], r[4], r[5], r[0]) for r in self._transactions if r[3] <= epoch]
            self._positions[epoch] = positions_from_records(
                records, lambda instrument, traded: self._factors[(instrument, traded, epoch)],
            )
        return self._positions[epoch]

    def validate(self, connection, portfolio_id, day):
        self._check(connection, portfolio_id, day)
        epoch = self._epoch(day)
        if epoch not in self._validation:
            records = [r for r in self._transactions if r[3] <= epoch]
            ids = {r[1] for r in records}
            transactions = pd.DataFrame(
                [(r[3], r[0], r[1], r[2], r[4], r[5]) for r in records],
                columns=["date", "id", "instrument_id", "ticker", "transaction_type", "quantity"],
            )
            splits = pd.DataFrame(
                [(r[1], r[2], r[3]) for r in self._splits if r[1] in ids and r[2] <= epoch],
                columns=["instrument_id", "ex_date", "ratio"],
            )
            try:
                validate_transaction_records(transactions, splits)
                self._validation[epoch] = None
            except ValueError as error:
                self._validation[epoch] = str(error)
        if self._validation[epoch] is not None:
            raise ValueError(self._validation[epoch])
        return True

    def cash(self, connection, portfolio_id, day):
        self._check(connection, portfolio_id, day)
        return self._cash[day]  # external contributions, then cash balance

    def _latest_prices(self, tickers, day):
        selected = []
        for ticker in sorted({normalise_ticker(symbol) for symbol in tickers}):
            eligible = [r for r in self._prices.get(ticker, ()) if r[1] <= day]
            if eligible:
                latest = max(r[1] for r in eligible)
                selected.extend(r for r in eligible if r[1] == latest)
        frame = pd.DataFrame.from_records(selected, columns=["ticker", "date", "close", "adjusted_close", "source"])
        # Same conversion as read_sql_query(parse_dates=['date']); never parse
        # unrelated historical rows, normalize raw stored dates, or deduplicate.
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        return frame

    def _validate_coverage(self, ticker, day):
        instrument = self._instruments[ticker][0]
        first_trade = min(r[3] for r in self._transactions if r[1] == instrument and r[3] <= day)
        rows = [(a, b) for ident, a, b in self._coverage if ident == instrument and b >= first_trade and a <= day]
        return validate_coverage_intervals(date.fromisoformat(first_trade), date.fromisoformat(day), rows)

    def _build_exact_join(self):
        """One stable raw-key join; unsupported dates keep the existing reader.

        Only complete canonical exact-date rows with the same float/string
        dtypes as the per-date reader participate. Do not coerce malformed
        prices, parse unrelated stored dates, rank sources, or deduplicate.
        Arithmetic and validation still run per date in the shared kernel.
        """
        self._exact_join = {}
        if any(not isinstance(row[2], str) or type(row[5]) is not float for row in self._transactions):
            # Do not eagerly reconstruct an unsupported future position state
            # that the previous per-date reader has not needed yet.
            return
        prices_by_day = {}
        requested = set(self._boundaries)
        for ticker, records in self._prices.items():
            for record in records:
                if record[1] in requested:
                    prices_by_day.setdefault(record[1], {}).setdefault(ticker, []).append(record)
        active_by_epoch = {}
        left = []
        right = []
        for day in self._boundaries:
            try:
                if date.fromisoformat(day).isoformat() != day:
                    continue
            except ValueError:
                continue
            epoch = self._epoch(day)
            if epoch not in active_by_epoch:
                active_by_epoch[epoch] = tuple(
                    (ticker, quantity) for ticker, quantity in self._position_state(day).itertuples(index=False, name=None)
                    if quantity > 0
                )
            active = active_by_epoch[epoch]
            prices = prices_by_day.get(day, {})
            if not active or any(
                normalise_ticker(ticker) != ticker or ticker not in prices or any(
                    type(row[2]) is not float or type(row[3]) is not float or not isinstance(row[4], str)
                    for row in prices[ticker]
                ) for ticker, _ in active
            ):
                continue
            if (len(left) + len(active) > _MAX_EXACT_JOIN_ROWS
                    or len(right) + sum(len(prices[ticker]) for ticker, _ in active) > _MAX_EXACT_JOIN_ROWS):
                # Bound the added request-local allocation; large windows use
                # the previous streaming per-date path with identical results.
                return
            for ticker, quantity in active:
                left.append((ticker, quantity, day, len(left)))
                for row in prices[ticker]:
                    right.append((*row, len(right)))
        if not left:
            return
        positions = pd.DataFrame.from_records(left, columns=["ticker", "quantity", "date", "_position_order"])
        prices = pd.DataFrame.from_records(right, columns=["ticker", "date", "close", "adjusted_close", "source", "_price_order"])
        joined = positions.merge(prices, on=["date", "ticker"], how="left", sort=False)
        joined = joined.sort_values(["_position_order", "_price_order"], kind="stable")
        columns = ["ticker", "quantity", "date", "close", "adjusted_close", "source"]
        joined = joined.loc[:, columns].reset_index(drop=True)
        raw_days = joined["date"].to_numpy(copy=False)
        # The explicit left ordinal groups each date contiguously, preserving
        # duplicate right rows. These boundaries never aggregate numeric data.
        starts = np.r_[0, np.flatnonzero(raw_days[1:] != raw_days[:-1]) + 1]
        stops = np.r_[starts[1:], len(raw_days)]
        ranges = {raw_days[start]: (int(start), int(stop)) for start, stop in zip(starts, stops)}
        self._exact_join = _LazyExactJoin(joined, ranges)
        self._prepare_bulk_holdings_inputs(joined, ranges)

    def _prepare_bulk_holdings_inputs(self, joined, ranges):
        if not all(joined[column].dtype == np.dtype("float64")
                   for column in ("quantity", "close", "adjusted_close")):
            return
        # A conservative common timestamp range permits a single homogeneous
        # ISO conversion. Extreme dates use the existing per-date parser below,
        # preserving its eligibility, coercion and diagnostic behavior exactly.
        days = [day for day in ranges if "1678-01-01" <= day <= "2261-12-31"]
        if not days:
            return
        parsed = pd.to_datetime(days, errors="coerce").to_numpy()
        quantity, price, tiingo = _select_holdings_arrays(
            joined["quantity"].to_numpy(copy=False), joined["close"].to_numpy(copy=False),
            joined["adjusted_close"].to_numpy(copy=False), joined["source"].to_numpy(copy=False),
        )
        tickers = joined["ticker"].to_numpy(copy=False)
        for day, stamp in zip(days, parsed):
            if stamp == np.datetime64(day):
                start, stop = ranges[day]
                coverage = tuple(tickers[start:stop][tiingo[start:stop]])
                self._holdings_inputs[day] = _HoldingsInputs(quantity[start:stop], price[start:stop], coverage)

    def value(self, connection, portfolio_id, day):
        self.validate(connection, portfolio_id, day)
        self._check(connection, portfolio_id, day)
        if self._exact_join is None:
            self._build_exact_join()
        joined = self._exact_join.get(day)
        if joined is not None:
            return value_joined_positions(joined.copy(), day, 0, self._validate_coverage)
        return value_positions(self._position_state(day).copy(), day, 0, False,
                               self._latest_prices, self._validate_coverage)

    @staticmethod
    def _frame_total(frame):
        # Match preparation's component checks before pandas' skip-NaN sum.
        for item in frame.market_value:
            if not isfinite(float(item)):
                raise ValueError("Non-finite valuation")
        total = float(frame.market_value.sum())
        if not isfinite(total):
            raise ValueError("Non-finite valuation")
        return total

    def _ordered_holdings_inputs(self, day):
        if day not in self._holdings_inputs:
            frame = self._exact_join.get(day)
            inputs = None
            if frame is not None and all(frame[column].dtype == np.dtype("float64")
                                         for column in ("quantity", "close", "adjusted_close")):
                dates = frame["date"].to_numpy(copy=False)
                # Only the already-joined exact midnight dates qualify. NaT,
                # stale prices and unsupported dtypes retain the original path.
                if (np.issubdtype(dates.dtype, np.datetime64)
                        and np.all(dates == np.datetime64(day))):
                    quantity, price, tiingo = _select_holdings_arrays(
                        frame["quantity"].to_numpy(copy=False), frame["close"].to_numpy(copy=False),
                        frame["adjusted_close"].to_numpy(copy=False), frame["source"].to_numpy(copy=False),
                    )
                    coverage = tuple(frame["ticker"].to_numpy(copy=False)[tiingo])
                    inputs = _HoldingsInputs(quantity, price, coverage)
            self._holdings_inputs[day] = inputs
        return self._holdings_inputs[day]

    def holdings_total(self, connection, portfolio_id, day, *, use_arrays=True):
        """Internal Performance total; public DataFrame valuation stays unchanged.

        Currency/cash validation remains in preparation. Exact-date validation
        precedes ordered corporate checks, which precede multiplication and
        finite checks. Preserve pandas' ordered Series.sum reduction and keep
        cash separate; no dot product, Python sum or cross-date aggregation.
        use_arrays=False retains the preceding path for differential checks.
        """
        if not use_arrays:
            return self._frame_total(self.value(connection, portfolio_id, day))
        self.validate(connection, portfolio_id, day)
        self._check(connection, portfolio_id, day)
        if self._exact_join is None:
            self._build_exact_join()
        inputs = self._ordered_holdings_inputs(day)
        if inputs is None:
            return self._frame_total(self.value(connection, portfolio_id, day))
        for ticker in inputs.coverage_tickers:
            self._validate_coverage(ticker, day)
        # Same float64 elementwise multiplication as pandas Series arithmetic;
        # retain its floating-error policy and never reorder duplicate rows.
        with np.errstate(all="ignore"):
            values = np.multiply(inputs.quantity, inputs.price)
        if not np.isfinite(values).all():
            raise ValueError("Non-finite valuation")
        total = float(pd.Series(values, name="market_value", copy=False).sum())
        if not isfinite(total):
            raise ValueError("Non-finite valuation")
        return total
