# Attendance at the released-lead handoff

The existing Lane 1 service continues to start the 30-day clock at the first
scheduled closer meeting, including a canceled or no-show booking. Its existing
eligibility checks, owner-stint evidence, release journal and release tags stay
unchanged. Released leads are staged under Dom before this handoff.

`route_released_leads.py` consumes only Dom-owned leads whose Lead Batch starts
with `Released - `. A past meeting with Close's native Meeting Outcome `Completed`
permits the Naria/QA Triage handoff. Calendar status `completed` alone, a blank
outcome, Scheduled, Rescheduled, Cancelled or No Show does not prove attendance.
There is no additional 30/60-day recency limit on attendance.

Current no-show/cancellation lead statuses return to the normal Lane 2 assignment
pool even when an older meeting was completed. Other leads without a confirmed
completion also return to Lane 2. The existing bucket rules select their state;
the handoff clears Lead Owner, and the regular assigner distributes them.

The handoff preserves manual overrides, QA/workflow holds, protected lead statuses,
won opportunities, upcoming/in-progress meetings and normal active-deal protection.
It reads all meeting/opportunity pages and verifies the lead stayed stable during
the read. Before applying it repeats the evidence read and requires the same plan.
One request changes only Lead Owner and Recapture State, followed by an independent
readback. It does not retry uncertain writes or change dates, statuses, opportunity
owners, original release tags, acquisition fields or existing Naria-owned leads.

The existing state reconciler runs this handoff after terminal QA cleanup and
before its normal reconciliation, under the same workflow concurrency group.
A terminal-only run does not execute the handoff. Dry-run and the shared
max-write budget apply across terminal cleanup, the handoff and normal updates.
Sample (`--limit`) runs leave staged-release handoffs out of their scope.
The standalone script defaults to dry-run.

This change covers ongoing staged releases. Retrospective cleanup of Naria's
existing QA backlog is a separate operation.
