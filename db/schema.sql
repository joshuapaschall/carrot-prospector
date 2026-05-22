-- ============================================================
-- Prospector schema: nationwide wholesaler domain prospecting
-- ============================================================
create extension if not exists "pgcrypto";

-- enums
create type prospect_status as enum ('new','qualified','contacted','replied','customer','dead');
create type query_layer as enum ('keyword','city_template','state_template','county_template','carrot_showcase','facebook');
create type query_status as enum ('pending','done','failed');
create type job_kind as enum ('harvest','qualify');
create type job_status as enum ('running','paused','complete','failed');

-- core prospect table
create table domains (
  id uuid primary key default gen_random_uuid(),
  domain text not null unique,
  business_name text,
  phone text,
  email text,
  city text,
  state text,
  is_carrot boolean not null default false,
  platform text not null default 'unknown',
  confidence_score int not null default 0,
  signals jsonb not null default '{}'::jsonb,
  http_status int,
  qualified boolean not null default false,
  status prospect_status not null default 'new',
  product_fit text[] not null default '{}',
  source text,
  notes text,
  first_seen timestamptz not null default now(),
  last_scraped_at timestamptz,
  updated_at timestamptz not null default now()
);
create index domains_state_idx on domains (state);
create index domains_status_idx on domains (status);
create index domains_qualified_idx on domains (qualified);
create index domains_is_carrot_idx on domains (is_carrot);
create index domains_score_idx on domains (confidence_score desc);
create index domains_browse_idx on domains (qualified, state, confidence_score desc);

-- query tracking: resume + dedup + cost
create table queries (
  id uuid primary key default gen_random_uuid(),
  query_text text not null unique,
  layer query_layer not null default 'keyword',
  status query_status not null default 'pending',
  results_count int not null default 0,
  new_domains int not null default 0,
  credits_used int not null default 0,
  error text,
  processed_at timestamptz,
  created_at timestamptz not null default now()
);
create index queries_status_idx on queries (status);
create index queries_layer_idx on queries (layer);

-- job monitoring
create table jobs (
  id uuid primary key default gen_random_uuid(),
  kind job_kind not null,
  status job_status not null default 'running',
  queries_total int not null default 0,
  queries_done int not null default 0,
  domains_found int not null default 0,
  domains_qualified int not null default 0,
  credits_used int not null default 0,
  started_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz
);

-- updated_at trigger
create or replace function set_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;
create trigger domains_updated_at before update on domains
  for each row execute function set_updated_at();
create trigger jobs_updated_at before update on jobs
  for each row execute function set_updated_at();

-- UI stats views
create view prospect_stats as
select
  count(*) as domains_total,
  count(*) filter (where qualified) as qualified_total,
  count(*) filter (where is_carrot) as carrot_total,
  count(*) filter (where status = 'contacted') as contacted_total,
  count(*) filter (where status = 'customer') as customer_total,
  count(*) filter (where phone is not null and phone <> '') as with_phone,
  count(*) filter (where email is not null and email <> '') as with_email
from domains;

create view prospect_by_state as
select state,
  count(*) as total,
  count(*) filter (where qualified) as qualified,
  count(*) filter (where is_carrot) as carrot
from domains
where state is not null
group by state
order by qualified desc;

-- RLS: locked down. Worker + Vercel server MUST use the service_role key.
-- NEVER use the anon key server-side or RLS silently returns zero rows.
alter table domains enable row level security;
alter table queries enable row level security;
alter table jobs enable row level security;

-- (end of schema.sql)
