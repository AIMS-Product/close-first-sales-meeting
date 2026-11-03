"""Tier eligibility and Close queue candidates. No network or write operations.

Consumes existing Lead Tier tags. Source restrictions and overflow affect who can
receive an unowned lead; an existing owner keeps their lead as its tag ages.
"""
from dataclasses import dataclass
from datetime import datetime,timedelta

from close_status_policy import OWNER,ORG,held_hash
from rep_access import parse_access,instant
from workflow_holds import BATCH,custom,no_sales_hold_query
from lead_tier_policy import REAL_OPT_INS

TIER='cf_Mpz1pnxLOSjscFDAErSDYOV0PSjlWKXSgBN4jWd73sM'
ENTRY='cf_FI1V2Zw8M0aoSg7yJKJo0wb8NbRU8LafnkgHrpLcfYA'
BUSINESS='cf_aJlNlilQZIgLLuhcymNN8fiOzewnFxrbWjLZFPmsucO'
OVERRIDE='cf_6PnYz6aaAkLzHMU3Faxz7kFurBEfadqlfGiBgfhjaVC'
BLOCKED_STATUSES=frozenset((
 'stat_0oW3iRpVp9z5DJq0cuwI1HgR0XhHAhykEPPIq4TFsxd',
 'stat_U9MI7pqsvIjceTv3pCU7b1EghO8Q83h1HUcL6fGVyi6',
 'stat_p3oblSTnbsyDAw4rWqZDePGYMOlKBgV2FjbqIMDrfvF',
 'stat_YV4ZngDB4IGjLjlOf0YTFEWuKZJ6fhNxVkzQkvKYfdB',
 'stat_QaUUQJHyfv4tyvA6G8lmyIP6btkoiNsLBQ3rGxTybAm'))
VP='Vendingpreneurs (VP)'


@dataclass(frozen=True)
class LeadFacts:
    lead:dict
    evidence:object
    outgoing_dials:tuple=()
    holds:tuple|None=None
    explicitly_excluded:bool=False
    calls_complete:bool=True
    tier_freshness_verified:bool=False


@dataclass(frozen=True)
class LeadDecision:
    state:str
    reason:str
    stored_tier:str|None=None
    assignment_tier:str|None=None
    overflow:bool=False


def lead_owners(lead):
    value=custom(lead,OWNER)
    if value is None:return ()
    if isinstance(value,str):return (value,)
    if isinstance(value,list) and all(isinstance(x,str) for x in value):return tuple(sorted(set(value)))
    raise ValueError('Malformed Lead Owner')


def stored_tier(lead):
    value=custom(lead,TIER)
    if isinstance(value,list):
        if len(value)!=1:return None
        value=value[0]
    return value if value in ('Ramp','Proven','Top','Reactivation') else None


def protect(facts,roster,now):
    lead,evidence=facts.lead,facts.evidence
    if lead.get('organization_id')!=ORG:return 'lead_tenant_mismatch'
    if not roster.valid(now):return 'fresh_active_closer_roster_required'
    if facts.explicitly_excluded:return 'explicit_held_lead'
    if set(lead_owners(lead)) & roster.closer_ids:return 'active_closer_owner'
    if lead.get('status_id') in BLOCKED_STATUSES:return 'protected_status'
    if 'STH1' in str(custom(lead,BATCH) or ''):return 'shared_hold_marker'
    if facts.holds is None:return 'durable_hold_verification_unavailable'
    if facts.holds:return 'shared_durable_hold'
    if not evidence.complete:return 'incomplete_protection_evidence'
    if evidence.suppressions:return 'stop_dnc_or_unsubscribe'
    if evidence.review_reasons:return 'protection_review_required'
    if evidence.future_meeting:return 'upcoming_meeting'
    for opportunity in evidence.opportunities:
        label=opportunity.label.lower()
        if opportunity.status_type=='won' or 'won' in label or 'do not contact' in label:return 'won_or_dnc_opportunity'
        if opportunity.status_type=='active' and opportunity.owner in roster.closer_ids:return 'active_closer_opportunity'
    if custom(lead,OVERRIDE)=='Yes':return 'reassignment_override'
    return None


def classify_for_assignment(facts,roster,now):
    reason=protect(facts,roster,now)
    tier=stored_tier(facts.lead)
    if reason:return LeadDecision('excluded',reason,tier)
    if lead_owners(facts.lead):return LeadDecision('retain_owner','existing_owner_keeps_lead',tier,tier)
    if tier is None:return LeadDecision('excluded','missing_or_ambiguous_lead_tier')
    if not facts.tier_freshness_verified:return LeadDecision('excluded','lead_tier_stale_or_unverified',tier)
    # Reuse the original October 14 source allowlist. No age tag is overwritten.
    if tier=='Top' and custom(facts.lead,ENTRY) not in REAL_OPT_INS:
        return LeadDecision('candidate','top_source_guard_routes_to_ramp',tier,'Ramp')
    if tier=='Top':
        if not facts.calls_complete:return LeadDecision('excluded','dial_history_incomplete',tier)
        created=instant(facts.lead['date_created'])
        if created>now:return LeadDecision('excluded','future_created_at',tier)
        undialed=not any(created<=at<=now for at in facts.outgoing_dials)
        overflow=undialed and now-created>=timedelta(hours=24)
        return LeadDecision('candidate','undialed_fresh_overflow' if overflow else 'fresh_top_opt_in',tier,tier,overflow)
    return LeadDecision('candidate','existing_lead_tier',tier,tier)


