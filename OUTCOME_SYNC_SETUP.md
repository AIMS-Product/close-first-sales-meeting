# Outcome Sync — Setup Guide

Drop-in addition to `close-first-sales-meeting`. Two new files, zero changes
to `update_field.py`:

```
close-first-sales-meeting/
├── outcome_sync.py                      # NEW
├── .github/workflows/outcome-sync.yml   # NEW
└── (everything else untouched)
```

## What it does every 30 minutes

For each **past** meeting in the last 7 days (excluded owners skipped):

1. **Already has a terminal outcome?** → skip. The script only ever writes when
   the outcome is blank or "Scheduled" — a rep's manual edit is never overwritten.
2. **Zoom attendance** — parses the Zoom meeting ID from the meeting's join link,
   pulls the participant report, matches the prospect by attendee email (fuzzy
   name match covers phone/renamed joins):
   - prospect on ≥ 5 min → `Completed`
   - prospect absent AND host on ≥ 10 min → `No Show` (listed as "auto no-show"
     in the report for review; disable entirely with `ZOOM_AUTO_NOSHOW=0`)
   - prospect on < 5 min, host barely present, or no Zoom data → **flag, no write**
3. **Canceled in Close** (status or "Canceled:" title) → `Rescheduled` if a later
   booking exists on the lead, else `Cancelled`.
4. **Phone conversation** — an answered Close call of at least 5 minutes on the
   meeting's day → `Completed` when Zoom is inconclusive.
5. **Lead status + attendee RSVP** — a Canceled or No Show lead status is used
   only when every external attendee declined or did not reply.
6. **No signal** → left blank and flagged in the completeness report — this is the
   "every show gets logged" guarantee: flagged meetings print as review links
   (`https://app.close.com/lead/...`) in the Actions log and land in
   `outcome_sync_report.json` (uploaded as a workflow artifact).

Residual Google Meet meetings simply have no Zoom link → they resolve through
Close call evidence or are flagged. No Meet integration needed while that tail shrinks.

## Setup steps

### 1. Create the Zoom Server-to-Server OAuth app (one-time, ~5 min)

1. [marketplace.zoom.us](https://marketplace.zoom.us) → **Develop → Build App →
   Server-to-Server OAuth** (must be created by a Zoom admin on the account that
   hosts the sales meetings).
2. Add scopes: `report:read:admin` and `meeting:read:admin`
   (granular equivalents: `report:read:list_meeting_participants:admin`,
   `meeting:read:list_past_instances:admin`).
3. Activate the app, copy **Account ID / Client ID / Client Secret**.

> Requires a paid Zoom plan (reports API). The meetings must be hosted on this
> Zoom account for participant reports to exist.

### 2. Add repo secrets

GitHub → repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `ZOOM_ACCOUNT_ID` | from step 1 |
| `ZOOM_CLIENT_ID` | from step 1 |
| `ZOOM_CLIENT_SECRET` | from step 1 |

(`CLOSE_API_KEY` already exists.)

The workflow requires all three Zoom secrets. A missing secret or rejected
OAuth token stops the sync before it writes to Close. The error log and uploaded
report include Zoom's error category without printing the credentials. For an
HTTP 400, compare the Account ID, Client ID, and Client Secret against the same
Server-to-Server OAuth app's **App Credentials** page, then verify that app is
activated. The Zoom account's profile number is not the app's Account ID.

### 3. Commit the two files, run in dry-run

The workflow ships with `DRY_RUN: "1"` — it logs every decision and writes
nothing. Trigger it manually (Actions → Meeting Outcome Sync → Run workflow) and
read the log. You want to sanity-check:

- SET lines look right (source + outcome match what you'd expect for those leads)
- auto no-shows are genuine no-shows (spot-check 3–5)
- FLAG list is small and explainable (mostly Google Meet residuals at first)

### 4. Go live

Edit one line in `outcome-sync.yml`: `DRY_RUN: "1"` → `"0"`. Done. To also
trigger via cron-job.org (like the field updater), point a second cron at the
`outcome-sync.yml` dispatch endpoint — or rely on the built-in schedule, which
is fine here since outcomes aren't as time-sensitive as FSCBD stamping.

## Tuning knobs (env in the workflow)

| Var | Default | Meaning |
|---|---|---|
| `LOOKBACK_DAYS` | 7 | how far back to scan past meetings |
| `MIN_ATTEND_SECONDS` | 300 | prospect time to count as attended |
| `HOST_MIN_SECONDS` | 600 | host presence required before an auto No Show |
| `ZOOM_AUTO_NOSHOW` | 1 | set 0 to make Zoom absence always flag instead |

## Constants to keep in sync with update_field.py

- `EXCLUDED_OWNER_NAMES` (Stephen Olivas, Ahmad Bukhari)

That's the only shared constant — the sync deliberately doesn't classify titles,
because outcomes apply to every external meeting, not just first sales calls.

## Testing without touching anything

```
python outcome_sync.py --selftest   # 12 decision-logic tests, no network
```

## What this does NOT do (by design)

- Never overwrites a manually-set or terminal outcome.
- Never marks No Show from Zoom absence alone (host must have been present).
- Doesn't touch custom fields, FSCBD, or any existing field-updater behavior.
- Doesn't write outcomes on future meetings (they keep their Scheduled default).
