#!/usr/bin/env python3
"""
MaintainX Open Work Order Dashboard
=====================================
Fetches open work orders from MaintainX, groups them by production line,
applies a weighted priority score, and generates a ranked HTML dashboard.

Usage:
    python maintainx_dashboard.py

Configuration:
    Set MAINTAINX_API_KEY as an environment variable, or save your key to
    MaintainX_API_key.txt in the same folder as this script.

Output:
    wo_dashboard.html   Saved to the same folder as this script
"""

import os
import sys
import re
import json
import requests
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL  = "https://api.getmaintainx.com/v1"
PAGE_SIZE = 100

# Statuses treated as "open" (MaintainX uses these exact strings)
OPEN_STATUSES = {"OPEN", "IN_PROGRESS", "INPROGRESS", "IN PROGRESS", "ON_HOLD", "ON HOLD", "ONHOLD"}

# Weighted score per work order (per WO on a line)
PRIORITY_WEIGHTS = {
    "HIGH":   3.0,
    "MEDIUM": 2.0,
    "LOW":    1.0,
    "NONE":   0.5,
}

# Extra score added per total WO on a line (captures backlog pressure)
COUNT_WEIGHT = 0.5

# Additional multiplier applied to any WO that is past its due date
OVERDUE_MULTIPLIER = 1.25

# Multiplier applied to the priority weight based on work order type
# Reactive (something is broken) outweighs preventive work of the same priority
TYPE_MULTIPLIERS = {
    "REACTIVE":    1.5,
    "CORRECTIVE":  1.5,
    "UNPLANNED":   1.5,
    "EMERGENCY":   1.5,
    "PREVENTIVE":  1.0,
    "PREVENTATIVE":1.0,
    "PLANNED":     1.0,
    "PM":          1.0,
    "INSPECTION":  1.0,
    "ROUTINE":     1.0,
}

# Line equipment metadata
LINE_META = {
    1:  "CM-92 Extruder",
    2:  "CM-92 Extruder",
    3:  "TC-92 Extruder",
    4:  "TC-92 Extruder",
    5:  "TC-92 Extruder",
    6:  "TC-92 Extruder",
    7:  "TC-92 Extruder",
    8:  "TC-92 Extruder",
    9:  "Buss Kneader",
    10: "Buss Kneader",
}

SCRIPT_DIR  = Path(__file__).parent
OUTPUT_FILE = SCRIPT_DIR / "wo_dashboard.html"
KEY_FILE    = SCRIPT_DIR / "MaintainX_API_key.txt"

# ── API key resolution ─────────────────────────────────────────────────────────

