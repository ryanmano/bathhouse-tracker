#!/usr/bin/env python3
"""Publish spreadsheets to a shared Dropbox folder after each scrape run.

Files maintained in the Dropbox app folder (share it once; everyone in the
share gets updates automatically). Styled Excel workbooks:

    latest.xlsx                          newest snapshot of every upcoming
                                         session — refreshed every run
    prestart-summary-YYYY-MM-DD.xlsx     clean daily summary: ONE row per class
                                         that day = its final reading before
                                         doors opened + minutes-before. Grouped
                                         by the class's date (Eastern). Today's
                                         refreshes each run as classes start;
                                         past days written once (backfilled).
    bathhouse-tracker-YYYY-MM-DD.xlsx    complete hour-by-hour history for a
                                         finished UTC day — uploaded once,
                                         shortly after that day ends (missed
                                         days are backfilled automatically)

Requires env: SUPABASE_URL, SUPABASE_SERVICE_KEY, DROPBOX_APP_KEY,
DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN. Exits 0 with a notice when the
Dropbox secrets are not configured, so the workflow works before setup.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import normalize
import sheet_format
from run import load_dotenv

EASTERN = ZoneInfo("America/New_York")

COLUMNS = [c for c in normalize.SCHEMA_FIELDS if c != "raw"]
PAGE = 1000
BACKFILL_DAYS = 7  # how far back to check for missing daily files
# Dropbox refuses to share an app folder's ROOT, but a subfolder can be shared.
# Everything is published under here so the whole set is shareable via one link.
SHARE_DIR = "/Bathhouse Tracker"
# Reliable 5-minute pre-start triggering began this date; earlier days had
# off timing and were removed, so we never (re)publish files before it.
EARLIEST_DAY = "2026-07-07"


def _sb_headers() -> dict:
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def sb_rows(client, filters: list[tuple[str, str]], with_time_raw: bool = True) -> list[dict]:
    """Fetch snapshot rows from Supabase with pagination.

    `with_time_raw=False` skips the raw-JSON end-time extractions (needed only
    for the Time column) — much cheaper for the summary, which has no Time.
    """
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/snapshots"
    cols = ["id"] + COLUMNS + (sheet_format.EXTRA_SELECTS if with_time_raw else [])
    select = ",".join(cols)
    rows: list[dict] = []
    last_id = 0
    while True:
        # Keyset pagination by primary key: cheap for the DB at any depth
        # (no deep OFFSET re-scan). Extra retries absorb free-tier 500 blips.
        page = normalize.get_json(
            client,
            url,
            params=[("select", select), ("order", "id.asc"), ("limit", str(PAGE))]
            + filters
            + [("id", f"gt.{last_id}")],
            headers=_sb_headers(),
            retries=6,
            backoff=1.5,
        )
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        last_id = page[-1]["id"]


def _et_day_bounds_utc(ymd: str) -> tuple[datetime, datetime]:
    """UTC [start, end) for one Eastern calendar day (YYYY-MM-DD)."""
    from datetime import date

    d = date.fromisoformat(ymd)
    start_et = datetime(d.year, d.month, d.day, tzinfo=EASTERN)
    end_et = start_et + timedelta(days=1)
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


def fetch_day(client, ymd: str, with_time_raw: bool = True) -> list[dict]:
    """Readings for classes that START on the given Eastern day.

    Bounded on BOTH start_time (the class day) and observed_at (readings from
    ~that day only), so we pull each session's near-start rows instead of its
    entire multi-week observation history. This keeps every query small enough
    that Postgres never times out, no matter how large the table grows.
    """
    s_utc, e_utc = _et_day_bounds_utc(ymd)
    obs_lo = s_utc - timedelta(hours=12)  # cover pre-midnight reads of early classes
    return sb_rows(
        client,
        [
            ("start_time", f"gte.{s_utc.isoformat()}"),
            ("start_time", f"lt.{e_utc.isoformat()}"),
            ("observed_at", f"gte.{obs_lo.isoformat()}"),
            ("observed_at", f"lt.{e_utc.isoformat()}"),
        ],
        with_time_raw=with_time_raw,
    )


def _days_from(earliest: str, today_et: str) -> list[str]:
    """List of YYYY-MM-DD Eastern days from `earliest` through `today_et`."""
    from datetime import date

    start = date.fromisoformat(max(earliest, EARLIEST_DAY))
    end = date.fromisoformat(today_et)
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def latest_observed_at(client) -> str | None:
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/snapshots"
    page = normalize.get_json(
        client,
        url,
        params=[("select", "observed_at"), ("order", "observed_at.desc"), ("limit", "1")],
        headers=_sb_headers(),
    )
    return page[0]["observed_at"] if page else None


def sheet_bytes(rows: list[dict]) -> bytes:
    return sheet_format.xlsx_bytes(rows)


def dropbox_access_token(client) -> str:
    resp = client.post(
        "https://api.dropbox.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": os.environ["DROPBOX_REFRESH_TOKEN"],
        },
        auth=(os.environ["DROPBOX_APP_KEY"], os.environ["DROPBOX_APP_SECRET"]),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def dropbox_exists(client, token: str, path: str) -> bool:
    resp = client.post(
        "https://api.dropboxapi.com/2/files/get_metadata",
        headers={"Authorization": f"Bearer {token}"},
        json={"path": path},
    )
    return resp.status_code == 200


def dropbox_upload(client, token: str, path: str, data: bytes) -> None:
    resp = client.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite", "mute": True}),
            "Content-Type": "application/octet-stream",
        },
        content=data,
    )
    resp.raise_for_status()
    print(f"uploaded {path} ({len(data):,} bytes)")


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    missing = [
        k
        for k in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN")
        if not os.environ.get(k)
    ]
    if missing:
        print(f"dropbox not configured ({', '.join(missing)} unset) — skipping publish")
        return 0

    with normalize.new_client() as client:
        token = dropbox_access_token(client)

        # The shared folder holds ONLY the clean daily pre-start summaries.
        # (latest.xlsx and the full hourly bathhouse-tracker-* history files
        # were intentionally removed — too noisy for the daily viewer. All raw
        # data still lives in the database and can be exported on demand via
        # export_to_csv.py if the detailed view is ever needed again.)

        now = datetime.now(timezone.utc)
        today_et = now.astimezone(EASTERN).date().isoformat()
        days = _days_from(EARLIEST_DAY, today_et)  # each queried in a small chunk

        # prestart-summary-YYYY-MM-DD.xlsx — one clean daily file per Eastern
        # day. Today refreshes every run; completed days are written once.
        for day in days:
            path = f"{SHARE_DIR}/prestart-summary-{day}.xlsx"
            if day != today_et and dropbox_exists(client, token, path):
                continue  # completed day already published — leave it
            day_rows = fetch_day(client, day, with_time_raw=True)
            if not sheet_format.prestart_rows(day_rows):
                continue  # no classes have started yet
            data = sheet_format.prestart_xlsx_bytes(day_rows)  # one tab per club
            dropbox_upload(client, token, path, data)

        # summary.xlsx — running overview: one row per club per day with that
        # day's final totals. Built from the same per-day chunks (light query,
        # no raw JSON needed since the summary has no Time column).
        sum_rows: list[dict] = []
        for day in days:
            sum_rows.extend(fetch_day(client, day, with_time_raw=False))
        summary = sheet_format.summary_xlsx_bytes(sum_rows, earliest=EARLIEST_DAY)
        dropbox_upload(client, token, f"{SHARE_DIR}/summary.xlsx", summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
