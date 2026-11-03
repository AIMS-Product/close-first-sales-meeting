# Shared sales hold contract v1

Status: candidate implementation locally; production writes and activation OFF.
Owner: SMS-rest work owns shared assignment/dial-queue gates. QA owns its release/ package.

## Durable state and serialization

Production assignment/release/enrollment writers MUST use workflow_holds.py with
WORKFLOW_HOLD_BACKEND=postgres and WORKFLOW_HOLDS_REQUIRED=1, the same tenant and
the same PostgreSQL advisory fence. SQLite is for local modeling only. Missing or
unreadable mandatory state blocks writes. In local tests, SQLite WAL and a shared process/thread advisory lock (.lock beside the database)
serializes fresh read → eligibility → write → readback. Data writes autocommit with synchronous=FULL so a crash cannot roll back a persisted write intent. All writers must take this lock through Store.serialized(). Never keep this database in Git,
immutable audit tables, ephemeral /tmp, or separate uncoordinated service disks.
Cross-host rollout uses the PostgreSQL adapter described below; deployment remains off.

Table sales_hold(lead_id, workflow, token, acquired_at, expires_at, payload), primary key
(lead_id, workflow). Workflow names: sms_rest, qa, education, recovery. Each owner can
release only its exact workflow+token. Expires_at is a due timer, never implicit permission
to clear. Any other hold blocks SMS admission/release/assignment. Admission to QA may
evaluate a current closer under its own 30-day rule; generic SMS active-closer exclusion
is NOT QA admission policy. QA keeps its hold until downstream acknowledged handoff.

## Close projection and exclusions

Existing Lead Batch cf_IruzyS5PO4dZsOKGQSYb4r7PhOEpw1C82FN17ZlmkQy (text).
Reserved marker: [STH1 <workflow> <token>]. Multiple markers may coexist. Block ALL
assignment, reclaim, reactivation, dial queues and sales enrollments on any STH1 marker
or durable hold. Query predicate: negate text full_words STH1 on Lead Batch.
Never remove another workflow marker or restore an old whole-field snapshot over it.
SMS state saves original batch, owner and Current Owner Since, terminal task ID,
terminal/grace/rest/expiry UTC timestamps, events, intended writes and receipts.
Completed reactivation cohort: [STR1 sms_reactivation <token>] (does NOT block).
Marketing email and invitations remain allowed under sms_rest; each workflow controls
its own communication policy. A cohort marker alone never enforces a hold.

## Acquisition and ownership

Lead Source - Company cf_HyxM8Psr8ZjrabqeR8hqAf4XtTArArHaAyHRvsrCwm1 is the existing
acquisition source. Entry Source cf_FI1V2Zw8M0aoSg7yJKJo0wb8NbRU8LafnkgHrpLcfYA remains
its existing derived channel; neither workflow overwrites either on rest/reactivation.
Lead Owner cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd changes through existing owner
tracking. Current Owner Since cf_2tuoNM0K9ND9na49hMsj02KI7732MuKSiFX2thJuIgU is preserved.
The SMS draft's use of it as an ownership-generation fence is provisional and is
not certified for activation. Genuine ownership-event evidence is required;
never synthesize or backfill the field here.

## Integration gate

Shared assigners, reconcilers and generated dial views consume the common gate. QA code
may call Store.acquire/release while inside Store.serialized(); it must re-read current
Close eligibility there and preserve all holds. Outbound dispatchers must check the gate
immediately before call/SMS/enrollment. Existing Close sales sequences must be paused
before rest is committed. Marketing-only subscriptions are preserved.
No activation until the selected persistent store, deployed gate/view inventory and all
writer acknowledgements are captured in an approved pilot deployment manifest.

## Multi-host production amendment (October 21)

