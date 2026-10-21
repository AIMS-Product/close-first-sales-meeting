"""Bounded, creation-fenced batch reads. Never used as the executor's write evidence."""
from datetime import datetime, timezone
from urllib.parse import urlencode
from close_status_policy import ORG, OWNER, WINDOW, Evidence, held_hash, timestamp
from close_status_reader import SnapshotReader

BATCH_SIZE = 50
PAGE_SIZE = 100
PARTITION_LIMIT = 9000
LEAD_FIELDS = ['id', 'organization_id', 'status_id', 'contacts', 'opportunities',
               'custom.' + OWNER, 'custom.cf_L1HjUOMsqq6sSADgSMn4aH2bma8ohho5enH7jeL1yRJ']
ACTIVITY_FIELDS = {
    '/activity/sms/': 'id,lead_id,organization_id,direction,status,date_created,activity_at,date_sent',
    '/activity/email/': 'id,lead_id,organization_id,direction,status,date_created,activity_at,date_sent',
    '/activity/call/': 'id,lead_id,organization_id,direction,activity_at,remote_phone',
    '/activity/status_change/lead/': 'id,lead_id,organization_id,old_status_id,new_status_id,old_status_label,new_status_label',
    '/activity/meeting/': 'id,lead_id,organization_id,status,starts_at',
    '/activity/note/': 'id,lead_id,organization_id,activity_at',
    '/activity/form_submission/': 'id,lead_id,organization_id,activity_at',
    '/activity/whatsapp_message/': 'id,lead_id,organization_id,direction,activity_at',
}
BODY_FIELDS = {'/activity/sms/': 'text,remote_phone', '/activity/email/': 'body_text,sender',
               '/activity/call/': 'note', '/activity/note/': 'note'}
TASK_FIELDS = 'id,lead_id,organization_id,text,date_created,is_complete'


def fenced_rows(client, path, params, cutoff):
    fields = ','.join(dict.fromkeys(params['_fields'].split(',') + ['date_created']))
    def partition(start, end):
        query = dict(params, _fields=fields, _order_by='date_created',
                     date_created__gte=start.isoformat(), date_created__lt=end.isoformat())
        rows, seen = [], set()
        while True:
            page = client.request('GET', path + '?' + urlencode(dict(query, _limit=PAGE_SIZE, _skip=len(rows))))
            if not isinstance(page.get('data'), list) or type(page.get('has_more')) is not bool:
                raise ValueError('Incomplete Close batch page')
            if page['has_more'] and not page['data']:
                raise ValueError('Empty continued Close batch page')
            for row in page['data']:
                if row['id'] in seen or row.get('organization_id') != ORG:
                    raise ValueError('Duplicate or cross-organization batch row')
                if not start <= timestamp(row['date_created']) < end:
                    raise ValueError('Close creation fence ignored')
                seen.add(row['id'])
            rows.extend(page['data'])
            if not page['has_more']:
                return rows
            if len(rows) >= PARTITION_LIMIT:
                if (end-start).total_seconds() < .001:
                    raise ValueError('Unpartitionable Close batch')
                middle = start + (end-start)/2
                left, right = partition(start, middle), partition(middle, end)
                if {r['id'] for r in left} & {r['id'] for r in right}:
                    raise ValueError('Close batch partition overlap')
                return left + right
    return partition(datetime(1970, 1, 1, tzinfo=timezone.utc), cutoff)


def exact_leads(client, ids):
    query = {'type': 'and', 'queries': [
        {'type': 'object_type', 'object_type': 'lead'},
        {'type': 'or', 'queries': [{'type': 'id', 'value': lead_id} for lead_id in ids]}]}
    page = client.request('POST', '/data/search/', {'query': query, '_fields': {'lead': LEAD_FIELDS},
                                                  '_limit': PAGE_SIZE, 'include_counts': True})
    rows = page['data']
    if page.get('cursor') or page['count']['total'] != len(ids) or len(rows) != len(ids):
        raise ValueError('Incomplete exact-lead batch')
    if {row['id'] for row in rows} != set(ids) or any(row['organization_id'] != ORG for row in rows):
        raise ValueError('Exact-lead batch scope mismatch')
    return {row['id']: row for row in rows}


