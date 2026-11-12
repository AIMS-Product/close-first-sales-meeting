#!/usr/bin/env python3
"""Refresh only Close Lead Tier using approved creation age and import provenance.

Default mode is preview. Explicit apply enables only this field; no assignment,
release, rep access, messaging or workflow controls are enabled.
The script does not change lead ownership, status, sequences, workflows, or any
other CRM field.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lane2_policy import age_tier, imported_lead_query
BASE_URL = "https://api.close.com/api/v1"
LEGACY_DATE_FIELD = "cf_YnLqVPMXx3VKNHqKsk0YK1OYVl07p6hhuUBb2XM18UI"
ENTRY_SOURCE_FIELD = "cf_FI1V2Zw8M0aoSg7yJKJo0wb8NbRU8LafnkgHrpLcfYA"
LEAD_TIER_FIELD = "cf_Mpz1pnxLOSjscFDAErSDYOV0PSjlWKXSgBN4jWd73sM"
FIELD_NAMES = {
    LEGACY_DATE_FIELD: "Legacy Create Date",
    ENTRY_SOURCE_FIELD: "Entry Source",
    LEAD_TIER_FIELD: "Lead Tier",
}
ALLOWED_TIERS = {"Ramp", "Proven", "Reactivation", "Top"}
REAL_OPT_IN_SOURCES = {"Website-Apply", "Typeform", "Webinar", "Lead-Magnet"}
CHUNK_SIZE = 500
POLL_SECONDS = 4
HTTP_TIMEOUT = 90
EXPORT_TIMEOUT = 3600
ACTION_TIMEOUT = 1800
PACIFIC = ZoneInfo("America/Los_Angeles")


def load_api_key() -> str:
    key = os.environ.get("CLOSE_API_KEY")
    if key:
        return key
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "CLOSE_API_KEY":
                return value.strip().strip("\"'")
    raise RuntimeError("CLOSE_API_KEY not found in environment or repository-root .env")


def auth_headers(api_key: str) -> dict[str, str]:
    token = base64.b64encode((api_key + ":").encode()).decode()
    return {"Authorization": "Basic " + token}


def request_json(api_key: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    headers = auth_headers(api_key)
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE_URL + path, data=payload, headers=headers, method=method)
    for attempt in range(8):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            if exc.code != 429 or attempt == 7:
                raise RuntimeError(f"Close API HTTP {exc.code}: {detail}") from exc
            delay = float(exc.headers.get("Retry-After") or min(2 ** (attempt + 1), 30))
            print(f"Close rate limit; retrying in {delay:g}s", file=sys.stderr, flush=True)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError):
            # A timed-out POST may already have created an export or bulk edit.
            # Retrying it blindly could submit the same write twice.
            if method != "GET":
                raise RuntimeError(
                    f"Close {method} {path} had an ambiguous network outcome; "
                    "stopped without retrying to avoid duplicate submissions."
                )
            if attempt == 7:
                raise
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError("Close API retries exhausted")


class StripCrossHostAuthorization(urllib.request.HTTPRedirectHandler):
    """Keep the Close key off the signed export download host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).hostname != urllib.parse.urlsplit(newurl).hostname:
            redirected.remove_header("Authorization")
        return redirected



def export_download_request(url: str, api_key: str):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Export download requires an HTTPS URL without embedded credentials")
    # Signed storage URLs authenticate themselves. Keep the Close key on Close.
    headers = auth_headers(api_key) if parsed.hostname == "api.close.com" else {}
    return urllib.request.Request(url, headers=headers)


