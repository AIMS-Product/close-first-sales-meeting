"""Fresh concurrent snapshots for StatusExecutor; no cross-read evidence cache."""
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from queue import Empty, Queue
from threading import Event, Lock, Thread, local
from time import monotonic, sleep
import json
import math
import os

from close_status_batch import ACTIVITY_FIELDS, BODY_FIELDS, TASK_FIELDS, SnapshotCache
from close_status_policy import ORG, OWNER, Evidence, held_hash
from close_status_reader import CloseClient, SnapshotReader


class RequestLimiter:
    def __init__(self, requests_per_second=None):
        rate=float(os.environ.get('CLOSE_STATUS_REQUESTS_PER_SECOND','5')) if requests_per_second is None else float(requests_per_second)
        if not math.isfinite(rate) or not 1 <= rate <= 10:
            raise ValueError('Maintenance request rate must be between 1 and 10 per second')
        self.interval = 1 / rate
        self.next_start = 0.0
        self.lock = Lock()

    def pause(self, seconds):
        with self.lock:
            now = monotonic()
            delay = max(0, self.next_start - now)
            self.next_start = max(now, self.next_start) + max(self.interval, seconds)
        sleep(delay)


class SharedTransport:
    """Each thread owns its HTTP session; one limiter covers all reads and writes."""
    def __init__(self, api_key, limiter, *, write_enabled=False, trace=None, write_fence=None):
        self.api_key, self.limiter = api_key, limiter
        self.write_enabled, self.trace = write_enabled, trace
        self.write_fence = write_fence or Event()
        self.threads, self.lock, self.requests = local(), Lock(), []

    def _client(self):
        if not hasattr(self.threads, 'client'):
            self.threads.client = CloseClient(self.api_key, write_enabled=self.write_enabled, pause=self.limiter.pause)
        return self.threads.client

    def _invoke(self, method, *args):
        client = self._client()
        start = len(client.requests)
        try:
            return getattr(client, method)(*args)
        finally:
            receipts = client.requests[start:]
            with self.lock:
                self.requests.extend(receipts)
                if self.trace:
                    for receipt in receipts:
                        self.trace(receipt)

    def request(self, method, path, payload=None):
        if method == 'PUT' and self.write_fence.is_set():
            raise PermissionError('Batch write fence is closed')
        try:
            return self._invoke('request', method, path, payload)
        except Exception:
            if method == 'PUT':
                self.write_fence.set()
            raise

    def list(self, path, params):
        return self._invoke('list', path, params)


