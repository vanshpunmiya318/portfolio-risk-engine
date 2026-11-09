"""Append missing daily ETF prices to the historical portfolio SQLite database.

Run from project root:
    python scripts/update_market_prices.py
Requires: pip install yfinance
"""
from __future__ import annotations

import argparse
import math
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

TICKERS = ("SPY", "QQQ", "IWM", "GLD", "TLT", "XLE")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "historical_portfolio.db"


def update(db_path: Path, dry_run: bool = False) -> None:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise SystemExit("Missing dependency: run python -m pip install yfinance") from exc

    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        instruments = dict(conn.execute("SELECT ticker, id FROM instruments").fetchall())
        missing = set(TICKERS) - set(instruments)
        if missing:
            raise ValueError(f"Missing instruments: {sorted(missing)}")

        latest = {}
        for ticker in TICKERS:
            latest[ticker] = conn.execute(
                "SELECT MAX(date) FROM prices WHERE instrument_id = ?",
                (instruments[ticker],),
            ).fetchone()[0]
            if latest[ticker] is None:
                raise ValueError(f"No existing prices for {ticker}; refusing full-history import")

        # Yahoo's end is exclusive; request through tomorrow to include today if published.
        end = (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        proposed = []
        for ticker in TICKERS:
            start = (datetime.strptime(latest[ticker], "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
            if start >= end:
                print(f"{ticker}: already current")
                continue
            print(f"{ticker}: fetching {start} through latest available ...")
            df = yf.download(
                ticker, start=start, end=end, auto_adjust=False,
                actions=False, progress=False, threads=False,
            )
            if df.empty:
                print(f"{ticker}: no newer trading prices available")
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            required = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
            if not required.issubset(df.columns):
                raise ValueError(f"{ticker}: unexpected download columns {list(df.columns)}")
            count = 0
            for index, row in df.iterrows():
                day = pd.Timestamp(index).date().isoformat()
                if day <= latest[ticker]:
                    continue
                values = [float(row[k]) for k in ("Open", "High", "Low", "Close", "Adj Close")]
                if not all(math.isfinite(v) and v > 0 for v in values):
                    raise ValueError(f"{ticker} {day}: invalid OHLC prices: {values}")
                if values[1] < max(values[0], values[3]) - 1e-6 or values[2] > min(values[0], values[3]) + 1e-6:
                    raise ValueError(f"{ticker} {day}: inconsistent OHLC data")
                volume = row["Volume"]
                volume = int(volume) if pd.notna(volume) else None
                if volume is not None and volume < 0:
                    raise ValueError(f"{ticker} {day}: negative volume")
                proposed.append((instruments[ticker], day, *values, volume, "yfinance", fetched_at))
                count += 1
            print(f"{ticker}: {count} new rows")

        if not proposed:
            print("No new price rows. Database unchanged.")
            return

        if dry_run:
            print(f"DRY RUN: {len(proposed)} rows ready; database unchanged.")
            return

        # Back up only after all downloads and validation have succeeded.
        backup = db_path.with_name(f"{db_path.stem}_backup_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.db")
        backup_conn = sqlite3.connect(str(backup))
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        print(f"Backup created: {backup}")

        inserted = 0
        with conn:
            for row in proposed:
                exists = conn.execute(
                    "SELECT 1 FROM prices WHERE instrument_id = ? AND date = ? LIMIT 1",
                    row[:2],
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """INSERT INTO prices
                    (instrument_id, date, open, high, low, close, adjusted_close, volume, source, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    row,
                )
                inserted += 1
        print(f"SUCCESS: inserted {inserted} new price rows. Existing rows untouched.")
        for ticker in TICKERS:
            date = conn.execute("SELECT MAX(date) FROM prices WHERE instrument_id=?", (instruments[ticker],)).fetchone()[0]
            print(f"  {ticker}: {date}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true", help="Download and validate without writing")
    args = parser.parse_args()
    update(args.db.resolve(), dry_run=args.dry_run)
