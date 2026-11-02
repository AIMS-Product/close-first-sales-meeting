"""Shared, composable sales holds; never touches the immutable audit ledger."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import threading
import json
import os
from pathlib import Path
import re
import sqlite3

BATCH = 'cf_IruzyS5PO4dZsOKGQSYb4r7PhOEpw1C82FN17ZlmkQy'
OWNER_SINCE = 'cf_2tuoNM0K9ND9na49hMsj02KI7732MuKSiFX2thJuIgU'
SOURCE = 'cf_HyxM8Psr8ZjrabqeR8hqAf4XtTArArHaAyHRvsrCwm1'
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
WORKFLOWS = frozenset(('sms_rest', 'qa', 'education', 'recovery'))
MARKER = re.compile(r'\[STH1 ([a-z_]+) ([A-Za-z0-9_-]+)\]')


def custom(lead, field):
    return lead.get('custom.' + field, (lead.get('custom') or {}).get(field))


def no_sales_hold_query():
    return {'type': 'field_condition', 'negate': True,
            'field': {'type': 'custom_field', 'custom_field_id': BATCH},
            'condition': {'type': 'text', 'mode': 'full_words', 'value': 'STH1'}}


def marker(workflow, token):
    if workflow not in WORKFLOWS or not re.fullmatch(r'[A-Za-z0-9_-]+', token):
        raise ValueError('Invalid hold identity')
    return f'[STH1 {workflow} {token}]'


def project(batch, workflow, token, *, acquire):
    """Edit our exact marker only; preserve original text and all other owners."""
    value = batch or ''
    own = marker(workflow, token)
    if acquire:
        return value if own in value else value + (' ' if value else '') + own
    if own not in value:
        raise ValueError('Our hold marker is missing; reconcile before release')
    # Only remove the separator added with our marker, never strip original text.
    return value.replace(' ' + own, '', 1) if ' ' + own in value else value.replace(own, '', 1) or None


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.executescript('''
          CREATE TABLE IF NOT EXISTS sales_hold(
            lead_id TEXT NOT NULL, workflow TEXT NOT NULL, token TEXT NOT NULL,
            acquired_at TEXT NOT NULL, expires_at TEXT, payload TEXT NOT NULL,
            PRIMARY KEY(lead_id, workflow));
          CREATE TABLE IF NOT EXISTS sms_cycle(
            lead_id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL,
            phase TEXT NOT NULL, due_at TEXT, payload TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS sms_due ON sms_cycle(due_at);
          CREATE TABLE IF NOT EXISTS workflow_event(
            event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS workflow_write(
            id INTEGER PRIMARY KEY, lead_id TEXT NOT NULL, token TEXT NOT NULL,
            state TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS workflow_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')

    @contextmanager
    def serialized(self):
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(str(self.path.resolve()), threading.RLock())
        with lock, open(str(self.path) + '.lock', 'a') as fence:
            fcntl.flock(fence, fcntl.LOCK_EX)
            try:
                yield self
            finally:
                fcntl.flock(fence, fcntl.LOCK_UN)

    def holds(self, lead_id):
        return [dict(x) for x in self.connection.execute('SELECT * FROM sales_hold WHERE lead_id=?', (lead_id,))]

    def acquire(self, lead_id, workflow, token, acquired_at, expires_at=None, payload=None):
        marker(workflow, token)
        existing = self.connection.execute('SELECT token FROM sales_hold WHERE lead_id=? AND workflow=?',
                                           (lead_id, workflow)).fetchone()
        if existing and existing['token'] != token:
            raise ValueError('Another generation owns this workflow hold')
        self.connection.execute('INSERT OR IGNORE INTO sales_hold VALUES(?,?,?,?,?,?)',
                                (lead_id, workflow, token, acquired_at, expires_at, json.dumps(payload or {})))

    def release(self, lead_id, workflow, token):
        marker(workflow, token)
        result = self.connection.execute('DELETE FROM sales_hold WHERE lead_id=? AND workflow=? AND token=?',
                                         (lead_id, workflow, token))
        if result.rowcount != 1:
            raise ValueError('Hold owner/token mismatch')

    def cycle(self, lead_id):
        row = self.connection.execute('SELECT payload FROM sms_cycle WHERE lead_id=?', (lead_id,)).fetchone()
        return json.loads(row['payload']) if row else None

    def save(self, cycle):
        self.connection.execute('INSERT INTO sms_cycle VALUES(?,?,?,?,?) ON CONFLICT(lead_id) DO UPDATE SET '
                                'token=excluded.token,phase=excluded.phase,due_at=excluded.due_at,payload=excluded.payload',
                                (cycle['lead_id'], cycle['token'], cycle['phase'], cycle.get('due_at'), json.dumps(cycle)))

    def due(self, now, limit=100):
        return [json.loads(x['payload']) for x in self.connection.execute(
            'SELECT payload FROM sms_cycle WHERE due_at<=? ORDER BY due_at LIMIT ?', (now, limit))]

    def intent(self, lead_id, token, payload):
        cursor = self.connection.execute('INSERT INTO workflow_write(lead_id,token,state,payload) VALUES(?,?,?,?)',
                                         (lead_id, token, 'uncertain', json.dumps(payload)))
        return cursor.lastrowid

    def receipt(self, write_id):
        self.connection.execute("UPDATE workflow_write SET state='verified' WHERE id=?", (write_id,))

    def uncertain(self, lead_id):
        return [dict(x, payload=json.loads(x['payload'])) for x in self.connection.execute(
            "SELECT * FROM workflow_write WHERE lead_id=? AND state='uncertain'", (lead_id,))]

    def close(self):
        self.connection.close()


def configured_store():
    if os.environ.get('WORKFLOW_HOLD_BACKEND') == 'postgres':
        return open_store()
    path = os.environ.get('WORKFLOW_HOLD_DB')
    if not path:
        if os.environ.get('WORKFLOW_HOLDS_REQUIRED') == '1':
            raise RuntimeError('Shared hold database required')
        return None
    if not Path(path).is_file():
        raise RuntimeError('Shared hold database missing; refuse automated writes')
    return Store(path)


def blocked(lead, store=None):
    # Unknown/malformed markers also fail closed.
    return 'STH1' in str(custom(lead, BATCH) or '') or bool(store and store.holds(lead['id']))


def guard_request(request):
    """Guard requests-style lead/opportunity mutations with one shared fence.

    Callers pass expected_owner for planned assignments/releases. Durable intents
    prevent automatic retry after a timeout, crash or uncertain readback.
    """
    def guarded(method, url, **kwargs):
        expected_owner = kwargs.pop('expected_owner', ...)
        expected_since = kwargs.pop('expected_owner_since', ...)
        from sales_dispatch_holds import is_dispatch_mutation, guard_dispatch
        if is_dispatch_mutation(method, url, kwargs.get('json') or {}):
            return guard_dispatch(request, method, url, **kwargs)
        match = re.search(r'/(lead|opportunity)/([A-Za-z0-9_]+)/$', url)
        if method.upper() != 'PUT' or not match:
            return request(method, url, **kwargs)
        store = configured_store()
        @contextmanager
        def lock():
            if store:
                with store.serialized(): yield
            else:
                yield
        try:
            with lock():
                read_kwargs = {key: kwargs[key] for key in ('auth','headers','timeout','verify') if key in kwargs}
                target = request('GET', url, **read_kwargs).json()
                lead_url = url if match[1] == 'lead' else url.split('/opportunity/')[0] + '/lead/' + target['lead_id'] + '/'
                current = target if match[1] == 'lead' else request('GET', lead_url, **read_kwargs).json()
                from close_status_policy import ORG
                expected_lead = match[2] if match[1] == 'lead' else target['lead_id']
                if store and (current.get('id') != expected_lead or current.get('organization_id') != ORG):
                    raise ValueError('Shared writer lead tenant/scope mismatch')
                if blocked(current, store): raise RuntimeError('Sales hold: write excluded')
                if store and store.uncertain(current['id']): raise RuntimeError('Uncertain write: reconcile before retry')
                current_owner = custom(current,'cf_gOfS9pFwext58oberEegLyix8hZzeHrxhCZOVh3P3rd')
                if expected_owner is not ... and current_owner != expected_owner:
                    raise RuntimeError('Owner changed before write')
                if expected_since is not ... and custom(current,OWNER_SINCE) != expected_since:
                    raise RuntimeError('Owner generation changed before write')
                payload = kwargs.get('json') or {}
                path = '/' + match[1] + '/' + match[2] + '/'
                write_id = store.intent(current['id'],'shared_writer',{'path':path,'patch':payload}) if store else None
                response = request(method, url, **kwargs)
                if getattr(response,'raise_for_status',None): response.raise_for_status()
                after = request('GET', url, **read_kwargs).json()
                if any((after.get(k) if not k.startswith('custom.') else custom(after,k[7:])) != v for k,v in payload.items()):
                    raise RuntimeError('Write readback differs; reconcile before retry')
                if store: store.receipt(write_id)
                return response
        finally:
            if store: store.close()
    return guarded


def guard_status_request(request, validate=None):
    """Adapt the same gate to CloseClient's dictionary-returning status transport."""
    class Response:
        def __init__(self, value): self.value=value
        def json(self): return self.value
    def transport(method,url,**kwargs):
        path=url.split('/api/v1')[-1]
        return Response(request(method,path,kwargs.get('json')))
    guarded=guard_request(transport)
    def invoke(method,path,payload=None):
        if validate: validate(method,path,payload)
        configured = (os.environ.get('WORKFLOW_HOLDS_REQUIRED') == '1' or os.environ.get('WORKFLOW_HOLD_DB')
                      or os.environ.get('WORKFLOW_HOLD_BACKEND') == 'postgres')
        if not configured: return request(method,path,payload)
        if method=='PUT': return guarded(method,'https://api.close.com/api/v1'+path,json=payload).json()
        return request(method,path,payload)
    return invoke


