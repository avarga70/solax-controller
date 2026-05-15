#!/usr/bin/env python3
"""
import_historical.py — Import historical consumption/production CSV from
pnd.cezdistribuce.cz into the SQLite PV_run table.

CSV format:
  - Semicolon-separated, quoted fields
  - Timestamps: "DD.MM.YYYY HH:MM:SS" (Czech locale), end-of-interval
    Special case: "HH:MM:24:00:00" → midnight next day
  - Values in kW (average power over the 15-minute interval)
  - Status column: "naměřená data OK" (or similar) — rows with bad status skipped

Mapping:
  - consumption (+A) → ploadT  (W, integer)
  - production  (−A) → ppv     (W, integer)
  - pgridT = ploadT − ppv  (positive = import, negative = export)
  - all other columns default to 0

The script aggregates 15-minute intervals to hourly averages before storing.
Each inserted row has psource = 'HIST' and pdtime = YYYY-MM-DD HH:00:00.

Usage:
  python3 import_historical.py \\
      --consumption consumption.csv \\
      --production  production.csv \\
      [--db /var/lib/solax/solax.db]

Run it twice with different files? Safe — uses INSERT OR REPLACE.
NOTE: These files contain site-specific data. Replace with destination site
      data before going live.
"""

import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DB = "/var/lib/solax/solax.db"
SOURCE = "HIST"

CREATE_PV_RUN = """
CREATE TABLE IF NOT EXISTS PV_run (
    psource  TEXT    NOT NULL DEFAULT 'HIST',
    pdtime   TEXT    NOT NULL,
    pworkm   INTEGER NOT NULL DEFAULT 0,
    pongrm   INTEGER NOT NULL DEFAULT 0,
    pbattm   INTEGER NOT NULL DEFAULT 0,
    ptotag   REAL    NOT NULL DEFAULT 0,
    ptodag   REAL    NOT NULL DEFAULT 0,
    ptodas   REAL    NOT NULL DEFAULT 0,
    pbackT   INTEGER NOT NULL DEFAULT 0,
    pback1   INTEGER NOT NULL DEFAULT 0,
    pback2   INTEGER NOT NULL DEFAULT 0,
    pback3   INTEGER NOT NULL DEFAULT 0,
    ploadT   INTEGER NOT NULL DEFAULT 0,
    pload1   INTEGER NOT NULL DEFAULT 0,
    pload2   INTEGER NOT NULL DEFAULT 0,
    pload3   INTEGER NOT NULL DEFAULT 0,
    phousT   INTEGER NOT NULL DEFAULT 0,
    pactiT   INTEGER NOT NULL DEFAULT 0,
    pxload   INTEGER NOT NULL DEFAULT 0,
    ppv      INTEGER NOT NULL DEFAULT 0,
    pgridT   INTEGER NOT NULL DEFAULT 0,
    pgrid1   INTEGER NOT NULL DEFAULT 0,
    pgrid2   INTEGER NOT NULL DEFAULT 0,
    pgrid3   INTEGER NOT NULL DEFAULT 0,
    pbattp   INTEGER NOT NULL DEFAULT 0,
    pbatts   REAL    NOT NULL DEFAULT 0,
    pbatth   REAL    NOT NULL DEFAULT 0,
    pbattw   INTEGER NOT NULL DEFAULT 0,
    ptemp1   REAL    NOT NULL DEFAULT 0,
    ptemp2   REAL    NOT NULL DEFAULT 0,
    ptempb   REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (psource, pdtime)
)
"""


def parse_ts(raw: str) -> datetime:
    """Parse Czech datetime string, handling 24:00:00 end-of-day."""
    raw = raw.strip().strip('"')
    if "24:00:00" in raw:
        # "14.05.2026 24:00:00" → midnight of next day
        date_part = raw.split()[0]
        dt = datetime.strptime(date_part, "%d.%m.%Y") + timedelta(days=1)
        return dt
    return datetime.strptime(raw, "%d.%m.%Y %H:%M:%S")


