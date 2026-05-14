-- Run in Supabase SQL editor once.

-- 1) Storage bucket (public so screenshot URLs work without auth).
insert into storage.buckets (id, name, public)
values ('serp-shots', 'serp-shots', true)
on conflict (id) do nothing;

-- 2) Results table.
create table if not exists serp_runs (
  id             bigserial primary key,
  run_id         text not null,
  point_index    int  not null,
  keyword        text not null,
  lat            double precision not null,
  lng            double precision not null,
  device         text not null default 'mobile',
  gl             text not null default 'us',
  organic_top    jsonb,
  local_pack     jsonb,
  screenshot_url text,
  created_at     timestamptz not null default now()
);

create index if not exists serp_runs_run_id_idx
  on serp_runs (run_id);
create index if not exists serp_runs_keyword_created_idx
  on serp_runs (keyword, created_at desc);

-- 3) RLS: public can READ; only the service-role key (runner) can WRITE.
alter table serp_runs enable row level security;

drop policy if exists "public read serp_runs" on serp_runs;
create policy "public read serp_runs"
  on serp_runs for select
  to anon, authenticated
  using (true);

-- No INSERT/UPDATE/DELETE policy = anon key cannot write.
-- The runner uses SUPABASE_SERVICE_KEY which bypasses RLS.
