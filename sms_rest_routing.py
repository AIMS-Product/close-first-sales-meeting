"""Reuse the certified Lane 2 roster and queue-deficit allocation for returns."""
from lane2_roster import OFFER_VP, assignment_roster
from workflow_holds import no_sales_hold_query
from close_status_policy import OWNER


def ready_reps():
    roster, missing = assignment_roster(OFFER_VP)
    if missing: raise ValueError('Certified Lane 2 ready roster has unresolved users')
    return roster


def select_target(client, *, preferred=None, max_queue=1000):
    """Count only target queues, server-side; no broad lead or activity scan."""
    from assign_lane2_leads import build_deficits, PRIORITY_STATES
    from lane2_state import F_STATE, SUPPRESS_STATUSES, status_in
    roster = ready_reps()
    counts = {}
    for owner in roster:
        query = {'type':'and','queries':[
            {'type':'object_type','object_type':'lead'},no_sales_hold_query(),
            status_in(SUPPRESS_STATUSES,negate=True),
            {'type':'field_condition','field':{'type':'custom_field','custom_field_id':OWNER},
             'condition':{'type':'reference','reference_type':'user_or_group','object_ids':[owner]}},
            {'type':'field_condition','field':{'type':'custom_field','custom_field_id':F_STATE},
             'condition':{'type':'term','values':PRIORITY_STATES}}]}
        result=client.request('POST','/data/search/',{'query':query,'_fields':{'lead':['id']},'_limit':1,'include_counts':True})
        count=result.get('count',{}).get('total')
        if not isinstance(count,int): raise ValueError('Queue count unavailable')
        counts[owner]=count
    deficits,_=build_deficits(counts,1,max_queue)
    eligible=[owner for owner in roster if deficits.get(owner,0)>0]
    if preferred in eligible: target=preferred
    else: target=min(eligible,key=lambda owner:counts[owner]) if eligible else None
    return {'target_owner':target,'eligible_reps':list(roster),'queue_counts':counts,
            'mechanism':'existing Lane 2 ready roster and build_deficits','max_queue':max_queue}
