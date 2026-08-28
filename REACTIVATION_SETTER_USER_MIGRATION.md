# Reactivation Setter User Migration Map

## Fields

Old field:
- Label: `Reactivation - Setter Name`
- Type: text/dropdown-style name value
- ID: `cf_vz6kNiu4ItFxRA8Y9HKlWIoQMq3TsdaQqKekQ2YuxVk`
- API key: `custom.cf_vz6kNiu4ItFxRA8Y9HKlWIoQMq3TsdaQqKekQ2YuxVk`

New field:
- Label: `Reactivation Setter User`
- Type: user field
- ID: `cf_7W3UCpJWWaIQsniF1upSxGO7rMX1yDT5qppHXBGJIhO`
- API key: `custom.cf_7W3UCpJWWaIQsniF1upSxGO7rMX1yDT5qppHXBGJIhO`

Current sync status after the initial backfill:
- `1,247` leads are synced.
- `0` mappable leads remain with the new user field blank.
- `2` leads remain unmapped because `Reactivation - Setter Name = Mariam Olufumi` does not map to a Close user.

Implementation status:
- `update_field.py` now fetches Close users and dual-writes `Reactivation Setter User` when a scraper setter name maps to exactly one Close user.
- Routine runs can backfill `Reactivation Setter User` when `Reactivation - Setter Name` is already set and the new user field is blank.
- Populated `Reactivation Setter User` values are not overwritten automatically; mismatches are logged for review.
- `dry_run_reactivation_setter_user_sync.py` de-dupes candidate leads before reporting/apply.
- `no_show_recovery.py` only maintains the `No show recovery` flag; it no longer assigns or clears `Lead Owner`.

## Migration Strategy

Phase 1: dual-write only.
- Keep writing the old name field exactly as-is.
- Also write the new user field when the setter name maps to a Close user.
- Do not change dashboard/report reads yet unless needed for a smart-list user check.

Phase 2: use the new user field for Close smart lists.
- Update saved searches that need `me` checks to filter on `Reactivation Setter User`.
- Keep old `exists` filters only where they are intentionally historical or text-based.

Phase 3: migrate readers gradually.
- Dashboards can continue displaying setter names, but should prefer the user field for identity and fall back to the old name field for historical/unmapped rows.
- After all reads are stable, the old field can remain as a human-readable shadow or be retired later.

## Name To User Mapping Requirement

`update_field.py` produces a setter name from `SCRAPER_TITLE_MAP`. The new user field needs a Close `user_xxx` id, so the script resolves setter display names against `/user/` at runtime.

Implemented pattern:
- Fetch Close users at runtime and normalize by full display name.
- If a name maps to exactly one user, write the new user field.
- If a name is missing or ambiguous, keep writing the old name field and log/skip the new user field.
- Never guess.

Known unresolved value:
- `Mariam Olufumi`

## Change Map

### 1. `close-first-sales-meeting/update_field.py`

Role:
- Primary writer for `Reactivation - Setter Name`.
- Maps scraper meeting titles to setter names.
- Writes old field only when blank.

Current references:
- Field constant: `FIELD_REACTIVATION_ID`
- API key: `FIELD_REACTIVATION_KEY`
- Current-field fetch: `FIELDS_PARAM`
- Setter source: `SCRAPER_TITLE_MAP`
- Write block: `write_lead()`, `payload[FIELD_REACTIVATION_KEY] = new_reactivation_label`
- State cache keys: `reactivation`

Implemented change:
- Added `FIELD_REACTIVATION_USER_ID = "cf_7W3UCpJWWaIQsniF1upSxGO7rMX1yDT5qppHXBGJIhO"`.
- Added `FIELD_REACTIVATION_USER_KEY`.
- Fetches the new user field in `FIELDS_PARAM`.
- Builds a setter-name-to-user-id map from Close users.
- In `write_lead()`, when writing a new old-field setter name, also writes the new user field if mapped.
- If the old field is already set but the new user field is blank, backfills the new user field during normal runs.

Suggested behavior:
- Old field remains the source of historical text display.
- New field is populated with a user id.
- Do not overwrite a populated new user field unless we explicitly decide to correct mismatches.

Risk:
- State cache now stores `"reactivation_user"` for fetched/updated leads. This avoids routine runs skipping leads only because the old `"reactivation"` value has not changed.

### 2. `close-first-sales-meeting/dry_run_reactivation_setter_user_sync.py`

