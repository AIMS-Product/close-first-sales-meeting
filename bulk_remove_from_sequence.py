#!/usr/bin/env python3
"""
bulk_remove_from_sequence.py — bulk pause / finish / remove leads from a Close
Workflow (Sequence) using a CSV.

Close's UI only lets you do this one lead at a time. This does it from a list.

IMPORTANT — "Workflow" and "Sequence" are the same object in the Close API.
The API calls them Sequences (`seq_...`); the UI calls them Workflows.

Three actions, in ascending order of destructiveness:

  pause   PUT status=paused    Subscription stays on the lead, steps stop.
                               Reversible via `resume`. Lead still shows in the
                               Workflow report as Paused. <- DEFAULT
  resume  PUT status=active    Un-pauses. The undo for `pause` -- defaults to
                               acting on paused subscriptions, not active ones.
  finish  PUT status=finished  Marked complete. Not meaningfully reversible.
  delete  DELETE               Removes the subscription record entirely. This
                               is what the UI's "Remove from Workflow" does.
                               IRREVERSIBLE -- the enrollment history is gone.

Note: resuming restarts the Workflow from where it paused. If a scheduled step
came due while paused, Close may fire it shortly after resume -- so test on a
Workflow whose next step isn't imminent, or on a handful of leads first.

Matching works the same way as bulk_update_field_from_csv.py:
  lead_id  — column named lead_id / id / close_lead_id
  email    — column named email / contact_email
  name     — column named company / lead_name / name

Usage:
  python bulk_remove_from_sequence.py --list-sequences
  python bulk_remove_from_sequence.py --csv leads.csv --sequence seq_xxx --dry-run
  python bulk_remove_from_sequence.py --csv leads.csv --sequence "Black Friday Promo" \
      --action delete
  python bulk_remove_from_sequence.py --csv leads.csv --all-sequences --dry-run

Repo conventions: --dry-run writes nothing, --selftest is pure logic, every
write is verified by re-reading, and a per-row report CSV is always produced.
"""

import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict

import requests

API_BASE = "https://api.close.com/api/v1"
PAGE_SIZE = 100
MAX_RETRIES = 5

# Statuses a subscription can be in. We only touch "live" ones by default --
# a subscription that already finished or hit its goal isn't sending anything,
# and rewriting it would corrupt the Workflow's reporting.
LIVE_STATUSES = ("active", "paused")
DEFAULT_TARGET_STATUSES = ("active",)

# Which statuses each action targets when --statuses isn't given explicitly.
# resume is the odd one out: it acts on paused subscriptions, not active ones.
DEFAULT_STATUSES_BY_ACTION = {
    "pause": ("active",),
    "resume": ("paused",),
    "finish": ("active", "paused"),
    "delete": ("active", "paused"),
}

# The status each action writes.
TARGET_STATUS_BY_ACTION = {"pause": "paused", "resume": "active", "finish": "finished"}

# Used for log/report labels so "finish" doesn't render as "finishd".
PAST_TENSE = {"pause": "paused", "resume": "resumed",
              "finish": "finished", "delete": "deleted"}

SESSION = requests.Session()

LEAD_ID_COLUMNS = ("lead_id", "leadid", "id", "close_lead_id", "close id", "lead id")
EMAIL_COLUMNS = ("email", "contact_email", "email_address", "primary_email", "emails")
NAME_COLUMNS = ("company", "lead_name", "name", "company_name", "display_name", "lead")


# ---------------------------------------------------------------- HTTP layer

