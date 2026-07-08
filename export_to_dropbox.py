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

import collections
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


def sb_rows(client, filters: list[tuple[str, str]]) -> list[dict]:
    """Fetch snapshot rows from Supabase with pagination."""
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/snapshots"
    base = [
        ("select", ",".join(COLUMNS + sheet_format.EXTRA_SELECTS)),
        ("order", "observed_at.asc,brand.asc"),
    ]
    rows: list[dict] = []
    offset = 0
    while True:
        page = normalize.get_json(
            client,
            url,
            params=base + filters + [("limit", str(PAGE)), ("offset", str(offset))],
            headers=_sb_headers(),
        )
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE


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

        # 1) latest.xlsx — one row per session from the most recent snapshot.
        newest = latest_observed_at(client)
        if newest:
            rows = sb_rows(client, [("observed_at", f"eq.{newest}")])
            dropbox_upload(client, token, f"{SHARE_DIR}/latest.xlsx", sheet_bytes(rows))
        else:
            print("no snapshots in database yet; skipping latest.xlsx")

        # 1b) prestart-summary-YYYY-MM-DD.xlsx — one clean daily file, grouped
        #     by each class's Eastern start date. Today refreshes every run;
        #     past days are written once (backfilled up to BACKFILL_DAYS).
        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(days=BACKFILL_DAYS)).date().isoformat()
        recent = sb_rows(
            client,
            [
                ("start_time", f"gte.{window_start}"),
                ("start_time", f"lt.{now.isoformat()}"),
            ],
        )
        by_day: dict[str, list[dict]] = collections.defaultdict(list)
        for r in recent:
            st = sheet_format._parse(r.get("start_time"))
            if st is not None:
                by_day[st.astimezone(EASTERN).date().isoformat()].append(r)

        today_et = now.astimezone(EASTERN).date().isoformat()
        for day, day_rows in sorted(by_day.items()):
            if day < EARLIEST_DAY:
                continue  # off-timing early days intentionally excluded
            path = f"{SHARE_DIR}/prestart-summary-{day}.xlsx"
            if day != today_et and dropbox_exists(client, token, path):
                continue  # completed days are final — write once
            if not sheet_format.prestart_rows(day_rows):
                continue  # no classes have started yet today
            data = sheet_format.prestart_xlsx_bytes(day_rows)  # 3 tabs, one/brand
            dropbox_upload(client, token, path, data)

        # Remove the previous single rolling summary if it lingers (best-effort).
        try:
            client.post(
                "https://api.dropboxapi.com/2/files/delete_v2",
                headers={"Authorization": f"Bearer {token}"},
                json={"path": "/prestart-summary.xlsx"},
            )
        except Exception:
            pass

        # 2) One permanent file per completed UTC day (backfill missed days).
        today = datetime.now(timezone.utc).date()
        for delta in range(1, BACKFILL_DAYS + 1):
            day = today - timedelta(days=delta)
            if day.isoformat() < EARLIEST_DAY:
                continue  # off-timing early days intentionally excluded
            path = f"{SHARE_DIR}/bathhouse-tracker-{day.isoformat()}.xlsx"
            if dropbox_exists(client, token, path):
                continue
            day_rows = sb_rows(
                client,
                [
                    ("observed_at", f"gte.{day.isoformat()}"),
                    ("observed_at", f"lt.{(day + timedelta(days=1)).isoformat()}"),
                ],
            )
            if not day_rows:
                continue  # day predates data collection
            dropbox_upload(client, token, path, sheet_bytes(day_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
