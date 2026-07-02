"""Shared helpers and the raw -> common-schema contract.

Every scraper module in scrapers/ exposes:

    scrape(brand: str, cfg: dict) -> list[dict]

returning one dict per upcoming bookable session, built with make_record().
run.py stamps observed_at on every record and writes the batch to Supabase.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("scrape")

USER_AGENT = (
    "tenancy-market-research/0.1 "
    "(contact: ryan@usetenancy.com; low-volume hourly schedule snapshots)"
)

SCHEMA_FIELDS = [
    "observed_at", "brand", "platform", "location", "session_id", "class_name",
    "start_time", "instructor", "capacity", "spots_available", "spots_booked",
    "price", "price_tier", "currency", "is_waitlist", "source_url", "raw",
]


def to_utc_iso(value: Any) -> str | None:
    """Coerce an epoch (seconds) or ISO-8601 string (offset or Z) to a UTC ISO string."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            s = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        log.warning("unparseable datetime value: %r", value)
        return None


def make_record(
    *,
    brand: str,
    platform: str,
    session_id: Any,
    location: str | None = None,
    class_name: str | None = None,
    start_time: Any = None,
    instructor: str | None = None,
    capacity: int | None = None,
    spots_available: int | None = None,
    price: float | None = None,
    price_tier: str | None = None,
    currency: str = "USD",
    is_waitlist: bool = False,
    source_url: str = "",
    raw: dict | None = None,
) -> dict:
    """Build one normalized snapshot record. Missing values stay None — never raise."""
    spots_booked = None
    if capacity is not None and spots_available is not None:
        spots_booked = max(capacity - spots_available, 0)
    return {
        "brand": brand,
        "platform": platform,
        "location": location,
        "session_id": str(session_id),
        "class_name": class_name,
        "start_time": to_utc_iso(start_time),
        "instructor": instructor,
        "capacity": capacity,
        "spots_available": spots_available,
        "spots_booked": spots_booked,
        "price": price,
        "price_tier": price_tier,
        "currency": currency,
        "is_waitlist": bool(is_waitlist),
        "source_url": source_url,
        "raw": raw or {},
    }


def new_client(**kwargs: Any) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
        **kwargs,
    )


TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 2.0,
    **kwargs: Any,
) -> Any:
    """HTTP request with exponential-backoff retries on transient failures."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.request(method, url, **kwargs)
            if resp.status_code in TRANSIENT_STATUSES:
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status not in TRANSIENT_STATUSES:
                raise
            last_exc = exc
            if attempt < retries:
                sleep = backoff * (2**attempt)
                log.warning(
                    "transient failure %s %s (%s); retry in %.1fs",
                    method, url, exc, sleep,
                )
                time.sleep(sleep)
    assert last_exc is not None
    raise last_exc


def get_json(client: httpx.Client, url: str, **kwargs: Any) -> Any:
    return request_json(client, "GET", url, **kwargs)
