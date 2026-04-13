"""
app.py — MaintainX Dashboards Web App
======================================
Flask wrapper that serves the Work Order and Purchase Order dashboards live
from the MaintainX API. No local database required.

Routes:
    GET /        — Home page with links to both dashboards
    GET /wo      — Open Work Order dashboard (fetched live from API)
    GET /po      — Purchase Orders (AP) dashboard (fetched live from API)

Configuration:
    Set MAINTAINX_API_KEY as an environment variable.
    On Vercel: add it in Project Settings → Environment Variables.
    Locally:   create a .env file (see .env.example).

Running locally:
    pip install -r requirements.txt
    python app.py
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

from flask import Flask, Response, render_template

# ── Vercel detection ─────────────────────────────────────────────────────────────
# On Vercel the filesystem is read-only; redirect the PO cache to /tmp so the
# module can still write it without crashing (cache persists for the lifetime of
# a warm serverless instance, which is fine).
IS_VERCEL = os.environ.get("VERCEL") == "1"

import maintainx_po_report as po
if IS_VERCEL:
    po.CACHE_FILE = Path("/tmp/po_cache.json")

import maintainx_dashboard as mxd

app = Flask(__name__)

# ── In-memory response cache ──────────────────────────────────────────────────────
# Stores rendered HTML for each dashboard so repeated page loads within the TTL
# window don't hammer the MaintainX API and trigger 429 rate-limit errors.
# The cache lives in the serverless instance's memory — it resets on cold starts
# but that's fine; the goal is just to absorb rapid back-to-back requests.
CACHE_TTL_MINUTES = 5

_cache: dict[str, dict] = {}   # key → {"html": str, "expires": datetime}


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and datetime.now() < entry["expires"]:
        return entry["html"]
    return None


def _cache_set(key: str, html: str):
    _cache[key] = {
        "html":    html,
        "expires": datetime.now() + timedelta(minutes=CACHE_TTL_MINUTES),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────────

def _get_api_key():
    """Return the MaintainX API key, or None if not configured."""
    key = os.environ.get("MAINTAINX_API_KEY", "").strip()
    if key:
        return key
    # Local fallback: read from key file next to this script
    key_file = Path(__file__).parent / "MaintainX_API_key.txt"
    if key_file.exists():
        key = key_file.read_text().strip()
        if key:
            return key
    return None


def _rate_limit_page():
    """Shown when MaintainX API rate limit is hit. Auto-retries after 90 seconds."""
    html = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Loading...</title>
<meta http-equiv="refresh" content="90">
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f1f5f9;display:flex;align-items:center;
       justify-content:center;height:100vh;margin:0;}
  .card{background:#fff;border-radius:10px;padding:36px 44px;
        box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:480px;text-align:center;}
  h1{color:#d97706;font-size:1.2rem;margin-bottom:12px;}
  p{color:#6b7280;font-size:.9rem;line-height:1.6;}
  .counter{font-size:2rem;font-weight:700;color:#0f2d52;margin:16px 0;}
  a{color:#2563eb;text-decoration:none;}
  .btn{display:inline-block;margin-top:16px;padding:8px 20px;
       background:#2563eb;color:#fff;border-radius:6px;font-size:.85rem;
       text-decoration:none;}
</style>
</head>
<body>
  <div class="card">
    <h1>API Rate Limit Reached</h1>
    <p>MaintainX limits how many requests can be made per minute.
       This usually happens right after the Work Order dashboard loads.</p>
    <div class="counter" id="t">90</div>
    <p>Retrying automatically in <strong id="s">90</strong> seconds&hellip;</p>
    <a href="/po" class="btn">Retry now</a>
    &nbsp;
    <a href="/">&#8592; Home</a>
  </div>
  <script>
    var n=90;
    var iv=setInterval(function(){
      n--;
      document.getElementById('t').textContent=n;
      document.getElementById('s').textContent=n;
      if(n<=0){clearInterval(iv);window.location.href='/po';}
    },1000);
  </script>
</body>
</html>"""
    return Response(html, status=429, content_type="text/html")


def _error_page(message, status=500):
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Error</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f1f5f9; display: flex; align-items: center;
         justify-content: center; height: 100vh; margin: 0; }}
  .card {{ background: #fff; border-radius: 10px; padding: 36px 44px;
           box-shadow: 0 2px 12px rgba(0,0,0,.08); max-width: 480px; text-align: center; }}
  h1 {{ color: #dc2626; font-size: 1.2rem; margin-bottom: 12px; }}
  p  {{ color: #6b7280; font-size: .9rem; line-height: 1.5; }}
  a  {{ color: #2563eb; text-decoration: none; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Something went wrong</h1>
    <p>{message}</p>
    <p style="margin-top:16px"><a href="/">← Back to home</a></p>
  </div>
</body>
</html>"""
    return Response(html, status=status, content_type="text/html")


# ── Routes ────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("home.html")


@app.route("/wo")
def wo_dashboard():
    """Fetch open work orders and return the ranked HTML dashboard.
    Results are cached in memory for CACHE_TTL_MINUTES to avoid rate limiting."""
    api_key = _get_api_key()
    if not api_key:
        return _error_page(
            "MAINTAINX_API_KEY is not configured. "
            "Add it in Vercel's Environment Variables settings, then redeploy.",
            status=500,
        )

    cached = _cache_get("wo")
    if cached:
        return Response(cached, content_type="text/html")

    try:
        wos = mxd.fetch_all_open_work_orders(api_key)
        lines_dict, non_line = mxd.compute_line_scores(wos)
        areas_dict = mxd.compute_area_scores(non_line)
        generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        html = mxd.build_html(lines_dict, areas_dict, wos, generated_at)
        _cache_set("wo", html)
        return Response(html, content_type="text/html")
    except Exception as e:
        return _error_page(f"Failed to fetch work orders from MaintainX: {e}")


@app.route("/po")
def po_dashboard():
    """Fetch completed purchase orders and return the AP HTML dashboard.
    Results are cached in memory for CACHE_TTL_MINUTES to avoid rate limiting."""
    api_key = _get_api_key()
    if not api_key:
        return _error_page(
            "MAINTAINX_API_KEY is not configured. "
            "Add it in Vercel's Environment Variables settings, then redeploy.",
            status=500,
        )

    cached = _cache_get("po")
    if cached:
        return Response(cached, content_type="text/html")

    try:
        pos = po.fetch_completed_pos(api_key, force_refresh=False, fetch_vendors=False)
        generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        html = po.build_po_html(pos, generated_at)
        _cache_set("po", html)
        return Response(html, content_type="text/html")
    except Exception as e:
        if "429" in str(e):
            return _rate_limit_page()
        return _error_page(f"Failed to fetch purchase orders from MaintainX: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _get_api_key():
        print("\nWARNING: MAINTAINX_API_KEY not found.")
        print("  Set it as an environment variable, or place your key in MaintainX_API_key.txt\n")

    print("\n  MaintainX Dashboards")
    print("  Open your browser to: http://127.0.0.1:5000")
    print("  Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
