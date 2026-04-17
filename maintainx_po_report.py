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
import csv
import io
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
CACHE_FILE        = SCRIPT_DIR / "po_cache.json"
VENDOR_CACHE_FILE = SCRIPT_DIR / "vendor_cache.json"
VENDOR_CACHE_TTL  = 120   # minutes — vendors change infrequently

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

# ── Rate-limit helpers ────────────────────────────────────────────────────────────

class RateLimitError(Exception):
    """
    Raised when the MaintainX API returns 429 and we cannot (or should not)
    block inside the current serverless function execution.
    """
    def __init__(self, reset_seconds: int):
        self.reset_seconds = reset_seconds
        super().__init__(f"Rate limited by MaintainX API; reset in {reset_seconds}s")


def _rl_get(session, url, params=None, timeout=30):
    """
    Rate-limit-aware GET wrapper.

    * On 429  → raises RateLimitError(reset_seconds) immediately (no sleeping).
    * On success → if X-Rate-Limit-Remaining == 0, sleep X-Rate-Limit-Reset + 1
      so the NEXT call lands in a fresh window (capped at 65 s for safety).
    * Returns the Response object on success.
    """
    resp = session.get(url, params=params, timeout=timeout)
    if resp.status_code == 429:
        reset = int(resp.headers.get("X-Rate-Limit-Reset", 62))
        raise RateLimitError(reset)
    resp.raise_for_status()

    remaining = resp.headers.get("X-Rate-Limit-Remaining")
    reset_hdr  = resp.headers.get("X-Rate-Limit-Reset")
    if remaining is not None and int(remaining) == 0 and reset_hdr is not None:
        wait = min(int(reset_hdr) + 1, 65)
        print(f"  Rate-limit budget exhausted — sleeping {wait}s for window reset...")
        time.sleep(wait)

    return resp


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
    """Return {vendorId (int): name (str)} for all vendors in the org.
    Propagates RateLimitError so the caller can decide whether to fall back
    to cached data or re-raise.
    """
    vendor_map = {}
    cursor = None
    while True:
        params = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        # _rl_get raises RateLimitError on 429 — let it propagate
        resp = _rl_get(session, f"{BASE_URL}/vendors", params=params)
        body = resp.json()
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


# ── CSV fetch (primary web path) ─────────────────────────────────────────────────

def _parse_csv_date(s):
    """Convert a CSV date string ('2025-10-20 13:00:12') to ISO-like format."""
    if not s or not s.strip():
        return None
    s = s.strip()
    return s.replace(" ", "T") if "T" not in s else s


