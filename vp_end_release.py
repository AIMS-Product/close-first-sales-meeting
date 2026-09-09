"""Shared, dry-run-safe release rule for terminal VP workflows."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

VP_END_TASK_TEXT = "VP THE END"


def _open_vp_end_lead_ids(paginate):
    """Return lead IDs with an open task whose text is exactly VP THE END."""
    lead_ids = set()
    for task in paginate("/task/", {
        "is_complete": "false",
        "_fields": "lead_id,text",
        "_limit": 100,
    }):
        if (task.get("text") or "").strip() == VP_END_TASK_TEXT and task.get("lead_id"):
            lead_ids.add(task["lead_id"])
    return lead_ids


def release_vp_end_leads(rows, *, owner_of, owner_names, paginate, write_owner,
                         apply, max_release, label):
    """Report or clear owners for rows with an open terminal task.

    Returns their IDs so callers can prevent same-run reassignment.
    """
    print("Reading open VP THE END tasks...")
    terminal_ids = _open_vp_end_lead_ids(paginate)
    candidates = [row for row in rows if row.get("id") in terminal_ids]
    if len(candidates) > max_release:
        raise RuntimeError(f"refusing to release {len(candidates):,} {label} leads; "
                           f"above --max-release {max_release:,}")
    by_owner = Counter(owner_of(row) for row in candidates)
    print(f"\nVP THE END release candidates: {len(candidates):,}")
    for owner, count in by_owner.most_common():
        print(f"  {owner_names.get(owner, owner):<28} {count:>6,}")
    ids = {row["id"] for row in candidates}
    if not candidates or not apply:
        print("  (dry run — not released)" if candidates else "  (none)")
        return ids
    with ThreadPoolExecutor(max_workers=8) as executor:
        failures = [result for result in executor.map(write_owner, ids) if result]
    if failures:
        raise RuntimeError(f"released {len(ids)-len(failures):,}/{len(ids):,}; "
                           f"{len(failures):,} failed")
    print(f"  Released {len(ids):,} lead owner(s).")
    return ids
