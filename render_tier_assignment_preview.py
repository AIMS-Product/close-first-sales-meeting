"""Render bounded read-only evidence and an exact roster-only SQL candidate."""
import argparse
from collections import Counter
from datetime import datetime
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from rep_access import parse_access
from tier_assignment import rep_available


def text(value):
    if value is None:return 'Not recorded'
    if isinstance(value,bool):return 'Yes' if value else 'No'
    return escape(str(value))


def table(headers,rows):
    return '<div class="table"><table><thead><tr>'+''.join('<th>'+escape(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join(
        '<tr>'+''.join('<td>'+text(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def literal(value):
    return "'"+str(value).replace("'","''")+"'"


def roster_sql(document):
    changes=document['metadata_changes']
    by_id={rep['company_user_id']:rep for rep in document['reps']}
    values=[]
    for row in changes:
        parse_access(row['after'])
        rep=by_id[row['company_user_id']]
        values.append('('+','.join((literal(row['company_user_id'])+'::uuid',literal(rep['close_user_id']),
            literal(json.dumps(row['before']))+'::jsonb',literal(json.dumps(row['after']))+'::jsonb'))+')')
    return '''-- CANDIDATE ONLY. Not executed. Stores approved rep access, without activation.
-- Existing certification, readiness, status, source, offers, ownership, queues and
-- all other metadata namespaces are preserved. Immutable audit tables are untouched.
BEGIN;
SELECT set_config('app.company_id','88a28173-2a61-4cbb-92ea-f6b8be4592f3',true);
CREATE TEMP TABLE requested_rep_access(
 company_user_id uuid PRIMARY KEY, close_user_id text UNIQUE NOT NULL,
 expected_before jsonb NOT NULL, requested_after jsonb NOT NULL
) ON COMMIT DROP;
INSERT INTO requested_rep_access VALUES
'''+',\n'.join(values)+''';
DO $$
DECLARE changed integer;
BEGIN
 IF (SELECT count(*) FROM requested_rep_access) <> '''+str(len(changes))+''' THEN
   RAISE EXCEPTION 'Unexpected rep-access change count';
 END IF;
 -- Lock every target row before checking fresh roster and exact metadata preimages.
 PERFORM 1 FROM app.company_users u JOIN requested_rep_access r ON r.company_user_id=u.id
 WHERE u.company_id='88a28173-2a61-4cbb-92ea-f6b8be4592f3'::uuid FOR UPDATE OF u;
 IF EXISTS(SELECT 1 FROM requested_rep_access r LEFT JOIN app.company_users u
  ON u.id=r.company_user_id AND u.company_id='88a28173-2a61-4cbb-92ea-f6b8be4592f3'::uuid
  WHERE u.id IS NULL OR u.status<>'active' OR u.sales_team_lane IS DISTINCT FROM 2
   OR u.metadata_json->'cohortTracker'->>'exitReason' IS NOT NULL
   OR coalesce(u.metadata_json->'leadAssignmentAccess','null'::jsonb) IS DISTINCT FROM r.expected_before
   OR (SELECT count(*) FROM app.company_user_provider_accounts a WHERE a.company_id=u.company_id
       AND a.company_user_id=u.id AND a.source_domain='crm' AND a.provider='close'
       AND a.status='active' AND a.provider_user_id=r.close_user_id)<>1)
 THEN RAISE EXCEPTION 'Roster identity, status, or access preimage changed; regenerate preview'; END IF;
 UPDATE app.company_users u SET metadata_json=jsonb_set(coalesce(u.metadata_json,'{}'::jsonb),
  '{leadAssignmentAccess}',r.requested_after,true),updated_at=now()
 FROM requested_rep_access r WHERE u.company_id='88a28173-2a61-4cbb-92ea-f6b8be4592f3'::uuid
  AND u.id=r.company_user_id;
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>'''+str(len(changes))+''' THEN RAISE EXCEPTION 'Partial roster save; rollback'; END IF;
END $$;
SELECT u.id,u.display_name,u.metadata_json->'leadAssignmentAccess' AS recorded_access
 FROM app.company_users u JOIN requested_rep_access r ON r.company_user_id=u.id
 WHERE u.company_id='88a28173-2a61-4cbb-92ea-f6b8be4592f3'::uuid ORDER BY u.display_name;
COMMIT;
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    args=parser.parse_args();root=args.input
    doc=json.loads((root/'preview.json').read_text());leads=json.loads((root/'source-leads.json').read_text())
    details={row['lead_id']:row for row in leads}
    by_tier=Counter(row['proposed_access']['baseTier'] for row in doc['reps'])
    now=datetime.fromisoformat(doc['captured_at']).astimezone(ZoneInfo('America/Los_Angeles'))
    html='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lane 2 assignment preview</title><style>
body{font:15px/1.5 system-ui,sans-serif;margin:0;padding:32px;color:#192337;background:#f4f6f9}main{max-width:1400px;margin:auto}
h1{font-size:30px;margin-bottom:4px}h2{font-size:22px;margin-top:30px}.meta{color:#566174}.notice{padding:16px 20px;background:#fff5d8;border-left:4px solid #a87500;margin:20px 0}
.cards{display:flex;flex-wrap:wrap;gap:12px}.card{background:white;padding:16px 22px;border:1px solid #dce2eb;border-radius:6px;min-width:130px}.card b{display:block;font-size:27px}
.table{overflow:auto}table{border-collapse:collapse;width:100%;background:white}th,td{text-align:left;vertical-align:top;padding:10px 12px;border-bottom:1px solid #e0e5ec}th{font-size:13px;background:#e7edf6}details{background:white;border:1px solid #dce2eb;padding:12px;margin:10px 0}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}a{color:#214ea5}code{font-size:13px}li{margin:7px 0}
</style><main><h1>Lane 2 assignment and dial-list preview</h1>'''
    html+='<p class="meta">Read-only capture: '+text(now.strftime('%B %d, %Y, %I:%M %p %Z'))+'. Ten sampled leads, not a complete assignment inventory.</p>'
    if doc.get('policy_replay_note'):
        html+='<p class="meta">'+text(doc['policy_replay_note'])+'</p>'
    html+='<div class="notice"><strong>Production writes: 0. Activation: off.</strong> Rep permissions are staged, not saved. Shared durable hold verification is '+('available' if doc['durable_holds_available'] else '<strong>unavailable</strong>')+'. Every actual assignment remains blocked until the deployment gates are satisfied.</div>'
    html+='<div class="cards">'+''.join('<div class="card"><b>'+str(n)+'</b>'+label+'</div>' for label,n in [('Active classified reps',len(doc['reps'])),('Top',by_tier['Top']),('Proven',by_tier['Proven']),('Ramp',by_tier['Ramp']),('Sampled leads',len(leads))])+'</div>'
    html+='<h2>Assignment rules</h2>'+table(['Lead tier','Eligible base tiers'],[
        ['Ramp','Ramp, Proven, Top'],['Proven','Proven, Top'],['Top: genuine opt-in','Top; Proven after 24 hours without an outbound dial, while unowned'],['Reactivation: 365+','Separate approved permission; Vince only']])
    html+='<p>The existing Lead Tier tagger supplies age bands and Legacy Create Date fallback. A freshness check reuses its existing classification function. Source protection reuses the original opt-in allowlist: Website-Apply, Typeform, Webinar, Lead-Magnet. An imported or missing-source lead tagged Top routes at Ramp eligibility; no lead tag is changed by this planner.</p>'
    html+='<p>Existing owners keep their leads across age-band changes. The 24-hour overflow rule broadens eligibility for unowned leads. It does not transfer an already-owned lead. The existing seven-day inactivity release remains a separate action.</p>'
    html+='<h2>Rep permissions and certification</h2><p>Lead assignment starts after certification. Uncertified reps remain visible in Before Roadmap with their tier recorded and receive no leads. After certification, the approved tier controls access, subject to active membership, offer boundaries, capacity, and lead protections. Older readiness flags are shown for reference and do not add another assignment gate.</p>'
    names={rep['close_user_id']:rep['rep'] for rep in doc['reps']}
    rep_rows=[]
    for rep in doc['reps']:
        access=rep['proposed_access']
        status=('Before Roadmap — no assignments' if rep['certified'] is False else
                'Certification not recorded — no assignments' if rep['certified'] is None else
                'Certified — tier eligible' if rep_available(rep,datetime.fromisoformat(doc['captured_at']),set(doc['active_close_membership_ids'])) else
                'Certified — identity or approved access unavailable')
        rep_rows.append([rep['rep'],rep['offer'],access['baseTier'],access['reactivationAccess'],rep['certified'],status,rep['crm_ready_for_leads'],doc['queue_counts'].get(rep['close_user_id'])])
    html+=table(['Rep','Offer','Approved tier','365+ access','Certified','Assignment status','Legacy flag (reference)','Current total owned'],rep_rows)
    html+='<p>Queue capacity is modeled conservatively against total owned leads, with the existing 1,000-lead target. It is not an actionable-queue capacity certificate.</p>'
    eligible=[rep for rep in doc['reps'] if rep_available(rep,datetime.fromisoformat(doc['captured_at']),set(doc['active_close_membership_ids']))]
    missing_counts=[rep['rep'] for rep in eligible if rep['close_user_id'] not in doc['queue_counts']]
    html+='<p>Certified rep capacity coverage: '+str(len(eligible)-len(missing_counts))+' of '+str(len(eligible))+'. '+('Missing counts: '+text(', '.join(missing_counts)) if missing_counts else 'All certified reps have a captured count.')+'</p>'
    html+='<h2>Roster exclusions</h2>'+table(['Rep','Reason','Recorded status','Exit reason'],[[r['rep'],r['reason'],r.get('roster_status'),(r.get('cohort_settings') or {}).get('exitReason')] for r in doc['excluded_reps']])
    html+='<h2>Identity mappings reconciled locally</h2><p>Six missing Close IDs were filled in the isolated candidate roster. Each existing readiness flag was preserved.</p>'+table(['Rep','Verified Close identity','Readiness preserved'],[[r['rep'],r['after'],r['ready_for_leads_preserved']] for r in doc['identity_reconciliation']])
    html+='<h2>Bounded lead-policy examples</h2><p>The table below shows policy results assuming the shared hold registry can be verified clear. That assumption is unproved. These are conditional examples and cannot be executed. Actual proposals remain excluded wherever shared hold verification is unavailable.</p>'
    examples=doc['conditional_policy_examples'] or doc['lead_proposals'];example_rows=[]
    for proposal in examples:
        row=details[proposal['lead_id']];decision=proposal['decision'];tag=row['tag_check']
        no_target=('Capacity not verified' if proposal.get('capacity_unverified_for') else 'Capacity full' if proposal['capacity_blocked'] else 'None')
        example_rows.append([row['lead'].get('display_name') or proposal['lead_id'],row['category'],tag['current_tier'],tag['proposed_tier'],decision['reason'],', '.join(proposal['eligible_reps']) or 'None',names.get(proposal['target_owner']) or no_target])
    html+=table(['Sample','Category','Stored tag','Tagger now expects','Policy result','Eligible reps if holds clear','Conditional target'],example_rows)
    conditional_changes=[proposal for proposal in examples if proposal['field_changes']]
    if conditional_changes:
        html+='<h3>Conditional owner changes — blocked from execution</h3><p>These changes illustrate routing only. Shared hold checks and all deployment gates must pass before any write is permitted.</p>'
        for proposal in conditional_changes:
            html+='<details><summary>'+text(proposal['lead_id'])+' → '+text(names.get(proposal['target_owner']))+'</summary><pre>'+escape(json.dumps(proposal['field_changes'],indent=2))+'</pre></details>'
    stale=[r for r in leads if not r['tier_freshness_verified']]
    html+='<p><strong>'+str(len(stale))+' of '+str(len(leads))+' sampled tags need refresh.</strong> This is a sample finding, not an estimate of the full database. No tag refresh was applied.</p>'
    html+='<h2>Exact proposed changes</h2><p>Roster storage would update only the leadAssignmentAccess metadata namespace and the normal updated_at timestamp. No assignment, certification, readiness, or Close custom-field write is included in that roster-only candidate.</p>'
    html+='<p><a href="roster-access-candidate.sql">Review the roster-only SQL candidate</a> · <a href="preview.json">Full field-change and view-query JSON</a></p>'
    for row in doc['metadata_changes']:
        html+='<details><summary>'+text(row['rep'])+' — '+text(row['after']['baseTier'])+'</summary><pre>'+escape(json.dumps({'before':row['before'],'after':row['after']},indent=2))+'</pre></details>'
    html+='<h2>Replacement dial-list candidates</h2><p>Each candidate uses current ownership, calling hours, protected statuses, booking and active closer exclusions, and the common hold marker. No Recapture State or Lead Tier filter removes an existing owner’s lead. All four filters passed read-only Close API validation. Historical suppression and durable holds still require the dispatch and writer integrations.</p>'
    for view in doc['view_candidates']:
        payload={'name':view['name'],'type':'lead','is_user_dependent':True,'is_shared':False,
                 's_query':{'query':view['query'],'sort':view['sort'],'results_limit':None}}
        html+='<details><summary>'+text(view['name'])+' — '+('filter validated' if view['api_validation']['valid'] else 'validation failed')+'</summary><pre>'+escape(json.dumps(payload,indent=2))+'</pre></details>'
    html+='<h2>Before activation</h2><ul>'+''.join('<li>'+text(gate)+'</li>' for gate in doc['deployment_gates'])+'</ul>'
    if doc.get('verification'):
        html+='<h2>Verification scope</h2><pre>'+escape(json.dumps(doc['verification'],indent=2))+'</pre>'
    html+='<p>Old Recapture lists remain published. This preview creates, updates, deletes, or activates nothing in Close.</p></main></html>'
    (root/'index.html').write_text(html)
    (root/'roster-access-candidate.sql').write_text(roster_sql(doc))
    summary={'active_reps':len(doc['reps']),'tier_counts':dict(by_tier),'sample_leads':len(leads),
        'certified_assignment_eligible':sum(rep_available(rep,datetime.fromisoformat(doc['captured_at']),set(doc['active_close_membership_ids'])) for rep in doc['reps']),
        'certified_capacity_counts_captured':len(eligible)-len(missing_counts),
        'capacity_missing_for':missing_counts,
        'before_roadmap':[rep['rep'] for rep in doc['reps'] if rep['certified'] is False],
        'stale_sample_tags':len(stale),'metadata_changes':len(doc['metadata_changes']),
        'actual_assignment_changes':sum(bool(p['field_changes']) for p in doc['lead_proposals']),
        'conditional_targets':[{'lead_id':p['lead_id'],'rep':names.get(p['target_owner']),'field_changes':p['field_changes']}
            for p in examples if p['target_owner']],
        'production_writes':0,'activation':'off'}
    (root/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
