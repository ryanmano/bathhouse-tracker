#!/usr/bin/env python3
"""Export snapshots from Supabase to a local CSV (openable in Excel/Google Sheets).

Default output is the simple, human-readable sheet (Eastern-time ranges like
"9-10am"). Use --full for the machine-friendly format with UTC timestamps.

Usage:
    python export_to_csv.py                          # everything -> snapshots.csv
    python export_to_csv.py --brand othership        # one brand
    python export_to_csv.py --since 2026-07-01 --out july.csv
    python export_to_csv.py --full                   # raw schema columns instead
    python export_to_csv.py --full --include-raw     # plus the raw JSON column
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import normalize
import sheet_format
from run import load_dotenv

PAGE_SIZE = 1000

COLUMNS = [c for c in normalize.SCHEMA_FIELDS if c != "raw"]


def fetch_rows(brand: str | None, since: str | None, include_raw: bool) -> list[dict]:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        # No Supabase configured -> export from the local SQLite store instead.
        import local_store

        print(f"no Supabase env set — exporting from {local_store.DB_PATH}")
        return local_store.query(brand=brand, since=since, include_raw=include_raw)
    select = ",".join(
        COLUMNS + sheet_format.EXTRA_SELECTS + (["raw"] if include_raw else [])
    )
    params: dict[str, str] = {"select": select, "order": "observed_at.asc,brand.asc"}
    if brand:
        params["brand"] = f"eq.{brand}"
    if since:
        params["observed_at"] = f"gte.{since}"
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    endpoint = url.rstrip("/") + "/rest/v1/snapshots"

    rows: list[dict] = []
    with normalize.new_client() as client:
        offset = 0
        while True:
            page = normalize.get_json(
                client,
                endpoint,
                params={**params, "limit": str(PAGE_SIZE), "offset": str(offset)},
                headers=headers,
            )
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            offset += PAGE_SIZE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand", help="filter to one brand")
    parser.add_argument("--since", help="only snapshots observed at/after this ISO date")
    parser.add_argument("--out", default="snapshots.csv", help="output file path")
    parser.add_argument(
        "--full", action="store_true", help="machine-friendly schema columns (UTC)"
    )
    parser.add_argument("--include-raw", action="store_true", help="include raw JSON column")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent / ".env")
    rows = fetch_rows(args.brand, args.since, args.include_raw)
    if not rows:
        print("no rows matched")
        return 0

    if args.full or args.include_raw:
        fieldnames = COLUMNS + (["raw"] if args.include_raw else [])
    else:
        fieldnames = sheet_format.SIMPLE_COLUMNS
        rows = sheet_format.simple_rows(rows)
    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
