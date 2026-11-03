# Lane 2 tier assigner — release readiness

Updated November 2, 2026. Candidate branch only. The V2 tier flag and scheduled
invocation remain off; no live assignments or queue publication are authorized.

## Ready in the candidate

- V2 tier routing is gated by an explicit flag and defaults off. A tiered apply
  cannot silently fall back to the legacy assigner.
- The bounded pilot requires exact rep/lead IDs, an assignment cap, a current
  approval packet, stored Lakebase permissions and mandatory shared PostgreSQL
  holds. The preview path is read-only.
- The shared `crm_workflow_control` schema and grants were installed on staging,
  production and primary on November 2 and read back. This is dormant infrastructure,
  not proof that all writers, dispatchers or native Close paths enforce it.
- Sixteen active Lane 2 reps have approved access records in Lakebase. The agreed
  distribution policy is documented in the [Anthony approval thread](https://modern-amenities.slack.com/archives/D0C0UUQ3N9M/p1792005677696509?thread_ts=1791931099.718719&cid=D0C0UUQ3N9M).
- Anthony's October 13–14 approval is the direct policy record found in Slack.
  A separate Thea approval or criteria message was not located in the searches;
  don't attribute the documented rules to Thea without that source.

## Remaining before a pilot can be authorized

1. Reconcile the current Lakebase roster: 22 active Lane 2 rows; six have no
   approved tier. Two of those six have duplicate or unlinked Close identities.
   Do not assign their tiers from tenure or certification alone.
2. Deploy and verify the shared hold gate across every writer, release path,
   sales workflow enrollment, outbound dispatcher, and manual Close dialing path.
   Existing local/fake-provider tests do not count as deployment evidence.
3. Review and publish only the approved calling-queue filters, with exact
   preimage checks and readbacks. Inspect alternate Smart Views and native Close
   workflows because they can bypass the Python assigner.
4. Capture fresh per-rep and per-lead previews, verify ownership, capacity,
   opt-in source, certification, suppressions and holds, then produce the exact
   pilot approval with complete deployment receipts.
5. Open the candidate PR and obtain review/merge. The GitHub CLI credentials
   currently fail authentication, so the existing compare link is the PR entry:
   https://github.com/AIMS-Product/close-first-sales-meeting/pull/new/codex/lane2-tier-routing-rollout

## Approved policy to preserve

- Rep stages: Ramp during days 1–21; Proven from day 22 after two consecutive
  completed goal weeks; Top after day 60 when booked calls are at least 2.5 per
  100 dials. These are eligibility criteria; individual stored approvals govern
  access and local recommendations never write those permissions.
- Lead ages: Ramp includes 30–60 and 180–365 days; Proven includes 8–30 days and
  the Ramp pools; Top receives all non-reactivation pools, with 0–7 day leads
  limited to genuine opt-ins. Imported lists remain Ramp regardless of creation
  date. Reactivation access separately permits 365+ day leads.
- A fresh 0–7 day lead can overflow to Proven after 24 hours without an outbound
  dial. Ownership stays with the current rep until the seven-day no-activity
  release; tier controls new or released assignments.

## Release boundary

Do not merge, deploy, activate `LANE2_TIER_ROUTING_ENABLED`, schedule the job,
publish queue changes, or assign leads until the gates above have fresh evidence
and the exact pilot approval is complete. The immutable audit ledger remains
outside the install and rollback scope.
