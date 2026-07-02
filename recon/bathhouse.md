# Bathhouse (NYC) — Booking Schedule API Recon

Date: 2026-07-02 · Contact: ryan@usetenancy.com
Scope: PUBLIC, unauthenticated schedule data only. No accounts, no login, no auth bypass. Low request volume.

## TL;DR

- **Platform is NOT Mariana Tek.** The starting intel was wrong. `bathhouse.marianatek.com` returns "Page not found". The booking platform is **Trybe** (try.be), a UK spa/wellness booking SaaS. The marketing site footer / widget footer both say "Powered by Trybe".
- The marketing site `abathhouse.com` is a **Squarespace** site. Its "Book" buttons link out to per-location **`*.try.be`** shopfront apps.
- Live, unauthenticated schedule data (including **price, capacity, and remaining spots**) is retrievable from `GET https://{location}.try.be/api/schedule`.
- **Price IS in the schedule list response** as an integer minor-currency amount (USD cents). No separate quote/pricing call is needed for the drop-in day-pass price.
- The `/api/schedule` endpoint only returns **Day Pass** class-sessions. Treatments (Massage / Couples / Packs) use a separate practitioner appointment-slot flow (not covered by this endpoint).

## Platform confirmation

- Shopfront app: Laravel + Vite + Vue/React SPA, hosted on **Laravel Vapor** (response header `x-vapor-base64-encode: True`), fronted by **Cloudflare** (`server: cloudflare`).
- App bundle: `https://bathhouseflatiron.try.be/build/assets/app-1t1oAD2o.js` (~6.9 MB).
- Two API bases seen in the bundle:
  - `https://api.try.be/shop` — the **admin/management** API (JSON:API-ish, mostly auth-required; hundreds of `/shop/...` and `/customers/...`, `/admin/...` routes). NOT used for public schedule.
  - `window.location.origin` (i.e. the `{location}.try.be` subdomain itself) — serves the **public shopfront** `/api/...` routes, including `/api/schedule` and `/api/my-visit/basket/...`.

## Locations (all 3 confirmed live, HTTP 200)

| Location    | Subdomain                          |
|-------------|------------------------------------|
| Flatiron    | `https://bathhouseflatiron.try.be`     |
| Williamsburg| `https://bathhousewilliamsburg.try.be` |
| Atlantic Ave| `https://bathhouseatlanticave.try.be`  |

**Location scoping is done by the subdomain host, not by a param.** The same `site_id` value returns each location's own schedule when sent to that location's subdomain.

Identifiers found in the Flatiron widget HTML (`data-categories` prop, decoded from HTML entities):
- **organisation_id** = `9b5df0bd-ad8d-48ce-8714-0ae9967406e9` (this is the value the widget passes as `site_id`; shared across all 3 NYC locations).
- Category ids (Mongo ObjectIds):
  - Day Pass = `661d5d567bf690aa890c3e25`
  - Massage  = `65d37a0ae7b9ad4f61073b4f`
  - Couples  = `65d37a5ebfed333a940563f5`
  - Packs    = `69160bd69abf4eae440e2c00`
- CDN media prefixes seen: `cdn.try.be/34520/` (logo), `cdn.try.be/67622/` (day-pass image). These are media-library ids, not site ids.

## Working endpoint — schedule

`GET https://{location}.try.be/api/schedule`

Required query params (validated server-side, 422 if missing):
- `site_id` — must be present/non-null. **Value is NOT actually validated for scoping** — a bogus UUID (`00000000-0000-0000-0000-000000000000`) returns the same data. Use the real org UUID `9b5df0bd-ad8d-48ce-8714-0ae9967406e9` to be safe.
- `date_from` — start date. Accepts `YYYY-MM-DD` (client sends full ATOM/ISO; date-only works).
- `date_to` — end date, `YYYY-MM-DD`.

Optional query params (from the JS client `getScheduleRaw`):
- `category_ids` — CSV of category ObjectIds.
- `practitioner_ids` — CSV of practitioner ids.

