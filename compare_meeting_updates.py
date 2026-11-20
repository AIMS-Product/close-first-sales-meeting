#!/usr/bin/env python3
"""Compare the existing Close full-scan jobs with shared SteelTrap meeting discovery."""
from argparse import ArgumentParser
from collections import Counter
from contextlib import closing, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlencode

import requests

from close_status_policy import COMPANY, ORG
from close_status_reader import CloseClient
from meeting_change_reader import Checkpoint, DatabricksSQL, SteelTrapMeetingReader, Window, lead_id, utc

MEETING_FIELDS = "id,organization_id,lead_id,title,starts_at,status,user_id,date_updated"
MEETING_ACTIONS = ("created", "updated", "deleted", "scheduled", "started", "completed", "canceled")
MAX_SEARCH_ROWS = 10_000


@dataclass(frozen=True)
class CloseChanges:
    leads: set
    meetings: set
    events: dict
    revisions: frozenset[str]


class CloseReads:
    def __init__(self, client, organization):
        self.client = client
        self.organization = organization
        self.calls = Counter()

    def get(self, path, params=None):
        self.calls[path if not path.startswith("/lead/") else "/lead/{id}/"] += 1
        suffix = "?" + urlencode(params) if params else ""
        return self.client.request("GET", path + suffix)

    def search(self, path, body):
        if path != "/data/search/":
            raise ValueError("Unexpected Close search route")
        self.calls[path] += 1
        return self.client.request("POST", path, body)

    def meetings(self, scoped_lead=None):
        params = {"_fields": MEETING_FIELDS}
        if scoped_lead:
            params["lead_id"] = lead_id(scoped_lead)
        rows, seen = [], set()
        for offset in range(0, 150_000, 100):
            page = self.get("/activity/meeting/", dict(params, _limit=100, _skip=offset))
            batch = page.get("data")
            if not isinstance(batch, list) or type(page.get("has_more")) is not bool:
                raise RuntimeError("Incomplete Close meeting response")
            if page["has_more"] and len(batch) != 100:
                raise RuntimeError("Incomplete Close meeting page")
            for meeting in batch:
                identity = meeting.get("id")
                if not identity or identity in seen or meeting.get("organization_id") != self.organization:
                    raise RuntimeError("Close meeting identity, tenant, or pagination changed")
                if scoped_lead and meeting.get("lead_id") != scoped_lead:
                    raise RuntimeError("Close per-lead history returned a different lead")
                seen.add(identity)
            rows.extend(batch)
            if not page["has_more"]:
                return rows
        raise RuntimeError("Close meeting pagination exceeded capacity")

    def event_leads(self, window):
        changes = self.event_changes(window)
        return changes.leads, changes.meetings, changes.events

    def event_changes(self, window, accepted=frozenset()):
        events, meetings, leads, revisions = {}, set(), set(), set()
        for action in MEETING_ACTIONS:
            cursor, cursors = None, set()
            for _ in range(100):
                params = {"object_type": "activity.meeting", "action": action, "_limit": 50}
                if cursor:
                    params["_cursor"] = cursor
                else:
                    params.update(date_updated__gte=window.start.isoformat(),
                                  date_updated__lt=window.end.isoformat())
                page = self.get("/event/", params)
                cursor = page.get("cursor_next")
                batch = page.get("data")
                if not isinstance(batch, list) or (cursor and not batch):
                    raise RuntimeError("Close event pagination returned an incomplete page")
                reached_start = False
                for event in batch:
                    if (event["organization_id"] != self.organization or
                            event["object_type"] != "activity.meeting" or event["action"] != action):
                        raise RuntimeError("Close event tenant or filter mismatch")
                    changed_at = utc(event["date_updated"])
                    if changed_at < window.start:
                        reached_start = True
                    if not window.start <= changed_at < window.end:
                        continue
                    if not event.get("id") or not event.get("object_id"):
                        raise RuntimeError("Close meeting event identity missing")
                    current = (event.get("data") or {}).get("lead_id") or event.get("lead_id")
                    prior = (event.get("previous_data") or {}).get("lead_id")
                    if "lead_id" in (event.get("changed_fields") or []) and not prior:
                        raise RuntimeError("Close moved meeting has no previous lead")
                    subjects = {lead_id(current or prior)}
                    if prior:
                        subjects.add(lead_id(prior))
                    revision = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    revisions.add(revision)
                    if revision not in accepted:
                        leads.update(subjects)
                        events.setdefault(event["id"], set()).update(subjects)
                        meetings.add(event["object_id"])
                if not cursor or reached_start:
                    break
                if cursor in cursors:
                    raise RuntimeError("Close event cursor repeated")
                cursors.add(cursor)
            else:
                raise RuntimeError("Close event pagination exceeded capacity")
        return CloseChanges(leads, meetings, events, frozenset(revisions))


