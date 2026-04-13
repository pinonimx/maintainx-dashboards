#!/usr/bin/env python3
"""
MaintainX → Maintenance & Reliability Dashboard
================================================
Fetches work orders and assets from the MaintainX API, computes
key M&R metrics, and writes a self-contained HTML dashboard file.

Requirements:
    pip install requests

Usage:
    python maintainx_dashboard_refresh.py

Output:
    maintenance_reliability_dashboard.html  (same folder as this script)

IMPORTANT: Rotate your API token in MaintainX after testing.
           Store future tokens in an environment variable, not in code:
               export MAINTAINX_TOKEN="your_token_here"
           Then update the TOKEN line below to:
               TOKEN = os.environ["MAINTAINX_TOKEN"]
"""

import os
import json
import math
import requests
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN         = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOjEwNTM2NzcsIm9yZ2FuaXphdGlvbklkIjo0MjMwNjgsImlhdCI6MTc3Mzc1NDM0Mywic3ViIjoiUkVTVF9BUElfQVVUSCIsImp0aSI6IjI5MmNlNWI0LWVhMDgtNDk5Ni1iZDRkLTI2NmRkMDRkOTRjNiJ9.Fx5alf0-gNc0JJ0pv6rMNpL0FYAQB7DHv7PYZ7mzXgw"
BASE_URL      = "https://api.getmaintainx.com/v1"
LOOKBACK_DAYS = 365          # how far back to pull work orders
OUTPUT_FILE   = "maintenance_reliability_dashboard.html"
PAGE_SIZE     = 100          # MaintainX max page size

