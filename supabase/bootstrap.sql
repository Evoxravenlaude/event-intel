-- Event Intel bootstrap script for Supabase
-- Run once after creating your project.
-- The Alembic migration (0002) handles the PostGIS DDL; this script
-- handles extensions, RLS, and any Supabase-specific setup.

create extension if not exists pgcrypto;
create extension if not exists postgis;   -- required for ST_DWithin radius queries

-- Row-level security: only the service role can read/write from the API.
-- You can add user-facing policies here once you have Auth set up.

alter table if exists public.events        enable row level security;
alter table if exists public.raw_signals   enable row level security;
alter table if exists public.review_queue  enable row level security;
alter table if exists public.venues        enable row level security;
alter table if exists public.venue_aliases enable row level security;

create policy if not exists "service_role_full_access_events"
  on public.events for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

create policy if not exists "service_role_full_access_raw_signals"
  on public.raw_signals for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

create policy if not exists "service_role_full_access_review_queue"
  on public.review_queue for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

create policy if not exists "service_role_full_access_venues"
  on public.venues for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

create policy if not exists "service_role_full_access_venue_aliases"
  on public.venue_aliases for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

-- Verify spatial index exists after running Alembic migration 0002.
-- If it's missing (e.g. if you ran DDL out of order), recreate it here:
-- create index if not exists ix_venues_location on venues using gist (location);
