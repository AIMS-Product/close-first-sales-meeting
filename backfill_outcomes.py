#!/usr/bin/env python3
"""
backfill_outcomes.py — One-time historical Meeting Outcome backfill
====================================================================

Derives outcomes for PAST meetings that the live sync can't resolve (the
Google Meet era + anything older than its lookback), using the custom show
fields the team has maintained all along:

  Lead field                          -> which meeting            -> outcome
  ------------------------------------------------------------------------
  First Call Show (Override) Yes/No   -> meeting on FSCBD date    -> Completed / No Show
  First Call Show Up (Opp)  Yes/No    ->            (override wins)
  Follow Up Call Show 1..3  Yes/No    -> meeting on F/U 1..3 date -> Completed / No Show
  (meeting canceled in Close)         -> itself                   -> Rescheduled / Cancelled

HARD RULES (same as outcome_sync.py)
------------------------------------
* Never overwrites an existing outcome (blank / "Scheduled" only).
* DRY_RUN=1 by default — review backfill_report.json before writing.
* Only touches meetings that ended more than SKIP_RECENT_DAYS ago (default 7),
  so it never races the live sync.

Matching rules (meeting <-> field date, all dates Pacific)
----------------------------------------------------------
First call (FSCBD date):
  1. qualifying sales-call title on that date  -> match (earliest if several)
  2. else exactly ONE non-canceled meeting     -> match (logged low-confidence)
  3. else                                      -> unmapped, reported
Follow-ups (F/U 1..3 dates): same, but prefers follow-up-ish titles.

ENV:  CLOSE_API_KEY (required) | DRY_RUN (default 1) |
      BACKFILL_SINCE (default 2026-01-01) | SKIP_RECENT_DAYS (default 7)

Usage:
  python backfill_outcomes.py             # dry unless DRY_RUN=0
  python backfill_outcomes.py --selftest  # matching-logic tests, no network
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

import requests
from workflow_holds import guard_request

PACIFIC = ZoneInfo("America/Los_Angeles")
CLOSE_API = "https://api.close.com/api/v1"

OUTCOMES = {
    "scheduled":   "outcome_032DjlzDKpdXJZOzK4f7q3",
    "completed":   "outcome_032Djn4dfeNuEoCunojA7K",
    "rescheduled": "outcome_032Djo72GJ2Lvw3Q296wxH",
    "no_show":     "outcome_032DjoyPo9BgPBdOF6DzqH",
    "cancelled":   "outcome_032DjpoQ9otqb8rGb7SIYt",
}

# Lead custom fields (API keys are "custom.cf_...").
CF = {
    "fscbd":      "custom.cf_LFdYEQ6bsgp49YjZzefypDmdVx8iwuakWDSLPLpVrBq",
    "show":       "custom.cf_OPyvpU45RdvjLqfm8V1VWwNxrGKogEH2IBJmfCj0Uhq",
    "show_ovr":   "custom.cf_CJMktLJShTyA86PdBqNUP59ZfJh0WpdB1tEt76Y3HEy",
    "fu1_date":   "custom.cf_2BzXSnYW94EQJxtedp3E6xxUaxnpZ4e0K1MNrFvNjIG",
    "fu2_date":   "custom.cf_N9VyFcSUGaYx9VIFg08Uafm7C0GiKiDgQKOLc21M9SV",
    "fu3_date":   "custom.cf_42swTFCof7d1J96AA2BNvgT9Y9vk7KO9q7Hs0xtCJ3R",
    "fu1_show":   "custom.cf_dObuoBvyXtiJr8DD1cwCJroonvji5Bsyog48xig7vBr",
    "fu2_show":   "custom.cf_MDhIC6P8CFyRxwGgEaOygkhgDp2VZeNXNAZKHDUD5Ob",
    "fu3_show":   "custom.cf_AepH7zN22aSBceUoSBZuiYL68wl8CEc5zlGK54bKAjA",
}

# SYNC WITH update_field.py — qualifying sales-call title fragments.
SALES_TITLE_PATTERNS = [
    r"vending strategy call",
    r"vendingpren[eu]+rs?\s+consultation",
    r"vendingpren[eu]+rs?\s+strategy call",
    r"new vendingpreneur strategy call",
    r"vending consult",
    r"post masterclass strategy call",
    r"vending route consultation",
    r"cash[- ]?flowing vending route advisory interview",
    r"vending route advisory call",
    r"vendingpren[eu]+rs?\s*-?\s*next steps",   # scraper closer calls
    r"vendingpreneur next steps",
]
SALES_TITLE_RE = re.compile("|".join(SALES_TITLE_PATTERNS), re.IGNORECASE)
FOLLOWUP_RE = re.compile(r"follow[\s-]?up|fallow up|f/u", re.IGNORECASE)

DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
BACKFILL_SINCE = os.environ.get("BACKFILL_SINCE", "2026-01-01")
SKIP_RECENT_DAYS = int(os.environ.get("SKIP_RECENT_DAYS", "7"))

# ---------------------------------------------------------------------------
# Close helpers
# ---------------------------------------------------------------------------

def close_session():
    s = requests.Session()
    s.auth = (os.environ["CLOSE_API_KEY"], "")
    s.request = guard_request(s.request)
    s.headers["Content-Type"] = "application/json"
    return s


def close_req(s, method, path, **kw):
    for _ in range(5):
        r = s.request(method, f"{CLOSE_API}{path}", timeout=60, **kw)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)) + 0.5)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{method} {path}: rate-limited after retries")


def fetch_all_meetings(s, since_dt, until_dt):
    fields = "id,lead_id,user_id,title,starts_at,duration,status,outcome_id"
    out, skip = [], 0
    while True:
        data = close_req(s, "GET", "/activity/meeting/",
                         params={"_skip": skip, "_limit": 100, "_fields": fields})
        rows = data["data"]
        if not rows:
            break
        page_all_old = True
        for m in rows:
            st = parse_dt(m.get("starts_at"))
            if st and st >= since_dt:
                page_all_old = False
                if st <= until_dt:
                    out.append(m)
        if page_all_old or not data.get("has_more"):
            break
        skip += 100
    return out

# ---------------------------------------------------------------------------
# Matching logic (pure — covered by --selftest)
# ---------------------------------------------------------------------------

def parse_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_date(v):
    """Close date custom fields arrive as 'YYYY-MM-DD' (sometimes with time)."""
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def pacific_date(m):
    st = parse_dt(m.get("starts_at"))
    return st.astimezone(PACIFIC).date() if st else None


def is_canceledish(m):
    t = (m.get("title") or "").strip().lower()
    return m.get("status") == "canceled" or t.startswith("canceled")


def show_to_outcome(v):
    if v is None:
        return None
    v = str(v).strip().lower()
    return {"yes": "completed", "no": "no_show"}.get(v)


def match_meeting_for_date(target, meetings, prefer):
    """
    Meetings on `target` Pacific date. prefer: 'sales' or 'followup'.
    -> (meeting, confidence) | (None, reason)
    """
    day = [m for m in meetings if pacific_date(m) == target and not is_canceledish(m)]
    if not day:
        return None, "no meeting on field date"
    pref_re = SALES_TITLE_RE if prefer == "sales" else FOLLOWUP_RE
    preferred = [m for m in day if pref_re.search(m.get("title") or "")]
    if prefer == "sales":  # a sales call must not look like a follow-up
        preferred = [m for m in preferred
                     if not FOLLOWUP_RE.search(m.get("title") or "")] or preferred
    if preferred:
        return min(preferred, key=lambda m: m["starts_at"]), "title-match"
    if len(day) == 1:
        return day[0], "only-meeting-that-day"
    return None, f"{len(day)} meetings that day, none title-matched"


def plan_for_lead(lead_fields, meetings):
    """
    -> list of (meeting, outcome_key, basis, confidence), list of unmapped dicts
    Only meetings whose outcome is blank/Scheduled are ever planned.
    """
    plans, unmapped = [], []
    planned_ids = set()

    def writable(m):
        cur = m.get("outcome_id")
        return (not cur or cur == OUTCOMES["scheduled"]) and m["id"] not in planned_ids

    # canceled meetings -> rescheduled/cancelled (independent of fields)
    for m in meetings:
        if is_canceledish(m) and writable(m):
            st = parse_dt(m.get("starts_at"))
            later = any(parse_dt(o.get("starts_at")) and not is_canceledish(o)
                        and parse_dt(o["starts_at"]) > st
                        for o in meetings if o["id"] != m["id"])
            plans.append((m, "rescheduled" if later else "cancelled",
                          "close-status", "high"))
            planned_ids.add(m["id"])

    # first sales call
    ovr = show_to_outcome(lead_fields.get(CF["show_ovr"]))
    base = show_to_outcome(lead_fields.get(CF["show"]))
    first_outcome = ovr or base
    fscbd = parse_date(lead_fields.get(CF["fscbd"]))
    if first_outcome and fscbd:
        m, why = match_meeting_for_date(fscbd, meetings, "sales")
        if m and writable(m):
            plans.append((m, first_outcome,
                          "field:first-call" + ("(override)" if ovr else ""),
                          "high" if why == "title-match" else "low"))
            planned_ids.add(m["id"])
        elif m is None:
            unmapped.append({"basis": "first-call", "date": str(fscbd),
                             "outcome": first_outcome, "reason": why})

    # follow-ups 1..3
    for i in (1, 2, 3):
        fo = show_to_outcome(lead_fields.get(CF[f"fu{i}_show"]))
        fd = parse_date(lead_fields.get(CF[f"fu{i}_date"]))
        if not (fo and fd):
            continue
        m, why = match_meeting_for_date(fd, meetings, "followup")
        if m and writable(m):
            plans.append((m, fo, f"field:fu{i}",
                          "high" if why == "title-match" else "low"))
            planned_ids.add(m["id"])
        elif m is None:
            unmapped.append({"basis": f"fu{i}", "date": str(fd),
                             "outcome": fo, "reason": why})
    return plans, unmapped

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    now = datetime.now(timezone.utc)
    since = datetime.fromisoformat(BACKFILL_SINCE).replace(tzinfo=timezone.utc)
    until = now - timedelta(days=SKIP_RECENT_DAYS)
    s = close_session()
    print(f"backfill: {since:%Y-%m-%d}..{until:%Y-%m-%d} dry_run={DRY_RUN}")

    meetings = fetch_all_meetings(s, since, until)
    by_lead = defaultdict(list)
    for m in meetings:
        if m.get("lead_id"):
            by_lead[m["lead_id"]].append(m)

    # only leads that still have unresolved past meetings
    todo = {lid: ms for lid, ms in by_lead.items()
            if any((not m.get("outcome_id")
                    or m.get("outcome_id") == OUTCOMES["scheduled"]) for m in ms)}
    print(f"meetings in range: {len(meetings)} | leads to examine: {len(todo)}")

    report = {"planned": [], "unmapped": [], "errors": [],
              "already_terminal": sum(
                  1 for m in meetings
                  if m.get("outcome_id") and m["outcome_id"] != OUTCOMES["scheduled"])}

    fields_param = "id," + ",".join(CF.values())
    for n, (lead_id, ms) in enumerate(sorted(todo.items()), 1):
        try:
            lead = close_req(s, "GET", f"/lead/{lead_id}/",
                             params={"_fields": fields_param})
            plans, unmapped = plan_for_lead(lead, ms)
            for m, outcome, basis, conf in plans:
                if not DRY_RUN:
                    close_req(s, "PUT", f"/activity/meeting/{m['id']}/",
                              json={"outcome_id": OUTCOMES[outcome]})
                report["planned"].append(
                    {"meeting": m["id"], "lead": lead_id, "outcome": outcome,
                     "basis": basis, "confidence": conf,
                     "title": m.get("title"), "starts_at": m.get("starts_at")})
            for u in unmapped:
                u["lead"] = lead_id
                report["unmapped"].append(u)
            if n % 200 == 0:
                print(f"  ...{n}/{len(todo)} leads")
        except Exception as e:
            report["errors"].append({"lead": lead_id, "error": str(e)})

    lowconf = [p for p in report["planned"] if p["confidence"] == "low"]
    print(f"\nplanned : {len(report['planned'])} "
          f"({'dry run — nothing written' if DRY_RUN else 'WRITTEN'})")
    print(f"  by outcome: " + ", ".join(
        f"{k}={v}" for k, v in sorted(
            __import__('collections').Counter(
                p['outcome'] for p in report['planned']).items())))
    print(f"  low-confidence (only-meeting-that-day): {len(lowconf)}")
    print(f"unmapped: {len(report['unmapped'])} | errors: {len(report['errors'])}")

    with open("backfill_report.json", "w") as fh:
        json.dump({"generated_at": now.isoformat(), "dry_run": DRY_RUN, **report},
                  fh, indent=2)
    print("report: backfill_report.json")
    return 1 if report["errors"] else 0

# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

def selftest():
    def mtg(id, title, start, status="completed", outcome=None):
        return {"id": id, "title": title, "starts_at": start,
                "status": status, "outcome_id": None if not outcome else outcome,
                "lead_id": "lead_1"}

    # 10am PT = 17:00 UTC same day
    first = mtg("m1", "Vending Strategy Call", "2026-06-10T17:00:00+00:00")
    fu = mtg("m2", "Follow-Up Call with John", "2026-06-15T17:00:00+00:00")
    other = mtg("m3", "Random sync", "2026-06-10T20:00:00+00:00")
    late = mtg("m4", "Vending Strategy Call", "2026-06-11T05:00:00+00:00")  # 10pm PT 6/10!

    fields = {
        CF["fscbd"]: "2026-06-10", CF["show"]: "Yes", CF["show_ovr"]: None,
        CF["fu1_date"]: "2026-06-15", CF["fu1_show"]: "No",
        CF["fu2_date"]: None, CF["fu2_show"]: None,
        CF["fu3_date"]: None, CF["fu3_show"]: None,
    }

    checks = []

    # 1. first call matched by title on FSCBD date, show Yes -> completed
    plans, unmapped = plan_for_lead(fields, [first, fu, other])
    got = {(p[0]["id"], p[1]) for p in plans}
    checks.append(("first->completed", ("m1", "completed") in got))
    # 2. follow-up matched, show No -> no_show
    checks.append(("fu1->no_show", ("m2", "no_show") in got))
    # 3. 'Random sync' untouched
    checks.append(("other untouched", not any(p[0]["id"] == "m3" for p in plans)))

    # 4. override beats base field
    f2 = dict(fields); f2[CF["show_ovr"]] = "No"
    plans, _ = plan_for_lead(f2, [first])
    checks.append(("override wins", plans[0][1] == "no_show"))

    # 5. UTC/Pacific boundary: 05:00 UTC 6/11 is 10pm PT 6/10 -> matches FSCBD 6/10
    plans, _ = plan_for_lead(fields, [late])
    checks.append(("pacific boundary", any(p[0]["id"] == "m4" for p in plans)))

    # 6. never overwrite: meeting already has terminal outcome
    done = mtg("m5", "Vending Strategy Call", "2026-06-10T17:00:00+00:00",
               outcome=OUTCOMES["no_show"])
    plans, _ = plan_for_lead(fields, [done])
    checks.append(("no overwrite", not plans))

    # 7. canceled + later booking -> rescheduled
    can = mtg("m6", "Canceled: Vending Strategy Call", "2026-06-08T17:00:00+00:00",
              status="canceled")
    plans, _ = plan_for_lead(fields, [can, first])
    checks.append(("cancel->resched", any(p[0]["id"] == "m6" and p[1] == "rescheduled"
                                          for p in plans)))

    # 8. ambiguous day (two non-matching meetings) -> unmapped
    a = mtg("m7", "Chat", "2026-06-10T17:00:00+00:00")
    b = mtg("m8", "Other chat", "2026-06-10T18:00:00+00:00")
    plans, unmapped = plan_for_lead(fields, [a, b])
    checks.append(("ambiguous unmapped",
                   any(u["basis"] == "first-call" for u in unmapped)))

    # 9. single non-titled meeting that day -> low-confidence match
    plans, _ = plan_for_lead(fields, [a])
    checks.append(("lone meeting low-conf",
                   any(p[0]["id"] == "m7" and p[3] == "low" for p in plans)))

    # 10. scraper Next Steps titles count as sales
    ns = mtg("m9", "Vendingpreneurs - Next Steps Call w/ Greg", "2026-06-10T17:00:00+00:00")
    plans, _ = plan_for_lead(fields, [ns])
    checks.append(("scraper title match",
                   any(p[0]["id"] == "m9" and p[3] == "high" for p in plans)))

    failed = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run())