def get_api_key():
    key = os.environ.get("MAINTAINX_API_KEY", "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
        if key:
            return key
    print("ERROR: No API key found.")
    print(f"  Option 1: export MAINTAINX_API_KEY='your_key'")
    print(f"  Option 2: save your key to {KEY_FILE}")
    sys.exit(1)

# ── API helpers ────────────────────────────────────────────────────────────────

def fetch_all_open_work_orders(api_key):
    """Page through /workorders using cursor-based pagination and return all open WOs."""
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })

    all_wos = []
    cursor = None

    # Filter server-side to open statuses only; expand location for name resolution;
    # show_upcoming includes WOs whose startDate is in the future (otherwise hidden by default)
    base_params = {
        "limit": PAGE_SIZE,
        "statuses": ["OPEN", "IN_PROGRESS", "ON_HOLD"],
        "expand": ["location"],
        "show_upcoming": "true",
    }

    while True:
        params = {**base_params}
        if cursor:
            params["cursor"] = cursor

        resp = session.get(
            f"{BASE_URL}/workorders",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()

        # MaintainX wraps results under "workOrders" list key
        wos = next((v for k, v in body.items() if isinstance(v, list)), None)
        if not wos:
            break

        for w in wos:
            # Skip parent WOs (containers for sub-WOs, not real tasks)
            if w.get("isParent", False):
                continue
            all_wos.append(w)

        # Advance cursor; stop only when API signals no more pages
        cursor = body.get("nextCursor")
        if not cursor:
            break

    return all_wos
# ── Line detection ─────────────────────────────────────────────────────────────

_LINE_RE = re.compile(r'\bline\s*[-_]?\s*(\d{1,2})\b', re.IGNORECASE)

def detect_line(wo):
    """
    Return the line number (int 1–10) for a work order, or None.

    MaintainX location names are 'Line 1' … 'Line 10' directly.
    Falls back to scanning asset name and title for the same pattern.
    """
    sources = []

    loc = wo.get("location") or {}
    if isinstance(loc, dict):
        sources.append(loc.get("name") or "")
    elif isinstance(loc, str):
        sources.append(loc)

    asset = wo.get("asset") or {}
    if isinstance(asset, dict):
        sources.append(asset.get("name") or "")
    elif isinstance(asset, list):
        sources.extend(a.get("name", "") for a in asset if isinstance(a, dict))

    sources.append(wo.get("title") or "")
    sources.append(wo.get("description") or "")

    for text in sources:
        m = _LINE_RE.search(str(text))
        if m:
            n = int(m.group(1))
            if 1 <= n <= 10:
                return n
    return None

# ── Scoring ────────────────────────────────────────────────────────────────────

def normalize_priority(raw):
    p = (raw or "NONE").upper().strip()
    return p if p in PRIORITY_WEIGHTS else "NONE"

def _make_bucket(label):
    return {"label": label, "wos": [], "counts": {p: 0 for p in PRIORITY_WEIGHTS}, "score": 0.0}

_NOW_UTC = datetime.now(timezone.utc)

def _is_overdue(wo):
    raw = wo.get("dueDate") or wo.get("due_date") or ""
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        return dt < _NOW_UTC
    except Exception:
        return False

def _add_wo_to_bucket(bucket, wo):
    priority   = normalize_priority(wo.get("priority"))
    wo_type    = (wo.get("type") or wo.get("workOrderType") or "").upper().strip()
    multiplier = TYPE_MULTIPLIERS.get(wo_type, 1.0)
    if _is_overdue(wo):
        multiplier *= OVERDUE_MULTIPLIER
    bucket["wos"].append(wo)
    bucket["counts"][priority] += 1
    bucket["score"] += PRIORITY_WEIGHTS[priority] * multiplier

def _finalize_scores(d):
    for b in d.values():
        b["score"] += COUNT_WEIGHT * len(b["wos"])
        b["score"] = round(b["score"], 1)

def get_location_name(wo):
    """Return the raw location name string for a WO, or 'No Location' if absent."""
    loc = wo.get("location") or {}
    raw  = loc.get("name") if isinstance(loc, dict) else (str(loc) if loc else "")
    name = (raw or "").strip()
    return name or "No Location"

def compute_line_scores(work_orders):
    """Group WOs that belong to Lines 1–10; return the rest as non-line WOs."""
    lines = {}
    non_line = []
    for wo in work_orders:
        line = detect_line(wo)
        if line is not None:
            if line not in lines:
                lines[line] = _make_bucket(f"Line {line}")
                lines[line]["line"] = line
            _add_wo_to_bucket(lines[line], wo)
        else:
            non_line.append(wo)
    _finalize_scores(lines)
    return lines, non_line

def compute_area_scores(non_line_wos):
    """Group non-line WOs by their location name and score them."""
    areas = {}
    for wo in non_line_wos:
        loc = get_location_name(wo)
        if loc not in areas:
            areas[loc] = _make_bucket(loc)
        _add_wo_to_bucket(areas[loc], wo)
    _finalize_scores(areas)
    return areas

def sorted_lines(lines_dict):
    return sorted(lines_dict.values(), key=lambda x: x["score"], reverse=True)

def sorted_areas(areas_dict):
    return sorted(areas_dict.values(), key=lambda x: x["score"], reverse=True)

# ── HTML helpers ───────────────────────────────────────────────────────────────

P_STYLE = {
    "HIGH":   ("bg:#dc2626;color:#fff",  "#dc2626"),
    "MEDIUM": ("bg:#d97706;color:#fff",  "#d97706"),
    "LOW":    ("bg:#2563eb;color:#fff",  "#2563eb"),
    "NONE":   ("bg:#6b7280;color:#fff",  "#6b7280"),
}

S_STYLE = {
    "OPEN":        ("Open",        "#16a34a", "#dcfce7"),
    "IN_PROGRESS": ("In Progress", "#2563eb", "#dbeafe"),
    "INPROGRESS":  ("In Progress", "#2563eb", "#dbeafe"),
    "IN PROGRESS": ("In Progress", "#2563eb", "#dbeafe"),
    "ON_HOLD":     ("On Hold",     "#9333ea", "#f3e8ff"),
    "ON HOLD":     ("On Hold",     "#9333ea", "#f3e8ff"),
    "ONHOLD":      ("On Hold",     "#9333ea", "#f3e8ff"),
}

def p_badge(priority):
    p = normalize_priority(priority)
    _, color = P_STYLE.get(p, P_STYLE["NONE"])
    return (
        f'<span style="background:{color};color:#fff;padding:2px 9px;'
        f'border-radius:9999px;font-size:.72rem;font-weight:700;letter-spacing:.04em;">'
        f'{p}</span>'
    )

def s_badge(status):
    s = (status or "OPEN").upper().strip()
    label, fg, bg = S_STYLE.get(s, ("Open", "#16a34a", "#dcfce7"))
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 9px;'
        f'border-radius:9999px;font-size:.72rem;font-weight:600;">{label}</span>'
    )