def export_all_leads(api_key: str, reuse_export_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = {
        "type": "leads",
        "format": "json",
        "results_limit": None,
        "fields": ["id", "display_name", "date_created", "custom"],
        "send_done_email": False,
        "s_query": {"type": "and", "queries": [{"type": "object_type", "object_type": "lead"}]},
        "sort": [{"direction": "asc", "field": {
            "type": "regular_field", "object_type": "lead", "field_name": "date_created",
        }}],
    }
    if reuse_export_id:
        if not reuse_export_id.startswith("expo_") or not reuse_export_id[5:].isalnum():
            raise ValueError("Invalid export ID")
        export = request_json(api_key, "GET", f"/export/lead/{reuse_export_id}/")
    else:
        export = request_json(api_key, "POST", "/export/lead/", payload)
    export_id = export.get("id")
    if not export_id:
        raise RuntimeError(f"Close did not return an export ID: {export}")
    print(f"Close lead export queued: {export_id}", flush=True)
    started = time.monotonic()
    while True:
        export = request_json(api_key, "GET", f"/export/lead/{urllib.parse.quote(export_id, safe='')}/")
        status = export.get("status")
        print(f"Export status={status}; processed={export.get('n_docs_processed')}/{export.get('n_docs')}", flush=True)
        if status == "done":
            break
        if status == "error":
            raise RuntimeError(f"Close lead export failed: {export}")
        if time.monotonic() - started > EXPORT_TIMEOUT:
            raise TimeoutError(f"Close export exceeded {EXPORT_TIMEOUT}s: {export_id}")
        time.sleep(5)

    download_url = export.get("download_url")
    if not download_url:
        raise RuntimeError("Completed Close export has no download URL")
    request = export_download_request(download_url, api_key)
    with urllib.request.build_opener(StripCrossHostAuthorization()).open(request, timeout=180) as response:
        data = response.read()
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    parsed = json.loads(data.decode("utf-8-sig"))
    if isinstance(parsed, dict):
        leads = parsed.get("data") or parsed.get("leads") or []
    elif isinstance(parsed, list):
        leads = parsed
    else:
        raise RuntimeError(f"Unexpected lead export payload: {type(parsed).__name__}")
    processed = export.get("n_docs_processed")
    if processed is None or int(processed) != len(leads):
        raise RuntimeError(
            f"Incomplete lead export; Close reports processed={processed}, downloaded={len(leads)}. "
            "No lead updates were submitted."
        )
    return leads, export


def custom_value(lead: dict[str, Any], field_id: str) -> Any:
    flat_key = f"custom.{field_id}"
    if flat_key in lead:
        return lead[flat_key]
    custom = lead.get("custom") or {}
    if isinstance(custom, dict):
        if field_id in custom:
            return custom[field_id]
        if flat_key in custom:
            return custom[flat_key]
        return custom.get(FIELD_NAMES[field_id])
    return None


def scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if len(value) == 1:
            return str(value[0])
        return " | ".join(str(item) for item in value if item not in (None, ""))
    return str(value).strip()


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).date()
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if "T" in text or " " in text:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=PACIFIC)
            return parsed.astimezone(PACIFIC).date()
        return parsed.date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def proposed_tier(age_days: int, entry_source: str) -> tuple[str | None, str]:
    return age_tier(age_days, entry_source)


def imported_lead_ids(api_key, leads):
    # Scope to the verified full export in <=500-ID chunks. This avoids Close's
    # 10k search cap and includes every creation age without date assumptions.
    imported = set()
    lead_ids = [lead["id"] for lead in leads]
    for offset in range(0, len(lead_ids), CHUNK_SIZE):
        scope = set(lead_ids[offset:offset + CHUNK_SIZE])
        payload = {"query": {"type": "and", "queries": [
            {"type": "object_type", "object_type": "lead"}, imported_lead_query(),
            {"type": "or", "queries": [{"type": "id", "value": lead_id} for lead_id in sorted(scope)]}]},
            "_fields": {"lead": ["id"]}, "_limit": 200}
        ids, cursors = set(), set()
        while True:
            page = request_json(api_key, "POST", "/data/search/", payload)
            rows = page.get("data")
            if not isinstance(rows, list):
                raise RuntimeError("Imported-list provenance search incomplete")
            for row in rows:
                if row.get("id") not in scope or row["id"] in ids:
                    raise RuntimeError("Imported-list provenance search scope/identity mismatch")
                ids.add(row["id"])
            cursor = page.get("cursor")
            if not cursor:
                count = page.get("count")
                if isinstance(count, int) and count != len(ids):
                    raise RuntimeError("Imported-list provenance search incomplete")
                imported.update(ids)
                break
            if cursor in cursors or not rows:
                raise RuntimeError("Imported-list provenance pagination incomplete")
            cursors.add(cursor)
            payload["cursor"] = cursor
    return imported


