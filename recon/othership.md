# Othership — Booking / Schedule API Recon

Date: 2026-07-02. Scope: PUBLIC, UNAUTHENTICATED data only. No accounts, no login, no auth bypass.
User-Agent used: `market-research-recon/0.1 (contact: ryan@usetenancy.com)`. Low request volume.

## 1. Platform confirmation

- **Confirmed: Mariana Tek** is the underlying booking platform.
  - Brand subdomain / slug: **`othership`** → API host **`https://othership.marianatek.com`**.
  - API namespace: `/api/customer/v1/` (Mariana Tek customer API), returns JSON, DRF-style pagination.
- **Schedule UI is a third-party widget: "Booko"** (`bookoapp.com`), loaded on the marketing site
  via `https://bookoapp.com/widget/booko-marianatek-overlay.js`. Booko proxies/normalizes the Mariana
  data and adds its own "account credit incentive" layer.
- Marketing site is Webflow (`cdn.prod.website-files.com`). The schedule embeds into a
  `<div data-mariana-integrations="/schedule/daily">` anchor which Booko targets.

Booko widget config (from `https://www.othership.us/schedule` HTML data-attributes):
```
data-org="othership"
data-mt-subdomain="othership"
data-api-base="https://bookoapp.com"
data-api-key="bk_live_f2cfe64a2e57c0c3dc6127c222bc4f28b0f734c910a90112"   # public widget key, sent as x-api-key
data-days="3"
```

## 2. Two usable unauthenticated endpoints

### A. Mariana Tek customer API (raw, richest data) — RECOMMENDED
Base: `https://othership.marianatek.com/api/customer/v1/`

Only three endpoints are exposed publicly (all others — `me`, `packages`, `reservations`,
`store`, `sales`, `class_sessions`, etc. — return **404**, i.e. not exposed at all, not 401):
- `GET /regions`
- `GET /locations`
- `GET /classes`  ← the schedule. **In this instance each "class" IS one bookable session/occurrence**
  (unique `id`, `start_datetime`, spot counts). There is NO separate `class_sessions` endpoint (404).
- `GET /classes/{id}` ← single-session detail (same shape as a list item, plus a `layout` seat map).

No auth header required. `Allow: GET, HEAD, OPTIONS` (read-only). Served through CloudFront.

**Working curl (live NYC schedule for a single day):**
```bash
curl -s -A "market-research-recon/0.1 (contact: ryan@usetenancy.com)" \
  "https://othership.marianatek.com/api/customer/v1/classes?min_start_date=2026-07-03&max_start_date=2026-07-03&region=48575&page_size=500&ordering=start_datetime"
```
Filter by a single studio instead of region with `location=48784`.
Single session:
```bash
curl -s -A "market-research-recon/0.1 (contact: ryan@usetenancy.com)" \
  "https://othership.marianatek.com/api/customer/v1/classes/80081"
```

Query params (confirmed working):
- `min_start_date=YYYY-MM-DD`, `max_start_date=YYYY-MM-DD` (inclusive date window)
- `region={regionId}`  OR  `location={locationId}` (location wins for a single studio)
- `page_size=` (default per response = matches request; **hard cap 500**)
- `page=` (integer, 1-based)
- `ordering=start_datetime` (recommended; default order is oldest-first, page 1 = year 2022)

Pagination: DRF style. Response has `count`, `next` (full URL or null), `previous`, `results[]`,
plus `meta.pagination { count, pages, page, per_page }`. Follow `next` until null.
For a single NYC day the full result set (44 sessions) fits in one page — no paging needed at
`page_size=500`. Across the whole brand/all-dates `count` is ~72k (2022→future), so always date-filter.

Schedule horizon: bookable sessions are published **well beyond 10 weeks out** (verified sessions
returned for Aug and mid-Sep 2026; ~80-90/day for NYC region).

Headers / rate limits: `server: gunicorn`, CloudFront (`x-cache`, `x-amz-cf-*`),
`cache-control: no-store`. **No RateLimit/X-RateLimit/Retry-After headers exposed.** Keep volume low anyway.

