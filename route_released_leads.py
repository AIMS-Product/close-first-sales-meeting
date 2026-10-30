#!/usr/bin/env python3
"""Route already-released leads: confirmed attendance to QA, otherwise Lane 2.

The Lane 1 release service owns the first-scheduled-meeting clock and its release
checks. This handoff consumes only its Released-tagged leads staged under Dom.
Native Meeting Outcome is attendance evidence; calendar status alone is not.
"""
import argparse
import json
from collections import Counter
from datetime import datetime, timezone

DOM_ID = "user_hqv8aEy844FqW29HDFof8hPyBiJx1XBxaDkdUtqc1Qp"
NARIA_ID = "user_PBMfAYkPSkMaYK58gXuG70Vu1SLd2bsG3Mvys6RZNgY"
ORGANIZATION_ID = "orga_Qwbx9tyUrl7Jdjurg9xJ8N6w1MN4PD2NQOgIhd8r7rC"
BATCH_FIELD = "cf_IruzyS5PO4dZsOKGQSYb4r7PhOEpw1C82FN17ZlmkQy"
OWNER_FIELD = "cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd"
STATE_FIELD = "cf_hKcyx4tQSMvHd7llfLX363bx3LGXMxVEmFHEtjpR5C2"
OVERRIDE_FIELD = "cf_6PnYz6aaAkLzHMU3Faxz7kFurBEfadqlfGiBgfhjaVC"
COMPLETED_OUTCOME = "outcome_032Djn4dfeNuEoCunojA7K"
WON_OPPORTUNITY_STATUSES = {
    "stat_WnFc0uhjcjV0cc3bVzdFVqDz7av6rbsOmOvHUsO6s03",
    "stat_ZA4DFlp3JeGCpqLVoQxIEGfd3WNxAqdM5RrHQrzrpm6",
}
READ_FIELDS = ["id", "organization_id", "status_id", "date_updated", "num_upcoming_meetings"] + [
    f"custom.{field}" for field in (OWNER_FIELD, STATE_FIELD, OVERRIDE_FIELD, BATCH_FIELD)
]


def custom(lead, field):
    return lead.get(f"custom.{field}", (lead.get("custom") or {}).get(field))


