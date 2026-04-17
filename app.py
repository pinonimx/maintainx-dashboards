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
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests as _http
from flask import Flask, Response, render_template, request, jsonify

# ── Vercel detection ──────────────────────────────────────────────────────────────
IS_VERCEL = os.environ.get("VERCEL") == "1"

import maintainx_po_report as po
if IS_VERCEL:
    po.CACHE_FILE        = Path("/tmp/po_cache.json")
    po.VENDOR_CACHE_FILE = Path("/tmp/vendor_cache.json")

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

    # Single CSV call returns all PO data including vendor names, totals,
    # and custom fields (Invoice Status).  Cache TTL = 60 min.
    try:
        pos = po.fetch_pos_from_csv(api_key)
        generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        html = po.build_po_html(pos, generated_at)
        _mem_set("po", html)
        return Response(html, content_type="text/html")
    except po.RateLimitError as e:
        return _rate_limit_page(e.reset_seconds)
    except Exception as e:
        if "429" in str(e):
            return _rate_limit_page(65)
        return _error_page(f"Failed to fetch purchase orders from MaintainX: {e}")


@app.route("/po/refresh")
def po_refresh():
    """
    Force a fresh CSV pull from MaintainX, bypassing the 60-min cache.
    Useful after new POs are approved or Invoice Status values change in MaintainX.
    """
    api_key = _get_api_key()
    if not api_key:
        return _error_page("MAINTAINX_API_KEY is not configured.", status=500)

    # Delete cache file so fetch_pos_from_csv() skips the freshness check
    try:
        if po.CACHE_FILE.exists():
            po.CACHE_FILE.unlink()
    except Exception:
        pass
    _mem_cache.pop("po", None)

    try:
        pos = po.fetch_pos_from_csv(api_key)
        generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        html = po.build_po_html(pos, generated_at)
        _mem_set("po", html)
        banner = (
            '<div style="background:#dcfce7;border:1px solid #16a34a;border-radius:8px;'
            'padding:10px 18px;margin:12px 28px;font-size:.85rem;color:#166534;">'
            '&#10003; Refresh complete &mdash; data is up to date.'
            '</div>'
        )
        return Response(html.replace("<body>", "<body>" + banner, 1), content_type="text/html")
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


@app.route("/vendor/update-infor-number", methods=["POST"])
def vendor_update_infor_number():
    """
    Update the 'Infor Vendor #' custom field on a vendor record.
    Body: { "vendor_id": 1227285, "infor_vendor_number": "V000076" | null }
    Returns: { "ok": true } or { "ok": false, "error": "..." }
    """
    api_key = _get_api_key()
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

    # Bust in-memory PO cache so next /po load regenerates HTML with updated value
    _mem_cache.pop("po", None)

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


@app.route("/po/update-status", methods=["POST"])
def po_update_status():
    """
    Update the Invoice Status custom field on a single PO.
    Body: { "po_id": 123456, "invoice_status": "Paid" | "Unpaid" | null }
    Returns: { "ok": true } or { "ok": false, "error": "..." }
    """
    api_key = _get_api_key()
    if not api_key:
        return jsonify({"ok": False, "error": "No API key configured"}), 500

    body          = request.get_json(silent=True) or {}
    po_id         = body.get("po_id")
    invoice_status = body.get("invoice_status")   # "Paid", "Unpaid", or None

    if not po_id:
        return jsonify({"ok": False, "error": "Missing po_id"}), 400

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
        resp.raise_for_status()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Bust in-memory cache so next /po load regenerates HTML
    _mem_cache.pop("po", None)

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


@app.route("/po/receipt/<int:po_id>")
def po_receipt(po_id):
    """
    Generate and stream a PDF payment receipt for a single PO.
    Built on-demand from the PO cache — no file storage required.
    """
    # Read PO from cache
    po_dict = None
    try:
        cached  = json.loads(po.CACHE_FILE.read_text(encoding="utf-8"))
        po_dict = cached.get("pos", {}).get(str(po_id))
    except Exception:
        pass

    if not po_dict:
        return _error_page(
            f"PO {po_id} was not found in the cache. "
            "Please refresh the dashboard and try again.",
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
