# Native meeting outcome reconciliation

The durable backlog covers Anthony's exact 320 historical Lane 2 activities plus
all supported meetings from November 1, 2026 onward. This fixed start never moves
forward, so new unresolved meetings cannot age out. Native Close search cursors
select open outcomes and recent terminal meetings for field projection; the
historical seed is always re-read. `LOOKBACK_DAYS=0` keeps that entire scope.
Positive values are for bounded read-only investigation. The broader 2024–2025
organization history is deliberately outside this historical repair.

Avoma tags normally get the first three hours after a meeting ends. The Zoom
backup then uses one unambiguous occurrence within 30 minutes of the appointment,
all participant pages, and unioned attendance intervals. It ignores known bots
and waiting-room entries, and counts only time after the scheduled start. A
verified prospect attending five minutes establishes Completed. A host waiting
at least ten minutes can establish No Show only with no identified prospect or
unidentified human, and no meeting-proximate answered phone evidence. A short
prospect join or unknown human requires review. Missing Zoom data is not absence.
Current lead status and RSVP are not proof of attendance at a historical meeting.
Prospect change requests block an automated no-show and carry the source activity
IDs into review. Replacement Zoom rooms sent during the appointment can recover
verified attendance, but never establish an automated negative.

Only blank/Scheduled native outcomes are written. The writer re-reads meeting
identity and the outcome immediately before PUT and verifies readback afterward.
Existing terminal outcomes, future bookings and qualification remain protected.
First Call Show Up projects only from the earliest eligible first-call outcome.

Reports retain meeting ID, host ID, reason, and a fixed deadline of meeting end
plus 48 hours. Overdue review or source/write errors return a nonzero exit;
a successful API run cannot conceal an incomplete attendance backlog. Original
hosts are the evidence owners; managers must reassign former hosts' reviews.
No Unknown outcome or payout eligibility is inferred from missing evidence.

Validation: `python -m unittest test_outcome_sync test_outcome_coverage test_native_outcome_backlog`.
Use workflow dispatch with `dry_run=1` for a read-only production-source replay.
Rollback: restore the previous code and set `ZOOM_AUTO_NOSHOW=0`; preserve already
recorded native outcomes and their receipts. No ledger records are modified.