class MissingLead(requests.HTTPError):
    """A cached Close identity no longer resolves."""


class LeadSnapshot:
    """Share each live field read across writer policies and comparison paths."""
    def __init__(self, close, fields):
        self.close = close
        self.fields = ",".join(("organization_id", *fields))
        self.records = {}
        self.absent = set()

    def read(self, path, params=None):
        match = re.fullmatch(r"/lead/(lead_[A-Za-z0-9]+)/", path)
        if not match:
            raise ValueError("Unexpected writer read route")
        identity = match[1]
        if identity not in self.records:
            try:
                row = self.close.get(path, {"_fields": self.fields})
                if row.get("id") != identity or row.get("organization_id") != self.close.organization:
                    raise RuntimeError("Close lead identity or tenant mismatch")
                self.records[identity] = row
            except requests.HTTPError as error:
                if error.response is None or error.response.status_code != 404:
                    raise
                self.records[identity] = None
        row = self.records[identity]
        if row is None:
            self.absent.add(identity)
            raise MissingLead("Close lead absent")
        return row


@dataclass(frozen=True)
class WriterInputs:
    job: object
    cache: dict
    users: dict
    choices: frozenset[str] = frozenset()


def evaluate_writers(first, follow, snapshots, rows, latest, scope=None, *, apply=False):
    def select(items):
        return items if scope is None else {k: v for k, v in items.items() if k in scope}

    first_job, follow_job = first.job, follow.job
    desired = first_job.calculate_desired_state(rows, first.users)
    first_job.suppress_invalid_reactivation_choices(desired, first.choices)
    follow_desired = follow_job.calculate_desired_state(rows, follow.users)
    proposals = {"first_sales_meeting": {}, "follow_up_dates": {}}

    def first_put(path, payload):
        match = re.fullmatch(r"/lead/(lead_[A-Za-z0-9]+)/", path)
        if (not match or match[1] in proposals["first_sales_meeting"] or not payload or
                not payload.keys() <= set(first_job.FIELDS_PARAM.split(",")) - {"id", "display_name"}):
            raise ValueError("Unexpected first-sales-meeting write proposal")
        if apply:
            first_job.api_put(path, payload)
        proposals["first_sales_meeting"][match[1]] = payload.copy()

    def follow_read(identity):
        try:
            row = snapshots.read(f"/lead/{identity}/")
        except MissingLead:
            raise
        except requests.HTTPError as error:
            raise RuntimeError("A required Close lead read failed") from error
        return {f: row.get(f"custom.{f}") for f in follow_job.FOLLOWUP_FIELDS}

    def follow_put(identity, updates):
        if identity in proposals["follow_up_dates"] or not updates or not updates.keys() <= set(follow_job.FOLLOWUP_FIELDS):
            raise ValueError("Unexpected follow-up write proposal")
        if apply:
            try:
                follow_job.patch_lead(identity, updates)
            except requests.HTTPError as error:
                raise RuntimeError("The guarded follow-up write failed") from error
        proposals["follow_up_dates"][identity] = {f"custom.{k}": v for k, v in updates.items()}

    def require_success(error):
        if not isinstance(error, MissingLead):
            raise RuntimeError("First Sales Meeting Field comparison was incomplete") from error

    first_cache = first_job.routine_update(desired, select(first.cache), first.users, select(latest),
                                           get=snapshots.read, put=first_put, on_error=require_success)
    follow_cache, _, _ = follow_job.routine_update(follow_desired, select(follow.cache), read_fields=follow_read,
                                                    write_fields=follow_put)
    return proposals, first_cache, follow_cache


def capture_proposals(first, follow, snapshots, rows, latest, scope=None):
    proposals, _, _ = evaluate_writers(first, follow, snapshots, rows, latest, scope)
    return proposals


def proposal_diff(baseline, targeted, affected):
    changes = {}
    for writer, baseline_leads in baseline.items():
        other_leads = targeted[writer]
        changes[writer] = {
            identity: {
                "baseline": baseline_leads.get(identity),
                "incremental": other_leads.get(identity),
                "in_incremental_scope": identity in affected,
            }
            for identity in sorted(baseline_leads.keys() | other_leads.keys())
            if baseline_leads.get(identity) != other_leads.get(identity)
        }
    return changes


