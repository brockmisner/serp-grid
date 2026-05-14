# SERP Grid

Mobile Google rank check from random points inside a service-area radius, with
screenshots stored in Supabase and a public read-only dashboard.

```
runner/      private, runs on your Mac mini — burns SerpApi credits, writes to Supabase
dashboard/   public, deployed to Streamlit Cloud — read-only viewer
setup.sql    one-time Supabase bootstrap (bucket + table + RLS)
```

The two apps share **only Supabase**. The dashboard cannot run searches, only display them.

---

## 1. Supabase (once)

Paste `setup.sql` into the SQL editor and run it. That creates:

- `serp-shots` storage bucket (public)
- `serp_runs` table
- RLS policy: anon key can `SELECT`; only service-role key can write

Grab two keys from **Project Settings → API**:
- `anon` key → for the public dashboard
- `service_role` key → for the runner only, never put it in the dashboard

---

## 2. Runner (Mac mini)

```bash
cd runner
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env       # fill SERPAPI_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
streamlit run app.py
```

Bound to `http://0.0.0.0:8501` by default — open it from your phone over LAN
or Tailscale, run grids, results stream to Supabase.

---

## 3. Dashboard (Streamlit Community Cloud)

1. Push this whole repo to GitHub.
2. Go to **share.streamlit.io → New app**.
3. Pick the repo, branch `main`, main file: `dashboard/dashboard.py`.
4. **Advanced → Secrets**, paste:
   ```toml
   SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
   SUPABASE_ANON_KEY = "eyJ..."
   ```
5. Deploy. You'll get a public URL like `https://<you>-serp-grid.streamlit.app`.

That's the link you share. Anyone can view runs, filter by keyword, see the
sampled-points map, track a target domain's rank across points, and see the
mobile SERP screenshots. They cannot trigger searches.

---

## Two radii — don't confuse them

- **Sample radius** (runner UI) — disk you scatter random lat/lng points in.
  This is your "targeted radius."
- **Local-pack bias** (`radius` param, optional, 1–1000 m on mobile) — SerpApi's
  own param that nudges Google's local results closer to the point. Off by
  default; only useful if you specifically want tighter local pack focus.

## Security notes

- Service-role key bypasses RLS — stays on the Mac mini, in `runner/.env`.
- Anon key obeys RLS — safe to put in the public dashboard's Streamlit
  secrets. The setup.sql policy only grants SELECT, so anyone can read runs
  but nobody can insert, update, or delete from the public side.
- The `serp-shots` bucket is public read by design — that's how
  `screenshot_url` works without signed URLs. Don't dump anything sensitive
  in there.
