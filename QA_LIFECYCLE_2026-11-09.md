# QA Lost handoff and five-business-day work window

Implements the user's November 9 clarification of the October 19 QA policy.

## Accepted behavior

- A closer's own human Lost change starts a 48-hour cooling period. The existing
  30-day first-meeting clock continues for leads the closer has not closed out.
- Early handoff permits only Price, Timing, Need to think, Spouse/partner and
  Unresponsive after a shown call. All other or missing reasons remain excluded.
  VendHub referrals, DNC, Disqualified, won deals, future meetings, overrides,
  other active deals and competing workflow holds retain protection.
- Use the existing `QA assigned at`, `QA Contacted` and `QA outcome` fields.
  An admission is one atomic lead update, including original-closer attribution,
  entry time and cleared prior QA disposition/contact flags. Retries never reset it.
- Five business days start at actual QA admission in America/Chicago. Entry is
  day zero; expiry keeps the same Central wall-clock time, skipping weekends and
  observed nationwide U.S. federal holidays. The user confirmed these conventions.
- The launch captures the current QA backlog as an exact ID cohort and starts
  one fresh window at launch, preserving prior contact/outcome history. Subsequent
  passes cannot restart it. New admissions use their actual handoff time.
- A lead gets only one QA pass. Retained QA fields, native event evidence and
  verified lifecycle receipts prevent readmission after a later closer Lost or
  30-day release. Previously released closer-owned leads with uncertain QA
  history remain held for review rather than risking another pass.
- A no-show from Naria's QA booking stays with her for recovery. Neither the
  five-day fallback nor the older terminal cleaner may immediately release it.
- At expiry, only complete no-connection evidence permits normal Lane 2 intake.
  Preserve the release tag. A connection or human disposition prevents automatic
  timeout. Ready/education cases retain their existing human/provider handoff;
  they are not treated as successfully booked or registered merely from a dropdown.
- A verified new booking after QA entry has clean-slate booking eligibility for
  the actual booking rep; it does not establish qualification, attendance or pay.

## Implementation

1. Add pure policy/calendar/event-provenance functions and boundary tests.
2. Add a native Close reader and bounded runner, using the current reconciler's
   concurrency, fresh-read checks and single-attempt write/readback pattern.
3. Stamp future normal released-lead QA admissions and run Lost/expiry handling
   in the existing Railway release service, behind explicit configuration. Its
   existing 15-minute, seven-day schedule handles elapsed-hour and deadline checks.
   GitHub delegates QA handoffs through the versioned deployment switch; ordinary
   non-QA handoffs keep their existing path. No database credentials or backlog IDs
   are added to GitHub.
4. Keep the Railway 30-day producer from releasing current Lost leads when the
   new Lost handler is enabled; the new handler owns their reason/delay checks.
5. Verify native field choices, closer roster, event coverage, calendar and tier
   handoff in a read-only preview before enabling coordinated production jobs.

## Deployment boundaries

No timer guesses, blanket backlog expiry, automatic retries after uncertain PUTs,
or changes to immutable reporting history. Unknown actor/API origin, changed
ownership, incomplete history or ambiguous target deals remain individually held.
The older prior-day Lost router must be retired for the covered closer population
before activation. Its Jason/Ryan routing cannot compete with the 48-hour handoff.

Native source contracts: [Close event provenance](https://developer.close.com/api/resources/events)
and [cursor pagination](https://developer.close.com/api/resources/events/list).

## Build and activation status

The source build is disabled by default. Local policy/adapter tests and read-only
native previews cover the fresh backlog window and the human Lost admission gates.
Verification: 139 Python tests passed; the exact packaged producer sources passed
TypeScript checking/build and 43 targeted release tests. The native previews made
no lead or workflow-store changes.
The shared Lost Reason dropdown now includes `Need to think`; existing choices
were preserved. No lead has been migrated or reassigned by this build.

`deploy/build_qa_railway_bundle.py` produces a private context from the pinned
existing Railway sources and import-reachable Python support. It excludes launch
cohort IDs and environment files. The wrapper shares the existing cohort fence,
then runs the original closer release with its original safeguards. The producer
patch delegates current Lost leads to this lifecycle only when its policy flag is on.

Activation requires a fresh private saved-view cohort, matched enabled deployment
switches in GitHub/Railway, the producer's delegation flag and recomputed existing
configuration digests. Do not reuse the preview's launch timestamp. The legacy
Lost router stops before any API/write operation once the deployment switch is on.

## Deferred follow-up — booking-credit reporting

On November 9 the user explicitly deferred the separate SteelTrap booking-credit
change until after the QA automation work. Preserve the prepared private Naria
30-day credit-exception candidate for that follow-up. Its remote upload/test is
not approved, and no reporting installation or activation has occurred.

This reporting work is **not a prerequisite for the QA automation release**.
The QA release scope is the 48-hour workable-Lost handoff, five-business-day QA
window, one-time fresh backlog window and existing outcome paths. The 30-day
lead-routing clock remains for open closer leads; eligible closer-marked Lost
leads bypass it after their separate 48-hour cooling period.

When resuming reporting, verify Naria's exact intended credit scope, rebase the
saved candidate against then-current reporting bindings, and complete its own
qualification before installing it. Preserve cancellation, duplicate-booking and
historical-credit safeguards. Do not treat the earlier preparation or this
follow-up note as authorization for the blocked remote upload/test.