### B. Booko normalized schedule (adds the incentive/credit field)
Base: `https://bookoapp.com/api/enterprise/{org}/marianatek/`
```bash
curl -s -A "market-research-recon/0.1 (contact: ryan@usetenancy.com)" \
  -H "x-api-key: bk_live_f2cfe64a2e57c0c3dc6127c222bc4f28b0f734c910a90112" \
  -H "Accept: application/json" \
  "https://bookoapp.com/api/enterprise/othership/marianatek/schedule?startDate=2026-07-03&endDate=2026-07-03&mtSubdomain=othership&locations=48784&maxPages=5"
```
Params: `startDate`, `endDate` (YYYY-MM-DD), `mtSubdomain=othership`, `locations=` (comma-sep ids),
`maxPages` (int). Header `x-api-key` required (optional `x-widget-token`).
Also: `GET .../marianatek/meta?mtSubdomain=othership` returns the region→location tree and filter lists.
Booko `booking-beacon` endpoint is analytics only.

## 3. JSON field paths

### Mariana `/classes` (per `results[]` item; also the `/classes/{id}` body):
| Field | Path |
|---|---|
| Session id (bookable occurrence) | `results[].id` (string) |
| Class name | `results[].name` and `results[].class_type.name` |
| Description | `results[].class_type.description` |
| Duration (min) | `results[].class_type.duration` / `.duration_formatted` |
| Start datetime (UTC) | `results[].start_datetime` (e.g. `2026-07-03T11:00:00Z`) |
| Start (local parts) | `results[].start_date`, `results[].start_time`, plus offset in `booking_start_datetime` |
| Timezone | `results[].location.timezone` (e.g. `America/New_York`, `Canada/Eastern`) |
| Location / venue | `results[].location.id`, `.name`, `.city`, `.formatted_address[]` |
| Region | `results[].location.region.id`, `.region.name` |
| Instructor | `results[].instructors[].name` (+ `.instagram_url`, `.photo_urls`) |
| Room | `results[].classroom.name` / `results[].classroom_name` |
| Capacity (total) | `results[].capacity` and `results[].spot_options.primary_capacity` |
| Spots available | `results[].available_spot_count` and `results[].spot_options.primary_availability` |
| Waitlist | `results[].waitlist_count`, `spot_options.waitlist_availability/_capacity` |
| Spots public? | `results[].is_remaining_spot_count_public` |
| Cancelled / free flags | `results[].is_cancelled`, `results[].is_free_class` |
| Layout format | `results[].layout_format` (`first-come-first-serve`, etc.) |
| Seat map (detail only) | `/classes/{id}` adds `layout` |
| **Price** | **NOT PRESENT** — no price/amount/cost/currency field anywhere in the class object |

### Booko `/schedule` (per `rows[]` item):
`id, className, classType, instructorName, locationId, locationName, classroomName, startAt (UTC),
localDate, localTime, timezone, status, capacity, spotsRemaining, durationMinutes, imageUrl,
description, instructors[], studioAddress[], studioPhone, studioEmail,`
`incentive { label, value, source }`  and  `deepLink`.

## 4. DYNAMIC / SURGE PRICING — the answer

**The live drop-in dollar price is NOT exposed on any public, unauthenticated JSON endpoint.**

What I verified:
- The Mariana `/classes` and `/classes/{id}` objects contain **no** price/amount/currency/surge field.
- Every Mariana pricing-related endpoint is **404 / not exposed**: `packages`, `purchasable_packages`,
  `products`, `store`, `store/products`, `sales`, `prices`, `credit_costs`, `plans`, `memberships`,
  `classes/{id}/products`, `classes/{id}/sales`, `dynamic_pricing`, `class_pricing`. (These 404, not 401
  — the public customer API simply does not surface them.)
