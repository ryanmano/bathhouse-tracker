"""Bathhouse (NYC) scraper — Trybe (try.be) shopfront schedule API.

Recon: recon/bathhouse.md. Public, unauthenticated. One flat `data[]` array
per location/date-range request — no pagination. Location is scoped by the
subdomain host; `site_id` is the shared org UUID from config.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import normalize
except ImportError:  # running as a script: repo root not on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import normalize
from normalize import log, make_record

PLATFORM = "trybe"


def _date_window(horizon_days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return today.isoformat(), (today + timedelta(days=horizon_days)).isoformat()


def _normalize_session(
    brand: str, session: dict[str, Any], location_name: str, source_url: str
) -> dict:
    price_cents = session.get("price")
    price: float | None = None
    if price_cents is not None:
        price = round(price_cents / 100, 2)

    practitioner = session.get("practitioner")
    instructor: str | None = None
    if isinstance(practitioner, dict):
        instructor = practitioner.get("name")

    session_type = session.get("session_type") or {}
    capacity = session.get("capacity")
    remaining = session.get("remaining_capacity")
    is_waitlist = bool(session.get("waitlist_enabled")) and remaining == 0

    return make_record(
        brand=brand,
        platform=PLATFORM,
        session_id=session["id"],
        location=location_name,
        class_name=session_type.get("name"),
        start_time=session.get("start_time"),
        instructor=instructor,
        capacity=capacity,
        spots_available=remaining,
        price=price,
        currency="USD",
        is_waitlist=is_waitlist,
        source_url=source_url,
        raw=session,
    )


def scrape(brand: str, cfg: dict) -> list[dict]:
    horizon_days = cfg.get("horizon_days", 14)
    date_from, date_to = _date_window(horizon_days)
    site_id = cfg["site_id"]
    locations: list[dict[str, Any]] = cfg["locations"]

    records: list[dict] = []
    with normalize.new_client() as client:
        for i, loc in enumerate(locations):
            if i > 0:
                time.sleep(1)
            name = loc["name"]
            url = (
                f"https://{loc['subdomain']}.try.be/api/schedule"
                f"?site_id={site_id}&date_from={date_from}&date_to={date_to}"
            )
            payload = normalize.get_json(client, url)
            sessions = payload.get("data") or []

            ok = 0
            for session in sessions:
                try:
                    records.append(_normalize_session(brand, session, name, url))
                    ok += 1
                except Exception:
                    log.warning(
                        "%s/%s: skipping malformed session %r",
                        brand, name, session.get("id"), exc_info=True,
                    )
            log.info(
                "%s/%s: fetched %d sessions, normalized %d (%s to %s)",
                brand, name, len(sessions), ok, date_from, date_to,
            )
    return records


if __name__ == "__main__":
    import json
    import logging

    import yaml

    logging.basicConfig(level=logging.INFO)
    root = Path(__file__).resolve().parent.parent
    brands = yaml.safe_load((root / "config" / "brands.yaml").read_text())
    cfg = dict(brands["brands"]["bathhouse"])
    cfg["horizon_days"] = 3
    recs = scrape("bathhouse", cfg)
    print(json.dumps(recs[:3], indent=2))
    print(f"total records: {len(recs)}")
