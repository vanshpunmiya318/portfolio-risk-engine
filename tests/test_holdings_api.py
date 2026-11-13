import asyncio
import json
import sqlite3
from contextlib import closing, contextmanager
from urllib.parse import urlencode

import pytest

from test_cost_basis_cutoff import ledger, price, split, trade
from riskengine.api import main
from riskengine.api.holdings_models import HoldingsResponse
from riskengine.db.connection import get_connection
from riskengine.db.portfolios import get_portfolio_value
from riskengine.risk.holdings import load_portfolio_holdings
from riskengine.risk.holdings_service import get_holdings_summary


@pytest.fixture
def api(ledger, monkeypatch):
    path, connection = ledger
    monkeypatch.delenv("RISKENGINE_DEMO", raising=False)
    monkeypatch.setenv("RISKENGINE_DB", str(path))

    def request(portfolio_id=1, **query):
        connection.commit()

        async def execute():
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            async with main.app.router.lifespan_context(main.app):
                await main.app({
                    "type": "http", "http_version": "1.1", "method": "GET",
                    "path": f"/api/portfolios/{portfolio_id}/holdings", "root_path": "",
                    "query_string": urlencode(query).encode(), "headers": [],
                    "scheme": "http", "server": ("test", 80), "client": ("test", 1),
                }, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            return status, json.loads(body)

        return asyncio.run(execute())

    return request


def seed(connection):
    trade(connection, "2026-01-02", "BUY", 10, 100)
    price(connection, "2026-01-05", 120)


def test_historical_response_matches_existing_holdings_and_totals(ledger, api):
    path, connection = ledger
    seed(connection)
    trade(connection, "2026-01-06", "SELL", 4, 150)
    status, body = api(as_of_date="2026-01-05")
    assert status == 200
    HoldingsResponse.model_validate(body)
    existing = load_portfolio_holdings(path, 1, "2026-01-05").iloc[0]
    row = body["holdings"][0]
    assert row["quantity"] == existing["shares"] == 10
    assert row["remaining_average_cost_basis"] == existing["net_invested"] == 1000
    assert row["unrealized_gain_loss"] == existing["unrealized_pl"] == 200
    assert row["market_value"] == 1200
    assert row["market_price"] == 120
    assert row["effective_price_date"] == "2026-01-05"
    assert row["price_age_days"] == 0
    assert row["price_status"] == "current"
    assert row["invested_weight"] == 1
    assert body["totals"]["market_value"] == 1200
    assert body["totals"]["unrealized_gain_loss"] == 200
    assert body["requested_valuation_date"] == body["effective_valuation_date"] == "2026-01-05"
    assert body["valuation_complete"] is True
    assert body["warnings"] == []
    assert body["weight_basis"] == "invested_market_value_excluding_cash"
    assert body["weight_unit"] == "fraction"
    assert "not_net_transaction_investment" in body["cost_basis_method"]


def test_same_day_splits_and_trades_use_same_cutoff(ledger, api):
    _, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    split(connection, "2026-01-05", 2)
    trade(connection, "2026-01-05", "BUY", 4, 60)
    trade(connection, "2026-01-05", "SELL", 6, 70)
    trade(connection, "2026-01-06", "BUY", 1, 90)
    price(connection, "2026-01-05", 80)
    status, body = api(as_of_date="2026-01-05")
    assert status == 200
    row = body["holdings"][0]
    assert row["quantity"] == 18
    assert row["remaining_average_cost_basis"] == pytest.approx(930)
    assert row["unrealized_gain_loss"] == pytest.approx(510)


def add_second_holding(connection, with_price=True):
    connection.execute(
        "INSERT INTO instruments (id,ticker,name,asset_type,currency,primary_source) "
        "VALUES (2,'OTHER','Other Instrument','STOCK','USD','test')"
    )
    connection.execute(
        "INSERT INTO transactions (portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) "
        "VALUES (1,2,'2026-01-02','BUY',5,80,'USD','test')"
    )
    if with_price:
        connection.execute(
            "INSERT INTO prices (instrument_id,date,open,high,low,close,adjusted_close,source,fetched_at) "
            "VALUES (2,'2026-01-05',100,100,100,100,100,'test','2026-01-05T00:00:00Z')"
        )


def test_weights_and_totals_exclude_cash(ledger, api):
    _, connection = ledger
    seed(connection)
    add_second_holding(connection)
    connection.execute("INSERT INTO cash_flows (portfolio_id,date,flow_type,amount,currency,source) VALUES (1,'2026-01-02','DEPOSIT',10000,'USD','test')")
    status, body = api()
    assert status == 200
    assert body["requested_valuation_date"] is None
    assert body["effective_valuation_date"] == "2026-01-05"
    assert body["totals"] == dict(holding_count=2, valued_holding_count=2, market_value=1700,
                                  remaining_average_cost_basis=1400, unrealized_gain_loss=300)
    assert sum(h["invested_weight"] for h in body["holdings"]) == pytest.approx(1)
    assert body["holdings"][0]["invested_weight"] == pytest.approx(500 / 1700)  # OTHER sorts first.


def test_missing_price_keeps_position_and_basis_but_invalidates_totals(ledger, api):
    _, connection = ledger
    seed(connection)
    add_second_holding(connection, with_price=False)
    status, body = api()
    assert status == 200
    row = next(h for h in body["holdings"] if h["ticker"] == "OTHER")
    assert row["quantity"] == 5
    assert row["remaining_average_cost_basis"] == 400
    assert row["price_status"] == "missing"
    for key in ("effective_price_date", "price_age_days", "market_price", "market_value", "unrealized_gain_loss", "invested_weight"):
        assert row[key] is None
    assert body["valuation_complete"] is False
    assert body["totals"]["market_value"] is None
    assert body["totals"]["unrealized_gain_loss"] is None
    assert body["totals"]["remaining_average_cost_basis"] == 1400
    assert all(h["invested_weight"] is None for h in body["holdings"])
    assert {w["code"] for w in body["warnings"]} == {"missing_price", "incomplete_valuation"}
    # Existing callers keep their strict missing-price behavior.
    with pytest.raises(ValueError, match="No price available"):
        get_portfolio_value(connection, 1, "2026-01-05")


@pytest.mark.parametrize("limit,complete,status", [(0, False, "stale"), (1, False, "stale"), (2, True, "carried_forward")])
def test_staleness_uses_calendar_days_and_preserves_requested_cutoff(ledger, api, limit, complete, status):
    _, connection = ledger
    seed(connection)
    code, body = api(as_of_date="2026-01-07", max_price_age_days=limit)
    assert code == 200
    assert body["effective_valuation_date"] == "2026-01-07"
    assert body["valuation_complete"] is complete
    row = body["holdings"][0]
    assert row["price_age_days"] == 2
    assert row["price_status"] == status
    assert row["market_price"] == 120  # Observed price remains visible even when excluded.
    assert row["market_value"] == (1200 if complete else None)
    assert body["warnings"]


def test_future_price_is_not_used(ledger, api):
    _, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    price(connection, "2026-01-06", 120)
    status, body = api(as_of_date="2026-01-05")
    assert status == 200
    assert body["holdings"][0]["price_status"] == "missing"
    assert body["totals"]["market_value"] is None


@pytest.mark.parametrize("pid,query", [
    (0, {}), (-1, {}), ("abc", {}), (9223372036854775808, {}), (1, {"as_of_date": "2026-02-30"}),
    (1, {"as_of_date": "20260105"}), (1, {"as_of_date": "2026-01-05T00:00:00"}),
    (1, {"max_price_age_days": -1}), (1, {"max_price_age_days": "abc"}),
    (1, {"max_price_age_days": 1.5}),
])
def test_invalid_inputs_are_422(api, pid, query):
    assert api(pid, **query)[0] == 422


def test_unknown_portfolio_and_missing_database(ledger, api, monkeypatch):
    path, _ = ledger
    assert api(999)[0] == 404
    monkeypatch.setenv("RISKENGINE_DB", str(path.parent / "absent.db"))
    status, body = api()
    assert status == 404
    assert body["detail"] == "Portfolio database not found"


def test_empty_portfolio_and_no_available_default_date(ledger, api):
    status, body = api()
    assert status == 200
    assert body["effective_valuation_date"] is None
    assert body["requested_valuation_date"] is None
    assert body["holdings"] == []
    assert body["valuation_complete"] is True
    assert body["totals"]["market_value"] == 0
    status, body = api(as_of_date="2026-01-05")
    assert status == 200
    assert body["holdings"] == []
    assert body["totals"]["market_value"] == 0
    assert body["totals"]["holding_count"] == 0
    assert body["valuation_complete"] is True
    assert body["warnings"][0]["code"] == "no_open_positions"


def test_read_only_connection_and_snapshot(ledger, api, monkeypatch):
    _, connection = ledger
    seed(connection)
    opened = []
    real = main.get_connection

    @contextmanager
    def check_closed(c):
        with closing(c):
            yield c
        # Check in the worker thread that owns the SQLite connection.
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            c.execute("SELECT 1")
        opened.append(True)

    def observe(path, *, read_only):
        assert read_only is True
        c = real(path, read_only=read_only)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("CREATE TABLE forbidden (id INTEGER)")
        c.rollback()
        return c

    monkeypatch.setattr(main, "get_connection", observe)
    monkeypatch.setattr(main, "closing", check_closed)
    assert api()[0] == 200
    assert opened == [True]


def test_nonfinite_values_are_json_safe(ledger):
    _, connection = ledger
    seed(connection)
    connection.execute("UPDATE prices SET adjusted_close = ?", (float("inf"),))
    result = get_holdings_summary(connection, 1, "2026-01-05")
    assert result["holdings"][0]["price_status"] == "invalid"
    assert result["holdings"][0]["market_value"] is None
    assert result["totals"]["market_value"] is None
    json.dumps(result, allow_nan=False)
    HoldingsResponse.model_validate(result)


def test_service_supports_read_only_connection(ledger):
    path, connection = ledger
    seed(connection)
    connection.commit()
    with closing(get_connection(path, read_only=True)) as c:
        assert get_holdings_summary(c, 1)["totals"]["market_value"] == 1200


def test_currency_mismatch_cannot_create_mixed_currency_totals(ledger, api):
    _, connection = ledger
    seed(connection)
    connection.execute("UPDATE instruments SET currency = 'EUR'")
    status, body = api()
    assert status == 200
    assert body["valuation_complete"] is False
    assert body["totals"]["remaining_average_cost_basis"] is None
    assert body["holdings"][0]["market_value"] is None
    assert body["holdings"][0]["market_price"] == 120
    assert body["holdings"][0]["market_price_currency"] == "EUR"
    assert body["holdings"][0]["currency_status"] == "conflict"
    assert "currency_mismatch" in {w["code"] for w in body["warnings"]}


def test_missing_corporate_action_coverage_is_explicit_conflict(ledger, api):
    _, connection = ledger
    seed(connection)
    connection.execute("UPDATE prices SET source = 'tiingo'")
    status, body = api()
    assert status == 409
    assert "corporate action coverage" in body["detail"]


def test_price_before_split_is_not_used_with_post_split_quantities(ledger, api):
    _, connection = ledger
    seed(connection)
    split(connection, "2026-01-06", 2)
    status, body = api(as_of_date="2026-01-06", max_price_age_days=1)
    assert status == 200
    row = body["holdings"][0]
    assert row["quantity"] == 20
    assert row["remaining_average_cost_basis"] == 1000
    assert row["price_status"] == "invalid"
    assert row["market_value"] is None
    assert body["valuation_complete"] is False
    assert "price_before_split" in {w["code"] for w in body["warnings"]}
    assert "carried_forward_price" not in {w["code"] for w in body["warnings"]}


@pytest.mark.parametrize("cutoff,conflict", [("2026-01-05", False), ("2026-01-06", True)])
def test_transaction_currency_validation_is_cutoff_aware(ledger, api, cutoff, conflict):
    _, connection = ledger
    seed(connection)
    trade(connection, "2026-01-06", "SELL", 4, 150)
    connection.execute("UPDATE transactions SET currency = 'EUR' WHERE date = '2026-01-06'")
    price(connection, "2026-01-06", 130)
    status, body = api(as_of_date=cutoff)
    assert status == 200
    row = body["holdings"][0]
    assert row["currency_status"] == ("conflict" if conflict else "consistent")
    assert row["market_price_currency"] == "USD"
    assert row["quantity"] == (6 if conflict else 10)
    assert body["valuation_complete"] is not conflict
    if conflict:
        for key in ("market_value", "remaining_average_cost_basis", "unrealized_gain_loss", "invested_weight"):
            assert row[key] is None
        for key in ("market_value", "remaining_average_cost_basis", "unrealized_gain_loss"):
            assert body["totals"][key] is None
        assert "currency_mismatch" in {w["code"] for w in body["warnings"]}
    else:
        assert row["remaining_average_cost_basis"] == 1000
        assert row["unrealized_gain_loss"] == 200
        assert body["warnings"] == []


def test_foreign_transaction_currency_suppressed_even_with_matching_metadata(ledger, api):
    _, connection = ledger
    seed(connection)
    connection.execute("UPDATE transactions SET currency = 'EUR'")
    status, body = api()
    assert status == 200
    assert body["currency"] == "USD"
    assert body["holdings"][0]["currency_status"] == "conflict"
    assert body["holdings"][0]["remaining_average_cost_basis"] is None
    assert body["totals"]["unrealized_gain_loss"] is None


def test_other_portfolio_transactions_do_not_invalidate_currency(ledger, api):
    _, connection = ledger
    seed(connection)
    connection.execute("INSERT INTO portfolios VALUES (2, 'Other', 'EUR')")
    connection.execute("INSERT INTO transactions (portfolio_id,instrument_id,date,transaction_type,quantity,price,currency,source) VALUES (2,1,'2026-01-02','BUY',1,100,'EUR','test')")
    status, body = api()
    assert status == 200
    assert body["valuation_complete"] is True
    assert body["warnings"] == []


def test_currency_conflict_invalidates_whole_portfolio_totals_not_other_rows(ledger, api):
    _, connection = ledger
    seed(connection)
    add_second_holding(connection)
    connection.execute("UPDATE transactions SET currency='EUR' WHERE instrument_id=2")
    status, body = api()
    assert status == 200
    valid = next(h for h in body["holdings"] if h["ticker"] == "TEST")
    assert valid["market_value"] == 1200
    assert valid["remaining_average_cost_basis"] == 1000
    assert valid["invested_weight"] is None
    assert body["totals"]["market_value"] is None
    assert body["totals"]["remaining_average_cost_basis"] is None


def test_no_prices_with_positions_is_usable_not_a_conflict(ledger, api):
    _, connection = ledger
    trade(connection, "2026-01-02", "BUY", 10, 100)
    status, body = api()
    assert status == 200
    assert body["effective_valuation_date"] == "2026-01-02"
    assert body["requested_valuation_date"] is None
    assert body["holdings"][0]["price_status"] == "missing"
    assert body["totals"]["market_value"] is None
    assert body["totals"]["remaining_average_cost_basis"] == 1000
    assert {w["code"] for w in body["warnings"]} == {"cutoff_from_transactions", "missing_price", "incomplete_valuation"}


def test_currency_rejection_does_not_claim_carried_price_accepted(ledger, api):
    _, connection = ledger
    seed(connection)
    connection.execute("UPDATE transactions SET currency='EUR'")
    status, body = api(as_of_date="2026-01-06", max_price_age_days=1)
    assert status == 200
    assert "carried_forward_price" not in {w["code"] for w in body["warnings"]}
    assert "currency_mismatch" in {w["code"] for w in body["warnings"]}


def test_price_age_policy_documented_in_openapi():
    parameters = main.app.openapi()["paths"]["/api/portfolios/{portfolio_id}/holdings"]["get"]["parameters"]
    parameter = next(p for p in parameters if p["name"] == "max_price_age_days")
    assert parameter["schema"]["default"] == 0
    assert "calendar days" in parameter["description"]
    assert "0 requires an exact-date price" in parameter["description"]


def test_unexpected_value_error_is_not_misclassified_as_conflict(ledger, api, monkeypatch):
    from riskengine.risk import holdings_service
    _, connection = ledger
    seed(connection)

    def fail(*args, **kwargs):
        raise ValueError("Unexpected programming error")

    monkeypatch.setattr(holdings_service, "get_portfolio_value", fail)
    with pytest.raises(ValueError, match="Unexpected programming error"):
        api()


def test_negative_position_is_a_genuine_integrity_conflict(ledger, api):
    _, connection = ledger
    seed(connection)
    trade(connection, "2026-01-05", "SELL", 11, 130)
    status, body = api()
    assert status == 409
    assert "Negative position" in body["detail"]
