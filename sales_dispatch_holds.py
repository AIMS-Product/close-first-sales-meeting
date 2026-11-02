"""Shared gate for sales tasks, call/SMS dispatch and workflow enrollment/resume."""
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit

from close_status_policy import ORG, DNC, timestamp

COLLECTIONS = ('activity/call', 'activity/sms', 'activity/email', 'sequence_subscription', 'task')


def is_dispatch_mutation(method, url, payload):
    path = urlsplit(url).path.split('/api/v1')[-1]
    if method.upper() == 'POST':
        return path in ('/' + collection + '/' for collection in COLLECTIONS)
    return bool(method.upper() == 'PUT' and re.fullmatch(r'/sequence_subscription/[A-Za-z0-9_]+/', path)
                and payload.get('status') not in ('paused', 'finished'))


def marketing_sequence_verified(sequence, store, now):
    """Email-only is necessary, but explicit reviewed marketing classification is too."""
    if not store or not sequence.get('steps'):
        return False
    if any(step.get('step_type') != 'email' for step in sequence['steps']):
        return False
    row = store.connection.execute("SELECT value FROM workflow_meta WHERE key=?",
        ('marketing_sequence:' + sequence['id'],)).fetchone()
    if not row:
        return False
    policy = json.loads(row['value'])
    steps_hash = hashlib.sha256(json.dumps(sequence['steps'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return bool(policy.get('schemaVersion') == 1 and policy.get('purpose') == 'marketing_only'
        and isinstance(policy.get('reviewedBy'), str) and policy['reviewedBy'].strip()
        and timestamp(policy['reviewedAt']) <= now < timestamp(policy['expiresAt'])
        and policy.get('stepsSha256') == steps_hash)


def guard_dispatch(request, method, url, **kwargs):
    from workflow_holds import BATCH, MARKER, configured_store, custom
    path = urlsplit(url).path.split('/api/v1')[-1]
    base = url.split('/api/v1')[0] + '/api/v1'
    payload = kwargs.get('json') or {}
    read_kwargs = {key: kwargs[key] for key in ('auth', 'headers', 'timeout', 'verify') if key in kwargs}
    store = configured_store()
    try:
        with store.serialized() if store else nullcontext():
            target = request('GET', url, **read_kwargs).json() if method.upper() == 'PUT' else payload
            lead_id = target.get('lead_id')
            contact_id = target.get('contact_id')
            if contact_id:
                contact = request('GET', base + '/contact/' + contact_id + '/', **read_kwargs).json()
                if contact.get('id') != contact_id or (lead_id and contact.get('lead_id') != lead_id):
                    raise ValueError('Dispatch contact/lead mismatch')
                lead_id = contact.get('lead_id')
            if not isinstance(lead_id, str) or not re.fullmatch(r'lead_[A-Za-z0-9]+', lead_id):
                raise ValueError('Dispatch requires a verified Close lead')
            lead = request('GET', base + '/lead/' + lead_id + '/', **read_kwargs).json()
            if lead.get('id') != lead_id or lead.get('organization_id') != ORG:
                raise ValueError('Dispatch lead tenant mismatch')
            if lead.get('status_id') == DNC:
                raise RuntimeError('DNC: dispatch blocked')
            holds = store.holds(lead_id) if store else []
            batch = str(custom(lead, BATCH) or '')
            if holds or 'STH1' in batch:
                markers = list(MARKER.finditer(batch))
                residue = MARKER.sub('', batch)
                sms_only = (bool(holds) and all(row['workflow'] == 'sms_rest' for row in holds)
                    and all(match[1] == 'sms_rest' for match in markers) and 'STH1' not in residue)
                sequence_id = target.get('sequence_id') or payload.get('sequence_id')
                marketing = False
                if sms_only and sequence_id and path.startswith('/sequence_subscription/'):
                    sequence = request('GET', base + '/sequence/' + sequence_id + '/', **read_kwargs).json()
                    if sequence.get('id') != sequence_id or sequence.get('organization_id') != ORG:
                        raise ValueError('Sequence tenant mismatch')
                    marketing = marketing_sequence_verified(sequence, store, datetime.now(timezone.utc))
                if not marketing:
                    raise RuntimeError('Sales hold: dispatch/enrollment excluded')
            if store and store.uncertain(lead_id):
                raise RuntimeError('Uncertain write: reconcile before retry')
            intent = store.intent(lead_id, 'sales_dispatch', {'method': method.upper(), 'path': path, 'patch': payload}) if store else None
            response = request(method, url, **kwargs)
            response.raise_for_status()
            result = response.json()
            result_id = result.get('id')
            if not isinstance(result_id, str) or not re.fullmatch(r'[A-Za-z0-9_]+', result_id):
                raise RuntimeError('Dispatch result unknown; reconcile before retry')
            read_url = url if method.upper() == 'PUT' else url + result_id + '/'
            after = request('GET', read_url, **read_kwargs).json()
            if after.get('id') != result_id or any(after.get(key) != value for key, value in payload.items()):
                raise RuntimeError('Dispatch readback differs; reconcile before retry')
            if store:
                store.receipt(intent)
            return response
    finally:
        if store:
            store.close()
