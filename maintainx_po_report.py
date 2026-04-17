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

try:
    from fpdf import FPDF as _FPDF
    _FPDF_AVAILABLE = True
except ImportError:
    _FPDF_AVAILABLE = False

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

        # Approver name — try common column name variants
        approver_name = (
            first.get("Approver Name") or first.get("Approved By") or
            first.get("Approver") or ""
        ).strip()

        pos_by_id[po_id] = {
            "id":            int(po_id),
            "overrideNumber": _sanitize((first.get("Purchase Order #") or "").strip()),
            "title":         _sanitize((first.get("Purchase Order Title") or "").strip()),
            "vendorName":    _sanitize((first.get("Vendor") or "").strip()) or "Unknown Vendor",
            "status":        status,
            "note":          _sanitize((first.get("Notes") or "").strip()),
            "approvalDate":  _parse_csv_date(first.get("Approved On")),
            "updatedAt":     _parse_csv_date(
                                 first.get("Completed On") or first.get("Approved On")
                             ),
            "dueDate":       _parse_csv_date(first.get("Due Date")),
            "invoice_status": invoice_status,
            "_total":        total,   # pre-computed; picked up by calc_po_total()
            "approver_name": _sanitize(approver_name),
            "line_items":    _extract_line_items(rows),
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


def _deep_sanitize(obj):
    """
    Recursively walk a JSON-serializable structure and sanitize all string values,
    replacing any lone surrogates that would cause json.dumps() to fail.
    """
    if isinstance(obj, dict):
        return {k: _deep_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_sanitize(v) for v in obj]
    if isinstance(obj, str):
        return _sanitize(obj)
    return obj


def save_cache(pos_by_id):
    """Persist {str(po_id): po_dict} to disk."""
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "pos": pos_by_id,
    }
    try:
        text = json.dumps(payload, default=str)
    except (UnicodeEncodeError, ValueError):
        # Surrogate characters slipped through — sanitize the whole payload and retry
        text = json.dumps(_deep_sanitize(payload), default=str)
    CACHE_FILE.write_text(text, encoding="utf-8")


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
    try:
        text = json.dumps(payload, default=str)
    except (UnicodeEncodeError, ValueError):
        text = json.dumps(_deep_sanitize(payload), default=str)
    VENDOR_CACHE_FILE.write_text(text, encoding="utf-8")


def fetch_vendor_data(api_key):
    """
    Fetch all vendor data including 'Infor Vendor #' via the CSV export endpoint.
    The REST API does not reliably return extraFields; the CSV always does.

    Returns {vendor_name_lower: {"id": int, "name": str, "infor_vendor_number": str|None}}.
    Cached in VENDOR_CACHE_FILE for VENDOR_CACHE_TTL minutes (one API call total).
    """
    cached = load_vendor_cache()
    age    = vendor_cache_age_minutes()
    if cached and age is not None and age < VENDOR_CACHE_TTL:
        print(f"  Vendor cache is {age:.0f}min old — using without API call.")
        return cached

    print("  Fetching vendor data from CSV export endpoint...")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})

    try:
        resp = _rl_get(session, f"{BASE_URL}/vendors/vendors.csv")
    except RateLimitError:
        if cached:
            print("  Rate limited on vendor CSV fetch — using cached data.")
            return cached
        raise

    # Strip BOM (same issue as the PO CSV export)
    text   = resp.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    vendors_by_name = {}
    for row in reader:
        vendor_id   = (row.get("ID") or "").strip()
        vendor_name = (row.get("Vendor") or "").strip()
        infor_num   = (row.get("Infor Vendor #") or "").strip() or None
        if vendor_id and vendor_name:
            try:
                vendors_by_name[vendor_name.lower()] = {
                    "id":                  int(vendor_id),
                    "name":                vendor_name,
                    "infor_vendor_number": infor_num,
                }
            except (ValueError, TypeError):
                pass

    save_vendor_cache(vendors_by_name)
    print(f"  Vendor CSV parsed — {len(vendors_by_name)} vendor(s).")
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


def _sanitize(s):
    """
    Strip lone surrogate code points (U+D800–U+DFFF) that are sometimes
    present in MaintainX CSV exports when a field contains unusual characters.
    Lone surrogates cannot be encoded as UTF-8 and cause json.dumps() to raise
    UnicodeEncodeError in Python 3.12's C JSON accelerator.
    """
    if not s:
        return s
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _get_csv_field(row, *keys):
    """Try multiple column name variants, return first non-empty sanitized value."""
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            return _sanitize(v)
    return ""