def score_bar_html(score, max_score):
    pct = min(100, round(score / max_score * 100)) if max_score > 0 else 0
    color = "#dc2626" if pct > 66 else "#d97706" if pct > 33 else "#16a34a"
    return (
        f'<div style="display:flex;align-items:center;gap:6px;">'
        f'<div style="flex:1;height:8px;background:#e5e7eb;border-radius:4px;overflow:hidden;">'
        f'<div style="width:{pct}%;height:100%;background:{color};border-radius:4px;transition:width .4s;"></div>'
        f'</div>'
        f'<span style="min-width:2.8rem;text-align:right;font-weight:700;color:{color};">{score}</span>'
        f'</div>'
    )

def due_date_html(wo):
    raw = wo.get("dueDate") or wo.get("due_date") or ""
    if not raw:
        return '<span style="color:#9ca3af;">—</span>'
    try:
        if isinstance(raw, str):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            dt = raw
        now = datetime.now(timezone.utc)
        overdue = dt.replace(tzinfo=timezone.utc) < now if dt.tzinfo is None else dt < now
        color = "#dc2626" if overdue else "#374151"
        flag  = " ⚠" if overdue else ""
        return f'<span style="color:{color};">{dt.strftime("%b %d, %Y")}{flag}</span>'
    except Exception:
        return str(raw)[:10]

# ── Build HTML ─────────────────────────────────────────────────────────────────

REACTIVE_TYPES   = {"REACTIVE", "CORRECTIVE", "UNPLANNED", "EMERGENCY"}
PREVENTIVE_TYPES = {"PREVENTIVE", "PREVENTATIVE", "PLANNED", "PM", "INSPECTION", "ROUTINE"}

def wo_type_str(wo):
    return (wo.get("type") or wo.get("workOrderType") or "").upper().strip()

