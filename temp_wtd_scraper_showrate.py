#!/usr/bin/env python3
"""
temp_wtd_scraper_showrate.py — One-off: WTD scraper show rate from native
Meeting Outcomes, cross-checked against the exported CSV dispositions.

READ-ONLY. Run in the close-first-sales-meeting repo (CLOSE_API_KEY secret),
same pattern as temp_lane2_live_truth_audit.py.

Definition (Option A, per DECISION doc):
  shown       = any WTD meeting with native outcome Completed
  not shown   = else any WTD meeting with native outcome No Show
  excluded    = only Rescheduled/Cancelled outcomes in window
  unresolved  = blank/Scheduled outcomes only  -> listed for review
  show rate   = shown / (shown + not shown)

ENV: CLOSE_API_KEY (required) | WTD_START (default 2026-08-17)
"""

import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

PACIFIC = ZoneInfo("America/Los_Angeles")
CLOSE_API = "https://api.close.com/api/v1"
WTD_START = os.environ.get("WTD_START", "2026-08-17")

OUTCOME_NAMES = {
    "outcome_032DjlzDKpdXJZOzK4f7q3": "Scheduled",
    "outcome_032Djn4dfeNuEoCunojA7K": "Completed",
    "outcome_032Djo72GJ2Lvw3Q296wxH": "Rescheduled",
    "outcome_032DjoyPo9BgPBdOF6DzqH": "No Show",
    "outcome_032DjpoQ9otqb8rGb7SIYt": "Cancelled",
}

# (display_name, email, CSV "Todays Call Disposition")
LEADS = [
    ("Dan Minton", "minton.dan@gmail.com", "Canceled"),
    ("marco rojas", "rojas.pedrom@gmail.com", "Canceled"),
    ("Hamid Zabeti Zabeti", "hamidzabeti@gmail.com", "New Call No Show"),
    ("Herberth Hernandez", "alexishernandezhah98@gmail.com", "New Call No Show"),
    ("Lawanda Ross", "lross1976@icloud.com", "New Call No Show"),
    ("Iven Toma", "iventoma46@gmail.com", "New Call No Show"),
    ("Robert Wofford jr", "robwoff06294@gmail.com", "New Call No Show"),
    ("Will Norton", "nortyr530@gmail.com", "New Call No Show"),
    ("Aaron Whittaker", "airwood2091@gmail.com", "New Call No Show"),
    ("Step Jones", "lilstep_jones@yahoo.com", "New Call No Show"),
    ("Paul Dion", "pauldion524@gmail.com", "New Call No Show"),
    ("alex gabb", "gabbayg221@gmail.com", "New Call No Show"),
    ("Nathan Loomis", "suncoastmodernpantry@gmail.com", "New Call Show"),
    ("Rashard Malone", "deeplyrootedentllc@gmail.com", "New Call Show"),
    ("Tait", "tait.njord@gmail.com", "New Call Show"),
    ("Kisha Mehak", "kisha.mehak@gmail.com", "New Call Show"),
    ("Amy Morton", "menagirl7@me.com", "New Call Show"),
    ("Ronald Williams", "rw7706@gmail.com", "New Call Show"),
    ("Leanard Ford", "lennyford1953@gmail.com", "New Call Show"),
    ("Jovan Crockett", "crockettjovan@gmail.com", "New Call Show"),
    ("Aaron LeSure", "aaronjlesure77@gmail.com", "New Call Show"),
    ("AL ELLIOTT", "alelliott4@gmail.com", "New Call Show"),
    ("Ross Scarantino", "griffin9130@gmail.com", "New Call Show"),
    ("Alex Zahrebelnyi", "alex.zahrebelnyi@gmail.com", "New Call Show"),
    ("Jonathan Sanchez", "jsanchez.entr@gmail.com", "New Call Show"),
    ("Rick Reading", "rick.reading@gmail.com", "New Call Show"),
    ("Geronimo Reyes", "geronimoreyes19@gmail.com", "Reschedule Show"),
    ("Chenoa", "infinity.jurnee@gmail.com", ""),
    ("Emma Fowler", "fowlere4530@gmail.com", ""),
    ("Okiki Miller", "kikimcnair23@gmail.com", ""),
    ("James Meek", "fuzyshark@yahoo.com", ""),
    ("Ruben Scott", "rscott1108@gmail.com", ""),
]


def close_get(s, path, params=None):
    for _ in range(5):
        r = s.get(f"{CLOSE_API}{path}", params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)) + 0.5)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"GET {path}: rate-limited")


