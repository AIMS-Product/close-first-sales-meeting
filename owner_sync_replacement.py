"""Plan the staged owner-sync replacement with zero provider writes."""
from close_status_policy import NONRESPONSIVE, ORG, OWNER, held_hash, timestamp
from lane2_policy import F_OVERRIDE, F_STATE, SUPPRESS_STATUSES
from sync_lead_owner_to_opp import WON_OPP_STATUS_IDS, owner_needs_update, read_custom
from tier_assignment import BLOCKED_STATUSES
from workflow_holds import BATCH, custom

WORKFLOW_ID = 'seq_3lhd6gq9obgJZDUVrF2D0g'
OPPORTUNITY_OWNER_MIRROR = 'cf_pmPlmHdimUejnQehnZxQfwHkZf5NadfQlx6UA5B35I2'
RECOVERY_STATUS = 'cf_yTdPnk0OSnyPenIIAuCG2BHJIBS1G4mwIkditQRTMtz'
REST_STATUSES = {NONRESPONSIVE, 'stat_QaUUQJHyfv4tyvA6G8lmyIP6btkoiNsLBQ3rGxTybAm'}
RECOVERY_LEAD_STATUSES = frozenset((
 'stat_lGHxEKwhbVswuchbpRo6XcMMSXz0fV4CID9qFWT8KCO',
 'stat_5CqIgNJnGYO357zXjSnH6BAkKyoCvYUOBxVvpYfDMZn',
 'stat_hWIGHjzyNpl4YjIFSFz3VK4fp2ny10SFJLKAihmo4KT',
 'stat_aP51Sa9Qv3IthtjKGhvtEriEiLgEB9WS7uvX1w5RP08',
 'stat_2SmOUMCp1vDFJF0TcJ011hNnpLYWDGwugyo4JyiRMEP'))

PROTECTED_STATUSES = BLOCKED_STATUSES | SUPPRESS_STATUSES | RECOVERY_LEAD_STATUSES | REST_STATUSES
MIRROR_LABEL = 'Opportunity Owner (Opp)'


def legacy_opportunity_plans(lead):
    """Match the current script's dry-run planner, before its write guards."""
    owner = read_custom(lead, OWNER, 'Lead Owner')
    override = read_custom(lead, F_OVERRIDE, 'Reassignment Override')
    if not owner or override == 'Yes':
        return set()
    return {opp['id'] for opp in lead.get('opportunities') or []
            if opp.get('status_id') not in WON_OPP_STATUS_IDS
            and owner_needs_update(opp.get('user_id'), owner)}


def replacement_plan(lead, closer_ids, exclusions):
    if lead.get('organization_id') != ORG or not lead.get('id','').startswith('lead_'):
        raise ValueError('Lead organization/identity mismatch')
    owner = custom(lead, OWNER)
    if owner is None:
        return 'no_lead_owner', {}, None
    if not isinstance(owner,str) or not owner.startswith('user_'):
        return 'lead_owner_unverified', {}, None
    opps = lead.get('opportunities')
    if not isinstance(opps,list):
        return 'opportunity_evidence_unavailable', {}, None
    if not opps:
        return 'awaiting_opportunity', {}, None
    ids = [opp.get('id') for opp in opps]
    if len(set(ids)) != len(ids) or any(
        not isinstance(opp.get('id'),str) or not opp['id'].startswith('oppo_')
        or opp.get('organization_id') != ORG or opp.get('lead_id') != lead['id']
        or not opp.get('status_id') or opp.get('status_type') not in ('active','won','lost')
        or 'user_id' not in opp for opp in opps
    ):
        return 'opportunity_evidence_unavailable', {}, None
    if held_hash(lead['id']) in exclusions:
        return 'explicit_lead_hold', {}, None
    if custom(lead,F_OVERRIDE) not in (None,'','No',False):
        return 'reassignment_override', {}, None
    if owner in closer_ids:
        return 'closer_is_lead_owner', {}, None
    if not lead.get('status_id'):
        return 'lead_status_unverified', {}, None
    if custom(lead,F_STATE)=='QA Triage' or lead['status_id'] in PROTECTED_STATUSES:
        return 'protected_lead_status', {}, None
    if 'STH1' in str(custom(lead,BATCH) or '') or lead.get('durable_hold') or lead.get('uncertain_write'):
        return 'shared_or_unconfirmed_write_hold', {}, None
    if custom(lead, RECOVERY_STATUS) in ('Active', 'Rebooked', 'Needs review'):
        return 'saved_booking_recovery', {}, None
    if lead.get('future_meeting_index') is not False:
        return ('upcoming_meeting' if lead.get('future_meeting_index') else 'meeting_evidence_unavailable'), {}, None
    if any(opp['status_id'] in WON_OPP_STATUS_IDS or opp['status_type']=='won'
           or 'won' in str(opp.get('status_label','')).casefold() for opp in opps):
        return 'won_opportunity', {}, None
    if any(opp['user_id'] in closer_ids for opp in opps):
        return 'closer_owned_opportunity', {}, None
    changes = {opp['id']:owner for opp in opps if owner_needs_update(opp['user_id'],owner)}
    try:
        dated = [(timestamp(opp['date_created']),opp['id']) for opp in opps]
    except (KeyError,TypeError,ValueError):
        return 'newest_opportunity_evidence_unavailable', {}, None
    newest_at = max(at for at,_ in dated)
    if sum(at==newest_at for at,_ in dated)!=1:
        return 'newest_opportunity_ambiguous', {}, None
    mirror=custom(lead,OPPORTUNITY_OWNER_MIRROR)
    if mirror == owner or mirror == [owner]:
        return 'sync_opportunity_only' if changes else 'aligned', changes, None
    return 'sync_opportunity_and_mirror' if changes else 'sync_mirror_only', changes, owner
