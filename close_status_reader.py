"""Validated Close reads and a separately gated status-only writer."""
from datetime import datetime, timezone
from email.utils import parseaddr
import json
import os
import re
import time
import requests
from close_status_policy import (COMPANY, ORG, OWNER, DNC, Evidence, Opportunity, Roster, timestamp, held_hash)

EXACT_STOP = re.compile(r'^(stop|stopall|unsubscribe|cancel|end|quit|revoke|opt\s*out)[.!\s]*$', re.I)
OPTOUT_REVIEW = re.compile(
    r'\b(stop|stopall|unsubscribe|remove|delete|quit|revoke|wrong|dnc)\b|'
    r'take.{0,15}off|leave me alone|lose my number|do not (contact|call|text|email)|'
    r'don[’\']?t (contact|call|text|email)', re.I)
EMAIL_OPTOUT = re.compile(
    r'^\s*(stop|unsubscribe|remove me)[.!\s]*$|\b(please (remove|unsubscribe)|remove me|'
    r'take me off|stop (emailing|calling|texting|contacting)|do not (contact|email|call|text)|'
    r'don[’\']?t (contact|email|call|text))\b', re.I)
CALLBACK = re.compile(r'\b(call[ -]?back|call me|give me a call|ring me)\b', re.I)


class CloseClient:
    def __init__(self, api_key, *, write_enabled=False, session=None, pause=time.sleep):
        self.session = session or requests.Session()
        self.session.auth = (api_key, '')
        self.write_enabled = write_enabled
        self.pause = pause
        self.requests = []

    def request(self, method, path, payload=None):
        if method not in ('GET', 'POST', 'PUT'):
            raise ValueError('Unsupported Close operation')
        if method == 'POST' and path != '/data/search/':
            raise ValueError('Only read-only search POST permitted')
        if method == 'PUT':
            if not self.write_enabled:
                raise PermissionError('Close writes disabled')
            if not re.fullmatch(r'/(lead|opportunity)/[A-Za-z0-9_]+/', path):
                raise ValueError('Status writer path rejected')
            if not isinstance(payload, dict) or set(payload) != {'status_id'} or not isinstance(payload['status_id'], str):
                raise ValueError('Only one native status_id may be written')
        for attempt in range(6):
            self.pause(0.12 if method != 'POST' else 0.6)
            self.requests.append({'method': method, 'path': path.split('?')[0]})
            try:
                response = self.session.request(method, 'https://api.close.com/api/v1' + path,
                                                json=payload, timeout=45)
            except requests.RequestException:
                if attempt == 5:
                    raise
                # PUT is idempotent but an unknown write outcome must be reconciled, not retried.
                if method == 'PUT':
                    raise
                self.pause(attempt + 1)
                continue
            if response.status_code == 429 and method != 'PUT':
                self.pause(min(float(response.headers.get('Retry-After', 2)), 60))
                continue
            if response.status_code >= 500 and method != 'PUT' and attempt < 5:
                self.pause(attempt + 1)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError('Close read retry limit')

    def list(self, path, params):
        from urllib.parse import urlencode
        rows, seen = [], set()
        while True:
            result = self.request('GET', path + '?' + urlencode(dict(params, _limit=100, _skip=len(rows))))
            if not isinstance(result.get('data'), list) or type(result.get('has_more')) is not bool:
                raise ValueError('Incomplete Close page')
            if result['has_more'] and not result['data']:
                raise ValueError('Empty continued page')
            for row in result['data']:
                if row['id'] in seen:
                    raise ValueError('Duplicate Close activity; live pagination changed')
                seen.add(row['id'])
            rows.extend(result['data'])
            if not result['has_more']:
                return rows