def instant(value):
    if not isinstance(value, str):
        raise ValueError("meeting_timestamp_missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("meeting_timezone_missing")
    return parsed.astimezone(timezone.utc)


def handoff_plan(snapshot, *, now, terminal_statuses, suppressed_statuses):
    """No dates, lead statuses, batches, opportunities or existing QA leads change."""
    lead = snapshot["lead"]
    held = lambda reason: {"destination": "held", "reason": reason, "changes": {}}
    batch = custom(lead, BATCH_FIELD)
    if lead.get("organization_id") != ORGANIZATION_ID:
        return held("organization_mismatch")
    if custom(lead, OWNER_FIELD) != DOM_ID or not isinstance(batch, str) or not batch.startswith("Released - "):
        return held("not_a_staged_release")
    if "STH1" in batch or "STL1" in batch:
        return held("workflow_hold_or_separate_qa_episode")
    if custom(lead, STATE_FIELD) == "QA Triage":
        return held("existing_qa_hold")
    if custom(lead, OVERRIDE_FIELD) not in (None, "", "No"):
        return held("reassignment_override")
    if lead.get("status_id") in suppressed_statuses:
        return held("protected_lead_status")
    if any(o.get("status_type") == "won" or o.get("status_id") in WON_OPPORTUNITY_STATUSES
           for o in snapshot["opportunities"]):
        return held("won_opportunity")
    if snapshot.get("complete") is not True:
        return held("incomplete_meeting_or_opportunity_read")
    if lead.get("num_upcoming_meetings") is None:
        return held("upcoming_meeting_count_missing")
    if lead["num_upcoming_meetings"] > 0:
        return held("future_meeting")
    attended = False
    for meeting in snapshot["meetings"]:
        try:
            starts_at = instant(meeting.get("starts_at"))
        except (TypeError, ValueError):
            return held("meeting_date_unverified")
        status = meeting.get("status", "")
        if status == "in-progress" or (starts_at > now and status in ("upcoming", "completed")):
            return held("future_or_in_progress_meeting")
        if starts_at <= now and meeting.get("outcome_id") == COMPLETED_OUTCOME:
            attended = True
    # Current no-show/cancellation status always returns to Lane 2, even if an
    # older meeting was completed. This preserves the existing terminal policy.
    terminal = lead.get("status_id") in terminal_statuses
    state = snapshot["lane2_state"]
    if state == "Booked" and not terminal:
        return held("active_deal_protection")
    if state in ("QA Triage", "Suppressed"):
        return held("protected_lane2_state")
    if attended and not terminal:
        return {"destination": "qa", "reason": "confirmed_completed_meeting",
                "changes": {f"custom.{OWNER_FIELD}": NARIA_ID,
                            f"custom.{STATE_FIELD}": "QA Triage"}}
    if state == "Booked":
        state = "Active-Nurture"
    return {"destination": "lane2", "reason": "terminal_status" if terminal else "no_confirmed_completed_meeting",
            "changes": {f"custom.{OWNER_FIELD}": None, f"custom.{STATE_FIELD}": state}}


class HandoffReader:
    def __init__(self, lane2):
        self.lane2 = lane2

    def get(self, path, params=None):
        return self.lane2._req("GET", self.lane2.BASE + path, params=params).json()

    def candidates(self):
        owner_query = {"type": "field_condition", "field": {"type": "custom_field", "custom_field_id": OWNER_FIELD},
                       "condition": {"type": "reference", "reference_type": "user", "object_ids": [DOM_ID]}}
        rows = self.lane2._search_page(self.lane2._wrap(owner_query), fields=READ_FIELDS,
                                      limit=self.lane2.SKIP_CAP)
        if len(rows) >= self.lane2.SKIP_CAP:
            raise RuntimeError("staged_release_search_incomplete")
        return [row for row in rows if isinstance(custom(row, BATCH_FIELD), str)
                and custom(row, BATCH_FIELD).startswith("Released - ")]

    def related(self, path, lead_id, fields):
        rows, seen = [], set()
        while True:
            page = self.get(path, {"lead_id": lead_id, "_skip": len(rows), "_limit": 100, "_fields": fields})
            batch = page.get("data")
            if not isinstance(batch, list) or not isinstance(page.get("has_more"), bool):
                raise RuntimeError("related_pagination_unverified")
            for row in batch:
                if row.get("lead_id") != lead_id or not row.get("id") or row["id"] in seen:
                    raise RuntimeError("related_scope_or_identity_mismatch")
                seen.add(row["id"])
            rows.extend(batch)
            if not page["has_more"]:
                return sorted(rows, key=lambda row: row["id"])
            if not batch or len(rows) >= self.lane2.SKIP_CAP:
                raise RuntimeError("related_read_incomplete")

    def snapshot(self, lead_id):
        before = self.get(f"/lead/{lead_id}/", {"_fields": ",".join(READ_FIELDS)})
        meetings = self.related("/activity/meeting/", lead_id,
                                "id,lead_id,starts_at,ends_at,status,outcome_id")
        opportunities = self.related("/opportunity/", lead_id, "id,lead_id,status_id,status_type")
        ids = {"type": "id", "value": lead_id}
        state = self.lane2.FALLTHROUGH
        for _label, candidate_state, query in self.lane2.BUCKETS:
            if self.lane2.search(self.lane2.add_condition(query, ids), fields=["id"], limit=2):
                state = candidate_state
                break
        after = self.get(f"/lead/{lead_id}/", {"_fields": ",".join(READ_FIELDS)})
        if before != after or after.get("id") != lead_id:
            raise RuntimeError("lead_changed_during_handoff_read")
        return {"lead": after, "meetings": meetings, "opportunities": opportunities,
                "lane2_state": state, "complete": True}

    def preflight(self):
        field = self.get(f"/custom_field/lead/{STATE_FIELD}/")
        if "QA Triage" not in field.get("choices", []):
            raise RuntimeError("qa_triage_choice_unavailable")
        organization = self.get(f"/organization/{ORGANIZATION_ID}/")
        members = {member.get("user_id", (member.get("user") or {}).get("id"))
                   for member in organization.get("memberships", [])}
        if not {DOM_ID, NARIA_ID}.issubset(members):
            raise RuntimeError("handoff_membership_unverified")

    def write_once(self, lead_id, changes):
        # Do not use the GET retry helper for a mutation with uncertain outcome.
        import requests
        response = requests.put(self.lane2.BASE + f"/lead/{lead_id}/", auth=self.lane2.AUTH,
                                json=changes, timeout=45)
        response.raise_for_status()
        actual = self.get(f"/lead/{lead_id}/", {"_fields": ",".join(READ_FIELDS)})
        for key, expected in changes.items():
            if custom(actual, key.removeprefix("custom.")) != expected:
                raise RuntimeError("handoff_readback_mismatch")


def run(reader, *, apply=False, max_writes=None):
    candidates = reader.candidates()
    if candidates:
        reader.preflight()
    counts, written = Counter(), 0
    for lead in candidates:
        lead_id = lead["id"]
        snapshot = reader.snapshot(lead_id)
        plan = handoff_plan(snapshot, now=datetime.now(timezone.utc),
                            terminal_statuses=reader.lane2.QA_TRIAGE_RELEASE_STATUSES,
                            suppressed_statuses=reader.lane2.SUPPRESS_STATUSES)
        counts[plan["destination"]] += 1
        if not plan["changes"]:
            counts["held_" + plan["reason"]] += 1
            continue
        if apply and (max_writes is None or written < max_writes):
            fresh = reader.snapshot(lead_id)
            fresh_plan = handoff_plan(fresh, now=datetime.now(timezone.utc),
                                      terminal_statuses=reader.lane2.QA_TRIAGE_RELEASE_STATUSES,
                                      suppressed_statuses=reader.lane2.SUPPRESS_STATUSES)
            if fresh != snapshot or fresh_plan != plan:
                counts["changed_before_write"] += 1
                continue
            reader.write_once(lead_id, plan["changes"])
            written += 1
    result = {"mode": "apply" if apply else "dry_run", "staged_candidates": len(candidates),
              "destinations": dict(counts), "verified_writes": written}
    print(json.dumps(result, sort_keys=True))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-writes", type=int)
    args = parser.parse_args()
    if args.max_writes is not None and args.max_writes < 0:
        parser.error("--max-writes must be non-negative")
    import lane2_state
    run(HandoffReader(lane2_state), apply=args.apply, max_writes=args.max_writes)


if __name__ == "__main__":
    main()