def close_request(method, path, allow_404=False, **kwargs):
    url = f"{API_BASE}{path}"
    for attempt in range(MAX_RETRIES):
        resp = SESSION.request(method, url, timeout=60, **kwargs)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After") or (2 ** attempt))
            print(f"    rate limited, sleeping {wait:.1f}s")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            wait = 2 ** attempt
            print(f"    HTTP {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code == 404 and allow_404:
            return None
        if not resp.ok:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.text else {}
    raise RuntimeError(f"{method} {path} failed after {MAX_RETRIES} attempts")


def search_leads(query, fields, limit=PAGE_SIZE, cursor=None):
    payload = {"query": query, "_fields": fields, "_limit": limit}
    if cursor:
        payload["cursor"] = cursor
    return close_request("POST", "/data/search/", json=payload)


def all_leads_query():
    return {"type": "object_type", "object_type": "lead"}


def email_query(email):
    return {"type": "and", "queries": [
        {"type": "object_type", "object_type": "lead"},
        {"type": "field_condition",
         "field": {"type": "regular_field", "object_type": "contact", "field_name": "email"},
         "condition": {"type": "text", "mode": "full_words", "value": email}}]}


def name_query(name):
    return {"type": "and", "queries": [
        {"type": "object_type", "object_type": "lead"},
        {"type": "field_condition",
         "field": {"type": "regular_field", "object_type": "lead", "field_name": "display_name"},
         "condition": {"type": "text", "mode": "phrase", "value": name}}]}


# ------------------------------------------------------------ sequence lookup

def fetch_sequences():
    out, skip = [], 0
    while True:
        data = close_request("GET", f"/sequence/?_skip={skip}&_limit={PAGE_SIZE}")
        out.extend(data.get("data", []))
        if not data.get("has_more"):
            break
        skip += PAGE_SIZE
    return out


def resolve_sequences(all_seqs, wanted):
    """Map user input (ids or names) -> {seq_id: name}. Raises on no match."""
    by_id = {str(s.get("id")): str(s.get("name", "")) for s in all_seqs}
    by_name = {}
    for s in all_seqs:
        by_name.setdefault(str(s.get("name", "")).strip().lower(), []).append(str(s.get("id")))

    resolved, missing, ambiguous = {}, [], []
    for raw in wanted:
        token = raw.strip()
        if token in by_id:
            resolved[token] = by_id[token]
            continue
        hits = by_name.get(token.lower(), [])
        if len(hits) == 1:
            resolved[hits[0]] = by_id[hits[0]]
        elif len(hits) > 1:
            ambiguous.append((token, hits))
        else:
            missing.append(token)
    return resolved, missing, ambiguous


# ------------------------------------------------------- subscription lookup

def fetch_lead_subscriptions(lead_id):
    """All sequence subscriptions on a lead (a lead can have several, one per
    contact per sequence). The list endpoint REQUIRES a lead/contact/sequence
    filter -- there is no unfiltered list."""
    out, skip = [], 0
    while True:
        data = close_request(
            "GET", f"/sequence_subscription/?lead_id={lead_id}&_skip={skip}&_limit={PAGE_SIZE}")
        out.extend(data.get("data", []))
        if not data.get("has_more"):
            break
        skip += PAGE_SIZE
    return out


def apply_action(sub_id, action, dry_run):
    """Returns (ok, detail). Verified by re-reading the subscription."""
    if dry_run:
        return True, "dry-run (no write)"

    if action == "delete":
        close_request("DELETE", f"/sequence_subscription/{sub_id}/")
        still_there = close_request("GET", f"/sequence_subscription/{sub_id}/", allow_404=True)
        if still_there is None:
            return True, "deleted"
        return False, f"still exists after DELETE (status {still_there.get('status')!r})"

    target = TARGET_STATUS_BY_ACTION[action]
    close_request("PUT", f"/sequence_subscription/{sub_id}/", json={"status": target})
    after = close_request("GET", f"/sequence_subscription/{sub_id}/", allow_404=True)
    if after is None:
        return False, "subscription vanished after PUT"
    if str(after.get("status", "")).lower() == target:
        return True, target
    return False, f"status is {after.get('status')!r}, expected {target!r}"


# ------------------------------------------------------------- CSV + matching

def detect_column(headers, candidates):
    lowered = {h.strip().lower(): h for h in headers}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


def detect_match_mode(headers):
    if detect_column(headers, LEAD_ID_COLUMNS):
        return "lead_id", detect_column(headers, LEAD_ID_COLUMNS)
    if detect_column(headers, EMAIL_COLUMNS):
        return "email", detect_column(headers, EMAIL_COLUMNS)
    if detect_column(headers, NAME_COLUMNS):
        return "name", detect_column(headers, NAME_COLUMNS)
    return None, None


def norm_email(raw):
    return (raw or "").strip().lower()


def norm_name(raw):
    return re.sub(r"\s+", " ", (raw or "").strip().lower())


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = [h for h in (reader.fieldnames or []) if h]
        return headers, list(reader)


def build_lead_index():
    email_index, name_index = defaultdict(list), defaultdict(list)
    fields = {"lead": ["id", "display_name", "contacts"], "contact": ["id", "emails"]}
    cursor, scanned, contacts_available = None, 0, False
    while True:
        data = search_leads(all_leads_query(), fields, cursor=cursor)
        page = data.get("data", [])
        for lead in page:
            lead_id = lead.get("id")
            scanned += 1
            key = norm_name(lead.get("display_name"))
            if key:
                name_index[key].append(lead_id)
            contacts = lead.get("contacts")
            if contacts is None:
                continue
            contacts_available = True
            for contact in contacts:
                for entry in contact.get("emails") or []:
                    ekey = norm_email(entry.get("email") if isinstance(entry, dict) else entry)
                    if ekey:
                        email_index[ekey].append(lead_id)
        cursor = data.get("cursor")
        if not cursor or not page:
            break
        if scanned % 1000 == 0:
            print(f"  indexed {scanned} leads...")
    return email_index, name_index, scanned, contacts_available


def lead_emails(lead):
    out = []
    for contact in lead.get("contacts") or []:
        for entry in contact.get("emails") or []:
            key = norm_email(entry.get("email") if isinstance(entry, dict) else entry)
            if key:
                out.append(key)
    return out


def search_leads_by_email(email):
    data = search_leads(email_query(email), {"lead": ["id"]})
    confirmed = []
    for hit in data.get("data", []):
        lead = close_request("GET", f"/lead/{hit['id']}/")
        if norm_email(email) in lead_emails(lead):
            confirmed.append(hit["id"])
    return confirmed


def search_leads_by_name(name):
    data = search_leads(name_query(name), {"lead": ["id", "display_name"]})
    return [h["id"] for h in data.get("data", [])
            if norm_name(h.get("display_name")) == norm_name(name)]


def should_act(sub, target_seq_ids, target_statuses):
    """target_seq_ids of None means 'every sequence'."""
    if target_seq_ids is not None and str(sub.get("sequence_id")) not in target_seq_ids:
        return False
    return str(sub.get("status", "")).lower() in target_statuses


# ---------------------------------------------------------------------- run

def run(args):
    api_key = os.environ.get("CLOSE_API_KEY")
    if not api_key:
        sys.exit("CLOSE_API_KEY is not set")
    SESSION.auth = (api_key, "")

    print("Fetching Workflows (Sequences)...")
    all_seqs = fetch_sequences()
    print(f"  found {len(all_seqs)}")

    if args.list_sequences:
        for s in sorted(all_seqs, key=lambda x: str(x.get("name", ""))):
            print(f"  {s.get('id')}  {str(s.get('status', '')):<8} {s.get('name')}")
        return 0

    # ---- which sequences
    if args.all_sequences:
        target_seq_ids, seq_label = None, "ALL Workflows"
    else:
        resolved, missing, ambiguous = resolve_sequences(all_seqs, args.sequence)
        for token, hits in ambiguous:
            print(f"!! {token!r} matches {len(hits)} Workflows by name — use the seq_ id: {hits}")
        if missing:
            print(f"!! no Workflow matches: {missing}")
        if ambiguous or missing:
            sys.exit("Resolve the Workflow names above (--list-sequences to see all).")
        target_seq_ids = set(resolved)
        seq_label = ", ".join(f"{name} ({sid})" for sid, name in resolved.items())
    print(f"Target: {seq_label}")

    if args.statuses:
        target_statuses = tuple(s.strip().lower() for s in args.statuses.split(",") if s.strip())
    else:
        target_statuses = DEFAULT_STATUSES_BY_ACTION[args.action]
    print(f"Acting on subscriptions in status: {', '.join(target_statuses)}")
    print(f"Action: {args.action.upper()}"
          + ("  *** IRREVERSIBLE ***" if args.action == "delete" and not args.dry_run else ""))

    headers, rows = read_csv_rows(args.csv)
    if not rows:
        sys.exit(f"{args.csv} has no data rows")
    if args.limit:
        rows = rows[:args.limit]
    print(f"Loaded {len(rows)} rows from {args.csv}")

    mode, column = detect_match_mode(headers)
    if args.match_by:
        mode = args.match_by
    if args.match_column:
        column = args.match_column
    if not mode or not column:
        sys.exit(f"Could not find a match column in {headers}. Use --match-by / --match-column.")
    print(f"Matching on: {mode}  (column {column!r})")

    email_index = name_index = None
    if mode in ("email", "name") and not args.per_row_search:
        print("Building lead index (one paginated scan)...")
        email_index, name_index, scanned, contacts_ok = build_lead_index()
        print(f"  indexed {scanned} leads")
        if mode == "email" and not contacts_ok:
            print("  contacts not returned by search -- falling back to per-row search")
            email_index = None

    report_rows, stats = [], defaultdict(int)
    seq_name_by_id = {str(s.get("id")): str(s.get("name", "")) for s in all_seqs}

    for i, row in enumerate(rows, 1):
        raw_key = (row.get(column) or "").strip()
        base = {"row": i, "match_key": raw_key}

        if not raw_key:
            stats["blank_key"] += 1
            report_rows.append({**base, "lead_id": "", "subscription_id": "", "sequence": "",
                                "prior_status": "", "status": "skipped", "detail": "empty match key"})
            continue

        try:
            if mode == "lead_id":
                lead_ids = [raw_key]
            elif mode == "email":
                key = norm_email(raw_key)
                lead_ids = (email_index.get(key, []) if email_index is not None
                            else search_leads_by_email(key))
            else:
                key = norm_name(raw_key)
                lead_ids = (name_index.get(key, []) if name_index is not None
                            else search_leads_by_name(key))
        except Exception as exc:  # noqa: BLE001
            stats["lookup_error"] += 1
            report_rows.append({**base, "lead_id": "", "subscription_id": "", "sequence": "",
                                "prior_status": "", "status": "error", "detail": f"lookup: {exc}"})
            continue

        if not lead_ids:
            stats["no_match"] += 1
            report_rows.append({**base, "lead_id": "", "subscription_id": "", "sequence": "",
                                "prior_status": "", "status": "no_match", "detail": "no lead found"})
            print(f"[{i}/{len(rows)}] {raw_key} -> NO MATCH")
            continue

        lead_ids = list(dict.fromkeys(lead_ids))
        if len(lead_ids) > 1:
            stats["multi_match_rows"] += 1

        for lead_id in lead_ids:
            try:
                subs = fetch_lead_subscriptions(lead_id)
            except Exception as exc:  # noqa: BLE001
                stats["lookup_error"] += 1
                report_rows.append({**base, "lead_id": lead_id, "subscription_id": "",
                                    "sequence": "", "prior_status": "", "status": "error",
                                    "detail": f"subscription fetch: {exc}"})
                continue

            hits = [s for s in subs if should_act(s, target_seq_ids, target_statuses)]
            if not hits:
                stats["not_subscribed"] += 1
                other = ", ".join(f"{seq_name_by_id.get(str(s.get('sequence_id')), '?')}"
                                  f"={s.get('status')}" for s in subs) or "none"
                report_rows.append({**base, "lead_id": lead_id, "subscription_id": "",
                                    "sequence": "", "prior_status": "", "status": "not_subscribed",
                                    "detail": f"subscriptions on lead: {other}"})
                print(f"[{i}/{len(rows)}] {raw_key} {lead_id} -> no matching subscription")
                continue

            print(f"[{i}/{len(rows)}] {raw_key} {lead_id} -> {len(hits)} subscription(s)")
            for sub in hits:
                sub_id = str(sub.get("id"))
                seq_id = str(sub.get("sequence_id"))
                prior = str(sub.get("status", ""))
                try:
                    ok, detail = apply_action(sub_id, args.action, args.dry_run)
                except Exception as exc:  # noqa: BLE001
                    ok, detail = False, str(exc)
                status = (f"would_{args.action}" if args.dry_run and ok
                          else PAST_TENSE[args.action] if ok else "failed")
                stats[status] += 1
                report_rows.append({**base, "lead_id": lead_id, "subscription_id": sub_id,
                                    "sequence": seq_name_by_id.get(seq_id, seq_id),
                                    "prior_status": prior, "status": status, "detail": detail})
                print(f"  {'->' if ok else '!!'} {sub_id} "
                      f"[{seq_name_by_id.get(seq_id, seq_id)}] {prior} -> {status} ({detail})")

    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["row", "match_key", "lead_id", "subscription_id",
                                                "sequence", "prior_status", "status", "detail"])
        writer.writeheader()
        writer.writerows(report_rows)

    acted = stats[PAST_TENSE[args.action]] + stats[f"would_{args.action}"]
    label = f"Subscriptions {PAST_TENSE[args.action]}:"
    print("\n" + "=" * 60)
    print("DRY RUN — no writes were made" if args.dry_run else "LIVE RUN")
    print(f"CSV rows:                {len(rows)}")
    print(f"Rows matching >1 lead:   {stats['multi_match_rows']}")
    print(f"{label:<25}{acted}")
    print(f"Failed:                  {stats['failed']}")
    print(f"Leads not subscribed:    {stats['not_subscribed']}")
    print(f"No lead match:           {stats['no_match']}")
    print(f"Skipped (blank key):     {stats['blank_key']}")
    print(f"Lookup errors:           {stats['lookup_error']}")
    print(f"Report: {args.report}")
    print("=" * 60)

    return 1 if (stats["failed"] or stats["lookup_error"]) else 0