def fetch_pos_from_csv(api_key):
    """
    Fetch ALL PO data in a single CSV export call.

    Returns a filtered list of PO dicts (COMPLETED + PARTIALLY_FULFILLED).
    One API call replaces the entire list-scan + N detail-fetch pattern.
    Vendor names, line-item totals, and custom fields (Invoice Status) are
    all present in the CSV.

    Caching: serves from po_cache.json if < 60 min old (zero API calls).
    """
    # Serve from cache if fresh
    cache = load_cache()
    age   = cache_age_minutes()
    if cache and age is not None and age < 60:
        print(f"  CSV cache is {age:.0f}min old — serving without API call.")
        return _sort_pos(list(cache.values()))

    print("  Fetching PO data from CSV export endpoint...")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})

    try:
        resp = _rl_get(session, f"{BASE_URL}/purchaseorders/purchaseOrders.csv")
    except RateLimitError:
        if cache:
            print("  Rate limited — serving cached data.")
            return _sort_pos(list(cache.values()))
        raise

    # Parse CSV — multiple rows per PO (one per line item).
    # Decode with utf-8-sig to strip the BOM character that MaintainX prepends
    # to the export; without this the first column key becomes "\ufeffPurchase Order #"
    # and the lookup returns None, causing po_number() to fall back to the numeric ID.
    text   = resp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    groups: dict[str, list] = {}
    for row in reader:
        po_id = (row.get("Purchase Order ID") or "").strip()
        if po_id:
            groups.setdefault(po_id, []).append(row)

    pos_by_id: dict[str, dict] = {}
    for po_id, rows in groups.items():
        first  = rows[0]
        status = (first.get("Status") or "").upper().strip()
        if status not in ALL_FETCH:
            continue

        # Total Ordered Cost repeats on every row — it's the PO-level total
        try:
            total = float((first.get("Total Ordered Cost") or "0").replace(",", ""))
        except (ValueError, TypeError):
            total = None

        invoice_status = (first.get("Invoice Status") or "").strip() or None

        pos_by_id[po_id] = {
            "id":            int(po_id),
            "overrideNumber": (first.get("Purchase Order #") or "").strip(),
            "title":         (first.get("Purchase Order Title") or "").strip(),
            "vendorName":    (first.get("Vendor") or "").strip() or "Unknown Vendor",
            "status":        status,
            "note":          (first.get("Notes") or "").strip(),
            "approvalDate":  _parse_csv_date(first.get("Approved On")),
            "updatedAt":     _parse_csv_date(
                                 first.get("Completed On") or first.get("Approved On")
                             ),
            "dueDate":       _parse_csv_date(first.get("Due Date")),
            "invoice_status": invoice_status,
            "_total":        total,   # pre-computed; picked up by calc_po_total()
        }

    # ── Enrich POs with vendor ID and Infor Vendor # ──────────────────────────────
    # Vendor names in the PO CSV are matched case-insensitively against the
    # vendor REST API response to look up the vendor ID (needed for PATCH calls)
    # and the Infor Vendor # custom field.
    try:
        vendor_data = fetch_vendor_data(api_key)
    except RateLimitError:
        vendor_data = load_vendor_cache()   # fall back to stale cache
        print("  Rate limited on vendor fetch — using stale vendor cache.")

    for po_dict in pos_by_id.values():
        vname = po_dict.get("vendorName", "")
        vinfo = vendor_data.get(vname.lower())
        if vinfo:
            po_dict["vendor_id"]           = vinfo["id"]
            po_dict["infor_vendor_number"] = vinfo["infor_vendor_number"]
        else:
            po_dict["vendor_id"]           = None
            po_dict["infor_vendor_number"] = None

    save_cache(pos_by_id)
    print(f"  CSV parsed — {len(pos_by_id)} completed/partial PO(s).")
    return _sort_pos(list(pos_by_id.values()))


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


# ── Vendor cache helpers ─────────────────────────────────────────────────────────

def load_vendor_cache():
    """Return {vendor_name_lower: {id, name, infor_vendor_number}} or {}."""
    if not VENDOR_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(VENDOR_CACHE_FILE.read_text(encoding="utf-8"))
        return data.get("vendors", {})
    except Exception:
        return {}


def vendor_cache_age_minutes():
    """Return age of vendor cache in minutes, or None if missing/unreadable."""
    if not VENDOR_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(VENDOR_CACHE_FILE.read_text(encoding="utf-8"))
        saved_at = data.get("saved_at")
        if not saved_at:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(saved_at)
        return age.total_seconds() / 60
    except Exception:
        return None


def save_vendor_cache(vendors_by_name):
    """Persist {name_lower: vendor_dict} to disk."""
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "vendors":  vendors_by_name,
    }
    VENDOR_CACHE_FILE.write_text(json.dumps(payload, default=str), encoding="utf-8")


def fetch_vendor_data(api_key):
    """
    Fetch all vendors with their 'Infor Vendor #' custom field via REST API.

    Returns {vendor_name_lower: {"id": int, "name": str, "infor_vendor_number": str|None}}.
    Results are cached in VENDOR_CACHE_FILE for VENDOR_CACHE_TTL minutes.
    Called once per cold-start (adds 1–2 API calls alongside the PO CSV call).
    """
    cached = load_vendor_cache()
    age    = vendor_cache_age_minutes()
    if cached and age is not None and age < VENDOR_CACHE_TTL:
        print(f"  Vendor cache is {age:.0f}min old — using without API call.")
        return cached

    print("  Fetching vendor list (for Infor Vendor # data)...")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})

    vendors_by_name = {}
    cursor = None
    while True:
        params = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = _rl_get(session, f"{BASE_URL}/vendors", params=params)
        except RateLimitError:
            if cached:
                print("  Rate limited on vendor fetch — using cached data.")
                return cached
            raise

        body        = resp.json()
        vendor_list = next((v for v in body.values() if isinstance(v, list)), [])
        for v in vendor_list:
            vid  = v.get("id")
            name = (v.get("name") or "").strip()
            if vid is not None and name:
                extra     = v.get("extraFields") or {}
                infor_num = (extra.get("Infor Vendor #") or "").strip() or None
                vendors_by_name[name.lower()] = {
                    "id":                 int(vid),
                    "name":               name,
                    "infor_vendor_number": infor_num,
                }
        cursor = body.get("nextCursor")
        if not cursor or len(vendor_list) < PAGE_SIZE:
            break
        time.sleep(0.5)

    save_vendor_cache(vendors_by_name)
    print(f"  Vendor data fetched — {len(vendors_by_name)} vendor(s).")
    return vendors_by_name


