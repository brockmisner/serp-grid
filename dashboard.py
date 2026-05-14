"""
SERP Grid Dashboard — public, read-only viewer.

Pulls from Supabase using the ANON key. RLS allows SELECT only.
Deploy entry point: dashboard/dashboard.py
Secrets needed:  SUPABASE_URL, SUPABASE_ANON_KEY
"""

import os
import pandas as pd
import streamlit as st
from supabase import create_client

st.set_page_config(
    page_title="SERP Grid",
    page_icon="📍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------- secrets ----------
SUPABASE_URL = st.secrets.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_ANON_KEY")
if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.error("Missing SUPABASE_URL / SUPABASE_ANON_KEY in Streamlit secrets.")
    st.stop()


@st.cache_resource
def get_client():
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


sb = get_client()


@st.cache_data(ttl=60, show_spinner=False)
def fetch_runs(limit: int = 2000) -> pd.DataFrame:
    res = (
        sb.table("serp_runs")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return pd.DataFrame(res.data or [])


# ---------- header ----------
st.title("📍 SERP Grid")
st.caption("Mobile Google rank check from random points within a service-area radius.")

df = fetch_runs()
if df.empty:
    st.info("No runs yet. The runner will populate this dashboard once it pushes data.")
    st.stop()

# ---------- sidebar filters ----------
st.sidebar.header("Filters")

keywords = sorted(df["keyword"].dropna().unique().tolist())
kw_filter = st.sidebar.selectbox("Keyword", ["(all)"] + keywords, index=0)

filt = df if kw_filter == "(all)" else df[df["keyword"] == kw_filter]

# Run options sorted by most-recent first, labeled with date + keyword(s) + point count
run_options = (
    filt.groupby("run_id")["created_at"].max().sort_values(ascending=False).index.tolist()
)
if not run_options:
    st.info("No runs match those filters.")
    st.stop()

run_labels = {}
for rid in run_options:
    sub = filt[filt["run_id"] == rid]
    ts = pd.to_datetime(sub["created_at"].max()).strftime("%b %d, %H:%M")
    kws = ", ".join(sub["keyword"].unique()[:2])
    run_labels[rid] = f"{ts}  ·  {kws}  ·  {len(sub)} pts"

selected_run = st.sidebar.selectbox(
    "Run",
    run_options,
    format_func=lambda r: run_labels.get(r, r),
)

target_domain = st.sidebar.text_input(
    "Track domain (optional)",
    placeholder="houseacrepair.com",
    help="Show where this domain ranks in each point's organic results.",
)

st.sidebar.divider()
st.sidebar.caption("Data refreshes ~60s. Read-only view.")

# ---------- overview metrics ----------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total runs", filt["run_id"].nunique())
c2.metric("Points captured", len(filt))
c3.metric("Keywords", filt["keyword"].nunique())
c4.metric("Latest", pd.to_datetime(filt["created_at"].max()).strftime("%b %d, %H:%M"))

st.divider()

# ---------- selected run detail ----------
run_df = (
    filt[filt["run_id"] == selected_run]
    .sort_values("point_index")
    .reset_index(drop=True)
)

h1, h2, h3, h4 = st.columns([3, 1, 1, 1])
h1.markdown(f"### `{selected_run}`")
h2.metric("Points", len(run_df))
h3.metric("Keyword", run_df.iloc[0]["keyword"])
h4.metric("Device", run_df.iloc[0]["device"])

# Map of sampled points
st.markdown("#### Sampled points")
map_df = run_df[["lat", "lng"]].rename(columns={"lng": "lon"})
st.map(map_df, zoom=10, use_container_width=True)

# ---------- target domain rank tracker ----------
if target_domain:
    target_l = target_domain.lower().strip()
    rows = []
    for _, r in run_df.iterrows():
        rank = None
        for o in (r["organic_top"] or []):
            haystack = (
                (o.get("link") or "") + " " + (o.get("displayed_link") or "")
            ).lower()
            if target_l in haystack:
                rank = o.get("position")
                break
        rows.append(
            {
                "Point": int(r["point_index"]) + 1,
                "Lat": round(r["lat"], 5),
                "Lng": round(r["lng"], 5),
                "Organic rank": rank if rank is not None else None,
            }
        )
    rank_df = pd.DataFrame(rows)
    found = rank_df[rank_df["Organic rank"].notna()]
    avg = f"{found['Organic rank'].astype(int).mean():.1f}" if not found.empty else "—"

    st.markdown(f"#### Rank for `{target_domain}`")
    rc1, rc2 = st.columns(2)
    rc1.metric("Avg rank (where found)", avg)
    rc2.metric("Coverage", f"{len(found)}/{len(rank_df)} points")
    st.dataframe(
        rank_df.fillna("—"), hide_index=True, use_container_width=True
    )

st.divider()

# ---------- screenshot grid ----------
st.markdown("#### Screenshots")
COLS = 3
for i in range(0, len(run_df), COLS):
    chunk = run_df.iloc[i : i + COLS]
    cols = st.columns(COLS)
    for j, (_, r) in enumerate(chunk.iterrows()):
        with cols[j]:
            if r["screenshot_url"]:
                st.image(r["screenshot_url"], use_container_width=True)
            st.caption(
                f"**Point {int(r['point_index']) + 1}** · "
                f"{r['lat']:.4f}, {r['lng']:.4f}"
            )
            with st.expander("Results"):
                lp = r["local_pack"] or []
                if lp:
                    st.markdown("**Local pack**")
                    for p in lp:
                        rating = p.get("rating") or "—"
                        reviews = p.get("reviews") or 0
                        st.write(
                            f"{p.get('position')}. {p.get('title')} — {rating}⭐ ({reviews})"
                        )
                org = r["organic_top"] or []
                if org:
                    st.markdown("**Top organic**")
                    for o in org[:5]:
                        link = o.get("link") or "#"
                        st.write(f"{o.get('position')}. [{o.get('title')}]({link})")
