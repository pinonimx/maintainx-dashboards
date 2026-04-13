#!/usr/bin/env python3
"""
MaintainX Dashboard — CSV Watcher
===================================
Watches for a CSV export file and automatically regenerates wo_dashboard.html
whenever the file is updated. Just overwrite the CSV with a fresh export and
the dashboard rebuilds itself within a few seconds.

Usage:
    python maintainx_watcher.py

Expected CSV filename (save your MaintainX export as this name):
    maintainx_export.csv   (in the same folder as this script)

The dashboard HTML (wo_dashboard.html) will be regenerated automatically
whenever the CSV is created or updated.
"""

import sys
import time
import csv
import importlib
from pathlib import Path
from datetime import datetime

SCRIPT_DIR   = Path(__file__).parent
CSV_PATH     = SCRIPT_DIR / "maintainx_export.csv"
POLL_SECS    = 4   # how often to check for changes

OPEN_STATUSES = {"OPEN","IN_PROGRESS","INPROGRESS","IN PROGRESS","ON_HOLD","ON HOLD","ONHOLD"}

def load_csv(path):
    """Read the MaintainX CSV export and return normalised WO dicts."""
    wos = []
    skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("Is Parent?", "").strip().lower() == "true":
                skipped += 1
                continue
            if (row.get("Status") or "").upper().strip() not in OPEN_STATUSES:
                continue
            wos.append({
                "title":     row.get("Title", ""),
                "status":    (row.get("Status") or "OPEN").upper(),
                "priority":  (row.get("Priority") or "NONE").upper(),
                "type":      row.get("Work Type", ""),
                "dueDate":   row.get("Due date", ""),
                "asset":     {"name": row.get("Asset", "")},
                "location":  {"name": row.get("Location", "")},
                "assignees": [],
            })
    return wos, skipped

def rebuild(mxd):
    """Load CSV, regenerate dashboard, return True on success."""
    try:
        wos, skipped = load_csv(CSV_PATH)
    except Exception as e:
        print(f"  [!] Could not read CSV: {e}")
        return False

    importlib.reload(mxd)  # pick up any edits to the dashboard script
    lines_dict, non_line = mxd.compute_line_scores(wos)
    areas_dict = mxd.compute_area_scores(non_line)

    generated_at = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
    html = mxd.build_html(lines_dict, areas_dict, wos, generated_at)
    (SCRIPT_DIR / "wo_dashboard.html").write_text(html, encoding="utf-8")
    email_html = mxd.build_email_html(lines_dict, areas_dict, wos, generated_at)
    (SCRIPT_DIR / "wo_dashboard_email.html").write_text(email_html, encoding="utf-8")

    line_list = mxd.sorted_lines(lines_dict)
    top = line_list[0]["label"] if line_list else "—"
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Rebuilt — "
          f"{len(wos)} WOs (skipped {skipped} parents) · Top line: {top}")
    return True

def main():
    sys.path.insert(0, str(SCRIPT_DIR))
    import maintainx_dashboard as mxd

    print("MaintainX Dashboard Watcher")
    print(f"  Watching:  {CSV_PATH.name}")
    print(f"  Output:    wo_dashboard.html")
    print(f"  Interval:  every {POLL_SECS}s")
    print(f"\nSave your MaintainX export as '{CSV_PATH.name}' to trigger a rebuild.")
    print("Press Ctrl+C to stop.\n")

    last_mtime = None

    # Build immediately if the file already exists
    if CSV_PATH.exists():
        last_mtime = CSV_PATH.stat().st_mtime
        print(f"Found existing CSV — building initial dashboard...")
        rebuild(mxd)
    else:
        print(f"Waiting for {CSV_PATH.name} to appear...")

    while True:
        try:
            time.sleep(POLL_SECS)

            if not CSV_PATH.exists():
                continue

            mtime = CSV_PATH.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                print(f"Change detected — rebuilding...")
                rebuild(mxd)

        except KeyboardInterrupt:
            print("\nWatcher stopped.")
            break
        except Exception as e:
            print(f"  [!] Unexpected error: {e}")

if __name__ == "__main__":
    main()