def is_held(lead):
    store=configured_store()
    try: return blocked(lead,store)
    finally:
        if store: store.close()


class PostgresStore(Store):
    """Production adapter: one shared CRM control schema and advisory fence.

    Uses existing managed Lakebase credentials. Schema installation is a separate,
    approval-gated migration; opening this adapter never runs DDL or seeds rows.
    The global organization lock deliberately serializes all pilot workflow writes.
    """
    TENANT = '88a28173-2a61-4cbb-92ea-f6b8be4592f3/orga_Qwbx9tyUrl7Jdjurg9xJ8N6w1MN4PD2NQOgIhd8r7rC'

    def __init__(self, connection):
        from psycopg.rows import dict_row
        if not connection.autocommit:
            raise ValueError('Workflow intents require an autocommit connection')
        connection.row_factory = dict_row
        self.raw = connection
        self.connection = _PostgresConnection(connection)
        row = self.connection.execute("SELECT value FROM workflow_meta WHERE key='tenant'").fetchone()
        if not row or row['value'] != self.TENANT:
            raise ValueError('Shared workflow schema tenant mismatch or migration missing')
        version = self.connection.execute("SELECT value FROM workflow_meta WHERE key='hold_contract_version'").fetchone()
        if not version or version['value'] != '1':
            raise ValueError('Shared workflow hold contract version mismatch')
        self._depth = 0

    def acquire(self, *args, **kwargs):
        if not self._depth:
            raise RuntimeError('Shared hold acquisition requires the tenant advisory fence')
        return super().acquire(*args, **kwargs)

    def release(self, *args, **kwargs):
        if not self._depth:
            raise RuntimeError('Shared hold release requires the tenant advisory fence')
        return super().release(*args, **kwargs)

    @contextmanager
    def serialized(self):
        if self._depth:
            self._depth += 1
            try: yield self
            finally: self._depth -= 1
            return
        self.raw.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', (self.TENANT,))
        self._depth = 1
        try:
            yield self
        finally:
            self._depth = 0
            self.raw.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (self.TENANT,))

    def intent(self, lead_id, token, payload):
        row = self.connection.execute('INSERT INTO workflow_write(lead_id,token,state,payload) VALUES(?,?,?,?) RETURNING id',
                                      (lead_id,token,'uncertain',json.dumps(payload))).fetchone()
        return row['id']

    def close(self):
        self.raw.close()


