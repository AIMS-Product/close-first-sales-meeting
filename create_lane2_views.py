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

# Imported rather than redeclared — the Lane 1 roster and the Closer-active
# opportunity statuses must not drift from the reconciler's definition of them.
# has_opp_status also carries the `status_id` (not `opp_status_id`) fix.
from lane2_state import LANE_1, LANE1_OPP_STATUSES, has_opp_status

# ---------------------------------------------------------------------------
# Sharing
#
# Close models this two ways and they're mutually exclusive in practice:
#   whole_org = True            -> everyone in the org
#   user_ids  = [...]           -> only these people (is_shared stays False)
#
# The view owner (whoever's API key runs this) always sees them regardless.
# ---------------------------------------------------------------------------
# Org-wide. Safe because the seven PERSONAL views carry Lead Owner = CURRENT_USER,
# so everyone sees the same view NAME but only their own leads. Verified against
# existing views in this account (e.g. "CRM Field Check - WTD" is whole_org=True
# AND is_user_dependent=True).
SHARE_WHOLE_ORG = True

SHARE_WITH = [
    "user_lUjlATIIgFg8mELa0GFzZUj0lG4Cs7PwQsxbi34I6Su",   # Joe Dysert
    "user_Xoi7ztP2y5IeCIRhObWx8E6eKZO6B9GvPze5WMAgP1e",   # Jess Mayo
    "user_hqv8aEy844FqW29HDFof8hPyBiJx1XBxaDkdUtqc1Qp",   # Dom Ellis

    # --- Lane 2 reps — uncomment at rollout so they can work their lists ---
    # "user_ZNKG1S9eI71qxhSozBK4jskTVtJqXzfNCPWqmADRR9F",   # William Nowak (Setter)
    # "user_0SuNg0OWd2reYMeyuDVqiVvjiGcRiFheKKOXXZpyaPZ",   # Pearl Sathekge (Setter)
    # "user_dQi0iL0igjCKtEXPSsv8ALDZMAz9orJxL60O7Q921jy",   # Vince Bartolini (Scraper)
    # "user_IeWR2TlhpjqoXy3K6jX7u9C8c83iBnHXSIvFZpotF3z",   # Jacob Hepner (Scraper)
    # "user_lXtgDE8eKS8s3tKDQrl8eUP7tYCXuNNJATddPUkuLlQ",   # Becca Leier (Scraper)
    # "user_p2y1gLbIgUb9xognGTvuXoRpzp4Ro8QkO20ltgF1CvJ",   # Jacob Herbig (Scraper)
    # "user_MrBLkl5wCqTm7QxHxPo2ydNV5KxMllg6YZDVc12Aqzj",   # Jason Aaron (Lane 2 lead)
]

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

def cool_off(channels=("call",)):
    """
    Hide a lead from the dialer for N hours after an outbound touch.

    ONLY used on the shared Pool views now. On an owned list it was solving a
    problem ownership already solves — two reps on one lead — while creating a
    real one: a rep who dials a lead couldn't see them again for 12 hours, even
    to try a second number the same afternoon. Pacing now comes from queue ORDER
    (least-recently-contacted first), which leaves the lead callable instead of
    hiding it. See the sort direction on the nurture views.

    Defaults to CALLS ONLY. SMS was in here and had to come out: bulk SMS is a
    marketing motion, not evidence a human is working the lead, so a blast was
    silently pulling thousands of leads out of the dial lists for 12 hours.

    Email was never included, for the same reason.
    """
    return {"negate": True, "type": "or",
            "queries": [has_outgoing(k, hours=COOL_OFF_HOURS) for k in channels]}

def has_incoming(kind, days):
    """Lead contacted US by this channel in the last N days."""
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
    """A fresh hand-raise, any channel."""
    return {"negate": False, "type": "or",
            "queries": [has_incoming(k, days) for k in ("sms", "email", "call")]}

