"""
app.py -- MaintainX Dashboards Web App
=======================================
Flask wrapper that serves the Work Order and Purchase Order dashboards
from the MaintainX API with two-layer caching:

  Layer 1 — in-memory (5 min TTL):  absorbs rapid back-to-back requests
  Layer 2 — file cache (60 min TTL): survives cold starts on warm containers

Routes:
    GET /           -- Home page
    GET /wo         -- Open Work Order dashboard
    GET /po         -- Purchase Orders (AP) dashboard (list-only, ~2 API calls)
    GET /po/refresh -- Trigger a full PO detail refresh (fetches every PO individually)
"""

import os
import json
import traceback
from pathlib import Path
from datetime import datetime, timedelta, timezone

import functools
import requests as _http
from flask import (Flask, Response, render_template, request, jsonify,
                   session, redirect, url_for)

# ── Vercel detection ──────────────────────────────────────────────────────────────
IS_VERCEL = os.environ.get("VERCEL") == "1"

import maintainx_po_report as po

import maintainx_dashboard as mxd

app = Flask(__name__)

# ── Session security ──────────────────────────────────────────────────────────────
# SECRET_KEY signs the session cookie — set this in Vercel env vars.
# APP_PASSWORD is the shared login password — also set in Vercel env vars.
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

# ── Site configuration ────────────────────────────────────────────────────────────
SITES = {
    "mckinney": {
        "label":       "McKinney",
        "badge_color": "#1d4ed8",
        "badge_bg":    "#dbeafe",
        "api_key_env": "MAINTAINX_API_KEY",          # existing env var
        "po_cache":    "po_cache_mckinney.json",
        "vendor_cache":"vendor_cache_mckinney.json",
        "wo_cache":    "wo_html_cache_mckinney.json",
    },
    "mtvernon": {
        "label":       "Mt. Vernon",
        "badge_color": "#6d28d9",
        "badge_bg":    "#ede9fe",
        "api_key_env": "MAINTAINX_API_KEY_MTVERNON",  # new env var
        "po_cache":    "po_cache_mtvernon.json",
        "vendor_cache":"vendor_cache_mtvernon.json",
        "wo_cache":    "wo_html_cache_mtvernon.json",
    },
}
DEFAULT_SITE = "mckinney"

# ── Cache paths ───────────────────────────────────────────────────────────────────
_TMP = Path("/tmp") if IS_VERCEL else Path(__file__).parent


def _cache_path(filename):
    return _TMP / filename

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


# ── Auth ─────────────────────────────────────────────────────────────────────────

def _check_password(entered):
    """Compare entered password against APP_PASSWORD env var."""
    expected = os.environ.get("APP_PASSWORD", "").strip()
    if not expected:
        return False          # no password configured → deny everyone
    return entered.strip() == expected


