# Close-only HOT/status maintenance

Build status: guarded batches and a dedicated scheduled worker are implemented. The worker defaults to disabled; follow the startup and activation gates below before enabling writes. The default command only reads Close. No Close status definitions, saved views, subscriptions, queues, owners, or custom fields are changed by a preview.

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

## Bounded write paths

The default CLI remains preview-only. An authorized bounded apply requires BOTH `--apply` and `CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES=1`. `DRY_RUN=0` alone cannot enable maintenance writes. An apply requires exact `--lead-ids`, uses fresh per-lead evidence rather than the batch cache, and defaults to at most 10 changed leads (`--max-changes`). Example: `CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES=1 python close_status_maintenance.py --lead-ids /private/path/pilot.json --apply --max-changes 10 --output /private/path/new-pilot`. Do not enable writes during a preview.

The executor re-reads and replans before either status-only PUT, refreshes unsubscribe evidence, and requires the same owner before writing the lead. It writes and fsyncs an intent receipt before each request. Unknown write outcomes are not retried blindly. An owner, inbound, meeting or suppression race between the two records stops the second write and emits a partial result for reconciliation. Close does not provide an atomic transaction spanning opportunity and lead updates; a final-read-to-PUT race cannot be eliminated. No rollback overwrites intervening business activity.

Full-history reads intentionally favor complete suppression evidence. A full-cohort scan can be expensive; measure its runtime in preview before choosing a cadence or enabling a schedule. The full-current-cohort read-only preview on October 20, 2026 reconciled 9,530 leads. It retained all 75 explicit exclusions (73 in this cohort; two outside), excluded active closers, completed 8,894 activity-evidence evaluations, and rechecked all 4,936 initial proposals against current lead/owner/contact/opportunity evidence. Final output: 4,854 proposals, 3,533 unchanged, 1,143 held, zero unresolved read errors, and zero Close writes. The 82 records changed during preview were held. The preview used a batched read-only transport over the same policy/reader; its 10.4-minute runtime through final checks is not a production timing measurement of the default per-lead CLI. The production CLI now supports `--batched` previews using the same policy and hydration/fence checks; this mode cannot apply changes. This was the original preview; current rollout and deployed schedule evidence are retained separately. Full preview evidence is retained in the private local task artifacts.

## Scheduled maintenance worker

`close_status_cycle.py` implements a separate Railway cron worker. Its default is disabled. Each enabled run inventories all four follow-up statuses read-only, selects at most `CLOSE_STATUS_MAX_CHANGES` (default 200), and uses independently verified batches of at most 100. Every write uses new owner, meeting, suppression, opportunity, and inbound evidence. Full histories are never reused between validation waves. Open tasks use Close's documented single-lead filter, paginate to completion, and reject a response outside that lead/organization or with an invalid completion flag. Deferred requests of the same kind coalesce into one fresh snapshot wave.

The dedicated worker does not replace the existing opportunity-to-lead sync or change its schedule. It reuses the existing Close secret and Lakebase roster credentials through service references. No SteelTrap database or immutable-ledger writes occur.

### Deployment

Create an isolated upload directory containing the published `close_status*.py` modules, both sealed exclusion JSON files, `requirements-status-maintenance.txt`, and `Dockerfile.close-status-maintenance` copied as **Dockerfile**. Do not upload environment files, local evidence, tests, caches, or other automation scripts.

Use a dedicated service and a persistent `/data` volume in the existing Railway production project. Apply `railway.close-status-maintenance.graphql` with that service's exact `serviceId` and `environmentId`, then upload the isolated bundle. The explicit service settings select the conventional Dockerfile, start command, hourly schedule at minute 17, one replica, and no automatic restart. They avoid the deprecated railway.toml configuration path. Check effective service settings and the mounted volume after deployment.

Set these variables through the existing secret mechanism:

| Variable | Value or purpose |
| --- | --- |
| `CLOSE_API_KEY` | Existing Close secret/service reference |
| `LAKEBASE_HOST`, `LAKEBASE_DATABASE`, `LAKEBASE_USER`, `LAKEBASE_PASSWORD` | Existing roster credentials/service references; SQL forces read-only transactions and verified TLS |
| `CLOSE_STATUS_DATA_DIR` | `/data` |
| `CLOSE_STATUS_SOURCE_REVISION` | Published source Git SHA |
| `CLOSE_STATUS_PROOF_LEAD_ID` | One exact authorized native lead used only for a tenant proof GET |
| `CLOSE_STATUS_STARTUP_CHECK_ONLY` | Start at `1`; executes only roster/exclusion/tenant read checks |
| `CLOSE_STATUS_MAINTENANCE_ENABLED` | Start at `0`; `1` permits normal cycles after startup-only is disabled |
| `CLOSE_STATUS_MAINTENANCE_ENABLE_WRITES` | Start at `0`; enabled cycles also require `1` |
| `CLOSE_STATUS_MAX_CHANGES` | `200` initially; validated range 1–1000; each write batch remains capped at 100 |
| `CLOSE_STATUS_REQUESTS_PER_SECOND` | `5` by default; validated range 1–10; all concurrent reads and writes share one limiter |

First run in startup-check-only mode and verify a private startup receipt, expected Close organization, current active-closer roster, all 157 exclusions, and zero Close writes. Finish the authorized cleanup and its verification before enabling recurring writes. Set startup-only to `0`, enabled to `1`, and the write gate to `1`; observe an actual complete cron run and its durable summary before calling the schedule operational.

### Receipts, interrupted runs, and stopping

A persistent-volume file lock serializes overlapping starts. Every run writes private evidence beneath `/data/runs/`. Before snapshots and write intents are fsynced before a PUT. Independent verification reads native leads and targeted opportunities, compares unrelated business fields, reconciles exact path/status/time receipt pairs, and replays the current policy.

Legacy Unresponsive consolidation may produce one subsequent HOT promotion for recent inbound. That follow-up receives its own fresh checks, receipts, and independent verification. Any other verification mismatch stops the cycle. The original receipts remain intact; a separate reconciliation receipt records a successful follow-up.

If a process exits after any write intent, `/data/active-run.json` blocks the next cycle. Do not delete it to retry. Reconcile native state read-only against `before-leads.ndjson`, the write journal, and result records, retaining the original evidence. Unknown PUT outcomes are not retried blindly, and intervening business activity is never rolled back. Only after reconciliation should an operator clear the interlock and resume.

To stop future writes, set `CLOSE_STATUS_MAINTENANCE_ENABLED=0` and deploy that gate. Allow an already-running batch to finish and verify; an abrupt stop leaves the reconciliation interlock in place. Schedules that overlap an active run do not start a second writer. No automatic messaging or notifications are sent.


## Offline verification

```sh
python -m unittest test_close_status_batch test_close_status_maintenance test_close_status_sync_guard test_close_status_live test_close_status_cycle
python sync_opp_lead_status.py --selftest
python -m compileall -q close_status_policy.py close_status_reader.py close_status_maintenance.py sync_opp_lead_status.py
```

All write-order/race tests use fake clients. No test requires live writes. No database migration or immutable-ledger backfill is part of this feature.