def _extract_line_items(rows):
    """
    Build a list of line-item dicts from the CSV rows belonging to one PO.
    MaintainX PO CSV has one row per line item; PO-level fields repeat on each row.
    """
    items = []
    for i, row in enumerate(rows, 1):
        part_name = _get_csv_field(row,
            "Part", "Item", "Description", "Part Name", "Item Name", "Line Name")
        part_num  = _get_csv_field(row,
            "Part #", "Part Number", "SKU", "Part No", "Part No.")
        unit_cost = _get_csv_field(row,
            "Unit Cost", "Unit Price", "Price")
        qty_ord   = _get_csv_field(row,
            "Quantity Ordered", "Qty Ordered", "Quantity", "Ordered Qty", "Qty")
        qty_rcv   = _get_csv_field(row,
            "Quantity Received", "Qty Received", "Received Qty", "Received")
        line_tot  = _get_csv_field(row,
            "Line Total", "Total Cost", "Ordered Cost", "Line Cost", "Amount")

        def _to_float(s):
            try:
                return round(float(s.replace(",", "").replace("$", "").strip()), 4) if s else None
            except (ValueError, TypeError):
                return None

        uc = _to_float(unit_cost)
        qo = _to_float(qty_ord)
        qr = _to_float(qty_rcv)
        lt = _to_float(line_tot)

        # Compute line total if missing
        if lt is None and uc is not None and qo is not None:
            lt = round(uc * qo, 2)

        # Skip rows with no meaningful content (blank line item rows)
        if not part_name and not part_num and uc is None and qo is None:
            continue

        items.append({
            "line_number":   i,
            "part_name":     part_name,
            "part_number":   part_num,
            "unit_cost":     uc,
            "qty_ordered":   qo,
            "qty_received":  qr,
            "line_total":    lt,
        })
    return items


_PDF_CHAR_MAP = [
    # Common Unicode punctuation that Helvetica (Latin-1) can't render
    ("\u2014", "-"),    # em dash  —
    ("\u2013", "-"),    # en dash  –
    ("\u2012", "-"),    # figure dash
    ("\u2015", "-"),    # horizontal bar
    ("\u2018", "'"),    # left single quote
    ("\u2019", "'"),    # right single quote
    ("\u201A", ","),    # single low-9 quote
    ("\u201C", '"'),    # left double quote
    ("\u201D", '"'),    # right double quote
    ("\u201E", '"'),    # double low-9 quote
    ("\u2026", "..."),  # ellipsis
    ("\u00A0", " "),    # non-breaking space
    ("\u2022", "*"),    # bullet
    ("\u00AE", "(R)"),  # registered trademark
    ("\u00A9", "(C)"),  # copyright
    ("\u2122", "(TM)"), # trademark
    ("\u00D7", "x"),    # multiplication sign
    ("\u00F7", "/"),    # division sign
]


def _pdf_safe(text):
    """
    Convert text to a string safe for fpdf2's built-in Helvetica font (Latin-1 range).
    Maps common Unicode punctuation/symbols to ASCII equivalents, then falls back
    to latin-1 encoding (replacing any remaining unsupported chars with '?').
    """
    if text is None:
        return ""
    s = str(text)
    for uni_char, replacement in _PDF_CHAR_MAP:
        s = s.replace(uni_char, replacement)
    # Encode to latin-1; this handles accented chars (e, n, etc.) natively
    # and replaces anything still outside latin-1 with '?'
    return s.encode("latin-1", errors="replace").decode("latin-1")


def _fmt_qty(val):
    """Format a quantity: integer if whole number, else up to 2 decimal places."""
    if val is None:
        return "—"
    try:
        f = float(val)
        return str(int(f)) if f == int(f) else f"{f:.2f}"
    except (ValueError, TypeError):
        return str(val)


