#!/usr/bin/env python3
"""Delete snapshot rows older than the retention window (default 21 days).

Runs after each scrape so the table stays bounded. Deletes are done by primary
key (indexed) in batches — the id boundary for the cutoff date is found by
binary search using cheap PK lookups — so this never triggers the full-table
scan timeouts that an `observed_at < cutoff` delete would.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import normalize
import sheet_format
from run import load_dotenv

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "21"))
BATCH = 20000


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    if not os.environ.get("SUPABASE_URL"):
        print("SUPABASE_URL unset — skipping prune")
        return 0
    key = os.environ["SUPABASE_SERVICE_KEY"]
    base = {"apikey": key, "Authorization": f"Bearer {key}"}
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/snapshots"
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    with normalize.new_client() as c:
        def obs_at(i: int):
            r = normalize.get_json(
                c, url, params=[("select", "observed_at"), ("id", f"eq.{i}")],
                headers=base, retries=4,
            )
            return sheet_format._parse(r[0]["observed_at"]) if r else None

        first = normalize.get_json(
            c, url, params=[("select", "id"), ("order", "id.asc"), ("limit", "1")],
            headers=base, retries=4,
        )
        if not first:
            print("table empty — nothing to prune")
            return 0
        lo = first[0]["id"]
        hi = normalize.get_json(
            c, url, params=[("select", "id"), ("order", "id.desc"), ("limit", "1")],
            headers=base, retries=4,
        )[0]["id"]

        # smallest id whose observed_at >= cutoff (observed_at is monotonic in id)
        a, b = lo, hi
        while a < b:
            m = (a + b) // 2
            o = obs_at(m)
            if o is None or o < cutoff:
                a = m + 1
            else:
                b = m
        boundary = a
        if boundary <= lo:
            print(f"nothing older than {cutoff.date()} (retention {RETENTION_DAYS}d)")
            return 0

        del_headers = {**base, "Prefer": "return=minimal"}
        removed = 0
        x = lo
        while x < boundary:
            y = min(x + BATCH, boundary)
            for attempt in range(4):
                r = c.delete(
                    url, params=[("id", f"gte.{x}"), ("id", f"lt.{y}")], headers=del_headers
                )
                if r.status_code in (200, 204):
                    break
                time.sleep(2 * (attempt + 1))
            else:
                print(f"failed to delete id [{x},{y}) after retries: {r.status_code}")
                return 1
            removed += y - x
            x = y
        print(f"pruned ~{removed:,} rows older than {cutoff.date()} (kept {RETENTION_DAYS}d)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