Live inventory from the QA session confirms local GitHub-main workers plus separate
Railway HOT and owner-since services. Independent SQLite volumes cannot coordinate them.
Production selection: PostgresStore, shared crm_workflow_control schema on the existing
managed Lakebase connection, tenant-bound to BTC company/Close org. The candidate DDL is
sql/sms_rest_control.sql; no migration has run. PostgresStore.serialized() holds a session
advisory lock on hashtextextended(company_id + '/' + organization_id,0); every participating
writer must use that SAME key. Intents autocommit before network calls. SQLite Store is
local test/pilot modeling only. Existing credentials and permissions must be verified;
no new account/key is requested. QA can keep its separate PostgreSQL journal but must
replace its SQLite bridge with this production fence before activation.

SMS projection retains permanent [STL1 lane1_release <token>] markers and all unrelated
text verbatim. It removes only its own exact [STH1 sms_rest <token>] on verified handoff.
Acquisition field is unchanged; its current choices do not establish webinar origin.
Opportunity owner/status writers and the hourly HOT worker must adopt the shared guard
or be quiesced with explicit coordination evidence before any held lead is admitted.
This is an activation prerequisite; no deployed maintenance code is changed here.

Migration/grant gate: verified runtime role st_gateway_writer cannot CREATE the
candidate schema. Installation and the scoped table/sequence grants in the DDL must
use the existing authorized migration path. Successful reads do not establish these
permissions. No schema/grant changes have been made. The shared module hash changes
during implementation; QA must pin the final reviewed hash before activation.

## Status transport adoption gate

The candidate status transport validates write permission and status-only payloads
before shared-hold reads. With no shared-store configuration it retains the existing
status transport behavior. Activating any sales hold requires WORKFLOW_HOLDS_REQUIRED=1
and the shared backend on every participating writer. The deployed hourly worker has
not been changed or certified for that adoption. The ownership-generation fence is
still provisional and requires genuine ownership-event evidence before activation.

## October 23 completion of the local guard package

The common transport now gates lead/opportunity writes plus sales task creation,
call/SMS/email dispatch, workflow enrollment and workflow resumption. Lead-linked
writes resolve current scope and check the same shared registry under the same
advisory fence. Authentication is forwarded to native pre-write and readback reads.
Mutation failures retain their durable intent and cannot be automatically retried.
Pausing/finishing a subscription remains available to stop communications.

No-show recovery, outcome/meeting-field projection, follow-up/funnel/sales-cycle
updates and bulk workflow resumption now use the guard in the candidate checkout.
The no-show fallback cannot mutate through an unguarded alternative transport.
Live SMS apply/observation explicitly reject isolated SQLite state. Production
PostgreSQL acquisition/release requires the tenant fence and contract version 1.

Marketing classification is explicit mutable control metadata, not another Close
field. A reviewed `marketing_sequence:<id>` workflow_meta record contains
schemaVersion=1, purpose=marketing_only, reviewedBy, reviewedAt, expiresAt and
stepsSha256. The current sequence must contain only email steps and match that
hash. Only an SMS-only hold allows this exception. Other holds, DNC, unknown
classification or changed sequence steps block dispatch. Existing verified
marketing subscriptions are left running; sales subscriptions must be paused
before SMS rest commits. No marketing classification has been saved live.

`hold_rollout.py` requires fresh tenant-bound deployment receipts for every
required system, queues, native workflows and manual calling controls. Both
SMS and tier pilot authorization now require this manifest as well as the exact
pilot approval. Boolean integration flags alone are insufficient. A pending
manifest is generated for review, never marked verified by local tests.

Sales queues get the common marker exclusion. Four audit/marketing views retain
visibility of held leads; they are not sales calling lists. The October 23 packet
contains 21 proposed sales-list filter patches with exact preimages; all corresponding
filters passed read-only API validation. Other saved searches need a separate
scope review before they can be certified as calling-safe.

Native Close workflows and manual communication controls are separate from these
Python transports. Their configuration/readback, the Railway workers and the
QA/education bridges remain unverified. No deployment, migration or activation
is implied by local coverage. See HOLD_ROLLOUT_REVIEW.md for the concrete packet.