def rep_available(rep,now,active_memberships):
    """Certification opens assignment; legacy readiness flags are informational."""
    access=parse_access(rep.get('proposed_access'))
    return bool(rep.get('identity_ready') and rep.get('certified') is True
        and rep['close_user_id'] in active_memberships and access and access.lead_tiers(now))


def eligible_reps(facts,decision,reps,now,active_memberships):
    if decision.state!='candidate':return []
    offer=custom(facts.lead,BUSINESS) or VP
    result=[]
    for rep in reps:
        if rep.get('offer')!=offer or not rep_available(rep,now,active_memberships):continue
        access=parse_access(rep['proposed_access'])
        allowed=decision.assignment_tier in access.lead_tiers(now)
        if decision.overflow and access.base_tier.value=='Proven':allowed=True
        if allowed:result.append(rep)
    return result


def allocate_sample(items,reps,roster,now,active_memberships,counts,max_queue=1000):
    """Keep the existing queue target and round-robin deficit allocation pattern.

    Candidate eligibility replaces the Recapture State ordering. Existing owners
    are never removed or transferred by this planner. Counts are supplied once.
    """
    if max_queue<1:raise ValueError('Positive working-queue target required')
    order=[rep['close_user_id'] for rep in reps if rep_available(rep,now,active_memberships)
           and rep['close_user_id'] in counts]
    deficits={owner:max(0,max_queue-counts[owner]) for owner in order}
    order.sort(key=lambda owner:(-deficits[owner],owner))
    pointer=0;proposals=[]
    for facts in items:
        decision=classify_for_assignment(facts,roster,now)
        eligible=eligible_reps(facts,decision,reps,now,active_memberships)
        allowed={rep['close_user_id'] for rep in eligible}
        target=None
        for offset in range(len(order)):
            index=(pointer+offset)%len(order);owner=order[index]
            if owner in allowed and deficits[owner]>0:
                target=owner;pointer=(index+1)%len(order);deficits[owner]-=1;break
        proposals.append({'lead_id':facts.lead['id'],'decision':decision.__dict__,
            'eligible_reps':[rep['rep'] for rep in eligible], 'target_owner':target,
            'field_changes':{'custom.'+OWNER:{'before':None,'after':target}} if target else {},
            'capacity_unverified_for':[rep['rep'] for rep in eligible if rep['close_user_id'] not in counts],
            'capacity_blocked':bool(eligible) and target is None})
    return proposals


def field(fid,condition,negate=False):
    return {'type':'field_condition','negate':negate,'field':{'type':'custom_field','custom_field_id':fid},'condition':condition}


def regular(name,condition,negate=False,object_type='lead'):
    return {'type':'field_condition','negate':negate,
            'field':{'type':'regular_field','object_type':object_type,'field_name':name},'condition':condition}


def no_closer_deal(closer_ids):
    return {'type':'has_related','negate':True,'this_object_type':'lead','related_object_type':'opportunity',
        'related_query':{'type':'and','queries':[
            regular('status_type',{'type':'term','values':['active','won']},object_type='opportunity'),
            regular('user_id',{'type':'reference','reference_type':'user_or_group','object_ids':sorted(closer_ids)},object_type='opportunity')]}}


def owned_queue(offer,closer_ids,extra=()):
    """No lead-tier gate here: aging never removes the current owner's work."""
    business=field(BUSINESS,{'type':'term','values':[offer]})
    if offer==VP:
        business={'type':'or','queries':[business,field(BUSINESS,{'type':'exists'},True)]}
    return {'type':'and','queries':[
        {'type':'object_type','object_type':'lead'},business,no_sales_hold_query(),
        field(OWNER,{'type':'reference','reference_type':'user_or_group','object_ids':['CURRENT_USER']}),
        field(OWNER,{'type':'reference','reference_type':'user_or_group','object_ids':sorted(closer_ids)},True),
        regular('status_id',{'type':'reference','reference_type':'status.lead','object_ids':sorted(BLOCKED_STATUSES)},True),
        regular('num_upcoming_meetings',{'type':'number_range','lte':0}),no_closer_deal(closer_ids),
        regular('timezone_id',{'type':'local_time','on_or_after':'08:00:00','before':'21:00:00'}),*extra]}


def view_candidates(offers,closer_ids):
    """Generate exact new definitions; original views remain untouched in preview."""
    views=[]
    for offer in sorted(offers):
        short={'Vendingpreneurs (VP)':'VP','The Land Geek (TLG)':'TLG','Publishing Profits Academy (PPA)':'PPA','Acquisition Ace (AA)':'AA'}.get(offer,offer)
        views.append({'name':short+' · My Assigned Leads','query':owned_queue(offer,closer_ids),
            'sort':[{'direction':'asc','field':{'type':'regular_field','object_type':'lead','field_name':'last_communication_date'}}],
            'is_user_dependent':True,'activation':'off',
            'ownership_rule':'Existing owners retain access across age-band changes; holds and protections still apply.'})
    return views
