CREATE TABLE IF NOT EXISTS instruments (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    sector TEXT,
    primary_source TEXT NOT NULL DEFAULT 'tiingo'
        CHECK (primary_source IN ('tiingo', 'yahoo_finance', 'test'))
);


