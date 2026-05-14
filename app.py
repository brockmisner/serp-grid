"""
SERP Grid Runner — random points OR bulk CSV.

Tab 1: scatter N random points inside a radius around a center.
Tab 2: upload a CSV, one search per row. Each row needs keyword + EITHER
       (lat AND lng) OR uule. Rows can mix the two styles.
"""

import math
import os
import random
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from supabase import create_client

load_dotenv()

SERPAPI_KEY  = os.environ["SERPAPI_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BUCKET       = os.environ.get("SUPABASE_BUCKET", "serp-shots")
TABLE        = os.environ.get("SUPABASE_TABLE",  "serp_runs")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------- helpers ----------

def geocode(address: str):
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": "serp-grid-sampler/1.0"},
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    return (float(j[0]["lat"]), float(j[0]["lon"])) if j else None


def random_point(center_lat, center_lng, radius_miles):
    radius_deg = (radius_miles * 1.60934) / 111.0
    u, v = random.random(), random.random()
    w = radius_deg * math.sqrt(u)
    t = 2 * math.pi * v
    dy = w * math.sin(t)
    dx = w * math.cos(t) / math.cos(math.radians(center_lat))
    return center_lat + dy, center_lng + dx


def serpapi_search(
    keyword,
    lat=None, lng=None, uule=None,
    device="mobile", gl="us", hl="en",
    bias_radius_m=None,
):
    params = {
        "engine": "google",
        "q": keyword,
        "device": device,
        "gl": gl,
        "hl": hl,
        "api_key": SERPAPI_KEY,
    }
    # uule wins over lat/lon — SerpApi rejects both at once
    if uule:
        params["uule"] = uule
    elif lat is not None and lng is not None:
        params["lat"] = lat
        params["lon"] = lng
    else:
        raise ValueError("Either uule or (lat, lng) is required.")
    if bias_radius_m:
        params["radius"] = bias_radius_m
    r = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


@contextmanager
def mobile_browser():
    """One chromium context per batch — much faster than launching per shot."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        ctx = browser.new_context(
            viewport={"width": 393, "height": 852},
            device_scale_factor=3,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
                "Mobile/15E148 Safari/604.1"
            ),
            is_mobile=True,
            has_touch=True,
        )
        try:
            yield ctx
        finally:
            # Suppress benign disconnection errors that fire after long batches —
            # by this point all uploads have already succeeded.
            for closer in (ctx.close, browser.close):
                try:
                    closer()
                except Exception:
                    pass


def screenshot(ctx, raw_html_url: str) -> bytes:
    """
    Use wait_until='load' (not 'networkidle'). Google SERPs have tracking
    pixels that keep the network 'busy' forever; networkidle was burning the
    full 30 s timeout on every shot and starving the driver after ~100 rows.
    """
    page = ctx.new_page()
    try:
        page.goto(raw_html_url, wait_until="load", timeout=20_000)
        page.wait_for_timeout(800)  # let any post-load paint settle
        return page.screenshot(full_page=True, type="png")
    finally:
        try:
            page.close()
        except Exception:
            pass


def extract_top_organic(sj, n=10):
    return [
        {
            "position": r.get("position"),
            "title": r.get("title"),
            "link": r.get("link"),
            "displayed_link": r.get("displayed_link"),
        }
        for r in (sj.get("organic_results") or [])[:n]
    ]


def extract_local_pack(sj):
    lr = sj.get("local_results") or {}
    places = lr.get("places") if isinstance(lr, dict) else []
    return [
        {
            "position": p.get("position"),
            "title": p.get("title"),
            "rating": p.get("rating"),
            "reviews": p.get("reviews"),
            "place_id": p.get("place_id"),
        }
        for p in (places or [])[:5]
    ]


def upload(run_id, idx, png_bytes, payload):
    path = f"{run_id}/{idx:03d}.png"
    sb.storage.from_(BUCKET).upload(
        path=path,
        file=png_bytes,
        file_options={"content-type": "image/png", "upsert": "true"},
    )
    url = sb.storage.from_(BUCKET).get_public_url(path)
    payload["screenshot_url"] = url
    sb.table(TABLE).insert(payload).execute()
    return url


def run_jobs(jobs, run_id, container):
    total = len(jobs)
    prog = container.progress(0.0, text="Starting…")
    results = []

    with mobile_browser() as ctx:
        for i, job in enumerate(jobs):
            label = job.get("label") or f"row {i+1}"
            prog.progress((i + 0.1) / total, text=f"{i+1}/{total} ({label}): search…")
            try:
                sj = serpapi_search(
                    keyword=job["keyword"],
                    lat=job.get("lat"),
                    lng=job.get("lng"),
                    uule=job.get("uule"),
                    device=job.get("device", "mobile"),
                    gl=job.get("gl", "us"),
                    bias_radius_m=job.get("bias_radius_m"),
                )
            except Exception as e:
                container.warning(f"#{i+1} ({label}) SerpApi error: {e}")
                continue

            raw_html_url = (sj.get("search_metadata") or {}).get("raw_html_file")
            if not raw_html_url:
                container.warning(f"#{i+1} ({label}): no raw_html_file from SerpApi")
                continue

            prog.progress((i + 0.5) / total, text=f"{i+1}/{total} ({label}): screenshot…")
            try:
                png = screenshot(ctx, raw_html_url)
            except Exception as e:
                container.warning(f"#{i+1} ({label}) screenshot error: {e}")
                continue

            payload = {
                "run_id": run_id,
                "point_index": i,
                "keyword": job["keyword"],
                "lat": job.get("lat"),
                "lng": job.get("lng"),
                "device": job.get("device", "mobile"),
                "gl": job.get("gl", "us"),
                "label": job.get("label"),
                "organic_top": extract_top_organic(sj, 10),
                "local_pack": extract_local_pack(sj),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            prog.progress((i + 0.9) / total, text=f"{i+1}/{total} ({label}): upload…")
            try:
                url = upload(run_id, i, png, payload)
            except Exception as e:
                container.warning(f"#{i+1} ({label}) upload error: {e}")
                continue

            results.append({**payload, "png": png, "screenshot_url": url})
            prog.progress((i + 1) / total, text=f"{i+1}/{total} ({label}) ✓")

    prog.empty()
    return results


def show_results(results, container):
    if not results:
        container.error("Nothing captured.")
        return
    for r in results:
        title = f"#{r['point_index']+1}"
        if r.get("label"):
            title += f"  ·  {r['label']}"
        if r.get("lat") is not None:
            title += f"  ·  {r['lat']:.4f}, {r['lng']:.4f}"
        with container.expander(title):
            st.image(r["png"], width=320)
            if r.get("local_pack"):
                st.write("**Local pack**")
                st.json(r["local_pack"])
            st.write("**Top organic**")
            st.json(r["organic_top"])
            st.write(f"[Screenshot URL]({r['screenshot_url']})")


# ---------- UI ----------

st.set_page_config(page_title="SERP Grid Runner", page_icon="📍", layout="centered")
st.title("📍 SERP Grid Runner")
st.caption("Mobile Google rank check — random points or bulk CSV.")

tab_random, tab_bulk = st.tabs(["🎯 Random grid", "📋 Bulk CSV"])


# ===== Tab 1: Random grid =====
with tab_random:
    with st.form("random_form"):
        keyword = st.text_input("Keyword", placeholder="ac repair near me")
        center = st.text_input(
            "Center (address or 'lat, lng')",
            placeholder="Fort Lauderdale, FL  — or —  26.1224, -80.1373",
        )
        c1, c2 = st.columns(2)
        with c1:
            radius_mi = st.number_input("Sample radius (miles)", 0.5, 25.0, 5.0, 0.5)
        with c2:
            n_points = st.number_input("Random points", 1, 50, 5, 1)
        c3, c4 = st.columns(2)
        with c3:
            gl = st.selectbox("Country", ["us", "ca", "uk", "au"], 0)
        with c4:
            bias_m = st.number_input(
                "Local-pack bias (m, 0=off)", 0, 1000, 0, 50,
                help="SerpApi 'radius' param. 1–1000 m on mobile.",
            )
        run = st.form_submit_button("Run grid", type="primary", use_container_width=True)

    if run:
        if not keyword.strip() or not center.strip():
            st.error("Keyword and center are required.")
            st.stop()
        try:
            a, b = [s.strip() for s in center.split(",", 1)]
            c_lat, c_lng = float(a), float(b)
        except ValueError:
            with st.spinner("Geocoding…"):
                geo = geocode(center)
            if not geo:
                st.error("Could not geocode that address.")
                st.stop()
            c_lat, c_lng = geo
        st.info(
            f"Center: **{c_lat:.5f}, {c_lng:.5f}**  ·  "
            f"radius **{radius_mi} mi**  ·  **{int(n_points)} points**"
        )

        jobs = []
        for _ in range(int(n_points)):
            p_lat, p_lng = random_point(c_lat, c_lng, float(radius_mi))
            jobs.append({
                "keyword": keyword,
                "lat": p_lat,
                "lng": p_lng,
                "gl": gl,
                "bias_radius_m": int(bias_m) or None,
            })

        run_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-" + uuid.uuid4().hex[:6]
        )
        results = run_jobs(jobs, run_id, st)
        st.success(f"Done. Run ID: `{run_id}` — {len(results)}/{len(jobs)} captured.")
        show_results(results, st)


# ===== Tab 2: Bulk CSV =====
with tab_bulk:
    st.markdown(
        "**One search per row.** Required: `keyword` + either (`lat` AND `lng`) **or** `uule`.\n\n"
        "Optional: `label`, `device` (default `mobile`), `gl` (default `us`), `bias_radius_m`.\n\n"
        "Mixing styles is fine — some rows lat/lng, others uule."
    )

    template = pd.DataFrame([
        {"keyword": "ac repair near me",   "lat": 26.1224, "lng": -80.1373, "uule": "",                                                              "label": "Downtown Fort Lauderdale"},
        {"keyword": "ac repair near me",   "lat": 26.1901, "lng": -80.1255, "uule": "",                                                              "label": "Wilton Manors"},
        {"keyword": "emergency ac repair", "lat": "",      "lng": "",       "uule": "w+CAIQICIfRm9ydCBMYXVkZXJkYWxlLEZsb3JpZGEsVW5pdGVkIFN0YXRlcw", "label": "Fort Lauderdale (canonical, UULE only)"},
    ])
    st.download_button(
        "⬇︎ Download CSV template",
        template.to_csv(index=False).encode(),
        file_name="serp_grid_template.csv",
        mime="text/csv",
    )

    f = st.file_uploader("Upload CSV", type=["csv"])
    if f is not None:
        try:
            df_csv = pd.read_csv(f)
        except Exception as e:
            st.error(f"Could not read CSV: {e}")
            st.stop()

        if "keyword" not in df_csv.columns:
            st.error("CSV must have a `keyword` column.")
            st.stop()

        has_uule   = "uule" in df_csv.columns
        has_latlng = "lat" in df_csv.columns and "lng" in df_csv.columns
        if not has_uule and not has_latlng:
            st.error("CSV needs either (`lat` AND `lng`) columns or a `uule` column.")
            st.stop()

        df_csv = df_csv.dropna(subset=["keyword"]).reset_index(drop=True)
        df_csv["keyword"] = df_csv["keyword"].astype(str)
        if has_latlng:
            df_csv["lat"] = pd.to_numeric(df_csv["lat"], errors="coerce")
            df_csv["lng"] = pd.to_numeric(df_csv["lng"], errors="coerce")

        def row_has_loc(row):
            uule = str(row.get("uule") or "").strip() if has_uule else ""
            if uule:
                return True
            if has_latlng and pd.notna(row.get("lat")) and pd.notna(row.get("lng")):
                return True
            return False

        df_csv = df_csv[df_csv.apply(row_has_loc, axis=1)].reset_index(drop=True)
        if df_csv.empty:
            st.error("No usable rows — each row needs (lat AND lng) or a uule.")
            st.stop()

        st.markdown(f"**{len(df_csv)} rows** loaded.")
        st.dataframe(df_csv, hide_index=True, use_container_width=True)

        # Map only the rows that have lat/lng
        if has_latlng:
            map_df = (
                df_csv.dropna(subset=["lat", "lng"])[["lat", "lng"]]
                .rename(columns={"lng": "lon"})
            )
            if not map_df.empty:
                st.map(map_df, zoom=9, use_container_width=True)
            else:
                st.info("All rows are uule-only — no map preview.")

        run = st.button(
            f"Run all {len(df_csv)} searches",
            type="primary",
            use_container_width=True,
        )
        if run:
            def pick(row, col, default=None, cast=None):
                if col in row and pd.notna(row.get(col)):
                    v = row[col]
                    if isinstance(v, str) and not v.strip():
                        return default
                    if cast is not None:
                        try:
                            v = cast(v)
                        except (TypeError, ValueError):
                            return default
                    return v
                return default

            jobs = []
            for _, row in df_csv.iterrows():
                uule_val = pick(row, "uule") if has_uule else None
                job = {
                    "keyword": row["keyword"],
                    "device": pick(row, "device", "mobile"),
                    "gl": pick(row, "gl", "us"),
                    "label": pick(row, "label"),
                    "bias_radius_m": pick(row, "bias_radius_m", cast=int),
                }
                if uule_val:
                    job["uule"] = uule_val
                    # store lat/lng too if present, just for map display
                    if has_latlng and pd.notna(row.get("lat")) and pd.notna(row.get("lng")):
                        job["lat"] = float(row["lat"])
                        job["lng"] = float(row["lng"])
                else:
                    job["lat"] = float(row["lat"])
                    job["lng"] = float(row["lng"])
                jobs.append(job)

            run_id = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                + "-" + uuid.uuid4().hex[:6]
            )
            results = run_jobs(jobs, run_id, st)
            st.success(f"Done. Run ID: `{run_id}` — {len(results)}/{len(jobs)} captured.")
            show_results(results, st)
