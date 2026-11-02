# Tier routing readiness

Candidate only. No production writes, schema changes, queue publication, or activation.

## October 23 read-only preview

`tier-preview-20261023-r4/index.html` renders the captured roster, current ownership
counts, exact metadata candidates, exclusions, conditional owner patch and four
replacement queue definitions. The capture used existing credentials, read-only
database transactions and 148 Close read/search requests; no data was written.

- 22 active classified reps: 4 Top, 3 Proven and 15 Ramp. Cassie is Proven.
- 20 certified reps have captured ownership counts. Eduardo and Jennifer remain
  Before Roadmap, retain their approved Ramp tier and receive no assignments.
- Certification opens tier eligibility. Legacy readiness flags add no second gate.
- Vince alone has the additional Reactivation permission.
- Existing 1,000-lead capacity is modeled conservatively using total owned leads;
  an actionable queue census has not been certified.
- Ten sampled leads: five protected, two excluded for stale age tags, two retaining
  their owners, and one conditional Ramp candidate for Osheana. Three tags in the
  overall sample are stale; one is independently excluded by its protected status.
- The shared hold registry is unavailable. All actual assignment proposals remain
  blocked. The Osheana owner patch is a conditional illustration, not executable.

## Local safeguards checked

240 local tests pass across rep access, tier assignment, SMS workflow, shared writer
guards and existing hourly status maintenance. New writer tests confirm that SMS,
QA, education and recovery holds block lead writes; opportunity writes check their
parent lead; expired due timers do not remove holds; clearing SMS leaves QA intact;
missing state and malformed markers block; owner changes prevent stale writes;
readback mismatches and ambiguous responses retain an intent and prevent retries.
These tests use temporary local state and fake provider transport. They do not
certify deployed PostgreSQL coordination, dispatchers or manual calling in Close.

All four candidate queue filters passed read-only API validation. These filters
check projected hold markers; they cannot query the separate durable hold registry
or all historical STOP/DNC evidence. Writer and dispatch enforcement plus verified
hold projection remain prerequisites to queue publication.

## Work still required before a production routing pilot

1. The VP assignment entry point now selects the tier planner only with an explicit
   pilot flag. It defaults off and production still uses existing logic. See
   `TIER_ROUTING_PILOT.md` for limits, approval requirements and entry-point evidence.
2. Complete the shared control-store migration and grants through the authorized
   migration path. The candidate is `sql/sms_rest_control.sql`; it has not run.
3. Verify the shared gate across every assignment, release, sales enrollment and
   outbound dispatch path, with coordination across the deployed hosts. Current
   Owner Since is not certified as a genuine ownership-generation fence.
4. Prepare a bounded executable pilot with fresh tier, source, certification,
   membership, owner, capacity, suppression and hold checks. Include exact rep
   permission changes and conditional lead patches for approval.
5. Obtain approval before production metadata writes, migration, list publication,
   lead assignment or activation. Preserve hourly maintenance and the immutable
   audit ledger.

The October 30 local integration aligns SMS return tags with actual lead age and
provides a tier-aware, capacity-checked return planner. Finishing 90 days of rest
does not grant the 365+ Reactivation classification. The legacy live target selector
has not been switched; adopting the shared planner in the production coordinator
remains part of activation. See `DISTRIBUTION_V2_LOCAL.md`.