Headers: none required. `Accept: application/json` is polite. `Authorization: Bearer <token>` is *optional* — the client only adds it when a customer is logged in; anonymous requests return full public schedule + prices.

Copy-pasteable working curl commands:

```bash
UA="market-research-recon/0.1 (contact: ryan@usetenancy.com)"
SID="9b5df0bd-ad8d-48ce-8714-0ae9967406e9"

# Flatiron, one day
curl -s -A "$UA" -H "Accept: application/json" \
  "https://bathhouseflatiron.try.be/api/schedule?site_id=$SID&date_from=2026-07-03&date_to=2026-07-04"

# Williamsburg
curl -s -A "$UA" -H "Accept: application/json" \
  "https://bathhousewilliamsburg.try.be/api/schedule?site_id=$SID&date_from=2026-07-03&date_to=2026-07-04"

# Atlantic Ave
curl -s -A "$UA" -H "Accept: application/json" \
  "https://bathhouseatlanticave.try.be/api/schedule?site_id=$SID&date_from=2026-07-03&date_to=2026-07-04"

# Filter to a category (e.g. Day Pass)
curl -s -A "$UA" \
  "https://bathhouseflatiron.try.be/api/schedule?site_id=$SID&date_from=2026-07-03&date_to=2026-07-04&category_ids=661d5d567bf690aa890c3e25"
```

## Pagination & horizon

- **No pagination.** The response is a single flat `data` array covering the entire requested date range. Examples: 1 day = 52 items; 30 days = 806 items; ~180 days (Jul–Dec 2026) = 4,680 items in one response. No `meta`/`links` pagination block, no `per_page` honored on this endpoint.
- **Horizon:** schedule is published far ahead. A single-day query for `2027-01-03` (6 months out) still returned 52 real bookable sessions. Practical limit not hit within tested ranges.
- No hard cap observed on `date_to` range (Jul–Dec returned 200 OK). Recommend chunking by month anyway to keep responses small.

## Response shape & exact field paths

Top-level keys: `data` (array of sessions), `categories` (array present in the range), `practitioners` (array; empty for day passes).

Per-session object (`data[]`):

| Data point            | JSON path                              | Notes |
|-----------------------|----------------------------------------|-------|
| Session id            | `data[].id`                            | Mongo ObjectId string, e.g. `69c436c2d988aceaa90a07c3` |
| Class/session name    | `data[].session_type.name`             | e.g. `"🎟️ Book Day Pass, Browse Prices"` |
| Session type id       | `data[].session_type.id`               | e.g. `674df9e8dc9171673c01bbe4` |
| Description           | `data[].session_type.description`      | HTML string |
| Image                 | `data[].session_type.image.url` / `.original_url` | |
| Start datetime + tz   | `data[].start_time`                    | ISO-8601 **with offset**, e.g. `2026-07-03T08:00:00-04:00` (EDT). No separate tz field; offset is embedded (`-04:00` summer / `-05:00` winter). |
| End datetime + tz     | `data[].end_time`                      | same format |
| Location / venue      | *(none in payload)*                    | Location is implicit in the **subdomain host** you queried. No venue field per session. |
| Instructor / staff    | `data[].practitioner`                  | `null` for day-pass sessions; populated for practitioner-led sessions. |
| Capacity (total)      | `data[].capacity`                      | integer, e.g. `60` |
| Spots available       | `data[].remaining_capacity`            | integer, e.g. `46` (booked = `capacity - remaining_capacity`) |
| **Price**             | `data[].price`                         | **Integer, minor units (USD cents).** e.g. `5900` = $59.00. |
| Waitlist              | `data[].waitlist_enabled`              | boolean |

Currency: **USD** (confirmed via `USD` in the JS bundle; no per-item currency field — prices are USD cents).

### Where the price lives (investigated)