def classify_leads(leads: list[dict[str, Any]], run_date: date, imported_ids: set[str] | None = None) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for lead in leads:
        lead_id = str(lead.get("id") or "")
        if not lead_id:
            raise RuntimeError("Close export includes a lead without an ID; no updates were submitted")
        if lead_id in seen:
            raise RuntimeError(f"Duplicate lead in Close export: {lead_id}")
        seen.add(lead_id)
        legacy_raw = custom_value(lead, LEGACY_DATE_FIELD)
        close_raw = lead.get("date_created")
        legacy_date = parse_date(legacy_raw)
        close_date = parse_date(close_raw)
        effective_date = close_date
        date_source = "Close date_created" if close_date else ""
        entry_source = scalar(custom_value(lead, ENTRY_SOURCE_FIELD)).strip()
        current_raw = custom_value(lead, LEAD_TIER_FIELD)
        current = scalar(current_raw).strip()
        issue = ""
        if imported_ids is None:
            issue = "import provenance census missing"
        elif effective_date is None and lead_id not in imported_ids:
            issue = "no usable Close date_created"
        if issue:
            target, reason = None, issue
            age_days = ""
        else:
            age_days = (run_date - effective_date).days if effective_date else 0
            target, reason = age_tier(age_days, entry_source, lead_id in imported_ids)
            if target is None:
                reason = f"REVIEW: {reason}"
        rows.append({
            "lead_id": lead_id,
            "name": str(lead.get("display_name") or ""),
            "close_url": f"https://app.close.com/lead/{lead_id}/",
            "legacy_create_date": str(legacy_raw or ""),
            "close_date_created": str(close_raw or ""),
            "effective_date": effective_date.isoformat() if effective_date else "",
            "effective_date_source": date_source,
            "age_days": age_days,
            "entry_source": entry_source,
            "current_tier": current,
            "proposed_tier": target or "REVIEW",
            "change_needed": "yes" if target and target != current else "no",
            "reason": reason,
        })
    return rows


def make_guarded_query(lead_ids: list[str], old_tier: str) -> dict[str, Any]:
    queries: list[dict[str, Any]] = [
        {"type": "object_type", "object_type": "lead"},
        {"type": "or", "queries": [{"type": "id", "value": lead_id} for lead_id in lead_ids]},
    ]
    if old_tier:
        queries.append({
            "type": "field_condition",
            "field": {"type": "custom_field", "custom_field_id": LEAD_TIER_FIELD},
            "condition": {"type": "term", "values": [value for value in old_tier.split(" | ") if value]},
        })
    else:
        queries.append({
            "type": "field_condition",
            "field": {"type": "custom_field", "custom_field_id": LEAD_TIER_FIELD},
            "condition": {"type": "exists"},
            "negate": True,
        })
    return {"type": "and", "queries": queries}


def create_bulk_edit(api_key: str, new_tier: str, old_tier: str, lead_ids: list[str]) -> dict[str, Any]:
    return request_json(api_key, "POST", "/bulk_action/edit/", {
        "type": "set_custom_field",
        "custom_field_id": LEAD_TIER_FIELD,
        "custom_field_value": new_tier,
        "custom_field_operation": "replace",
        "send_done_email": False,
        "results_limit": None,
        "s_query": make_guarded_query(lead_ids, old_tier),
    })