Role:
- Backfill/audit helper for old-field to new-field sync.

Current status:
- Already knows both field IDs.
- `--apply` writes only the new user field for rows where the new field is blank and the old name maps cleanly.

Implemented change:
- Kept as an audit/repair tool.
- Added duplicate lead de-duping in `fetch_candidate_leads()` and before the apply loop, because Close cursor results included one duplicate during the apply snapshot.

### 3. `close-first-sales-meeting/no_show_recovery.py`

Role:
- Maintains the `No show recovery` flag for eligible no-show leads.
- Leaves `Lead Owner` unchanged during no-show recovery.

Current references:
- `NO_SHOW_RECOVERY_FIELD`

Implemented change:
- Removed Lead Owner reassignment and release logic from no-show recovery.
- Removed the no-show recovery dependency on `Reactivation Setter User` and `Reactivation - Setter Name`.

Suggested behavior:
- Keep no-show recovery focused on the recovery flag only.

### 4. `close-first-sales-meeting/update_funnel_name.py`

Role:
- Uses the old setter name field as a gate for marking leads as `Reactivation Scrapers`.
- Creates review tasks with the setter name in the task text.

Current references:
- `SETTER_NAME_FIELD_ID`
- `SETTER_NAME_DISPLAY_NAME`
- Reads `setter = custom_value(lead, SETTER_NAME_DISPLAY_NAME)`

Required change:
- For gate logic, either continue requiring the old name field or allow either old name or new user field to count as setter-attributed.
- For task text, still use old name for readability, or resolve the new user id to a display name.

Suggested behavior:
- During dual-write, leave this mostly old-field based.
- Add new user field as fallback/consistency check only.
- Later, prefer new user field for identity and old field for task display text.

### 5. `Lane-2-dashboard/scripts/fetch_data.py`

Role:
- Attributes scraper activity, closes, revenue, set/booked/shown metrics.
- Currently compares old text field values to configured names.

Current references:
- `SCRAPERS[].setter_field_value`
- `SETTERS[].scraper_setter_field_value`
- `FIELD_SETTER_NAME`
- `setter_name_val = get_custom(ld, FIELD_SETTER_NAME)`
- `setter_to_uid = {s["setter_field_value"]: s["user_id"] for s in scrapers}`

Required change:
- Add `FIELD_SETTER_USER`.
- Fetch both old name field and new user field.
- Prefer user field for attribution when populated.
- Fall back to old name field for unmapped/historical rows.

Suggested behavior:
- Replace name comparisons with user-id comparisons where possible.
- Keep `setter_field_value` temporarily for fallback and display.
- In `fetch_closes_per_day()` and `fetch_scraper_meetings_bulk()`, derive `setter_uid` from the new user field first.

### 6. `mtd-funnel-dashboard/fetch_and_build.py`

Role:
- For `Reactivation Scrapers`, uses setter name as the sub-breakdown instead of UTM.

Current references:
- `CF_SETTER_NAME`
- Fetches `custom.{CF_SETTER_NAME}`
- Uses `setter_raw` as `utm`/sub-breakdown label.

Required change:
- Add `CF_SETTER_USER`.
- Fetch both fields.
- Prefer new user field for identity, but convert user id to a display name before rendering the sub-breakdown.
- Fall back to old name field if user field is blank.

Suggested behavior:
- Keep report labels human-readable.
- Use Close user map or a local id-to-name map.

### 7. `call-capacity-dashboard/update_dashboard.py`

Role:
- Main capacity dashboard.
- Reactivation Scrapers drilldown by setter.
- EOD email scraper bookings/show rate/set counts.
- EOD closed-won from Lane 2 fallback attribution.

Current references:
- Early dashboard constant: `FIELD_REACTIVATION_SETTER`
- EOD constant: `CF_REACT_SETTER`
- `setter_data` drilldown uses old field text.
- EOD fetches old field for scraper bookings and Lane 2 closed-won attribution.
- `SCRAPER_SETTERS` stores old text values as identity keys.

Required change:
- Add new user-field constants in both sections or centralize them.
- Fetch new user field wherever old field is fetched.
- Main dashboard drilldown should prefer user field identity and display friendly names.
- EOD scraper attribution should use new user field first, then old field fallback.
- `SCRAPER_SETTERS` should eventually store user ids as the primary key.

