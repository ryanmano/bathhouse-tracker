"""Othership scraper — Mariana Tek customer API, plus optional Booko enrichment.

Recon: recon/othership.md. Public, unauthenticated. Each Mariana "class" is one
bookable session/occurrence. Price is NOT exposed on any public endpoint
(documented recon finding) — the Booko widget API adds a per-session credit
`incentive` (a demand-tier signal, not the purchase price) which we surface
as `price_tier` when present.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import normalize
except ImportError:  # running as a script: repo root not on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import normalize

from normalize import log, make_record

PLATFORM = "mariana_tek"


def _date_window(horizon_days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return today.isoformat(), (today + timedelta(days=horizon_days)).isoformat()


def _normalize_session(brand: str, item: dict[str, Any], source_url: str) -> dict:
    location = item.get("location") or {}
    instructors = item.get("instructors") or []
    names = [i.get("name") for i in instructors if isinstance(i, dict) and i.get("name")]
    instructor = ", ".join(names) or None

    available = item.get("available_spot_count")

    return make_record(
        brand=brand,
        platform=PLATFORM,
        session_id=item["id"],
        location=location.get("name"),
        class_name=item.get("name"),
        start_time=item.get("start_datetime"),
        instructor=instructor,
        capacity=item.get("capacity"),
        spots_available=available,
        price=None,  # not exposed publicly by Mariana Tek — see recon/othership.md §4
        currency=location.get("currency_code") or "USD",
        is_waitlist=available == 0,
        source_url=source_url,
        raw=item,
    )


def _fetch_booko_incentives(
    client: Any, cfg: dict, date_from: str, date_to: str
) -> dict[str, str]:
    """Map Mariana session id -> incentive label from the Booko widget API.

    Best-effort: any failure is logged and returns {} (enrichment is optional).
    """
    booko = cfg.get("booko") or {}
    if not booko.get("enabled"):
        return {}
    try:
        url = (
            f"{booko['api_base']}/api/enterprise/{booko['org']}/marianatek/schedule"
            f"?startDate={date_from}&endDate={date_to}"
            f"&mtSubdomain={cfg['subdomain']}&maxPages=10"
        )
        payload = normalize.get_json(
            client, url, headers={"x-api-key": booko["api_key"]}
        )
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            log.warning("booko: unexpected response shape (keys=%s)",
                        list(payload)[:10] if isinstance(payload, dict) else type(payload))
            return {}
        incentives: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            incentive = row.get("incentive")
            if isinstance(incentive, dict) and incentive.get("label"):
                incentives[str(row.get("id"))] = str(incentive["label"])
        log.info("booko: %d rows, %d with incentives", len(rows), len(incentives))
        return incentives
    except Exception:
        log.warning("booko enrichment failed; continuing without it", exc_info=True)
        return {}


def scrape(brand: str, cfg: dict) -> list[dict]:
    horizon_days = cfg.get("horizon_days", 14)
    date_from, date_to = _date_window(horizon_days)
    subdomain = cfg["subdomain"]

    base_url = (
        f"https://{subdomain}.marianatek.com/api/customer/v1/classes"
        f"?min_start_date={date_from}&max_start_date={date_to}"
        f"&region={cfg['region_id']}&page_size=500&ordering=start_datetime"
    )

    records: list[dict] = []
    with normalize.new_client() as client:
        url: str | None = base_url
        page = 0
        skipped_cancelled = 0
        while url:
            page += 1
            payload = normalize.get_json(client, url)
            results = payload.get("results") or []
            ok = 0
            for item in results:
                if item.get("is_cancelled"):
                    skipped_cancelled += 1
                    continue
                try:
                    records.append(_normalize_session(brand, item, base_url))
                    ok += 1
                except Exception:
                    log.warning(
                        "%s: skipping malformed session %r",
                        brand, item.get("id"), exc_info=True,
                    )
            log.info(
                "%s: page %d — %d results, normalized %d (%s to %s)",
                brand, page, len(results), ok, date_from, date_to,
            )
            url = payload.get("next")
        if skipped_cancelled:
            log.info("%s: skipped %d cancelled sessions", brand, skipped_cancelled)

        # Optional Booko enrichment: credit incentive -> price_tier.
        incentives = _fetch_booko_incentives(client, cfg, date_from, date_to)
        enriched = 0
        for rec in records:
            label = incentives.get(rec["session_id"])
            if label:
                rec["price_tier"] = f"incentive: {label}"
                enriched += 1
        log.info("%s: %d/%d records enriched with booko incentives",
                 brand, enriched, len(records))
    return records


if __name__ == "__main__":
    import json
    import logging

    import yaml

    logging.basicConfig(level=logging.INFO)
    root = Path(__file__).resolve().parent.parent
    brands = yaml.safe_load((root / "config" / "brands.yaml").read_text())
    cfg = dict(brands["brands"]["othership"])
    cfg["horizon_days"] = 3
    recs = scrape("othership", cfg)
    print(json.dumps(recs[:3], indent=2))
    print(f"total records: {len(recs)}")
