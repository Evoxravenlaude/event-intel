-- Event Intel bootstrap script for Supabase
-- ----------------------------------------------------------------------------
-- Run this ONCE in the Supabase SQL editor *after* the first API deploy has
-- applied the Alembic migrations (so the tables exist).
-- Safe to re-run: every statement is idempotent.
--
-- What it does:
--   1. Enables the extensions the app uses (PostGIS, pgvector, pgcrypto).
--      Alembic migrations 0002/0003 also try to enable them, but on Supabase
--      the migration role may lack permission — running it here guarantees it.
--   2. Enables row-level security on EVERY application table. Supabase exposes
--      the whole `public` schema through its REST API using the anon key; any
--      table without RLS is readable AND writable by anyone holding that key.
--   3. Grants the service role full access. The API itself connects over the
--      direct Postgres connection string as the table owner and is unaffected.
-- ----------------------------------------------------------------------------

create extension if not exists pgcrypto;
create extension if not exists postgis;   -- ST_DWithin radius queries
create extension if not exists vector;    -- pgvector nearest-neighbour clustering

-- Postgres has no CREATE POLICY IF NOT EXISTS, so wrap in a DO block.
do $$
declare
  t text;
begin
  foreach t in array array[
    'venues', 'venue_aliases', 'organizers', 'events', 'raw_signals',
    'event_evidence', 'review_queue', 'source_runs', 'alembic_version'
  ]
  loop
    if to_regclass('public.' || t) is null then
      raise notice 'table public.% does not exist yet — run migrations first', t;
      continue;
    end if;

    execute format('alter table public.%I enable row level security', t);

    if not exists (
      select 1 from pg_policies
      where schemaname = 'public' and tablename = t
        and policyname = 'service_role_full_access'
    ) then
      execute format(
        'create policy service_role_full_access on public.%I for all '
        'using (auth.role() = ''service_role'') with check (auth.role() = ''service_role'')',
        t
      );
    end if;
  end loop;
end
$$;

-- Repair step: if the extensions were NOT available when Alembic 0002/0003 ran,
-- the migrations fell back (no location column; TEXT embedding columns). Now
-- that the extensions exist, upgrade those columns and indexes in place.
do $$
begin
  -- PostGIS location column + GIST index + sync trigger
  if not exists (select 1 from information_schema.columns
                 where table_schema = 'public' and table_name = 'venues' and column_name = 'location') then
    alter table public.venues add column location geography(Point, 4326);
    update public.venues
       set location = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography
     where latitude is not null and longitude is not null;
  end if;
  create index if not exists ix_venues_location on public.venues using gist (location);

  create or replace function public.sync_venue_location() returns trigger as $f$
  begin
    if new.latitude is not null and new.longitude is not null then
      new.location := ST_SetSRID(ST_MakePoint(new.longitude, new.latitude), 4326)::geography;
    end if;
    return new;
  end;
  $f$ language plpgsql;
  drop trigger if exists trg_sync_venue_location on public.venues;
  create trigger trg_sync_venue_location
    before insert or update of latitude, longitude on public.venues
    for each row execute function public.sync_venue_location();

  -- pgvector embedding columns: convert TEXT fallback → vector(384)
  if exists (select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'events'
               and column_name = 'embedding' and data_type = 'text') then
    alter table public.events alter column embedding type vector(384) using embedding::vector;
  end if;
  if exists (select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'raw_signals'
               and column_name = 'embedding' and data_type = 'text') then
    alter table public.raw_signals alter column embedding type vector(384) using embedding::vector;
  end if;
  create index if not exists ix_events_embedding_hnsw on public.events using hnsw (embedding vector_cosine_ops);
  create index if not exists ix_raw_signals_embedding_hnsw on public.raw_signals using hnsw (embedding vector_cosine_ops);
end
$$;
