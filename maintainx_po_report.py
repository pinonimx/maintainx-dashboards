#!/usr/bin/env python3
"""
maintainx_po_report.py
Fetches COMPLETED (and PARTIALLY_FULFILLED) Purchase Orders from MaintainX
and generates an Accounts Payable dashboard showing which POs are ready to pay.

Outputs:
  po_dashboard.html       — interactive browser dashboard
  po_dashboard_email.html — email-pasteable version (no JavaScript)
"""

import os
import sys
import json
import time
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
BASE_URL     = "https://api.getmaintainx.com/v1"
PAGE_SIZE    = 100
KEY_FILE     = SCRIPT_DIR / "MaintainX_API_key.txt"
OUTPUT_FILE  = SCRIPT_DIR / "po_dashboard.html"
EMAIL_OUTPUT = SCRIPT_DIR / "po_dashboard_email.html"
CACHE_FILE   = SCRIPT_DIR / "po_cache.json"

# Statuses to include — COMPLETED = all items received = ready to pay
#                       PARTIALLY_FULFILLED = some items received = partially ready
READY_STATUSES   = {"COMPLETED"}
PARTIAL_STATUSES = {"PARTIALLY_FULFILLED"}
ALL_FETCH        = READY_STATUSES | PARTIAL_STATUSES

STATUS_LABEL = {
    "COMPLETED":           "Ready to Pay",
    "PARTIALLY_FULFILLED": "Partial Receipt",
}
STATUS_COLOR = {
    "COMPLETED":           "#16a34a",
    "PARTIALLY_FULFILLED": "#d97706",
}
STATUS_BG = {
    "COMPLETED":           "#dcfce7",
    "PARTIALLY_FULFILLED": "#fef9c3",
}

