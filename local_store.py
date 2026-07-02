"""Local storage sink — SQLite + daily CSV, for the no-accounts setup.

Data lives in ~/BathhouseData:
    snapshots.db                          append-only SQLite database
    daily/bathhouse-tracker-YYYY-MM-DD.csv   one spreadsheet per day

To keep disk use small, the raw JSON blob is stored only when a session is
first seen or when its price/spots/capacity/waitlist state changed since the
previous observation — the normalized columns are stored on every row.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import normalize
from normalize import log

DATA_DIR = Path.home() / "BathhouseData"
DB_PATH = DATA_DIR / "snapshots.db"
DAILY_DIR = DATA_DIR / "daily"

CSV_COLUMNS = [c for c in normalize.SCHEMA_FIELDS if c != "raw"]

DDL = """
create table if not exists snapshots (
    observed_at     text not null,
    brand           text not null,
    platform        text not null,
    location        text,
    session_id      text not null,
    class_name      text,
    start_time      text,
    instructor      text,
    capacity        integer,
    spots_available integer,
    spots_booked    integer,
    price           real,
    price_tier      text,
    currency        text,
    is_waitlist     integer,
    source_url      text,
    raw             text
);
create unique index if not exists snapshots_brand_session_observed_uq
    on snapshots (brand, session_id, observed_at);
create index if not exists snapshots_brand_start_idx
    on snapshots (brand, start_time);
create index if not exists snapshots_session_observed_idx
    on snapshots (session_id, observed_at);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    return conn


def _last_states(conn: sqlite3.Connection) -> dict:
    """Most recent (price, spots, capacity, waitlist) per (brand, session_id)."""
    rows = conn.execute(
        # SQLite guarantees bare columns come from the max(observed_at) row
        "select brand, session_id, price, spots_available, capacity, is_waitlist,"
        " max(observed_at) from snapshots group by brand, session_id"
    )
    return {(r[0], r[1]): (r[2], r[3], r[4], r[5]) for r in rows}


def insert(records: list[dict], db_path: Path = DB_PATH) -> tuple[int, int]:
    """Insert snapshot rows (duplicates ignored). Returns (new_rows, rows_with_raw)."""
    conn = connect(db_path)
    try:
        last = _last_states(conn)
        new_rows = 0
        raw_rows = 0
        for rec in records:
            key = (rec["brand"], rec["session_id"])
            waitlist = 1 if rec["is_waitlist"] else 0
            state = (rec["price"], rec["spots_available"], rec["capacity"], waitlist)
            keep_raw = last.get(key) != state
            raw_json = json.dumps(rec["raw"], default=str) if keep_raw else None
            cur = conn.execute(
                "insert or ignore into snapshots values"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec["observed_at"], rec["brand"], rec["platform"], rec["location"],
                    rec["session_id"], rec["class_name"], rec["start_time"],
                    rec["instructor"], rec["capacity"], rec["spots_available"],
                    rec["spots_booked"], rec["price"], rec["price_tier"],
                    rec["currency"], waitlist, rec["source_url"], raw_json,
                ),
            )
            if cur.rowcount:
                new_rows += 1
                raw_rows += 1 if keep_raw else 0
        conn.commit()
        log.info(
            "local: %d new rows (%d with raw json) -> %s", new_rows, raw_rows, db_path
        )
        return new_rows, raw_rows
    finally:
        conn.close()


def query(
    brand: str | None = None,
    since: str | None = None,
    include_raw: bool = False,
    db_path: Path = DB_PATH,
) -> list[dict]:
    cols = CSV_COLUMNS + (["raw"] if include_raw else [])
    sql = f"select {', '.join(cols)} from snapshots"
    where, params = [], []
    if brand:
        where.append("brand = ?")
        params.append(brand)
    if since:
        where.append("observed_at >= ?")
        params.append(since)
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by observed_at, brand"
    conn = connect(db_path)
    try:
        return [dict(zip(cols, row)) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def export_daily_csv(date_str: str, db_path: Path = DB_PATH) -> Path:
    """(Re)write the spreadsheet for one UTC day (YYYY-MM-DD)."""
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    out = DAILY_DIR / f"bathhouse-tracker-{date_str}.csv"
    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"select {', '.join(CSV_COLUMNS)} from snapshots"
            " where substr(observed_at, 1, 10) = ?"
            " order by observed_at, brand, start_time",
            (date_str,),
        ).fetchall()
    finally:
        conn.close()
    with open(out, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
    log.info("local: wrote %d rows -> %s", len(rows), out)
    return out
