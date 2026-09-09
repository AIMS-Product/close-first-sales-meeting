# VP THE END Lead-Release Rule

Status: Draft
Date: 2026-09-09

## Context & problem

Completed workflow leads can remain owned by Setters or Scrapers and accumulate in their queues. A Close task titled exactly `VP THE END` is the user-defined terminal signal. Both assignment scripts should release the owner on matching leads so their capacity can be refilled.

## Goals / non-goals

| Type | Item |
| --- | --- |
| Goal | Detect an exact, open `VP THE END` task. |
| Goal | In dry-run, report release candidates by current owner; in `--apply`, clear only Lead Owner. |
| Goal | Run before capacity counts in both assigners. |
| Non-goal | Change Recapture State, Owner Team, sequences, opportunities, or tasks. |
| Non-goal | Reassign a released lead in the same invocation. |

## Current state

- `assign_setter_leads.py` and `assign_lane2_leads.py` are independently executable, dry-run-by-default Lead Owner assigners.
- The Scraper script already has a guarded reclaim path that reports then clears owners only under `--apply`.
- Close task records provide `text`, `lead_id`, and `is_complete`; task-list filters do not support exact task-text filtering, so matching must be done client-side.
- A preliminary lead-embedded-task read returned no matches, but it cannot prove historical completed tasks are absent.

## Considered approaches

| Approach | Trade-offs |
| --- | --- |
| Query embedded lead tasks | Fast, but misses historical/completed terminal tasks. Not safe. |
| Page Close task records, match exact text, intersect with each script's eligible roster | Accurate for current and completed tasks; adds a paginated read. Recommended. |
| Add a Close custom field set by the workflow | Fastest steady-state query, but requires workflow changes and a backfill. Future option. |

## Proposed design

Add a shared helper module used by both assigners:

1. Page only open `/task/` records with `lead_id,text,is_complete` and collect unique lead IDs whose trimmed task text equals `VP THE END` exactly.
2. Query each assigner's owned eligible lead set, intersect it with those IDs, and group the result by owner.
3. Print the candidate count and a small sample in every run.
4. On dry-run, stop after reporting. On `--apply`, clear only `custom.<Lead Owner>` for the candidates using the existing bounded worker/error-reporting convention.
5. Only after successful release writes, re-query/rebuild queue counts, then assign unowned leads. A candidate is never reassigned during the same run.

```
Close tasks -> exact VP THE END IDs -> owned eligible leads -> release preview/write -> fresh queue census -> normal assignment
```

## Failure modes and safeguards

| Failure | Behavior |
| --- | --- |
| Task pagination/API error | Abort before any release or assignment writes. |
| Candidate changed owner after read | Re-fetch or conditional owner check immediately before clearing; skip changed records. |
| Partial owner clears | Report successes/errors; next dry run is idempotent and shows remaining candidates. |
| Unexpectedly large candidate count | Require a `--max-release` safety ceiling and abort above it. |

## Implementation plan

1. Add the read-only helper and a `--max-release` default ceiling; write unit tests for exact matching, de-duplication, and no-match behavior.
2. Integrate a dry-run report into both assigners before their count phase.
3. Enable `--apply` owner clearing behind the ceiling; verify with a limited production dry run and then a small apply.
4. Monitor the first scheduled runs; rollback is immediate by omitting `--apply` or disabling the helper invocation. Cleared owners are recoverable by reassignment on the next normal run, but no automatic rollback will be attempted.
