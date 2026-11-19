"""Scheduled Close ownership sync. This runner has no workflow or assignment write path."""
import argparse
from collections import Counter
from itertools import islice
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
import time
from urllib.parse import urlencode

import requests

from close_status_policy import ORG, OWNER, load_exclusions
from close_status_reader import CloseClient, load_roster
from lane2_policy import F_OVERRIDE, F_STATE
from owner_sync_replacement import (
    OPPORTUNITY_OWNER_MIRROR, RECOVERY_STATUS, WORKFLOW_ID, replacement_plan,
)
from sync_lead_owner_to_opp import BASE_URL
from tier_assignment import field, regular
from workflow_holds import BATCH, OWNER_SINCE, custom, open_store

WRITE_TOKEN = 'owner_sync_replacement'
WRITE_SPACING_SECONDS = 3


class RateLimited(Exception):
    def __init__(self, response):
        try:
            self.delay = max(0, min(float(response.headers.get('Retry-After', 15)), 60))
        except ValueError:
            self.delay = 15


LEAD_FIELDS = [
    'id', 'organization_id', 'status_id', 'opportunities', 'num_upcoming_meetings',
    *['custom.' + field_id for field_id in (
        OWNER, OWNER_SINCE, OPPORTUNITY_OWNER_MIRROR, F_OVERRIDE, F_STATE, BATCH, RECOVERY_STATUS,
    )],
]


def native_workflow(client):
    try:
        state = client.request('GET', f'/sequence/{WORKFLOW_ID}/')
    except requests.HTTPError as error:
        if (error.response is None or error.response.status_code != 404
                or os.environ.get('CLOSE_OWNER_SYNC_RETIRED_WORKFLOW_ID') != WORKFLOW_ID):
            raise
        organization = client.request('GET', f'/organization/{ORG}/?_fields=id')
        if organization.get('id') != ORG:
            raise ValueError('Close organization identity mismatch during workflow retirement')
        return None
    if state.get('id') != WORKFLOW_ID or state.get('organization_id') != ORG:
        raise ValueError('Native workflow identity mismatch')
    return state


def require_inactive_native_workflow(client):
    state = native_workflow(client)
    if state is not None and (state.get('status') != 'paused'
                              or state['subscription_counts_by_status']['active'] != 0):
        raise RuntimeError('Custom writes require the native workflow paused and drained')


def candidates(client, now):
    related = {
        'type': 'has_related', 'this_object_type': 'lead', 'related_object_type': 'opportunity',
        'related_query': {'type': 'object_type', 'object_type': 'opportunity'},
    }
    ranges = [(date(1970, 1, 1), now.date() + timedelta(days=2))]
    while ranges:
        lower, upper = ranges.pop()
        bounds = regular('date_created', {
            'type': 'moment_range',
            'on_or_after': {'type': 'fixed_local_date', 'value': lower.isoformat(), 'which': 'start'},
            'before': {'type': 'fixed_local_date', 'value': upper.isoformat(), 'which': 'start'},
        })
        payload = {
            'query': {'type': 'and', 'queries': [
                {'type': 'object_type', 'object_type': 'lead'}, field(OWNER, {'type': 'exists'}), related, bounds,
            ]},
            '_fields': {'lead': LEAD_FIELDS}, '_limit': 200, 'include_counts': True,
            'sort': [{'direction': 'desc', 'field': {
                'type': 'regular_field', 'object_type': 'lead', 'field_name': 'date_created',
            }}],
        }
        page = client.request('POST', '/data/search/', payload)
        if page['count']['total'] >= 8000:
            if upper - lower <= timedelta(days=1):
                raise ValueError('Close search partition exceeds its supported result window')
            middle = lower + (upper - lower) // 2
            ranges.extend([(lower, middle), (middle, upper)])
            continue
        cursors = set()
        while True:
            yield from page['data']
            cursor = page['cursor']
            if not cursor:
                break
            if cursor in cursors or not page['data']:
                raise ValueError('Close search returned an invalid continuation')
            cursors.add(cursor)
            page = client.request('POST', '/data/search/', {**payload, 'cursor': cursor})


