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



CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    ex_date TEXT NOT NULL,
    action_type TEXT NOT NULL
        CHECK (action_type IN ('SPLIT')),
    ratio REAL NOT NULL
        CHECK (ratio > 0),
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,

    FOREIGN KEY (instrument_id)
        REFERENCES instruments(id),

    UNIQUE (instrument_id, ex_date, action_type)
);



CREATE TABLE IF NOT EXISTS corporate_action_coverage (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,

    CHECK (start_date <= end_date),

    FOREIGN KEY (instrument_id)
        REFERENCES instruments(id),

    UNIQUE (
        instrument_id,
        start_date,
        end_date,
        source
    )
);