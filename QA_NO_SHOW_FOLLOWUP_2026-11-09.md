# Next milestone: QA booking no-show recovery

User-requested order: first activate the closer-marked Lost handoff and existing
QA lifecycle. Start this recovery milestone after that cutover is live.

Anthony's November 9 policy:

- The Lane 2 booking rep, including Naria, owns the rebook.
- Day 0: automated text and email 10–15 minutes after the no-show; call task due
  within two hours. Day 1: automated text. Day 2: rep call. Day 3: automated email.
  Day 5: rep call. Day 7: final close-it-out text.
- Stop automated recovery on a reply, a new booking or STOP. Honor existing
  channel opt-outs and DNC protections before every message or enrollment.
- If a rebook shows, preserve the original booking rep's credit even when a
  closer books the replacement live. Actual reporting integration is separate
  from the automation, per the user's reporting deferral.
- If no response after day 7, retain the rep through 30 days after the missed
  meeting. Manual outreach may continue. No new meeting at day 30: move to
  Non-responsive, rest for 90 days, then enter Lane 2 through the existing tier,
  permission and capacity rules.
- A lead that passed through QA never enters QA again. This also covers a QA
  lead later marked Lost by a closer. Keep this exclusion through recovery,
  Non-responsive rest and tier re-entry.

First-release protections retain Naria's current QA no-shows and block a second
QA admission. The seven-day communication chain and 30/90-day transitions are a
separate implementation; do not represent them as live from those protections.

Before enabling messages, inspect and reuse the current Close recovery workflow,
templates, sender identities and stop conditions. Rep calls are tasks, not
automated calls. Use durable cycle identities, owner checks and single-attempt
delivery receipts so retries do not send duplicate outreach. Preserve the missed
meeting's original timestamp; unrelated edits must not restart the 30-day clock.

November 10 protection correction: the same booking protection now covers
cancellations and reschedules as well as no-shows. Status-only cleanup cannot
clear QA ownership. Initial uncontacted QA leads still use the five-business-day
window; booked recovery never falls through to that initial outreach timer.
At 30 days, terminal handling remains a distinct held handoff as documented above.