def load_writer_inputs(close, first_job, follow_job):
    first_cache, follow_cache = first_job.load_state_cache(), follow_job.load_state_cache()
    if not first_cache or not follow_cache:
        raise RuntimeError("Shadow comparison requires both existing routine writer caches")
    choices = close.get(f"/custom_field/lead/{first_job.FIELD_REACTIVATION_ID}/").get("choices")
    if not isinstance(choices, list) or not choices or not all(isinstance(x, str) for x in choices):
        raise RuntimeError("Close Reactivation Setter dropdown choices unavailable")
    first_users = first_job.fetch_close_users(get=close.get)
    follow_users = close.get("/user/")["data"]
    if not first_users or not follow_users:
        raise RuntimeError("Close user directory is incomplete")
    first = WriterInputs(first_job, first_cache, first_job.build_user_name_map(first_users), frozenset(choices))
    follow = WriterInputs(follow_job, follow_cache, follow_job.build_user_map(follow_users))
    return first, follow


def latest_full_cohort(close, first_job):
    latest = first_job.fetch_leads_with_first_date(search=close.search)
    if len(latest) >= MAX_SEARCH_ROWS or not latest:
        raise RuntimeError("Close first-date cohort may be incomplete")
    return latest


def lead_snapshot(close, first_job, follow_job):
    return LeadSnapshot(close, (*first_job.FIELDS_PARAM.split(","),
                                *(f"custom.{f}" for f in follow_job.FOLLOWUP_FIELDS)))


def compare(close, steel, window, first_job, follow_job):
    discovered_at = datetime.now(timezone.utc).isoformat()
    steel_changes = steel.changes(window)
    close_leads, close_meetings, close_events = close.event_leads(window)
    affected = steel_changes.leads
    full = close.meetings()
    close.calls["full_meeting_pages"] = close.calls["/activity/meeting/"]
    targeted = {identity: close.meetings(identity) for identity in sorted(affected | close_leads)}
    close.calls["shared_targeted_meeting_pages"] = close.calls["/activity/meeting/"] - close.calls["full_meeting_pages"]
    first, follow = load_writer_inputs(close, first_job, follow_job)
    latest = latest_full_cohort(close, first_job)
    snapshots = lead_snapshot(close, first_job, follow_job)
    baseline = capture_proposals(first, follow, snapshots, full, latest)
    proposals = {
        name: capture_proposals(first, follow, snapshots,
                                [row for identity in sorted(scope) for row in targeted[identity]], latest, scope)
        for name, scope in (("steeltrap", affected), ("close_event_log", close_leads))
    }
    full_scope = {row["id"]: row for row in full if row.get("lead_id") in targeted}
    targeted_scope = {row["id"]: row for meetings in targeted.values() for row in meetings}
    history_diffs = sum(full_scope.get(key) != targeted_scope.get(key)
                        for key in full_scope.keys() | targeted_scope.keys())
    return {
        "cutoff": window.end.isoformat(), "window_start": window.start.isoformat(),
        "previous_cutoff": window.previous_cutoff.isoformat() if window.previous_cutoff else None,
        "previous_steeltrap_version": window.previous_version,
        "steeltrap_version": steel_changes.version, "steeltrap_commit_at": steel_changes.published_at,
        "discovery_started_at": discovered_at, "completed_at": datetime.now(timezone.utc).isoformat(),
        "counts": {"steeltrap_leads": len(affected), "close_leads": len(close_leads),
                   "steeltrap_meetings": len(steel_changes.meetings), "close_meetings": len(close_meetings),
                   "steeltrap_events": len(steel_changes.events), "close_events": len(close_events),
                   "steeltrap_late_events_after_prior_cutoff": steel_changes.late_events,
                   "steeltrap_late_events_outside_overlap": steel_changes.late_events_outside_overlap,
                   "close_events_also_in_steeltrap": len(close_events.keys() & steel_changes.events.keys()),
                   "steeltrap_previous_leads": steel_changes.previous_leads,
                   "full_meeting_rows": len(full), "shared_targeted_meeting_rows": len(targeted_scope),
                   "changed_history_rows_during_capture": history_diffs,
                   "absent_leads": len(snapshots.absent)},
        "calls": dict(close.calls),
        "discovery": {"close_only_leads": sorted(close_leads - affected),
                      "steeltrap_only_leads": sorted(affected - close_leads),
                      "close_only_meetings": sorted(close_meetings - steel_changes.meetings),
                      "steeltrap_only_meetings": sorted(steel_changes.meetings - close_meetings),
                      "common_events_with_different_leads": sorted(
                          key for key in close_events.keys() & steel_changes.events.keys()
                          if close_events[key] != steel_changes.events[key])},
        "proposals": {"full_scan": baseline, **proposals},
        "differences": {"steeltrap": proposal_diff(baseline, proposals["steeltrap"], affected),
                        "close_event_log": proposal_diff(baseline, proposals["close_event_log"], close_leads)},
    }


