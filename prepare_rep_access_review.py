"""Render the read-only roster snapshot into a local classification worksheet."""
import argparse
from collections import Counter
from html import escape
import json
from pathlib import Path
from rep_access import prepare_review, apply_approved_classifications
from lane2_roster import LANE2_ROSTER


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--approval', type=Path)
    args = parser.parse_args()
    source = json.loads(args.source.read_text())
    if source['proof']['read_only'] != 'on' or source['production_writes'] != 0:
        raise ValueError('Read-only source receipt required')
    rows = prepare_review(source, LANE2_ROSTER)
    exclusions = []
    if args.approval:
        rows, exclusions = apply_approved_classifications(source, rows, json.loads(args.approval.read_text()))
    args.output.mkdir(parents=True, exist_ok=True)
    document = {'company_id': source['company_id'], 'captured_at': source['proof']['captured_at'],
        'metadata_namespace_candidate': 'app.company_users.metadata_json.leadAssignmentAccess',
        'status': 'approved_classifications_staged' if args.approval else 'classification_pending',
        'production_writes': 0, 'activation': 'off',
        'source': str(args.source.resolve()), 'reps': rows, 'excluded_classifications': exclusions}
    document['proposed_metadata_changes'] = [
        {'table': 'app.company_users', 'company_id': source['company_id'],
         'company_user_id': row['company_user_id'], 'rep': row['rep'],
         'metadata_key': 'leadAssignmentAccess', 'before': row['current_access'],
         'after': row['proposed_access']}
        for row in rows if row['proposed_access'].get('state') == 'approved'
        and row['proposed_access'] != row['current_access']]
    (args.output/'rep-classifications.json').write_text(json.dumps(document, indent=2)+'\n')
    cells = []
    def yesno(value): return 'Yes' if value is True else 'No' if value is False else 'Not recorded'
    for row in rows:
        values = [row['rep'], row['offer'] or 'Not recorded', row['start_date'] or 'Not recorded',
                  yesno(row['certified']), yesno(row['crm_ready_for_leads']),
                  row['proposed_access'].get('baseTier') or 'Needs classification',
                  yesno(row['proposed_access'].get('reactivationAccess'))]
        cells.append('<tr>'+''.join('<td>'+escape(str(value))+'</td>' for value in values)+'</tr>')
    html = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lane 2 rep access review</title><style>
body{font:15px/1.5 system-ui,sans-serif;color:#172033;background:#f5f6f8;margin:0;padding:32px}
main{max-width:1400px;margin:auto}h1{font-size:28px;margin-bottom:8px}p{max-width:1000px}
.notice{background:#fff;border-left:4px solid #3456a3;padding:14px 18px;margin:20px 0}
table{border-collapse:collapse;width:100%;background:#fff}th,td{text-align:left;padding:11px 14px;border-bottom:1px solid #dce1e8}
th{background:#e9eef6;font-size:13px}td:nth-last-child(-n+2){color:#755117}small{color:#566173}.table{overflow:auto}
</style><main><h1>Lane 2 rep access review</h1><p>Base tier and separate Reactivation permission. Approved classifications are staged for review.</p>
<div class="notice">Read-only review. No tier permissions have been inferred from certification, tenure, or readiness. Production assignments, lead tags, queues, and ownership are unchanged.</div>
<p><strong>Access:</strong> Ramp → Ramp leads. Proven → Ramp + Proven. Top → Ramp + Proven + Top. Reactivation permission adds 365+ leads to any base tier.</p>
<p><small>Source snapshot: ''' + escape(str(source['proof']['captured_at'])) + '''. Existing certification and CRM readiness are shown for context. “Not recorded” can indicate a mismatch between the current company roster and the existing Python roster.</small></p>
<div class="table"><table><thead><tr>''' + ''.join('<th>'+v+'</th>' for v in ['Rep','Offer','Start date','Certified','CRM ready','Approved base tier','Reactivation access']) + '</tr></thead><tbody>' + ''.join(cells) + '</tbody></table></div>'
    if exclusions:
        html += '<h2>Recorded but excluded from assignment</h2><ul>' + ''.join(
            '<li>' + escape(row['rep'] + ': ' + row['reason']) + '</li>' for row in exclusions) + '</ul>'
    html += '</main></html>'
    (args.output/'index.html').write_text(html)
    print(json.dumps({'active_lane2_reps':len(rows),'by_offer':dict(Counter(row['offer'] for row in rows)),
        'already_classified':sum(row['access_record_valid'] for row in rows),
        'staged_by_tier':dict(Counter(row['proposed_access'].get('baseTier') for row in rows)),
        'reactivation_access':[row['rep'] for row in rows if row['proposed_access'].get('reactivationAccess')],
        'excluded_classifications':exclusions,
        'crm_roster_mismatches':sum(not row['crm_roster_match'] for row in rows),
        'production_writes':0,'review':str((args.output/'index.html').resolve())},indent=2))


if __name__ == '__main__': main()