# ── API helpers ───────────────────────────────────────────────────────────────
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def get_all_pages(endpoint, params=None):
    """Fetch all pages from a paginated MaintainX endpoint."""
    params = params or {}
    params["pageSize"] = PAGE_SIZE
    results = []
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(f"{BASE_URL}{endpoint}", headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()

        # MaintainX wraps results in a key matching the endpoint (e.g. "workOrders", "assets")
        data_key = None
        for k in body:
            if isinstance(body[k], list):
                data_key = k
                break
        if not data_key:
            break

        batch = body[data_key]
        results.extend(batch)

        total = body.get("total", len(results))
        if len(results) >= total or len(batch) < PAGE_SIZE:
            break
        page += 1

    return results

# ── Fetch data ────────────────────────────────────────────────────────────────
def fetch_data():
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    print("Fetching work orders...")
    work_orders = get_all_pages("/workorders", {"createdAfter": cutoff_str})
    print(f"  → {len(work_orders)} work orders retrieved")

    print("Fetching assets...")
    assets = get_all_pages("/assets")
    print(f"  → {len(assets)} assets retrieved")

    print("Fetching locations...")
    try:
        locations = get_all_pages("/locations")
        print(f"  → {len(locations)} locations retrieved")
    except Exception:
        locations = []
        print("  → Could not fetch locations (skipping)")

    return work_orders, assets, locations

# ── Metric helpers ────────────────────────────────────────────────────────────
def parse_dt(val):
    """Parse a datetime string to a timezone-aware datetime, or return None."""
    if not val:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(val[:26].rstrip("Z") + "Z", "%Y-%m-%dT%H:%M:%S.%fZ"
                                   if "." in val else "%Y-%m-%dT%H:%M:%SZ")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def safe_float(val, fallback=0.0):
    try:
        return float(val) if val is not None else fallback
    except (TypeError, ValueError):
        return fallback

def is_reactive(wo):
    """Work order types that count as unplanned / failures."""
    wo_type = (wo.get("type") or wo.get("workOrderType") or "").upper()
    return wo_type in ("REACTIVE", "CORRECTIVE", "UNPLANNED", "EMERGENCY")

def is_planned(wo):
    wo_type = (wo.get("type") or wo.get("workOrderType") or "").upper()
    return wo_type in ("PREVENTIVE", "PLANNED", "PM", "INSPECTION", "ROUTINE")

def wo_status(wo):
    s = (wo.get("status") or "").upper()
    if s in ("DONE", "COMPLETE", "COMPLETED"):
        return "Completed"
    if s in ("OPEN", "IN_PROGRESS", "IN PROGRESS", "INPROGRESS"):
        return "In Progress"
    return "Overdue"

def wo_priority(wo):
    p = (wo.get("priority") or "NONE").upper()
    mapping = {"NONE": "Low", "LOW": "Low", "MEDIUM": "Medium", "HIGH": "High", "CRITICAL": "Critical"}
    return mapping.get(p, "Medium")

def get_asset_name(wo):
    asset = wo.get("asset") or {}
    if isinstance(asset, dict):
        return asset.get("name") or asset.get("title") or "Unknown Asset"
    return str(asset) if asset else "Unknown Asset"

def get_asset_id(wo):
    asset = wo.get("asset") or {}
    if isinstance(asset, dict):
        return asset.get("id") or asset.get("uid") or ""
    return ""

def get_location_name(wo):
    loc = wo.get("location") or wo.get("site") or {}
    if isinstance(loc, dict):
        return loc.get("name") or loc.get("title") or "Unknown Site"
    return str(loc) if loc else "Unknown Site"

def get_downtime_hours(wo):
    """Estimate downtime from actualDuration or estimatedDuration (in minutes → hours)."""
    for key in ("actualDuration", "estimatedDuration", "duration"):
        val = wo.get(key)
        if val is not None:
            return safe_float(val) / 60.0  # MaintainX stores durations in minutes
    return 2.0  # fallback estimate

# ── Compute metrics ───────────────────────────────────────────────────────────
def compute_metrics(work_orders, assets):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    # ── Monthly summary ──────────────────────────────────────────────────────
    monthly = defaultdict(lambda: {"planned": 0, "unplanned": 0, "downtime_hrs": 0.0})
    for wo in work_orders:
        created = parse_dt(wo.get("createdAt"))
        if not created or created < cutoff:
            continue
        key = created.strftime("%Y-%m")
        if is_reactive(wo):
            monthly[key]["unplanned"] += 1
            monthly[key]["downtime_hrs"] += get_downtime_hours(wo)
        elif is_planned(wo):
            monthly[key]["planned"] += 1

    # Fill in all months in range and compute availability
    monthly_list = []
    for i in range(LOOKBACK_DAYS // 30, -1, -1):
        month_dt = now - timedelta(days=30 * i)
        key = month_dt.strftime("%Y-%m")
        lbl = month_dt.strftime("%b %y")
        hours_in_month = 24 * 30
        downtime = monthly[key]["downtime_hrs"]
        avail = max(0.0, min(100.0, (1 - downtime / hours_in_month) * 100))
        monthly_list.append({
            "m":       key,
            "lbl":     lbl,
            "avail":   round(avail, 1),
            "plan":    monthly[key]["planned"],
            "unplan":  monthly[key]["unplanned"],
        })
    # Keep only last 12 months, remove future months
    monthly_list = [m for m in monthly_list if m["m"] <= now.strftime("%Y-%m")][-12:]

    # ── Per-asset metrics ────────────────────────────────────────────────────
    asset_wos = defaultdict(list)
    for wo in work_orders:
        aid = get_asset_id(wo)
        if aid:
            asset_wos[aid].append(wo)

    # Build lookup from MaintainX asset records
    asset_lookup = {}
    for a in assets:
        aid = a.get("id") or a.get("uid") or ""
        if aid:
            asset_lookup[aid] = a

    asset_metrics = []
    seen_names = set()

    # Collect assets that appear in work orders
    for aid, wos_for_asset in asset_wos.items():
        a_rec = asset_lookup.get(aid, {})
        name  = a_rec.get("name") or a_rec.get("title") or get_asset_name(wos_for_asset[0])
        if name in seen_names:
            continue
        seen_names.add(name)

        atype = (a_rec.get("category") or a_rec.get("assetType") or
                 a_rec.get("type") or "Mechanical")
        site  = get_location_name(wos_for_asset[0])

        reactive_wos   = [w for w in wos_for_asset if is_reactive(w)]
        completed_wos  = [w for w in wos_for_asset if wo_status(w) == "Completed"]
        failures       = len(reactive_wos)
        period_days    = LOOKBACK_DAYS

        # MTBF (days) = operating_days / number_of_failures
        mtbf = round(period_days / failures) if failures > 0 else period_days

        # MTTR (hours) = average repair duration across reactive completed WOs
        repair_times = [get_downtime_hours(w) for w in reactive_wos if wo_status(w) == "Completed"]
        mttr = round(sum(repair_times) / len(repair_times), 1) if repair_times else 2.0

        # Availability = 1 - (total downtime / total hours)
        total_downtime = sum(get_downtime_hours(w) for w in reactive_wos)
        total_hours    = 24 * period_days
        avail = round(max(0, min(100, (1 - total_downtime / total_hours) * 100)), 1)

        # Health score (0–100): composite of availability, MTBF normalised, planned ratio
        avail_score = min(avail, 100)
        mtbf_score  = min(mtbf / 90 * 100, 100)   # 90-day MTBF = full score
        total_wos   = len(wos_for_asset)
        plan_ratio  = (total_wos - failures) / total_wos if total_wos > 0 else 1.0
        plan_score  = plan_ratio * 100
        health = round(0.4 * avail_score + 0.3 * mtbf_score + 0.3 * plan_score)

        asset_metrics.append({
            "id":       aid,
            "name":     name,
            "atype":    atype,
            "site":     site,
            "failures": failures,
            "mtbf":     mtbf,
            "mttr":     mttr,
            "avail":    avail,
            "health":   health,
        })

    # ── Work order table rows ────────────────────────────────────────────────
    wo_rows = []
    for wo in work_orders:
        created = parse_dt(wo.get("createdAt"))
        if not created:
            continue
        wo_rows.append({
            "id":       str(wo.get("id") or wo.get("uid") or ""),
            "date":     created.strftime("%Y-%m-%d"),
            "asset":    get_asset_name(wo),
            "atype":    (wo.get("asset") or {}).get("category", "Mechanical") if isinstance(wo.get("asset"), dict) else "Mechanical",
            "site":     get_location_name(wo),
            "priority": wo_priority(wo),
            "wo_type":  "Unplanned" if is_reactive(wo) else "Planned",
            "status":   wo_status(wo),
            "downtime": round(get_downtime_hours(wo), 1),
            "tech":     _first_assignee(wo),
        })

    # Sort WOs newest first, cap at 100 rows for the table
    wo_rows.sort(key=lambda x: x["date"], reverse=True)
    wo_rows = wo_rows[:100]

    return monthly_list, asset_metrics, wo_rows

def _first_assignee(wo):
    assignees = wo.get("assignees") or wo.get("assignedTo") or []
    if assignees and isinstance(assignees, list):
        a = assignees[0]
        if isinstance(a, dict):
            fn = a.get("firstName") or ""
            ln = a.get("lastName") or ""
            return f"{fn[0]}. {ln}".strip(". ") if fn else ln or "Unassigned"
    return "Unassigned"

# ── Build HTML ────────────────────────────────────────────────────────────────
def build_html(monthly_list, asset_metrics, wo_rows):
    now_str = datetime.now().strftime("%B %d, %Y")
    monthly_json = json.dumps(monthly_list, indent=2)
    assets_json  = json.dumps(asset_metrics, indent=2)
    wos_json     = json.dumps(wo_rows, indent=2)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Maintenance &amp; Reliability Dashboard</title>
  <script
    src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1"
    integrity="sha384-jb8JQMbMoBUzgWatfe6COACi2ljcDdZQ2OxczGA3bGNeWe+6DChMTBJemed7ZnvJ"
    crossorigin="anonymous"
  ></script>
  <style>
    :root {{
      --bg:#f0f2f5;--card:#fff;--header:#0d1f3c;--text:#1a1a2e;--muted:#6c757d;
      --on-dark:#dde3ef;--gap:16px;--radius:10px;--shadow:0 1px 4px rgba(0,0,0,.09);
      --c1:#2563eb;--c2:#16a34a;--c3:#d97706;--c4:#dc2626;--c5:#7c3aed;--c6:#0891b2;
    }}
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}}
    .wrap{{max-width:1440px;margin:0 auto;padding:var(--gap)}}
    .hdr{{background:var(--header);color:var(--on-dark);padding:16px 24px;border-radius:var(--radius);margin-bottom:var(--gap);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}}
    .hdr-left{{display:flex;align-items:center;gap:12px}}
    .hdr h1{{font-size:18px;font-weight:700;letter-spacing:-.3px}}
    .hdr-sub{{font-size:11px;color:rgba(221,227,239,.6);margin-top:1px}}
    .live-tag{{font-size:11px;background:rgba(22,163,74,.22);color:#4ade80;padding:3px 8px;border-radius:4px;border:1px solid rgba(74,222,128,.28)}}
    .filters{{display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
    .fg{{display:flex;align-items:center;gap:6px}}
    .fg label{{font-size:11px;color:rgba(221,227,239,.65);text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}}
    .fg select{{padding:5px 10px;border:1px solid rgba(255,255,255,.14);border-radius:6px;background:rgba(255,255,255,.09);color:var(--on-dark);font-size:12px;cursor:pointer;outline:none}}
    .fg select:hover{{background:rgba(255,255,255,.15)}}
    .fg select option{{background:#1e3a5f}}
    .kpi-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:var(--gap);margin-bottom:var(--gap)}}
    .kpi{{background:var(--card);border-radius:var(--radius);padding:20px 22px;box-shadow:var(--shadow);border-left:4px solid var(--c1)}}
    .kpi:nth-child(1){{border-color:var(--c1)}}.kpi:nth-child(2){{border-color:var(--c2)}}.kpi:nth-child(3){{border-color:var(--c3)}}.kpi:nth-child(4){{border-color:var(--c5)}}
    .kpi-lbl{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px}}
    .kpi-val{{font-size:30px;font-weight:800;line-height:1;margin-bottom:5px}}
    .kpi-sub{{font-size:11.5px;color:var(--muted)}}
    .ok{{color:#16a34a;font-weight:600}}.warn{{color:#d97706;font-weight:600}}.bad{{color:#dc2626;font-weight:600}}
    .row-full{{margin-bottom:var(--gap)}}
    .row-2{{display:grid;grid-template-columns:1.55fr 1fr;gap:var(--gap);margin-bottom:var(--gap)}}
    .row-2b{{display:grid;grid-template-columns:1fr 1fr;gap:var(--gap);margin-bottom:var(--gap)}}
    .card{{background:var(--card);border-radius:var(--radius);padding:20px 24px;box-shadow:var(--shadow)}}
    .ctitle{{font-size:13px;font-weight:700;margin-bottom:2px}}
    .csub{{font-size:11px;color:var(--muted);margin-bottom:16px}}
    .ch{{position:relative;height:260px}}.ch-lg{{position:relative;height:290px}}
    .h-item{{margin-bottom:11px}}
    .h-top{{display:flex;justify-content:space-between;margin-bottom:3px}}
    .h-name{{font-size:12px;font-weight:600}}
    .h-score{{font-size:11px;font-weight:700}}
    .h-track{{height:7px;background:#e5e7eb;border-radius:4px;overflow:hidden}}
    .h-fill{{height:100%;border-radius:4px}}
    .hc-g{{background:#16a34a}}.hc-w{{background:#d97706}}.hc-r{{background:#dc2626}}
    .h-meta{{font-size:10px;color:#9ca3af;margin-top:2px}}
    .tbl-wrap{{overflow-x:auto}}
    table{{width:100%;border-collapse:collapse;font-size:12.5px}}
    thead th{{text-align:left;padding:9px 12px;border-bottom:2px solid #e5e7eb;color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap;cursor:pointer;user-select:none}}
    thead th:hover{{color:var(--text);background:#f9fafb}}
    tbody td{{padding:9px 12px;border-bottom:1px solid #f3f4f6;white-space:nowrap}}
    tbody tr:hover{{background:#f9fafb}}
    tbody tr:last-child td{{border-bottom:none}}
    .badge{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}}
    .b-critical{{background:#fee2e2;color:#991b1b}}.b-high{{background:#ffedd5;color:#9a3412}}
    .b-medium{{background:#fef9c3;color:#713f12}}.b-low{{background:#f0fdf4;color:#166534}}
    .b-completed{{background:#dcfce7;color:#15803d}}.b-inprogress{{background:#dbeafe;color:#1d4ed8}}
    .b-overdue{{background:#fee2e2;color:#b91c1c}}.b-planned{{background:#ede9fe;color:#5b21b6}}
    .b-unplanned{{background:#ffedd5;color:#9a3412}}
    .footer{{text-align:center;font-size:11px;color:#9ca3af;margin-top:10px;padding-bottom:6px}}
    @media(max-width:900px){{.kpi-row{{grid-template-columns:repeat(2,1fr)}}.row-2,.row-2b{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
<div class="wrap">
  <header class="hdr">
    <div class="hdr-left">
      <span style="font-size:26px">🔧</span>
      <div>
        <h1>Maintenance &amp; Reliability Dashboard</h1>
        <div class="hdr-sub">Live data from MaintainX &middot; Asset uptime &middot; work orders &middot; failure analysis</div>
      </div>
    </div>
    <div class="filters">
      <span class="live-tag">&#9679; Live Data</span>
      <div class="fg"><label>Period</label>
        <select id="f-period" onchange="dash.apply()">
          <option value="12">Last 12 Months</option><option value="6">Last 6 Months</option><option value="3">Last 3 Months</option>
        </select></div>
      <div class="fg"><label>Site</label><select id="f-site" onchange="dash.apply()"><option value="all">All Sites</option></select></div>
      <div class="fg"><label>Asset Type</label><select id="f-type" onchange="dash.apply()"><option value="all">All Types</option></select></div>
    </div>
  </header>

  <section class="kpi-row">
    <div class="kpi"><div class="kpi-lbl">Overall Availability</div><div class="kpi-val" id="kv-avail">—</div><div class="kpi-sub" id="ks-avail"></div></div>
    <div class="kpi"><div class="kpi-lbl">Avg MTBF</div><div class="kpi-val" id="kv-mtbf">—</div><div class="kpi-sub">Mean Time Between Failures</div></div>
    <div class="kpi"><div class="kpi-lbl">Avg MTTR</div><div class="kpi-val" id="kv-mttr">—</div><div class="kpi-sub">Mean Time To Repair</div></div>
    <div class="kpi"><div class="kpi-lbl">WO Completion Rate</div><div class="kpi-val" id="kv-wo">—</div><div class="kpi-sub" id="ks-wo"></div></div>
  </section>

  <div class="row-full"><div class="card">
    <div class="ctitle">Equipment Availability Trend</div>
    <div class="csub">Monthly availability % &mdash; target 95%</div>
    <div class="ch-lg"><canvas id="ch-avail"></canvas></div>
  </div></div>

  <div class="row-2">
    <div class="card">
      <div class="ctitle">Planned vs Unplanned Work Orders</div>
      <div class="csub">Monthly volume by maintenance type</div>
      <div class="ch"><canvas id="ch-maint"></canvas></div>
    </div>
    <div class="card">
      <div class="ctitle">Failures by Asset Type</div>
      <div class="csub">Total recorded failures (last 12 months)</div>
      <div class="ch"><canvas id="ch-fail"></canvas></div>
    </div>
  </div>

  <div class="row-2b">
    <div class="card">
      <div class="ctitle">MTTR by Asset</div>
      <div class="csub">Average repair time per asset (hours)</div>
      <div class="ch"><canvas id="ch-mttr"></canvas></div>
    </div>
    <div class="card">
      <div class="ctitle">Asset Health Scores</div>
      <div class="csub">Composite index 0&ndash;100 (green &ge;85 &middot; amber &ge;70 &middot; red &lt;70)</div>
      <div id="health-list" style="padding-top:2px;max-height:280px;overflow-y:auto;"></div>
    </div>
  </div>

  <div class="card" style="margin-bottom:var(--gap);">
    <div class="ctitle">Recent Work Orders</div>
    <div class="csub" id="tbl-sub">—</div>
    <div class="tbl-wrap"><table>
      <thead><tr>
        <th onclick="dash.sort('id')">WO #</th><th onclick="dash.sort('date')">Date</th>
        <th onclick="dash.sort('asset')">Asset</th><th onclick="dash.sort('atype')">Type</th>
        <th onclick="dash.sort('site')">Site</th><th onclick="dash.sort('priority')">Priority</th>
        <th onclick="dash.sort('wo_type')">WO Type</th><th onclick="dash.sort('status')">Status</th>
        <th onclick="dash.sort('downtime')" style="text-align:right">Downtime (h)</th>
        <th onclick="dash.sort('tech')">Technician</th>
      </tr></thead>
      <tbody id="tbl-body"></tbody>
    </table></div>
  </div>

  <div class="footer">&#9881; Live data from MaintainX &nbsp;|&nbsp; Refreshed {now_str}</div>
</div>

<script>
const MONTHLY = {monthly_json};
const ASSETS  = {assets_json};
const WOS     = {wos_json};
const C = ['#2563eb','#16a34a','#d97706','#dc2626','#7c3aed','#0891b2'];

// Populate Site and Asset Type filter dropdowns dynamically from data
(function populateFilters() {{
  const sites = [...new Set(ASSETS.map(a => a.site))].sort();
  const types = [...new Set(ASSETS.map(a => a.atype))].sort();
  const siteEl = document.getElementById('f-site');
  const typeEl = document.getElementById('f-type');
  sites.forEach(s => {{ const o=document.createElement('option'); o.value=s; o.textContent=s; siteEl.appendChild(o); }});
  types.forEach(t => {{ const o=document.createElement('option'); o.value=t; o.textContent=t; typeEl.appendChild(o); }});
}})();

class Dashboard {{
  constructor() {{ this.charts={{}}; this.sortCol='date'; this.sortDir='desc'; this.apply(); }}

  filters() {{
    return {{
      period: parseInt(document.getElementById('f-period').value),
      site:   document.getElementById('f-site').value,
      type:   document.getElementById('f-type').value,
    }};
  }}

  apply() {{
    const {{period,site,type}} = this.filters();
    const months = MONTHLY.slice(-period);
    const assets = ASSETS.filter(a => (site==='all'||a.site===site)&&(type==='all'||a.atype===type));
    const wos    = WOS.filter(w   => (site==='all'||w.site===site) &&(type==='all'||w.atype===type));
    this.kpis(assets,wos); this.availChart(months); this.maintChart(months);
    this.failChart(assets); this.mttrChart(assets); this.healthList(assets); this.table(wos);
  }}

  kpis(assets,wos) {{
    if (!assets.length) {{ ['kv-avail','kv-mtbf','kv-mttr','kv-wo'].forEach(id=>document.getElementById(id).textContent='N/A'); return; }}
    const n=assets.length;
    const avail=(assets.reduce((s,a)=>s+a.avail,0)/n).toFixed(1);
    const mtbf=Math.round(assets.reduce((s,a)=>s+a.mtbf,0)/n);
    const mttr=(assets.reduce((s,a)=>s+a.mttr,0)/n).toFixed(1);
    const done=wos.filter(w=>w.status==='Completed').length;
    const ov=wos.filter(w=>w.status==='Overdue').length;
    const woR=wos.length?((done/wos.length)*100).toFixed(1):'—';
    document.getElementById('kv-avail').textContent=avail+'%';
    const sub=document.getElementById('ks-avail');
    if(+avail>=95) sub.innerHTML='<span class="ok">✔ Target met (≥95%)</span>';
    else if(+avail>=90) sub.innerHTML='<span class="warn">⚠ Near target</span>';
    else sub.innerHTML='<span class="bad">✘ Below target</span>';
    document.getElementById('kv-mtbf').textContent=mtbf+' days';
    document.getElementById('kv-mttr').textContent=mttr+' hrs';
    document.getElementById('kv-wo').textContent=woR+'%';
    document.getElementById('ks-wo').textContent=done+' completed · '+ov+' overdue of '+wos.length;
  }}

  availChart(months) {{
    const labels=months.map(m=>m.lbl), data=months.map(m=>m.avail);
    if(this.charts.avail){{ this.charts.avail.data.labels=labels; this.charts.avail.data.datasets[0].data=data; this.charts.avail.update('none'); return; }}
    const tgtPlugin={{id:'tgt',afterDraw(chart){{const{{ctx,chartArea,scales}}=chart;if(!scales.y)return;const y=scales.y.getPixelForValue(95);ctx.save();ctx.beginPath();ctx.setLineDash([6,4]);ctx.strokeStyle='#dc2626';ctx.lineWidth=1.5;ctx.moveTo(chartArea.left,y);ctx.lineTo(chartArea.right,y);ctx.stroke();ctx.fillStyle='#dc2626';ctx.font='10px system-ui';ctx.fillText('Target 95%',chartArea.right-68,y-5);ctx.restore();}}}};
    const ctx=document.getElementById('ch-avail').getContext('2d');
    this.charts.avail=new Chart(ctx,{{type:'line',plugins:[tgtPlugin],data:{{labels,datasets:[{{label:'Availability %',data,borderColor:C[0],backgroundColor:C[0]+'1a',borderWidth:2.5,fill:true,tension:0.35,pointRadius:4,pointHoverRadius:7,pointBackgroundColor:C[0]}}]}},options:{{responsive:true,maintainAspectRatio:false,animation:false,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>' Availability: '+c.parsed.y.toFixed(1)+'%'}}}}}},scales:{{x:{{grid:{{display:false}}}},y:{{min:80,max:100,ticks:{{callback:v=>v+'%',stepSize:2}},grid:{{color:'#f0f0f0'}}}}}}}}}});
  }}

  maintChart(months) {{
    const labels=months.map(m=>m.lbl),planned=months.map(m=>m.plan),unplan=months.map(m=>m.unplan);
    if(this.charts.maint){{this.charts.maint.data.labels=labels;this.charts.maint.data.datasets[0].data=planned;this.charts.maint.data.datasets[1].data=unplan;this.charts.maint.update('none');return;}}
    const ctx=document.getElementById('ch-maint').getContext('2d');
    this.charts.maint=new Chart(ctx,{{type:'bar',data:{{labels,datasets:[{{label:'Planned',data:planned,backgroundColor:C[4]+'bb',borderColor:C[4],borderWidth:1,borderRadius:3,stack:'s'}},{{label:'Unplanned',data:unplan,backgroundColor:C[2]+'bb',borderColor:C[2],borderWidth:1,borderRadius:3,stack:'s'}}]}},options:{{responsive:true,maintainAspectRatio:false,animation:false,plugins:{{legend:{{position:'top',labels:{{usePointStyle:true,padding:16,font:{{size:12}}}}}},tooltip:{{callbacks:{{label:c=>' '+c.dataset.label+': '+c.parsed.y+' WOs'}}}}}},scales:{{x:{{stacked:true,grid:{{display:false}}}},y:{{stacked:true,beginAtZero:true,grid:{{color:'#f0f0f0'}},ticks:{{stepSize:5}}}}}}}}}});
  }}

  failChart(assets) {{
    const types=[...new Set(assets.map(a=>a.atype))];
    const counts=types.map(t=>assets.filter(a=>a.atype===t).reduce((s,a)=>s+a.failures,0));
    const cols=C.slice(0,types.length);
    if(this.charts.fail){{this.charts.fail.data.labels=types;this.charts.fail.data.datasets[0].data=counts;this.charts.fail.data.datasets[0].backgroundColor=cols.map(c=>c+'cc');this.charts.fail.data.datasets[0].borderColor=cols;this.charts.fail.update('none');return;}}
    const ctx=document.getElementById('ch-fail').getContext('2d');
    this.charts.fail=new Chart(ctx,{{type:'doughnut',data:{{labels:types,datasets:[{{data:counts,backgroundColor:cols.map(c=>c+'cc'),borderColor:cols,borderWidth:2,hoverOffset:6}}]}},options:{{responsive:true,maintainAspectRatio:false,animation:false,cutout:'62%',plugins:{{legend:{{position:'right',labels:{{usePointStyle:true,padding:16,font:{{size:12}}}}}},tooltip:{{callbacks:{{label(c){{const tot=c.dataset.data.reduce((a,b)=>a+b,0);return' '+c.label+': '+c.parsed+' ('+((c.parsed/tot)*100).toFixed(1)+'%)';}}}}}}}}}}}}});
  }}

  mttrChart(assets) {{
    const sorted=[...assets].sort((a,b)=>b.mttr-a.mttr);
    const labels=sorted.map(a=>a.name),data=sorted.map(a=>a.mttr);
    const cols=data.map(v=>v>=5?C[3]+'cc':v>=3.5?C[2]+'cc':C[1]+'cc');
    if(this.charts.mttr){{this.charts.mttr.data.labels=labels;this.charts.mttr.data.datasets[0].data=data;this.charts.mttr.data.datasets[0].backgroundColor=cols;this.charts.mttr.update('none');return;}}
    const ctx=document.getElementById('ch-mttr').getContext('2d');
    this.charts.mttr=new Chart(ctx,{{type:'bar',data:{{labels,datasets:[{{label:'MTTR (h)',data,backgroundColor:cols,borderRadius:4}}]}},options:{{responsive:true,maintainAspectRatio:false,animation:false,indexAxis:'y',plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>' MTTR: '+c.parsed.x.toFixed(1)+' hrs'}}}}}},scales:{{x:{{beginAtZero:true,ticks:{{callback:v=>v+'h'}},grid:{{color:'#f0f0f0'}}}},y:{{grid:{{display:false}}}}}}}}}}}});
  }}

  healthList(assets) {{
    const el=document.getElementById('health-list');
    if(!assets.length){{el.innerHTML='<p style="color:#9ca3af;font-size:13px;margin-top:16px;">No assets match filters.</p>';return;}}
    const sorted=[...assets].sort((a,b)=>a.health-b.health);
    el.innerHTML=sorted.map(a=>{{
      const cls=a.health>=85?'hc-g':a.health>=70?'hc-w':'hc-r';
      const color=a.health>=85?'#16a34a':a.health>=70?'#d97706':'#dc2626';
      return `<div class="h-item"><div class="h-top"><span class="h-name">${{a.name}}</span><span class="h-score" style="color:${{color}}">${{a.health}}</span></div><div class="h-track"><div class="h-fill ${{cls}}" style="width:${{a.health}}%"></div></div><div class="h-meta">${{a.site}} &middot; ${{a.atype}} &middot; ${{a.failures}} failures &middot; Avail ${{a.avail}}%</div></div>`;
    }}).join('');
  }}

  table(wos) {{
    const sorted=[...wos].sort((a,b)=>{{const av=a[this.sortCol],bv=b[this.sortCol];const c=av<bv?-1:av>bv?1:0;return this.sortDir==='asc'?c:-c;}});
    document.getElementById('tbl-sub').textContent='Showing '+sorted.length+' work order'+(sorted.length!==1?'s':'')+' — filtered view';
    const tbody=document.getElementById('tbl-body');
    if(!sorted.length){{tbody.innerHTML='<tr><td colspan="10" style="text-align:center;color:#9ca3af;padding:24px;">No work orders match filters.</td></tr>';return;}}
    tbody.innerHTML=sorted.map(w=>{{
      const pb='b-'+w.priority.toLowerCase();
      const sb=w.status==='Completed'?'b-completed':w.status==='In Progress'?'b-inprogress':'b-overdue';
      const tb=w.wo_type==='Planned'?'b-planned':'b-unplanned';
      return `<tr><td><strong>WO-${{w.id}}</strong></td><td>${{w.date}}</td><td>${{w.asset}}</td><td>${{w.atype}}</td><td>${{w.site}}</td><td><span class="badge ${{pb}}">${{w.priority}}</span></td><td><span class="badge ${{tb}}">${{w.wo_type}}</span></td><td><span class="badge ${{sb}}">${{w.status}}</span></td><td style="text-align:right">${{w.downtime.toFixed(1)}}</td><td>${{w.tech}}</td></tr>`;
    }}).join('');
  }}

  sort(col) {{
    if(this.sortCol===col) this.sortDir=this.sortDir==='asc'?'desc':'asc';
    else{{this.sortCol=col;this.sortDir=col==='date'?'desc':'asc';}}
    const{{site,type}}=this.filters();
    this.table(WOS.filter(w=>(site==='all'||w.site===site)&&(type==='all'||w.atype===type)));
  }}
}}

const dash = new Dashboard();
</script>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  MaintainX → M&R Dashboard Refresh")
    print("=" * 55)

    try:
        work_orders, assets, locations = fetch_data()
    except requests.exceptions.HTTPError as e:
        print(f"\nAPI error: {e}")
        print("Check that your TOKEN is valid and has not expired.")
        raise SystemExit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"\nConnection error: {e}")
        print("Check your internet connection and that api.getmaintainx.com is reachable.")
        raise SystemExit(1)

    print("\nComputing metrics...")
    monthly_list, asset_metrics, wo_rows = compute_metrics(work_orders, assets)
    print(f"  → {len(monthly_list)} months of trend data")
    print(f"  → {len(asset_metrics)} assets with computed metrics")
    print(f"  → {len(wo_rows)} work order rows for table")

    print("\nBuilding dashboard HTML...")
    html = build_html(monthly_list, asset_metrics, wo_rows)

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_FILE)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nDone! Dashboard saved to:\n  {out_path}")
    print("\nOpen it in any browser. Re-run this script any time to refresh the data.")
    print("=" * 55)