CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY,
    instrument_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL CHECK (high >= low),
    low REAL NOT NULL,
    close REAL NOT NULL CHECK (close > 0),
    adjusted_close REAL NOT NULL CHECK (adjusted_close > 0),
    volume INTEGER,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (instrument_id) REFERENCES instruments(id),
    UNIQUE (instrument_id, date)
);