def hour_bucket(ts: datetime) -> datetime:
    """
    Map an end-of-interval timestamp to its hour bucket (start of hour).
    15-min end-of-interval: 00:15 → hour 0, 01:00 → hour 0, 01:15 → hour 1.
    Strategy: subtract 1 second then floor to hour.
    """
    adjusted = ts - timedelta(seconds=1)
    return adjusted.replace(minute=0, second=0, microsecond=0)


def read_csv(path: str) -> dict[datetime, list[float]]:
    """
    Read a PND CSV file. Returns dict: hour_bucket → list of kW values.
    Rows with non-OK status or non-numeric values are skipped.
    """
    buckets: dict[datetime, list[float]] = defaultdict(list)
    skipped = 0
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)  # skip header
        for lineno, row in enumerate(reader, start=2):
            if len(row) < 2:
                continue
            try:
                ts = parse_ts(row[0])
                val = float(str(row[1]).replace(",", "."))
            except (ValueError, IndexError):
                skipped += 1
                continue
            # skip rows explicitly flagged as invalid ("neplatná data")
            if len(row) >= 3 and "NEPLATN" in row[2].upper():
                skipped += 1
                continue
            buckets[hour_bucket(ts)].append(val)
    if skipped:
        print(f"  [{Path(path).name}] skipped {skipped} bad/unparseable rows")
    return buckets


def aggregate(buckets: dict[datetime, list[float]]) -> dict[datetime, float]:
    """Average the kW readings per hour bucket → return average kW per hour."""
    return {ts: sum(vals) / len(vals) for ts, vals in buckets.items()}


def main():
    ap = argparse.ArgumentParser(description="Import CEZ PND historical data into SQLite PV_run")
    ap.add_argument("--consumption", required=True, help="Path to +A consumption CSV")
    ap.add_argument("--production",  required=True, help="Path to -A production CSV")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")
    ap.add_argument("--dry-run", action="store_true", help="Parse and report without writing to DB")
    args = ap.parse_args()

    print(f"Reading consumption: {args.consumption}")
    cons_raw = read_csv(args.consumption)
    cons = aggregate(cons_raw)
    print(f"  → {len(cons)} hourly buckets")

    print(f"Reading production: {args.production}")
    prod_raw = read_csv(args.production)
    prod = aggregate(prod_raw)
    print(f"  → {len(prod)} hourly buckets")

    all_hours = sorted(set(cons.keys()) | set(prod.keys()))
    print(f"Total unique hours: {len(all_hours)}")

    if args.dry_run:
        # Show a sample
        sample = all_hours[:5] + all_hours[-3:]
        print("\nSample rows (dry-run):")
        print(f"  {'pdtime':<22}  {'ploadT':>7}  {'ppv':>7}  {'pgridT':>7}")
        for h in sample:
            load_w = int(cons.get(h, 0.0) * 1000)
            pv_w   = int(prod.get(h, 0.0) * 1000)
            grid_w = load_w - pv_w
            print(f"  {h.strftime('%Y-%m-%d %H:%M:%S')}  {load_w:>7}  {pv_w:>7}  {grid_w:>7}")
        print("\nDry-run complete — no data written.")
        return

    print(f"\nOpening DB: {args.db}")
    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(CREATE_PV_RUN)

    rows = []
    for h in all_hours:
        load_w = int(cons.get(h, 0.0) * 1000)
        pv_w   = int(prod.get(h, 0.0) * 1000)
        grid_w = load_w - pv_w
        rows.append((
            SOURCE,
            h.strftime("%Y-%m-%d %H:%M:%S"),
            load_w,  # ploadT
            load_w,  # phousT (same as ploadT for grid-metered site)
            pv_w,    # ppv
            grid_w,  # pgridT
        ))

    con.executemany("""
        INSERT OR REPLACE INTO PV_run
            (psource, pdtime, ploadT, phousT, ppv, pgridT)
        VALUES (?, ?, ?, ?, ?, ?)
    """, rows)
    con.commit()
    con.close()

    print(f"Inserted/replaced {len(rows)} rows into PV_run (source='{SOURCE}').")
    print("Done.")


if __name__ == "__main__":
    main()
