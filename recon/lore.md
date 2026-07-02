# Lore Bathing Club — Booking API Recon

**Date:** 2026-07-02
**Analyst:** market-research-recon/0.1 (ryan@usetenancy.com)
**Target:** Lore Bathing Club (NYC bath house), https://www.lorebathingclub.com/
**Scope:** Public, unauthenticated booking-schedule data. No accounts, no login, no auth bypass.

---

## TL;DR

- **Platform CONFIRMED: Arketa.** Lore's `/bookings` page embeds `https://app.arketa.co/iframe/lorebathingclub/schedule` via an `<iframe id="sutraWidgetIframe">` plus `app.arketa.co/scripts/embed.js`. Slug **`lorebathingclub`** confirmed.
- **The embed is a Create-React-App SPA that reads schedule data DIRECTLY FROM FIRESTORE** (Google Firebase project `sutra-prod` — Arketa's internal codename is "Sutra"), using the Firebase JS SDK — **not** a clean REST endpoint.
- **There is a bootstrap-token requirement.** The widget calls `signInAnonymously` (Firebase anonymous auth) at page load to get an ID token, then reads Firestore under security rules that require `request.auth != null`. A raw unauthenticated Firestore REST call returns **403 PERMISSION_DENIED** (observed).
- **Could NOT capture live schedule JSON.** Minting the anonymous token is the documented public-client bootstrap, but it constitutes an anonymous account signUp, which is outside the recon guardrails (and was blocked). So no live class data was retrieved. Whether Lore has live sessions is therefore **UNCONFIRMED** (Lore targets ~summer 2026 open; schedule may be empty regardless).
- The publicly documented "site_id / date_from / date_to" `/api/schedule` REST shape mentioned in starting intel belongs to a **different platform** (see "Not Arketa" note) — do not use it for Arketa.

---

## 1. Platform & slug confirmation (OBSERVED)

- `https://www.lorebathingclub.com/` (HTTP 200) — the Book/booking nav points to the relative path **`/bookings`**.
- `https://www.lorebathingclub.com/bookings` (HTTP 200) contains:
  - `<iframe id="sutraWidgetIframe" src="https://app.arketa.co/iframe/lorebathingclub/schedule">`
  - `<script src="https://app.arketa.co/scripts/embed.js">`
  - Only embed present is the **schedule** widget. No appointments/events/shop iframe is embedded on Lore's site (see §7).
- `https://app.arketa.co/iframe/lorebathingclub/schedule` (HTTP 200) returns a CRA HTML shell (no `__NEXT_DATA__`; it is CRA, not Next.js). Scripts:
  - `/static/js/main.36dd9cc0.chunk.js`
  - `/static/js/762.71568cc1.chunk.js`
  - 3rd-party: Stripe.js, Google Maps JS (`key=AIzaSyCNSSHH1yTQ492d42qWOG_V_m2uQGdQF74`), ProfitWell.

## 2. Backend architecture (OBSERVED in JS bundles)

Inlined CRA env config found in both bundles:

```
REACT_APP_FIREBASE_API_KEY   = AIzaSyCNSSHH1yTQ492d42qWOG_V_m2uQGdQF74   (public, also used for Maps)
REACT_APP_FIREBASE_PROJECT_ID= sutra-prod
REACT_APP_FIREBASE_AUTH_DOMAIN = sutra-prod.firebaseapp.com
REACT_APP_FIREBASE_APP_ID    = 1:812673543259:web:2b4ef62689288f0775637e
REACT_APP_FIREBASE_MESSAGING_SENDER_ID = 812673543259
REACT_APP_API_ROOT_URL       = https://us-central1-sutra-prod.cloudfunctions.net   (legacy Cloud Functions)
REACT_APP_APIV2_BASE_URL     = https://apiv2-tkaeguucxq-uc.a.run.app               (Cloud Run REST)
REACT_APP_WIDGET_API_BASE_URL= https://widget-api-tkaeguucxq-uc.a.run.app          (Cloud Run REST, widget)
REACT_APP_STRIPE_PUBLIC_KEY  = pk_live_CnzWwEFQuIrgKkwJK1dqW5Ow00tKCf7zzj
```

Key point: **the schedule data itself is not fetched from these REST bases.** The widget subscribes to Firestore directly via the Firebase SDK. The Cloud Run REST bases are used for mutations/marketing (e.g. the only unauthenticated GET found is `apiv2.../mass_campaigns_public/global-forms/{id}` with a `partner-id` header — marketing forms, not schedule). Cloud-function calls for writes go through `httpsCallable` (e.g. `updateClientInformation`, `sendStripeInvoice`, `syncPartnerStatus`).

## 3. How the widget actually loads the schedule (OBSERVED in JS)

1. **Anonymous auth bootstrap:** bundle contains `signInAnonymously` + `signInWithCustomToken`; Firebase auth endpoints referenced: `identitytoolkit.googleapis.com/v1/accounts:*`, `securetoken.googleapis.com/v1/token`. The widget mints an anonymous Firebase ID token on page load.
2. **Slug → studio resolution:** the iframe slug `lorebathingclub` is the **`widgetName`** field on the top-level `users` collection. Observed query pattern: `collection("users").where("widgetName","==", <slug>).get()`. The matched doc's id is the studio account id = **`partnerId`** (the "site/studio id").
3. **Schedule read (real-time listener):** `firestore.collection("users").doc(<partnerId>).collection("services")` (offerings/templates), plus the top-level **`classes`** collection filtered by `partnerId` and a `start_time` range. Observed class query fragments:
   - `collection("classes").where("start_time",">=",startOfDayUnix).where("start_time","<=",endUnix).where("canceled","==",false).where("deleted","==",false)`
   - also filtered `.where("hidden","==",false)` and `.where("partnerId","==",<id>)` in related queries.
   - `start_time` is a **UNIX epoch in SECONDS**.
4. Client applies `getClassesWithFilters` / `getServicesWithFilters` and groups by day. Horizon is controlled by a **`rangeDays`** URL query param on the iframe (observed `R().get("rangeDays")`, parsed as int) — i.e. the client requests a window; no server-side pagination cursor. Effectively pagination = widen the `start_time` range / bump `rangeDays`.

## 4. Endpoint(s) — copy-pasteable

### 4a. What the client does (requires bootstrap token — NOT executed here)

Step 1, mint anonymous token (THIS IS AN ANONYMOUS ACCOUNT SIGNUP — out of guardrail scope, not run):
```bash
# POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=AIzaSyCNSSHH1yTQ492d42qWOG_V_m2uQGdQF74
#   body: {"returnSecureToken":true}  -> returns {"idToken": "..."}
```
Step 2, resolve slug and read classes via Firestore REST with `Authorization: Bearer <idToken>`:
```bash
curl -s -A "market-research-recon/0.1 (contact: ryan@usetenancy.com)" \
  -H "Authorization: Bearer $ID_TOKEN" -H "Content-Type: application/json" \
  -X POST "https://firestore.googleapis.com/v1/projects/sutra-prod/databases/(default)/documents:runQuery?key=AIzaSyCNSSHH1yTQ492d42qWOG_V_m2uQGdQF74" \
  -d '{"structuredQuery":{"from":[{"collectionId":"users"}],"where":{"fieldFilter":{"field":{"fieldPath":"widgetName"},"op":"EQUAL","value":{"stringValue":"lorebathingclub"}}},"limit":1}}'
# then, with the resolved <partnerId>:
curl -s -H "Authorization: Bearer $ID_TOKEN" -H "Content-Type: application/json" \
  -X POST "https://firestore.googleapis.com/v1/projects/sutra-prod/databases/(default)/documents:runQuery?key=AIzaSyCNSSHH1yTQ492d42qWOG_V_m2uQGdQF74" \
  -d '{"structuredQuery":{"from":[{"collectionId":"classes"}],"where":{"compositeFilter":{"op":"AND","filters":[
        {"fieldFilter":{"field":{"fieldPath":"partnerId"},"op":"EQUAL","value":{"stringValue":"<PARTNER_ID>"}}},
        {"fieldFilter":{"field":{"fieldPath":"start_time"},"op":"GREATER_THAN_OR_EQUAL","value":{"integerValue":"1751328000"}}},
        {"fieldFilter":{"field":{"fieldPath":"start_time"},"op":"LESS_THAN_OR_EQUAL","value":{"integerValue":"1753920000"}}}
      ]}}}}'
```
NOTE: the real widget uses the Firestore **Listen/WebChannel** streaming channel, not `:runQuery`; `:runQuery` above is the equivalent one-shot REST form. Both honor the same security rules and the same Bearer token.

### 4b. What was actually tested (OBSERVED, guardrail-safe)

Raw unauthenticated `:runQuery` (no Bearer token), exactly as saved in `lore_sample.json`:
```bash
curl -s -H "Content-Type: application/json" \
  -X POST "https://firestore.googleapis.com/v1/projects/sutra-prod/databases/(default)/documents:runQuery?key=AIzaSyCNSSHH1yTQ492d42qWOG_V_m2uQGdQF74" \
  -d '{"structuredQuery":{"from":[{"collectionId":"users"}],"where":{"fieldFilter":{"field":{"fieldPath":"widgetName"},"op":"EQUAL","value":{"stringValue":"lorebathingclub"}}},"limit":3}}'
```
Response (HTTP 403):
```json
[{"error":{"code":403,"message":"Missing or insufficient permissions.","status":"PERMISSION_DENIED"}}]
```
=> Confirms: **no data without the anonymous bearer token.** The API key alone is insufficient.

## 5. Field paths (class/session)

Legend: **[obs]** = field path seen literally in the widget bundle; **[inf]** = inferred meaning/type.

| Concept | Field path | Notes |
|---|---|---|
| Class name | `name` **[obs]** / `title` **[obs]** | `name` is primary (very frequent); `title` alt |
| Session id | doc `id` **[obs]** | Firestore doc id (SDK uses `idField:"id"`) |
| Start datetime | `start_time` **[obs]** | UNIX epoch **seconds** [inf]; range-queried |
| End / duration | `duration` **[obs]** | minutes [inf]; explicit `end_time` not confirmed |
| Timezone | `timezone` **[obs]** / `timeZone` **[obs]** | both appear in code |
| Location / venue | `location` **[obs]**, `location_type` **[obs]**, `room` **[obs]**, `address` **[obs]** | `location_type` ∈ in-person / virtual / ondemand / product [inf] |
| Instructor | `instructor` **[obs]**, `instructor_id` **[obs]**, `host_ids[]` **[obs]**, `hosts` **[obs]** | `host_ids` array-contains queried |
| Capacity / total spots | `max_capacity` **[obs]** | `Number(max_capacity||9999)` default [obs] |
| Spots available | `spotsLeft` **[obs, derived]**, `spotsLeftText` **[obs, derived]** | computed client-side (= capacity − booked) [inf] |
| Full flag | `isFull` **[obs, derived]** | boolean |
| Booked count | not a clean single field | `reservation`/`registered` tokens appear [obs]; exact path unconfirmed [inf] |
| Price | `price` **[obs]**, `amount` **[obs]**; service: `price`, `minimum_price`, `rates[]` **[obs]** | |
| Waitlist | join via `POST app/checkout/waitlist {classId}` **[obs]**; appt: `POST /{id}/appointments/waitlist` **[obs]** | status flag path unconfirmed [inf] |
| Category | `category` **[obs]** / `categoryId` **[obs]** | |
| Recurrence | `recurringClassId` **[obs]** | |
| Visibility flags | `canceled` **[obs]**, `deleted` **[obs]**, `hidden` **[obs]** | all filtered `==false` |

See `lore_sample.json` for a reconstructed example document (clearly marked inferred).

## 6. Pagination, horizon, rate limits, tokens

- **Pagination:** No server cursor observed. Client controls the window via `start_time` range + iframe `rangeDays` query param. To page, widen the range.
- **Horizon:** whatever `rangeDays` the embed requests; the underlying `classes` collection holds all future scheduled instances (no hard server cap observed).
- **Rate limits:** Firestore/Google APIs enforce their own quotas; Arketa's documented ~25 req/s applies to their REST API. Recon kept volume to a handful of requests.
- **Token that expires:** YES — Firebase anonymous **ID token** (~1 h lifetime, refreshable via `securetoken.googleapis.com/v1/token`). It is minted at page load by `signInAnonymously`. **Any scraper MUST replicate this bootstrap** (anonymous signUp → idToken → Firestore reads), then refresh the token. This is the core "bootstrap complexity."

## 7. Other bookable things on Lore

- Lore's `/bookings` page embeds **only** the group-class **schedule** widget (`/iframe/lorebathingclub/schedule`). No appointments/events/shop/membership iframe is embedded on their site currently.
- Arketa supports other widget types generally (bundle references `"shop"`, `"store"`, appointments, waitlist, memberships), and other slugs like `/iframe/lorebathingclub/appointments` may exist server-side, but none are surfaced on Lore's marketing site. Whether any are live is unconfirmed.
- The homepage also links to Truemed (`app.truemed.com/qualify/...`) for HSA/FSA eligibility — not a booking surface.

## 8. robots.txt findings

- **www.lorebathingclub.com/robots.txt:** only `Sitemap: https://www.lorebathingclub.com/sitemap.xml`. No `Disallow` rules. `/bookings` is not disallowed.
- **app.arketa.co/robots.txt:** `User-agent: *` with many `Disallow`s (`/`, `/auth`, `/signin`, `/dashboard`, `/settings`, `/pricing`, `/support`, etc.). **`/iframe/`, `/static/`, and `/scripts/` are NOT disallowed** — the embed, JS bundles, and embed.js are fair game.
- `firestore.googleapis.com` / `identitytoolkit.googleapis.com` are Google API hosts (robots not applicable to API endpoints).

## 9. "Not Arketa" caveat (important)

The starting-intel REST shape `GET /api/schedule?site_id=&date_from=&date_to=` (bearerAuth optional) was found only in a **stale, unrelated bundle** (`recon/app.js`, plus `recon/flatiron_widget.html`) left over from a prior recon of a **different platform** — a hotel/PMS booking system (its bundle contains `/sites/{siteId}/integrations/adyen|avvio|guestline|giftpro`, `/api/my-visit/basket`, zero references to arketa/sutra). **Do not attribute that endpoint to Arketa.** Arketa/Lore uses the Firestore mechanism documented above.

## 10. Open questions

1. Does Lore have live sessions? **Unknown** — could not read Firestore without the anonymous token; Lore may also not be open yet.
2. Exact `partnerId` for lorebathingclub — unresolved (blocked at the 403 step).
3. Exact stored field names for booked-count and waitlist-status (only client-derived `spotsLeft`/`isFull` confirmed).
4. Whether Firestore security rules would actually return `lorebathingclub` classes to an anonymous token (expected yes, since that is how the public widget works) — not verified.
5. Whether Arketa exposes a cleaner unauthenticated REST schedule endpoint on `widget-api-tkaeguucxq-uc.a.run.app` was not exhaustively probed (no schedule path found in the bundle for it; the widget clearly uses Firestore).
