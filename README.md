# First SALES Meeting Field Updater

Populates the **"First Sales Call Booked Date"** custom field on Close CRM leads with the date of each lead's earliest qualifying first sales meeting.

Runs every 30 minutes via GitHub Actions + cron-job.org.

---

## What It Does

Our current custom "First Call Booked Date" field is unreliable — it updates on reschedules and includes non-sales meetings. This script writes a clean, filtered date to a custom field using the same title classification logic that powers the Call Capacity and MTD Funnel dashboards.

**Result:** Marketing (and anyone else) can filter leads in Close with:
- `First Sales Call Booked Date = Yesterday` + `Funnel Name Deal = Instagram`

...and get an accurate count of leads that had a first sales meeting booked — no external dashboard required.

---

## Classification Rules (applied in order)

### Always excluded
- Title starts with `Canceled` (with or without colon)
- Title contains follow-up/reschedule patterns: `follow-up`, `follow up`, `fallow up`, `F/U`, `Next Steps`, `Rescheduled`, `reschedule`
- Title contains both `Anthony` and `Q&A` (group Q&A sessions)
- Title contains enrollment patterns: `enrollment`, `Silver Start up`, `Bronze enrollment`, `questions on enrollment`

### Not a sales meeting
- Title contains `Vending Quick Discovery`
- Meeting owned by Kristin Nelson or Spencer Reynolds

### Excluded owners (meetings fully ignored)
- Stephen Olivas
- Ahmad Bukhari

### Qualifying titles (ONLY these count)
- `Vending Strategy Call`
- `Vendingprenuers Consultation` (handles misspellings)
- `Vendingprenuers Strategy Call` (handles misspellings)
- `New Vendingpreneur Strategy Call`
- `Vending Consult` (partial match, e.g. "Vending Consult w/Dillan")

---

## Custom Field

| Property | Value |
|----------|-------|
| Field name | First Sales Call Booked Date |
| Field ID | `cf_LFdYEQ6bsgp49YjZzefypDmdVx8iwuakWDSLPLpVrBq` |
| Type | Date |
| Object | Lead |

---

## Setup

### 1. Add the API key secret

In your GitHub repo → Settings → Secrets → Actions:

| Secret | Value |
|--------|-------|
| `CLOSE_API_KEY` | Your Close CRM API key |

### 2. Set up cron-job.org (primary trigger)

Create a job at [cron-job.org](https://cron-job.org) to hit the GitHub Actions `workflow_dispatch` endpoint every 30 minutes:

```
URL: https://api.github.com/repos/YOUR_ORG/YOUR_REPO/actions/workflows/update-field.yml/dispatches
Method: POST
Headers:
  Authorization: Bearer YOUR_GITHUB_PAT
  Accept: application/vnd.github+json
Body: {"ref": "main"}
Schedule: Every 30 minutes
```

The workflow also has a built-in `schedule` cron as a backup.

### 3. Run the initial backfill

Trigger the workflow manually via GitHub Actions → Run workflow. The first run will scan all meetings and populate the field for every lead that has a qualifying meeting. Expect 10-15 minutes.

---

## Combined meeting field updater

`run_meeting_updates.py` runs First Sales Meeting Field and Follow-Up Dates together.
Use `--verify` to compare exact full-scan and incremental proposals, including cleared
values. This is a read-only command unless `--apply` is supplied.

```sh
python3 run_meeting_updates.py --verify
```

The first live invocation requires `--verify --apply` and applies only after the
proposals and affected histories match. It uses the existing guarded writer
functions and commits both caches and the checkpoint together. Afterwards, use
`--apply` for the normal cycle. It uses Close's Event Log with a two-hour overlap,
deduplicates previously evaluated event revisions, and fetches affected lead
histories once for both field writers. A full reconciliation runs daily and on
incomplete discovery. `--apply --full` forces the complete scan.

The `Update Close Meeting Fields` workflow is dispatch-only. For cutover, stop both
legacy cron triggers and let any active runs finish. Dispatch the combined workflow
once with `apply=true` and `verify=true`. After a successful initial run, retain the
`:00/:30` cadence on one cron trigger with `apply=true` and `verify=false`.
For rollback, pause the combined trigger and restore the two legacy triggers.

---

## Meeting update shadow comparison

Run this on a host with Python 3.11 or newer, `requests`, the Databricks CLI, a read-capable
Close API key, and read access to the SteelTrap Silver meeting-event ledger. Use a
persistent, private directory for the SQLite checkpoint and reports. Use the latest
production copies of `state_cache.json` and `followups_state_cache.json` through
`--cache-dir` when the shadow code and production checkout are separate.

```sh
install -d -m 700 "$HOME/.local/state/close-meeting-shadow"
python3 compare_meeting_updates.py \
  --warehouse "$DATABRICKS_WAREHOUSE_ID" \
  --state-db "$HOME/.local/state/close-meeting-shadow/shadow.sqlite"
```

The first run reads two hours of meeting changes. Subsequent runs replay two hours
before the previous cutoff through the new cutoff and also read Delta inserts since the
previous committed ledger version. This captures events delivered after the earlier
run, even when their source timestamps are older than the overlap. Both lead IDs are
included when a meeting moves. The Close Event Log is a separate discovery control.
Each affected Close lead's full meeting history is fetched once and reused across both
writers. The full scan remains the baseline for finding work outside the discovered set.

The comparison records exact proposals before the transport and hold checks. The
shadow runner uses read-only Close calls and leaves both production caches to the
scheduled writers. Only aggregate counts print to stdout. The private SQLite
`runs` table holds exact proposal values and difference details, including lead IDs.
Review source differences, history changes during capture, and any full-scan proposal
outside the incremental set before counting a run as a clean comparison.

A completed comparison saves its report and checkpoint in one transaction. A failed
or overlapping run cannot advance the checkpoint. `--replay-until` accepts a
timezone-qualified ISO 8601 cutoff for a two-hour historical discovery replay. It
uses current Close histories and lead values and keeps the live checkpoint intact.

---

## Performance

| Step | API calls |
|------|-----------|
| Paginate all meetings (~10,600) | ~107 GET calls |
| Fetch + update changed leads | varies (initial: hundreds; ongoing: ~10-50) |
| Clear stale field values | minimal |

- All calls throttled at 0.5s sleep between requests
- No threading or concurrency (avoids rate limit errors)
- Uses `_fields` parameter to minimize payload on lead fetches
- Runtime: 3-10 min depending on update volume

---

## Related Repos

- [call-capacity-dashboard](https://github.com/stephenolivas/call-capacity-dashboard)
- [mtd-funnel-reporting](https://github.com/stephenolivas/mtd-funnel-reporting)