def project_meeting(lead):
    count = lead.get('num_upcoming_meetings')
    return {**lead, 'future_meeting_index': bool(count) if type(count) is int and count >= 0 else None}


def read_lead(client, lead_id, store):
    lead = project_meeting(client.request(
        'GET', f'/lead/{lead_id}/?' + urlencode({'_fields': ','.join(LEAD_FIELDS)}),
    ))
    if lead.get('id') != lead_id or lead.get('organization_id') != ORG:
        raise ValueError('Close lead identity mismatch')
    return {**lead, 'durable_hold': bool(store.holds(lead_id)),
            'uncertain_write': bool(store.uncertain(lead_id))}


def generation(lead):
    return custom(lead, OWNER), custom(lead, OWNER_SINCE)


def reconcile(client, store, lead_id, closer_ids, exclusions, write, *, budget):
    with store.serialized():
        current = read_lead(client, lead_id, store)
        if write is None:
            reason, opps, mirror = replacement_plan(current, closer_ids, exclusions)
            return reason, len(opps) + int(mirror is not None), 0
        initial_generation = generation(current)
        reason, opps, mirror = replacement_plan(current, closer_ids, exclusions)
        planned = len(opps) + int(mirror is not None)
        if not planned or planned > budget:
            return reason, planned, 0
        # Each fresh plan must confirm the same owner generation and protections.
        # Any uncertainty stops this run and leaves the normal durable write intent.
        written = 0
        while opps or mirror:
            if written >= planned:
                raise RuntimeError('Live plan expanded during ownership sync')
            require_inactive_native_workflow(client)
            if opps:
                target, owner = next(iter(opps.items()))
                path, patch = f'/opportunity/{target}/', {'user_id': owner}
                before = client.request('GET', path)
                expected = next(row for row in current['opportunities'] if row['id'] == target)
                keys = ('id', 'lead_id', 'organization_id', 'status_id', 'status_type', 'user_id')
                if any(key not in before or before[key] != expected[key] for key in keys):
                    return 'opportunity_changed_during_sync', planned, written
            else:
                path, patch = f'/lead/{lead_id}/', {'custom.' + OPPORTUNITY_OWNER_MIRROR: mirror}
            latest = read_lead(client, lead_id, store)
            if latest != current:
                return 'lead_snapshot_changed_during_sync', planned, written
            receipt = store.intent(lead_id, WRITE_TOKEN, {'path': path, 'patch': patch})
            try:
                write(path, patch)
            except RateLimited as rejected:
                row = store.connection.execute(
                    "UPDATE workflow_write SET state=? WHERE id=? AND token=? AND state=? RETURNING id",
                    ('rate_limited', receipt, WRITE_TOKEN, 'uncertain'),
                ).fetchone()
                if not row or row['id'] != receipt:
                    raise RuntimeError('Could not record the provider rejection')
                rejected.written = written
                rejected.planned = planned
                raise
            verified = client.request('GET', path)
            if verified.get('id') != path.split('/')[2] or verified.get('organization_id') != ORG:
                raise RuntimeError('Write readback identity mismatch')
            if opps:
                match = verified.get('lead_id') == lead_id and verified.get('user_id') == owner
            else:
                match = custom(verified, OPPORTUNITY_OWNER_MIRROR) in (mirror, [mirror])
            if not match:
                raise RuntimeError('Owner write not independently verified')
            store.receipt(receipt)
            written += 1
            current = read_lead(client, lead_id, store)
            if generation(current) != initial_generation:
                return 'lead_owner_changed_during_sync', planned, written
            next_reason, opps, mirror = replacement_plan(current, closer_ids, exclusions)
            if not opps and not mirror and next_reason != 'aligned':
                return next_reason, planned, written
        return reason, planned, written