def has_outgoing(kind, hours=None):
    """We contacted THEM by this channel — optionally only within the last N hours."""
    ot = f"activity.{kind}"
    inner = [{"type": "field_condition", "negate": False,
              "field": {"type": "regular_field", "object_type": ot,
                        "field_name": "direction"},
              "condition": {"type": "term", "values": ["outgoing"]}}]
    if hours:
        off = {"years": 0, "months": 0, "weeks": 0, "days": 0,
               "hours": hours, "minutes": 0, "seconds": 0}
        inner.append({"type": "field_condition", "negate": False,
                      "field": {"type": "regular_field", "object_type": ot,
                                "field_name": "date_created"},
                      "condition": {"type": "moment_range", "before": {"type": "now"},
                                    "on_or_after": {"type": "offset", "direction": "past",
                                                    "moment": {"type": "now"}, "offset": off,
                                                    "which_day_end": "start"}}})
    return {"type": "has_related", "negate": False,
            "this_object_type": "lead", "related_object_type": ot,
            "related_query": {"negate": False, "type": "and", "queries": inner}}

def never_touched():
    """No outbound call, SMS or email has EVER gone to this lead."""
    return {"negate": True, "type": "or",
            "queries": [has_outgoing(k) for k in ("sms", "email", "call")]}

def negated(cond):
    """Flip a condition block's negate flag."""
    c = json.loads(json.dumps(cond))
    c["negate"] = not c.get("negate", False)
    return c

def has_completed_meeting(negate=False):
    """Live check — does NOT depend on the stamped Ever Had Call field."""
    return {"type": "has_related", "negate": negate,
            "this_object_type": "lead", "related_object_type": "activity.meeting",
            "related_query": {"negate": False, "type": "and", "queries": [
                {"type": "field_condition", "negate": False,
                 "field": {"type": "regular_field", "object_type": "activity.meeting",
                           "field_name": "status"},
                 "condition": {"type": "term", "values": ["completed"]}}]}}

REENGAGE_DAYS = 7

F_OWNER = "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd"   # Lead Owner (custom, not assigned_to)

def mine():
    """
    Lead Owner = whoever is looking at the view.

    This is how two reps stop working the same lead: one shared view definition,
    but each person only ever sees their own book. Close resolves CURRENT_USER at
    read time — the view must also be flagged is_user_dependent.
    """
    return {"type": "field_condition", "negate": False,
            "field": {"type": "custom_field", "custom_field_id": F_OWNER},
            "condition": {"type": "reference", "reference_type": "user_or_group",
                          "object_ids": ["CURRENT_USER"]}}

def unclaimed():
    """No Lead Owner at all — the team pool nobody has picked up yet."""
    return {"type": "field_condition", "negate": True,
            "field": {"type": "custom_field", "custom_field_id": F_OWNER},
            "condition": {"type": "exists"}}

def mine_or_unclaimed():
    """
    Lead Owner = me, OR nobody owns it.

    For the shared warm-reply lists. They can't be strictly personal — an unowned
    hand-raise has to stay visible to whoever can act on it — but with no owner
    filter at all they showed EVERY rep's book, which is the one place two people
    could genuinely dial the same person.

    This is also a complete Lane 1 exclusion on its own: if the lead is mine I'm
    not a Closer, and if it's unowned no Closer holds it. That's why the Lane 1
    guard drops its ownership clause on these views.
    """
    return {"negate": False, "type": "or", "queries": [mine(), unclaimed()]}


def owner_in(user_ids, negate=False):
    return {"type": "field_condition", "negate": negate,
            "field": {"type": "custom_field", "custom_field_id": F_OWNER},
            "condition": {"type": "reference", "reference_type": "user_or_group",
                          "object_ids": list(user_ids)}}

def not_lane1(include_owner=True):
    """
    Hard gate against dialling into a deal a Closer is actively working.

    `include_owner=False` drops the ownership clause. Use it on any view that
    ALREADY pins Lead Owner — the personal views (Lead Owner = CURRENT_USER) and
    the Pool views (Lead Owner not present). On those, the clause cannot change
    the result: if the lead is mine it is by definition not a Closer's, and if it
    is unowned the first OR branch is trivially true. Worse, it *reads* as a
    contradiction — "Lead Owner = Me" sitting above "Lead Owner not present" —
    which is exactly the sort of thing that gets "fixed" by someone later.

    Two independent clauses, deliberately belt-and-braces:

      1. No opportunity in a Closer-active status (Contract Sent / Follow Up /
         Reschedule). This is a LIVE check on the opportunity, so it does not
         wait on the hourly reconciler — which matters, because the whole risk
         window is the gap between runs.

      2. Lead Owner is not a Lane 1 rep.

    Clause 2 is written as "unclaimed OR not-a-closer" rather than a bare
    negation. A negated reference condition may or may not match a lead whose
    Lead Owner is EMPTY, and most of the hot cohort is unowned — getting that
    wrong would silently empty the very lists this protects. Spelling out the
    empty case makes it correct either way.

    Deliberately NOT keyed on Recapture State = Booked. Brand-new leads have no
    state stamp yet (the reconciler runs hourly), so a state-based exclusion
    would drop exactly the sub-1-hour leads the SLA views exist to catch. Same
    bug we already fixed once on Ever Had Call.
    """
    no_live_deal = negated(has_opp_status(LANE1_OPP_STATUSES))
    if not include_owner:
        return no_live_deal
    return {"negate": False, "type": "and", "queries": [
        no_live_deal,
        {"negate": False, "type": "or", "queries": [
            unclaimed(),
            owner_in(LANE_1, negate=True),
        ]},
    ]}

