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

import requests

API_KEY = os.environ.get("CLOSE_API_KEY")
if not API_KEY:
    sys.exit("Set CLOSE_API_KEY first.")

BASE = "https://api.close.com/api/v1"
AUTH = (API_KEY, "")

# ============================================================================
# CONFIG — everything tunable lives here
# ============================================================================

HOT_WINDOW_DAYS = 14      # Setter owns a fresh inbound signal this long
BLITZ_DAYS = 14           # all-angles push after going cold
DEEP_NURTURE_DAYS = 180   # active -> deep

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

def within(field_name, days=None, hours=None, negate=False):
    off = {"years": 0, "months": 0, "weeks": 0, "days": days or 0,
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

def has_completed_meeting(negate=False):
    return {"type": "has_related", "negate": negate,
            "this_object_type": "lead", "related_object_type": "activity.meeting",
            "related_query": {"negate": False, "type": "and", "queries": [
                {"type": "field_condition", "negate": False,
                 "field": {"type": "regular_field", "object_type": "activity.meeting",
                           "field_name": "status"},
                 "condition": {"type": "term", "values": ["completed"]}}]}}

# ---- the buckets, in precedence order (first match wins) -------------------

BUCKETS = [
    ("Suppressed",     _wrap(status_in(SUPPRESS_STATUSES))),
    ("Booked",         _wrap(status_in(SUPPRESS_STATUSES, negate=True),
                             num_range("num_upcoming_meetings", gte=1))),
    ("Hot-Inbound",    _wrap(status_in(SUPPRESS_STATUSES, negate=True),
                             num_range("num_upcoming_meetings", lte=0),
                             status_in([S_NEW]),
                             within("date_created", days=HOT_WINDOW_DAYS))),
    ("Blitz",          _wrap(status_in(SUPPRESS_STATUSES, negate=True),
                             num_range("num_upcoming_meetings", lte=0),
                             status_in([S_LOST, S_NOSHOW, S_CANCELED]),
                             within("last_lead_status_change_date", days=BLITZ_DAYS))),
    ("Active-Nurture", _wrap(status_in(SUPPRESS_STATUSES, negate=True),
                             num_range("num_upcoming_meetings", lte=0),
                             within("last_communication_date", days=DEEP_NURTURE_DAYS))),
    # Deep-Nurture is the fallthrough — anything contactable not caught above.
    # List-Swap is terminal and needs a drip-exhaustion signal we don't have yet.
]
FALLTHROUGH = "Deep-Nurture"

# ============================================================================
# API
# ============================================================================

def _req(method, url, **kw):
    for attempt in range(6):
        r = requests.request(method, url, auth=AUTH, timeout=45, **kw)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("retry-after", 2)) + attempt)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()

def search(query, fields=None, limit=None):
    """Run a Close search, return list of lead dicts."""
    out, cursor = [], None
    body = {"query": query, "_limit": 200}
    if fields:
        body["_fields"] = {"lead": fields}
    while True:
        if cursor:
            body["cursor"] = cursor
        r = _req("POST", f"{BASE}/data/search/", json=body)
        j = r.json()
        out.extend(j.get("data", []))
        cursor = j.get("cursor")
        if not cursor or (limit and len(out) >= limit):
            break
    return out[:limit] if limit else out

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
    args = ap.parse_args()

    read_fields = ["id", "display_name", "status_id",
                   f"custom.{F_OWNER}", f"custom.{F_HANDRAISER}", f"custom.{F_FUNNEL}",
                   f"custom.{F_LOSTREASON}", f"custom.{F_OVERRIDE}",
                   f"custom.{F_STATE}", f"custom.{F_ENTRY}",
                   f"custom.{F_TEAM}", f"custom.{F_ANGLE}",
                   f"custom.{F_RESOURCE}", f"custom.{F_SALESLANE}"]
    if F_EVERCALL:
        read_fields.append(f"custom.{F_EVERCALL}")

    print("Loading leads...", file=sys.stderr)
    everything = _wrap(status_in([], negate=True)) if False else {
        "negate": False, "type": "and",
        "queries": [{"negate": False, "object_type": "lead", "type": "object_type"}]}
    leads = search(everything, fields=read_fields, limit=args.limit)
    print(f"  {len(leads)} leads\n", file=sys.stderr)
    by_id = {l["id"]: l for l in leads}

    # --- state, by precedence ---
    state_of, claimed = {}, set()
    print("Resolving buckets...", file=sys.stderr)
    for name, q in BUCKETS:
        ids = {l["id"] for l in search(q, fields=["id"])}
        fresh = (ids & set(by_id)) - claimed
        for i in fresh:
            state_of[i] = name
        claimed |= fresh
        print(f"  {name:<16} {len(fresh):>7,}", file=sys.stderr)
    for i in by_id:
        state_of.setdefault(i, FALLTHROUGH)
    print(f"  {FALLTHROUGH:<16} {sum(1 for v in state_of.values() if v==FALLTHROUGH):>7,}\n",
          file=sys.stderr)

    # --- ever had call ---
    ever = set()
    if F_EVERCALL:
        print("Resolving Ever Had Call...", file=sys.stderr)
        ever = {l["id"] for l in search(_wrap(has_completed_meeting()), fields=["id"])}
        print(f"  {len(ever):,} leads with a completed meeting\n", file=sys.stderr)

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
        if F_EVERCALL:
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

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        sample = list(changes.items())[:5]
        if sample:
            print("\nSample:")
            for lid, ch in sample:
                print(f"  {by_id[lid].get('display_name','?')[:35]:<35} {ch}")
        return

    print(f"\nApplying to {len(changes):,} leads...")
    ok = err = 0
    for n, (lid, payload) in enumerate(changes.items(), 1):
        try:
            _req("PUT", f"{BASE}/lead/{lid}/", json=payload)
            ok += 1
        except Exception as e:
            err += 1
            print(f"  {lid}: {e}", file=sys.stderr)
        if n % 250 == 0:
            print(f"  {n:,}/{len(changes):,}", file=sys.stderr)
    print(f"\nDone. {ok:,} updated, {err:,} failed.")


if __name__ == "__main__":
    main()
