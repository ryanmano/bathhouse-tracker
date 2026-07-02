# RECON.md — Bath House Market-Research Pipeline, Phase 1 Findings

Date: 2026-07-02 · Recon UA: `market-research-recon/0.1 (contact: ryan@usetenancy.com)`
Detailed per-brand write-ups and full sample responses live in `recon/` (`bathhouse.md`, `othership.md`, `lore.md`, `*_sample.json`).

---

## Executive summary

| Brand | Platform (verified) | Live data unauth? | Spots/capacity | Price |
|---|---|---|---|---|
| **Bathhouse** | **Trybe** (`try.be`) — *not* Mariana Tek | ✅ Yes, re-verified 2026-07-02 | ✅ `capacity` + `remaining_capacity` | ✅ **In the schedule payload** (integer USD cents), varies by time slot ($49–$114 observed) |
| **Othership** | **Mariana Tek** (`othership.marianatek.com`) | ✅ Yes, re-verified 2026-07-02 | ✅ `capacity` + `available_spot_count` (+ waitlist counts) | ❌ **Not exposed on any public endpoint** — surge $ only renders inside auth/checkout-gated reserve flow |
| **Lore** | **Arketa** (`app.arketa.co/api/widget/*` REST) | ✅ Yes — **Lore is already live** (705 sessions/14 days, verified 2026-07-02) | ✅ `max_capacity` + `total_booked` | ✅ `price` in whole dollars ($0 / $25 / $55 observed) |

Key corrections to starting intel:
1. **Bathhouse is NOT on Mariana Tek** — it's on Trybe. So there is no shared scraper for two brands; we need three platform modules (Trybe, Mariana Tek, Arketa). Each is small.
2. The `site_id/date_from/date_to` REST shape from the intel belongs to **Trybe** (Bathhouse), not Arketa. Arketa has no clean REST schedule endpoint — its widget reads Firestore directly.
3. **Othership's surge dollar price is not publicly readable.** See §2 and the decision needed in §5.

---

## 1. Bathhouse — Trybe ✅ (easiest target)

**Endpoint (no auth, no special headers, re-verified live):**
```
GET https://{location}.try.be/api/schedule?site_id=9b5df0bd-ad8d-48ce-8714-0ae9967406e9&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
```
Locations = subdomains (scoping is by host, `site_id` just must be present):
`bathhouseflatiron` · `bathhousewilliamsburg` · `bathhouseatlanticave` (all `.try.be`, all confirmed live).

- **No pagination** — one flat `data[]` array for the whole range (180 days ≈ 4,680 items in one response). Chunk by week/month anyway. Horizon 6+ months (real sessions into Jan 2027).
- Optional filter: `category_ids` (Day Pass = `661d5d567bf690aa890c3e25`).

**Field paths (`data[]`):** id `id` · name `session_type.name` · start/end `start_time`/`end_time` (ISO-8601 with embedded `-04:00`/`-05:00` offset) · capacity `capacity` · spots left `remaining_capacity` · **price `price` (integer USD cents, e.g. `5900` = $59)** · waitlist `waitlist_enabled` · instructor `practitioner` (null for day passes). Location is implicit in the subdomain — our scraper must stamp it from config.

**Pricing note:** Day Pass price is per-time-slot ($49–$114 observed across slots/locations). Whether it also flexes with real-time demand vs. a fixed day-part rate card is exactly what hourly snapshots will answer.

**Scope note:** `/api/schedule` returns **Day Pass sessions only**. Massage/Couples/Packs use a separate practitioner appointment-slot flow (`/shop/appointment-slots/...`) — not wired in v1.

Sample: `recon/bathhouse_sample.json` (52 sessions). robots: `*.try.be` serves no robots.txt; abathhouse.com (Squarespace) irrelevant to data.

## 2. Othership — Mariana Tek ✅ (schedule) / ❌ (surge price)