# ── API key ──────────────────────────────────────────────────────────────────────
def get_api_key():
    key = os.environ.get("MAINTAINX_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    if not key:
        print("ERROR: No API key found. Set MAINTAINX_API_KEY env var or place key in MaintainX_API_key.txt")
        sys.exit(1)
    return key


# ── Vendor map ───────────────────────────────────────────────────────────────────
def _fetch_vendor_map(session):
    """Return {vendorId (int): name (str)} for all vendors in the org."""
    vendor_map = {}
    cursor = None
    while True:
        params = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = session.get(f"{BASE_URL}/vendors", params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
        except Exception:
            break
        vendors = next((v for v in body.values() if isinstance(v, list)), [])
        for v in vendors:
            vid = v.get("id")
            if vid is not None:
                vendor_map[int(vid)] = v.get("name") or f"Vendor {vid}"
        cursor = body.get("nextCursor")
        if not cursor or len(vendors) < PAGE_SIZE:
            break
        time.sleep(0.5)   # rate-limit buffer between vendor pages
    return vendor_map


def _sort_pos(pos):
    """Sort POs: COMPLETED first, then most recently updated."""
    def _key(p):
        status_order = 0 if (p.get("status") or "").upper() == "COMPLETED" else 1
        raw = p.get("updatedAt") or p.get("approvalDate") or "2000-01-01T00:00:00Z"
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = 0
        return (status_order, -ts)
    pos.sort(key=_key)
    return pos


# ── Cache helpers ────────────────────────────────────────────────────────────────
def load_cache():
    """Return {str(po_id): po_dict} from the cache file, or {} if missing/corrupt."""
    if not CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data.get("pos", {})
    except Exception:
        return {}


def cache_age_minutes():
    """Return how many minutes old the cache file is, or None if missing/unreadable."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        saved_at = data.get("saved_at")
        if not saved_at:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(saved_at)
        return age.total_seconds() / 60
    except Exception:
        return None


def save_cache(pos_by_id):
    """Persist {str(po_id): po_dict} to disk."""
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "pos": pos_by_id,
    }
    CACHE_FILE.write_text(json.dumps(payload, default=str), encoding="utf-8")


# ── Fetch POs ────────────────────────────────────────────────────────────────────
def fetch_completed_pos(api_key, force_refresh=False):
    """
    Fetch all COMPLETED and PARTIALLY_FULFILLED POs with full details.

    Strategy (cache-aware):
      - If cache is less than 60 minutes old, serve it with zero API calls.
      - Otherwise scan the list endpoint, compare to cache, only fetch
        records that are new or have changed status.
      - Falls back to stale cache if rate-limited during refresh.
    """
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })

    # Load cache FIRST so we can (a) serve it if fresh, (b) fall back on 429
    cache: dict[str, dict] = {} if force_refresh else load_cache()

    # Serve from cache if it is less than 60 minutes old — zero API calls
    if not force_refresh and cache:
        age = cache_age_minutes()
        if age is not None and age < 60:
            print(f"  Cache is {age:.0f}min old — serving without API calls.")
            return _sort_pos(list(cache.values()))

    # ── Vendor map ────────────────────────────────────────────────────────────────
    print("  Fetching vendor list...")
    try:
        vendor_map = _fetch_vendor_map(session)
        print(f"  Found {len(vendor_map)} vendor(s).")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429 and cache:
            print("  Rate limited on vendor fetch — serving cached data.")
            return _sort_pos(list(cache.values()))
        raise

    time.sleep(1.0)   # buffer before PO list scan to let rate-limit window recover

    # ── Step 1: scan list endpoint → {po_id (int): status (str)} ─────────────────
    print("  Scanning PO list...")
    current: dict[int, str] = {}
    cursor = None
    while True:
        params = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        resp = session.get(f"{BASE_URL}/purchaseorders", params=params, timeout=30)
        if resp.status_code == 429:
            if cache:
                print("  Rate limited on PO scan — serving cached data.")
                return _sort_pos(list(cache.values()))
            resp.raise_for_status()
        resp.raise_for_status()
        body = resp.json()
        batch = next((v for k, v in body.items() if isinstance(v, list)), None)
        if not batch:
            break
        for po_item in batch:
            status = (po_item.get("status") or "").upper().strip()
            if status in ALL_FETCH:
                current[int(po_item["id"])] = status
        cursor = body.get("nextCursor")
        if not cursor:
            break
        time.sleep(0.5)   # rate-limit buffer between PO list pages

    print(f"  List scan complete — {len(current)} completed/partial PO(s) on server.")

    # ── Step 2: decide what to fetch ──────────────────────────────────────────────
    ids_to_fetch = []
    for po_id, status in current.items():
        key = str(po_id)
        cached = cache.get(key)
        if cached is None or (cached.get("status") or "").upper() != status:
            ids_to_fetch.append(po_id)

    if force_refresh:
        ids_to_fetch = list(current.keys())
        print("  --refresh: ignoring cache, re-fetching all POs.")
    elif ids_to_fetch:
        print(f"  {len(current) - len(ids_to_fetch)} PO(s) from cache, "
              f"{len(ids_to_fetch)} to fetch...")
    else:
        print(f"  All {len(current)} PO(s) loaded from cache.")

    # ── Step 3: fetch full record for each PO that needs it ───────────────────────
    DELAY      = 0.5    # seconds between individual PO fetches
    RETRY_WAIT = 12
    MAX_RETRY  = 3

    for i, po_id in enumerate(ids_to_fetch, 1):
        if i % 25 == 0:
            print(f"  ...fetched {i}/{len(ids_to_fetch)}")
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = session.get(f"{BASE_URL}/purchaseorders/{po_id}", timeout=30)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", RETRY_WAIT))
                    print(f"  Rate limited — waiting {wait}s before retrying...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                body = resp.json()
                po = body.get("purchaseOrder") or body
                if isinstance(po, dict):
                    vid = po.get("vendorId")
                    po["vendorName"] = vendor_map.get(int(vid), f"Vendor {vid}") if vid else "Unknown Vendor"
                    cache[str(po_id)] = po
                break
            except requests.exceptions.HTTPError as e:
                if attempt < MAX_RETRY:
                    time.sleep(RETRY_WAIT)
                else:
                    print(f"  Warning: could not fetch PO {po_id} — {e}")
            except Exception as e:
                print(f"  Warning: could not fetch PO {po_id} — {e}")
                break
        time.sleep(DELAY)

    # Drop stale entries and save updated cache
    stale = [k for k in list(cache.keys()) if int(k) not in current]
    for k in stale:
        del cache[k]
    save_cache(cache)

    return _sort_pos(list(cache.values()))


# ── Helpers ───────────────────────────────────────────────────────────────────────
def calc_po_total(po):
    """
    Calculate PO total from the items array.
    MaintainX API stores costs as integer cents in `price` / `unitCost`
    and as decimal dollar strings in `priceDecimal` / `unitCostDecimal`.
    `price` = line total (unitCost x quantityOrdered), so we use
    `priceDecimal` directly for each item line.
    Returns a float (dollars) or None if no cost data is present.
    """
    total = 0.0
    has_data = False

    for item in (po.get("items") or []):
        line_str = item.get("priceDecimal")
        if line_str is not None:
            try:
                total += float(line_str)
                has_data = True
                continue
            except (ValueError, TypeError):
                pass
        price_int = item.get("price")
        if price_int is not None:
            try:
                total += float(price_int) / 100.0
                has_data = True
            except (ValueError, TypeError):
                pass

    return round(total, 2) if has_data else None


def fmt_currency(val):
    if val is None:
        return "—"
    return f"${val:,.2f}"


def fmt_date(iso_str):
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except Exception:
        return str(iso_str)[:10]


def po_number(po):
    override = (po.get("overrideNumber") or "").strip()
    auto = po.get("autoGeneratedNumber")
    if override:
        return override
    if auto is not None:
        return f"#{auto}"
    return f"ID {po.get('id', '?')}"


def _esc(s):
    """HTML-escape a string."""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _title_redundant(title, vendor):
    """Return True if the PO title is just a case/spacing variant of the vendor name."""
    def norm(s):
        return ''.join(c for c in s.lower() if c.isalnum())
    t, v = norm(title), norm(vendor)
    if not t:
        return True
    return t == v or t in v or v in t


# ── Interactive HTML dashboard ───────────────────────────────────────────────────
def build_po_html(pos, generated_at):
    n_ready   = sum(1 for p in pos if (p.get("status") or "").upper() == "COMPLETED")
    n_partial = sum(1 for p in pos if (p.get("status") or "").upper() == "PARTIALLY_FULFILLED")
    totals    = [calc_po_total(p) for p in pos]
    known     = [t for t in totals if t is not None]
    total_val = fmt_currency(sum(known)) if known else "—"

    vendors = sorted(set(p.get("vendorName", "Unknown Vendor") for p in pos))

    rows_html = ""
    for i, (po, total) in enumerate(zip(pos, totals)):
        status     = (po.get("status") or "").upper()
        label      = STATUS_LABEL.get(status, status)
        color      = STATUS_COLOR.get(status, "#6b7280")
        bg         = STATUS_BG.get(status, "#f3f4f6")
        vendor_raw = po.get("vendorName", "Unknown Vendor")
        title_raw  = po.get("title") or ""
        vendor     = _esc(vendor_raw)
        pnum       = _esc(po_number(po))
        title      = _esc(title_raw)
        note       = _esc((po.get("note") or "").replace("\n", " ")[:120])
        approved   = fmt_date(po.get("approvalDate") or po.get("updatedAt"))
        due        = fmt_date(po.get("dueDate"))
        amt        = fmt_currency(total)

        rows_html += f"""
        <tr class="po-row" data-status="{status}" data-vendor="{vendor}">
          <td style="font-weight:600;color:#1e40af;white-space:nowrap">{pnum}</td>
          <td>{vendor}{f'<div style="font-size:.78rem;color:#6b7280;margin-top:2px">{title}</div>' if not _title_redundant(title_raw, vendor_raw) else ''}</td>
          <td><span style="display:inline-block;padding:3px 10px;border-radius:12px;font-size:.78rem;font-weight:600;background:{bg};color:{color}">{label}</span></td>
          <td style="text-align:right;font-weight:600;font-variant-numeric:tabular-nums">{amt}</td>
          <td style="white-space:nowrap;color:#374151">{approved}</td>
          <td style="white-space:nowrap;color:#374151">{due}</td>
          <td style="font-size:.82rem;color:#6b7280;max-width:260px">{note}</td>
        </tr>"""

    vendor_options = "\n".join(f'<option value="{_esc(v)}">{_esc(v)}</option>' for v in vendors)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AP -- POs Ready to Pay</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f1f5f9;color:#1e293b;min-height:100vh}}
  .header{{background:#fff;border-bottom:1px solid #e2e8f0;padding:18px 28px;display:flex;align-items:center;justify-content:space-between}}
  .header h1{{font-size:1.25rem;font-weight:700;color:#0f2d52}}
  .header .sub{{font-size:.8rem;color:#94a3b8;margin-top:2px}}
  .kpi-strip{{display:flex;gap:14px;padding:18px 28px;flex-wrap:wrap}}
  .kpi{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 20px;min-width:160px;flex:1}}
  .kpi .val{{font-size:1.8rem;font-weight:700;color:#0f2d52;line-height:1}}
  .kpi .lbl{{font-size:.75rem;color:#94a3b8;margin-top:4px;text-transform:uppercase;letter-spacing:.05em}}
  .kpi.green .val{{color:#16a34a}}
  .kpi.amber .val{{color:#d97706}}
  .content{{padding:0 28px 28px}}
  .card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}}
  .card-header{{padding:14px 18px;background:#f8fafc;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}}
  .card-header h2{{font-size:.95rem;font-weight:600;color:#374151}}
  .filters{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
  .filters input,.filters select{{border:1px solid #d1d5db;border-radius:6px;padding:5px 10px;font-size:.82rem;color:#374151;outline:none}}
  .filters input:focus,.filters select:focus{{border-color:#2563eb;box-shadow:0 0 0 2px rgba(37,99,235,.15)}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem}}
  th{{background:#f8fafc;padding:10px 14px;text-align:left;font-size:.75rem;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e2e8f0;white-space:nowrap}}
  td{{padding:11px 14px;border-bottom:1px solid #f1f5f9;vertical-align:top}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#fafbff}}
  .empty{{text-align:center;padding:40px;color:#9ca3af;font-size:.9rem}}
  @media(max-width:700px){{.kpi-strip{{flex-direction:column}}.filters{{flex-direction:column}}}}
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:14px;">
    <a href="/" style="font-size:.8rem;color:#64748b;text-decoration:none;padding:5px 12px;border:1px solid #e2e8f0;border-radius:6px;white-space:nowrap;">&#8592; Home</a>
    <div>
      <h1>Accounts Payable &mdash; Purchase Orders</h1>
      <div class="sub">Generated {generated_at}</div>
    </div>
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi green">
    <div class="val">{n_ready}</div>
    <div class="lbl">Ready to Pay</div>
  </div>
  <div class="kpi amber">
    <div class="val">{n_partial}</div>
    <div class="lbl">Partial Receipt</div>
  </div>
  <div class="kpi">
    <div class="val">{len(pos)}</div>
    <div class="lbl">Total POs</div>
  </div>
  <div class="kpi">
    <div class="val" style="font-size:1.4rem">{total_val}</div>
    <div class="lbl">Est. Total Value</div>
  </div>
</div>

<div class="content">
  <div class="card">
    <div class="card-header">
      <h2>Purchase Orders</h2>
      <div class="filters">
        <input type="text" id="search" placeholder="Search PO # or vendor..." oninput="applyFilters()">
        <select id="statusFilter" onchange="applyFilters()">
          <option value="">All Statuses</option>
          <option value="COMPLETED">Ready to Pay</option>
          <option value="PARTIALLY_FULFILLED">Partial Receipt</option>
        </select>
        <select id="vendorFilter" onchange="applyFilters()">
          <option value="">All Vendors</option>
          {vendor_options}
        </select>
        <span id="rowCount" style="font-size:.8rem;color:#9ca3af"></span>
      </div>
    </div>
    <div style="overflow-x:auto">
      <table id="poTable">
        <thead>
          <tr>
            <th>PO #</th>
            <th>Vendor</th>
            <th>Status</th>
            <th style="text-align:right">Total</th>
            <th>Approved</th>
            <th>Due Date</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody id="poBody">
          {rows_html if rows_html else '<tr><td colspan="7" class="empty">No completed purchase orders found.</td></tr>'}
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
function applyFilters() {{
  var search  = document.getElementById('search').value.toLowerCase();
  var status  = document.getElementById('statusFilter').value;
  var vendor  = document.getElementById('vendorFilter').value;
  var rows    = document.querySelectorAll('#poBody .po-row');
  var visible = 0;
  rows.forEach(function(row) {{
    var matchSearch = !search || row.textContent.toLowerCase().includes(search);
    var matchStatus = !status || row.dataset.status === status;
    var matchVendor = !vendor || row.dataset.vendor === vendor;
    var show = matchSearch && matchStatus && matchVendor;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  var total = rows.length;
  document.getElementById('rowCount').textContent =
    visible === total ? total + ' POs' : visible + ' of ' + total + ' POs';
}}
applyFilters();
</script>
</body>
</html>"""


# ── Email-safe HTML ───────────────────────────────────────────────────────────────
def build_po_email_html(pos, generated_at):
    n_ready   = sum(1 for p in pos if (p.get("status") or "").upper() == "COMPLETED")
    n_partial = sum(1 for p in pos if (p.get("status") or "").upper() == "PARTIALLY_FULFILLED")
    totals    = [calc_po_total(p) for p in pos]
    known     = [t for t in totals if t is not None]
    total_val = fmt_currency(sum(known)) if known else "—"

    rows_html = ""
    for i, (po, total) in enumerate(zip(pos, totals)):
        status   = (po.get("status") or "").upper()
        label    = STATUS_LABEL.get(status, status)
        color    = STATUS_COLOR.get(status, "#6b7280")
        bg       = STATUS_BG.get(status, "#f3f4f6")
        vendor   = _esc(po.get("vendorName", "Unknown Vendor"))
        pnum     = _esc(po_number(po))
        approved = fmt_date(po.get("approvalDate") or po.get("updatedAt"))
        due      = fmt_date(po.get("dueDate"))
        amt      = fmt_currency(total)
        row_bg   = "#ffffff" if i % 2 == 0 else "#f8fafc"

        rows_html += f"""
        <tr style="background:{row_bg}">
          <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#1e40af;white-space:nowrap;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px">{pnum}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px">{vendor}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;white-space:nowrap;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px">
            <span style="display:inline-block;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:600;background:{bg};color:{color}">{label}</span>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:600;white-space:nowrap;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px">{amt}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;white-space:nowrap;color:#374151;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px">{approved}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #e2e8f0;white-space:nowrap;color:#374151;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px">{due}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AP -- POs Ready to Pay</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:860px;margin:24px auto">
  <tr>
    <td>
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:10px 10px 0 0;border-top:6px solid #1d4ed8;margin-bottom:2px">
        <tr>
          <td style="padding:20px 24px">
            <div style="font-size:18px;font-weight:700;color:#0f2d52">Accounts Payable &mdash; Purchase Orders Ready to Pay</div>
            <div style="font-size:12px;color:#94a3b8;margin-top:4px">Generated {generated_at}</div>
          </td>
        </tr>
      </table>
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;margin-bottom:2px">
        <tr>
          <td width="25%" style="padding:14px 20px;border-right:1px solid #f1f5f9;border-bottom:1px solid #f1f5f9">
            <div style="font-size:26px;font-weight:700;color:#16a34a">{n_ready}</div>
            <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em;margin-top:2px">Ready to Pay</div>
          </td>
          <td width="25%" style="padding:14px 20px;border-right:1px solid #f1f5f9;border-bottom:1px solid #f1f5f9">
            <div style="font-size:26px;font-weight:700;color:#d97706">{n_partial}</div>
            <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em;margin-top:2px">Partial Receipt</div>
          </td>
          <td width="25%" style="padding:14px 20px;border-right:1px solid #f1f5f9;border-bottom:1px solid #f1f5f9">
            <div style="font-size:26px;font-weight:700;color:#0f2d52">{len(pos)}</div>
            <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em;margin-top:2px">Total POs</div>
          </td>
          <td width="25%" style="padding:14px 20px;border-bottom:1px solid #f1f5f9">
            <div style="font-size:22px;font-weight:700;color:#0f2d52">{total_val}</div>
            <div style="font-size:11px;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em;margin-top:2px">Est. Total Value</div>
          </td>
        </tr>
      </table>
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:0 0 10px 10px">
        <tr style="background:#f8fafc">
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:2px solid #e2e8f0">PO #</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:2px solid #e2e8f0">Vendor</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:2px solid #e2e8f0">Status</th>
          <th style="padding:10px 12px;text-align:right;font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:2px solid #e2e8f0">Total</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:2px solid #e2e8f0">Approved</th>
          <th style="padding:10px 12px;text-align:left;font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:2px solid #e2e8f0">Due Date</th>
        </tr>
        {rows_html if rows_html else '<tr><td colspan="6" style="padding:30px;text-align:center;color:#9ca3af;font-size:13px">No completed purchase orders found.</td></tr>'}
      </table>
      <div style="text-align:center;padding:14px;font-size:11px;color:#94a3b8">
        Ready to Pay = all items received &nbsp;|&nbsp; Partial Receipt = some items received
      </div>
    </td>
  </tr>
</table>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Generate MaintainX AP PO dashboard")
    parser.add_argument(
        "--refresh", action="store_true",
        help="Ignore local cache and re-fetch all PO details from the API"
    )
    args = parser.parse_args()

    api_key = get_api_key()

    print("Fetching completed purchase orders from MaintainX...")
    if args.refresh:
        print("  (full refresh requested -- cache will be rebuilt)")
    try:
        pos = fetch_completed_pos(api_key, force_refresh=args.refresh)
    except requests.exceptions.HTTPError as e:
        print(f"ERROR: API request failed -- {e}")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"ERROR: Could not connect to MaintainX API -- {e}")
        sys.exit(1)

    print(f"  Retrieved {len(pos)} completed/partial PO(s).")

    n_ready   = sum(1 for p in pos if (p.get("status") or "").upper() == "COMPLETED")
    n_partial = sum(1 for p in pos if (p.get("status") or "").upper() == "PARTIALLY_FULFILLED")
    print(f"  Ready to Pay (COMPLETED):       {n_ready}")
    print(f"  Partial Receipt (PART. FULFIL): {n_partial}")

    generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")

    html = build_po_html(pos, generated_at)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"\nDashboard saved      -> {OUTPUT_FILE}")

    email_html = build_po_email_html(pos, generated_at)
    EMAIL_OUTPUT.write_text(email_html, encoding="utf-8")
    print(f"Email version saved  -> {EMAIL_OUTPUT}")


if __name__ == "__main__":
    main()
