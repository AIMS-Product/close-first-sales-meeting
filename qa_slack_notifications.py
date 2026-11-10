"""Completion alerts from verified QA admissions; durable, single-attempt sends.

Backlog clock resets and transfers out of QA never count as new QA leads.
An ambiguous Slack response is retained for review, never blindly resent.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

from close_status_policy import COMPANY, ORG
from qa_lifecycle_policy import POLICY_VERSION

PREFIX = 'qa-slack-completion-v1|'
QA_LIST_URL = 'https://app.close.com/leads/save_im68DOFza11Qjg23wGV93WdyoyUdmirUeckrrwH6Yh1/'
NARIA_SLACK_ID = 'U0BSCGYRZ2M'
PACIFIC = ZoneInfo('America/Los_Angeles')
MAX_DELIVERIES = 10
DEFINITE_REJECTIONS = frozenset((
    'not_in_channel', 'channel_not_found', 'missing_scope', 'invalid_auth',
    'token_revoked', 'account_inactive', 'is_archived', 'restricted_action',
    'no_permission', 'invalid_arguments', 'msg_too_long', 'ratelimited', 'rate_limited'))


def configuration(env=None):
    env = os.environ if env is None else env
    if env.get('QA_SLACK_NOTIFICATIONS_ENABLED') != '1':
        return None
    channel, token = env.get('QA_SLACK_CHANNEL_ID', ''), env.get('QA_SLACK_BOT_TOKEN', '')
    if not re.fullmatch(r'[CG][A-Z0-9]+', channel) or not token.startswith('xoxb-'):
        raise ValueError('qa_slack_configuration_invalid')
    return {'channel': channel, 'token': token}


def _save(store, key, record):
    store.connection.execute('INSERT INTO workflow_meta(key,value) VALUES(?,?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(record, sort_keys=True)))


def record_completed_run(store, result, *, completed_at, config=None):
    """Persist only completed apply runs with readback-verified QA additions."""
    config = configuration() if config is None else config
    if config is None or result.get('mode') != 'apply':
        return None
    count = result.get('counts', {}).get('verified_qa', 0)
    if (result.get('policy_version') != POLICY_VERSION or type(count) is not int
            or type(result.get('verified_writes')) is not int
            or not 0 <= count <= result['verified_writes']):
        raise ValueError('qa_slack_completion_counts_invalid')
    if not count:
        return None
    if completed_at.tzinfo is None:
        raise ValueError('qa_slack_completion_timezone_required')
    run_id = str(uuid4())
    record = {'run_id': run_id, 'channel': config['channel'], 'count': count,
              'completed_at': completed_at.astimezone(timezone.utc).isoformat(), 'state': 'pending'}
    with store.serialized():
        _save(store, PREFIX + run_id, record)
    return run_id


def message(record):
    count = record['count']
    completed = datetime.fromisoformat(record['completed_at']).astimezone(PACIFIC)
    noun, verb = ('lead', 'was') if count == 1 else ('leads', 'were')
    return (f'<@{NARIA_SLACK_ID}> *QA automation completed*\n'
            f'*{count} new {noun}* {verb} added to your QA list.\n'
            f'<{QA_LIST_URL}|Open your QA list>\n'
            f'Completed {completed:%b %d, %I:%M %p %Z}.')


class SlackRejected(Exception):
    def __init__(self, code, retry_after=60):
        self.code, self.retry_after = code, retry_after
        super().__init__(code)


class SlackUncertain(Exception):
    pass


class SlackSender:
    def __init__(self, config, session=None):
        self.config, self.session = config, session or requests.Session()

    def __call__(self, record):
        response = self.session.post('https://slack.com/api/chat.postMessage',
            headers={'Authorization': 'Bearer ' + self.config['token']},
            json={'channel': record['channel'], 'text': message(record),
                  'unfurl_links': False, 'unfurl_media': False,
                  'metadata': {'event_type': 'qa_leads_released',
                               'event_payload': {'run_id': record['run_id'], 'count': record['count']}}},
            timeout=(5, 20), allow_redirects=False)
        if response.status_code == 429:
            try:
                delay = int(response.headers.get('Retry-After', '60'))
            except (ValueError, TypeError):
                delay = 60
            raise SlackRejected('ratelimited', max(1, min(900, delay)))
        if response.status_code != 200:
            raise SlackUncertain('slack_http_response_uncertain')
        try:
            body = response.json()
        except ValueError as exc:
            raise SlackUncertain('slack_json_response_uncertain') from exc
        if body.get('ok') is False and body.get('error') in DEFINITE_REJECTIONS:
            raise SlackRejected(body['error'])
        if (body.get('ok') is not True or body.get('channel') != record['channel']
                or not re.fullmatch(r'\d+\.\d+', str(body.get('ts', '')))):
            raise SlackUncertain('slack_message_receipt_unverified')
        return {'channel': body['channel'], 'ts': body['ts']}


def deliver_pending(store, config, *, sender=None, now=None):
    """Claim durably before POST. Slack trouble never retries CRM changes."""
    now = now or datetime.now(timezone.utc)
    sender = sender or SlackSender(config)
    rows = store.connection.execute('SELECT key,value FROM workflow_meta WHERE key LIKE ?', (PREFIX + '%',)).fetchall()
    keys = sorted((row['key'] for row in rows), key=lambda key: key)
    counts = {'sent': 0, 'pending': 0, 'uncertain': 0}
    attempted = 0
    for key in keys:
        if attempted >= MAX_DELIVERIES:
            break
        with store.serialized():
            row = store.connection.execute('SELECT value FROM workflow_meta WHERE key=?', (key,)).fetchone()
            record = json.loads(row['value'])
            if record['channel'] != config['channel']:
                continue
            if record['state'] == 'sending':
                # A prior worker may have delivered before losing its receipt.
                if now >= datetime.fromisoformat(record['attempted_at']) + timedelta(minutes=2):
                    record.update(state='uncertain', error='interrupted_delivery_requires_review')
                    _save(store, key, record)
                    counts['uncertain'] += 1
                continue
            if record['state'] != 'pending':
                continue
            if record.get('not_before') and now < datetime.fromisoformat(record['not_before']):
                continue
            record.update(state='sending', attempted_at=now.isoformat())
            _save(store, key, record)
        attempted += 1
        try:
            receipt = sender(record)
            record.update(state='sent', receipt=receipt)
            counts['sent'] += 1
        except SlackRejected as exc:
            record.update(state='pending', error=exc.code,
                          not_before=(now + timedelta(seconds=exc.retry_after)).isoformat())
            counts['pending'] += 1
        except (SlackUncertain, requests.RequestException):
            record.update(state='uncertain', error='delivery_requires_review')
            counts['uncertain'] += 1
        with store.serialized():
            _save(store, key, record)
    return counts


def deliver_configured():
    """An independent notification failure must not fail or replay QA routing."""
    store = None
    try:
        config = configuration()
        if config is None:
            return
        from workflow_holds import configured_store
        store = configured_store()
        if store is None or not hasattr(store, 'raw'):
            raise RuntimeError('qa_slack_shared_postgres_required')
        store.raw.execute("SELECT set_config('app.company_id',%s,false),set_config('app.close_organization_id',%s,false)",
                          (COMPANY, ORG))
        print(json.dumps({'event': 'qa_slack_delivery', **deliver_pending(store, config)}), flush=True)
    except Exception as exc:
        # Do not log HTTP exception text, payloads or environment credentials.
        print(json.dumps({'event': 'qa_slack_delivery_requires_review', 'error_type': type(exc).__name__}), flush=True)
    finally:
        if store is not None:
            store.close()
