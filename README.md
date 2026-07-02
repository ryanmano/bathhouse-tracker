# Bath House Market-Research Pipeline

Hourly snapshots of class schedules, availability, and pricing for three NYC bath
houses — **Bathhouse** (Trybe), **Othership** (Mariana Tek), **Lore Bathing Club**
(Arketa) — stored append-only in Supabase for fill-rate and dynamic-pricing
analysis over time. Endpoint discovery and field mappings are documented in
[RECON.md](RECON.md).

## How it works

Every run (hourly via GitHub Actions, or manual):

1. `run.py` loads `config/brands.yaml`, calls each platform scraper
   (`scrapers/trybe.py`, `scrapers/mariana_tek.py`, `scrapers/arketa.py`),
2. each scraper fetches the next 14 days of bookable sessions from the platform's
   public JSON API and normalizes them via `normalize.make_record()`,
3. all records get one shared `observed_at` timestamp and are batch-inserted into
   the `snapshots` table (duplicate `(brand, session_id, observed_at)` rows are
   ignored, so re-runs are idempotent),
4. a per-brand summary is printed; one brand failing never aborts the others.

~10 HTTP requests per run total, honest User-Agent, retries with backoff.

> **Current state (2026-07-02):** cloud mode is live — hourly GitHub Actions
> runs (repo `ryanmano/bathhouse-tracker`, private) writing to Supabase project
> `rtahlkpxzyjcjtxdryzg`. The local launchd job is unloaded; ~3 early local
> snapshots remain in `~/BathhouseData/snapshots.db`.

## Local mode (no accounts) — optional alternative

No GitHub or Supabase needed. A macOS launchd job
(`~/Library/LaunchAgents/com.tenancy.bathhouse-tracker.plist`) runs
`python3 run.py --local` at the top of every hour while the Mac is awake
(missed hours are skipped; one catch-up run fires on wake). Data lands in
`~/BathhouseData/`:

- `snapshots.db` — append-only SQLite database (all history)
- `daily/bathhouse-tracker-YYYY-MM-DD.csv` — one spreadsheet per day,
  refreshed every run; opens directly in Excel / Google Sheets
- `logs/run.log` — run output

To keep disk use small (~7MB/day), the raw JSON blob is stored only when a
session's price/spots/capacity/waitlist state changed since its previous
observation; normalized columns are stored on every row. `run.py` also strips
bulky presentation fields (descriptions, images, addresses) from raw before
storage in both modes.

Handy commands:

```bash
python3 export_to_csv.py --out everything.csv    # full history -> one CSV
launchctl kickstart gui/$(id -u)/com.tenancy.bathhouse-tracker   # run now
launchctl bootout   gui/$(id -u)/com.tenancy.bathhouse-tracker   # stop the schedule
sqlite3 ~/BathhouseData/snapshots.db "select count(*) from snapshots"
```

The SQL queries below work on the SQLite db too (open with `sqlite3` or any
GUI like DB Browser). To upgrade to 24/7 cloud capture later, follow the
Supabase/GitHub setup below — nothing about local mode conflicts with it.

## Cloud setup (optional upgrade: Supabase + GitHub Actions)

1. **Supabase**: create a (free-tier) project, then run `db/schema.sql` in the
   SQL editor to create the `snapshots` table and indexes.
2. **Local env**: `cp .env.example .env` and fill in `SUPABASE_URL` and
   `SUPABASE_SERVICE_KEY` (Project Settings → API). Never commit `.env`.
3. **Install**: `pip install -r requirements.txt` (Python 3.11+).
4. **GitHub Actions**: push this repo to GitHub and add `SUPABASE_URL` and
   `SUPABASE_SERVICE_KEY` as repository **secrets** (Settings → Secrets and
   variables → Actions). The workflow (`.github/workflows/scrape.yml`) runs
   hourly and on manual dispatch.

## Running

```bash
python run.py --dry-run              # fetch live, print samples, write nothing
python run.py                        # fetch + insert snapshot into Supabase
python run.py --brands bathhouse     # subset
python export_to_csv.py --brand othership --since 2026-07-01 --out out.csv
```

The CSV opens directly in Excel / Google Sheets (File → Import). For a live
Sheets sync later, Supabase's API + Apps Script or a connector like Coefficient
works, but CSV export is the supported v1 path.

## Ready-made SQL queries

**Latest snapshot of every upcoming session per brand:**
```sql
select distinct on (brand, session_id)
       brand, location, class_name, start_time, capacity, spots_available,
       price, price_tier, observed_at
from snapshots
where start_time > now()
order by brand, session_id, observed_at desc;
```

**Price history for one session (dynamic-pricing curve):**
```sql
select observed_at, price, spots_available
from snapshots
where brand = 'bathhouse' and session_id = '<SESSION_ID>'
order by observed_at;
```

**Fill-rate curve as a session approaches its start:**
```sql
select observed_at,
       round(extract(epoch from (start_time - observed_at)) / 3600, 1) as hours_to_start,
       capacity, spots_available, spots_booked,
       round(100.0 * spots_booked / nullif(capacity, 0), 1) as pct_full
from snapshots
where brand = 'othership' and session_id = '<SESSION_ID>'
order by observed_at;
```

## What each brand exposes (v1 caveats)

| Brand | Fill data | Price data |
|---|---|---|
| Bathhouse | capacity + spots remaining | **Live drop-in price in every snapshot** (varies by time slot, $49–$114 observed). Day Pass sessions only; treatments use a separate flow (out of scope). |
| Othership | capacity + spots remaining + waitlist | **No public price.** The surge dollar amount only renders inside the auth-gated checkout (also robots-disallowed), so `price` is null. `price_tier` carries Booko's per-session credit *incentive* (their demand-shaping signal) when present. Fill velocity is the demand proxy. |
| Lore | capacity (`max_capacity`) + booked (`total_booked`) | **Live — already selling sessions.** `price` in whole dollars ($0/$25/$55 tiers observed; $0 rows may be member-included slots — raw JSON is kept for re-deriving). Fully unauthenticated REST (`app.arketa.co/api/widget/*`), no token of any kind. |

## Scheduling notes (GitHub Actions)

- Cadence is hourly (`0 * * * *`); switch to every 30 min by editing one line in
  `scrape.yml` (`0,30 * * * *`).
- Scheduled workflows are **best-effort** — runs can start minutes late under load.
- A **public** repo gets unlimited Actions minutes; a **private** repo is capped
  (~2,000 min/mo — hourly ~1–2 min runs fit comfortably; 30-min cadence may not).
- GitHub **disables schedules after ~60 days of repo inactivity** — an occasional
  commit or a manual dispatch keeps it alive.

## Guardrails

- Public, unauthenticated data only; no accounts or tokens of any kind, no
  paywall/login bypass, no checkout flows.
- Low volume by design (~10 requests/hour), honest User-Agent with contact email,
  retries capped with backoff.
- robots.txt notes: `othership.us` disallows only the checkout deep-link path
  (never fetched); `othership.marianatek.com` has a blanket crawler disallow while
  serving the API openly to its own public widget — polling is deliberately
  minimal (1–2 requests/run) and this tension is documented, not hidden.
- Both Mariana Tek and Arketa offer official partner APIs (key required) if
  sanctioned access is ever wanted; out of scope for this read-only v1.
- All timestamps stored in UTC (`raw` keeps the platform's original values).