def load_roster(now, path=None):
    if path:
        raw = json.loads(open(path).read())
        if raw['company_id'] != COMPANY or raw['organization_id'] != ORG:
            raise ValueError('Roster tenant mismatch')
        roster = Roster(frozenset(raw['active_closer_ids']), timestamp(raw['captured_at']))
    else:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(host=os.environ['LAKEBASE_HOST'], dbname=os.environ['LAKEBASE_DATABASE'],
                             user=os.environ['LAKEBASE_USER'], password=os.environ['LAKEBASE_PASSWORD'],
                             sslmode='verify-full', sslrootcert=os.environ.get('LAKEBASE_SSLROOTCERT') or requests.certs.where(), connect_timeout=20, row_factory=dict_row,
                             options='-c default_transaction_read_only=on -c statement_timeout=30000') as connection:
            with connection.transaction():
                connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                connection.execute("select set_config('app.company_id', %s, true)", (COMPANY,))
                proof = connection.execute("select current_user as reader_role,current_database() as database_name,current_setting('transaction_read_only') as read_only,transaction_timestamp() as captured_at,txid_current_snapshot() as snapshot").fetchone()
                if proof['read_only'] != 'on':
                    raise ValueError('Roster connection is not read-only')
                rows = connection.execute('''select a.provider_user_id from app.company_users u
                    join app.company_user_provider_accounts a
                      on a.company_id=u.company_id and a.company_user_id=u.id
                    where u.company_id=%s and u.status='active' and u.sales_team_lane=1
                      and a.provider='close' and a.source_domain='crm' ''', (COMPANY,)).fetchall()
                roster = Roster(frozenset(row['provider_user_id'] for row in rows), proof['captured_at'],
                                {key: value.isoformat() if isinstance(value, datetime) else value for key, value in proof.items()})
    validation_now = datetime.now(timezone.utc) if not path else now
    if not roster.valid(validation_now) or any(not isinstance(x, str) or not x.startswith('user_') for x in roster.closer_ids):
        raise ValueError('Fresh authoritative active-closer roster required')
    return roster


