# Shared sales holds — review package

Status: locally implemented and tested. Production writes and activation remain off.

## Prepared changes

- Common hold checks for assignments, releases, lead/opportunity reconciliations,
  sales tasks, sales call/text/email dispatch and workflow enrollment/resumption.
- One PostgreSQL tenant fence across hosts. Hold acquisition/release requires that
  fence, and each workflow can clear only its own exact token. Due time alone never
  removes a hold. Uncertain provider writes require reconciliation before retry.
- Explicit reviewed marketing-only workflow classification. Marketing can continue
  during an SMS-only rest; another workflow's hold cannot be bypassed by that exception.
- An atomic, tenant/version-checked installation candidate at
  `sql/sms_rest_control.sql`, with grants limited to the five CRM control tables and
  their write-intent identity sequence. Existing runtime credentials are reused.
  No audit-ledger privileges, new accounts or Close fields are added.
- Fresh deployment evidence is mandatory for pilot authorization. The template
  contains pending entries; local tests never certify production deployment.

## Read-only production findings, October 23

Packet: `tier-preview-20261023-hold-readiness/`.

| Item | Finding |
| --- | --- |
| Shared control tables | Absent; current runtime role cannot create the schema |
| Saved searches inspected | 151 |
| Existing Lane 2 views | 25; none currently has the shared hold exclusion |
| Proposed sales-list changes | 21; exact existing query plus the hold exclusion |
| Audit/marketing views preserved | 4 |
| Proposed filter validation | All 21 final candidates accepted by Close in read-only searches |
| Workflow definitions inspected | 383; 222 include call or SMS steps |
| Workflow configuration changes | None |
| Production data writes | Zero |

`queue-change-preview.json` holds exact query preimages, expected hashes and
candidate patches. Existing query conditions, sorting and result limits are
preserved. The initial diagnostic included four audit/marketing views; those
changes were withdrawn from the final candidate file. The original diagnostic
and its 25 filter validations are retained as evidence, not publication scope.

## Remaining production gates

1. Install the reviewed shared schema/grants through the existing authorized
   migration path, after approval. Read back tenant, contract, tables and privileges.
2. Deploy the candidate guards with the mandatory shared backend to participating
   CRM writers. Verify separate Railway status/ownership writers and the QA and
   education bridges; do not change their workflow-specific rules or release holds.
3. Apply only approved queue patches after exact preimage checks, then read back
   definitions and verify held-lead exclusion. Review other saved searches that
   can be used for calling; the 126 other saved searches are not certified by this packet.
4. Review native sales workflows and their existing subscriptions. These execute
   inside Close and bypass Python middleware. Close supports Filter steps that
   pause a run when a condition fails; prepare the hold predicate before applicable
   sales actions, with scope-specific readbacks. Pausing an existing sales run is
   separate from excluding a new enrollment. Marketing classification must be
   reviewed explicitly; names or email-only steps alone do not approve a workflow.
5. Verify manual calling/texting and workflow-resume controls in Close, including
   alternate views and pending call tasks. Queue exclusion alone does not certify
   this requirement. Leave the corresponding manifest entries pending until proven.
6. Produce the final fresh pilot approval using stored rep permissions, current
   lead evidence and complete deployment receipts. Tier routing and SMS rest remain off.

Close documents that its power dialer can run from any saved Smart View, so the
list inventory needs to cover every sales entry point. See
[Power & Predictive Dialing](https://help.close.com/feature-guide/power-predictive-dialing),
[Workflow Filter steps](https://help.close.com/feature-guide/workflows/workflow-steps#filter)
and [workflow run management](https://help.close.com/feature-guide/workflows).
These product capabilities do not establish that the current account is configured safely.

## Rollback boundary

Turn off new tier assignments and SMS-rest admission if a pilot is stopped. Keep
the shared hold backend, existing holds and their exclusions enforced while any
held leads remain. Do not disable hold enforcement, delete control state, clear
another workflow's marker or restore entire Lead Batch snapshots. The immutable
audit ledger is never part of installation or rollback.
