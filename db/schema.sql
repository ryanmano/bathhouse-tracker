-- Append-only snapshot table: one row per upcoming bookable session per run.
-- Run this in the Supabase SQL editor (or psql) once.

create table if not exists snapshots (
    id               bigint generated always as identity primary key,
    observed_at      timestamptz not null,
    brand            text not null,          -- lore / bathhouse / othership
    platform         text not null,          -- trybe / mariana_tek / arketa
    location         text,                   -- e.g. Williamsburg
    session_id       text not null,          -- platform's stable id for the occurrence
    class_name       text,
    start_time       timestamptz,            -- session start, UTC
    instructor       text,
    capacity         integer,
    spots_available  integer,
    spots_booked     integer,                -- capacity - available when both known
    price            numeric(10, 2),         -- live price in currency units (dollars)
    price_tier       text,                   -- tier/package/incentive label
    currency         text not null default 'USD',
    is_waitlist      boolean not null default false,  -- session full / waitlist state
    source_url       text,
    raw              jsonb                   -- raw platform session object
);

-- Idempotency: re-running the same snapshot must not duplicate rows.
create unique index if not exists snapshots_brand_session_observed_uq
    on snapshots (brand, session_id, observed_at);

-- Query paths: schedule browsing and per-session time series.
create index if not exists snapshots_brand_start_idx
    on snapshots (brand, start_time);
create index if not exists snapshots_session_observed_idx
    on snapshots (session_id, observed_at);