def run(db, end, compare_run, *, replay=False):
    """Commit the report and its checkpoint together after both writers finish."""
    db.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, report TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS checkpoint (
        id INTEGER PRIMARY KEY CHECK (id = 1), cutoff TEXT NOT NULL, version INTEGER NOT NULL
    )""")
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT cutoff, version FROM checkpoint WHERE id = 1").fetchone()
        checkpoint = Checkpoint(utc(row[0]), row[1]) if row and not replay else None
        report = compare_run(Window.next(end, checkpoint))
        recorded = db.execute("INSERT INTO runs (report) VALUES (?)", (json.dumps(report, sort_keys=True),))
        if not replay:
            db.execute("""INSERT INTO checkpoint (id, cutoff, version) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET cutoff=excluded.cutoff, version=excluded.version
            """, (report["cutoff"], report["steeltrap_version"]))
        return recorded.lastrowid, report


def secret(env_file):
    if os.environ.get("CLOSE_API_KEY"):
        return os.environ["CLOSE_API_KEY"]
    if env_file:
        for line in env_file.read_text().splitlines():
            if line.startswith("CLOSE_API_KEY="):
                return line.partition("=")[2].strip().strip('"\'')
    raise RuntimeError("CLOSE_API_KEY is required")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, required=True, help="SQLite report and checkpoint in a private directory")
    parser.add_argument("--warehouse", required=True, help="Databricks SQL warehouse ID")
    parser.add_argument("--profile", help="Databricks CLI profile")
    parser.add_argument("--cache-dir", type=Path, default=Path(__file__).resolve().parent,
                        help="Directory with current production copies of both writer caches")
    parser.add_argument("--env-file", type=Path, help="Optional file containing CLOSE_API_KEY")
    parser.add_argument("--replay-until", help="Historical source cutoff in ISO 8601; leave the live cursor alone")
    args = parser.parse_args()

    os.umask(0o077)
    db_path = args.state_db.expanduser().resolve()
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if db_path.parent.stat().st_mode & 0o077 or (db_path.exists() and db_path.stat().st_mode & 0o077):
        parser.error("Use a state database inside a private directory (mode 0700)")
    try:
        key = secret(args.env_file)
        os.environ.setdefault("CLOSE_API_KEY", key)
        import update_field
        import update_followups
        now = datetime.now(timezone.utc)
        end = now
        if args.replay_until:
            try:
                supplied = datetime.fromisoformat(args.replay_until)
            except ValueError:
                parser.error("--replay-until requires a timezone-qualified ISO 8601 timestamp")
            if supplied.tzinfo is None or supplied > now:
                parser.error("--replay-until requires a timezone and a cutoff at or before the current time")
            end = supplied.astimezone(timezone.utc)
        close = CloseReads(CloseClient(key, write_enabled=False), ORG)
        steel = SteelTrapMeetingReader(DatabricksSQL(args.warehouse, args.profile), COMPANY, ORG)
        cache_root = args.cache_dir.resolve()
        update_field.STATE_CACHE_FILE = str(cache_root / Path(update_field.STATE_CACHE_FILE).name)
        update_followups.STATE_CACHE_FILE = str(cache_root / Path(update_followups.STATE_CACHE_FILE).name)
        with closing(sqlite3.connect(db_path, timeout=0)) as db, redirect_stdout(io.StringIO()):
            run_id, report = run(db, end, lambda window: compare(close, steel, window, update_field, update_followups),
                                 replay=bool(args.replay_until))
    except (ValueError, RuntimeError, requests.RequestException, sqlite3.Error, OSError) as error:
        detail = str(error) if isinstance(error, (RuntimeError, sqlite3.Error)) else type(error).__name__
        print(f"Meeting comparison incomplete: {detail}", file=sys.stderr)
        return 1
    summary = {"report_id": run_id, "cutoff": report["cutoff"], "steeltrap_version": report["steeltrap_version"],
               "counts": report["counts"], "calls": report["calls"],
               "discovery_difference_counts": {k: len(v) for k, v in report["discovery"].items() if isinstance(v, list)},
               "proposal_counts": {source: {writer: len(leads) for writer, leads in values.items()}
                                   for source, values in report["proposals"].items()},
               "proposal_difference_counts": {source: {writer: len(leads) for writer, leads in values.items()}
                                              for source, values in report["differences"].items()}}
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
