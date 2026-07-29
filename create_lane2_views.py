#!/usr/bin/env python3
"""
Create (or update) the Lane 2 smart views in Close.

The bucket definitions here are the SAME query logic `lane2_state.py` uses to
stamp Recapture State. That's deliberate:

    dial views      = live queries  -> real-time, no reconciler lag
    Recapture State = stamped label -> nurture routing, exports, reporting

Because both come from one definition they can't disagree, and a Setter never
waits on a reconciler run to see a lead that arrived two minutes ago.

Idempotent: matches on view name, updates in place if it exists, creates if not.
Never touches a view it didn't create.

    python3 create_lane2_views.py            # dry run — prints what it would do
    python3 create_lane2_views.py --apply
"""

import argparse
import json
import os
import sys

import requests

API_KEY = os.environ.get("CLOSE_API_KEY")
if not API_KEY:
    sys.exit("Set CLOSE_API_KEY first.")

BASE = "https://api.close.com/api/v1"
AUTH = (API_KEY, "")

PREFIX = "L2 · "          # every view this script owns starts with this
SHARE_VIEWS = True        # visible to the whole org, not just the API user

# ---- field / status ids ----------------------------------------------------
F_STATE = "cf_hKcyx4tQSMvHd7llfLX363bx3LGXMxVEmFHEtjpR5C2"
F_TEAM = "cf_3SkrryGiRGT83UHgsBOXpUYbVSyc9RmKKfmKY1uWWib"
F_ANGLE = "cf_6jVIeFyPI8qeQbZSo3ZoaKpjeeLD7du6k2nec8tNPZi"
F_ENTRY = "cf_FI1V2Zw8M0aoSg7yJKJo0wb8NbRU8LafnkgHrpLcfYA"
F_EVERCALL = "cf_6g8pVOlyVciSucCIvbI3CjKsPE4nlJEaKUX0DY9H5XY"
F_RESOURCE = "cf_PWAGlTAxZ62ybFh01xcEzVl0RHw6KUXHp4g4YFb2PgT"

S_LOST = "stat_aR2jBa8YnTNZmHAnPsnlQuinBdaXpSBCkZGP3UvoBlV"
S_NOSHOW = "stat_5CqIgNJnGYO357zXjSnH6BAkKyoCvYUOBxVvpYfDMZn"
S_CANCELED = "stat_hWIGHjzyNpl4YjIFSFz3VK4fp2ny10SFJLKAihmo4KT"
S_WON = "stat_0oW3iRpVp9z5DJq0cuwI1HgR0XhHAhykEPPIq4TFsxd"
S_DNC = "stat_U9MI7pqsvIjceTv3pCU7b1EghO8Q83h1HUcL6fGVyi6"
S_DQ = "stat_p3oblSTnbsyDAw4rWqZDePGYMOlKBgV2FjbqIMDrfvF"
S_OUTSIDE_US = "stat_YV4ZngDB4IGjLjlOf0YTFEWuKZJ6fhNxVkzQkvKYfdB"
SUPPRESS = [S_WON, S_DNC, S_DQ, S_OUTSIDE_US]

COOL_OFF_HOURS = 12       # hide from the dialer this long after an outbound touch
CALL_WINDOW = ("08:00:00", "21:00:00")   # lead's own local time

# ---- condition builders ---------------------------------------------------

def status_in(ids, negate=False):
    return {"type": "field_condition", "negate": negate,
            "field": {"type": "regular_field", "object_type": "lead", "field_name": "status_id"},
            "condition": {"type": "reference", "reference_type": "status.lead", "object_ids": ids}}

def choice(fid, values, negate=False):
    return {"type": "field_condition", "negate": negate,
            "field": {"type": "custom_field", "custom_field_id": fid},
            "condition": {"type": "term", "values": values}}

def exists(fid, negate=False):
    return {"type": "field_condition", "negate": negate,
            "field": {"type": "custom_field", "custom_field_id": fid},
            "condition": {"type": "exists"}}

