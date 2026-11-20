#!/usr/bin/env python3
"""Run both Close meeting field jobs against one shared read."""
from argparse import ArgumentParser
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import requests

from compare_meeting_updates import (
    CloseReads, MissingLead, evaluate_writers, latest_full_cohort,
    lead_snapshot, load_writer_inputs, proposal_diff, secret,
)
from close_status_policy import ORG
from close_status_reader import CloseClient
from meeting_change_reader import OVERLAP, Window, utc

# A daily complete read repairs non-meeting edits and catches omissions in discovery.
FULL_INTERVAL = timedelta(days=1)
EVENT_RETENTION = timedelta(days=30)
CHECKPOINT = "meeting_update_checkpoint.json"


@dataclass(frozen=True)
class Progress:
    cutoff: datetime
    last_full: datetime
    revisions: frozenset[str] = frozenset()

    @classmethod
    def load(cls, path):
        if not path.is_file():
            return None
        value = json.loads(path.read_text())
        revisions = value.get("accepted_revisions", [])
        if not isinstance(revisions, list) or any(not isinstance(item, str) or len(item) != 64 for item in revisions):
            raise ValueError("Invalid meeting update revision checkpoint")
        result = cls(utc(value["cutoff"]), utc(value["last_full"]), frozenset(revisions))
        if result.last_full > result.cutoff:
            raise ValueError("Invalid meeting updater checkpoint")
        return result

    def json(self):
        return {"cutoff": self.cutoff.isoformat(), "last_full": self.last_full.isoformat(),
                "accepted_revisions": sorted(self.revisions)}


def current_first_dates(snapshots, first_job, affected):
    latest = {}
    for identity in sorted(affected):
        try:
            row = snapshots.read(f"/lead/{identity}/")
        except MissingLead:
            continue
        if row.get(first_job.FIELD_DATE_KEY):
            latest[identity] = {"first": row[first_job.FIELD_DATE_KEY],
                                "latest": row.get(first_job.FIELD_LATEST_DATE_KEY)}
    return latest


def merge_cache(previous, observed, scope):
    if scope is None:
        return observed
    combined = dict(previous)
    for identity in scope - observed.keys():
        combined.pop(identity, None)
    combined.update(observed)
    return combined


def execute(close, end, progress, first_job, follow_job, *, verify, apply, force_full=False):
    if progress and end <= progress.cutoff:
        raise RuntimeError("Meeting update cutoff must advance")
    if apply and not progress and not verify:
        raise RuntimeError("First live run requires an exact full comparison")
    full_due = force_full or not progress or end - progress.last_full >= FULL_INTERVAL
    start = (progress.cutoff if progress else end) - OVERLAP
    if start < end - EVENT_RETENTION + OVERLAP:
        if verify:
            raise RuntimeError("Checkpoint predates the Close Event Log retention window")
        full_due = True

    first, follow = load_writer_inputs(close, first_job, follow_job)
    snapshots = lead_snapshot(close, first_job, follow_job)
    affected, targeted, revisions = set(), [], frozenset()
    if verify or not full_due:
        try:
            changes = close.event_changes(Window(start, end), progress.revisions if progress else frozenset())
            affected, revisions = changes.leads, changes.revisions
            targeted = [meeting for identity in sorted(affected) for meeting in close.meetings(identity)]
        except (requests.RequestException, RuntimeError, ValueError):
            if verify:
                raise
            full_due = True
            affected, targeted, revisions = set(), [], frozenset()

    full, full_latest = [], {}
    if verify or full_due:
        full = close.meetings()
        full_latest = latest_full_cohort(close, first_job)
    targeted_latest = current_first_dates(snapshots, first_job, affected) if affected else {}

    summary = {"checkpoint_cutoff": end.isoformat(), "affected_leads": len(affected), "applied": False}
    if verify:
        baseline, _, _ = evaluate_writers(first, follow, snapshots, full, full_latest)
        incremental, _, _ = evaluate_writers(first, follow, snapshots, targeted, targeted_latest, affected)
        differences = proposal_diff(baseline, incremental, affected)
        in_scope = {row["id"]: row for row in full if row.get("lead_id") in affected}
        targeted_rows = {row["id"]: row for row in targeted}
        changed_rows = sum(in_scope.get(key) != targeted_rows.get(key) for key in in_scope.keys() | targeted_rows.keys())
        summary.update({
            "comparison_matches": not any(differences.values()) and changed_rows == 0,
            "changed_history_rows": changed_rows,
            "baseline_proposals": {writer: len(entries) for writer, entries in baseline.items()},
            "incremental_proposals": {writer: len(entries) for writer, entries in incremental.items()},
            "proposal_differences": {writer: len(entries) for writer, entries in differences.items()},
            "baseline_meetings": len(full), "incremental_meetings": len(targeted),
        })
        summary["close_requests"] = sum(close.calls.values())
        if not apply or not summary["comparison_matches"]:
            return summary, None
        # The first matched live run establishes the full reconciliation checkpoint.
        full_due = True

    rows, scope = (full, None) if full_due else (targeted, affected)
    execution_snapshot = lead_snapshot(close, first_job, follow_job) if verify else snapshots
    latest = full_latest if full_due else targeted_latest
    proposals, first_cache, follow_cache = evaluate_writers(
        first, follow, execution_snapshot, rows, latest, scope, apply=apply
    )
    summary.update({"mode": "full" if full_due else "incremental", "applied": apply,
                    "proposals": {name: len(items) for name, items in proposals.items()},
                    "close_requests": sum(close.calls.values())})
    if not apply:
        return summary, None
    return summary, (
        Progress(end, end if full_due else progress.last_full, revisions),
        merge_cache(first.cache, first_cache, scope),
        merge_cache(follow.cache, follow_cache, scope),
    )


