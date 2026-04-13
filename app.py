"""
app.py -- MaintainX Dashboards Web App
=======================================
Flask wrapper that serves the Work Order and Purchase Order dashboards
from the MaintainX API with two-layer caching:

  Layer 1 — in-memory (5 min TTL):  absorbs rapid back-to-back requests
  Layer 2 — file cache (60 min TTL): survives cold starts, prevents both
            dashboards from hammering the API at the same time

Routes:
    GET /   -- Home page
    GET /wo -- Open Work Order dashboard
    GET /po -- Purchase Orders (AP) dashboard
"""

import os
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, render_template

# ── Vercel detection ──────────────────────────────────────────────────────────────
IS_VERCEL = os.environ.get("VERCEL") == "1"

import maintainx_po_report as po
if IS_VERCEL:
    po.CACHE_FILE = Path("/tmp/po_cache.json")

import maintainx_dashboard as mxd

app = Flask(__name__)

# ── Cache paths ───────────────────────────────────────────────────────────────────
_TMP = Path("/tmp") if IS_VERCEL else Path(__file__).parent
WO_HTML_CACHE_FILE = _TMP / "wo_html_cache.json"

# ── Layer 1: in-memory cache (5 min) ─────────────────────────────────────────────
CACHE_TTL_MINUTES = 5
_mem_cache: dict[str, dict] = {}


def _mem_get(key: str):
    entry = _mem_cache.get(key)
    if entry and datetime.now() < entry["expires"]:
        return entry["html"]
    return None


def _mem_set(key: str, html: str):
    _mem_cache[key] = {
        "html":    html,
        "expires": datetime.now() + timedelta(minutes=CACHE_TTL_MINUTES),
    }


# ── Layer 2: file cache (60 min) ─────────────────────────────────────────────────
FILE_CACHE_TTL_MINUTES = 60


def _file_get(path: Path):
    """Return cached HTML if the file exists and is under 60 min old."""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        saved_at = datetime.fromisoformat(data["saved_at"])
        age_min = (datetime.now(timezone.utc) - saved_at).total_seconds() / 60
        if age_min < FILE_CACHE_TTL_MINUTES:
            return data["html"]
    except Exception:
        pass
    return None


def _file_set(path: Path, html: str):
    """Write HTML to file cache with a UTC timestamp."""
    try:
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "html": html,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────────

def _get_api_key():
    key = os.environ.get("MAINTAINX_API_KEY", "").strip()
    if key:
        return key
    key_file = Path(__file__).parent / "MaintainX_API_key.txt"
    if key_file.exists():
        key = key_file.read_text().strip()
        if key:
            return key
    return None


def _rate_limit_page():
    """Shown when MaintainX API is rate-limited. Auto-retries after 90 seconds."""
    html = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Loading...</title>
<style>
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f1f5f9;display:flex;align-items:center;
       justify-content:center;height:100vh;margin:0;}
  .card{background:#fff;border-radius:10px;padding:36px 44px;
        box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:480px;text-align:center;}
  h1{color:#d97706;font-size:1.2rem;margin-bottom:12px;}
  p{color:#6b7280;font-size:.9rem;line-height:1.6;}
  .counter{font-size:2.5rem;font-weight:700;color:#0f2d52;margin:16px 0;}
  .btn{display:inline-block;margin-top:16px;padding:8px 20px;
       background:#2563eb;color:#fff;border-radius:6px;font-size:.85rem;
       text-decoration:none;}
  a{color:#2563eb;text-decoration:none;}
</style>
</head>
<body>
  <div class="card">
    <h1>API Rate Limit Reached</h1>
    <p>MaintainX limits API requests per minute. This typically happens
       right after the Work Order dashboard loads for the first time.</p>
    <div class="counter" id="t">90</div>
    <p>Auto-retrying in <strong id="s">90</strong> seconds&hellip;</p>
    <a href="/po" class="btn">Retry now</a>
    &nbsp;&nbsp;
    <a href="/">&#8592; Home</a>
  </div>
  <script>
    var n=90;
    var iv=setInterval(function(){
      n--;document.getElementById('t').textContent=n;
      document.getElementById('s').textContent=n;
      if(n<=0){clearInterval(iv);window.location.href='/po';}
    },1000);
  </script>
</body>
</html>"""
    return Response(html, status=200, content_type="text/html")


def _error_page(message, status=500):
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Error</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f1f5f9;display:flex;align-items:center;
       justify-content:center;height:100vh;margin:0;}}
  .card{{background:#fff;border-radius:10px;padding:36px 44px;
        box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:480px;text-align:center;}}
  h1{{color:#dc2626;font-size:1.2rem;margin-bottom:12px;}}
  p{{color:#6b7280;font-size:.9rem;line-height:1.5;}}
  a{{color:#2563eb;text-decoration:none;}}
</style>
</head>
<body>
  <div class="card">
    <h1>Something went wrong</h1>
    <p>{message}</p>
    <p style="margin-top:16px"><a href="/">&#8592; Back to home</a></p>
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
    """Work Order dashboard with two-layer caching (memory + file)."""
    api_key = _get_api_key()
    if not api_key:
        return _error_page(
            "MAINTAINX_API_KEY is not configured. "
            "Add it in Vercel Project Settings then redeploy.",
            status=500,
        )

    # Layer 1: in-memory
    html = _mem_get("wo")
    if html:
        return Response(html, content_type="text/html")

    # Layer 2: file cache (survives cold starts)
    html = _file_get(WO_HTML_CACHE_FILE)
    if html:
        _mem_set("wo", html)
        return Response(html, content_type="text/html")

    # Layer 3: fetch from API
    try:
        wos = mxd.fetch_all_open_work_orders(api_key)
        lines_dict, non_line = mxd.compute_line_scores(wos)
        areas_dict = mxd.compute_area_scores(non_line)
        generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        html = mxd.build_html(lines_dict, areas_dict, wos, generated_at)
        _mem_set("wo", html)
        _file_set(WO_HTML_CACHE_FILE, html)
        return Response(html, content_type="text/html")
    except Exception as e:
        return _error_page(f"Failed to fetch work orders from MaintainX: {e}")


@app.route("/po")
def po_dashboard():
    """Purchase Order dashboard with two-layer caching (memory + file via po module)."""
    api_key = _get_api_key()
    if not api_key:
        return _error_page(
            "MAINTAINX_API_KEY is not configured. "
            "Add it in Vercel Project Settings then redeploy.",
            status=500,
        )

    # Layer 1: in-memory
    html = _mem_get("po")
    if html:
        return Response(html, content_type="text/html")

    # Layers 2+3 handled inside fetch_completed_pos (file cache + API)
    try:
        pos = po.fetch_completed_pos(api_key, force_refresh=False, fetch_vendors=False)
        generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        html = po.build_po_html(pos, generated_at)
        _mem_set("po", html)
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