def within(field_name, days=None, hours=None, months=None, negate=False):
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

def calling_hours():
    """Only surface a lead when it's a civilised hour where they live."""
    return {"type": "field_condition", "negate": False,
            "field": {"type": "regular_field", "object_type": "lead", "field_name": "timezone_id"},
            "condition": {"type": "local_time",
                          "on_or_after": CALL_WINDOW[0], "before": CALL_WINDOW[1]}}

def cool_off():
    return within("last_outgoing_call_date", hours=COOL_OFF_HOURS, negate=True)

NOT_SUPPRESSED = status_in(SUPPRESS, negate=True)
NO_UPCOMING = num_range("num_upcoming_meetings", lte=0)

def view(*conds):
    """Wrap conditions into a saved-search query. Suppression is always applied."""
    return {"negate": False, "type": "and", "queries": [
        {"negate": False, "object_type": "lead", "type": "object_type"},
        {"negate": False, "type": "and", "queries": [NOT_SUPPRESSED, *conds]},
    ]}

def sort_by(field_name, direction="desc"):
    return [{"direction": direction,
             "field": {"type": "regular_field", "object_type": "lead",
                       "field_name": field_name}}]

DIAL_COLS = ["display_name", "primary_phone", "primary_email", "status_id", "date_created"]

def cols(*extra):
    return [{"type_id": "lead", "field_id": f} for f in DIAL_COLS] + \
           [{"type_id": "lead", "field_id": f"custom.{f}"} for f in extra]

# ============================================================================
# THE VIEW SET
# ============================================================================

VIEWS = [
    # ---------------- Setter lane ----------------
    ("Setter · Hot Inbound — SLA (< 1 hour)",
     "Fresh hand-raise, under an hour old, never spoken to us. Same-day-hot: work this first.",
     view(NO_UPCOMING, choice(F_EVERCALL, ["No"]),
          within("date_created", hours=1), calling_hours()),
     sort_by("date_created"), cols(F_ENTRY, F_RESOURCE)),

    ("Setter · Hot Inbound — All (< 14 days)",
     "The full Setter hot window: created within 14 days, no meeting held, nothing booked.",
     view(NO_UPCOMING, choice(F_EVERCALL, ["No"]),
          within("date_created", days=14), cool_off(), calling_hours()),
     sort_by("date_created"), cols(F_ENTRY, F_RESOURCE)),

    ("Setter · Booked — Confirm & Disco",
     "Meeting on the calendar. Setter confirms and runs a quick disco before the Lane 1 call — "
     "our biggest no-show-prevention lever.",
     view(num_range("num_upcoming_meetings", gte=1)),
     sort_by("date_created"), cols(F_ENTRY)),

    # ---------------- Scraper lane ----------------
    ("Scraper · Blitz — All Angles (14 day)",
     "Just went cold: lost, no-showed or cancelled in the last 14 days. Max intensity window.",
     view(NO_UPCOMING, status_in([S_LOST, S_NOSHOW, S_CANCELED]),
          within("last_lead_status_change_date", days=14), cool_off(), calling_hours()),
     sort_by("last_lead_status_change_date"), cols(F_ANGLE, F_ENTRY)),

    ("Scraper · Blitz — No-Show Recovery",
     "The no-show slice of Blitz. Different opener from a lost deal — they never heard the pitch.",
     view(NO_UPCOMING, status_in([S_NOSHOW, S_CANCELED]),
          within("last_lead_status_change_date", days=14), cool_off(), calling_hours()),
     sort_by("last_lead_status_change_date"), cols(F_ENTRY)),

    ("Scraper · VendHub Downsell (Price + DIY)",
     "Showed and didn't close on price, financing, DIY or a competitor. The only objection cut we "
     "trust today — safe to work before the transcript backfill lands.",
     view(NO_UPCOMING, choice(F_ANGLE, ["Price", "DIY"]),
          choice(F_EVERCALL, ["Yes"]), cool_off(), calling_hours()),
     sort_by("last_communication_date"), cols(F_ANGLE, F_RESOURCE)),

    ("Scraper · Active Nurture",
     "Didn't book inside the Blitz window. Steady re-engagement: dials plus weekly marketing.",
     view(NO_UPCOMING, choice(F_STATE, ["Active-Nurture"]), cool_off(), calling_hours()),
     sort_by("last_communication_date"), cols(F_ANGLE, F_ENTRY)),

    # ---------------- Ops / marketing ----------------
    ("Ops · Deep Nurture (6mo+ quiet)",
     "No communication in 6+ months. Intended as the source list for the low-touch content drip "
     "(Instantly) once that sync is built — nothing is exporting yet. NOTE: this cohort mixes "
     "'went quiet after engaging with us' and 'imported and never contacted', which want different "
     "treatment. Segment before sending.",
     view(choice(F_STATE, ["Deep-Nurture"])),
     sort_by("last_communication_date", "asc"), cols(F_ENTRY, F_RESOURCE)),

    ("Ops · Unassigned Recapture Universe",
     "In a working bucket but owned by nobody on either team. This is the staffing question, "
     "not a tooling one.",
     view(exists(F_TEAM, negate=True),
          choice(F_STATE, ["Hot-Inbound", "Blitz", "Active-Nurture"])),
     sort_by("date_created"), cols(F_STATE, F_ANGLE)),

    ("Ops · Recapture State — Audit",
     "Every non-suppressed lead with its state and team. Use to eyeball the reconciler.",
     view(exists(F_STATE)),
     sort_by("date_created"), cols(F_STATE, F_TEAM, F_ANGLE, F_ENTRY, F_EVERCALL)),
]

