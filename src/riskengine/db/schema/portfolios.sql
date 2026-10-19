CREATE TABLE IF NOT EXISTS portfolios (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    currency TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    portfolio_id INTEGER NOT NULL,
    instrument_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL')),
    quantity REAL NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price > 0),
    currency TEXT NOT NULL,
    source TEXT NOT NULL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(id),
    FOREIGN KEY (instrument_id) REFERENCES instruments(id)
);



CREATE TABLE IF NOT EXISTS cash_flows (
    id INTEGER PRIMARY KEY,
    portfolio_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    flow_type TEXT NOT NULL
        CHECK (flow_type IN ('DEPOSIT', 'WITHDRAWAL')),
    amount REAL NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL,
    source TEXT NOT NULL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(id)
);