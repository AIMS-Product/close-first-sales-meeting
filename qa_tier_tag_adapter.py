"""Reuse the shared lead-age policy for QA's existing tier allocator."""
from datetime import datetime, time
from zoneinfo import ZoneInfo
from lead_tier_policy import classify_age


def classify_leads(leads, day):
    now = datetime.combine(day, time.max, ZoneInfo('America/Los_Angeles'))
    return [{'proposed_tier': classify_age(lead, now).tier} for lead in leads]