class SnapshotReader:
    def __init__(self, client, unsubscribed=()):
        self.client = client
        self.unsubscribed = set(unsubscribed)
        self.blocked_phones = set()

    def refresh_unsubscribed(self):
        # This endpoint returns no record IDs; handle its pagination separately.
        from urllib.parse import urlencode
        addresses, offset = set(), 0
        while True:
            page = self.client.request('GET', '/unsubscribe/email/?' + urlencode({'_limit': 100, '_skip': offset}))
            if not isinstance(page.get('data'), list) or type(page.get('has_more')) is not bool:
                raise ValueError('Unsubscribe snapshot incomplete')
            if page['has_more'] and not page['data']:
                raise ValueError('Unsubscribe snapshot empty and incomplete')
            addresses.update(row['email'].strip().lower() for row in page['data'])
            offset += len(page['data'])
            if not page['has_more']:
                self.unsubscribed = addresses
                self.blocked_phones = {re.sub(r"\D", "", row["phone"]) for row in self.client.list("/blocked_phone_number/", {"_fields": "id,phone"})}
                return

    def read(self, lead_id, now, *, held, roster):
        if not re.fullmatch(r'lead_[A-Za-z0-9]+', lead_id):
            raise ValueError('Malformed lead ID')
        # Explicit exclusions do not require even a Close lead read.
        if held_hash(lead_id) in held:
            return Evidence(lead_id, '', ())
        lead = self.client.request('GET', f'/lead/{lead_id}/')
        if lead['id'] != lead_id or lead['organization_id'] != ORG:
            raise ValueError('Close lead scope mismatch')
        raw_owner = lead.get('custom.' + OWNER)
        if raw_owner is None:
            owners = ()
        elif isinstance(raw_owner, str) and raw_owner.startswith('user_'):
            owners = (raw_owner,)
        elif isinstance(raw_owner, list) and all(isinstance(x, str) and x.startswith('user_') for x in raw_owner):
            owners = tuple(sorted(set(raw_owner)))
        else:
            raise ValueError('Invalid owner field')
        status = lead['status_id']
        if set(owners) & roster.closer_ids:
            return Evidence(lead_id, status, owners)
        contacts = lead['contacts']
        contact_emails = {email['email'].strip().lower() for c in contacts for email in c['emails']
                          if email.get('email') and email['email'].strip()}
        contact_phones = {re.sub(r'\D', '', phone['phone']) for c in contacts for phone in c['phones']
                          if phone.get('phone') and re.sub(r'\D', '', phone['phone'])}
        suppressions, review, latest = [], [], None
        if contact_phones & self.blocked_phones:
            suppressions.append("blocked_phone")
        if any(email.get('is_unsubscribed') or email['email'].strip().lower() in self.unsubscribed
               for c in contacts for email in c['emails']):
            suppressions.append('email_unsubscribed')
        if lead.get('custom.cf_L1HjUOMsqq6sSADgSMn4aH2bma8ohho5enH7jeL1yRJ'):
            review.append('legacy_do_not_contact_field')
        opportunities = []
        for opp in lead['opportunities']:
            if opp['lead_id'] != lead_id or opp['organization_id'] != ORG:
                raise ValueError('Close opportunity scope mismatch')
            opportunities.append(Opportunity(opp['id'], opp['status_id'], timestamp(opp['date_updated']),
                                            opp['status_type'], opp['status_label'], opp.get('user_id'), opp['pipeline_id']))
        def activities(path, fields):
            rows = self.client.list(path, {'lead_id': lead_id, '_fields': fields})
            if any(a['lead_id'] != lead_id or a['organization_id'] != ORG for a in rows):
                raise ValueError('Close activity scope mismatch')
            return rows
        def remember(at):
            nonlocal latest
            if at > now:
                review.append('future_activity_timestamp')
            latest = max(latest, at) if latest else at
        for kind, inbound in (('sms', 'inbound'), ('email', 'incoming')):
            fields = 'id,lead_id,organization_id,direction,status,date_created,activity_at,date_sent'
            fields += ',text,remote_phone' if kind == 'sms' else ',body_text,sender'
            for activity in activities('/activity/' + kind + '/', fields):
                if activity['direction'] != inbound:
                    continue
                body = activity.get('text') if kind == 'sms' else activity.get('body_text')
                if body is None:
                    review.append('missing_inbound_message_body')
                    body = ''
                if kind == 'sms' and EXACT_STOP.fullmatch(body.strip()):
                    suppressions.append('historical_stop')
                elif kind == 'sms' and OPTOUT_REVIEW.search(body):
                    review.append('historical_sms_optout_wording')
                elif kind == 'email' and EMAIL_OPTOUT.search(body):
                    review.append('historical_email_optout_wording')
                at = timestamp(activity.get('date_sent') or activity['activity_at'])
                from_lead = (re.sub(r'\D', '', (activity.get('remote_phone') or '')) in contact_phones
                             if kind == 'sms' else parseaddr((activity.get('sender') or ''))[1].lower() in contact_emails)
                if from_lead:
                    remember(at)
                elif (now - at).total_seconds() < 7 * 86400:
                    review.append('unverified_recent_inbound_sender')
        for call in activities('/activity/call/', 'id,lead_id,organization_id,direction,activity_at,remote_phone,note'):
            at = timestamp(call['activity_at'])
            if call['direction'] == 'inbound':
                if re.sub(r'\D', '', (call.get('remote_phone') or '')) in contact_phones:
                    remember(at)
                elif (now - at).total_seconds() < 7 * 86400:
                    review.append('unverified_recent_inbound_caller')
            elif (now - at).total_seconds() < 7 * 86400 and CALLBACK.search(call.get('note') or ''):
                review.append('outbound_call_callback_note')
        for message in activities('/activity/whatsapp_message/', 'id,lead_id,organization_id,direction,activity_at'):
            if message['direction'] in ('incoming', 'inbound') and (now - timestamp(message['activity_at'])).total_seconds() < 7 * 86400:
                review.append('recent_whatsapp_sender_needs_verification')
        for change in activities('/activity/status_change/lead/',
                                 'id,lead_id,organization_id,old_status_id,new_status_id,old_status_label,new_status_label'):
            if DNC in (change.get('old_status_id'), change.get('new_status_id')) or re.search(
                    r'do not contact|\bdnc\b', str(change.get('old_status_label', '')) + ' ' + str(change.get('new_status_label', '')), re.I):
                suppressions.append('historical_dnc_status')
        future_meeting = any(meeting['status'] != 'canceled' and timestamp(meeting['starts_at']) > now
                             for meeting in activities('/activity/meeting/', 'id,lead_id,organization_id,status,starts_at'))
        for form in activities('/activity/form_submission/', 'id,lead_id,organization_id,activity_at'):
            remember(timestamp(form['activity_at']))
        for note in activities('/activity/note/', 'id,lead_id,organization_id,note,activity_at'):
            if (now - timestamp(note['activity_at'])).total_seconds() < 7 * 86400 and (
                    CALLBACK.search(note.get('note') or '') or '## Qualification completed' in (note.get('note') or '')):
                review.append('recent_callback_or_form_note')
        for task in activities('/task/', 'id,lead_id,organization_id,text,date_created,is_complete'):
            if not task['is_complete'] and CALLBACK.search(task.get('text') or ''):
                review.append('open_callback_task')
        return Evidence(lead_id, status, owners, tuple(opportunities), latest,
                        tuple(suppressions), tuple(review), future_meeting)