# ============================================================================

def _req(method, url, **kw):
    r = requests.request(method, url, auth=AUTH, timeout=45, **kw)
    if r.status_code >= 400:
        try:
            detail = json.dumps(r.json(), indent=1)[:1200]
        except Exception:
            detail = r.text[:1200]
        raise SystemExit(f"{r.status_code} from {url}\n{detail}")
    return r


def existing_views():
    out, skip = {}, 0
    while True:
        j = _req("GET", f"{BASE}/saved_search/",
                 params={"_skip": skip, "_limit": 100}).json()
        for v in j.get("data", []):
            out[v.get("name", "")] = v
        if not j.get("has_more"):
            break
        skip += 100
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="create/update in Close")
    args = ap.parse_args()

    print("Reading existing views...", file=sys.stderr)
    have = existing_views()

    created = updated = 0
    for short, desc, query, sort, selected in VIEWS:
        name = PREFIX + short
        payload = {
            "name": name,
            "description": desc,
            "type": "lead",
            "s_query": {"query": query, "results_limit": None, "sort": sort},
            "selected_fields": selected,
            "is_shared": SHARE_VIEWS,
        }
        found = have.get(name)
        action = "UPDATE" if found else "CREATE"
        print(f"  {action:<7} {name}")
        if not args.apply:
            continue
        if found:
            _req("PUT", f"{BASE}/saved_search/{found['id']}/", json=payload)
            updated += 1
        else:
            _req("POST", f"{BASE}/saved_search/", json=payload)
            created += 1

    print(f"\n{len(VIEWS)} views defined.")
    if not args.apply:
        print("DRY RUN — nothing changed. Re-run with --apply.")
        stale = [n for n in have if n.startswith(PREFIX)
                 and n not in {PREFIX + v[0] for v in VIEWS}]
        if stale:
            print(f"\n{len(stale)} view(s) carry the '{PREFIX}' prefix but are no longer "
                  f"defined here — delete by hand if unwanted:")
            for n in stale:
                print(f"  {n}")
    else:
        print(f"{created} created, {updated} updated.")


if __name__ == "__main__":
    main()
