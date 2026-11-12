# Lead Tier maintenance

Enabled by Dom on November 12, 2026. The dedicated `Refresh Close Lead Tier` workflow runs daily at 11:20 UTC. Manual runs preview by default; choose apply to update the field.

- Only the Lead Tier custom field is updated. No owner, status, task, workflow, message or rep-access change is made.
- Pacific Close creation age drives classification. Imported-list provenance takes precedence and stays Ramp at every age.
- Genuine opt-ins: Top at 0–7 days; Proven at 8–29 and 61–179; Ramp at 30–60 and 180–364; Reactivation at 365+. First-week non-opt-ins are Ramp.
- Every run captures a fresh complete export and verifies import provenance. Invalid inputs stop writes; changed old tier values are guarded. Uncertain POSTs are not blindly retried.
- Runs do not overlap. Reports and submitted bulk-action identities are retained as private workflow artifacts for 14 days.
- This workflow does not activate V2 assignment, release or recovery messaging.

Rep tiers are separate from lead age tiers. The weekly promotion review retains each rep's current approved tier as a floor. Promotions require approval; a missed goal or missing data never causes demotion.