def build_html(lines_dict, areas_dict, all_wos, generated_at):
    line_list    = sorted_lines(lines_dict)
    area_list    = sorted_areas(areas_dict)
    all_buckets  = line_list + area_list
    total        = len(all_wos)
    max_score    = max((b["score"] for b in all_buckets), default=1)
    n_high       = sum(1 for w in all_wos if normalize_priority(w.get("priority")) == "HIGH")
    n_reactive   = sum(1 for w in all_wos if wo_type_str(w) in REACTIVE_TYPES)
    n_preventive = sum(1 for w in all_wos if wo_type_str(w) in PREVENTIVE_TYPES)
    n_overdue    = 0
    now_utc      = datetime.now(timezone.utc)
    for w in all_wos:
        raw = w.get("dueDate") or w.get("due_date") or ""
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z","+00:00"))
                if dt.replace(tzinfo=timezone.utc) < now_utc if dt.tzinfo is None else dt < now_utc:
                    n_overdue += 1
            except Exception:
                pass

    # Rank → color: red/orange/amber for top 3, blue for mid, gray for tail
    def rank_color(rank):
        if rank == 1: return "#dc2626"
        if rank == 2: return "#ea580c"
        if rank == 3: return "#d97706"
        if rank <= 6: return "#2563eb"
        return "#6b7280"

    def make_line_card(label, subtitle, rank, score, counts, cnt):
        rc = rank_color(rank)
        return f"""
        <div onclick="filterToLine('{label}')"
             style="background:#fff;border:1px solid #e5e7eb;
                    border-top:5px solid {rc};border-radius:10px;
                    padding:14px 14px 12px;cursor:pointer;
                    transition:box-shadow .15s,transform .15s;
                    box-shadow:0 1px 4px rgba(0,0,0,.08);"
             onmouseover="this.style.boxShadow='0 6px 18px rgba(0,0,0,.14)';this.style.transform='translateY(-2px)'"
             onmouseout="this.style.boxShadow='0 1px 4px rgba(0,0,0,.08)';this.style.transform=''">
          <div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:10px;">
            <div style="font-size:2.4rem;font-weight:900;color:{rc};line-height:1;
                        min-width:2.8rem;text-align:center;letter-spacing:-.04em;">
              {rank}
            </div>
            <div style="flex:1;min-width:0;padding-top:2px;">
              <div style="font-size:1.05rem;font-weight:800;color:#111;
                          white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{label}</div>
              <div style="font-size:.73rem;color:#6b7280;margin-top:1px;">{subtitle}</div>
            </div>
          </div>
          {score_bar_html(score, max_score)}
          <div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:10px;align-items:center;">
            <span style="background:#fee2e2;color:#dc2626;padding:2px 6px;border-radius:9999px;font-size:.7rem;font-weight:700;">H:{counts["HIGH"]}</span>
            <span style="background:#fef3c7;color:#d97706;padding:2px 6px;border-radius:9999px;font-size:.7rem;font-weight:700;">M:{counts["MEDIUM"]}</span>
            <span style="background:#dbeafe;color:#2563eb;padding:2px 6px;border-radius:9999px;font-size:.7rem;font-weight:700;">L:{counts["LOW"]}</span>
            <span style="background:#f3f4f6;color:#6b7280;padding:2px 6px;border-radius:9999px;font-size:.7rem;font-weight:700;">—:{counts["NONE"]}</span>
            <span style="margin-left:auto;font-size:.78rem;color:#374151;font-weight:600;">{cnt} WO{'s' if cnt!=1 else ''}</span>
          </div>
        </div>"""

    def make_area_card(label, rank, score, counts, cnt):
        rc = ["#059669","#0891b2","#7c3aed"][rank-1] if rank <= 3 else "#6b7280"
        return f"""
        <div onclick="filterToLine('{label}')"
             style="background:#f8fafc;border:1px solid #e2e8f0;
                    border-left:4px solid {rc};border-radius:8px;
                    padding:10px 12px;cursor:pointer;
                    transition:box-shadow .15s;box-shadow:0 1px 2px rgba(0,0,0,.05);"
             onmouseover="this.style.boxShadow='0 4px 12px rgba(0,0,0,.1)'"
             onmouseout="this.style.boxShadow='0 1px 2px rgba(0,0,0,.05)'">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
            <div style="font-size:1.3rem;font-weight:800;color:{rc};min-width:1.8rem;">{rank}</div>
            <div style="font-size:.9rem;font-weight:700;color:#374151;
                        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{label}</div>
            <span style="margin-left:auto;font-size:.75rem;color:#6b7280;white-space:nowrap;">{cnt} WOs</span>
          </div>
          {score_bar_html(score, max_score)}
          <div style="display:flex;gap:4px;margin-top:7px;">
            <span style="background:#fee2e2;color:#dc2626;padding:1px 5px;border-radius:9999px;font-size:.67rem;font-weight:700;">H:{counts["HIGH"]}</span>
            <span style="background:#fef3c7;color:#d97706;padding:1px 5px;border-radius:9999px;font-size:.67rem;font-weight:700;">M:{counts["MEDIUM"]}</span>
            <span style="background:#dbeafe;color:#2563eb;padding:1px 5px;border-radius:9999px;font-size:.67rem;font-weight:700;">L:{counts["LOW"]}</span>
          </div>
        </div>"""

    # ── Line summary cards ─────────────────────────────────────────────────────
    cards = ""
    for rank, b in enumerate(line_list, 1):
        equip = LINE_META.get(b["line"], "")
        cards += make_line_card(b["label"], equip, rank, b["score"], b["counts"], len(b["wos"]))

    # ── Area summary cards ─────────────────────────────────────────────────────
    area_cards = ""
    for rank, b in enumerate(area_list, 1):
        area_cards += make_area_card(b["label"], rank, b["score"], b["counts"], len(b["wos"]))

    # ── WO table rows ──────────────────────────────────────────────────────────
    p_order = {"HIGH":0,"MEDIUM":1,"LOW":2,"NONE":3}
    rows = ""
    for b in all_buckets:
        label = b["label"]
        wos_sorted = sorted(
            b["wos"],
            key=lambda w: (p_order.get(normalize_priority(w.get("priority")),4),
                           str(w.get("dueDate") or "9999"))
        )
        for wo in wos_sorted:
            title    = wo.get("title","(No title)")
            priority = normalize_priority(wo.get("priority"))
            status   = (wo.get("status") or "OPEN").upper().strip()
            wo_type  = wo.get("type") or wo.get("workOrderType") or "—"

            asset = wo.get("asset") or {}
            if isinstance(asset, dict):
                asset_name = asset.get("name","") or asset.get("title","") or "—"
            elif isinstance(asset, list):
                asset_name = " / ".join(a.get("name","") for a in asset if isinstance(a,dict)) or "—"
            else:
                asset_name = "—"

            assignees = wo.get("assignees") or []
            if isinstance(assignees, list):
                assignee_str = ", ".join(
                    a.get("name") or a.get("firstName","") for a in assignees
                ) or "—"
            else:
                assignee_str = "—"

            rows += f"""
            <tr data-line="{label}" data-priority="{priority}">
              <td style="padding:9px 12px;font-weight:700;white-space:nowrap;">{label}</td>
              <td style="padding:9px 12px;">{p_badge(priority)}</td>
              <td style="padding:9px 12px;max-width:300px;">{title}</td>
              <td style="padding:9px 12px;">{s_badge(status)}</td>
              <td style="padding:9px 12px;color:#6b7280;font-size:.83rem;">{asset_name}</td>
              <td style="padding:9px 12px;color:#6b7280;font-size:.83rem;white-space:nowrap;">{wo_type}</td>
              <td style="padding:9px 12px;color:#374151;font-size:.83rem;">{assignee_str}</td>
              <td style="padding:9px 12px;font-size:.83rem;">{due_date_html(wo)}</td>
            </tr>"""

    # ── Filter options for both lines and areas ────────────────────────────────
    line_opts = (
        '<optgroup label="Production Lines">\n' +
        "\n".join(f'<option value="{b["label"]}">{b["label"]} (#{r})</option>'
                  for r, b in enumerate(line_list, 1)) +
        '\n</optgroup>\n<optgroup label="Facility / Areas">\n' +
        "\n".join(f'<option value="{b["label"]}">{b["label"]} (#{r})</option>'
                  for r, b in enumerate(area_list, 1)) +
        '\n</optgroup>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Open WO Dashboard — {generated_at}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      background:#f0f4f8;color:#111827;min-height:100vh}}
.hdr{{background:linear-gradient(135deg,#0f2d52 0%,#1d4ed8 100%);
       color:#fff;padding:22px 32px 18px}}
.hdr h1{{font-size:1.5rem;font-weight:800;letter-spacing:-.02em}}
.hdr p{{font-size:.82rem;opacity:.7;margin-top:3px}}
.kpis{{display:flex;gap:0;background:#fff;border-bottom:1px solid #e5e7eb}}
.kpi{{flex:1;padding:14px 20px;text-align:center;border-right:1px solid #f0f4f8}}
.kpi:last-child{{border-right:none}}
.kpi .val{{font-size:1.7rem;font-weight:800;color:#0f2d52;line-height:1}}
.kpi .lbl{{font-size:.68rem;color:#9ca3af;text-transform:uppercase;letter-spacing:.07em;margin-top:3px}}
.body{{padding:24px 32px}}
.sec{{font-size:.8rem;font-weight:800;color:#6b7280;text-transform:uppercase;
       letter-spacing:.07em;margin-bottom:12px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(185px,1fr));
         gap:10px;margin-bottom:28px}}
.fbar{{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap;align-items:center}}
.fbar input,.fbar select{{
  padding:7px 11px;border:1px solid #d1d5db;border-radius:8px;
  font-size:.85rem;background:#fff;color:#111}}
.fbar input{{flex:1;min-width:160px}}
.fbar button{{padding:7px 14px;border:none;background:#1d4ed8;color:#fff;
               border-radius:8px;cursor:pointer;font-size:.82rem;font-weight:600}}
.fbar button:hover{{background:#1e40af}}
.badge-row{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;background:#fff;
        border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;
        box-shadow:0 1px 3px rgba(0,0,0,.06)}}
thead{{background:#f8fafc}}
th{{padding:9px 12px;text-align:left;font-size:.72rem;font-weight:700;
     color:#6b7280;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap;
     border-bottom:1px solid #e5e7eb}}
tbody tr{{border-bottom:1px solid #f3f4f6}}
tbody tr:last-child{{border-bottom:none}}
tbody tr:hover td{{background:#f8fafc}}
.note{{font-size:.75rem;color:#9ca3af;margin-top:10px}}
@media(max-width:640px){{
  .body{{padding:16px}}
  .kpis{{flex-wrap:wrap}}
  .kpi{{min-width:50%;border-bottom:1px solid #f0f4f8}}
}}
</style>
</head>
<body>
<div class="hdr">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
    <h1>Open Work Order Dashboard</h1>
    <span style="font-size:.75rem;opacity:.55;">{generated_at}</span>
  </div>
</div>

<div class="kpis">
  <div class="kpi"><div class="val">{total}</div><div class="lbl">Open WOs</div></div>
  <div class="kpi"><div class="val" style="color:#dc2626">{n_reactive}</div><div class="lbl">Reactive</div></div>
  <div class="kpi"><div class="val" style="color:#16a34a">{n_preventive}</div><div class="lbl">Preventive</div></div>
  <div class="kpi"><div class="val" style="color:#dc2626">{n_overdue}</div><div class="lbl">Overdue</div></div>
</div>

<div class="body">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
    <div class="sec" style="margin-bottom:0;">Line Priority Ranking</div>
    <span style="font-size:.72rem;color:#9ca3af;cursor:help;position:relative;"
          onmouseenter="document.getElementById('score-tip').style.display='block'"
          onmouseleave="document.getElementById('score-tip').style.display='none'">
      How scores work ⓘ
      <span id="score-tip" style="display:none;position:absolute;right:0;top:1.6em;
            background:#1e293b;color:#f1f5f9;border:1px solid #334155;
            border-radius:8px;padding:10px 14px;font-size:.8rem;white-space:nowrap;
            z-index:100;box-shadow:0 4px 12px rgba(0,0,0,.4);line-height:1.7;">
        <strong style="color:#fff;display:block;margin-bottom:4px;">Score per line =</strong>
        🔴 High &nbsp;&times;&nbsp;3<br>
        🟡 Medium &nbsp;&times;&nbsp;2<br>
        🔵 Low &nbsp;&times;&nbsp;1<br>
        ⚫ None &nbsp;&times;&nbsp;0.5<br>
        <span style="border-top:1px solid #334155;display:block;margin-top:6px;padding-top:6px;">
        + 0.5 &times; total WO count
        </span>
        <span style="border-top:1px solid #334155;display:block;margin-top:6px;padding-top:6px;">
        🔧 Reactive &nbsp;&times;&nbsp;1.5 multiplier<br>
        🗓 Preventive &nbsp;&times;&nbsp;1.0 multiplier<br>
        ⚠ Overdue &nbsp;&times;&nbsp;1.25 (stacks)
        </span>
      </span>
    </span>
  </div>
  <div class="cards">{cards}</div>

  <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;cursor:pointer;"
       onclick="toggleSection('area-section','area-icon')">
    <div class="sec" style="margin-bottom:0;">Facility / Area Ranking</div>
    <span id="area-icon"
          style="font-size:.75rem;font-weight:700;color:#6b7280;
                 background:#e5e7eb;padding:2px 10px;border-radius:9999px;
                 user-select:none;">Show ▾</span>
  </div>
  <div id="area-section" style="display:none;">
    <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr));">{area_cards}</div>
  </div>

  <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;cursor:pointer;"
       onclick="toggleTable()">
    <div class="sec" style="margin-bottom:0;">All Open Work Orders</div>
    <span id="toggle-icon"
          style="font-size:.75rem;font-weight:700;color:#6b7280;
                 background:#e5e7eb;padding:2px 10px;border-radius:9999px;
                 user-select:none;">Show ▾</span>
  </div>

  <div id="wo-section" style="display:none;">
    <div class="fbar">
      <input type="text" id="srch" placeholder="Search title, asset, assignee..." oninput="filt()">
      <select id="pFilt" onchange="filt()">
        <option value="">All Priorities</option>
        <option value="HIGH">High</option>
        <option value="MEDIUM">Medium</option>
        <option value="LOW">Low</option>
        <option value="NONE">None</option>
      </select>
      <select id="lFilt" onchange="filt()">
        <option value="">All Lines</option>
        {line_opts}
      </select>
      <button onclick="clr()">Clear</button>
    </div>

    <table>
      <thead>
        <tr>
          <th>Line</th><th>Priority</th><th>Work Order</th><th>Status</th>
          <th>Asset</th><th>Type</th><th>Assignee</th><th>Due Date</th>
        </tr>
      </thead>
      <tbody id="tbody">{rows}</tbody>
    </table>

    <p class="note">⚠ = overdue due date</p>
  </div>
</div>

<script>
function toggleSection(secId, iconId, forceOpen){{
  const sec  = document.getElementById(secId);
  const icon = document.getElementById(iconId);
  const open = forceOpen !== undefined ? forceOpen : sec.style.display === 'none';
  sec.style.display = open ? '' : 'none';
  icon.textContent  = open ? 'Hide ▴' : 'Show ▾';
}}
function toggleTable(forceOpen){{ toggleSection('wo-section','toggle-icon',forceOpen); }}
function filt(){{
  const s = document.getElementById('srch').value.toLowerCase();
  const p = document.getElementById('pFilt').value;
  const l = document.getElementById('lFilt').value;
  document.querySelectorAll('#tbody tr').forEach(r=>{{
    const txt  = r.textContent.toLowerCase();
    const okS  = !s || txt.includes(s);
    const okP  = !p || r.dataset.priority === p;
    const okL  = !l || r.dataset.line === l;
    r.style.display = (okS && okP && okL) ? '' : 'none';
  }});
}}
function filterToLine(lbl){{
  toggleTable(true);
  document.getElementById('lFilt').value = lbl;
  filt();
  document.getElementById('wo-section').scrollIntoView({{behavior:'smooth',block:'start'}});
}}
function clr(){{
  document.getElementById('srch').value='';
  document.getElementById('pFilt').value='';
  document.getElementById('lFilt').value='';
  filt();
}}
</script>
</body>
</html>"""

# ── Email-safe HTML ────────────────────────────────────────────────────────────

EMAIL_OUTPUT_FILE = SCRIPT_DIR / "wo_dashboard_email.html"

def build_email_html(lines_dict, areas_dict, all_wos, generated_at):
    """
    Generate a static, Outlook-compatible HTML snippet.
    Table-based layout, fully inline styles, no JavaScript.
    Intended to be copy-pasted into an Outlook email body.
    """
    line_list  = sorted_lines(lines_dict)
    area_list  = sorted_areas(areas_dict)
    total      = len(all_wos)
    n_reactive = sum(1 for w in all_wos if wo_type_str(w) in REACTIVE_TYPES)
    n_prev     = sum(1 for w in all_wos if wo_type_str(w) in PREVENTIVE_TYPES)
    n_overdue  = 0
    now_utc    = datetime.now(timezone.utc)
    for w in all_wos:
        raw = w.get("dueDate") or w.get("due_date") or ""
        if raw:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if dt.replace(tzinfo=timezone.utc) < now_utc if dt.tzinfo is None else dt < now_utc:
                    n_overdue += 1
            except Exception:
                pass

    max_score = max((b["score"] for b in line_list), default=1)

    def rank_color(rank):
        if rank == 1: return "#dc2626"
        if rank == 2: return "#ea580c"
        if rank == 3: return "#d97706"
        if rank <= 6: return "#2563eb"
        return "#6b7280"

    def score_bar_table(score, max_score, width=140):
        pct  = min(100, round(score / max_score * 100)) if max_score else 0
        fill = round(width * pct / 100)
        rest = width - fill
        color = "#dc2626" if pct > 66 else "#d97706" if pct > 33 else "#16a34a"
        return (
            f'<table width="{width}" cellpadding="0" cellspacing="0" border="0">'
            f'<tr>'
            f'<td width="{fill}" bgcolor="{color}" height="7" style="font-size:1px;line-height:1px;">&nbsp;</td>'
            f'<td bgcolor="#e5e7eb" height="7" style="font-size:1px;line-height:1px;">&nbsp;</td>'
            f'</tr></table>'
        )

    # ── KPI strip ──────────────────────────────────────────────────────────────
    kpi_style = "padding:12px 20px;text-align:center;border-right:1px solid #f0f4f8;"
    val_style = "font-size:26px;font-weight:800;font-family:Arial,sans-serif;line-height:1.1;"
    lbl_style = "font-size:10px;color:#9ca3af;font-family:Arial,sans-serif;text-transform:uppercase;letter-spacing:1px;"

    kpis = (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:#ffffff;border-bottom:2px solid #e5e7eb;">'
        f'<tr>'
        f'<td style="{kpi_style}"><div style="{val_style}color:#0f2d52;">{total}</div>'
        f'<div style="{lbl_style}">Open WOs</div></td>'
        f'<td style="{kpi_style}"><div style="{val_style}color:#dc2626;">{n_reactive}</div>'
        f'<div style="{lbl_style}">Reactive</div></td>'
        f'<td style="{kpi_style}"><div style="{val_style}color:#16a34a;">{n_prev}</div>'
        f'<div style="{lbl_style}">Preventive</div></td>'
        f'<td style="{kpi_style}border-right:none;"><div style="{val_style}color:#dc2626;">{n_overdue}</div>'
        f'<div style="{lbl_style}">Overdue</div></td>'
        f'</tr></table>'
    )

    # ── Line ranking rows ──────────────────────────────────────────────────────
    line_rows = ""
    for rank, b in enumerate(line_list, 1):
        rc     = rank_color(rank)
        c      = b["counts"]
        cnt    = len(b["wos"])
        equip  = LINE_META.get(b.get("line", 0), "")
        bg     = "#fff8f8" if rank == 1 else "#ffffff"

        line_rows += (
            f'<tr style="background:{bg};">'
            # Rank number cell with left color bar
            f'<td width="52" style="border-left:5px solid {rc};padding:10px 8px 10px 12px;'
            f'vertical-align:middle;">'
            f'<span style="font-size:28px;font-weight:900;color:{rc};font-family:Arial,sans-serif;'
            f'line-height:1;">{rank}</span></td>'
            # Line name + equipment
            f'<td style="padding:10px 12px;vertical-align:middle;">'
            f'<div style="font-size:14px;font-weight:700;color:#111827;font-family:Arial,sans-serif;">'
            f'{b["label"]}</div>'
            f'<div style="font-size:11px;color:#6b7280;font-family:Arial,sans-serif;">{equip}</div>'
            f'</td>'
            # WO count
            f'<td style="padding:10px 12px;vertical-align:middle;text-align:right;white-space:nowrap;">'
            f'<span style="font-size:12px;font-weight:600;color:#374151;font-family:Arial,sans-serif;">'
            f'{cnt} WOs</span></td>'
            # Score bar
            f'<td style="padding:10px 12px;vertical-align:middle;">'
            f'{score_bar_table(b["score"], max_score)}'
            f'<div style="font-size:10px;color:#9ca3af;font-family:Arial,sans-serif;margin-top:2px;">'
            f'score: {b["score"]}</div></td>'
            f'</tr>'
            # Thin separator
            f'<tr><td colspan="4" style="padding:0;"><div style="height:1px;background:#f3f4f6;"></div></td></tr>'
        )

    ranking_table = (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">'
        f'{line_rows}</table>'
    )

    # ── Full email HTML ────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Open WO Dashboard — {generated_at}</title>
</head>
<body style="margin:0;padding:0;background:#f0f4f8;">
<table width="680" cellpadding="0" cellspacing="0" border="0"
       style="margin:20px auto;font-family:Arial,sans-serif;">

  <!-- Header accent bar -->
  <tr>
    <td height="6" bgcolor="#1d4ed8"
        style="font-size:1px;line-height:1px;border-radius:8px 8px 0 0;">&nbsp;</td>
  </tr>

  <!-- Header title -->
  <tr>
    <td style="background:#ffffff;padding:14px 24px 12px;
               border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
        <td><span style="font-size:17px;font-weight:800;color:#0f2d52;
                         font-family:Arial,sans-serif;">Open Work Order Dashboard</span></td>
        <td align="right" style="vertical-align:middle;">
          <span style="font-size:11px;color:#9ca3af;font-family:Arial,sans-serif;">{generated_at}</span>
        </td>
      </tr></table>
    </td>
  </tr>

  <!-- KPI strip -->
  <tr>
    <td style="padding:0;border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb;">
      {kpis}
    </td>
  </tr>

  <!-- Section label -->
  <tr>
    <td style="padding:18px 0 10px 0;">
      <span style="font-size:11px;font-weight:800;color:#6b7280;font-family:Arial,sans-serif;
                   text-transform:uppercase;letter-spacing:1.5px;">Line Priority Ranking</span>
    </td>
  </tr>

  <!-- Ranking table -->
  <tr><td>{ranking_table}</td></tr>

  <!-- Footer note -->
  <tr>
    <td style="padding:12px 0 4px 0;">
      <span style="font-size:10px;color:#9ca3af;font-family:Arial,sans-serif;">
        Ranked by weighted score: High ×3, Medium ×2, Low ×1, None ×0.5 · Reactive ×1.5,
        Preventive ×1.0 · Overdue ×1.25 (stacks) · +0.5 per total WO count.
        See attached dashboard for full detail and filtering.
      </span>
    </td>
  </tr>

</table>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    api_key = get_api_key()

    print("Fetching open work orders from MaintainX...")
    try:
        all_wos = fetch_all_open_work_orders(api_key)
    except requests.exceptions.HTTPError as e:
        print(f"ERROR: API request failed — {e}")
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"ERROR: Could not connect to MaintainX API — {e}")
        sys.exit(1)

    print(f"  Retrieved {len(all_wos)} open work order(s).")

    lines_dict, non_line = compute_line_scores(all_wos)
    areas_dict = compute_area_scores(non_line)
    line_list  = sorted_lines(lines_dict)
    area_list  = sorted_areas(areas_dict)

    print(f"\n{'Rank':<5} {'Line':<14} {'Score':<8} {'WOs':<6} {'H/M/L'}")
    print("─" * 48)
    for rank, b in enumerate(line_list, 1):
        c = b["counts"]
        print(f"#{rank:<4} {b['label']:<14} {b['score']:<8} {len(b['wos']):<6} {c['HIGH']}/{c['MEDIUM']}/{c['LOW']}")

    print(f"\n{'Rank':<5} {'Area':<22} {'Score':<8} {'WOs':<6} {'H/M/L'}")
    print("─" * 54)
    for rank, b in enumerate(area_list, 1):
        c = b["counts"]
        print(f"#{rank:<4} {b['label']:<22} {b['score']:<8} {len(b['wos']):<6} {c['HIGH']}/{c['MEDIUM']}/{c['LOW']}")

    generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
    html = build_html(lines_dict, areas_dict, all_wos, generated_at)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"\nDashboard saved      → {OUTPUT_FILE}")

    email_html = build_email_html(lines_dict, areas_dict, all_wos, generated_at)
    EMAIL_OUTPUT_FILE.write_text(email_html, encoding="utf-8")
    print(f"Email version saved  → {EMAIL_OUTPUT_FILE}")


if __name__ == "__main__":
    main()