def find_lead(s, name, email):
    for q in (f'email:"{email}"', f'"{name}"'):
        data = close_get(s, "/lead/", {"query": q, "_fields": "id,display_name", "_limit": 3})
        if data.get("data"):
            return data["data"][0]
    return None


def wtd_meetings(s, lead_id):
    data = close_get(s, "/activity/meeting/", {
        "lead_id": lead_id, "_limit": 100,
        "_fields": "id,title,starts_at,status,outcome_id"})
    out = []
    for m in data.get("data", []):
        st = m.get("starts_at")
        if not st:
            continue
        dt = datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone(PACIFIC)
        if str(dt.date()) >= WTD_START and dt <= datetime.now(PACIFIC):
            m["_pac_date"] = str(dt.date())
            out.append(m)
    return sorted(out, key=lambda m: m["starts_at"])


def verdict(meetings):
    names = [OUTCOME_NAMES.get(m.get("outcome_id") or "", "(blank)") for m in meetings]
    if "Completed" in names:
        return "SHOWN", names
    if "No Show" in names:
        return "NOT SHOWN", names
    if names and all(n in ("Rescheduled", "Cancelled") for n in names):
        return "EXCLUDED (resched/cancel)", names
    if names:
        return "UNRESOLVED (blank outcome)", names
    return "NO WTD MEETING FOUND", names


DISPO_EXPECT = {  # CSV disposition -> expected verdict for mismatch check
    "New Call Show": "SHOWN", "Reschedule Show": "SHOWN", "Follow Up Show": "SHOWN",
    "New Call No Show": "NOT SHOWN", "Reschedule No Show": "NOT SHOWN",
    "Follow Up No Show": "NOT SHOWN",
    "Canceled": "EXCLUDED (resched/cancel)", "Canceled - Rescheduled": "EXCLUDED (resched/cancel)",
}


def main():
    s = requests.Session()
    s.auth = (os.environ["CLOSE_API_KEY"], "")
    counts = {"SHOWN": 0, "NOT SHOWN": 0, "EXCLUDED (resched/cancel)": 0,
              "UNRESOLVED (blank outcome)": 0, "NO WTD MEETING FOUND": 0}
    mismatches, rows = [], []

    for name, email, dispo in LEADS:
        lead = find_lead(s, name, email)
        if not lead:
            rows.append((name, "LEAD NOT FOUND", "", dispo, ""))
            continue
        ms = wtd_meetings(s, lead["id"])
        v, outcome_names = verdict(ms)
        counts[v] += 1
        detail = "; ".join(
            f"{m['_pac_date']} '{(m.get('title') or '')[:35]}' -> "
            f"{OUTCOME_NAMES.get(m.get('outcome_id') or '', '(blank)')}"
            for m in ms) or "(none)"
        flag = ""
        expected = DISPO_EXPECT.get(dispo)
        if expected and v in ("SHOWN", "NOT SHOWN", "EXCLUDED (resched/cancel)") and v != expected:
            flag = "MISMATCH"
            mismatches.append((name, dispo, v, detail,
                               f"https://app.close.com/lead/{lead['id']}/"))
        rows.append((name, v, detail, dispo, flag))

    w = max(len(r[0]) for r in rows) + 2
    print(f"{'LEAD':<{w}} {'OUTCOME VERDICT':<28} {'CSV DISPOSITION':<20} FLAG")
    for name, v, detail, dispo, flag in rows:
        print(f"{name:<{w}} {v:<28} {dispo or '(blank)':<20} {flag}")
        print(f"{'':<{w}}   {detail}")

    shown, noshow = counts["SHOWN"], counts["NOT SHOWN"]
    denom = shown + noshow
    print("\n=== WTD SCRAPER SHOW RATE (native Meeting Outcome, Option A) ===")
    for k, v in counts.items():
        print(f"  {k:<28}: {v}")
    print(f"\n  SHOW RATE: {shown}/{denom} = "
          f"{(shown / denom * 100):.1f}%" if denom else "  SHOW RATE: n/a")
    print(f"\n  disposition-vs-outcome mismatches: {len(mismatches)}")
    for name, dispo, v, detail, url in mismatches:
        print(f"    {name}: CSV says '{dispo}', outcomes say {v}  {url}")
        print(f"      {detail}")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
               "counts": counts, "show_rate_pct": (shown / denom * 100) if denom else None,
               "mismatches": [dict(zip(("lead", "csv_disposition", "outcome_verdict",
                                        "meetings", "url"), m)) for m in mismatches]},
              open("wtd_showrate_report.json", "w"), indent=2)
    print("\nreport: wtd_showrate_report.json")


if __name__ == "__main__":
    main()
