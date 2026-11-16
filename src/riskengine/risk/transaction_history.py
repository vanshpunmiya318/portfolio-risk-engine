"""Read recorded ledger rows without valuation or accounting aggregation."""
from datetime import date
import math

from riskengine.api.transaction_models import (
    HistoryIssue, TradeHistoryRow, CashFlowHistoryRow, TransactionsResponse,
)


class HistoryDataConflict(ValueError):
    pass


def get_transactions_history(connection, portfolio_id):
    from riskengine.risk.holdings_service import PortfolioNotFound
    portfolio = connection.execute('SELECT name,currency FROM portfolios WHERE id=?', (portfolio_id,)).fetchone()
    if portfolio is None:
        raise PortfolioNotFound(f'Portfolio {portfolio_id} not found')

    def issues(row, kind, numeric):
        result = []
        def warn(code, message):
            result.append(HistoryIssue(code=code, message=message))
        try:
            if date.fromisoformat(row['date']).isoformat() != row['date']:
                raise ValueError()
        except (ValueError, TypeError):
            warn('invalid_recorded_date', 'Recorded date is not a valid YYYY-MM-DD calendar date; ordering uses recorded text.')
        if row[kind] not in (('BUY', 'SELL') if kind == 'transaction_type' else ('DEPOSIT', 'WITHDRAWAL')):
            warn('invalid_recorded_type', 'Unrecognized recorded ledger type.')
        for field in numeric:
            value = row[field]
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise HistoryDataConflict(f"{kind} row {row['id']}: {field} is not a finite recorded number; history cannot be serialized safely.")
            if value <= 0:
                warn('invalid_recorded_value', f'Recorded {field} must be positive; original value retained.')
        if row['currency'] != portfolio[1]:
            warn('currency_mismatch', 'Recorded currency differs from portfolio currency; no conversion has been performed.')
        if not row['source'].strip():
            warn('missing_source', 'Recorded source is blank.')
        return result

    trades = []
    cursor = connection.execute('''SELECT t.id,t.date,t.transaction_type,t.instrument_id,
        i.ticker,i.name AS instrument_name,i.currency AS instrument_currency,
        t.quantity,t.price,t.currency,t.source FROM transactions t
        LEFT JOIN instruments i ON i.id=t.instrument_id
        WHERE t.portfolio_id=? ORDER BY t.date DESC,t.id DESC''', (portfolio_id,))
    for values in cursor:
        row = dict(zip([column[0] for column in cursor.description], values))
        notes = issues(row, 'transaction_type', ('quantity', 'price'))
        if row['ticker'] is None:
            notes.append(HistoryIssue(code='missing_instrument', message='Recorded instrument has no matching instrument metadata. Ledger row retained.'))
        elif row['currency'] != row['instrument_currency']:
            notes.append(HistoryIssue(code='instrument_currency_mismatch', message='Recorded trade currency differs from instrument currency; no conversion has been performed.'))
        trades.append(TradeHistoryRow(**row, issues=notes))
    cash = []
    cursor = connection.execute('''SELECT id,date,flow_type,amount,currency,source FROM cash_flows
        WHERE portfolio_id=? ORDER BY date DESC,id DESC''', (portfolio_id,))
    for values in cursor:
        row = dict(zip([column[0] for column in cursor.description], values))
        cash.append(CashFlowHistoryRow(**row, issues=issues(row, 'flow_type', ('amount',))))
    return TransactionsResponse(portfolio_id=portfolio_id, name=portfolio[0], currency=portfolio[1], trades=trades, external_cash_flows=cash)
