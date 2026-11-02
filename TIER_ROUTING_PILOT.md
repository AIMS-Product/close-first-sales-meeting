# Tier routing pilot switch

Status: implemented locally; production switch OFF. No deployment or live writes.

## Selection and safety limits

| Invocation | Behavior |
| --- | --- |
| Existing invocation without `--tier-routing` | Existing legacy assignment path. No tier reader or pilot writer runs. |
| `--tier-routing` without `--apply` | Read-only pilot preview, scoped to explicit reps and leads. |
| `--tier-routing --apply` with switch absent or `0` | Stop before provider reads or writes; never fall back to legacy assignment. |
| `--tier-routing --apply` with switch `1` | Still requires exact approval, stored permissions and mandatory shared PostgreSQL holds. |

`LANE2_TIER_ROUTING_ENABLED` defaults to `0`. Only the exact value `1` permits
pilot writes. Setting it to `0` stops subsequent tier assignments, including
between leads within a running pilot. It does not reverse completed assignments.
Changing the environment does not alter an already-running process's environment;
change the configuration and restart/stop the worker for an operational kill.
The explicit CLI flag is also required: changing the environment alone does not
redirect existing jobs into tier routing.

The pilot accepts one to five exact Close rep IDs, one to ten exact lead IDs,
and a required assignment cap of 1–10. There is no automatic cohort discovery,
full pool scan, reclaim, terminal-task release or uncapped assignment in this path.
Only an empty Lead Owner can become an explicitly selected rep. No status, batch,
tier, source, certification, owner-since or other fields are written. Existing
owners stay with their leads. Other offer assigners are unchanged by this switch.

## Preview command

Use the existing credentials already provided to the job:

```sh
python assign_lane2_leads.py --tier-routing \
  --tier-rep user_EXACT_ID --tier-lead lead_EXACT_ID \
  --tier-max-assignments 1 --max-queue 1000 \
  --tier-tagger /absolute/path/to/existing/sync_lead_tier_tags.py \
  --tier-output /new/local/preview/directory
```

The default preview consumes stored rep permissions. To inspect local approved
roster candidates before those permissions are saved, add
`--tier-use-staged-permissions`. Such a preview **cannot authorize writes**.
The directory contains `plan.json`, `approval-template.json` and the read/search
receipt. The template has no approver, no expiry and false integration receipts;
it is not an approval. Output directories must be new to preserve prior evidence.

## Before any approved write

The candidate execution path also requires `--apply`, a reviewed
`--tier-approval` file, `LANE2_TIER_ROUTING_ENABLED=1`,
`WORKFLOW_HOLD_BACKEND=postgres` and `WORKFLOW_HOLDS_REQUIRED=1`.
None of those settings have been enabled by this work.

Approval must match the exact plan digest and pilot scope, identify the approver,
be unexpired, reference a preview at most ten minutes old and use stored rep
permissions. It must acknowledge shared-store/all-writer coordination, dispatch
and enrollment protections, dial queues, schema/tenant and ownership tracking.
Missing registry or receipts blocks execution. The existing shared migration,
writer-adoption and ownership-tracking prerequisites remain unproved.

For each proposed owner change, the executor takes the shared fence and rechecks
only that lead and target rep: current certification and approved tier, current
Close membership, offer, age/source, holds, historical suppressions, existing
workflow subscriptions and capacity. It refreshes native owner/status immediately
before the write, persists the intent, writes once and checks owner/status again.
An uncertain result remains durable and prevents retries pending reconciliation.
The pilot never clears any workflow hold.

## Evidence

`tier-preview-20261023-flag-pilot/` records the real entry point running in preview
mode with two selected reps (Osheana and Tara), one selected lead and a one-lead
assignment cap. The write switch was false and no mutation was sent. The shared
hold registry was unavailable, so the lead was excluded with
`durable_hold_verification_unavailable` and no owner patch was proposed.

Local tests cover the unchanged legacy path, no fallback after tier errors,
default-off writes, exact approval, hard scope/caps, loss of certification, fresh
capacity, new holds, owner changes, readback mismatches, uncertain writes and the
kill switch between leads. These are not a deployed PostgreSQL pilot certificate.