NOT_SUPPRESSED = status_in(SUPPRESS, negate=True)
# REMOVED 2026-08-12 — "no upcoming meeting" is no longer filtered on any view.
#
# Every dial view was returning 0 leads while the underlying data was fine.
# Stephen isolated it in the UI: with the meetings condition present the view
# returned 0; removing it alone returned 156. Both authored forms failed the
# same way — `lte: 0` and `NOT(gte: 1)` — so this is not a matter of writing the
# condition differently. In the smart-view engine a lead that has never had a
# meeting appears to carry NO value for `num_upcoming_meetings` (not 0), and any
# condition on that field drops those leads regardless of negation.
#
# Nothing is lost by removing it:
#   - the 9 state-based views can't show booked leads anyway. The reconciler
#     buckets any lead with an upcoming meeting into Recapture State = "Booked",
#     and none of these views select that state.
#   - the live Lane 1 opportunity guard still hides anything a Closer is on.
# The only residual exposure is the live-criteria Setter views (SLA < 1 hour,
# Warm Reply, SLA Breach), where a lead that booked minutes ago can linger for
# one cycle. That is a small annoyance next to empty call lists.
#
# `gte: 1` (positive, no negation) is still used by "Setter · Booked — Confirm
# & Disco" — that direction selects leads that DO have a value, so it is not
# affected by the same hole. Verify that view has rows after any change here.
NO_UPCOMING = None   # not applied to any view — see above

def view(*conds):
    """Wrap conditions into a saved-search query. Suppression is always applied."""
    return {"negate": False, "type": "and", "queries": [
        {"negate": False, "object_type": "lead", "type": "object_type"},
        {"negate": False, "type": "and", "queries": [NOT_SUPPRESSED, *conds]},
    ]}

# SORT_ASC_RATIONALE
#
# Any view sorted on `last_communication_date` must sort **asc**, never desc.
#
# `last_communication_date` counts OUTBOUND as well as inbound. Sorted desc, the
# moment a rep calls a lead that lead jumps to the top of the list — so the rep's
# own work resurfaces in front of them and the untouched leads sink out of sight.
# Charlie hit this on 2026-08-13: "mine are all ones I've contacted this past few
# days." Exactly the reported symptom, on exactly the three desc-sorted views.
#
# asc gives the behaviour a work queue needs: a lead you just called sinks to the
# bottom, and whatever you haven't touched rises. Within an inbound window it also
# reads as SLA order — the reply that has been waiting longest is first, which is
# the one about to be dropped. Membership on these lists is already inbound-only
# (any_inbound / stamped Hot-Inbound), so asc cannot surface a stale lead.
#
# Views sorted on `date_created` or `last_lead_status_change_date` are NOT affected
# — rep activity does not move those fields, so desc is correct there.
#
# ONE deliberate exception: `Scraper · Pool — Unclaimed Dormant` stays desc.
# Nobody owns those leads, so no rep's outbound is inflating the sort, and the view
# carries a 12h call/SMS cool-off that suppresses anything just touched. Warmest
# recent contact first is the right order for a dip-in pool. If the cool-off is ever
# removed from that view, this exception has to be revisited.
def sort_by(field_name, direction="desc"):
    return [{"direction": direction,
             "field": {"type": "regular_field", "object_type": "lead",
                       "field_name": field_name}}]

