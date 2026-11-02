"""Shared lead-age and rep-ladder policy. Pure local calculations only."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import math
from zoneinfo import ZoneInfo

from rep_access import instant
from workflow_holds import custom

POLICY_VERSION = 'anthony-lead-distribution-2026-10-30-v2'
LEGACY_DATE = 'cf_YnLqVPMXx3VKNHqKsk0YK1OYVl07p6hhuUBb2XM18UI'
TIER = 'cf_Mpz1pnxLOSjscFDAErSDYOV0PSjlWKXSgBN4jWd73sM'
ENTRY = 'cf_FI1V2Zw8M0aoSg7yJKJo0wb8NbRU8LafnkgHrpLcfYA'
REAL_OPT_INS = frozenset(('Website-Apply', 'Typeform', 'Webinar', 'Lead-Magnet'))
PACIFIC = ZoneInfo('America/Los_Angeles')


class Tier(str, Enum):
    RAMP = 'Ramp'
    PROVEN = 'Proven'
    TOP = 'Top'
    REACTIVATION = 'Reactivation'


@dataclass(frozen=True)
class AgeClassification:
    tier: str | None
    age_days: int | None
    effective_date: str | None
    date_source: str | None
    genuine_opt_in: bool
    reason: str


def age_band(days):
    if type(days) is not int or days < 0:
        raise ValueError('Lead age must be a nonnegative integer')
    if days <= 7:
        return Tier.TOP.value
    if days <= 29:
        return Tier.PROVEN.value
    if days <= 60:
        return Tier.RAMP.value
    if days <= 179:
        return Tier.PROVEN.value
    if days <= 364:
        return Tier.RAMP.value
    return Tier.REACTIVATION.value


def legacy_calendar_date(value):
    """Match the deployed tagger's date-only, legacy epoch and local-time forms."""
    if type(value) in (int, float):
        try:
            return datetime.fromtimestamp(value, timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if 'T' not in text and ' ' not in text:
        return parsed.date()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=PACIFIC)
    return parsed.astimezone(PACIFIC).date()


def effective_date(lead):
    legacy = custom(lead, LEGACY_DATE)
    day = legacy_calendar_date(legacy)
    if day is not None:
        return day, 'Legacy Create Date'
    created = instant(lead['date_created'])
    source = 'Close date_created (invalid legacy fallback)' if legacy not in (None, '') else 'Close date_created'
    return created.astimezone(PACIFIC).date(), source


def classify_age(lead, now):
    if now.tzinfo is None:
        raise ValueError('Timezone-aware evaluation time required')
    source_value = custom(lead, ENTRY)
    genuine = isinstance(source_value, str) and source_value in REAL_OPT_INS
    try:
        created = instant(lead['date_created'])
        day, source = effective_date(lead)
    except (KeyError, TypeError, ValueError):
        return AgeClassification(None, None, None, None, genuine, 'lead_creation_evidence_unavailable')
    age = (now.astimezone(PACIFIC).date() - day).days
    if created > now or age < 0:
        return AgeClassification(None, age, day.isoformat(), source, genuine, 'future_lead_creation')
    tier = age_band(age)
    reason = 'age_band'
    if tier == Tier.TOP.value and not genuine:
        tier, reason = Tier.RAMP.value, 'fresh_import_or_unknown_source_not_top'
    return AgeClassification(tier, age, day.isoformat(), source, genuine, reason)


def new_hire_week4(rep):
    start = rep.get('live_start_date')
    return (date.fromisoformat(start) + timedelta(days=21)).isoformat() if start else None


def validate_trial_roster(config):
    policy = config.get('trial_policy', {})
    if not policy.get('enabled'):
        return
    seen = set()
    for group in ('core_reps', 'new_hires'):
        for rep in policy[group]:
            uid = rep.get('user_id')
            if uid:
                if not isinstance(uid, str) or not uid.startswith('user_') or uid in seen:
                    raise ValueError('Trial reps require unique Close identities across all segments')
                seen.add(uid)
            if group == 'core_reps' and (not uid or rep.get('arm') not in ('predictive', 'power')):
                raise ValueError('Core reps require an identity and fixed trial arm')
            if rep.get('live_start_date'):
                date.fromisoformat(rep['live_start_date'])


def trial_rep_segment(user_id, arm, at, config):
    policy = config.get('trial_policy', {})
    if not policy.get('enabled'):
        return 'core'
    for rep in policy['core_reps']:
        if user_id and rep.get('user_id') == user_id:
            return 'core' if rep['arm'] == arm else 'wrong_arm'
    for rep in policy['new_hires']:
        if user_id and rep.get('user_id') == user_id:
            day = instant(at).astimezone(ZoneInfo(config['timezone'])).date().isoformat()
            eligible = new_hire_week4(rep)
            if not eligible or day < rep['live_start_date']:
                return 'new_hire_start_unconfirmed'
            if day < eligible:
                return 'new_hire_training' if arm == 'power' else 'new_hire_predictive_too_early'
            return 'new_hire_week4' if arm == 'predictive' else 'new_hire_wrong_method_after_week4'
    return 'outside_roster'


def two_goal_weeks(performance, now):
    if not performance or performance.get('complete') is not True or not performance.get('source'):
        return False
    today = now.astimezone(PACIFIC).date()
    current_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    required = {current_sunday - timedelta(days=7), current_sunday - timedelta(days=14)}
    weeks = {}
    for row in performance.get('weeks', []):
        start = date.fromisoformat(row['week_start'])
        if start in weeks:
            raise ValueError('Duplicate performance week')
        weeks[start] = row
    for start in required:
        row = weeks.get(start)
        if not row or row.get('complete') is not True or row.get('paused') is not False:
            return False
        goal, booked = row.get('goal'), row.get('booked')
        if not numeric(goal) or goal <= 0 or not numeric(booked) or booked < goal:
            return False
    return True


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def conversion_gate(performance, user_id, now):
    if not performance or performance.get('complete') is not True or not performance.get('source'):
        return False
    conversion = performance.get('conversion') or {}
    if conversion.get('complete') is not True or conversion.get('user_id') != user_id:
        return False
    try:
        begin, end = instant(conversion['start']), instant(conversion['end_exclusive'])
    except (KeyError, ValueError):
        return False
    if not begin < end <= now or now - end > timedelta(days=7):
        return False
    dials, booked = conversion.get('dials'), conversion.get('booked')
    return bool(numeric(dials) and dials > 0 and numeric(booked) and booked <= dials and booked * 100 / dials >= 2.5)


def earned_rep_stage(rep, now):
    """Return a local recommendation; never modifies approved access metadata."""
    start = rep.get('live_start_date')
    if not start:
        return {'tier': None, 'live_day': None, 'reason': 'live_start_date_unconfirmed'}
    live_day = (now.astimezone(PACIFIC).date() - date.fromisoformat(start)).days + 1
    if live_day < 1:
        return {'tier': None, 'live_day': live_day, 'reason': 'not_live_yet'}
    performance = rep.get('performance')
    if live_day > 60 and conversion_gate(performance, rep.get('close_user_id'), now):
        return {'tier': 'Top', 'live_day': live_day, 'reason': 'day61_and_conversion_gate'}
    if live_day >= 22 and two_goal_weeks(performance, now):
        return {'tier': 'Proven', 'live_day': live_day, 'reason': 'day22_and_two_completed_goal_weeks'}
    return {'tier': 'Ramp', 'live_day': live_day, 'reason': 'ramp_or_performance_gate_not_met'}
