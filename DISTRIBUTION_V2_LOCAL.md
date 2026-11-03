# Unified lead distribution and dialer trial — local implementation

Dom approved combining tier rules and V2 distribution on October 30, with all work
limited to local implementation and dry runs. This follows the agreed separation:
distribution owns eligibility, protection and assignment; the dialer consumes its
cohort decisions. No provider mutations, purchases, schedules or outbound messages.

## Integration

Extend the existing `tier_assignment`, `rep_access` and shared hold protections in
this candidate. Do not replace the legacy assigners, enable the bounded live pilot,
or create a second distribution engine in the dialer project.

- A pure age/source policy computes Top 0–7, Proven 8–29/61–179, Ramp 30–60/180–364,
  Reactivation 365+. Reuse Legacy Create Date before Close creation and Pacific
  calendar-day aging, matching the existing tagger. Genuine opt-in source is required
  for Top; unknown/imported fresh sources receive Ramp assignment eligibility.
- Reuse approved cumulative permissions and separate Reactivation access. Add local
  performance-ladder evaluation: live days 1–21 Ramp, days 22+ Proven after two
  consecutive completed weeks meeting the rep's own goal, days 61+ Top at >=2.5
  booked per 100 dials. Existing explicitly approved access is preserved; automatic
  ladder evaluation must be explicitly selected for a rep. Proposals do not save
  permission metadata or override an explicit manual classification.
- Preserve owned leads across age changes. Independently propose seven-day inactivity
  releases only with complete evidence, no task/workflow/hold, no recent activity/call,
  no future appointment, suppression, protected status or closer involvement. No
  release is applied or reassigned in the same dry-run cycle.
- Reuse certification, exact identity, current membership, offer, capacity and hold
  checks. Missing evidence remains excluded, never replaced with invented clear state.
- A completed rest period returns through the same ordinary tier permissions and
  capacity checks. Recompute age on return, preserve its original text, and remove
  only that cycle's own hold in the proposed patch. Other holds and opt-outs block
  return. Rest completion alone never labels a lead Reactivation. The legacy live
  selector remains unchanged and is not the entry point for this local planner.
- Trial access is explicit and temporary. The approved fixed predictive/control
  roster can receive the same Top genuine-opt-in population during the trial without
  permanent Top promotion. Retain offer restrictions and balance within each offer
  and source stratum; show unmatched offers instead of crossing offer boundaries.
- New hires remain on older Ramp Power inventory before live Week 4 and outside the
  core comparison. At Week 4 they use a separate predictive segment, subject to their
  ordinary approved/earned lead access. Week 4 does not itself grant Top access.
- Exclude inbound work, webinar follow-up, QA and closers from the comparison. A
  genuine opt-in's acquisition source is distinct from its current workstream.
- Export a versioned, hashed local distribution receipt. Dialer cohort code imports
  the shared trial participation policy; report/workflow dry runs can consume the
  receipt and reject calls outside its lead, arm, segment and validity window.

## Local completion and verification

Provide a no-network CLI that consumes a complete local snapshot and writes private
plans, queue definitions and dialer receipts. It deliberately has no `--apply` mode.
Include deterministic scenarios covering all tiers, source guards, performance gates,
owner retention/releases, opt-outs, tasks/workflows, holds, stale or missing evidence,
trial expiry, per-offer capacity, new-hire dates, duplicate inputs and repeated runs.
Replay the existing captured preview without inventing unavailable hold evidence.
Run the existing distribution/hold regressions and the dialer tests, plus the unified
local end-to-end path. A successful local dry run does not certify production readiness.

From the dialer folder, run `../.local-automation-venv/bin/python verify_local.py
--output private/unified-verification-new` for the repeatable offline check. Its output
contains a verification receipt, test logs, synthetic distribution and rest-return
plans, reporting/follow-up previews, and a replay of the original captured snapshot.
The live attachments diagnostic is excluded; all unit-test sockets are blocked.

For a custom local snapshot, run `python3 distribution_local.py --snapshot INPUT.json
--trial-config TRIAL.json --at 2026-11-09T17:00:00Z --output output/new-run`.
Use schema version 1; `distribution_fixture.py` generates an example. Optional
`rest_returns` cycles reference leads in the same snapshot and receive separate
`rest-return-plans.json` output. Those proposals reserve capacity after the ordinary
allocation preview but do not clear the real hold or assign any record.

## Activation boundary

Production migration, all-writer hold adoption, provider field/view installation,
live permission changes and scheduler deployment remain separate from this local
request. Keep the existing live pilot gates intact. The snapshot and receipt expose
their capture/evaluation times and completeness; they are not executable approvals.
