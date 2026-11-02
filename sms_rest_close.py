"""Bounded Close adapter and event normalization for SMS rest."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
import json
import re
from urllib.parse import urlencode

from close_status_policy import ORG, OWNER, DNC, Evidence, Opportunity, timestamp, held_hash, load_exclusions
from close_status_reader import CloseClient, SnapshotReader, EXACT_STOP, OPTOUT_REVIEW, EMAIL_OPTOUT
from sms_rest_policy import Snapshot, TERMINAL_SEQUENCE, owners, OVERRIDE
from workflow_holds import BATCH, OWNER_SINCE, custom
from sales_dispatch_holds import marketing_sequence_verified


def event_pages(client, since, until, *, lead_id=None):
    """Use native cursors and a fixed window; caller retains a five-minute overlap."""
    if until - since > timedelta(days=29):
        raise RuntimeError('Event coverage gap exceeds Close retention; reconcile before continuing')
    params = {'date_updated__gte': since.isoformat(), 'date_updated__lte': until.isoformat(), '_limit': 50}
    if lead_id: params['lead_id'] = lead_id
    cursors = set()
    while True:
        page = client.request('GET', '/event/?' + urlencode(params))
        if not isinstance(page.get('data'), list) or 'cursor_next' not in page:
            raise ValueError('Incomplete event page')
        for event in page['data']:
            if event.get('organization_id') != ORG: raise ValueError('Event tenant mismatch')
            if lead_id and event.get('lead_id') != lead_id: raise ValueError('Event lead mismatch')
            yield event
        cursor = page['cursor_next']
        if not cursor: return
        if cursor in cursors: raise ValueError('Event cursor repeated')
        cursors.add(cursor)
        # Close cursors carry the original date boundary; retain nondate filters.
        params = {'_cursor': cursor, '_limit': 50}
        if lead_id: params['lead_id'] = lead_id


def signal_from_event(event, lead, registration_types=()):
    """Recognize recorded facts; never treat an invitation, open, or outbound as a reply."""
    data = event.get('data')
    kind = event.get('object_type')
    if event.get('action') == 'deleted' or not data: return None
    if kind == 'activity.form_submission':
        return timestamp(data['activity_at'])
    if kind == 'activity.custom' and data.get('custom_activity_type_id') in registration_types:
        return timestamp(data['activity_at'])
    phones = {re.sub(r'\D', '', p['phone']) for c in lead['contacts'] for p in c.get('phones', [])}
    emails = {p['email'].strip().lower() for c in lead['contacts'] for p in c.get('emails', [])}
    if kind == 'activity.sms' and data.get('direction') == 'inbound':
        body = data.get('text')
        if body is None: raise ValueError('Inbound SMS body unavailable')
        if OPTOUT_REVIEW.search(body) or EXACT_STOP.fullmatch(body.strip()): return None
        if re.sub(r'\D', '', data.get('remote_phone') or '') in phones:
            return timestamp(data.get('date_sent') or data['activity_at'])
    if kind == 'activity.email' and data.get('direction') == 'incoming':
        body = data.get('body_text')
        if body is None: raise ValueError('Inbound email body unavailable')
        if EMAIL_OPTOUT.search(body): return None
        if parseaddr(data.get('sender') or '')[1].lower() in emails:
            return timestamp(data.get('date_sent') or data['activity_at'])
    if kind == 'activity.call' and data.get('direction') == 'inbound':
        if re.sub(r'\D', '', data.get('remote_phone') or '') in phones:
            return timestamp(data['activity_at'])
    return None


def opted_out(event):
    data = event.get('data') or {}
    previous = event.get('previous_data') or {}
    if event.get('object_type') == 'lead':
        return DNC in (data.get('status_id'), previous.get('status_id'))
    if event.get('object_type') == 'activity.status_change.lead':
        return DNC in (data.get('old_status_id'), data.get('new_status_id'))
    if event.get('object_type') == 'activity.sms' and data.get('direction') == 'inbound':
        return bool(OPTOUT_REVIEW.search(data.get('text') or '') or EXACT_STOP.fullmatch((data.get('text') or '').strip()))
    if event.get('object_type') == 'activity.email' and data.get('direction') == 'incoming':
        return bool(EMAIL_OPTOUT.search(data.get('body_text') or ''))
    return False


class Reader:
    def __init__(self, client, roster, store, *, registration_types=(), sequences=()):
        self.client, self.roster, self.store = client, roster, store
        self.history = SnapshotReader(client)
        self.registration_types = tuple(registration_types)
        self.sequences = {s['id']: s for s in sequences}
        self.exclusions = load_exclusions()

    def refresh_suppression(self):
        self.history.refresh_unsubscribed()

    def _subscriptions(self, lead):
        # Close workflows are contact-scoped. Validate every result rather than
        # accepting a lead filter that could be ignored by the provider.
        rows = {}
        for contact in lead['contacts']:
            subscriptions = self.client.list('/sequence_subscription/', {'contact_id': contact['id']})
            for sub in subscriptions:
                if sub.get('contact_id') != contact['id'] or sub.get('lead_id') not in (None,lead['id']):
                    raise ValueError('Subscription contact/lead scope mismatch')
                if sub['id'] in rows and rows[sub['id']] != sub:
                    raise ValueError('Subscription changed during read')
                rows[sub['id']] = sub
        sales, unknown = [], []
        for sub in rows.values():
            if sub['status'] not in ('active', 'paused'): continue
            sequence = self.sequences.get(sub['sequence_id'])
            if sequence is None:
                unknown.append(sub['id']); continue
            types = {step.get('step_type') for step in sequence.get('steps',[])}
            # SMS/call steps must stop. Email-only marketing can continue.
            if types & {'sms', 'call'}:
                if sub['status'] == 'active': sales.append(sub['id'])
            elif not marketing_sequence_verified(sequence,self.store,datetime.now(timezone.utc)):
                unknown.append(sub['id'])
        return tuple(sales), tuple(unknown)

    def read(self, lead_id, now, *, cycle=None, task_id=None):
        lead = self.client.request('GET', f'/lead/{lead_id}/')
        if lead.get('id') != lead_id or lead.get('organization_id') != ORG:
            raise ValueError('Lead tenant mismatch')
        task = self.client.request('GET', f'/task/{task_id}/') if task_id else None
        if task and (task.get('organization_id') != ORG or task.get('lead_id') != lead_id):
            raise ValueError('Task tenant mismatch')
        holds = tuple(self.store.holds(lead_id))
        if not cycle:
            evidence = self.history.read(lead_id, now, held=self.exclusions, roster=self.roster)
            if held_hash(lead_id) in self.exclusions:
                evidence = replace(evidence, complete=False)
            signal_at, signal_id = evidence.latest_inbound, None
        else:
            # Historical STOP/DNC is sticky. Admission captures full history once;
            # steady state uses the durable baseline plus incremental native events.
            baseline = cycle.get('evidence_baseline')
            if not baseline: raise ValueError('Historical suppression baseline missing')
            after = timestamp(cycle.get('evidence_through') or cycle['admitted_at'])
            coverage = self.store.connection.execute("SELECT value FROM workflow_meta WHERE key='event_through'").fetchone()
            if coverage: after = max(after, timestamp(coverage['value']))
            if now - after > timedelta(days=29):
                raise RuntimeError('Unobserved history gap; reconcile before re-entry')
            suppressions = list(baseline['suppressions'])
            review = []
            signal_at = timestamp(cycle['signal_at']) if cycle.get('signal_at') else None
            signal_id = cycle.get('signal_id')
            for event in event_pages(self.client, after - timedelta(minutes=5), now, lead_id=lead_id):
                if opted_out(event): suppressions.append('stop_dnc_event')
                # Missing bodies on relevant creates/updates make coverage unknown.
                if event.get('object_type') in ('activity.sms','activity.email','activity.form_submission','activity.custom') and event.get('action') != 'deleted' and event.get('data') is None:
                    review.append('event_payload_unavailable'); continue
                signal = signal_from_event(event, lead, self.registration_types)
                if signal and (not signal_at or signal > signal_at):
                    signal_at, signal_id = signal, event['id']
            phones = {re.sub(r'\D','',p['phone']) for c in lead['contacts'] for p in c.get('phones',[])}
            emails = {p['email'].strip().lower() for c in lead['contacts'] for p in c.get('emails',[])}
            if phones & self.history.blocked_phones: suppressions.append('blocked_phone')
            if emails & self.history.unsubscribed: suppressions.append('email_unsubscribed')
            if lead.get('custom.cf_L1HjUOMsqq6sSADgSMn4aH2bma8ohho5enH7jeL1yRJ'):
                suppressions.append('legacy_do_not_contact')
            opps = tuple(Opportunity(o['id'],o['status_id'],timestamp(o['date_updated']),o['status_type'],
                                      o['status_label'],o.get('user_id'),o['pipeline_id']) for o in lead['opportunities'])
            meetings = self.client.list('/activity/meeting/', {'lead_id':lead_id,'_fields':'id,lead_id,organization_id,status,starts_at'})
            if any(m['lead_id'] != lead_id or m['organization_id'] != ORG for m in meetings):
                raise ValueError('Meeting scope mismatch')
            future = any(m['status'] != 'canceled' and timestamp(m['starts_at']) > now for m in meetings)
            evidence = Evidence(lead_id,lead['status_id'],owners(lead),opps,signal_at,
                                tuple(sorted(set(suppressions))),tuple(review),future)
        sales, unknown = self._subscriptions(lead)
        return Snapshot(lead,evidence,task,holds,signal_at,signal_id,sales,unknown,now)


class Writer:
    """No retries for mutations. Read client stays permanently read-only."""
    def __init__(self, read_client, *, enabled=False):
        self.read = read_client
        self.enabled = enabled
        self.session = read_client.session
        self.requests = []

    def put(self, path, payload):
        if not self.enabled: raise PermissionError('SMS rest writes disabled')
        if not re.fullmatch(r'/(lead|task|sequence_subscription)/[A-Za-z0-9_]+/', path):
            raise ValueError('SMS writer path rejected')
        allowed = {'status_id','custom.'+OWNER,'custom.'+BATCH,'custom.cf_Mpz1pnxLOSjscFDAErSDYOV0PSjlWKXSgBN4jWd73sM'}
        if path.startswith('/lead/') and not set(payload) <= allowed: raise ValueError('Unexpected lead fields')
        if path.startswith('/task/') and not set(payload) <= {'assigned_to','date'}: raise ValueError('Unexpected task fields')
        if path.startswith('/sequence_subscription/') and payload != {'status':'paused'}: raise ValueError('Only sales pause permitted')
        self.requests.append({'method':'PUT','path':path,'payload':payload})
        result = self.session.request('PUT','https://api.close.com/api/v1'+path,json=payload,timeout=45)
        result.raise_for_status()
        return result.json()