class _PostgresConnection:
    def __init__(self, connection): self.raw = connection

    def execute(self, sql, params=()):
        sql = sql.replace('?', '%s')
        if sql.startswith('INSERT OR IGNORE INTO '):
            sql = sql.replace('INSERT OR IGNORE INTO ', 'INSERT INTO ', 1) + ' ON CONFLICT DO NOTHING'
        return self.raw.execute(sql, params)


def open_store(*, local_path=None):
    backend = os.environ.get('WORKFLOW_HOLD_BACKEND', 'sqlite')
    if backend == 'postgres':
        import psycopg
        import requests
        connection = psycopg.connect(host=os.environ['LAKEBASE_HOST'],dbname=os.environ['LAKEBASE_DATABASE'],
            user=os.environ['LAKEBASE_USER'],password=os.environ['LAKEBASE_PASSWORD'],autocommit=True,
            sslmode='verify-full',sslrootcert=os.environ.get('LAKEBASE_SSLROOTCERT') or requests.certs.where(),
            connect_timeout=20,options='-c search_path=crm_workflow_control -c statement_timeout=30000 -c lock_timeout=30000')
        try:
            return PostgresStore(connection)
        except BaseException:
            connection.close()
            raise
    if backend != 'sqlite': raise ValueError('Unknown workflow hold backend')
    path = local_path or os.environ.get('WORKFLOW_HOLD_DB')
    if not path: raise ValueError('Local workflow database path required')
    return Store(path)