- Booko's schedule exposes only an **`incentive`** object, e.g. `{"label":"+$9.00 credit","value":9,
  "source":"booko_public_incentives"}`. **This is an account-credit reward Booko offers on selected
  low-demand sessions to shape demand — it is NOT the purchase price.** It is null on most sessions and
  present on a scattered subset (verified: same day, some 7am/8am sessions carry +$9 credit, most midday
  Free Flows carry none). So it is a *demand signal* but not the surge price itself.

**Where the surge price actually lives:** the real drop-in price is rendered only inside the Mariana Tek
**checkout/reserve embed**. Flow: clicking "Reserve" runs Booko's `br(id)` which navigates to
`https://www.othership.us/schedule?_mt=/classes/{id}/reserve` (deepLink field
`?_mt=%2Fclasses%2F{id}%2Freserve`) and then Booko *hides itself* (`Ye()!=="daily"` → overlay hidden),
handing the page to the Mariana Tek checkout integration, which fetches price client-side and requires
entering the guest checkout / payment step.

- **This `/schedule?_mt=` path is explicitly Disallowed in robots.txt** (see §6). It is the auth/checkout
  boundary. I did NOT crawl it, per guardrails. Reaching the actual surge number would require driving the
  Mariana checkout flow (guest cart / account), which is out of scope for unauthenticated recon.
- There is **no clean per-session price/quote GET** you can hit with just a session id. Best documented
  unauth request for a "price-adjacent" signal is the Booko schedule call in §2B (the `incentive` field).

Net: For competitive monitoring you get, unauthenticated, **capacity + live spots-remaining per session**
(a strong real-time demand proxy) and Booko's **credit incentive** flag — but not the surge dollar amount.
The dollar price only surfaces in the auth/checkout-gated Mariana reserve step.

## 5. Locations / ids (from `/api/customer/v1/locations` and Booko `/meta`)

Region **NYC = `48575`**:
- **Flatiron** — id **`48784`** — 23 W. 20th St, New York, NY 10011 — tz America/New_York — flatiron@othership.us
- **Williamsburg** — id **`48817`** — Brooklyn, NY — region NYC (48575)

Region **Toronto = `48541`**:
- **Adelaide** — id `48717` — 425 Adelaide St W, Toronto ON M5V 3C1 — CAD — tz Canada/Eastern
- **Yorkville** — id `48750` — Toronto

(Only these 4 studios / 2 regions are listed.)

## 6. robots.txt findings

- `https://www.othership.us/robots.txt`:
  ```
  User-agent: *
  Disallow: /schedule?_mt=
  Sitemap: https://www.othership.us/sitemap.xml
  ```
  → The **reserve/checkout deep-link path (`/schedule?_mt=...`) is disallowed** for all crawlers. The
  public schedule list (`/schedule`) itself is allowed. I honored this and did not crawl `?_mt=` URLs.
- `https://othership.marianatek.com/robots.txt`: `User-agent: * / Disallow: /` — Mariana asks crawlers not
  to crawl the host. The `/api/customer/v1/` endpoints are nonetheless served openly (200, no auth, CORS/
  CloudFront). Noting the tension: this is a public JSON API but the host's robots disallows crawling.
  Keep volume minimal and cache results.
- `bookoapp.com` — robots not separately relevant; widget JS/API are public assets.

## 7. Trimmed sample response

Mariana `/classes` result item (live Flatiron session, trimmed):
```json
{
  "id": "80081",
  "name": "Guided Down: Senses",
  "available_spot_count": 57,
  "capacity": 60,
  "start_datetime": "2026-07-03T11:00:00Z",
  "start_date": "2026-07-03", "start_time": "07:00:00",
  "class_type": { "id": "5889", "name": "Guided Down: Senses", "duration": 75 },
  "classroom_name": "Sauna",
  "instructors": [ { "id": "...", "name": "Maggie Scrantom" } ],
  "location": { "id": "48784", "name": "Flatiron", "city": "New York",
                "timezone": "America/New_York", "currency_code": "USD",
                "region": { "id": "48575", "name": "NYC" } },
  "spot_options": { "primary_availability": 57, "primary_capacity": 60,
                    "waitlist_availability": 10, "waitlist_capacity": 10 },
  "is_free_class": false, "layout_format": "first-come-first-serve", "waitlist_count": 0
}
```
Full samples saved:
- `/Users/ryanmanocherian/Jonathan/recon/othership_sample.json` (full NYC-region day, Mariana `/classes`)
- `/Users/ryanmanocherian/Jonathan/recon/othership_pricing_sample.json` (Booko `/schedule`, shows `incentive`)
