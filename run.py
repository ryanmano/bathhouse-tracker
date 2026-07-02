#!/usr/bin/env python3
"""Orchestrator: fetch all brands -> normalize -> insert into Supabase -> summarize.

Usage:
    python run.py --dry-run              # print sample records, write nothing
    python run.py                        # full run (needs SUPABASE_URL / SUPABASE_SERVICE_KEY)
    python run.py --brands bathhouse     # subset of brands
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
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

    for brand, cfg in configs.items():
        try:
            records = scrape_brand(brand, cfg)
            for rec in records:
                rec["observed_at"] = observed_at
            counts[brand] = len(records)
            all_records.extend(records)
            log.info("%s: %d sessions", brand, len(records))
            if args.dry_run:
                print(f"\n=== {brand}: {len(records)} sessions (sample) ===")
                print_samples(brand, records)
        except Exception as exc:  # one brand failing must not abort the others
            errors[brand] = f"{type(exc).__name__}: {exc}"
            log.error("%s: FAILED — %s", brand, errors[brand])

    if not args.dry_run and all_records:
        insert_supabase(all_records)

    print(f"\n=== run summary ({observed_at}) ===")
    for brand in configs:
        status = f"{counts[brand]} sessions" if brand in counts else f"ERROR — {errors[brand]}"
        print(f"  {brand:10s} {status}")
    mode = "dry-run, nothing written" if args.dry_run else f"{len(all_records)} records written"
    print(f"  total      {len(all_records)} records ({mode})")

    # partial success is OK (exit 0); all-brands failure is a real failure
    return 1 if errors and not counts else 0


if __name__ == "__main__":
    sys.exit(main())