# ------------------------------------------------------------------ selftest

def selftest():
    checks = []

    def check(label, actual, expected):
        ok = actual == expected
        checks.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              + ("" if ok else f"  (got {actual!r}, want {expected!r})"))

    seqs = [{"id": "seq_AAA", "name": "Black Friday Promo"},
            {"id": "seq_BBB", "name": "30 Day Inactivity - Added Task"},
            {"id": "seq_CCC", "name": "Dupe Name"},
            {"id": "seq_DDD", "name": "Dupe Name"}]

    r, m, a = resolve_sequences(seqs, ["seq_AAA"])
    check("resolve by id", (r, m, a), ({"seq_AAA": "Black Friday Promo"}, [], []))
    r, m, a = resolve_sequences(seqs, ["black friday promo"])
    check("resolve by name, case-insensitive", r, {"seq_AAA": "Black Friday Promo"})
    r, m, a = resolve_sequences(seqs, ["Nope"])
    check("missing reported", (r, m), ({}, ["Nope"]))
    r, m, a = resolve_sequences(seqs, ["Dupe Name"])
    check("ambiguous name reported", len(a), 1)
    check("ambiguous not resolved", r, {})
    r, m, a = resolve_sequences(seqs, ["seq_AAA", "30 Day Inactivity - Added Task"])
    check("multiple targets", sorted(r), ["seq_AAA", "seq_BBB"])

    sub_active = {"sequence_id": "seq_AAA", "status": "active"}
    sub_paused = {"sequence_id": "seq_AAA", "status": "paused"}
    sub_other = {"sequence_id": "seq_BBB", "status": "active"}
    sub_done = {"sequence_id": "seq_AAA", "status": "finished"}
    sub_goal = {"sequence_id": "seq_AAA", "status": "goal"}

    check("active in target seq acts", should_act(sub_active, {"seq_AAA"}, ("active",)), True)
    check("other seq skipped", should_act(sub_other, {"seq_AAA"}, ("active",)), False)
    check("finished skipped by default", should_act(sub_done, {"seq_AAA"}, ("active",)), False)
    check("goal skipped by default", should_act(sub_goal, {"seq_AAA"}, ("active",)), False)
    check("paused skipped by default", should_act(sub_paused, {"seq_AAA"}, ("active",)), False)
    check("paused acts when requested",
          should_act(sub_paused, {"seq_AAA"}, ("active", "paused")), True)
    check("all-sequences mode", should_act(sub_other, None, ("active",)), True)
    check("all-sequences still respects status", should_act(sub_done, None, ("active",)), False)

    check("detect lead_id wins", detect_match_mode(["Email", "Lead ID"]), ("lead_id", "Lead ID"))
    check("detect email", detect_match_mode(["Company", "Email"]), ("email", "Email"))
    check("detect none", detect_match_mode(["foo"]), (None, None))
    check("norm email", norm_email(" Bob@X.COM "), "bob@x.com")
    check("norm name", norm_name(" Acme   LLC "), "acme llc")
    check("LIVE_STATUSES sane", set(DEFAULT_TARGET_STATUSES) <= set(LIVE_STATUSES), True)

    check("pause defaults to active", DEFAULT_STATUSES_BY_ACTION["pause"], ("active",))
    check("resume defaults to paused", DEFAULT_STATUSES_BY_ACTION["resume"], ("paused",))
    check("pause writes paused", TARGET_STATUS_BY_ACTION["pause"], "paused")
    check("resume writes active", TARGET_STATUS_BY_ACTION["resume"], "active")
    check("finish past tense", PAST_TENSE["finish"], "finished")
    check("every action has a label", sorted(PAST_TENSE),
          ["delete", "finish", "pause", "resume"])
    check("every non-delete action writes a status",
          sorted(TARGET_STATUS_BY_ACTION), ["finish", "pause", "resume"])
    check("resume targets paused sub", should_act(sub_paused, {"seq_AAA"},
                                                  DEFAULT_STATUSES_BY_ACTION["resume"]), True)
    check("resume skips already-active sub", should_act(sub_active, {"seq_AAA"},
                                                        DEFAULT_STATUSES_BY_ACTION["resume"]), False)
    check("resume skips finished sub", should_act(sub_done, {"seq_AAA"},
                                                  DEFAULT_STATUSES_BY_ACTION["resume"]), False)

    print(f"\n{sum(checks)}/{len(checks)} checks passed")
    return 0 if all(checks) else 1


