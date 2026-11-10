"""Prepare a private QA launch cohort; optionally add the approved Lost reason.

Default operations are GET/read-only search. The sole mutation option appends
`Need to think` to the existing shared Lost Reason dropdown, preserving choices.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time

import requests

from qa_lifecycle_policy import LOST_REASON, ORGANIZATION_ID, POLICY_VERSION, qa_field_time

QA_VIEW = 'save_im68DOFza11Qjg23wGV93WdyoyUdmirUeckrrwH6Yh1'


def load_env_file(path):
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class SetupClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.auth = (os.environ['CLOSE_API_KEY'], '')

    def read(self, path, body=None):
        # These are GETs or the read-only native search POST. Mutations below
        # retain their single-attempt behavior, including uncertain PUTs.
        for attempt in range(4):
            response = self.session.request('POST' if body is not None else 'GET',
                'https://api.close.com/api/v1' + path, json=body, timeout=45)
            if (response.status_code == 429 or response.status_code >= 500) and attempt < 3:
                try:
                    delay = float(response.headers.get('Retry-After', 2 ** (attempt + 1)))
                except ValueError:
                    delay = 2 ** (attempt + 1)
                time.sleep(min(15, max(1, delay)))
                continue
            response.raise_for_status()
            return response.json()

    def append_need_to_think(self):
        path = f'/custom_field/shared/{LOST_REASON}/'
        before = self.read(path)
        if (before.get('organization_id') != ORGANIZATION_ID or before.get('id') != LOST_REASON
                or before.get('type') != 'choices' or not isinstance(before.get('choices'), list)):
            raise ValueError('lost_reason_schema_scope_mismatch')
        if 'Need to think' in before['choices']:
            return {'choice_added': False, 'already_present': True}
        # Recheck immediately before the single mutation; never replay a timeout.
        fresh = self.read(path)
        if fresh != before:
            raise ValueError('lost_reason_definition_changed_refresh')
        response = self.session.put('https://api.close.com/api/v1' + path,
            json={'choices': [*before['choices'], 'Need to think']}, timeout=45)
        response.raise_for_status()
        after = self.read(path)
        # Close returns the choices sorted; order is not a mutation receipt.
        if (not isinstance(after.get('choices'), list)
                or set(after['choices']) != {*before['choices'], 'Need to think'}
                or len(after['choices']) != len(before['choices']) + 1):
            raise RuntimeError('lost_reason_choice_readback_mismatch')
        return {'choice_added': True, 'existing_choices_preserved': len(before['choices'])}

    def backlog_ids(self):
        view = self.read(f'/saved_search/{QA_VIEW}/')
        if view.get('organization_id') != ORGANIZATION_ID:
            raise ValueError('qa_saved_view_scope_mismatch')
        saved = json.loads(view['s_query']) if isinstance(view['s_query'], str) else view['s_query']
        ids, cursor = [], None
        for _ in range(50):
            body = {'query': saved['query'], '_fields': {'lead': ['id']}, '_limit': 200, 'results_limit': None}
            if cursor:
                body['cursor'] = cursor
            page = self.read('/data/search/', body)
            if not isinstance(page.get('data'), list) or 'cursor' not in page:
                raise RuntimeError('qa_backlog_pagination_unverified')
            ids.extend(row['id'] for row in page['data'])
            next_cursor = page['cursor']
            if not next_cursor:
                if len(set(ids)) != len(ids):
                    raise RuntimeError('qa_backlog_duplicate_identity')
                return sorted(ids)
            if cursor == next_cursor or not page['data']:
                raise RuntimeError('qa_backlog_cursor_stalled')
            cursor = next_cursor
        raise RuntimeError('qa_backlog_cohort_incomplete')

    def prepare(self, output):
        ids = self.backlog_ids()
        if self.backlog_ids() != ids:
            raise RuntimeError('qa_backlog_changed_refresh_capture')
        now = datetime.now(timezone.utc)
        config = {'policy_version': POLICY_VERSION, 'organization_id': ORGANIZATION_ID,
            'enabled': True, 'authorization_reference': 'User-approved QA rules and fresh backlog window, 2026-11-09',
            'calendar': 'entry_day_zero_same_central_time_observed_us_federal',
            'launched_at': qa_field_time(now.isoformat()), 'lost_events_since': (now - timedelta(days=30)).isoformat(),
            'backlog_lead_ids': ids, 'max_writes': 50}
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with output.open('x', encoding='utf8') as file:
            os.chmod(output, 0o600)
            json.dump(config, file, indent=2)
            file.write('\n')
        return {'backlog_count': len(ids), 'captured_at': now.isoformat(),
                'configuration_saved_locally': True, 'production_configuration_changed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file')
    parser.add_argument('--output', help='new private launch configuration file')
    parser.add_argument('--add-need-to-think', action='store_true', help='append only the approved shared dropdown choice')
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    client = SetupClient()
    if args.add_need_to_think:
        print(json.dumps(client.append_need_to_think(), sort_keys=True))
    if args.output:
        print(json.dumps(client.prepare(args.output), sort_keys=True))
    if not args.output and not args.add_need_to_think:
        parser.error('specify a private output or the exact dropdown addition')


if __name__ == '__main__':
    main()