class FreshSnapshotBroker:
    """Coalesce only currently waiting reads. Every call loads all sources anew."""
    def __init__(self, read_client, write_client, *, group_size=50, collect_seconds=.15, capture_first=None):
        if read_client.write_enabled:
            raise PermissionError('Snapshot transport must reject writes')
        if not 1 <= group_size <= 50:
            raise ValueError('Snapshot group must contain 1–50 leads')
        self.read_client, self.client = read_client, write_client
        self.group_size, self.collect_seconds = group_size, collect_seconds
        self.capture_first = capture_first
        self.queue, self.pending = Queue(), deque()
        self.raw_first, self.capture_receipts = {}, []
        self.worker = Thread(target=self._serve, daemon=True)
        self.worker.start()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.queue.put(None)
        self.worker.join()

    def _submit(self, kind, arguments):
        result = Future()
        self.queue.put((kind, arguments, result))
        return result.result()

    def refresh_unsubscribed(self):
        return self._submit('refresh', None)

    def read(self, lead_id, now, *, held, roster):
        return self._submit('read', (lead_id, now, held, roster))

    def _serve(self):
        while True:
            first = self.pending.popleft() if self.pending else self.queue.get()
            if first is None:
                return
            group = [first]
            # Requests deferred behind another kind remain eligible for this wave.
            # Leaving them in pending would serialize each into its own full snapshot.
            waiting=list(self.pending)
            self.pending.clear()
            for item in waiting:
                if item is not None and item[0]==first[0] and len(group)<self.group_size:
                    group.append(item)
                else:
                    self.pending.append(item)
            deadline = monotonic() + self.collect_seconds
            while len(group) < self.group_size:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self.queue.get(timeout=remaining)
                except Empty:
                    break
                if item is None or item[0] != first[0]:
                    self.pending.append(item)
                else:
                    group.append(item)
            try:
                if first[0] == 'refresh':
                    SnapshotReader(self.read_client).refresh_unsubscribed()
                    values = [None] * len(group)
                else:
                    values = self._capture([item[1] for item in group])
                for item, value in zip(group, values):
                    item[2].set_result(value)
            except BaseException as error:
                for item in group:
                    item[2].set_exception(error)

    def _capture(self, requests):
        ids = [row[0] for row in requests]
        if len(ids) != len(set(ids)):
            raise ValueError('Concurrent duplicate lead readers rejected')
        held, roster = requests[0][2:]
        if any(row[2] != held or row[3] != roster for row in requests):
            raise ValueError('Mixed exclusion or roster contexts rejected')
        started = datetime.now(timezone.utc)
        suppressions = SnapshotReader(self.read_client)
        suppressions.refresh_unsubscribed()
        wanted = [lead_id for lead_id in ids if held_hash(lead_id) not in held]
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda lead_id: self.read_client.request('GET', f'/lead/{lead_id}/'), wanted))
        leads = dict(zip(wanted, rows))
        active = []
        output = {lead_id: Evidence(lead_id, '', ()) for lead_id in ids if held_hash(lead_id) in held}
        for lead_id, lead in leads.items():
            if lead['id'] != lead_id or lead['organization_id'] != ORG:
                raise ValueError('Fresh native lead scope mismatch')
            if lead_id not in self.raw_first:
                if self.capture_first:
                    self.capture_first(lead_id, lead)
                self.raw_first[lead_id] = lead
            raw = lead.get('custom.' + OWNER)
            owners = tuple(raw) if isinstance(raw, list) else (raw,) if raw else ()
            if set(owners) & roster.closer_ids:
                output[lead_id] = Evidence(lead_id, lead['status_id'], owners)
            else:
                active.append(lead_id)
        sources = {}
        if active:
            for path, metadata in ACTIVITY_FIELDS.items():
                fields = metadata + (',' + BODY_FIELDS[path] if path in BODY_FIELDS else '')
                rows = self.read_client.list(path, {'lead_id': ','.join(active), '_fields': fields})
                if any(row['lead_id'] not in active or row['organization_id'] != ORG for row in rows):
                    raise ValueError('Fresh activity scope mismatch')
                sources[path] = {lead_id: [] for lead_id in active}
                for row in rows:
                    sources[path][row['lead_id']].append(row)
            # Tasks accept one lead_id, unlike activity endpoints' comma-separated IDs.
            # Read each lead's complete open-task pages; never interpret an ignored filter as empty.
            def open_tasks(lead_id):
                tasks=self.read_client.list('/task/', {'lead_id':lead_id,'is_complete':'false','_fields':TASK_FIELDS})
                if any(row['lead_id']!=lead_id or row['organization_id']!=ORG or row['is_complete'] is not False for row in tasks):
                    raise ValueError('Fresh lead task snapshot incomplete or wrong scope')
                return tasks
            with ThreadPoolExecutor(max_workers=8) as pool:
                sources['/task/']=dict(zip(active,pool.map(open_tasks,active)))
            reader = SnapshotReader(SnapshotCache(leads, sources), suppressions.unsubscribed)
            reader.blocked_phones = suppressions.blocked_phones
            for lead_id, now, _, _ in requests:
                if lead_id in active:
                    output[lead_id] = reader.read(lead_id, now, held=held, roster=roster)
        self.capture_receipts.append({'started_at': started.isoformat(),
            'finished_at': datetime.now(timezone.utc).isoformat(), 'lead_ids': ids,
            'source_rows': {path: sum(map(len, buckets.values())) for path, buckets in sources.items()}})
        return [output[lead_id] for lead_id in ids]
