#!/usr/bin/env python3
"""Orchestrator: fetch all brands -> normalize -> insert into Supabase -> summarize.

Usage:
    python run.py --dry-run              # print sample records, write nothing
    python run.py                        # full run (needs SUPABASE_URL / SUPABASE_SERVICE_KEY)
    python run.py --brands bathhouse     # subset of brands
    python run.py --sweep                # pre-start sweep: only sessions about to begin
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import normalize
from normalize import log

ROOT = Path(__file__).resolve().parent

PLATFORM_MODULES = {
    "trybe": "scrapers.trybe",
    "mariana_tek": "scrapers.mariana_tek",
    "arketa": "scrapers.arketa",
}

INSERT_BATCH_SIZE = 500

# Pre-start sweep window: capture sessions starting up to this many minutes
# from now (and a small grace behind, in case the scheduled run fired late).
SWEEP_AHEAD_MIN = 8
SWEEP_BEHIND_MIN = 3


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); real env vars take precedence."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_brand_configs() -> dict[str, dict]:
    cfg = yaml.safe_load((ROOT / "config" / "brands.yaml").read_text())
    defaults = cfg.get("defaults") or {}
    return {name: {**defaults, **bc} for name, bc in (cfg.get("brands") or {}).items()}


def scrape_brand(brand: str, cfg: dict) -> list[dict]:
    module = importlib.import_module(PLATFORM_MODULES[cfg["platform"]])
    return module.scrape(brand, cfg)


def insert_supabase(records: list[dict]) -> None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (see .env.example)"
        )
    endpoint = (
        url.rstrip("/") + "/rest/v1/snapshots?on_conflict=brand,session_id,observed_at"
    )
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # ignore-duplicates makes re-runs idempotent on the unique index
        "Prefer": "resolution=ignore-duplicates,return=minimal",
    }
    with normalize.new_client() as client:
        for i in range(0, len(records), INSERT_BATCH_SIZE):
            batch = records[i : i + INSERT_BATCH_SIZE]
            for attempt in range(3):
                resp = client.post(endpoint, headers=headers, content=json.dumps(batch))
                if resp.status_code in normalize.TRANSIENT_STATUSES and attempt < 2:
                    wait = 2.0 * (2**attempt)
                    log.warning(
                        "supabase insert got %s; retrying in %.0fs", resp.status_code, wait
                    )
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"supabase insert failed ({resp.status_code}): {resp.text[:500]}"
                    )
                break
            log.info("inserted batch of %d records", len(batch))


def print_samples(brand: str, records: list[dict], n: int = 2) -> None:
    for rec in records[:n]:
        display = {k: v for k, v in rec.items() if k != "raw"}
        print(json.dumps(display, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print samples, write nothing")
    parser.add_argument("--brands", help="comma-separated subset of brands to run")
    parser.add_argument(
        "--local",
        action="store_true",
        help="store to the local SQLite db + daily CSV in ~/BathhouseData "
        "(this is also the automatic fallback when SUPABASE_URL is not set)",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="pre-start sweep: fetch a 1-day window and keep only sessions "
        f"starting within the next {SWEEP_AHEAD_MIN} minutes, to capture the "
        "final fill state right before class begins",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    load_dotenv(ROOT / ".env")

    configs = load_brand_configs()
    if args.brands:
        wanted = {b.strip() for b in args.brands.split(",")}
        unknown = wanted - configs.keys()
        if unknown:
            raise SystemExit(f"unknown brands: {sorted(unknown)}")
        configs = {b: c for b, c in configs.items() if b in wanted}

    observed_at = datetime.now(timezone.utc).isoformat()
    all_records: list[dict] = []
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}

    sweep_lo = sweep_hi = None
    if args.sweep:
        now = datetime.now(timezone.utc)
        sweep_lo = now - timedelta(minutes=SWEEP_BEHIND_MIN)
        sweep_hi = now + timedelta(minutes=SWEEP_AHEAD_MIN)

    for brand, cfg in configs.items():
        if args.sweep:
            # narrow fetch + skip optional enrichment: sweeps run often, keep them light
            cfg = {**cfg, "horizon_days": 1, "booko": {"enabled": False}}
        try:
            records = scrape_brand(brand, cfg)
            if args.sweep:
                records = [
                    r
                    for r in records
                    if r.get("start_time")
                    and sweep_lo <= datetime.fromisoformat(r["start_time"]) <= sweep_hi
                ]
            for rec in records:
                rec["observed_at"] = observed_at
                rec["raw"] = normalize.slim_raw(rec["raw"])
            counts[brand] = len(records)
            all_records.extend(records)
            log.info("%s: %d sessions", brand, len(records))
            if args.dry_run:
                print(f"\n=== {brand}: {len(records)} sessions (sample) ===")
                print_samples(brand, records)
        except Exception as exc:  # one brand failing must not abort the others
            errors[brand] = f"{type(exc).__name__}: {exc}"
            log.error("%s: FAILED — %s", brand, errors[brand])

    use_local = args.local or not os.environ.get("SUPABASE_URL")
    if use_local and os.environ.get("GITHUB_ACTIONS"):
        # A CI runner's local disk is discarded after the job — silent data loss.
        raise SystemExit(
            "running in GitHub Actions without SUPABASE_URL/SUPABASE_SERVICE_KEY "
            "secrets — refusing to fall back to local storage"
        )
    mode = "dry-run, nothing written"
    if not args.dry_run and all_records:
        if use_local:
            import local_store

            new_rows, _ = local_store.insert(all_records)
            csv_path = local_store.export_daily_csv(observed_at[:10])
            mode = f"{new_rows} new rows -> {local_store.DB_PATH}; spreadsheet: {csv_path}"
        else:
            insert_supabase(all_records)
            mode = f"{len(all_records)} records written to Supabase"

    print(f"\n=== run summary ({observed_at}) ===")
    for brand in configs:
        status = f"{counts[brand]} sessions" if brand in counts else f"ERROR — {errors[brand]}"
        print(f"  {brand:10s} {status}")
    print(f"  total      {len(all_records)} records ({mode})")

    # partial success is OK (exit 0); all-brands failure is a real failure
    return 1 if errors and not counts else 0


if __name__ == "__main__":
    sys.exit(main())
