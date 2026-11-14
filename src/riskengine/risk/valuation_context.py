"""Request-local historical records for one uninterrupted caller-owned snapshot.

No connection lifecycle operations, SQL adapters or cross-request caches. SQLite
still computes split factors and cash sums; shared legacy kernels do accounting.
"""
from bisect import bisect_right
from datetime import date
import json
import sqlite3

import pandas as pd

from riskengine.db.corporate_actions import validate_coverage_intervals
from riskengine.db.instruments import normalise_ticker
from riskengine.db.portfolios import value_positions
from riskengine.db.transactions import positions_from_records, validate_transaction_records


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
        epoch = self._epoch(day)
        if epoch not in self._positions:
            records = [(r[1], r[2], r[3], r[4], r[5], r[0]) for r in self._transactions if r[3] <= epoch]
            self._positions[epoch] = positions_from_records(
                records, lambda instrument, traded: self._factors[(instrument, traded, epoch)],
            )
        return self._positions[epoch].copy()

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

    def value(self, connection, portfolio_id, day):
        self.validate(connection, portfolio_id, day)
        return value_positions(self.positions(connection, portfolio_id, day), day, 0, False,
                               self._latest_prices, self._validate_coverage)