def discover_batch(client, store, roster, exclusions, now, *, limit, max_items, counts):
    # Exhaust the required search pages before mutation. Provider cursors expire
    # while a page of verified writes is in progress.
    held = {row['lead_id'] for row in store.connection.execute(
        "SELECT lead_id FROM sales_hold UNION SELECT lead_id FROM workflow_write WHERE state='uncertain'"
    )}
    selected = []
    for lead in islice(candidates(client, now), limit):
        counts['scanned'] += 1
        if lead['id'] in held:
            counts['shared_or_unconfirmed_write_hold'] += 1
            continue
        _, opps, mirror = replacement_plan(project_meeting(lead), roster.closer_ids, exclusions)
        if opps or mirror:
            selected.append(lead['id'])
            if len(selected) >= max_items:
                break
    return selected


def run(args):
    now = datetime.now(timezone.utc)
    api_key = os.environ['CLOSE_API_KEY']
    client = CloseClient(api_key)
    if os.environ.get('WORKFLOW_HOLD_BACKEND') != 'postgres':
        raise RuntimeError('Production ownership sync requires the shared PostgreSQL store')
    mode = os.environ.get('CLOSE_OWNER_SYNC_MODE')
    write = None
    if not args.dry_run:
        if mode not in ('canary', 'live') or (mode == 'canary' and not args.lead_id):
            raise RuntimeError('Ownership sync has no write authority for this invocation')
        require_inactive_native_workflow(client)
        # We hold the same tenant fence ourselves and persist exact write intents.
        # Use an unwrapped transport only for these two private, hard-coded patch shapes.
        session = requests.Session()
        session.auth = (api_key, '')

        def write(path, patch):
            response = session.put(BASE_URL + path, json=patch, timeout=45)
            if response.status_code == 429:
                raise RateLimited(response)
            response.raise_for_status()
    roster = load_roster(now)
    exclusions = load_exclusions()
    counts = Counter()
    store = open_store()
    try:
        leads = [args.lead_id] if args.lead_id else discover_batch(
            client, store, roster, exclusions, now, limit=args.limit,
            max_items=args.max_writes * 3, counts=counts,
        )
        print(json.dumps({'scanned':counts['scanned'],'candidate_leads':len(leads)}),flush=True)
        for lead_id in leads:
            try:
                reason, planned, written = reconcile(
                    client, store, lead_id, roster.closer_ids, exclusions, write,
                    budget=args.max_writes - counts['verified_writes'],
                )
            except RateLimited as rejected:
                counts.update(proposed_writes=rejected.planned, verified_writes=rejected.written,
                              rate_limited=1)
                # Defer only this lead until a fresh future pass. Wait outside the shared fence.
                time.sleep(max(WRITE_SPACING_SECONDS, rejected.delay))
                continue
            counts.update(proposed_writes=planned, verified_writes=written)
            counts[reason] += 1
            if write and written:
                time.sleep(WRITE_SPACING_SECONDS)
            if counts['verified_writes'] >= args.max_writes:
                break
        native = native_workflow(client)
        print(json.dumps({'dry_run': args.dry_run, 'native_status': native['status'] if native else 'retired',
                          'lead_id': args.lead_id, **counts}, sort_keys=True), flush=True)
        if args.lead_id and write and not counts['verified_writes']:
            raise RuntimeError('Canary made zero verified writes')
    finally:
        store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--lead-id')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--max-writes', type=int, default=100, choices=range(1, 101), metavar='1..100')
    arguments = parser.parse_args()
    if arguments.lead_id and not re.fullmatch(r'lead_[A-Za-z0-9]+', arguments.lead_id):
        parser.error('Invalid Close lead ID')
    if arguments.limit is not None and arguments.limit < 1:
        parser.error('Scan limit must be positive')
    run(arguments)
