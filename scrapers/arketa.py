"""Arketa scraper — brand: lore (Lore Bathing Club).

Arketa's booking widget (embedded on lorebathingclub.com/bookings as
`app.arketa.co/iframe/lorebathingclub/schedule`) is a CRA SPA on Google
Firebase project `sutra-prod` (Arketa codename "Sutra").

WHY THIS MODULE DOES NOT USE FIRESTORE DIRECTLY
------------------------------------------------
recon/lore.md hypothesized that the widget reads Firestore directly using a
Firebase *anonymous* ID token (accounts:signUp -> :runQuery). Verified live on
2026-07-02, that path does NOT work:

  * The anonymous token mints fine, but a `:runQuery` on the `users` collection
    (to resolve slug -> partnerId) returns 403 PERMISSION_DENIED — the security
    rules do not permit anonymous clients to query `users`.
  * A `:runQuery` on the `classes` collection returns 400 FAILED_PRECONDITION
    "The query requires an index" — even the minimal `partnerId` + `start_time`
    composite. The required composite indexes are not published on sutra-prod
    and a public client cannot create them, so no `classes` runQuery can succeed.

The widget's ACTUAL public mechanism (confirmed in the JS bundle and live) is an
*unauthenticated* REST layer on `app.arketa.co/api/widget/*` that proxies
Firestore server-side. That is what this module uses — no token, no account, no
login, low volume (2 GETs per run):

  1. GET /api/widget/exists?widgetName=<slug>   -> {"partnerId": "..."}
     (supports a cfg["partner_id"] override to skip this call).
  2. GET /api/widget/partners/<partnerId>/classes?start=<epoch>&end=<epoch>
     -> {"partnerData": {...}, "classes": [ {plain-JSON class}, ... ]}
     start_time/end_time are UNIX epoch SECONDS; the endpoint honors the window.

Then filter visibility client-side and normalize per make_record().
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

# Ensure the repo root (which holds normalize.py) is importable whether this
# module is imported by run.py or executed directly as `python3 scrapers/arketa.py`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import normalize
from normalize import log, make_record

PLATFORM = "arketa"
DEFAULT_API_BASE = "https://app.arketa.co/api"


# --------------------------------------------------------------------------- #
# HTTP steps
# --------------------------------------------------------------------------- #
def resolve_partner_id(client, api_base: str, slug: str) -> str | None:
    """Resolve partnerId from the slug via GET /widget/exists?widgetName=<slug>."""
    url = f"{api_base}/widget/exists"
    data = normalize.request_json(
        client, "GET", url, params={"widgetName": slug}
    )
    if not isinstance(data, dict):
        return None
    if not data.get("exists"):
        return None
    partner_id = data.get("partnerId")
    return partner_id or None


def fetch_classes(
    client, api_base: str, partner_id: str, start_epoch: int, end_epoch: int
) -> list[dict]:
    """GET the schedule for one partner within [start_epoch, end_epoch] (seconds)."""
    url = f"{api_base}/widget/partners/{partner_id}/classes"
    data = normalize.request_json(
        client, "GET", url, params={"start": start_epoch, "end": end_epoch}
    )
    if not isinstance(data, dict):
        return []
    classes = data.get("classes")
    return classes if isinstance(classes, list) else []


# --------------------------------------------------------------------------- #
# Filtering & normalization
# --------------------------------------------------------------------------- #
def is_visible(cls: dict[str, Any]) -> bool:
    """Keep only bookable, publicly-visible sessions.

    Verified field values on live Lore data: `canceled`/`deleted` booleans and
    `display` in {"public","private"}.
    """
    if cls.get("canceled") is True:
        return False
    if cls.get("deleted") is True:
        return False
    display = cls.get("display")
    if isinstance(display, str) and display.lower() != "public":
        return False
    return True


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _location_label(cls: dict[str, Any]) -> str | None:
    """Human-readable location. `location` is a dict on live data; `room`/
    `location_name` are opaque ids, so prefer the nested name/address."""
    loc = cls.get("location")
    if isinstance(loc, dict):
        label = _first(loc, "name", "address", "line1", "cityState", "city")
        if isinstance(label, str) and label.strip():
            return label.strip()
    for key in ("location", "room"):
        v = cls.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _instructor_label(cls: dict[str, Any]) -> str | None:
    """Best-effort instructor/host label.

    NOTE: on live Lore data `instructor_name`/`host_name` are sometimes used as
    session labels (e.g. "Aufguss Time: 5:30 PM") rather than a person's name —
    reported as a field-mapping caveat. We surface the value as-is.
    """
    val = _first(cls, "instructor", "instructor_name", "host_name", "hostName")
    if isinstance(val, str) and val.strip():
        return val.strip()
    hosts = cls.get("hosts")
    if isinstance(hosts, list) and hosts:
        names: list[str] = []
        for h in hosts:
            if isinstance(h, str) and h.strip():
                names.append(h.strip())
            elif isinstance(h, dict):
                n = _first(h, "name", "full_name", "displayName", "first_name")
                if isinstance(n, str) and n.strip():
                    names.append(n.strip())
        if names:
            return ", ".join(names)
    return None


def _price(cls: dict[str, Any]) -> float | None:
    """Price in DOLLARS.

    Verified against live Lore data: `price` takes values like 0 / 25 / 55 for
    bath-house sessions — i.e. whole dollars, NOT cents. Passed through as-is.
    `minimum_price` was uniformly 0 and is ignored.
    """
    val = _first(cls, "price", "amount")
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.replace("$", "").replace(",", "").strip())
        except ValueError:
            return None
    return None


def normalize_class(brand: str, slug: str, cls: dict[str, Any]) -> dict:
    capacity = _as_int(cls.get("max_capacity"))
    booked = _as_int(cls.get("total_booked"))

    spots_available: int | None = None
    if capacity is not None and booked is not None:
        spots_available = max(capacity - booked, 0)

    # A session is effectively waitlist-only when it is full.
    is_waitlist = False
    if spots_available is not None and spots_available <= 0:
        is_waitlist = True
    waitlist_len = _as_int(cls.get("waitlistLength"))
    if waitlist_len is not None and waitlist_len > 0:
        is_waitlist = True

    return make_record(
        brand=brand,
        platform=PLATFORM,
        session_id=cls.get("id"),
        location=_location_label(cls),
        class_name=_first(cls, "class_name", "name", "title"),
        start_time=cls.get("start_time"),  # epoch seconds; to_utc_iso handles int
        instructor=_instructor_label(cls),
        capacity=capacity,
        spots_available=spots_available,
        price=_price(cls),
        currency="USD",
        is_waitlist=is_waitlist,
        source_url=f"https://app.arketa.co/iframe/{slug}/schedule",
        raw=cls,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def scrape(brand: str, cfg: dict) -> list[dict]:
    """Scrape upcoming Lore/Arketa classes. Returns [] cleanly when empty."""
    horizon_days = cfg.get("horizon_days", 14)
    slug = cfg["slug"]
    api_base = cfg.get("api_base", DEFAULT_API_BASE).rstrip("/")

    now_epoch = int(time.time())
    end_epoch = now_epoch + int(horizon_days) * 86400

    records: list[dict] = []
    with normalize.new_client() as client:
        # Step 1: resolve partnerId (allow override to skip the lookup).
        partner_id = cfg.get("partner_id")
        if partner_id:
            log.info("using partner_id override from cfg: %s", partner_id)
        else:
            partner_id = resolve_partner_id(client, api_base, slug)
            if not partner_id:
                log.warning(
                    "partnerId resolution for slug=%r returned nothing; returning []",
                    slug,
                )
                return []
            log.info("resolved partnerId=%s for slug=%s", partner_id, slug)

        # Step 2: fetch the schedule window.
        classes = fetch_classes(client, api_base, partner_id, now_epoch, end_epoch)
        log.info("fetched %d raw class(es) for next %d day(s)", len(classes), horizon_days)

        if not classes:
            log.info(
                "no upcoming classes for slug=%s in next %d day(s) "
                "(an empty schedule is normal); returning []",
                slug,
                horizon_days,
            )
            return []

        # Step 3: filter visibility, then normalize per session with a per-item
        # try/except so one bad doc never crashes the run.
        visible = [c for c in classes if isinstance(c, dict) and is_visible(c)]
        log.info("visibility filter kept %d/%d class(es)", len(visible), len(classes))

        for cls in visible:
            try:
                records.append(normalize_class(brand, slug, cls))
            except Exception:  # noqa: BLE001
                log.exception(
                    "failed to normalize class id=%r",
                    cls.get("id") if isinstance(cls, dict) else cls,
                )

    log.info("arketa/%s produced %d record(s)", brand, len(records))
    return records


if __name__ == "__main__":
    import json
    import logging

    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with open("config/brands.yaml") as fh:
        conf = yaml.safe_load(fh)

    defaults = conf.get("defaults", {})
    lore_cfg = {**defaults, **conf["brands"]["lore"]}

    results = scrape("lore", lore_cfg)
    print(f"\n=== scrape('lore') -> {len(results)} record(s) ===")
    if not results:
        print("no sessions")
    else:
        for rec in results[:3]:
            print(json.dumps(rec, indent=2, default=str))
