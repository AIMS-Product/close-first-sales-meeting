# Close-only HOT/status maintenance

Build status: tested for a bounded live pilot; **ongoing maintenance is not scheduled**. The default command only reads Close. No Close status definitions, saved views, subscriptions, queues, owners, or custom fields are changed by a preview.

## Rules

- A verified inbound SMS, incoming email, inbound call, or native Close form submission starts a seven-calendar-day elapsed window. Use actual event/sent timestamps, not lead edits or rep activity. At exactly seven days, HOT expires.
- Only HOT, General Follow Up, Non-responsive, and legacy Unresponsive are in scope. Fresh inbound may promote General Follow Up or Non-responsive to HOT; booked, won, lost, DNC and other native statuses are not promoted by this worker. Legacy Unresponsive normalizes to Non-responsive first.
- Expired HOT becomes General Follow Up. If the controlling opportunity would restore HOT, update that opportunity first, then the lead. A latent HOT opportunity on a Non-responsive lead is aligned to Non-responsive without reactivating the lead.
- Never create opportunities or alter unrelated opportunities. The most recently updated opportunity controls; tied, conflicting or non-Sales-Pipeline winners require review. Any won/DNC opportunity, future meeting, active closer as lead owner or active-opportunity owner blocks maintenance.
- Historical STOP keywords and DNC status history block changes. Unsubscribe flags, global email unsubscribes, blocked phones, and legacy DNC fields are checked. Ambiguous opt-out text, open callback tasks, recent callback/form notes, unverified inbound senders and recent WhatsApp messages require review. A new reply never clears historical suppression automatically.
- The explicitly held 75 leads are excluded before even reading their Close record. `close_status_held_leads.json` stores hashes of their native IDs; the separately sealed `close_status_deferred_leads.json` blocks the 82 changed records as well; an integrity check rejects a missing or modified manifest. No customer names, emails, phones or message bodies are in this configuration.

## Run a preview

Use Python 3.11+ and install `requirements-status-maintenance.txt`. Existing local automation Python also works. Supply `CLOSE_API_KEY` through the existing secret mechanism.

```sh
python close_status_maintenance.py --lead-ids /private/path/lead-ids.json --batched --output /private/path/new-preview
```

`lead-ids.json` must be a unique JSON list of exact Close lead IDs. `--scan-follow-up` inventories all four statuses instead; it partitions broad searches to respect Close's 10k search ceiling. `--limit` bounds a preview. The complete inventory is collected before any potential apply. Search uses stable creation-date sorting and reconciles each partition against the reported count. Duplicated or incomplete pagination fails closed.

The active-closer directory uses the existing Lakebase company roster and existing `LAKEBASE_HOST`, `LAKEBASE_DATABASE`, `LAKEBASE_USER`, `LAKEBASE_PASSWORD` credentials. The SQL transaction is forced read-only and retains a source receipt. Verified TLS uses Requests' CA bundle, with optional `LAKEBASE_SSLROOTCERT` for an existing trust bundle. No new account, profile or secret is needed.

An alternative `--roster` / `CLOSE_STATUS_ROSTER_FILE` is a fresh authoritative roster export:

```json
{"company_id":"88a28173-2a61-4cbb-92ea-f6b8be4592f3","organization_id":"orga_Qwbx9tyUrl7Jdjurg9xJ8N6w1MN4PD2NQOgIhd8r7rC","captured_at":"2026-10-20T22:00:00Z","active_closer_ids":["user_example"]}
```

The example is a schema illustration, not a usable production roster. The directory must be nonempty, tenant-matched and less than 24 hours old; missing credentials or stale exports stop the run. The roster is control-plane ownership metadata; all lead, communication and suppression decisions use Close only.

Outputs are private local `results.json`, `summary.json`, `read-trace.json` and `roster-source.json`. Preview transport rejects PUT and all non-search POSTs. Messages, calls and notifications are never sent.

## Sync integration

`sync_opp_lead_status.py` now consults the same policy before restoring HOT, and excludes the 75 held leads from all of its writes. If the policy cannot read a fresh roster, unsubscribe snapshot or complete lead history, it leaves HOT unchanged and reports a guard failure. Other sync targets retain their existing behavior. Configure the existing runtime with the same roster source and dependencies before deploying this integration. The local main-only fallback imports only the four existing roster credentials from the adjacent SteelTrapAuditLog environment when not already configured; only the status-sync job receives them. GitHub Actions uses encrypted repository secrets. Neither path creates a new database account or changes source .env files.

There were no legacy Unresponsive references in the existing sync map to replace. Close saved-view changes and deletion of the old native status are deferred; protected and held leads still use it.

## Write path, kept disabled

There is no new cron schedule or workflow trigger. An authorized bounded apply requires BOTH `--apply` and `CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES=1`. `DRY_RUN=0` alone cannot enable maintenance writes. An apply requires exact `--lead-ids`, uses fresh per-lead evidence rather than the batch cache, and defaults to at most 10 changed leads (`--max-changes`). Example: `CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES=1 python close_status_maintenance.py --lead-ids /private/path/pilot.json --apply --max-changes 10 --output /private/path/new-pilot`. Do not enable writes during a preview.

The executor re-reads and replans before either status-only PUT, refreshes unsubscribe evidence, and requires the same owner before writing the lead. It writes and fsyncs an intent receipt before each request. Unknown write outcomes are not retried blindly. An owner, inbound, meeting or suppression race between the two records stops the second write and emits a partial result for reconciliation. Close does not provide an atomic transaction spanning opportunity and lead updates; a final-read-to-PUT race cannot be eliminated. No rollback overwrites intervening business activity.

Full-history reads intentionally favor complete suppression evidence. A full-cohort scan can be expensive; measure its runtime in preview before choosing a cadence or enabling a schedule. The full-current-cohort read-only preview on October 20, 2026 reconciled 9,530 leads. It retained all 75 explicit exclusions (73 in this cohort; two outside), excluded active closers, completed 8,894 activity-evidence evaluations, and rechecked all 4,936 initial proposals against current lead/owner/contact/opportunity evidence. Final output: 4,854 proposals, 3,533 unchanged, 1,143 held, zero unresolved read errors, and zero Close writes. The 82 records changed during preview were held. The preview used a batched read-only transport over the same policy/reader; its 10.4-minute runtime through final checks is not a production timing measurement of the default per-lead CLI. The production CLI now supports `--batched` previews using the same policy and hydration/fence checks; this mode cannot apply changes. Ongoing cadence remains disabled. Full preview evidence is retained in the private local task artifacts.

## Offline verification

```sh
python -m unittest test_close_status_batch test_close_status_maintenance test_close_status_sync_guard
python sync_opp_lead_status.py --selftest
python -m compileall -q close_status_policy.py close_status_reader.py close_status_maintenance.py sync_opp_lead_status.py
```

All write-order/race tests use fake clients. No test requires live writes. No database migration or immutable-ledger backfill is part of this feature.