# Lead Owner is a column on EVERY L2 view. Dom added it by hand across the set on
# 2026-08-13; baking it in here is what stops the next `create_lane2_views.py` run
# from silently reverting that. This script rewrites columns wholesale — anything
# added in the Close UI is lost on the next apply unless it lives in this list.
#
# Worth having even on the PERSONAL views, where every row is the viewer: it makes
# the ownership filter visible rather than something reps have to take on trust,
# and it is the first thing to check when someone asks "why am I seeing this lead?"
DIAL_COLS = ["display_name", "primary_phone", "primary_email", "status_id",
             F_OWNER, "date_created"]

def cols(*extra):
    # Custom-field columns use the bare cf_ id — NOT "custom.<id>". The prefixed
    # form is 53 chars and Close caps selected_fields.field_id at 48.
    seen, out = set(), []
    for f in list(DIAL_COLS) + list(extra):
        if f in seen:            # a view passing F_OWNER again must not duplicate it
            continue
        seen.add(f)
        out.append({"type_id": "lead", "field_id": f})
    return out

# ============================================================================
# THE VIEW SET
# ============================================================================
#
# PERSONAL views get `Lead Owner = CURRENT_USER` appended and are flagged
# is_user_dependent — one definition, but each rep only sees their own book.
# That's what stops two people dialling the same lead.
#
# Everything else stays SHARED on purpose:
#   - Warm Reply / SLA Breach are small and speed-critical — race them.
#   - The SLA (<1hr) view can't be personal: brand-new leads have no owner yet.
#   - Pool views exist precisely to show unowned work.
#   - Ops views are for managers.
# Views only these people should see. An escalation list in a rep's sidebar is a
# collision risk: it shows leads that are, by definition, nobody's yet.
MANAGERS = [
    "user_MrBLkl5wCqTm7QxHxPo2ydNV5KxMllg6YZDVc12Aqzj",   # Jason Aaron (Lane 2 manager)
    "user_Xoi7ztP2y5IeCIRhObWx8E6eKZO6B9GvPze5WMAgP1e",   # Jess Mayo
    "user_hqv8aEy844FqW29HDFof8hPyBiJx1XBxaDkdUtqc1Qp",   # Dom Ellis
]
# Stephen owns the API key, so he sees these regardless of what's listed here.
MANAGER_ONLY = {
    "🚨 SLA BREACH — Untouched Hot Inbound",
    # The four Ops · views are described as manager views but are still shared
    # org-wide. Add them here to make that true — left out for now because it
    # changes who can see what, which is your call not mine.
}

# Every view gets the Lane 1 guard appended automatically — safe by default, so
# a view added later is protected without anyone remembering to do it. These are
# the deliberate opt-outs:
NO_LANE1_GUARD = {
    # The whole point of this one is the Lane 1 handoff — Setter confirms the
    # meeting and runs disco before the Closer takes the call.
    "Setter · Booked — Confirm & Disco",
    # Audit/reporting views must show the true picture, including Booked.
    "Ops · Deep Nurture (6mo+ quiet)",
    "Ops · Unassigned Recapture Universe",
    "Ops · Recapture State — Audit",
}

# Shared lists that still must not show another rep's book. Get "mine OR unowned"
# and are flagged user-dependent so CURRENT_USER resolves at read time.
MINE_OR_UNCLAIMED = {
    "⚡ Warm Reply — TODAY",
    "Warm Backlog — 2 to 7 days",
    # Safe here too, and strictly better: a sub-1-hour lead is unowned, so it
    # still shows to everyone. Once the setter assigner gives it an owner it
    # drops out of the other Setter's list instead of both racing for it.
    "Setter · Hot Inbound — SLA (< 1 hour)",
}

PERSONAL = {
    "Scraper · Hot Inbound — Re-engaged (work first)",
    "Setter · Hot Inbound — All",
    "Setter · Booked — Confirm & Disco",
    "Scraper · Blitz — No-Show Recovery (work first)",
    "Scraper · Blitz — Lost & Re-engaged",
    "Scraper · VendHub Downsell (Price + DIY)",
    "Scraper · Active Nurture",
    "Scraper · Deep Nurture — Revival Dials",
}