def login_required(f):
    """Decorator: redirect to /login if the user is not authenticated."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def _login_page(error=None):
    err_block = (
        f'<p style="color:#dc2626;font-size:.85rem;margin-top:8px">{error}</p>'
        if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign In</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:#f1f5f9;display:flex;align-items:center;
       justify-content:center;min-height:100vh}}
  .card{{background:#fff;border-radius:12px;padding:40px 44px;
        box-shadow:0 4px 24px rgba(0,0,0,.08);width:100%;max-width:380px}}
  .logo{{text-align:center;margin-bottom:28px}}
  .logo h1{{font-size:1.15rem;font-weight:700;color:#0f2d52;margin-top:10px}}
  .logo p{{font-size:.8rem;color:#94a3b8;margin-top:3px}}
  label{{display:block;font-size:.8rem;font-weight:600;color:#374151;margin-bottom:5px}}
  input[type=password]{{width:100%;border:1px solid #d1d5db;border-radius:7px;
    padding:10px 14px;font-size:.9rem;color:#1e293b;outline:none;
    transition:border-color .15s,box-shadow .15s}}
  input[type=password]:focus{{border-color:#2563eb;
    box-shadow:0 0 0 3px rgba(37,99,235,.15)}}
  button{{width:100%;margin-top:20px;padding:11px;background:#0f2d52;color:#fff;
    border:none;border-radius:7px;font-size:.9rem;font-weight:600;
    cursor:pointer;transition:background .15s}}
  button:hover{{background:#1e40af}}
</style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <svg width="40" height="40" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect width="40" height="40" rx="10" fill="#0f2d52"/>
        <path d="M12 28V14h6l2 4 2-4h6v14h-4v-8l-4 6-4-6v8z" fill="#fff"/>
      </svg>
      <h1>MaintainX Dashboards</h1>
      <p>Accounts Payable Portal</p>
    </div>
    <form method="POST" action="/login">
      <input type="hidden" name="next" value="{request.args.get('next', '/')}">
      <label for="password">Password</label>
      <input type="password" id="password" name="password"
             placeholder="Enter your password" autofocus autocomplete="current-password">
      {err_block}
      <button type="submit">Sign In</button>
    </form>
  </div>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))

    if request.method == "POST":
        entered  = request.form.get("password", "")
        next_url = request.form.get("next", "/")
        if _check_password(entered):
            session["logged_in"] = True
            session.permanent    = False   # session ends when browser closes
            # Safety: only redirect to internal paths
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = "/"
            return redirect(next_url)
        return Response(
            _login_page(error="Incorrect password. Please try again."),
            status=401, content_type="text/html"
        )

    return Response(_login_page(), status=200, content_type="text/html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Helpers ───────────────────────────────────────────────────────────────────────

def _get_site():
    """Return the active site key from session, defaulting to mckinney."""
    return session.get("site", DEFAULT_SITE)


def _get_site_cfg():
    """Return the SITES config dict for the active site."""
    return SITES.get(_get_site(), SITES[DEFAULT_SITE])


def _get_api_key(site=None):
    """Return the MaintainX API key for the given site (or active session site)."""
    cfg     = SITES.get(site, _get_site_cfg())
    env_var = cfg["api_key_env"]
    key     = os.environ.get(env_var, "").strip()
    if key:
        return key
    # Local dev fallback: read from file (McKinney only)
    if env_var == "MAINTAINX_API_KEY":
        key_file = Path(__file__).parent / "MaintainX_API_key.txt"
        if key_file.exists():
            key = key_file.read_text().strip()
            if key:
                return key
    return None


def _configure_po_module_for_site(site=None):
    """Point po module cache files at the correct per-site paths."""
    cfg = SITES.get(site, _get_site_cfg())
    po.CACHE_FILE        = _cache_path(cfg["po_cache"])
    po.VENDOR_CACHE_FILE = _cache_path(cfg["vendor_cache"])


def _rate_limit_page(wait_seconds=65):
    """Shown when MaintainX API is rate-limited.
    wait_seconds: the X-Rate-Limit-Reset value from the 429 response.
    Adds a 5-second buffer and auto-retries /po when the countdown reaches 0.
    """
    countdown = max(int(wait_seconds) + 5, 15)  # add buffer; minimum 15 s
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Loading...</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f1f5f9;display:flex;align-items:center;
       justify-content:center;height:100vh;margin:0;}}
  .card{{background:#fff;border-radius:10px;padding:36px 44px;
        box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:480px;text-align:center;}}
  h1{{color:#d97706;font-size:1.2rem;margin-bottom:12px;}}
  p{{color:#6b7280;font-size:.9rem;line-height:1.6;}}
  .counter{{font-size:2.5rem;font-weight:700;color:#0f2d52;margin:16px 0;}}
  .btn{{display:inline-block;margin-top:16px;padding:8px 20px;
       background:#2563eb;color:#fff;border-radius:6px;font-size:.85rem;
       text-decoration:none;}}
  a{{color:#2563eb;text-decoration:none;}}
</style>
</head>
<body>
  <div class="card">
    <h1>API Rate Limit Reached</h1>
    <p>MaintainX limits API requests per minute. This typically happens
       right after the Work Order dashboard loads for the first time.</p>
    <div class="counter" id="t">{countdown}</div>
    <p>Auto-retrying in <strong id="s">{countdown}</strong> seconds&hellip;</p>
    <a href="/po" class="btn">Retry now</a>
    &nbsp;&nbsp;
    <a href="/">&#8592; Home</a>
  </div>
  <script>
    var n={countdown};
    var iv=setInterval(function(){{
      n--;document.getElementById('t').textContent=n;
      document.getElementById('s').textContent=n;
      if(n<=0){{clearInterval(iv);window.location.href='/po';}}
    }},1000);
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
@login_required
def index():
    return render_template("home.html")


# ── Legacy redirects (bookmarks / old links still work) ───────────────────────────
@app.route("/wo")
@login_required
def wo_legacy():
    return redirect(url_for("wo_dashboard", site=DEFAULT_SITE))


@app.route("/po")
@login_required
def po_legacy():
    return redirect(url_for("po_dashboard", site=DEFAULT_SITE))


@app.route("/po/refresh")
@login_required
def po_refresh_legacy():
    return redirect(url_for("po_refresh", site=DEFAULT_SITE))


# ── Site-aware routes ─────────────────────────────────────────────────────────────

def _site_header_html(site, label):
    """Small coloured site badge for injection into dashboard headers."""
    cfg = SITES.get(site, SITES[DEFAULT_SITE])
    other_site  = "mtvernon" if site == "mckinney" else "mckinney"
    other_label = SITES[other_site]["label"]
    return (
        f'<span style="display:inline-flex;align-items:center;gap:6px;'
        f'padding:3px 10px;border-radius:12px;font-size:.75rem;font-weight:700;'
        f'background:{cfg["badge_bg"]};color:{cfg["badge_color"]};'
        f'margin-right:6px">{label}</span>'
        f'<a href="/" style="font-size:.75rem;color:#94a3b8;text-decoration:none;'
        f'padding:3px 9px;border:1px solid #e2e8f0;border-radius:6px;white-space:nowrap">'
        f'Switch to {other_label}</a>'
    )


@app.route("/site/<site>/wo")
@login_required
def wo_dashboard(site):
    if site not in SITES:
        return redirect(url_for("index"))
    session["site"] = site

    api_key = _get_api_key(site)
    if not api_key:
        return _error_page(
            f"API key for {SITES[site]['label']} is not configured. "
            "Add it in Vercel Project Settings then redeploy.",
            status=500,
        )

    mem_key      = f"wo_{site}"
    wo_cache_file = _cache_path(SITES[site]["wo_cache"])

    html = _mem_get(mem_key)
    if html:
        return Response(html, content_type="text/html")

    html = _file_get(wo_cache_file)
    if html:
        _mem_set(mem_key, html)
        return Response(html, content_type="text/html")

    try:
        wos = mxd.fetch_all_open_work_orders(api_key)
        lines_dict, non_line = mxd.compute_line_scores(wos)
        areas_dict   = mxd.compute_area_scores(non_line)
        generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        html         = mxd.build_html(lines_dict, areas_dict, wos, generated_at)
        # Inject site badge into the header
        site_badge = _site_header_html(site, SITES[site]["label"])
        html = html.replace(
            "<h1>Open Work Order Dashboard</h1>",
            f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
            f'{site_badge}<h1>Open Work Order Dashboard</h1></div>',
            1,
        )
        _mem_set(mem_key, html)
        _file_set(wo_cache_file, html)
        return Response(html, content_type="text/html")
    except Exception as e:
        return _error_page(f"Failed to fetch work orders from MaintainX: {e}")


@app.route("/site/<site>/po")
@login_required
def po_dashboard(site):
    if site not in SITES:
        return redirect(url_for("index"))
    session["site"] = site
    _configure_po_module_for_site(site)

    api_key = _get_api_key(site)
    if not api_key:
        return _error_page(
            f"API key for {SITES[site]['label']} is not configured. "
            "Add it in Vercel Project Settings then redeploy.",
            status=500,
        )

    mem_key = f"po_{site}"
    html    = _mem_get(mem_key)
    if html:
        return Response(html, content_type="text/html")

    try:
        pos          = po.fetch_pos_from_csv(api_key)
        generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        site_label   = SITES[site]["label"]
        html         = po.build_po_html(pos, generated_at, site_label=site_label,
                                        site=site)
        html         = html.encode("utf-8", errors="replace").decode("utf-8")
        _mem_set(mem_key, html)
        return Response(html, content_type="text/html; charset=utf-8")
    except po.RateLimitError as e:
        return _rate_limit_page(e.reset_seconds)
    except Exception as e:
        if "429" in str(e):
            return _rate_limit_page(65)
        return _error_page(f"Failed to fetch purchase orders from MaintainX: {e}")


@app.route("/site/<site>/po/refresh")
@login_required
def po_refresh(site):
    if site not in SITES:
        return redirect(url_for("index"))
    session["site"] = site
    _configure_po_module_for_site(site)

    api_key = _get_api_key(site)
    if not api_key:
        return _error_page(f"API key for {SITES[site]['label']} is not configured.", status=500)

    try:
        if po.CACHE_FILE.exists():
            po.CACHE_FILE.unlink()
    except Exception:
        pass
    _mem_cache.pop(f"po_{site}", None)

    try:
        pos          = po.fetch_pos_from_csv(api_key)
        generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        site_label   = SITES[site]["label"]
        html         = po.build_po_html(pos, generated_at, site_label=site_label,
                                        site=site)
        html         = html.encode("utf-8", errors="replace").decode("utf-8")
        _mem_set(f"po_{site}", html)
        banner = (
            '<div style="background:#dcfce7;border:1px solid #16a34a;border-radius:8px;'
            'padding:10px 18px;margin:12px 28px;font-size:.85rem;color:#166534;">'
            '&#10003; Refresh complete &mdash; data is up to date.'
            '</div>'
        )
        return Response(html.replace("<body>", "<body>" + banner, 1),
                        content_type="text/html; charset=utf-8")
    except po.RateLimitError as e:
        return _rate_limit_refresh_page(e.reset_seconds)
    except Exception as e:
        if "429" in str(e):
            return _rate_limit_refresh_page(65)
        return _error_page(f"Refresh failed: {e}")


def _rate_limit_refresh_page(wait_seconds=65):
    """Shown when /po/refresh hits the rate limit."""
    countdown = max(int(wait_seconds) + 5, 15)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Refresh rate limited</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f1f5f9;display:flex;align-items:center;
       justify-content:center;height:100vh;margin:0;}}
  .card{{background:#fff;border-radius:10px;padding:36px 44px;
        box-shadow:0 2px 12px rgba(0,0,0,.08);max-width:480px;text-align:center;}}
  h1{{color:#d97706;font-size:1.2rem;margin-bottom:12px;}}
  p{{color:#6b7280;font-size:.9rem;line-height:1.6;}}
  .counter{{font-size:2.5rem;font-weight:700;color:#0f2d52;margin:16px 0;}}
  .btn{{display:inline-block;margin-top:16px;padding:8px 20px;
       background:#2563eb;color:#fff;border-radius:6px;font-size:.85rem;
       text-decoration:none;}}
  a{{color:#2563eb;text-decoration:none;}}
</style>
</head>
<body>
  <div class="card">
    <h1>Rate Limit Reached — Refresh Queued</h1>
    <p>The API rate limit was hit during the full refresh. The page will
       auto-retry in <strong id="s">{countdown}</strong> seconds.</p>
    <div class="counter" id="t">{countdown}</div>
    <a href="/po/refresh" class="btn">Retry refresh now</a>
    &nbsp;&nbsp;
    <a href="/po">&#8592; Back to dashboard</a>
  </div>
  <script>
    var n={countdown};
    var iv=setInterval(function(){{
      n--;document.getElementById('t').textContent=n;
      document.getElementById('s').textContent=n;
      if(n<=0){{clearInterval(iv);window.location.href='/po/refresh';}}
    }},1000);
  </script>
</body>
</html>"""
    return Response(html, status=200, content_type="text/html")


