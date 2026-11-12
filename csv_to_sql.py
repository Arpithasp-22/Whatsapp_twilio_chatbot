"""
csv_to_sql.py
-------------
Load sales data from a CSV into the SQLite database used by the bot.
Run once during setup, and again whenever the dataset is refreshed:

    python csv_to_sql.py [--csv data/sales.csv] [--db data/sales.db] [--dayfirst]

- Validates that the columns the bot relies on exist
- Normalises "Invoice Date" to ISO (YYYY-MM-DD) so SQLite date functions work
- Adds indexes for the lookups the bot performs
"""

import argparse
import sqlite3
import sys

import pandas as pd

TABLE_NAME = "sales"
REQUIRED_COLUMNS = ["Dealer", "State", "Product", "Basic Amount", "Invoice Date"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--csv", default="data/sales.csv")
    parser.add_argument("--db", default="data/sales.db")
    parser.add_argument("--dayfirst", action="store_true",
                        help="Parse dates like 31/12/2025 (day before month)")
    args = parser.parse_args()

    df = pd.read_csv(args.csv, low_memory=False)
    df.columns = [c.strip() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        print(f"❌ CSV is missing required columns: {missing}\n   Found: {list(df.columns)}")
        return 1

    df["Invoice Date"] = pd.to_datetime(df["Invoice Date"], dayfirst=args.dayfirst, errors="coerce")
    bad = int(df["Invoice Date"].isna().sum())
    if bad:
        print(f"⚠️  Dropping {bad} rows with unparseable Invoice Date")
        df = df.dropna(subset=["Invoice Date"])
    df["Invoice Date"] = df["Invoice Date"].dt.strftime("%Y-%m-%d")

    df["Basic Amount"] = pd.to_numeric(df["Basic Amount"], errors="coerce").fillna(0)
    for col in ("Dealer", "State", "Product"):
        df[col] = df[col].astype(str).str.strip()

    with sqlite3.connect(args.db) as conn:
        df.to_sql(TABLE_NAME, conn, if_exists="replace", index=False)
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_dealer ON {TABLE_NAME}("Dealer")')
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_date ON {TABLE_NAME}("Invoice Date")')
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_state ON {TABLE_NAME}("State")')

    print(f"✅ Loaded {len(df):,} rows into {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