VIEWS = [
    # ---------------- The one arrow back up ----------------
    ("⚡ Warm Reply — TODAY",
     "They contacted US in the last 24 hours and nothing is booked. The hottest cohort we have and "
     "historically the biggest leak. Same-day SLA — this list should self-clear overnight. "
     "Never-called goes to a Setter, had-a-call to a Scraper.",
     view(any_inbound(1), calling_hours()),
     # asc, NOT desc — see the note above SORT_ASC_RATIONALE below.
     sort_by("last_communication_date", "asc"), cols(F_STATE, F_TEAM, F_EVERCALL, F_ANGLE)),

    ("Warm Backlog — 2 to 7 days",
     "Contacted us in the last week but not in the last 24h — i.e. they slipped past the same-day "
     "SLA. Work after the TODAY list is clear. If this is consistently large, the same-day queue "
     "isn't being cleared.",
     view(any_inbound(REENGAGE_DAYS), negated(any_inbound(1)), calling_hours()),
     sort_by("last_communication_date", "asc"), cols(F_STATE, F_TEAM, F_EVERCALL, F_ANGLE)),

    # ---------------- Setter lane ----------------
    ("🚨 SLA BREACH — Untouched Hot Inbound",
     "New leads 1h-14d old, never spoken to us, and never called/texted/emailed by us. Not a "
     "worklist — an escalation list. Anything here is being dropped. Empty is the target. "
     "Uses live criteria, not the stamped state, so nothing hides behind reconciler lag.",
     view(has_completed_meeting(negate=True),
          within("date_created", days=14),
          within("date_created", hours=1, negate=True),
          never_touched(), calling_hours()),
     sort_by("date_created", "asc"), cols(F_TEAM, F_ENTRY, F_RESOURCE)),

    ("Setter · Hot Inbound — SLA (< 1 hour)",
     "Fresh hand-raise, under an hour old, never spoken to us. Work this first. "
     "Deliberately uses LIVE criteria, not Recapture State or Ever Had Call — the reconciler runs "
     "hourly, so a lead five minutes old has no stamp yet and a state-based filter would miss "
     "exactly the leads this view exists to catch.",
     view(has_completed_meeting(negate=True),
          within("date_created", hours=1), calling_hours()),
     sort_by("date_created"), cols(F_ENTRY, F_RESOURCE)),

    ("Setter · Hot Inbound — All",
     "The full Setter hot window, read from Recapture State rather than re-derived. This is what "
     "makes the re-engagement arrow visible: an older never-called lead who replies is re-stamped "
     "Hot-Inbound and appears here, which a created-date filter would never have caught.",
     view(choice(F_STATE, ["Hot-Inbound"]), calling_hours()),
     sort_by("date_created"), cols(F_ENTRY, F_RESOURCE, F_EVERCALL)),

    ("Setter · Booked — Confirm & Disco",
     "Meeting on the calendar. Setter confirms and runs a quick disco before the Lane 1 call — "
     "our biggest no-show-prevention lever.",
     view(num_range("num_upcoming_meetings", gte=1)),
     sort_by("date_created"), cols(F_ENTRY)),

    # ---------------- Scraper lane ----------------
    ("Scraper · Hot Inbound — Re-engaged (work first)",
     "Leads YOU own who just raised their hand again and have never spoken to us. The re-engagement "
     "arrow: a dormant lead fills in a form or replies and jumps straight back to the top. Hottest "
     "thing in your book — work it before Blitz. Previously these sat in no Scraper list at all.",
     view(choice(F_STATE, ["Hot-Inbound"]), calling_hours()),
     sort_by("last_communication_date", "asc"), cols(F_ENTRY, F_RESOURCE, F_EVERCALL, F_ANGLE)),

    # These two are DISJOINT and together cover all of Blitz. Work Recovery first.
    ("Scraper · Blitz — No-Show Recovery (work first)",
     "The no-show and cancellation slice of Blitz. Different opener from a lost deal — they never "
     "heard the pitch. Disjoint from Lost & Re-engaged, so no double-dialling.",
     view(choice(F_STATE, ["Blitz"]), status_in([S_NOSHOW, S_CANCELED]),
          calling_hours()),
     sort_by("last_lead_status_change_date"), cols(F_ENTRY, F_ANGLE)),

    ("Scraper · Blitz — Lost & Re-engaged",
     "The rest of Blitz: recently lost deals, plus anyone who had a call and has just re-engaged. "
     "Read from Recapture State, so re-engaged leads actually surface here.",
     view(choice(F_STATE, ["Blitz"]),
          status_in([S_NOSHOW, S_CANCELED], negate=True), calling_hours()),
     sort_by("last_lead_status_change_date"), cols(F_ANGLE, F_ENTRY, F_EVERCALL)),

    ("Scraper · VendHub Downsell (Price + DIY)",
     "Showed and didn't close on price, financing, DIY or a competitor. The only objection cut we "
     "trust today. Overlaps your other lists on purpose — same person, better-known objection. "
     "Least-recently-contacted first.",
     view(choice(F_ANGLE, ["Price", "DIY"]),
          choice(F_EVERCALL, ["Yes"]), calling_hours()),
     sort_by("last_communication_date", "asc"), cols(F_ANGLE, F_RESOURCE)),

    ("Scraper · Active Nurture",
     "Didn't book inside the Blitz window. Steady re-engagement: dials plus weekly marketing. Sorted least-recently-contacted first, so someone you just called sinks to the bottom instead of resurfacing — call as often as you judge right, the queue paces itself.",
     view(choice(F_STATE, ["Active-Nurture"]), calling_hours()),
     sort_by("last_communication_date", "asc"), cols(F_ANGLE, F_ENTRY)),

    ("Scraper · Deep Nurture — Revival Dials",
     "6mo+ quiet AND assigned to a rep. The dial list for the cold/stale universe — the Ops "
     "Deep Nurture view is the whole 29k cohort, this is only what's owned and workable now.",
     # NO exists(F_TEAM) here. It used to require Owner Team to be populated,
     # which made this view lag the assigner by up to an hour: a rep dealt 1,000
     # Deep-Nurture leads saw NOTHING until the next reconciler run stamped their
     # team. Owner Team is derived from Lead Owner anyway, so `mine()` already
     # proves ownership — the check was redundant as well as harmful.
     # (Cost us the evening before go-live on 2026-08-12. Don't add it back.)
     view(choice(F_STATE, ["Deep-Nurture"]), calling_hours()),
     sort_by("last_communication_date", "asc"), cols(F_ANGLE, F_ENTRY, F_RESOURCE)),

    # ---------------- Team pools — unowned work, anyone can claim ----------------
    ("Setter · Pool — Unclaimed Hot Inbound",
     "Hot inbound with NO owner yet. Shared on purpose — this is where new leads land before "
     "anyone picks them up. Claim by setting yourself as Lead Owner, and it moves into your "
     "personal Hot Inbound list.",
     view(choice(F_STATE, ["Hot-Inbound"]), unclaimed(),
          cool_off(("call", "sms")), calling_hours()),
     sort_by("date_created"), cols(F_ENTRY, F_RESOURCE)),

    ("Scraper · Pool — Unclaimed Dormant",
     "Blitz and Active-Nurture leads with NO owner. The overflow bench: work this when your own "
     "lists are clear. Claim by setting yourself as Lead Owner.",
     view(choice(F_STATE, ["Blitz", "Active-Nurture"]), unclaimed(),
          cool_off(("call", "sms")), calling_hours()),
     sort_by("last_communication_date"), cols(F_STATE, F_ANGLE, F_ENTRY)),

    # ---------------- Ops / marketing ----------------
    ("Ops · Deep Nurture (6mo+ quiet)",
     "No communication in 6+ months. Intended source list for the low-touch content drip "
     "(Instantly) once that sync is built — nothing exports yet. Mixes 'went quiet after "
     "engaging' with 'imported, never contacted' — segment before sending.",
     view(choice(F_STATE, ["Deep-Nurture"])),
     sort_by("last_communication_date", "asc"), cols(F_ENTRY, F_RESOURCE)),

    ("Ops · Unassigned Recapture Universe",
     "In a working bucket but owned by nobody on either team. This is the staffing question, "
     "not a tooling one. Catches both an empty Owner Team and an explicit 'None' — a lead that "
     "loses a roster owner gets set to None rather than blanked.",
     view({"negate": False, "type": "or",
           "queries": [exists(F_TEAM, negate=True), choice(F_TEAM, ["None"])]},
          choice(F_STATE, ["Hot-Inbound", "Blitz", "Active-Nurture"])),
     sort_by("date_created"), cols(F_STATE, F_TEAM, F_ANGLE)),

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
    ap.add_argument("--prune", action="store_true",
                    help="also DELETE any 'L2 · ' view no longer defined here "
                         "(e.g. left behind by a rename)")
    args = ap.parse_args()

    # Pre-flight: validate everything BEFORE any API call, so a limit violation
    # can't leave half the view set created.
    MAX_DESC, MAX_FIELD_ID = 300, 48
    problems = []
    for entry in VIEWS:
        short, desc, _q, _s, selected = entry[:5]
        if len(desc) > MAX_DESC:
            problems.append(f"{short}: description {len(desc)} chars (max {MAX_DESC})")
        for f in selected:
            if len(f["field_id"]) > MAX_FIELD_ID:
                problems.append(f"{short}: field_id {f['field_id']} "
                                f"is {len(f['field_id'])} chars (max {MAX_FIELD_ID})")
    if problems:
        print("Validation failed — nothing sent to Close:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        sys.exit(1)

    if SHARE_WHOLE_ORG:
        print("Sharing: WHOLE ORG\n", file=sys.stderr)
    else:
        print(f"Sharing: {len(SHARE_WITH)} named user(s) + the view owner\n", file=sys.stderr)

    print("Reading existing views...", file=sys.stderr)
    have = existing_views()

    created = updated = 0
    for entry in VIEWS:
        short, desc, query, sort, selected = entry[:5]
        personal = short in PERSONAL
        mine_or_pool = short in MINE_OR_UNCLAIMED
        mgr = short in MANAGER_ONLY
        user_dependent = personal or mine_or_pool

        query = json.loads(json.dumps(query))          # never mutate the source
        # Detected, not hand-listed: any view that already constrains Lead Owner
        # gets the deal-check only. A new view picks the right form by itself.
        pins_owner = user_dependent or (F_OWNER in json.dumps(query))
        if short not in NO_LANE1_GUARD:
            query["queries"][1]["queries"].append(not_lane1(include_owner=not pins_owner))
        if personal:
            query["queries"][1]["queries"].append(mine())
        elif mine_or_pool:
            query["queries"][1]["queries"].append(mine_or_unclaimed())

        share_org = SHARE_WHOLE_ORG and not mgr
        name = PREFIX + short
        payload = {
            "name": name,
            "description": desc,
            "type": "lead",
            "s_query": {"query": query, "results_limit": None, "sort": sort},
            "selected_fields": selected,
            "is_user_dependent": user_dependent,
            "is_shared": share_org,
            "sharing_settings": {
                "whole_org": share_org,
                "user_ids": MANAGERS if mgr else ([] if share_org else SHARE_WITH),
                "group_ids": [],
            },
        }
        found = have.get(name)
        action = "UPDATE" if found else "CREATE"
        tags = []
        if personal: tags.append("mine-only")
        if mine_or_pool: tags.append("mine + unowned")
        if mgr: tags.append("MANAGERS ONLY")
        if short in NO_LANE1_GUARD: tags.append("no lane1 guard")
        suffix = f"   [{', '.join(tags)}]" if tags else ""
        print(f"  {action:<7} {name}{suffix}")
        if not args.apply:
            continue
        if found:
            _req("PUT", f"{BASE}/saved_search/{found['id']}/", json=payload)
            updated += 1
        else:
            _req("POST", f"{BASE}/saved_search/", json=payload)
            created += 1

    print(f"\n{len(VIEWS)} views defined.")

    defined = {PREFIX + v[0] for v in VIEWS}
    stale = [(n, v["id"]) for n, v in have.items()
             if n.startswith(PREFIX) and n not in defined]

    if stale:
        verb = "DELETING" if (args.apply and args.prune) else "STALE"
        print(f"\n{len(stale)} view(s) carry the '{PREFIX}' prefix but are no longer "
              f"defined here — {verb}:")
        for n, vid in stale:
            print(f"  {n}")
        if args.apply and args.prune:
            for n, vid in stale:
                _req("DELETE", f"{BASE}/saved_search/{vid}/")
            print(f"  → {len(stale)} deleted.")
        elif args.apply:
            print("  (re-run with --prune to delete them)")

    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply.")
    else:
        print(f"\n{created} created, {updated} updated.")


if __name__ == "__main__":
    main()