@app.route("/site/<site>/vendor/update-infor-number", methods=["POST"])
@login_required
def vendor_update_infor_number(site):
    """
    Update the 'Infor Vendor #' custom field on a vendor record.
    Body: { "vendor_id": 1227285, "infor_vendor_number": "V000076" | null }
    Returns: { "ok": true } or { "ok": false, "error": "..." }
    """
    _configure_po_module_for_site(site)
    api_key = _get_api_key(site)
    if not api_key:
        return jsonify({"ok": False, "error": "No API key configured"}), 500

    body               = request.get_json(silent=True) or {}
    vendor_id          = body.get("vendor_id")
    infor_vendor_number = body.get("infor_vendor_number")   # str or None

    if not vendor_id:
        return jsonify({"ok": False, "error": "Missing vendor_id"}), 400

    try:
        resp = _http.patch(
            f"https://api.getmaintainx.com/v1/vendors/{vendor_id}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={"extraFields": {"Infor Vendor #": infor_vendor_number}},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    _mem_cache.pop(f"po_{site}", None)

    # Update vendor cache in-place (find by vendor ID)
    try:
        vcached = json.loads(po.VENDOR_CACHE_FILE.read_text(encoding="utf-8"))
        for vinfo in vcached.get("vendors", {}).values():
            if vinfo.get("id") == vendor_id:
                vinfo["infor_vendor_number"] = infor_vendor_number
                break
        po.VENDOR_CACHE_FILE.write_text(json.dumps(vcached, default=str), encoding="utf-8")
    except Exception:
        try:
            po.VENDOR_CACHE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    # Update PO cache in-place for all POs belonging to this vendor
    try:
        pcached  = json.loads(po.CACHE_FILE.read_text(encoding="utf-8"))
        pos_dict = pcached.get("pos", {})
        for po_dict in pos_dict.values():
            if po_dict.get("vendor_id") == vendor_id:
                po_dict["infor_vendor_number"] = infor_vendor_number
        pcached["pos"] = pos_dict
        po.CACHE_FILE.write_text(json.dumps(pcached, default=str), encoding="utf-8")
    except Exception:
        try:
            po.CACHE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    return jsonify({"ok": True})


@app.route("/site/<site>/po/update-status", methods=["POST"])
@login_required
def po_update_status(site):
    """
    Update the Invoice Status custom field on a single PO.
    Body: { "po_id": 123456, "invoice_status": "Paid" | "Unpaid" | null }
    Returns: { "ok": true } or { "ok": false, "error": "..." }
    """
    try:
        _configure_po_module_for_site(site)
        api_key = _get_api_key(site)
        if not api_key:
            print(f"[update-status] No API key for site={site}")
            return jsonify({"ok": False, "error": f"No API key configured for site '{site}'"}), 200

        body           = request.get_json(silent=True) or {}
        po_id          = body.get("po_id")
        invoice_status = body.get("invoice_status")   # "Paid", "Partially Paid", "Unpaid", or None

        print(f"[update-status] site={site} po_id={po_id} status={invoice_status}")

        if not po_id:
            return jsonify({"ok": False, "error": "Missing po_id"}), 200

        try:
            resp = _http.patch(
                f"https://api.getmaintainx.com/v1/purchaseorders/{po_id}",
                headers={
                    "Authorization":  f"Bearer {api_key}",
                    "Content-Type":   "application/json",
                },
                json={"extraFields": {"Invoice Status": invoice_status}},
                timeout=15,
            )
            print(f"[update-status] MaintainX PATCH status={resp.status_code} body={resp.text[:300]}")
            resp.raise_for_status()
        except Exception as e:
            print(f"[update-status] PATCH failed: {e}\n{traceback.format_exc()}")
            return jsonify({"ok": False, "error": str(e)}), 200

    except Exception as e:
        print(f"[update-status] Unhandled error: {e}\n{traceback.format_exc()}")
        return jsonify({"ok": False, "error": f"Server error: {e}"}), 200

    _mem_cache.pop(f"po_{site}", None)

    # Update the cache file in-place so the next /po request doesn't need
    # to re-fetch the CSV just because one status changed
    try:
        cached = json.loads(po.CACHE_FILE.read_text(encoding="utf-8"))
        po_key = str(po_id)
        if po_key in cached.get("pos", {}):
            cached["pos"][po_key]["invoice_status"] = invoice_status
            # Record paid_at timestamp when AP marks as Paid
            if invoice_status == "Paid":
                cached["pos"][po_key]["paid_at"] = datetime.now(timezone.utc).isoformat()
            elif "paid_at" in cached["pos"][po_key]:
                # Remove paid_at if status is rolled back from Paid
                del cached["pos"][po_key]["paid_at"]
            po.CACHE_FILE.write_text(json.dumps(cached, default=str), encoding="utf-8")
    except Exception:
        # Cache update failed — delete it so next load re-fetches cleanly
        try:
            po.CACHE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    return jsonify({"ok": True})


@app.route("/site/<site>/po/receipt/<int:po_id>")
@login_required
def po_receipt(site, po_id):
    """
    Generate and stream a PDF payment receipt for a single PO.
    Built on-demand from the PO cache — no file storage required.
    """
    _configure_po_module_for_site(site)

    # Try cache first
    po_dict = None
    try:
        cached  = json.loads(po.CACHE_FILE.read_text(encoding="utf-8"))
        po_dict = cached.get("pos", {}).get(str(po_id))
    except Exception:
        pass

    # Cache miss (cold container on Vercel) — re-fetch CSV to find the PO
    if not po_dict:
        api_key = _get_api_key(site)
        if api_key:
            try:
                pos_list = po.fetch_pos_from_csv(api_key)
                po_dict  = next((p for p in pos_list if p.get("id") == po_id), None)
            except Exception:
                pass

    if not po_dict:
        return _error_page(
            f"PO {po_id} was not found. Please refresh the dashboard and try again.",
            status=404,
        )

    try:
        pdf_bytes = po.build_receipt_pdf(po_dict)
    except RuntimeError as e:
        # fpdf2 not installed
        return _error_page(str(e), status=500)
    except Exception as e:
        return _error_page(f"Failed to generate receipt PDF: {e}", status=500)

    # Build a clean filename from the PO number
    pnum     = po.po_number(po_dict).replace("/", "-").replace("\\", "-")
    filename = f"receipt_{pnum}.pdf"

    return Response(
        pdf_bytes,
        status=200,
        content_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ── Entry point ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _get_api_key():
        print("\nWARNING: MAINTAINX_API_KEY not found.")
        print("  Set it as an environment variable, or place your key in MaintainX_API_key.txt\n")

    print("\n  MaintainX Dashboards")
    print("  Open your browser to: http://127.0.0.1:5000")
    print("  Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
