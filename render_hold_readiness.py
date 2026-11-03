"""Render the read-only hold setup and exact queue changes for review."""
import argparse
from html import escape
import json

from pathlib import Path
from render_tier_assignment_preview import table


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    args = parser.parse_args(); root = args.input
    report = json.loads((root/'readiness.json').read_text())
    changes = json.loads((root/'queue-change-preview.json').read_text())
    validation = json.loads((root/'queue-filter-validation.json').read_text())
    valid_ids = {row['id'] for row in validation['validated'] if row['filter_valid']}
    validated = sum(row['path'].split('/')[2] in valid_ids for row in changes)
    verification = json.loads((root/'local-verification.json').read_text())
    manifest = json.loads((root/'deployment-manifest.pending.json').read_text())
    html = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shared sales hold review</title><style>body{font:15px/1.5 system-ui;background:#f5f7fa;color:#182433;padding:30px}main{max-width:1200px;margin:auto}h1{font-size:30px}h2{margin-top:30px}.notice{padding:18px;background:#fff1ce;border-left:4px solid #a27600}.table{overflow:auto}table{border-collapse:collapse;width:100%;background:white}th,td{padding:10px;border-bottom:1px solid #dae0e8;text-align:left;vertical-align:top}th{background:#e9eef5}details{padding:12px;background:white;margin:10px 0;border:1px solid #d7dfe9}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}a{color:#2256a0}</style><main>
<h1>Shared sales holds: setup review</h1><div class="notice"><b>Production writes: 0. Activation: OFF.</b><p>The local guard package is tested. The shared production store is absent, and deployed/native safeguards remain unverified. This packet does not authorize migration, publishing lists, assigning leads or admitting holds.</p></div>'''
    html += '<h2>Read-only findings</h2>' + table(['Item','Result'],[
        ['Capture',report['captured_at']],['Saved searches inspected',len(report['queues'])],
        ['Lane 2 views missing the marker exclusion',sum(row['managed_by_this_package'] and not row['verified_hold_exclusion'] for row in report['queues'])],
        ['Proposed sales-list patches',len(changes)],['Final candidate filters accepted by Close',validated],
        ['Audit / marketing views preserved',report['audit_or_marketing_views_unchanged']],
        ['Workflow definitions inspected',len(report['sequences'])],
        ['Definitions with call/SMS steps',sum(row['classification']=='sales' for row in report['sequences'])],
        ['Local checks passed',verification['tests_passed']],['Immutable audit ledger','Untouched']])
    html += '<h2>Local enforcement prepared</h2><ul><li>Shared checks for assignment, release, reconciliation, sales tasks, enrollment/resumption and outbound dispatch.</li><li>Only the owning workflow and token can clear a hold; another hold continues blocking.</li><li>Reviewed marketing-only email subscriptions can continue during SMS rest. Other holds and DNC still block.</li><li>Fresh scope checks, durable write intent, owner/status readback and no automatic retry after uncertainty.</li><li>SMS and tier pilots require exact approval plus complete, fresh deployment evidence.</li></ul>'
    html += '<h2>Shared store installation candidate</h2><p>Uses existing credentials and a separate mutable CRM control schema. No Close field or audit-ledger change. Current runtime permissions cannot install the schema.</p><p><a href="sms_rest_control.candidate.sql">Review exact unexecuted SQL</a></p>'
    html += '<h2>Exact sales-list changes</h2><p>Each patch adds the shared hold exclusion to the existing query, preserving its other conditions, sort and result limit. No patch has been published. Four audit/marketing views stay unchanged.</p>'
    for row in changes:
        html += '<details><summary>' + escape(row['name']) + '</summary><pre>' + escape(json.dumps(row,indent=2)) + '</pre></details>'
    html += '<h2>Required deployment readbacks</h2><p>Every item remains pending. Local checks do not certify deployment, native Close behavior or manual calling restrictions.</p>'
    html += table(['Required surface','State'],[[name,row['state']] for name,row in manifest['surfaces'].items()])
    html += '<p>Close can dial any Smart View, and native workflows execute outside these Python guards. Native filters, existing subscriptions, alternate views and manual communication controls require their own account-level verification. <a href="https://help.close.com/feature-guide/power-predictive-dialing">Dialer documentation</a> · <a href="https://help.close.com/feature-guide/workflows/workflow-steps#filter">Workflow filters</a></p>'
    html += '<h2>Next rollout boundary</h2><p>Review installation and deployment first, then verify native protections, then approve a fresh bounded pilot. Tier routing and SMS admission remain off throughout preparation. If a future pilot is stopped, preserve existing holds and their enforcement.</p>'
    html += '<p><a href="readiness.json">Full inventory</a> · <a href="queue-change-preview.json">Exact final list patches</a> · <a href="local-verification.json">Verification scope</a> · <a href="deployment-manifest.pending.json">Pending deployment manifest</a></p></main></html>'
    (root/'index.html').write_text(html)
    print(json.dumps({'final_candidate_filters_validated':validated,'candidate_changes':len(changes),'output':str(root/'index.html')}))


if __name__ == '__main__': main()