def poll_bulk_edit(api_key: str, action_id: str) -> dict[str, Any]:
    started = time.monotonic()
    while True:
        result = request_json(api_key, "GET", f"/bulk_action/edit/{urllib.parse.quote(action_id, safe='')}/")
        status = result.get("status")
        print(f"Bulk edit {action_id}: {status}; processed={result.get('n_leads_processed')}/{result.get('n_leads')}", flush=True)
        if status in {"done", "completed", "complete"}:
            return result
        if status in {"error", "failed", "canceled", "cancelled", "paused"}:
            raise RuntimeError(f"Close bulk edit {action_id} stopped: {result}")
        if time.monotonic() - started > ACTION_TIMEOUT:
            raise TimeoutError(f"Close bulk edit {action_id} still running after {ACTION_TIMEOUT}s: {result}")
        time.sleep(POLL_SECONDS)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["lead_id", "name", "close_url", "legacy_create_date", "close_date_created",
              "effective_date", "effective_date_source", "age_days", "entry_source", "current_tier",
              "proposed_tier", "change_needed", "reason"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def require_fresh_export(export, now):
    value = export.get('date_created')
    if not isinstance(value, str):
        raise RuntimeError('Export creation timestamp is unavailable')
    created = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if created.tzinfo is None:
        raise RuntimeError('Export creation timestamp must include its timezone')
    age = (now - created).total_seconds()
    if not 0 <= age <= 1800 or created.astimezone(PACIFIC).date() != now.astimezone(PACIFIC).date():
        raise RuntimeError('Apply requires a same-day export less than 30 minutes old')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write changed Lead Tier values to Close")
    mode.add_argument("--dry-run", action="store_true", help="preview only (the default)")
    parser.add_argument("--export-id", help="Resume an existing read-only Close lead export")
    parser.add_argument("--run-date", help="override the Pacific run date (YYYY-MM-DD), mainly for review")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE, help=f"leads per bulk edit (default {CHUNK_SIZE})")
    args = parser.parse_args(argv)
    if args.apply and args.run_date:
        parser.error("Apply requires today's Pacific date")
    (ROOT / "outputs").mkdir(exist_ok=True)
    if not 1 <= args.chunk_size <= 500:
        parser.error("--chunk-size must be between 1 and 500")

    run_date = date.fromisoformat(args.run_date) if args.run_date else datetime.now(PACIFIC).date()
    api_key = load_api_key()
    schema = request_json(api_key, "GET", f"/custom_field/lead/{LEAD_TIER_FIELD}/")
    if schema.get("id") != LEAD_TIER_FIELD or schema.get("name") != "Lead Tier" or schema.get("type") != "choices":
        raise RuntimeError(f"Unexpected Lead Tier field schema: {schema}")
    schema_choices = schema.get("choices") or []
    choice_values = {choice.get("value") if isinstance(choice, dict) else choice for choice in schema_choices}
    if not ALLOWED_TIERS.issubset(choice_values):
        raise RuntimeError(f"Lead Tier choices do not match the approved values: {schema_choices}")

    leads, export = export_all_leads(api_key, args.export_id)
    if args.apply:
        require_fresh_export(export, datetime.now(timezone.utc))
    rows = classify_leads(leads, run_date, imported_lead_ids(api_key, leads))
    timestamp = datetime.now(PACIFIC).strftime("%Y%m%dT%H%M%S%z")
    report_path = ROOT / "outputs" / f"lead_tier_sync_{timestamp}.csv"
    summary_path = ROOT / "outputs" / f"lead_tier_sync_{timestamp}.json"
    write_csv(report_path, rows)

    proposed_counts = Counter(row["proposed_tier"] for row in rows if row["proposed_tier"] in ALLOWED_TIERS)
    current_counts = Counter(row["current_tier"] or "(blank)" for row in rows)
    changed = [row for row in rows if row["change_needed"] == "yes"]
    review_rows = [row for row in rows if row["proposed_tier"] == "REVIEW"]
    print(f"Scanned {len(rows)} exported leads as of {run_date.isoformat()}")
    print(f"Current tiers: {dict(current_counts)}")
    print(f"Proposed tiers: {dict(proposed_counts)}")
    print(f"Changes: {len(changed)}; review: {len(review_rows)}")
    print(f"Dry-run report: {report_path}")
    if not args.apply:
        summary = {
            "mode": "dry-run",
            "run_date_pacific": run_date.isoformat(),
            "lead_tier_field_id": LEAD_TIER_FIELD,
            "export_id": export.get("id"),
            "export_n_docs": export.get("n_docs"),
            "export_n_docs_processed": export.get("n_docs_processed"),
            "downloaded_leads": len(leads),
            "current_tiers": dict(current_counts),
            "proposed_tiers": dict(proposed_counts),
            "changes_proposed": len(changed),
            "review_count": len(review_rows),
            "csv": str(report_path),
        }
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"Summary: {summary_path}")
        return 0

    if datetime.now(PACIFIC).date() != run_date:
        raise RuntimeError("Pacific date changed during capture; refresh before applying")
    require_fresh_export(export, datetime.now(timezone.utc))
    if review_rows:
        raise RuntimeError("Unclassified leads require review before applying")

    if not changed:
        summary_path.write_text(json.dumps({"mode": "apply", "run_date_pacific": run_date.isoformat(),
            "leads_scanned": len(rows), "changes_applied": 0, "review_count": len(review_rows),
            "csv": str(report_path)}, indent=2) + "\n", encoding="utf-8")
        print("No Lead Tier updates needed.")
        return 0

    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in changed:
        grouped[(row["proposed_tier"], row["current_tier"])].append(row["lead_id"])
    action_records: list[dict[str, Any]] = []
    for (new_tier, old_tier), ids in grouped.items():
        chunks = [ids[i:i + args.chunk_size] for i in range(0, len(ids), args.chunk_size)]
        for chunk_index, chunk in enumerate(chunks, start=1):
            created = create_bulk_edit(api_key, new_tier, old_tier, chunk)
            action_id = created.get("id")
            if not action_id:
                raise RuntimeError(f"Close did not return a bulk edit ID: {created}")
            record = {"action_id": action_id, "old_tier": old_tier or "(blank)",
                      "new_tier": new_tier, "lead_ids": chunk, "status": created.get("status", "created")}
            action_records.append(record)
            summary_path.write_text(json.dumps({"mode": "applying", "run_date_pacific": run_date.isoformat(),
                "export_id": export.get("id"), "actions": action_records,
                "changes_proposed": len(changed), "csv": str(report_path)}, indent=2) + "\n")
            print(f"Submitted {new_tier} ({old_tier or '(blank)'} → {new_tier}) batch {chunk_index}/{len(chunks)}: {action_id}", flush=True)

    processed_by_tier: Counter[str] = Counter()
    actions_with_guard_skips: list[dict[str, Any]] = []
    for action in action_records:
        result = poll_bulk_edit(api_key, action["action_id"])
        processed = int(result.get("n_leads_processed") or 0)
        action.update({"status": result.get("status"), "n_leads": result.get("n_leads"),
                       "n_leads_processed": processed})
        processed_by_tier[action["new_tier"]] += processed
        if processed != len(action["lead_ids"]):
            actions_with_guard_skips.append({"action_id": action["action_id"],
                                             "tier": action["new_tier"],
                                             "requested": len(action["lead_ids"]), "processed": processed})

    summary = {
        "mode": "apply",
        "run_date_pacific": run_date.isoformat(),
        "lead_tier_field_id": LEAD_TIER_FIELD,
        "export_id": export.get("id"),
        "export_n_docs": export.get("n_docs"),
        "export_n_docs_processed": export.get("n_docs_processed"),
        "downloaded_leads": len(leads),
        "changes_proposed": len(changed),
        "updates_processed_by_tier": dict(processed_by_tier),
        "review_count": len(review_rows),
        "actions": action_records,
        "guard_skips_to_review": actions_with_guard_skips,
        "csv": str(report_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Apply summary: {summary_path}")
    print(json.dumps({key: summary[key] for key in (
        "downloaded_leads", "changes_proposed", "updates_processed_by_tier", "review_count", "guard_skips_to_review",
    )}, indent=2))
    return 2 if review_rows or actions_with_guard_skips else 0


if __name__ == "__main__":
    raise SystemExit(main())