**Endpoint (no auth, re-verified live):**
```
GET https://othership.marianatek.com/api/customer/v1/classes?min_start_date=YYYY-MM-DD&max_start_date=YYYY-MM-DD&region=48575&page_size=500&ordering=start_datetime
```
- DRF pagination (`count`/`next`/`results`), `page_size` cap 500 — one NYC day ≈ 44 sessions, so a 2-week window fits in 1–2 pages. Always date-filter (unfiltered count ≈ 72k back to 2022). Horizon 10+ weeks.
- Each "class" **is** one bookable occurrence here (`class_sessions` doesn't exist — 404). Also public: `/regions`, `/locations`, `/classes/{id}` (adds seat map). Everything else 404s.
- **Locations:** NYC region `48575` → Flatiron `48784`, Williamsburg `48817`. Toronto region `48541` → Adelaide `48717`, Yorkville `48750` (CAD).

**Field paths (`results[]`):** id `id` · name `name` · start `start_datetime` (UTC Z) · tz `location.timezone` · venue `location.name`/`.id` · instructor `instructors[].name` · capacity `capacity` · available `available_spot_count` · waitlist `waitlist_count` + `spot_options.waitlist_*` · flags `is_cancelled`, `is_free_class`. **No price field of any kind.**

**Dynamic/surge pricing — the hard finding:** every pricing-related Mariana endpoint (`packages`, `products`, `store`, `prices`, `credit_costs`, …) returns 404 unauthenticated. The live drop-in dollar price renders only inside the Mariana **reserve/checkout embed** (`othership.us/schedule?_mt=/classes/{id}/reserve`) — which is (a) behind the guest-checkout boundary and (b) **explicitly disallowed in othership.us robots.txt**. Not crawled, per guardrails. There is no per-session price/quote GET.

**Partial substitute:** Othership's site wraps the schedule in a third-party widget, **Booko** (`bookoapp.com`), whose public widget API (key `bk_live_…` embedded in the page HTML, sent as `x-api-key`) returns a normalized schedule including an `incentive` object (e.g. `+$9.00 credit`) on selected **low-demand** sessions. That's a demand-shaping credit, not the price — but it is a usable *demand-tier signal* alongside spots-remaining. Sample: `recon/othership_pricing_sample.json`.

**robots tension (flagging, not hiding):** `othership.marianatek.com/robots.txt` is a blanket `Disallow: /` even though the JSON API is served openly with no auth and CORS enabled for the public widget. An hourly, 1–3-request poll is minimal, but this is a judgment call surfaced in §5.

Sample: `recon/othership_sample.json` (full NYC day).

## 3. Lore — Arketa ✅ (UPDATED after build-phase verification)

> **This section supersedes the initial recon below.** During the build phase
> (2026-07-02) the Firestore/anonymous-auth theory was tested live and does
> **not** work (anonymous tokens can't query `users` — 403; `classes` queries
> hit missing composite indexes — 400). The widget's real public data path is an
> **unauthenticated REST layer** that proxies Firestore server-side — no token,
> no account, nothing to bootstrap:
>
> ```
> GET https://app.arketa.co/api/widget/exists?widgetName=lorebathingclub
>     -> { "exists": true, "partnerId": "8SxAk4JNDoPTK6j81zrxghwzkz22" }
> GET https://app.arketa.co/api/widget/partners/{partnerId}/classes?start={epoch_s}&end={epoch_s}
>     -> { "partnerData": {...}, "classes": [ ... ] }
> ```
>
> **Lore is already open/selling:** 717 raw classes in a 14-day window (705 after
> filtering `display=="private"`/`canceled`) — "Weekday Session", "Weekend
> Session", "Quiet Hours", etc. Verified field mappings: id `id`, name
> `class_name`/`name`, start `start_time` (epoch **seconds**), capacity
> `max_capacity`, **booked `total_booked`** (reliable), `price` in **whole
> dollars** (0 / 25 / 55 observed), location `location.name` (e.g. "676
> Broadway"), waitlist `waitlistLength`. Caveat: `instructor_name`/`host_name`
> hold session labels ("Aufguss Time: 5:30 PM"), not people.

### Original recon (historical — Firestore path, superseded above)

**Confirmed:** slug `lorebathingclub`; Lore's `/bookings` page embeds `https://app.arketa.co/iframe/lorebathingclub/schedule` (only the schedule widget — no appointments/shop embeds). robots.txt permits the iframe/static paths.

**Architecture (from the widget's JS bundles):** there is **no REST schedule endpoint**. The widget is a React SPA that reads **Google Firestore directly** (project `sutra-prod`):
1. On load it calls Firebase **`signInAnonymously`** → short-lived (~1 h, refreshable) anonymous ID token.
2. Resolves slug → `partnerId` via `users` collection (`widgetName == "lorebathingclub"`).
3. Queries the `classes` collection by `partnerId` + `start_time` (UNIX **seconds**) range, filtered `canceled/deleted/hidden == false`. No server pagination — you widen the time range.

**Verified:** without the anonymous token, Firestore returns **403 PERMISSION_DENIED** (saved as `recon/lore_sample.json`). The API key alone is insufficient.

**The blocker is a policy call, not a technical one:** minting the anonymous token is a Firebase "anonymous account signUp" — the recon agent stopped there under the "no account creation" guardrail. Note this is what *every anonymous browser visitor's* widget does automatically on page load; it is the platform's intended public-access mechanism, not a bypass. Decision in §5.

**Field paths (observed in bundle code, not live data):** name `name`/`title` · id = Firestore doc id · start `start_time` (epoch seconds) · `duration` (min) · `timezone` · location `location`/`room`/`address` · instructor `instructor`/`host_ids[]` · capacity `max_capacity` · availability derived client-side (`spotsLeft`, `isFull`) · price `price`/`amount` (+ service `rates[]`). Exact booked-count field unconfirmed until we can read live docs.

**Live sessions:** unknown — both because of the 403 and because Lore may not open until ~summer 2026. The scraper should treat an empty result as normal.

## 4. Cross-cutting notes for the build

- **Three platform modules**, not two: `trybe.py` (Bathhouse), `mariana_tek.py` (Othership), `arketa.py` (Lore). All fit one normalized schema.
- Nothing needs a browser and nothing needs a token — all three are plain unauthenticated HTTPS GETs.
- No rate-limit headers observed anywhere; hourly cadence with 1–5 requests/brand is far below any plausible threshold.
- Timezones: Trybe embeds the offset in timestamps; Mariana gives UTC + IANA tz; Arketa gives epoch seconds. Normalizer converts all to UTC.
- Request volume per hourly run (14-day window): Bathhouse 3 (one per location), Othership 1–2 (+1 optional Booko call), Lore 1 (partnerId is cached in config). ~7 requests/hour total.

## 5. Decisions needed before Phase 2+

1. **Othership surge price:** the dollar amount is unreachable without driving the auth-gated checkout, and the checkout path is robots-disallowed. **Recommendation:** track `available_spot_count` over time (the direct fill/demand curve) + Booko's `incentive` field (their own demand-tier signal), and leave `price` null for Othership in v1. The alternative — automating guest checkout — crosses the brief's own guardrails; not recommended.
2. **Mariana robots.txt tension:** `othership.marianatek.com` blanket-disallows crawlers while serving the API openly to the public widget. OK to proceed with hourly minimal polling, or drop/reduce?
3. **Lore anonymous Firebase token:** proceed with the anonymous-auth bootstrap (identical to what every visitor's browser does; no PII, no credentials), or leave Lore stubbed until they open?
4. **Bathhouse treatments** (massage/couples): out of scope for v1 (separate appointment-slot flow)? Recommend yes — Day Pass is the dynamic-pricing product anyway.
