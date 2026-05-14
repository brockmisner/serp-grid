"""
SERP Grid Sampler — mobile Google rank check from random points within a radius.

Flow per point:
  1. Random lat/lng inside disk(center, radius_miles)
  2. SerpApi google search with device=mobile + lat/lon (SerpApi builds the UULE)
  3. Playwright renders SerpApi's raw_html_file at iPhone viewport → full-page PNG
  4. PNG → Supabase Storage; metadata + organic + local pack → Supabase table
"""

import os
import math
import random
import uuid
from datetime import datetime, timezone

import requests
import streamlit as st
from playwright.sync_api import sync_playwright
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

SERPAPI_KEY  = os.environ["SERPAPI_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BUCKET       = os.environ.get("SUPABASE_BUCKET", "serp-shots")
TABLE        = os.environ.get("SUPABASE_TABLE",  "serp_runs")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------- helpers ----------

def geocode(address: str):
    """Free OSM Nominatim. Returns (lat, lng) or None."""
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": "serp-grid-sampler/1.0"},
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    return (float(j[0]["lat"]), float(j[0]["lon"])) if j else None


def random_point(center_lat: float, center_lng: float, radius_miles: float):
    """Uniform random point inside disk(center, radius). Simple equirect approx."""
    radius_deg = (radius_miles * 1.60934) / 111.0
    u, v = random.random(), random.random()
    w = radius_deg * math.sqrt(u)
    t = 2 * math.pi * v
    dy = w * math.sin(t)
    dx = w * math.cos(t) / math.cos(math.radians(center_lat))
    return center_lat + dy, center_lng + dx


def serpapi_search(keyword: str, lat: float, lng: float,
                   device: str = "mobile", gl: str = "us", hl: str = "en",
                   bias_radius_m: int | None = None):
    """Call SerpApi Google search. SerpApi generates UULE internally from lat/lon."""
    params = {
        "engine": "google",
        "q": keyword,
        "device": device,
        "lat": lat,
        "lon": lng,
        "gl": gl,
        "hl": hl,
        "api_key": SERPAPI_KEY,
    }
    if bias_radius_m:
        params["radius"] = bias_radius_m  # 1..1000 m on mobile, biases local pack
    r = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def screenshot_serp(raw_html_url: str) -> bytes:
    """Load SerpApi's raw HTML in a mobile-emulated Chromium and capture full page."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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
        page = ctx.new_page()
        page.goto(raw_html_url, wait_until="networkidle", timeout=30_000)
        png = page.screenshot(full_page=True, type="png")
        browser.close()
        return png


def extract_top_organic(serp_json, n=10):
    out = []
    for r in (serp_json.get("organic_results") or [])[:n]:
        out.append({
            "position": r.get("position"),
            "title": r.get("title"),
            "link": r.get("link"),
            "displayed_link": r.get("displayed_link"),
        })
    return out


def extract_local_pack(serp_json):
    out = []
    lr = serp_json.get("local_results") or {}
    places = lr.get("places") if isinstance(lr, dict) else None
    for p in (places or [])[:5]:
        out.append({
            "position": p.get("position"),
            "title": p.get("title"),
            "rating": p.get("rating"),
            "reviews": p.get("reviews"),
            "place_id": p.get("place_id"),
        })
    return out


def upload_to_supabase(run_id: str, idx: int, png_bytes: bytes, payload: dict) -> str:
    path = f"{run_id}/{idx:02d}.png"
    sb.storage.from_(BUCKET).upload(
        path=path,
        file=png_bytes,
        file_options={"content-type": "image/png", "upsert": "true"},
    )
    url = sb.storage.from_(BUCKET).get_public_url(path)
    payload["screenshot_url"] = url
    sb.table(TABLE).insert(payload).execute()
    return url


# ---------- Streamlit UI ----------

st.set_page_config(page_title="SERP Grid Sampler", page_icon="📍", layout="centered")
st.title("📍 SERP Grid Sampler")
st.caption("Mobile Google rank check from random points within a radius.")

with st.form("run"):
    keyword = st.text_input("Keyword", placeholder="ac repair near me")
    center  = st.text_input(
        "Center (address or 'lat, lng')",
        placeholder="Fort Lauderdale, FL   — or —   26.1224, -80.1373",
    )
    c1, c2 = st.columns(2)
    with c1:
        radius_mi = st.number_input("Sample radius (miles)", 0.5, 25.0, 5.0, 0.5)
    with c2:
        n_points  = st.number_input("Random points", 1, 20, 5, 1)
    c3, c4 = st.columns(2)
    with c3:
        gl = st.selectbox("Country", ["us", "ca", "uk", "au"], 0)
    with c4:
        bias_m = st.number_input(
            "Local-pack bias (m, 0=off)", 0, 1000, 0, 50,
            help="SerpApi 'radius' param — biases local results within N meters of each point. 0 = let Google decide."
        )
    run = st.form_submit_button("Run grid", type="primary", use_container_width=True)

if run:
    if not keyword.strip() or not center.strip():
        st.error("Keyword and center are required.")
        st.stop()

    # parse center: "lat, lng" first, otherwise geocode
    try:
        a, b = [s.strip() for s in center.split(",", 1)]
        c_lat, c_lng = float(a), float(b)
    except ValueError:
        with st.spinner("Geocoding center..."):
            geo = geocode(center)
        if not geo:
            st.error("Could not geocode that address.")
            st.stop()
        c_lat, c_lng = geo
    st.info(f"Center: **{c_lat:.5f}, {c_lng:.5f}**  ·  radius **{radius_mi} mi**  ·  **{int(n_points)} points**")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    prog = st.progress(0.0, text="Starting…")
    results = []

    for i in range(int(n_points)):
        p_lat, p_lng = random_point(c_lat, c_lng, float(radius_mi))
        prog.progress((i + 0.1) / n_points, text=f"Point {i+1}/{n_points}: SerpApi search…")
        try:
            sj = serpapi_search(keyword, p_lat, p_lng, "mobile", gl,
                                bias_radius_m=int(bias_m) or None)
        except Exception as e:
            st.warning(f"Point {i+1} SerpApi error: {e}")
            continue

        raw_html_url = (sj.get("search_metadata") or {}).get("raw_html_file")
        if not raw_html_url:
            st.warning(f"Point {i+1}: SerpApi returned no raw_html_file")
            continue

        prog.progress((i + 0.5) / n_points, text=f"Point {i+1}/{n_points}: screenshot…")
        try:
            png = screenshot_serp(raw_html_url)
        except Exception as e:
            st.warning(f"Point {i+1} screenshot error: {e}")
            continue

        payload = {
            "run_id": run_id,
            "point_index": i,
            "keyword": keyword,
            "lat": p_lat,
            "lng": p_lng,
            "device": "mobile",
            "gl": gl,
            "organic_top": extract_top_organic(sj, 10),
            "local_pack":  extract_local_pack(sj),
            "created_at":  datetime.now(timezone.utc).isoformat(),
        }

        prog.progress((i + 0.9) / n_points, text=f"Point {i+1}/{n_points}: upload…")
        try:
            url = upload_to_supabase(run_id, i, png, payload)
        except Exception as e:
            st.warning(f"Point {i+1} upload error: {e}")
            continue

        results.append({**payload, "png": png, "screenshot_url": url})
        prog.progress((i + 1) / n_points, text=f"Point {i+1}/{n_points} ✓")

    prog.empty()
    st.success(f"Done. Run ID: `{run_id}` — {len(results)}/{int(n_points)} points captured.")

    for r in results:
        with st.expander(f"Point {r['point_index']+1}  ·  {r['lat']:.4f}, {r['lng']:.4f}"):
            st.image(r["png"], width=320)
            if r["local_pack"]:
                st.markdown("**Local pack**")
                st.json(r["local_pack"])
            st.markdown("**Top organic**")
            st.json(r["organic_top"])
            st.markdown(f"[Open screenshot]({r['screenshot_url']})")