Suggested behavior:
- Initial pass: fetch new field and use helper `setter_key_for_lead(lead)` that returns a display/name key compatible with existing `SCRAPER_SETTERS`.
- Later pass: convert `SCRAPER_SETTERS` tuples from `(old_name, display, goal)` to `(user_id, display, goal, old_name_fallback)`.

### 8. `call-capacity-dashboard/diagnose_vendhub.py`

Role:
- Diagnostic script that prints raw relevant fields from `update_dashboard.py`.

Current references:
- Prints `ud.CF_REACT_SETTER`.

Required change:
- Print both old and new fields.

### 9. `call-capacity-dashboard/diagnose_lane2_closed_won.py`

Role:
- Diagnostic for Lane 2 closed-won attribution.
- Uses old field as fallback when title evidence is missing.

Current references:
- Fetches `ud.FIELD_REACTIVATION_SETTER`
- Uses `setter_field = lead.get(ud.FIELD_REACTIVATION_SETTER)`

Required change:
- Fetch and print new user field.
- Prefer new user field for fallback confidence.
- Keep old field for readable output and historical fallback.

### 10. `Dom_context/SCRAPER_SETTER_SETUP.md`

Role:
- Human setup instructions for new scraper setter links.

Current references:
- Tells user to add names to `Reactivation - Setter Name`.

Required change:
- Update setup process to mention both fields.
- For new setters, ensure the setter exists as a Close user and the script mapping can resolve their full name to a user id.
- Old dropdown/name value may still need to exist while dual-writing.

### 11. `close-first-sales-meeting/export_smart_views.py`

Role:
- Read-only saved-search audit.

Current references:
- Tracks the old field in `FIELDS`.

Required change:
- Add the new field to `FIELDS`.
- Re-export saved searches after smart-list changes to verify no important views still depend only on the old text field.

### 12. Close Smart Lists

Live saved-search export found 8 saved searches referencing the old field. All use an `exists` condition on `Reactivation - Setter Name`.

Views:
- `save_0LGHvlnEGoZgW8YpvLOhArQDWeCMDQWuro5FJw6WMCO` - Webinar - Aug 25 Scraper Bookings
- `save_n9N985anemUKUnmkUDn6tPnu2ILQnEPco47dt9PMRtf` - Webinar - June 30 Scraper Bookings
- `save_2sOK0qbQg4OCNgNahsN1CcFnI6XgBTW5010jE9XGfWe` - Webinar - Aug 18th Scraper Bookings
- `save_7Ccq3ogjUwrx336Hcebkxw7sphTh21pigy3vFm6wAeI` - Webinar - July 29 Scraper Bookings
- `save_gEwnNWJBnQHNrKB8ZNLPunsbXXsLZGTKQ8bKBslJRx8` - Webinar - July 29 Scraper Bookings
- `save_PogLAjuVBJut5gmzd9YKUSXCmIhnNsj3lZaLJBaUSmT` - Webinar - July 23 Scraper Bookings
- `save_TZLbxbtsF1EUSNbnLTqqi7SYKhsHC0vfLgJ9HDbNYYo` - Webinar - July 14 Scraper Bookings
- `save_YXAlFgNgBdx17P7RU3S6JfBnQ5hx6dVVQXtikX4DOn2` - Webinar - Aug 11th Scraper Bookings

Required change:
- Replace or supplement old-field `exists` filters with new user-field filters.
- For the "me" use case, use `Reactivation Setter User is me`.
- If a list should include all scraper bookings, use `Reactivation Setter User exists`, with old field fallback only if historical unmapped rows matter.

## Validation Checklist

Before code changes:
- Run `python3 dry_run_reactivation_setter_user_sync.py` and confirm `Would set blank user = 0`.
- Confirm the only unmapped old names are expected.

After dual-write changes:
- Run a dry-run update path if available.
- Create or identify a test scraper booking.
- Confirm the lead receives both:
  - `Reactivation - Setter Name`
  - `Reactivation Setter User`
- Run `python3 dry_run_reactivation_setter_user_sync.py` again and confirm no new `would_set` rows.

After smart-list changes:
- Export saved searches with `export_smart_views.py`.
- Confirm critical views reference `cf_7W3UCpJWWaIQsniF1upSxGO7rMX1yDT5qppHXBGJIhO`.
- Confirm `me` checks work in Close.

After dashboard read changes:
- Compare pre/post dashboard totals for Reactivation Scrapers.
- Compare EOD scraper booked/set/shown counts.
- Confirm old-field fallback still catches the two unresolved `Mariam Olufumi` rows if they should remain visible.
