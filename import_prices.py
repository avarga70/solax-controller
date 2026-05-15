#!/usr/bin/env python3
"""
import_prices.py — fetch OTE-backed spot prices and store them in SQLite.
Run daily (for example via cron at 14:00) to import tomorrow's prices once
published. The script also backfills today's prices when missing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

SQLITE_DB = os.getenv("SQLITE_DB", "/var/lib/solax/solax.db")
PRICE_API = "https://spotovaelektrina.cz/api/v1/price/get-prices-json"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS EL_dailypr (
    edate  TEXT NOT NULL,
    etime  INTEGER NOT NULL,
    epreur REAL,
    eprczk REAL,
    eexch  REAL,
    ecomm  TEXT,
    PRIMARY KEY (edate, etime)
)
"""

CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_el_dailypr_edate ON EL_dailypr(edate)"
UPSERT_SQL = """
INSERT OR REPLACE INTO EL_dailypr (edate, etime, epreur, eprczk, eexch, ecomm)
VALUES (?, ?, ?, ?, ?, ?)
"""


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(SQLITE_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_prices_table(conn: sqlite3.Connection) -> None:
    conn.execute(CREATE_TABLE)
    conn.execute(CREATE_INDEX)
    conn.commit()


def _load_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "solax-import-prices/1.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def fetch_ote_prices(target_date: date) -> list[tuple[str, int, float | None, float | None, float | None, str]]:
    """Fetch hourly prices for one day from the public OTE-backed JSON feed."""
    urls = [
        f"{PRICE_API}?date={target_date.isoformat()}",
        PRICE_API,
    ]
    last_error: Exception | None = None
    for url in urls:
        try:
            payload = _load_json(url)
        except Exception as exc:  # pragma: no cover - network failure path
            last_error = exc
            continue

        if payload.get("hoursToday") and payload.get("hoursTomorrow") is not None:
            if payload.get("date") == target_date.isoformat():
                rows = payload.get("hoursToday", [])
            elif target_date == date.today():
                rows = payload.get("hoursToday", [])
            elif target_date == date.today() + timedelta(days=1):
                rows = payload.get("hoursTomorrow", [])
            else:
                rows = payload.get("hoursToday", []) if url.endswith(target_date.isoformat()) else []
        else:
            rows = []

        parsed = []
        for row in rows:
            hour = row.get("hour")
            if hour is None:
                continue
            parsed.append(
                (
                    target_date.isoformat(),
                    int(hour),
                    float(row["priceEur"]) if row.get("priceEur") is not None else None,
                    float(row["priceCZK"]) if row.get("priceCZK") is not None else None,
                    None,
                    "spotovaelektrina.cz",
                )
            )
        if parsed:
            return parsed

    if last_error is not None:
        raise RuntimeError(f"could not fetch prices for {target_date}: {last_error}") from last_error
    return []


def store_prices(conn: sqlite3.Connection, rows: list[tuple[str, int, float | None, float | None, float | None, str]]) -> int:
    if not rows:
        return 0
    conn.executemany(UPSERT_SQL, rows)
    conn.commit()
    return len(rows)


def main() -> int:
    conn = db_connect()
    try:
        ensure_prices_table(conn)
        total = 0
        for target_date in (date.today(), date.today() + timedelta(days=1)):
            rows = fetch_ote_prices(target_date)
            if not rows:
                print(f"No prices published yet for {target_date}")
                continue
            total += store_prices(conn, rows)
            print(f"Imported {len(rows)} hourly prices for {target_date}")
        if total == 0:
            print("No prices imported", file=sys.stderr)
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:  # pragma: no cover - network failure path
        print(f"Price download failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