- **The drop-in price is in the schedule list itself** (`data[].price`, integer cents). No per-session quote/checkout call is required to read it.
- Day Pass uses **time-based pricing** — that is why each time-slot session has its own `price`. Observed Flatiron day-pass range across a 30-day window: **5400–10400 cents ($54–$104)** depending on time/peak. Williamsburg first slot `11400`, Atlantic Ave `4900` on the sampled day.
- The session_type name literally reads "Browse Prices" — additional price *tiers* (packages/memberships, member rates) are **not** in the schedule payload. Those live behind:
  - the checkout/basket flow: `POST https://{location}.try.be/api/my-visit/basket/add-item` (and `/basket/add-package-item/{basketItemId}`) — starts a basket; **not exercised** (would initiate a checkout/basket, borderline vs. guardrails).
  - the admin `api.try.be/shop` API: `/shop/packages`, `/shop/appointment-types/{id}/price-rules`, `/shop/course-types/{id}/price-rules` — **auth-required**, not public.
- Bottom line: for competitive drop-in pricing, `data[].price` from `/api/schedule` is sufficient and authoritative for day passes. Package/membership pricing is not exposed unauthenticated via a simple GET.

### Treatments (Massage / Couples / Packs)

`/api/schedule` returned **only Day Pass** sessions across all tested ranges (`practitioner` always null; only the Day Pass category appears in `categories`). Treatments are practitioner appointments and use a **different flow** — appointment-type + slot endpoints seen in the bundle (`/shop/appointment-slots/{appointmentTypeId}/{date}`, `/shop/appointment-types`). Not investigated further here (out of scope for schedule/spot data, and would need the appointment-type ids).

## Rate limits & robots

- **No rate-limit headers** exposed on `/api/schedule` responses (no `x-ratelimit-*`, no `retry-after`). Response headers of note: `content-type: application/json`, `cache-control: no-cache, private`, `server: cloudflare`, `x-vapor-base64-encode: True`. Cloudflare may throttle abusive volume — keep it low.
- **robots.txt:**
  - `https://www.abathhouse.com/robots.txt` — Squarespace default. Disallows `/config`, `/search`, `/account`, `/api/` (allows `/api/ui-extensions/`), `/static/`. Explicitly names many AI crawlers (anthropic-ai, ClaudeBot, GPTBot, etc.) — but note that block list has no `Disallow` rules under those UA groups in the excerpt; the effective `*` rules are the disallows above. Booking data does not live here anyway.
  - `https://{location}.try.be/robots.txt` — **no robots.txt served**; the path returns the app's 404 HTML page (SPA catch-all). No crawl directives published for the shopfront. Treated as absence of explicit restriction; kept volume minimal regardless.

## Sample response

Full sample saved: `/Users/ryanmanocherian/Jonathan/recon/bathhouse_sample.json`
(Flatiron, `date_from=2026-07-03&date_to=2026-07-04`, 52 sessions.)

Trimmed snippet:

```json
{
  "data": [
    {
      "id": "69c436c2d988aceaa90a07c3",
      "capacity": 60,
      "start_time": "2026-07-03T08:00:00-04:00",
      "end_time": "2026-07-03T08:30:00-04:00",
      "practitioner": null,
      "price": 5900,
      "remaining_capacity": 46,
      "waitlist_enabled": false,
      "session_type": {
        "id": "674df9e8dc9171673c01bbe4",
        "name": "🎟️ Book Day Pass, Browse Prices",
        "description": "<div><p>Look, feel and perform your very best ...</p></div>",
        "image": { "url": "https://cdn.try.be/67622/conversions/...-thumbnail@2x.jpg" }
      }
    }
  ],
  "categories": [ { "name": "Day Pass", "id": "661d5d567bf690aa890c3e25" } ],
  "practitioners": []
}
```

## Open questions

- **Dynamic pricing:** day-pass `price` varies per time slot (peak/off-peak). Unclear whether it also varies by real-time demand/remaining capacity or is a fixed time-of-day rate card — would need to snapshot the same slot over time to tell. Member/package/promo pricing is not visible unauthenticated.
- Whether treatment (massage/couples) prices and availability are reachable unauthenticated via the appointment-slots endpoints (needs appointment-type ids; not tested).
- Exact far-future horizon cap not pinned down (data exists at least into 2027).