def publish(state, first_job, follow_job):
    """Publish both caches and their checkpoint as one Git commit."""
    progress, first_cache, follow_cache = state
    first_path, follow_path = first_job.STATE_CACHE_FILE, follow_job.STATE_CACHE_FILE
    if first_cache != first_job.load_state_cache():
        first_job.save_state_cache(first_cache, push=False)
    if follow_cache != follow_job.load_state_cache():
        follow_job.save_state_cache(follow_cache)
    Path(CHECKPOINT).write_text(json.dumps(progress.json(), indent=2) + "\n")
    def git(*args):
        subprocess.run(["git", *args], check=True)

    git("add", "--", CHECKPOINT, first_path, follow_path)
    git("-c", "user.name=github-actions[bot]", "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", "Advance shared meeting updater checkpoint [skip ci]", "--",
        CHECKPOINT, first_path, follow_path)
    if subprocess.run(["git", "push"], check=False).returncode:
        # Other scheduled jobs can publish to main while Close is being read.
        git("pull", "--rebase")
        git("push")


def assert_clean_apply_checkout(paths):
    subprocess.run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
                   check=True, capture_output=True, text=True)
    result = subprocess.run(["git", "status", "--porcelain", "--", *paths],
                            check=True, capture_output=True, text=True)
    if result.stdout:
        raise RuntimeError("Meeting updater cache or checkpoint has uncommitted changes")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Optional file containing CLOSE_API_KEY")
    parser.add_argument("--apply", action="store_true", help="Apply through the existing guarded Close writers")
    parser.add_argument("--verify", action="store_true", help="Require an exact full-versus-incremental comparison")
    parser.add_argument("--full", action="store_true", help="Force full reconciliation")
    parser.add_argument("--cutoff", help="Timezone-qualified historical discovery cutoff, for read-only checks")
    args = parser.parse_args()
    if args.cutoff and args.apply:
        parser.error("Historical cutoffs are available for read-only checks")
    env_file = args.env_file.expanduser().resolve() if args.env_file else None
    os.chdir(Path(__file__).resolve().parent)
    try:
        key = secret(env_file)
        os.environ["CLOSE_API_KEY"] = key
        import update_field
        import update_followups
        now = datetime.now(timezone.utc)
        cutoff = datetime.fromisoformat(args.cutoff) if args.cutoff else now
        if cutoff.tzinfo is None or cutoff > now:
            parser.error("The cutoff must include a timezone and be at or before the current time")
        if args.apply:
            assert_clean_apply_checkout((CHECKPOINT, update_field.STATE_CACHE_FILE, update_followups.STATE_CACHE_FILE))
        progress = Progress.load(Path(CHECKPOINT)) if not args.cutoff else None
        close = CloseReads(CloseClient(key, write_enabled=False), ORG)
        # The existing routines print lead names and field values. Keep them out of shared logs.
        with redirect_stdout(io.StringIO()):
            summary, state = execute(close, cutoff.astimezone(timezone.utc), progress, update_field, update_followups,
                                     verify=args.verify or not args.apply, apply=args.apply, force_full=args.full)
        if state:
            with redirect_stdout(io.StringIO()):
                publish(state, update_field, update_followups)
        print(json.dumps(summary, sort_keys=True))
        return 0 if summary.get("comparison_matches", True) else 2
    except (ValueError, RuntimeError, requests.RequestException, OSError, subprocess.CalledProcessError) as error:
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f"Meeting update command stopped: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
