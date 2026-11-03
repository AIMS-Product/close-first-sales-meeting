"""Require fresh, complete deployment evidence before any held-lead pilot writes."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re

from close_status_policy import COMPANY, ORG, timestamp

SURFACES = ('shared_schema', 'crm_assigners', 'crm_reconcilers', 'crm_sales_tasks_and_enrollment',
    'railway_status_maintenance', 'railway_owner_tracking', 'qa_hold_bridge', 'education_hold_bridge',
    'native_close_workflows', 'dial_queues', 'sales_dispatch', 'manual_calling_controls')


def contract_hash():
    root = Path(__file__).parent
    values = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
              for name in ('workflow_holds.py', 'sales_dispatch_holds.py', 'hold_rollout.py')}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def validate_manifest(manifest, now):
    if not isinstance(manifest, dict):
        raise PermissionError('Shared hold deployment manifest required')
    if (manifest.get('schemaVersion') != 1 or manifest.get('companyId') != COMPANY
            or manifest.get('organizationId') != ORG or manifest.get('contractSha256') != contract_hash()):
        raise PermissionError('Shared hold deployment tenant/contract mismatch')
    try:
        if not isinstance(manifest.get('capturedAt'),str): raise ValueError('Missing capture timestamp')
        age = now - timestamp(manifest['capturedAt'])
        if not timedelta(0) <= age <= timedelta(hours=24):
            raise ValueError('stale')
        for surface in SURFACES:
            row = manifest['surfaces'][surface]
            if not isinstance(row.get('observedAt'),str): raise ValueError('Missing observation timestamp')
            observed_age = now - timestamp(row['observedAt'])
            if (row.get('state') != 'verified' or not timedelta(0) <= observed_age <= timedelta(hours=24)
                    or row.get('contractSha256') != manifest['contractSha256']
                    or not re.fullmatch(r'[a-f0-9]{64}', row.get('evidenceSha256', ''))):
                raise ValueError(surface)
    except (KeyError, TypeError, ValueError) as error:
        raise PermissionError('Shared hold deployment evidence incomplete or stale') from error


def pending_manifest(now):
    return {'schemaVersion': 1, 'companyId': COMPANY, 'organizationId': ORG,
        'contractSha256': contract_hash(), 'capturedAt': now.isoformat(),
        'surfaces': {name: {'state': 'pending', 'observedAt': None, 'contractSha256': None,
                            'evidenceSha256': None} for name in SURFACES}}