# ── Fetch POs ────────────────────────────────────────────────────────────────────
def fetch_completed_pos(api_key, force_refresh=False, fetch_vendors=True,
                        fetch_details=True):
    """
    Fetch all COMPLETED and PARTIALLY_FULFILLED POs.

    Strategy (cache-aware):
      - If cache < 60 min old, serve with zero API calls.
      - Otherwise: scan the list endpoint (1–3 calls), update cache.

    fetch_vendors:  if False, skip /vendors calls (saves rate-limit quota).
    fetch_details:  if False (web path), skip per-PO detail fetches entirely.
                    List-level data is stored for new/unknown POs so the
                    dashboard renders immediately.  Totals show "—" for POs
                    that have never been detail-fetched; cached detail data
                    (vendor names, item totals) is reused when available.
                    Use the /po/refresh route to trigger a full detail fetch.
    """
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })

    # Load cache FIRST — (a) serve fresh, (b) fall back on 429, (c) enrich list data
    cache: dict[str, dict] = {} if force_refresh else load_cache()

    # Serve from cache if it is less than 60 minutes old — zero API calls
    if not force_refresh and cache:
        age = cache_age_minutes()
        if age is not None and age < 60:
            print(f"  Cache is {age:.0f}min old — serving without API calls.")
            return _sort_pos(list(cache.values()))

    # ── Vendor map (optional — skipped on web path to save rate-limit quota) ──────
    if fetch_vendors:
        print("  Fetching vendor list...")
        try:
            vendor_map = _fetch_vendor_map(session)
            print(f"  Found {len(vendor_map)} vendor(s).")
        except RateLimitError:
            if cache:
                print("  Rate limited on vendor fetch — serving cached data.")
                return _sort_pos(list(cache.values()))
            raise
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429 and cache:
                print("  Rate limited on vendor fetch — serving cached data.")
                return _sort_pos(list(cache.values()))
            raise
        time.sleep(1.0)   # buffer before PO list scan
    else:
        vendor_map = {}
        print("  Skipping vendor fetch (using cached vendor names).")

    # ── Step 1: scan list endpoint → {po_id: status} + capture full list items ───
    print("  Scanning PO list...")
    current: dict[int, str] = {}      # id → status
    list_items: dict[int, dict] = {}  # id → full list-level record
    cursor = None
    while True:
        params = {"limit": PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = _rl_get(session, f"{BASE_URL}/purchaseorders", params=params)
        except RateLimitError:
            if cache:
                print("  Rate limited on PO scan — serving cached data.")
                return _sort_pos(list(cache.values()))
            raise   # propagates so app.py shows accurate countdown
        body = resp.json()
        batch = next((v for k, v in body.items() if isinstance(v, list)), None)
        if not batch:
            break
        for po_item in batch:
            status = (po_item.get("status") or "").upper().strip()
            if status in ALL_FETCH:
                po_id = int(po_item["id"])
                current[po_id] = status
                list_items[po_id] = po_item   # keep full summary for list-only mode
        cursor = body.get("nextCursor")
        if not cursor:
            break
        time.sleep(0.5)

    print(f"  List scan complete — {len(current)} completed/partial PO(s) on server.")

    # ── Step 2a: list-only mode (web path) ────────────────────────────────────────
    if not fetch_details:
        for po_id, list_item in list_items.items():
            key = str(po_id)
            cached = cache.get(key)
            if cached is None:
                # Brand-new PO: store list-level record.
                # calc_po_total returns None when "items" absent → shows "—"
                entry = dict(list_item)
                vid = entry.get("vendorId")
                entry["vendorName"] = (
                    vendor_map.get(int(vid), f"Vendor {vid}") if vid else "Unknown Vendor"
                )
                cache[key] = entry
            else:
                # Update mutable fields from the fresh list scan
                cached["status"]       = list_item.get("status", cached.get("status"))
                cached["updatedAt"]    = list_item.get("updatedAt", cached.get("updatedAt"))
                cached["approvalDate"] = list_item.get("approvalDate", cached.get("approvalDate"))
                cached["dueDate"]      = list_item.get("dueDate", cached.get("dueDate"))
                cached["note"]         = list_item.get("note", cached.get("note"))

        # Drop POs no longer on server
        stale = [k for k in list(cache.keys()) if int(k) not in current]
        for k in stale:
            del cache[k]
        save_cache(cache)
        print(f"  List-only mode: {len(cache)} PO(s) ready (no detail fetches).")
        return _sort_pos(list(cache.values()))

    # ── Step 2b: decide which POs need a full detail fetch ────────────────────────
    ids_to_fetch = []
    if force_refresh:
        ids_to_fetch = list(current.keys())
        print("  --refresh: ignoring cache, re-fetching all POs.")
    else:
        for po_id, status in current.items():
            key = str(po_id)
            cached = cache.get(key)
            needs_fetch = (
                cached is None                                            # never fetched
                or "items" not in cached                                  # only list data
                or (cached.get("status") or "").upper() != status         # status changed
            )
            if needs_fetch:
                ids_to_fetch.append(po_id)
        if ids_to_fetch:
            print(f"  {len(current) - len(ids_to_fetch)} PO(s) from cache, "
                  f"{len(ids_to_fetch)} to fetch...")
        else:
            print(f"  All {len(current)} PO(s) loaded from cache.")

    # ── Step 3: fetch full record for each PO that needs it ───────────────────────
    DELAY      = 0.5
    RETRY_WAIT = 12
    MAX_RETRY  = 3

    for i, po_id in enumerate(ids_to_fetch, 1):
        if i % 25 == 0:
            print(f"  ...fetched {i}/{len(ids_to_fetch)}")
        for attempt in range(1, MAX_RETRY + 1):
            try:
                resp = _rl_get(session, f"{BASE_URL}/purchaseorders/{po_id}")
                body = resp.json()
                po = body.get("purchaseOrder") or body
                if isinstance(po, dict):
                    vid = po.get("vendorId")
                    po["vendorName"] = (
                        vendor_map.get(int(vid), f"Vendor {vid}") if vid else "Unknown Vendor"
                    )
                    cache[str(po_id)] = po
                break
            except RateLimitError as e:
                wait = min(e.reset_seconds + 1, 65)
                print(f"  Rate limited on PO {po_id} (attempt {attempt}) — sleeping {wait}s...")
                time.sleep(wait)
            except requests.exceptions.HTTPError as e:
                if attempt < MAX_RETRY:
                    time.sleep(RETRY_WAIT)
                else:
                    print(f"  Warning: could not fetch PO {po_id} — {e}")
            except Exception as e:
                print(f"  Warning: could not fetch PO {po_id} — {e}")
                break
        time.sleep(DELAY)

    # Drop stale entries and save
    stale = [k for k in list(cache.keys()) if int(k) not in current]
    for k in stale:
        del cache[k]
    save_cache(cache)

    return _sort_pos(list(cache.values()))


# ── Helpers ───────────────────────────────────────────────────────────────────────
def calc_po_total(po):
    """
    Return the PO total as a float (dollars), or None if unknown.

    CSV-fetched POs carry a pre-computed ``_total`` field (Total Ordered Cost
    from the export).  Legacy detail-fetched POs fall back to calculating
    from the ``items`` array.
    """
    # CSV path — pre-computed total
    if "_total" in po:
        t = po.get("_total")
        try:
            return round(float(t), 2) if t else None
        except (ValueError, TypeError):
            return None

    # Legacy path — calculate from items array
    total    = 0.0
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
    n_needs   = sum(1 for p in pos if (p.get("invoice_status") or "").strip() != "Paid")
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
        po_id      = po.get("id", 0)

        # ── Infor Vendor # sub-row (lives inside the Vendor cell) ───────────────
        vendor_id      = po.get("vendor_id")
        infor_num_raw  = (po.get("infor_vendor_number") or "").strip()
        infor_esc      = _esc(infor_num_raw)
        vid_js         = str(vendor_id) if vendor_id else "null"
        if vendor_id:
            save_extra = ""
        else:
            save_extra = ' disabled title="Vendor ID not found — use Refresh Data"'

        infor_sub = (
            f'<div style="display:flex;align-items:center;gap:4px;margin-top:5px">'
            f'<span style="font-size:.72rem;color:#94a3b8;white-space:nowrap">Infor&nbsp;#</span>'
            f'<input type="text" id="inf-{po_id}" value="{infor_esc}" placeholder="Not set"'
            f' style="border:1px solid #d1d5db;border-radius:4px;padding:2px 6px;'
            f'font-size:.75rem;width:80px;color:#374151;background:#fff">'
            f'<button onclick="saveInforNum({po_id},{vid_js},this)"{save_extra}'
            f' style="padding:2px 7px;background:#2563eb;color:#fff;border:none;'
            f'border-radius:4px;font-size:.72rem;cursor:pointer;font-weight:500'
            f'{";opacity:.4;cursor:not-allowed" if not vendor_id else ""}">Save</button>'
            f'</div>'
        )

        # ── Invoice Status cell ──────────────────────────────────────────────────
        inv_raw = (po.get("invoice_status") or "").strip()
        if inv_raw == "Paid":
            inv_opts = ('<option value="Paid" selected>Paid</option>'
                        '<option value="Unpaid">Unpaid</option>')
        elif inv_raw == "Unpaid":
            inv_opts = ('<option value="Unpaid" selected>Unpaid</option>'
                        '<option value="Paid">Paid</option>')
        else:
            inv_raw  = ""   # normalise NULL / unknown
            inv_opts = ('<option value="" selected>&#8212; Not Set</option>'
                        '<option value="Unpaid">Unpaid</option>')

        rows_html += f"""
        <tr class="po-row" data-status="{status}" data-vendor="{vendor}" data-invoice-status="{inv_raw}">
          <td style="font-weight:600;color:#1e40af;white-space:nowrap">{pnum}</td>
          <td style="white-space:nowrap">
            <select id="inv-{po_id}" style="border:1px solid #d1d5db;border-radius:5px;padding:3px 7px;font-size:.78rem;color:#374151;background:#fff;margin-right:4px">{inv_opts}</select><button onclick="saveInvStatus({po_id},this)" style="padding:3px 8px;background:#2563eb;color:#fff;border:none;border-radius:5px;font-size:.75rem;cursor:pointer;font-weight:500">Save</button>
          </td>
          <td>{vendor}{f'<div style="font-size:.78rem;color:#6b7280;margin-top:2px">{title}</div>' if not _title_redundant(title_raw, vendor_raw) else ''}{infor_sub}</td>
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
  .header{{background:#fff;border-bottom:1px solid #e2e8f0;padding:18px 28px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}}
  .header h1{{font-size:1.25rem;font-weight:700;color:#0f2d52}}
  .header .sub{{font-size:.8rem;color:#94a3b8;margin-top:2px}}
  .kpi-strip{{display:flex;gap:14px;padding:18px 28px;flex-wrap:wrap}}
  .kpi{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 20px;min-width:140px;flex:1}}
  .kpi .val{{font-size:1.8rem;font-weight:700;color:#0f2d52;line-height:1}}
  .kpi .lbl{{font-size:.75rem;color:#94a3b8;margin-top:4px;text-transform:uppercase;letter-spacing:.05em}}
  .kpi.green .val{{color:#16a34a}}
  .kpi.amber .val{{color:#d97706}}
  .kpi.red   .val{{color:#dc2626}}
  .content{{padding:0 28px 28px}}
  .card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}}
  .card-header{{padding:14px 18px;background:#f8fafc;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}}
  .card-header h2{{font-size:.95rem;font-weight:600;color:#374151}}
  .filters{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
  .filters input,.filters select{{border:1px solid #d1d5db;border-radius:6px;padding:5px 10px;font-size:.82rem;color:#374151;outline:none}}
  .filters input:focus,.filters select:focus{{border-color:#2563eb;box-shadow:0 0 0 2px rgba(37,99,235,.15)}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem}}
  th{{background:#f8fafc;padding:10px 14px;text-align:left;font-size:.75rem;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid #e2e8f0;white-space:nowrap}}
  td{{padding:11px 14px;border-bottom:1px solid #f1f5f9;vertical-align:middle}}
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
  <a href="/po/refresh" title="Pull latest data from MaintainX" style="font-size:.8rem;color:#2563eb;text-decoration:none;padding:5px 12px;border:1px solid #bfdbfe;border-radius:6px;white-space:nowrap;background:#eff6ff;">&#8635; Refresh Data</a>
</div>

<div class="kpi-strip">
  <div class="kpi red">
    <div class="val">{n_needs}</div>
    <div class="lbl">Needs Payment</div>
  </div>
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
        <select id="invoiceFilter" onchange="applyFilters()">
          <option value="unpaid-pending">Unpaid &amp; Pending</option>
          <option value="all">All (incl. Paid)</option>
          <option value="unpaid">Unpaid Only</option>
          <option value="pending">Pending Only</option>
          <option value="paid">Paid Only</option>
        </select>
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
            <th>Invoice Status</th>
            <th>Vendor</th>
            <th>Status</th>
            <th style="text-align:right">Total</th>
            <th>Approved</th>
            <th>Due Date</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody id="poBody">
          {rows_html if rows_html else '<tr><td colspan="8" class="empty">No completed purchase orders found.</td></tr>'}
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
function saveInvStatus(poId, btn) {{
  var sel     = document.getElementById('inv-' + poId);
  var newVal  = sel.value;
  var origTxt = btn.textContent;
  btn.textContent  = 'Saving\u2026';
  btn.disabled     = true;
  btn.style.background = '#6b7280';
  fetch('/po/update-status', {{
    method:  'POST',
    headers: {{'Content-Type': 'application/json'}},
    body:    JSON.stringify({{po_id: poId, invoice_status: newVal || null}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(data) {{
    if (data.ok) {{
      btn.textContent      = '\u2713 Saved';
      btn.style.background = '#16a34a';
      sel.closest('tr').dataset.invoiceStatus = newVal;
      setTimeout(function() {{ applyFilters(); }}, 400);
    }} else {{
      btn.textContent      = 'Error';
      btn.style.background = '#dc2626';
    }}
    setTimeout(function() {{
      btn.textContent      = origTxt;
      btn.style.background = '#2563eb';
      btn.disabled         = false;
    }}, 2000);
  }})
  .catch(function() {{
    btn.textContent      = 'Error';
    btn.style.background = '#dc2626';
    setTimeout(function() {{
      btn.textContent      = origTxt;
      btn.style.background = '#2563eb';
      btn.disabled         = false;
    }}, 2000);
  }});
}}

function saveInforNum(poId, vendorId, btn) {{
  if (!vendorId) {{ alert('No vendor ID found — use Refresh Data to reload vendor info.'); return; }}
  var inp     = document.getElementById('inf-' + poId);
  var newVal  = inp.value.trim();
  var origTxt = btn.textContent;
  btn.textContent      = 'Saving\u2026';
  btn.disabled         = true;
  btn.style.background = '#6b7280';
  fetch('/vendor/update-infor-number', {{
    method:  'POST',
    headers: {{'Content-Type': 'application/json'}},
    body:    JSON.stringify({{vendor_id: vendorId, infor_vendor_number: newVal || null}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(data) {{
    if (data.ok) {{
      btn.textContent      = '\u2713 Saved';
      btn.style.background = '#16a34a';
    }} else {{
      btn.textContent      = 'Error';
      btn.style.background = '#dc2626';
    }}
    setTimeout(function() {{
      btn.textContent      = origTxt;
      btn.style.background = '#2563eb';
      btn.disabled         = false;
    }}, 2000);
  }})
  .catch(function() {{
    btn.textContent      = 'Error';
    btn.style.background = '#dc2626';
    setTimeout(function() {{
      btn.textContent      = origTxt;
      btn.style.background = '#2563eb';
      btn.disabled         = false;
    }}, 2000);
  }});
}}

function applyFilters() {{
  var search  = document.getElementById('search').value.toLowerCase();
  var status  = document.getElementById('statusFilter').value;
  var vendor  = document.getElementById('vendorFilter').value;
  var invFilt = document.getElementById('invoiceFilter').value;
  var rows    = document.querySelectorAll('#poBody .po-row');
  var visible = 0;
  rows.forEach(function(row) {{
    var inv          = row.dataset.invoiceStatus;
    var matchSearch  = !search || row.textContent.toLowerCase().includes(search);
    var matchStatus  = !status || row.dataset.status === status;
    var matchVendor  = !vendor || row.dataset.vendor === vendor;
    var matchInvoice = true;
    if      (invFilt === 'unpaid-pending') {{ matchInvoice = inv !== 'Paid'; }}
    else if (invFilt === 'unpaid')         {{ matchInvoice = inv === 'Unpaid'; }}
    else if (invFilt === 'pending')        {{ matchInvoice = inv === ''; }}
    else if (invFilt === 'paid')           {{ matchInvoice = inv === 'Paid'; }}
    var show = matchSearch && matchStatus && matchVendor && matchInvoice;
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
