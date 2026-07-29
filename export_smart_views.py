#!/usr/bin/env python3
"""
Export every Close smart view (saved search) with its full s_query filter definition,
then report which ones reference the Lane 2 fields we're about to change.

Why: the Close MCP tools return smart-view metadata only — no filter definitions.
This is the one gap blocking Phase 3 of the Lane 2 bucket standardization.
(Part 12, step 1 of Jess's build guide.)

READ-ONLY. Issues GET requests only; never writes to Close.

Usage:
    export CLOSE_API_KEY=api_xxxxx
    python3 export_smart_views.py

Outputs:
    smart_views_full.json   every view with its s_query
    smart_views_index.csv   id, name, owner, updated, what it references
    ...plus a markdown report to stdout and to the GitHub Actions job summary.
"""

import csv
import json
import os
import sys
from collections import defaultdict

import requests

API_KEY = os.environ.get("CLOSE_API_KEY")
if not API_KEY:
    sys.exit("Set CLOSE_API_KEY first.")

BASE = "https://api.close.com/api/v1"
AUTH = (API_KEY, "")
PAGE = 100

# --- what we're looking for -------------------------------------------------

FIELDS = {
    "cf_Q1hRv8It46xsAEmpv4PRKdI1y0sPJnrnQrgRbIlF8uL": "Lane 2 Handraiser",
    "cf_UD9Hm3dpLGtcUd37tX8Y9GAK1Lhc3BdtDX769ffFvyB": "Sales Team Lane",
    "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd": "Lead Owner",
    "cf_LFdYEQ6bsgp49YjZzefypDmdVx8iwuakWDSLPLpVrBq": "First Sales Call Booked Date",
    "cf_vz6kNiu4ItFxRA8Y9HKlWIoQMq3TsdaQqKekQ2YuxVk": "Reactivation - Setter Name",
    "cf_xqDQE8fkPsWa0RNEve7hcaxKblCe6489XeZGRDzyPdX": "Funnel Name DEAL (Opp)",
}

# The four Handraiser values that carry *state* rather than source.
# Any view filtering on these breaks silently if the value is removed.
STATE_VALUES = [
    "No Activity / Past 14 Days",
    "No Activity / Past 30 Days",
    "30 Day Aged Deals",
    "Prior Day Lost Deals",
]

# Load-bearing: these two views ARE the reassignment logic (fetched by ID).
PROTECTED_PREFIXES = {
    "save_usGcGnOy": "L2 Handoff: 14-days No Comms",
    "save_vUj7qzI7": "L2 Handoff: 30 Days since Booking",
}

# William Nowak — his 8 standing views may already be the Setter lane.
WILLIAM = "user_ZNKG1S9eI71qxhSozBK4jskTVtJqXzfNCPWqmADRR9F"


def fetch_all():
    """Page through every saved search."""
    views, skip = [], 0
    while True:
        r = requests.get(
            f"{BASE}/saved_search/",
            auth=AUTH,
            params={"_skip": skip, "_limit": PAGE},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        batch = body.get("data", [])
        views.extend(batch)
        print(f"  fetched {len(views)}...", file=sys.stderr)
        if not body.get("has_more") or not batch:
            break
        skip += PAGE
    return views


def scan(view):
    """Return what this view references."""
    blob = json.dumps(view.get("s_query") or view.get("query") or "")
    return {
        "fields": [lbl for cf, lbl in FIELDS.items() if cf in blob],
        "state_values": [v for v in STATE_VALUES if v in blob],
    }


def main():
    print("Fetching smart views...", file=sys.stderr)
    views = fetch_all()

    with open("smart_views_full.json", "w") as f:
        json.dump(views, f, indent=2)

    by_field = defaultdict(list)
    by_state_value = defaultdict(list)
    protected_found, william_views, rows = [], [], []

    for v in views:
        vid, name = v.get("id", ""), v.get("name", "")
        hits = scan(v)

        for label in hits["fields"]:
            by_field[label].append((name, vid))
        for val in hits["state_values"]:
            by_state_value[val].append((name, vid))

        if any(vid.startswith(p) for p in PROTECTED_PREFIXES):
            protected_found.append((name, vid, bool(hits["fields"] or hits["state_values"])))
        if WILLIAM in (v.get("user_id"), v.get("owner_id")):
            william_views.append((name, vid))

        rows.append({
            "id": vid,
            "name": name,
            "owner_id": v.get("owner_id") or v.get("user_id", ""),
            "updated_at": v.get("date_updated") or v.get("updated_at", ""),
            "fields_referenced": "; ".join(hits["fields"]),
            "state_values_referenced": "; ".join(hits["state_values"]),
        })

    if rows:
        with open("smart_views_index.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    # ---- build the report ----
    md = []
    a = md.append

    a(f"# Close Smart View Audit — Lane 2\n")
    a(f"**{len(views)} smart views** exported with full filter definitions.\n")

    flagged = sum(1 for r in rows if r["fields_referenced"] or r["state_values_referenced"])
    a(f"- {flagged} reference at least one field we're changing")
    a(f"- {len(by_state_value)} of 4 Handraiser state-values are referenced by a view")
    a(f"- {len(william_views)} views owned by William Nowak\n")

    a("## ⚠️ Views filtering on Handraiser *state* values\n")
    a("These break silently if the value is removed. Highest-priority review.\n")
    if not by_state_value:
        a("**None found — those four values are safe to retire.**\n")
    for val, hits in sorted(by_state_value.items()):
        a(f"\n**`{val}`** — {len(hits)} view(s)\n")
        for name, vid in hits:
            a(f"- {name} — `{vid}`")
    a("")

    a("## 🔒 Protected views (automation reads these by ID — never recreate)\n")
    if not protected_found:
        a("**Neither L2 Handoff view found — check whether they were renamed or deleted.**\n")
    for name, vid, _ in protected_found:
        a(f"- {name} — `{vid}`")
    a("")

    a("## Views referencing fields we're changing\n")
    for label, hits in sorted(by_field.items(), key=lambda kv: -len(kv[1])):
        a(f"\n**{label}** — {len(hits)} view(s)\n")
        for name, vid in hits:
            a(f"- {name} — `{vid}`")
    a("")

    a("## William's views — read before building the Setter lane\n")
    for name, vid in william_views:
        a(f"- {name} — `{vid}`")
    a("")

    a("---\n")
    a("Download `smart-views-export` from the artifacts above for the full JSON and CSV index.")

    report = "\n".join(md)
    print(report)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
