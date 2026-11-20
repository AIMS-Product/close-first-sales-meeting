"""Read-only discovery of Close meeting changes from a pinned SteelTrap ledger."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import time
from uuid import UUID

LEDGER = "steeltrap_staging.silver_core.crm_event_ledger_by_company"
OVERLAP = timedelta(hours=2)
MAX_CDF_VERSIONS = 256
MAX_ROWS = 10_000


def utc(value):
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lead_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"lead_[A-Za-z0-9]+", value):
        raise ValueError("Invalid Close lead identity")
    return value


@dataclass(frozen=True)
class Checkpoint:
    cutoff: datetime
    version: int


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    previous_version: int | None = None
    previous_cutoff: datetime | None = None

    @classmethod
    def next(cls, end, checkpoint=None):
        if end.tzinfo is None:
            raise ValueError("Close discovery cutoff must include a timezone")
        if checkpoint and end <= checkpoint.cutoff:
            raise ValueError("Cutoff must advance beyond the previous comparison")
        start = (checkpoint.cutoff if checkpoint else end) - OVERLAP
        return cls(start, end, checkpoint.version, checkpoint.cutoff) if checkpoint else cls(start, end)


@dataclass(frozen=True)
class ChangeSet:
    leads: frozenset[str]
    meetings: frozenset[str]
    events: dict
    version: int
    published_at: str
    late_events: int
    late_events_outside_overlap: int
    previous_leads: int


class DatabricksSQL:
    def __init__(self, warehouse, profile=None):
        if not re.fullmatch(r"[A-Za-z0-9]+", warehouse):
            raise ValueError("Invalid Databricks SQL warehouse ID")
        self.warehouse = warehouse
        self.profile = profile

    def _api(self, method, path, body=None):
        args = ["databricks", "api", method, path, "--output", "json"]
        if self.profile:
            args.extend(("--profile", self.profile))
        if body is not None:
            args.extend(("--json", "@/dev/stdin"))
        result = subprocess.run(args, input=None if body is None else json.dumps(body),
                                capture_output=True, text=True, timeout=45, check=False)
        if result.returncode:
            raise RuntimeError("Databricks SQL request failed")
        return json.loads(result.stdout)

    def read(self, statement):
        if ";" in statement or not re.match(r"\s*(?:SELECT|DESCRIBE HISTORY)\b", statement, re.I):
            raise ValueError("Only a single read-only Databricks statement is allowed")
        path = "/api/2.0/sql/statements"
        response = self._api("post", path, {
            "statement": statement, "warehouse_id": self.warehouse,
            "wait_timeout": "10s", "on_wait_timeout": "CONTINUE",
            "disposition": "INLINE", "format": "JSON_ARRAY",
        })
        result_path = f"{path}/{UUID(response['statement_id'])}"
        for _ in range(45):
            if response["status"]["state"] not in {"RUNNING", "PENDING"}:
                break
            time.sleep(1)
            response = self._api("get", result_path)
        if response["status"]["state"] != "SUCCEEDED":
            status = response["status"]
            error = status.get("error", {})
            codes = re.findall(r"\[[A-Z_0-9.]+\]", error.get("message", ""))[:2]
            raise RuntimeError(f"Databricks SQL {status['state']}: {error.get('error_code', '')} {' '.join(codes)}")
        manifest = response["manifest"]
        if manifest.get("truncated"):
            raise RuntimeError("Databricks returned truncated results")
        columns = [col["name"] for col in manifest["schema"]["columns"]]
        rows = list(response.get("result", {}).get("data_array", []))
        for i in range(1, manifest.get("total_chunk_count") or 1):
            chunk = self._api("get", f"{result_path}/result/chunks/{i}")
            rows.extend(chunk["data_array"])
        if len(rows) != manifest["total_row_count"]:
            raise RuntimeError("Databricks returned an incomplete result")
        return [dict(zip(columns, row, strict=True)) for row in rows]


class SteelTrapMeetingReader:
    def __init__(self, sql, company, organization):
        self.sql = sql
        self.company = str(UUID(company))
        if not re.fullmatch(r"orga_[A-Za-z0-9]+", organization):
            raise ValueError("Invalid Close organization")
        self.organization = organization

    def changes(self, window):
        history, = self.sql.read(f"DESCRIBE HISTORY {LEDGER} LIMIT 1")
        version = int(history["version"])
        query = self._query(window, version)
        rows = self.sql.read(query)
        if len(rows) > MAX_ROWS:
            raise RuntimeError("SteelTrap meeting change window exceeded capacity")

        events, previous_leads, late_ids, older_ids = {}, set(), set(), set()
        leads, meetings = set(), set()
        for row in rows:
            if row["change_type"] != "insert":
                raise RuntimeError("SteelTrap meeting ledger changed an existing row")
            if not row["event_id"] or not row["meeting_id"]:
                raise RuntimeError("SteelTrap meeting event identity missing")
            if row["lead_changed"] == "true" and not row["previous_lead"]:
                raise RuntimeError("SteelTrap moved meeting has no previous lead")
            lead = lead_id(row["current_lead"] or row["previous_lead"])
            prior = lead_id(row["previous_lead"]) if row["previous_lead"] else None
            if prior and prior != lead:
                previous_leads.add(prior)
                leads.add(prior)
            if row["source_us"] is None:
                raise RuntimeError("SteelTrap meeting event has no source timestamp")
            update = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(row["source_us"]))
            leads.add(lead)
            meetings.add(row["meeting_id"])
            if row["via_cdf"] == "true" and window.previous_cutoff and update < window.previous_cutoff:
                late_ids.add(row["id"])
                if update < window.start:
                    older_ids.add(row["id"])
            events.setdefault(row["event_id"], set()).update({lead, prior} - {None})
        return ChangeSet(frozenset(leads), frozenset(meetings), events, version,
                         history["timestamp"], len(late_ids), len(older_ids), len(previous_leads))

    def _query(self, window, version):
        if window.start >= window.end or min(version, window.previous_version or 0) < 0:
            raise ValueError("Invalid SteelTrap discovery range")
        end = window.end.astimezone(timezone.utc).isoformat()
        start = window.start.astimezone(timezone.utc).isoformat()
        source_time = "coalesce(source_updated_at, source_created_at, event_time)"
        scope = (f"company_id = '{self.company}' AND provider_org_id = '{self.organization}' "
                 "AND source_domain = 'crm' AND provider = 'close' AND provider_object_type = 'activity.meeting'")
        columns = f"""
            id, provider_event_id AS event_id, provider_object_id AS meeting_id,
            coalesce(get_json_object(provider_data, '$.lead_id'), provider_subject_id,
                     regexp_replace(canonical_subject_id, '^close:lead:', '')) AS current_lead,
            get_json_object(provider_previous_data, '$.lead_id') AS previous_lead,
            coalesce(array_contains(source_changed_fields, 'lead_id'), false) AS lead_changed,
            unix_micros({source_time}) AS source_us
        """
        replay = f"""
            SELECT {columns}, 'insert' AS change_type, false AS via_cdf
            FROM {LEDGER} VERSION AS OF {version}
            WHERE {scope} AND {source_time} >= TIMESTAMP '{start}' AND {source_time} < TIMESTAMP '{end}'
        """
        previous = window.previous_version
        if previous is not None:
            if previous > version or version - previous > MAX_CDF_VERSIONS:
                raise RuntimeError("SteelTrap checkpoint is outside the supported Delta change range")
            if previous < version:
                replay += f"""
                    UNION ALL
                    SELECT {columns}, _change_type AS change_type, true AS via_cdf
                    FROM table_changes('{LEDGER}', {previous + 1}, {version})
                    WHERE {scope} AND (_change_type <> 'insert'
                        OR {source_time} IS NULL OR {source_time} < TIMESTAMP '{end}')
                """
        return f"SELECT * FROM ({replay}) AS changes LIMIT {MAX_ROWS + 1}"
