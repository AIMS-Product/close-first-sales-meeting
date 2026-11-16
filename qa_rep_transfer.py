"""Owner-only QA staffing transfer; no new admission or changes to earned credit."""
from qa_identity import QA_REP_ID, LEGACY_QA_REP_ID
from route_released_leads import ORGANIZATION_ID, OWNER_FIELD, STATE_FIELD, custom

TRANSFER_POLICY = 'qa-rep-handoff-tara-20261116-v1'
OWNER_SINCE_FIELD = 'cf_2tuoNM0K9ND9na49hMsj02KI7732MuKSiFX2thJuIgU'
OWNERSHIP_FIELDS = frozenset((OWNER_FIELD, 'Lead Owner', OWNER_SINCE_FIELD, 'Current Owner Since'))


def preserved_fields(lead):
    """Preserve business fields; the owner-history worker records the real transfer."""
    values = dict(lead.get('custom') or {})
    values.update({key[7:]: value for key, value in lead.items() if key.startswith('custom.')})
    # Native Close returns each custom field by ID and display name. The existing
    # owner worker may update its own timestamp, never the separate QA entry clock.
    for key in OWNERSHIP_FIELDS:
        values.pop(key, None)
    return {'id': lead['id'], 'organization_id': lead['organization_id'],
            'status_id': lead['status_id'], 'custom': values}


def transfer_changes(lead):
    if (lead.get('organization_id') != ORGANIZATION_ID
            or custom(lead, OWNER_FIELD) != LEGACY_QA_REP_ID
            or custom(lead, STATE_FIELD) != 'QA Triage'):
        raise ValueError('not_current_legacy_qa')
    return {f'custom.{OWNER_FIELD}': QA_REP_ID}


def verify_transfer(before, after):
    if (custom(after, OWNER_FIELD) != QA_REP_ID
            or (after.get('custom', {}).get('Lead Owner', QA_REP_ID) != QA_REP_ID)
            or preserved_fields(before) != preserved_fields(after)):
        raise RuntimeError('qa_transfer_readback_mismatch')