def main():
    p = argparse.ArgumentParser(
        description="Bulk pause/finish/remove leads from a Close Workflow (Sequence) via CSV.")
    p.add_argument("--csv", help="path to the input CSV")
    p.add_argument("--sequence", action="append", default=[],
                   help="Workflow to remove from: seq_xxx id or exact name. Repeatable.")
    p.add_argument("--all-sequences", action="store_true",
                   help="act on EVERY Workflow the lead is in (use with care)")
    p.add_argument("--action", choices=["pause", "resume", "finish", "delete"], default="pause",
                   help="pause (default, reversible) | resume (undo a pause) | "
                        "finish | delete (irreversible)")
    p.add_argument("--statuses", default=None,
                   help="comma-separated subscription statuses to act on. Defaults per action: "
                        "pause=active, resume=paused, finish/delete=active,paused")
    p.add_argument("--match-by", choices=["lead_id", "email", "name"])
    p.add_argument("--match-column")
    p.add_argument("--per-row-search", action="store_true",
                   help="search per row instead of building one lead index")
    p.add_argument("--limit", type=int, help="only process the first N CSV rows (testing)")
    p.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    p.add_argument("--report", default="sequence_removal_report.csv")
    p.add_argument("--list-sequences", action="store_true",
                   help="print all Workflows with their seq_ ids and exit")
    p.add_argument("--selftest", action="store_true", help="pure-logic checks, no network")
    args = p.parse_args()

    if args.selftest:
        return selftest()
    if not args.list_sequences:
        if not args.csv:
            p.error("--csv is required (or use --list-sequences / --selftest)")
        if not args.sequence and not args.all_sequences:
            p.error("specify --sequence (repeatable) or --all-sequences")
        if args.sequence and args.all_sequences:
            p.error("--sequence and --all-sequences are mutually exclusive")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
