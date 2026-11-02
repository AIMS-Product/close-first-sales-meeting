"""Rep assignment access, separate from lead age tags and certification.

This phase prepares classifications only. Existing assigners do not consume this
module until the reviewed roster and the routing phase are explicitly adopted.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

METADATA_KEY = 'leadAssignmentAccess'
SCHEMA_VERSION = 1


class BaseTier(str, Enum):
    RAMP = 'Ramp'
    PROVEN = 'Proven'
    TOP = 'Top'


@dataclass(frozen=True)
class RepAccess:
    base_tier: BaseTier
    reactivation_access: bool
    approved_by: str
    approved_at: datetime
    effective_from: datetime

    def lead_tiers(self, now):
        """Cumulative access; Reactivation is an independent additional permission."""
        if now.tzinfo is None:
            raise ValueError('Timezone-aware evaluation time required')
        if now < self.effective_from or now < self.approved_at:
            return ()
        tiers = ['Ramp']
        if self.base_tier in (BaseTier.PROVEN, BaseTier.TOP):
            tiers.append('Proven')
        if self.base_tier == BaseTier.TOP:
            tiers.append('Top')
        if self.reactivation_access:
            tiers.append('Reactivation')
        return tuple(tiers)


def instant(raw):
    if not isinstance(raw, str):
        raise ValueError('Timestamp must be text')
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Timestamp timezone required')
    return value.astimezone(timezone.utc)


def parse_access(raw):
    """Absent or pending values grant no new permissions; malformed values raise."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get('schemaVersion') != SCHEMA_VERSION:
        raise ValueError('Invalid rep-access schema')
    if raw.get('state') == 'pending':
        return None
    if raw.get('state') != 'approved':
        raise ValueError('Invalid rep-access approval state')
    tier = BaseTier(raw['baseTier'])
    reactivation = raw['reactivationAccess']
    if type(reactivation) is not bool:
        raise ValueError('Reactivation access must be explicit Yes/No')
    approver = raw.get('approvedBy')
    if not isinstance(approver, str) or not approver.strip():
        raise ValueError('Approved access requires a named approver')
    return RepAccess(tier, reactivation, approver.strip(), instant(raw['approvedAt']),
                     instant(raw['effectiveFrom']))


def pending_access():
    return {'schemaVersion': SCHEMA_VERSION, 'state': 'pending', 'baseTier': None,
            'reactivationAccess': None, 'approvedBy': None, 'approvedAt': None,
            'effectiveFrom': None}


def prepare_review(source, configured_roster):
    """Combine current company identities with existing CRM readiness without inference."""
    by_provider = {row['user_id']: row for row in configured_roster if row.get('user_id')}
    rows = []
    for row in source['rows']:
        if row['status'] != 'active' or row['sales_team_lane'] != 2:
            continue
        accounts = [account['provider_user_id'] for account in row['close_accounts']
                    if account['status'] == 'active']
        identity_ready = len(accounts) == 1 and accounts[0].startswith('user_')
        configured = by_provider.get(accounts[0]) if identity_ready else None
        cohort = row.get('cohort_settings') or {}
        configuration = row.get('configuration') or {}
        raw_access = row['possible_access_metadata'].get(METADATA_KEY)
        try:
            parsed = parse_access(raw_access)
            issue = None
        except (KeyError, TypeError, ValueError) as error:
            parsed, issue = None, str(error)
        rows.append({'company_user_id': row['company_user_id'], 'rep': row['display_name'],
            'close_user_id': accounts[0] if identity_ready else None,
            'identity_ready': identity_ready,
            'offer': configuration.get('offer') or row.get('legacy_offer'),
            'start_date': cohort.get('startDate') if 'startDate' in cohort else row.get('legacy_start_date'),
            'certified': cohort.get('certified'),
            'crm_ready_for_leads': configured['ready_for_leads'] if configured else None,
            'crm_roster_match': configured is not None,
            'current_access': raw_access,
            'access_record_valid': parsed is not None,
            'access_issue': issue,
            'proposed_access': raw_access if parsed else pending_access()})
    return sorted(rows, key=lambda row: ((row['offer'] or ''), row['rep'].casefold()))


def apply_approved_classifications(source, review, approval):
    """Stage exact approved identities; never reactivate an exited rep.

    Match only a unique normalized full name in the current company roster.
    Source permission and certification remain independent from this decision.
    """
    if approval.get('companyId') != source.get('company_id'):
        raise ValueError('Tier approval company differs from roster company')
    recorded_at = instant(approval['recordedAt'])
    by_name = {}
    for row in source['rows']:
        key = ' '.join(row['display_name'].split()).casefold()
        by_name.setdefault(key, []).append(row)
    review_by_id = {row['company_user_id']: row for row in review}
    updated = [dict(row) for row in review]
    updated_by_id = {row['company_user_id']: row for row in updated}
    exclusions, seen = [], set()
    for decision in approval['reps']:
        name = decision['rep']
        key = ' '.join(name.split()).casefold()
        if key in seen:
            raise ValueError('Duplicate tier approval identity: ' + name)
        seen.add(key)
        matches = by_name.get(key, [])
        access = {'schemaVersion': 1, 'state': 'approved',
                  'baseTier': decision['baseTier'], 'reactivationAccess': decision['reactivationAccess'],
                  'approvedBy': approval['approvalReportedBy'] if decision.get('overrideEvidence') else approval['approvedBy'],
                  'approvedAt': recorded_at.isoformat(),
                  'effectiveFrom': recorded_at.isoformat(),
                  'approvalReportedBy': approval['approvalReportedBy'],
                  'approvalTimestampMeaning': 'User confirmation recorded; original approval date unavailable',
                  'originalApprovalAt': approval.get('originalApprovalAt'),
                  'approvalEvidence': decision.get('overrideEvidence') or approval['approvalEvidence']}
        parse_access(access)
        if len(matches) != 1:
            exclusions.append({'rep': name, 'reason': 'roster_identity_missing_or_ambiguous',
                               'classification': decision})
            continue
        matched = matches[0]
        if matched['company_user_id'] not in review_by_id:
            exclusions.append({'rep': name, 'reason': 'inactive_or_outside_lane2',
                               'roster_status': matched['status'],
                               'cohort_settings': matched.get('cohort_settings'),
                               'classification': decision})
            continue
        row = updated_by_id[matched['company_user_id']]
        if not row['identity_ready']:
            exclusions.append({'rep': name, 'reason': 'close_identity_not_unique',
                               'classification': decision})
            continue
        row.update(proposed_access=access, classification_source=(
            'Direct correction from Dom' if decision.get('overrideEvidence') else 'Anthony-approved list confirmed by Dom'))
    return updated, exclusions
