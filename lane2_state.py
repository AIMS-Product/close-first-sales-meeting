#!/usr/bin/env python3
"""
Lane 2 state reconciler.

Computes the correct value of all six Lane 2 fields for every lead, from source
data, and writes only what differs. Idempotent: backfill and steady-state are the
same code path, so it is self-healing and there is no separate migration step.

    Recapture State   the bucket a lead is in       (precedence, first match wins)
    Owner Team        who dials it                  (derived from Lead Owner)
    Ever Had Call     has a completed meeting       (one-way, never resets)
    Entry Source      how they arrived              (permanent once set)
    Objection Angle   content angle                 (from Lost Reason)

Bucket definitions are Close query JSON, so they are portable straight into
smart views — the dial list and the state machine stay in sync by construction.

    python3 lane2_state.py                 # dry run, full report, writes nothing
    python3 lane2_state.py --apply         # write changes
    python3 lane2_state.py --limit 500     # sample, for testing
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

API_KEY = os.environ.get("CLOSE_API_KEY")
if not API_KEY:
    sys.exit("Set CLOSE_API_KEY first.")

BASE = "https://api.close.com/api/v1"
AUTH = (API_KEY, "")

# ============================================================================
# CONFIG — everything tunable lives here
# ============================================================================

# Close caps search pagination at 10,000 results, so every full query is sliced
# by month of date_created. This must predate the oldest lead in the account or
# those leads are invisible to the reconciler.
# Three full runs confirmed zero leads created before 2024, so start there and
# skip ~60 empty windows per query (roughly halves runtime — matters on an hourly
# schedule). The guard below shouts if that assumption ever stops holding.
LEAD_HISTORY_START = "2024-01-01"

WRITE_WORKERS = 8      # parallel PUTs. 56k sequential writes will not finish in one job.

HOT_WINDOW_DAYS = 14        # Setter owns a fresh inbound signal this long
BLITZ_DAYS = 14             # all-angles push after going cold
REENGAGE_DAYS = 7           # an inbound reply keeps them "hot" this long
DEEP_NURTURE_MONTHS = 6     # active -> deep (months, not days — see within())

# --- fields we write --------------------------------------------------------
F_STATE = "cf_hKcyx4tQSMvHd7llfLX363bx3LGXMxVEmFHEtjpR5C2"   # Recapture State
F_ENTRY = "cf_FI1V2Zw8M0aoSg7yJKJo0wb8NbRU8LafnkgHrpLcfYA"   # Entry Source
F_TEAM = "cf_3SkrryGiRGT83UHgsBOXpUYbVSyc9RmKKfmKY1uWWib"    # Owner Team
F_ANGLE = "cf_6jVIeFyPI8qeQbZSo3ZoaKpjeeLD7du6k2nec8tNPZi"   # Objection Angle
F_EVERCALL = "cf_6g8pVOlyVciSucCIvbI3CjKsPE4nlJEaKUX0DY9H5XY"  # Ever Had Call (Yes/No)

# Sales Team Lane — legacy field, previously maintained by update_sales_lane.py.
# This script now derives it from the SAME roster as Owner Team, so that script
# can be retired. Set to False once nothing external reads Sales Team Lane.
F_SALESLANE = "cf_UD9Hm3dpLGtcUd37tX8Y9GAK1Lhc3BdtDX769ffFvyB"
WRITE_SALES_LANE = True

# Resource Tag — TEXT field. Form automation owns it going forward; this script
# only SEEDS it where empty, from legacy Handraiser/Funnel data, and never
# overwrites. Being text, it fails soft: an unexpected value lands and shows up
# in the drift report below, rather than being silently rejected like a bad
# dropdown choice would be.
F_RESOURCE = "cf_PWAGlTAxZ62ybFh01xcEzVl0RHw6KUXHp4g4YFb2PgT"
SEED_RESOURCE_TAG = True

# --- fields we read ---------------------------------------------------------
F_OWNER = "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd"   # Lead Owner
F_HANDRAISER = "cf_Q1hRv8It46xsAEmpv4PRKdI1y0sPJnrnQrgRbIlF8uL"
F_FUNNEL = "cf_xqDQE8fkPsWa0RNEve7hcaxKblCe6489XeZGRDzyPdX"
F_LOSTREASON = "cf_R4i05fLNOQP8yveAs4ofTMMYGAQnkLLklunP4lov2Bt"
F_OVERRIDE = "cf_6PnYz6aaAkLzHMU3Faxz7kFurBEfadqlfGiBgfhjaVC"  # Reassignment Override

# --- statuses ---------------------------------------------------------------
S_NEW = "stat_EwxduBOxA2CLBUrvXAyB7ZrVXKGw7v9i5xz0f2JuIY9"
S_LOST = "stat_aR2jBa8YnTNZmHAnPsnlQuinBdaXpSBCkZGP3UvoBlV"
S_NOSHOW = "stat_5CqIgNJnGYO357zXjSnH6BAkKyoCvYUOBxVvpYfDMZn"
S_CANCELED = "stat_hWIGHjzyNpl4YjIFSFz3VK4fp2ny10SFJLKAihmo4KT"
S_WON = "stat_0oW3iRpVp9z5DJq0cuwI1HgR0XhHAhykEPPIq4TFsxd"
S_DNC = "stat_U9MI7pqsvIjceTv3pCU7b1EghO8Q83h1HUcL6fGVyi6"
S_DQ = "stat_p3oblSTnbsyDAw4rWqZDePGYMOlKBgV2FjbqIMDrfvF"
S_OUTSIDE_US = "stat_YV4ZngDB4IGjLjlOf0YTFEWuKZJ6fhNxVkzQkvKYfdB"

SUPPRESS_STATUSES = [S_WON, S_DNC, S_DQ, S_OUTSIDE_US]

# --- rosters (user_id -> Owner Team) ----------------------------------------
SETTERS = {
    "user_ZNKG1S9eI71qxhSozBK4jskTVtJqXzfNCPWqmADRR9F": "William Nowak",
    "user_0SuNg0OWd2reYMeyuDVqiVvjiGcRiFheKKOXXZpyaPZ": "Pearl Sathekge",
}
SCRAPERS = {
    "user_dQi0iL0igjCKtEXPSsv8ALDZMAz9orJxL60O7Q921jy": "Vince Bartolini",
    "user_IeWR2TlhpjqoXy3K6jX7u9C8c83iBnHXSIvFZpotF3z": "Jacob Hepner",
    "user_lXtgDE8eKS8s3tKDQrl8eUP7tYCXuNNJATddPUkuLlQ": "Becca Leier",
    "user_p2y1gLbIgUb9xognGTvuXoRpzp4Ro8QkO20ltgF1CvJ": "Jacob Herbig",
    "user_MrBLkl5wCqTm7QxHxPo2ydNV5KxMllg6YZDVc12Aqzj": "Jason Aaron",  # lane manager
}
LANE_1 = {
    "user_lUjlATIIgFg8mELa0GFzZUj0lG4Cs7PwQsxbi34I6Su": "Joe Dysert",
    "user_7F059xEinVentOEvkRMP77fWZyvwUiTRTUOuhD11J0e": "Robin Perkins",
    "user_7HSxi55O8q5jO11khvrTcAGoL2nlcoa3kZ6loAY6i78": "Joe Vaughan",
    "user_F0VeLnOQlWpkDncNW8rBl1V2QJ08fnDt6DcUjNATUJK": "Scott Seymour",
    "user_wHm1vcLde4RExd3vv9UOjnms5Oz8ssXg8600mQuxMPb": "Christian Hartwell",
    "user_wF5aATmDljO6g6AHqehRPVmfCmH5j9VszbO6Q6Pjzm4": "Eric Piccione",
    "user_1TKtkacQ7ZMKkcqnmCERikTYWwGltp5XUjEE9Hshple": "Shreya Bechra",
    "user_XEbPgLixZy4dhuLp34WogOzCIChkKEnrffDnHlxOnA7": "Danny Santolaya",
    "user_1xDZSeOa8omjfxHXD80twTf8OieXfQ6tNCaYbVygtv1": "Dubem Adindu",
    "user_6kp6k4OcqKqFNrxGjgMUncedjiCYC6JHU8EI28F7etV": "Luke Herman",
    "user_vyiPzY0qxbLwnW5Ubwae8vY2MLviPuozSTIsEKcyrFE": "Zac Clover",
}

# --- Handraiser / Funnel -> Entry Source ------------------------------------
ENTRY_FROM_HANDRAISER = {
    "Webinar - Registered / Did Not Attend": "Webinar",
    "Webinar - Attended / Did Not Book": "Webinar",
    "WWWS / Registrants": "Webinar",
    "Internal Webinar": "Webinar",
    "Typeform Entry / Did Not Book": "Typeform",
    "Typeform Entry / DQ'd": "Typeform",
    "LTF Course Purchased / Did Not Book": "Lead-Magnet",
    "LTF Course Purchased / High Activity": "Lead-Magnet",
    "LTF All Booked / Did Not Convert": "Lead-Magnet",
    "Quiz Funnel": "Lead-Magnet",
    "Route Builder": "Lead-Magnet",
    # state-values deliberately unmapped — they say nothing about origin
}
ENTRY_FROM_FUNNEL = {
    "Internal Webinar": "Webinar", "Webinar": "Webinar", "WWWS": "Webinar",
    "Reactivation Email": "Reactivation-Email",
    "Reactivation Email Setter": "Reactivation-Email",
    "Reactivation Scrapers": "Rep-Outbound",
    "Sales Reactivation": "Rep-Outbound",
    "Low Ticket Funnel": "Lead-Magnet", "LTF - In-House": "Lead-Magnet",
    "LTF - Quiz Funnel": "Lead-Magnet", "VSL": "Lead-Magnet",
}

# --- Handraiser / Funnel -> Resource Tag (the specific asset) ---------------
# Entry Source is the channel; Resource Tag is the asset. Seed only.
RESOURCE_FROM_HANDRAISER = {
    "Webinar - Registered / Did Not Attend": "Internal Webinar",
    "Webinar - Attended / Did Not Book": "Internal Webinar",
    "Internal Webinar": "Internal Webinar",
    "WWWS / Registrants": "WWWS",
    "LTF Course Purchased / Did Not Book": "LTF - Course",
    "LTF Course Purchased / High Activity": "LTF - Course",
    "LTF All Booked / Did Not Convert": "LTF - Course",
    "Quiz Funnel": "LTF - Quiz Funnel",
    "Route Builder": "Route Builder",
    "Typeform Entry / Did Not Book": "Typeform - Application",
    "Typeform Entry / DQ'd": "Typeform - Application",
}
RESOURCE_FROM_FUNNEL = {
    "WWWS": "WWWS", "Internal Webinar": "Internal Webinar",
    "LTF - Quiz Funnel": "LTF - Quiz Funnel",
    "LTF - In-House": "LTF - Course", "Low Ticket Funnel": "LTF - Course",
    "VSL": "VSL",
}

# --- Lost Reason -> Objection Angle -----------------------------------------
ANGLE_FROM_LOST = {
    'Price- "Thats more than I can afford to pay"': "Price",
    'Finance- "Not approved for financing"': "Price",
    'DIY- "I can do this on my own"': "DIY",
    "Competitor": "DIY",
    "Timing": "Timing",
    "Not currently looking to acquire a business": "Timing",
    'Support- "Spouse/partner won\'t get onboard"': "Spouse",
    'Bad Fit- "Vending is not for me"': "Not-a-Fit",
    'Doubt- "I do not see the value/I am skeptical"': "Not-a-Fit",
    "Unresponsive": "None", "No Show": "None", "Do Not Contact": "None",
    "Outside of the US or Canada": "None", "payment dipped": "None",
}

# ============================================================================
# query builders — shapes verified against exported smart views
# ============================================================================

def _wrap(*conds):
    return {"negate": False, "type": "and", "queries": [
        {"negate": False, "object_type": "lead", "type": "object_type"},
        {"negate": False, "type": "and", "queries": list(conds)},
    ]}

def status_in(ids, negate=False):
    return {"type": "field_condition", "negate": negate,
            "field": {"type": "regular_field", "object_type": "lead", "field_name": "status_id"},
            "condition": {"type": "reference", "reference_type": "status.lead", "object_ids": ids}}

def within(field_name, days=None, hours=None, months=None, negate=False):
    # Close appears to reject large `days` offsets — no exported smart view uses
    # more than 60. Express anything longer in months.
    off = {"years": 0, "months": months or 0, "weeks": 0, "days": days or 0,
           "hours": hours or 0, "minutes": 0, "seconds": 0}
    return {"type": "field_condition", "negate": negate,
            "field": {"type": "regular_field", "object_type": "lead", "field_name": field_name},
            "condition": {"type": "moment_range", "before": {"type": "now"},
                          "on_or_after": {"type": "offset", "direction": "past",
                                          "moment": {"type": "now"}, "offset": off,
                                          "which_day_end": "start"}}}

def num_range(field_name, gte=None, lte=None):
    c = {"type": "number_range"}
    if gte is not None: c["gte"] = float(gte)
    if lte is not None: c["lte"] = float(lte)
    return {"type": "field_condition", "negate": False,
            "field": {"type": "regular_field", "object_type": "lead", "field_name": field_name},
            "condition": c}

def created_between(start, end):
    """Absolute date_created window — used to partition around the 10k skip cap."""
    return {"type": "field_condition", "negate": False,
            "field": {"type": "regular_field", "object_type": "lead",
                      "field_name": "date_created"},
            "condition": {"type": "moment_range",
                          "on_or_after": {"type": "fixed_local_date", "value": start,
                                          "which": "start"},
                          "before": {"type": "fixed_local_date", "value": end,
                                     "which": "start"}}}

def add_condition(query, extra):
    """Return a copy of `query` with one more ANDed condition."""
    q = json.loads(json.dumps(query))
    q["queries"][1]["queries"].append(extra)
    return q

def has_incoming(kind, days):
    """Lead received an inbound sms / email / call in the last N days."""
    off = {"years": 0, "months": 0, "weeks": 0, "days": days,
           "hours": 0, "minutes": 0, "seconds": 0}
    ot = f"activity.{kind}"
    return {"type": "has_related", "negate": False,
            "this_object_type": "lead", "related_object_type": ot,
            "related_query": {"negate": False, "type": "and", "queries": [
                {"type": "field_condition", "negate": False,
                 "field": {"type": "regular_field", "object_type": ot,
                           "field_name": "direction"},
                 "condition": {"type": "term", "values": ["incoming"]}},
                {"type": "field_condition", "negate": False,
                 "field": {"type": "regular_field", "object_type": ot,
                           "field_name": "date_created"},
                 "condition": {"type": "moment_range", "before": {"type": "now"},
                               "on_or_after": {"type": "offset", "direction": "past",
                                               "moment": {"type": "now"}, "offset": off,
                                               "which_day_end": "start"}}}]}}

def any_inbound(days):
    """A fresh hand-raise: they contacted US, by any channel."""
    return {"negate": False, "type": "or",
            "queries": [has_incoming(k, days) for k in ("sms", "email", "call")]}

def has_completed_meeting(negate=False):
    return {"type": "has_related", "negate": negate,
            "this_object_type": "lead", "related_object_type": "activity.meeting",
            "related_query": {"negate": False, "type": "and", "queries": [
                {"type": "field_condition", "negate": False,
                 "field": {"type": "regular_field", "object_type": "activity.meeting",
                           "field_name": "status"},
                 "condition": {"type": "term", "values": ["completed"]}}]}}

# ---- the buckets, in precedence order (first match wins) -------------------

# (report label, Recapture State written, query) — first match wins.
BUCKETS = [
    ("Suppressed", "Suppressed",
     _wrap(status_in(SUPPRESS_STATUSES))),

    ("Booked", "Booked",
     _wrap(status_in(SUPPRESS_STATUSES, negate=True),
           num_range("num_upcoming_meetings", gte=1))),

    # --- the one arrow back up ------------------------------------------------
    # A fresh hand-raise outranks everything except a booking. Jess's model: reset
    # to Hot if they've never spoken to us, to the warm/Blitz queue if they have.
    # Uses the live completed-meeting query rather than the stored Ever Had Call
    # flag, so it can't lag a run behind.
    ("Re-engaged → Hot", "Hot-Inbound",
     _wrap(status_in(SUPPRESS_STATUSES, negate=True),
           num_range("num_upcoming_meetings", lte=0),
           any_inbound(REENGAGE_DAYS),
           has_completed_meeting(negate=True))),

    ("Re-engaged → Blitz", "Blitz",
     _wrap(status_in(SUPPRESS_STATUSES, negate=True),
           num_range("num_upcoming_meetings", lte=0),
           any_inbound(REENGAGE_DAYS),
           has_completed_meeting())),
    # -------------------------------------------------------------------------

    # Blitz outranks Hot-Inbound: a lead created 3 days ago that already
    # no-showed belongs in the no-show push, not the fresh-hand-raise queue.
    ("Blitz", "Blitz",
     _wrap(status_in(SUPPRESS_STATUSES, negate=True),
           num_range("num_upcoming_meetings", lte=0),
           status_in([S_LOST, S_NOSHOW, S_CANCELED]),
           within("last_lead_status_change_date", days=BLITZ_DAYS))),

    # Deliberately NOT keyed on status = New. A rep moving a 2-day-old lead to
    # "Follow Up" shouldn't drop it out of the Setter's hot window — recency plus
    # "hasn't spoken to us yet" is what actually defines a fresh hand-raise.
    ("Hot-Inbound", "Hot-Inbound",
     _wrap(status_in(SUPPRESS_STATUSES, negate=True),
           num_range("num_upcoming_meetings", lte=0),
           has_completed_meeting(negate=True),
           within("date_created", days=HOT_WINDOW_DAYS))),

    ("Active-Nurture", "Active-Nurture",
     _wrap(status_in(SUPPRESS_STATUSES, negate=True),
           num_range("num_upcoming_meetings", lte=0),
           within("last_communication_date", months=DEEP_NURTURE_MONTHS))),
    # Deep-Nurture is the fallthrough — anything contactable not caught above.
    # List-Swap is terminal and needs a drip-exhaustion signal we don't have yet.
]
FALLTHROUGH = "Deep-Nurture"

# ============================================================================
# API
# ============================================================================

class CloseError(Exception):
    pass

def _req(method, url, **kw):
    r = None
    for attempt in range(6):
        r = requests.request(method, url, auth=AUTH, timeout=45, **kw)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("retry-after", 2)) + attempt)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            # Close returns useful field-level detail — surface it instead of
            # a bare "400 Bad Request".
            try:
                detail = json.dumps(r.json(), indent=1)[:1500]
            except Exception:
                detail = r.text[:1500]
            raise CloseError(f"{r.status_code} from {url}\n{detail}")
        return r
    raise CloseError(f"gave up after retries: {r.status_code if r else '?'} {url}")

SKIP_CAP = 10000   # Close rejects cursor skip > 10,000. Hard API limit.

def _search_page(query, fields=None, limit=None):
    """Single un-partitioned search. Only safe when the result set is < SKIP_CAP."""
    out, cursor = [], None
    body = {"query": query, "_limit": 200}
    if fields:
        body["_fields"] = {"lead": fields}
    while True:
        if cursor:
            body["cursor"] = cursor
        j = _req("POST", f"{BASE}/data/search/", json=body).json()
        out.extend(j.get("data", []))
        cursor = j.get("cursor")
        if not cursor or (limit and len(out) >= limit) or len(out) >= SKIP_CAP:
            break
    return out[:limit] if limit else out


def _month_windows(start=None):
    """Month boundaries from `start` to just past today, as (from, to) strings."""
    start = start or LEAD_HISTORY_START
    y, m = int(start[:4]), int(start[5:7])
    today = time.gmtime()
    out = []
    while (y, m) <= (today.tm_year, today.tm_mon):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01"))
        y, m = ny, nm
    return out


def search(query, fields=None, limit=None):
    """
    Run a Close search, partitioning by month of date_created.

    Close caps cursor skip at 10,000 results, so any query matching more than
    that cannot be paged straight through. Slicing by creation month keeps every
    slice well under the cap and covers the whole database.
    """
    if limit and limit <= SKIP_CAP:
        # Small sample — one pass is enough and much faster.
        return _search_page(query, fields=fields, limit=limit)

    seen, out = set(), []
    for i, (a, b) in enumerate(_month_windows(), 1):
        rows = _search_page(add_condition(query, created_between(a, b)), fields=fields)
        if i == 1 and rows:
            # Leads exist in the very first window, so older ones probably do too
            # and they'd be invisible. Push LEAD_HISTORY_START back.
            print(f"    ⚠️  {len(rows)} leads in the first window ({a}) — leads may predate "
                  f"LEAD_HISTORY_START and be missed. Move it earlier.", file=sys.stderr)
        if len(rows) >= SKIP_CAP:
            print(f"    ⚠️  {a} hit the {SKIP_CAP:,} cap — window too wide, results truncated",
                  file=sys.stderr)
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
        if i % 12 == 0:
            print(f"    ...{a[:4]}: {len(out):,} so far", file=sys.stderr)
    return out

def cf(lead, fid):
    return lead.get(f"custom.{fid}") or lead.get("custom", {}).get(fid)

def _norm(s):
    return "".join(c for c in s.lower() if c.isalnum())

def _near_duplicates(counter):
    """Text fields drift. Flag values that normalise to the same thing."""
    groups = defaultdict(list)
    for v in counter:
        groups[_norm(v)].append(v)
    out = []
    for vs in groups.values():
        if len(vs) > 1:
            for i in range(len(vs) - 1):
                out.append((vs[i], vs[i + 1]))
    return out

# ============================================================================
# derivation
# ============================================================================

def owner_team(lead):
    o = cf(lead, F_OWNER)
    if not o: return "None"
    if o in SETTERS: return "Setter"
    if o in SCRAPERS: return "Scraper"
    if o in LANE_1: return "Lane 1"
    return "None"

def sales_lane(lead):
    """Legacy Sales Team Lane, from the same roster — replaces update_sales_lane.py."""
    t = owner_team(lead)
    if t in ("Setter", "Scraper"): return "Lane 2"
    if t == "Lane 1": return "Lane 1"
    return None   # unknown owner -> leave alone, same as the old script

def entry_source(lead):
    h = cf(lead, F_HANDRAISER)
    if h and h in ENTRY_FROM_HANDRAISER:
        return ENTRY_FROM_HANDRAISER[h]
    f = cf(lead, F_FUNNEL)
    return ENTRY_FROM_FUNNEL.get(f)

def objection_angle(lead):
    return ANGLE_FROM_LOST.get(cf(lead, F_LOSTREASON))

def resource_tag_seed(lead):
    """Seed value only — returns None if the lead already has one."""
    if cf(lead, F_RESOURCE):
        return None                     # form automation owns it; never overwrite
    h = cf(lead, F_HANDRAISER)
    if h and h in RESOURCE_FROM_HANDRAISER:
        return RESOURCE_FROM_HANDRAISER[h]
    return RESOURCE_FROM_FUNNEL.get(cf(lead, F_FUNNEL))

# ============================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--limit", type=int, help="only process N leads (testing)")
    ap.add_argument("--max-writes", type=int,
                    help="cap writes this run; re-run to continue (idempotent)")
    ap.add_argument("--emit-views", action="store_true",
                    help="print each bucket's query JSON (paste into a smart view) and exit")
    args = ap.parse_args()

    if args.emit_views:
        for label, state, q in BUCKETS:
            print(f"\n===== {label}  (writes Recapture State = {state}) =====")
            print(json.dumps({"query": q, "results_limit": None, "sort": []}, indent=1))
        print(f"\n===== {FALLTHROUGH} =====\n(fallthrough — everything not matched above)")
        return

    read_fields = ["id", "display_name", "status_id",
                   f"custom.{F_OWNER}", f"custom.{F_HANDRAISER}", f"custom.{F_FUNNEL}",
                   f"custom.{F_LOSTREASON}", f"custom.{F_OVERRIDE}",
                   f"custom.{F_STATE}", f"custom.{F_ENTRY}",
                   f"custom.{F_TEAM}", f"custom.{F_ANGLE}",
                   f"custom.{F_RESOURCE}", f"custom.{F_SALESLANE}"]
    if F_EVERCALL:
        read_fields.append(f"custom.{F_EVERCALL}")

    print("Loading leads...", file=sys.stderr)
    leads = search(_wrap(), fields=read_fields, limit=args.limit)
    print(f"  {len(leads)} leads\n", file=sys.stderr)
    by_id = {l["id"]: l for l in leads}

    # --- state, by precedence ---
    # A --limit run is a smoke test: bucket queries run un-partitioned (one fast
    # pass) so they may be incomplete. Never judge correctness from a sample run.
    bucket_limit = SKIP_CAP if args.limit else None
    if args.limit:
        print("  (sample run — bucket queries un-partitioned, counts are indicative only)\n",
              file=sys.stderr)

    state_of, claimed, failed = {}, set(), []
    print("Resolving buckets...", file=sys.stderr)
    for label, state, q in BUCKETS:
        try:
            ids = {l["id"] for l in search(q, fields=["id"], limit=bucket_limit)}
        except CloseError as e:
            # Don't lose the whole run to one bad bucket — report and continue.
            # Leads that would have matched fall through to a later bucket.
            failed.append(label)
            print(f"  {label:<20}   QUERY FAILED\n{e}\n", file=sys.stderr)
            continue
        fresh = (ids & set(by_id)) - claimed
        for i in fresh:
            state_of[i] = state
        claimed |= fresh
        print(f"  {label:<20} {len(fresh):>7,}", file=sys.stderr)
    for i in by_id:
        state_of.setdefault(i, FALLTHROUGH)
    print(f"  {FALLTHROUGH:<16} {sum(1 for v in state_of.values() if v==FALLTHROUGH):>7,}\n",
          file=sys.stderr)

    # --- ever had call ---
    ever, ever_ok = set(), False
    if F_EVERCALL:
        print("Resolving Ever Had Call...", file=sys.stderr)
        try:
            ever = {l["id"] for l in search(_wrap(has_completed_meeting()),
                                            fields=["id"], limit=bucket_limit)}
            ever_ok = True
            print(f"  {len(ever):,} leads with a completed meeting\n", file=sys.stderr)
        except CloseError as e:
            print(f"  QUERY FAILED — Ever Had Call skipped this run\n{e}\n", file=sys.stderr)

    # --- diff ---
    changes = defaultdict(dict)
    counts, skipped_override = Counter(), 0
    for lid, lead in by_id.items():
        desired = {
            F_STATE: state_of[lid],
            F_TEAM: owner_team(lead),
            F_ENTRY: entry_source(lead),
            F_ANGLE: objection_angle(lead),
        }
        if F_EVERCALL and ever_ok:
            desired[F_EVERCALL] = "Yes" if lid in ever else "No"
        if WRITE_SALES_LANE:
            desired[F_SALESLANE] = sales_lane(lead)
        if SEED_RESOURCE_TAG:
            desired[F_RESOURCE] = resource_tag_seed(lead)

        if cf(lead, F_OVERRIDE) == "Yes":
            desired.pop(F_TEAM, None)          # respect the escape hatch
            desired.pop(F_SALESLANE, None)
            skipped_override += 1

        for fid, want in desired.items():
            if want is None:
                continue
            have = cf(lead, fid)
            if want == "None" and not have and fid == F_TEAM:
                continue                        # don't churn empties
            if have != want:
                changes[lid][f"custom.{fid}"] = want
                counts[fid] += 1

    # --- report ---
    label = {F_STATE: "Recapture State", F_TEAM: "Owner Team", F_ENTRY: "Entry Source",
             F_ANGLE: "Objection Angle", F_EVERCALL: "Ever Had Call",
             F_SALESLANE: "Sales Team Lane", F_RESOURCE: "Resource Tag (seed)"}
    print("=" * 60)
    print(f"{len(changes):,} leads need updates")
    print("=" * 60)
    for fid, n in counts.most_common():
        print(f"  {label.get(fid, fid):<18} {n:>7,}")
    print(f"\nRecapture State distribution:")
    for s, n in Counter(state_of.values()).most_common():
        print(f"  {s:<18} {n:>7,}")
    print(f"\nOwner Team distribution:")
    for t, n in Counter(owner_team(l) for l in leads).most_common():
        print(f"  {t:<18} {n:>7,}")
    if skipped_override:
        print(f"\n{skipped_override:,} leads have Reassignment Override — Owner Team left alone")
    # --- Resource Tag drift report (it's a text field — watch for variants) ---
    res = Counter()
    for lid, lead in by_id.items():
        v = cf(lead, F_RESOURCE) or changes.get(lid, {}).get(f"custom.{F_RESOURCE}")
        if v:
            res[v] += 1
    if res:
        print(f"\nResource Tag — {len(res)} distinct values ({sum(res.values()):,} leads):")
        for v, n in res.most_common():
            print(f"  {v[:40]:<40} {n:>7,}")
        near = _near_duplicates(res)
        if near:
            print("\n  ⚠️  Possible drift — these look like variants of each other:")
            for a, b in near:
                print(f"     {a!r}  vs  {b!r}")

    if WRITE_SALES_LANE:
        print("\nSales Team Lane is written from the same roster — retire update_sales_lane.py.")

    if failed:
        print(f"\n⚠️  {len(failed)} bucket quer{'y' if len(failed)==1 else 'ies'} failed: "
              f"{', '.join(failed)}")
        print("   Those leads fell through to a later bucket — DO NOT --apply until fixed.")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        sample = list(changes.items())[:5]
        if sample:
            print("\nSample:")
            for lid, ch in sample:
                print(f"  {by_id[lid].get('display_name','?')[:35]:<35} {ch}")
        return

    if failed:
        sys.exit("\nRefusing to --apply while bucket queries are failing.")

    todo = list(changes.items())
    if args.max_writes:
        todo = todo[:args.max_writes]
        print(f"\n--max-writes {args.max_writes:,}: writing a chunk of "
              f"{len(todo):,} of {len(changes):,}. Re-run to continue "
              f"(the reconciler only ever writes what still differs).")

    print(f"\nApplying to {len(todo):,} leads with {WRITE_WORKERS} workers...")
    ok = err = done = 0
    errors = []

    def _write(item):
        lid, payload = item
        try:
            _req("PUT", f"{BASE}/lead/{lid}/", json=payload)
            return lid, None
        except Exception as e:
            return lid, str(e)

    with ThreadPoolExecutor(max_workers=WRITE_WORKERS) as pool:
        for lid, e in pool.map(_write, todo):
            done += 1
            if e:
                err += 1
                if len(errors) < 20:
                    errors.append(f"  {lid}: {e[:200]}")
            else:
                ok += 1
            if done % 1000 == 0:
                print(f"  {done:,}/{len(todo):,}", file=sys.stderr)

    print(f"\nDone. {ok:,} updated, {err:,} failed.")
    if errors:
        print("First errors:")
        print("\n".join(errors))
    if len(todo) < len(changes):
        print(f"\n{len(changes) - len(todo):,} leads still pending — run again.")


if __name__ == "__main__":
    main()