def hydrate_bodies(client, path, rows, cutoff):
    if path not in BODY_FIELDS:
        return
    if path == '/activity/sms/':
        selected = [r for r in rows if r['direction'] == 'inbound']
    elif path == '/activity/email/':
        selected = [r for r in rows if r['direction'] == 'incoming']
    elif path == '/activity/call/':
        selected = [r for r in rows if r['direction'] != 'inbound' and cutoff-timestamp(r['activity_at']) < WINDOW]
    else:
        selected = [r for r in rows if cutoff-timestamp(r['activity_at']) < WINDOW]
    for offset in range(0, len(selected), BATCH_SIZE):
        lookup = {r['id']: r for r in selected[offset:offset+BATCH_SIZE]}
        details = fenced_rows(client, path, {'id__in': ','.join(lookup),
                              '_fields': ACTIVITY_FIELDS[path] + ',' + BODY_FIELDS[path]}, cutoff)
        if len(details) != len(lookup) or {r['id'] for r in details} != set(lookup):
            raise ValueError('Incomplete inbound body hydration')
        for row in details:
            original = lookup[row['id']]
            if any(row.get(key) != value for key, value in original.items()):
                raise ValueError('Activity changed during batch hydration')
            original.update(row)


class SnapshotCache:
    """Every source must be populated explicitly; an absent source is never empty."""
    write_enabled = False
    def __init__(self, leads, sources):
        self.leads, self.sources = leads, sources

    def request(self, method, path, payload=None):
        if method != 'GET' or not path.startswith('/lead/') or not path.endswith('/'):
            raise PermissionError('Batch snapshot cache is read-only')
        return self.leads[path.split('/')[2]]

    def list(self, path, params):
        return self.sources[path][params['lead_id']]


class BatchedEvidenceReader:
    def __init__(self, client, roster, held, cutoff):
        if client.write_enabled:
            raise PermissionError('Batch preview requires a read-only transport')
        self.client, self.roster, self.held, self.cutoff = client, roster, held, cutoff
        self.suppression_reader = SnapshotReader(client)
        self.tasks = None

    def prepare(self):
        self.suppression_reader.refresh_unsubscribed()
        self.tasks = fenced_rows(self.client, '/task/', {'is_complete': 'false', '_fields': TASK_FIELDS}, self.cutoff)
        if any(type(row['is_complete']) is not bool for row in self.tasks):
            raise ValueError('Invalid Close task completion flag')

    def read_batch(self, ids):
        if self.tasks is None:
            raise RuntimeError('Batch reader must be prepared before reading')
        if len(ids) > BATCH_SIZE or len(set(ids)) != len(ids):
            raise ValueError('Invalid bounded lead batch')
        output = {lead_id: Evidence(lead_id, '', ()) for lead_id in ids if held_hash(lead_id) in self.held}
        wanted = [lead_id for lead_id in ids if lead_id not in output]
        if not wanted:
            return output
        leads = exact_leads(self.client, wanted)
        for lead_id, lead in leads.items():
            raw = lead.get('custom.' + OWNER)
            owners = tuple(raw) if isinstance(raw, list) else (raw,) if raw else ()
            if set(owners) & self.roster.closer_ids:
                output[lead_id] = Evidence(lead_id, lead['status_id'], owners)
        active = [lead_id for lead_id in wanted if lead_id not in output]
        if not active:
            return output
        sources = {}
        for path, fields in ACTIVITY_FIELDS.items():
            rows = fenced_rows(self.client, path, {'lead_id': ','.join(active), '_fields': fields}, self.cutoff)
            if any(row['lead_id'] not in active for row in rows):
                raise ValueError('Close batch lead filter ignored')
            hydrate_bodies(self.client, path, rows, self.cutoff)
            sources[path] = {lead_id: [] for lead_id in active}
            for row in rows:
                sources[path][row['lead_id']].append(row)
        sources['/task/'] = {lead_id: [] for lead_id in active}
        for row in self.tasks:
            if row.get('lead_id') in sources['/task/']:
                sources['/task/'][row['lead_id']].append(row)
        reader = SnapshotReader(SnapshotCache(leads, sources), self.suppression_reader.unsubscribed)
        reader.blocked_phones = self.suppression_reader.blocked_phones
        for lead_id in active:
            output[lead_id] = reader.read(lead_id, self.cutoff, held=self.held, roster=self.roster)
        return output