def build_receipt_pdf(po_dict):
    """
    Generate a PDF payment receipt for a single PO.
    Returns raw PDF bytes for streaming as a file download.
    Requires fpdf2 (pure Python — no system dependencies, works on Vercel).
    """
    if not _FPDF_AVAILABLE:
        raise RuntimeError(
            "fpdf2 is not installed. Add 'fpdf2>=2.7.0' to requirements.txt and redeploy."
        )

    # ── Colours ──────────────────────────────────────────────────────────────────
    C_BLUE  = (15,  45,  82)
    C_GREEN = (22,  163, 74)
    C_LGRAY = (248, 250, 252)
    C_DGRAY = (107, 114, 128)
    C_BORD  = (226, 232, 240)
    C_WHITE = (255, 255, 255)
    C_BLACK = (30,  41,  59)

    pdf = _FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pw = pdf.w - 30   # usable page width (A4 210 mm − 30 mm margins)

    # ── Header band ───────────────────────────────────────────────────────────────
    pdf.set_fill_color(*C_BLUE)
    pdf.set_text_color(*C_WHITE)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(pw, 11, "PURCHASE ORDER RECEIPT", border=0, align="C", fill=True)
    pdf.ln(11)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(pw, 6, "Accounts Payable Department", border=0, align="C", fill=True)
    pdf.ln(8)

    # ── Info grid (2-column) ──────────────────────────────────────────────────────
    pnum       = _pdf_safe(po_number(po_dict))
    vendor     = _pdf_safe(po_dict.get("vendorName", "Unknown Vendor"))
    infor_num  = _pdf_safe(po_dict.get("infor_vendor_number") or "N/A")
    approver   = _pdf_safe(po_dict.get("approver_name") or "N/A")
    status_lbl = _pdf_safe(STATUS_LABEL.get((po_dict.get("status") or "").upper(),
                                             po_dict.get("status", "N/A")))
    approved   = _pdf_safe(fmt_date(po_dict.get("approvalDate") or po_dict.get("updatedAt")))
    due        = _pdf_safe(fmt_date(po_dict.get("dueDate")))
    paid_raw   = po_dict.get("paid_at")
    paid_date  = _pdf_safe(fmt_date(paid_raw) if paid_raw
                           else datetime.now().strftime("%b %d, %Y"))

    lx    = pdf.l_margin
    rx    = lx + pw / 2 + 2
    half  = pw / 2 - 2
    sy    = pdf.get_y()
    LH    = 6        # row height in info grid
    LBL_W = 30       # label column width

    left_items  = [("PO #",           pnum,       None   ),
                   ("Vendor",         vendor,     None   ),
                   ("Infor #",        infor_num,  None   ),
                   ("Approver",       approver,   None   )]
    right_items = [("Status",         status_lbl, None   ),
                   ("Approved",       approved,   None   ),
                   ("Due Date",       due,        None   ),
                   ("Posted Date",           paid_date,  C_GREEN)]

    for i, (label, value, color) in enumerate(left_items):
        pdf.set_xy(lx, sy + i * LH)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*C_DGRAY)
        pdf.cell(LBL_W, LH, label.upper(), border=0)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*(color or C_BLACK))
        pdf.cell(half - LBL_W, LH, _pdf_safe(value or "N/A"), border=0)

    for i, (label, value, color) in enumerate(right_items):
        pdf.set_xy(rx, sy + i * LH)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*C_DGRAY)
        pdf.cell(22, LH, label.upper(), border=0)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*(color or C_BLACK))
        pdf.cell(half - 22, LH, _pdf_safe(value or "N/A"), border=0)

    pdf.set_text_color(*C_BLACK)
    pdf.set_y(sy + len(left_items) * LH + 4)

    # ── Divider ───────────────────────────────────────────────────────────────────
    pdf.set_draw_color(*C_BORD)
    pdf.line(lx, pdf.get_y(), lx + pw, pdf.get_y())
    pdf.ln(3)

    # ── Line items section header ─────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*C_DGRAY)
    pdf.cell(pw, 5, "LINE ITEMS", border=0)
    pdf.ln(6)

    # ── Line items table ──────────────────────────────────────────────────────────
    line_items = po_dict.get("line_items") or []
    fixed_w = 8 + 28 + 16 + 16 + 24 + 24   # sum of all fixed columns
    desc_w  = pw - fixed_w
    col_w   = [8, desc_w, 28, 16, 16, 24, 24]
    headers = ["#", "Description", "Part #", "Qty Ord", "Qty Rcv", "Unit Cost", "Line Total"]
    aligns  = ["C", "L",           "L",      "R",       "R",       "R",         "R"         ]
    TH      = 6   # table row height

    # Header row
    pdf.set_fill_color(*C_BLUE)
    pdf.set_text_color(*C_WHITE)
    pdf.set_font("Helvetica", "B", 7.5)
    for w, h_text, a in zip(col_w, headers, aligns):
        pdf.cell(w, TH, h_text, border=0, align=a, fill=True)
    pdf.ln(TH)

    # Data rows
    pdf.set_font("Helvetica", "", 8)
    for idx, item in enumerate(line_items):
        fill = (idx % 2 == 1)
        pdf.set_fill_color(*C_LGRAY)
        pdf.set_text_color(*C_BLACK)
        row_data = [
            str(item.get("line_number", idx + 1)),
            _pdf_safe((item.get("part_name")   or "")[:60]),
            _pdf_safe((item.get("part_number") or "")[:18]),
            _pdf_safe(_fmt_qty(item.get("qty_ordered"))),
            _pdf_safe(_fmt_qty(item.get("qty_received"))),
            _pdf_safe(fmt_currency(item.get("unit_cost"))),
            _pdf_safe(fmt_currency(item.get("line_total"))),
        ]
        for w, d, a in zip(col_w, row_data, aligns):
            pdf.cell(w, TH, d, border=0, align=a, fill=fill)
        pdf.ln(TH)

    if not line_items:
        pdf.set_text_color(*C_DGRAY)
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(pw, TH, "No line item data available.", border=0, align="C")
        pdf.ln(TH)

    # ── Total bar ─────────────────────────────────────────────────────────────────
    pdf.ln(1)
    total_str   = _pdf_safe(fmt_currency(calc_po_total(po_dict)))
    tot_label_w = pw - 40
    pdf.set_fill_color(*C_BLUE)
    pdf.set_text_color(*C_WHITE)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(tot_label_w, 8, "ORDER TOTAL", border=0, align="R", fill=True)
    pdf.set_fill_color(*C_GREEN)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(40, 8, total_str, border=0, align="C", fill=True)
    pdf.ln(8)

    # ── Notes ─────────────────────────────────────────────────────────────────────
    notes = _pdf_safe((po_dict.get("note") or "").strip())
    if notes:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*C_DGRAY)
        pdf.cell(pw, 5, "NOTES", border=0)
        pdf.ln(5)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*C_BLACK)
        pdf.multi_cell(pw, 5, notes)

    # ── Footer ────────────────────────────────────────────────────────────────────
    pdf.ln(4)
    pdf.set_draw_color(*C_BORD)
    pdf.line(lx, pdf.get_y(), lx + pw, pdf.get_y())
    pdf.ln(2)
    pdf.set_text_color(*C_DGRAY)
    pdf.set_font("Helvetica", "", 7.5)
    generated = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    pdf.cell(pw / 2, 4, f"Receipt generated: {generated}", border=0)
    pdf.cell(pw / 2, 4, f"PO ID: {po_dict.get('id', '')}", border=0, align="R")

    return bytes(pdf.output())


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

        # Download Receipt button — shown for already-Paid rows (server-rendered)
        receipt_btn = (
            f'<a href="/po/receipt/{po_id}" '
            f'style="display:inline-block;padding:3px 8px;background:#16a34a;color:#fff;'
            f'border-radius:5px;font-size:.75rem;font-weight:500;text-decoration:none;'
            f'margin-left:4px" title="Download PDF receipt">&#128196;&nbsp;Receipt</a>'
        ) if inv_raw == "Paid" else ""

        rows_html += f"""
        <tr class="po-row" data-status="{status}" data-vendor="{vendor}" data-invoice-status="{inv_raw}">
          <td style="font-weight:600;color:#1e40af;white-space:nowrap">{pnum}</td>
          <td style="white-space:nowrap">
            <select id="inv-{po_id}" style="border:1px solid #d1d5db;border-radius:5px;padding:3px 7px;font-size:.78rem;color:#374151;background:#fff;margin-right:4px">{inv_opts}</select><button onclick="saveInvStatus({po_id},this)" style="padding:3px 8px;background:#2563eb;color:#fff;border:none;border-radius:5px;font-size:.75rem;cursor:pointer;font-weight:500">Save</button>{receipt_btn}
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
  btn.textContent      = 'Saving\u2026';
  btn.disabled         = true;
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

      if (newVal === 'Paid') {{
        // Add / update the Receipt download link next to the Save button
        var td = sel.closest('td');
        var existing = td.querySelector('.receipt-link');
        if (!existing) {{
          var a = document.createElement('a');
          a.className   = 'receipt-link';
          a.style.cssText = 'display:inline-block;padding:3px 8px;background:#16a34a;'
            + 'color:#fff;border-radius:5px;font-size:.75rem;font-weight:500;'
            + 'text-decoration:none;margin-left:4px';
          a.title     = 'Download PDF receipt';
          a.innerHTML = '&#128196;&nbsp;Receipt';
          td.appendChild(a);
          existing = a;
        }}
        existing.href = '/po/receipt/' + poId;
        // Auto-trigger download (browser stays on page because Content-Disposition: attachment)
        setTimeout(function() {{ window.location.href = '/po/receipt/' + poId; }}, 350);
        // Hide row after a brief delay (filter will remove it from "unpaid-pending" view)
        setTimeout(function() {{ applyFilters(); }}, 900);
      }} else {{
        // Remove receipt link if status moved away from Paid
        var existing = sel.closest('td').querySelector('.receipt-link');
        if (existing) {{ existing.remove(); }}
        setTimeout(function() {{ applyFilters(); }}, 400);
      }}
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
    # Final safety net: strip any stray surrogate code-points that could have
    # been introduced by MaintainX field values embedded in table cells.
    return html.encode("utf-8", errors="replace").decode("utf-8")


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
