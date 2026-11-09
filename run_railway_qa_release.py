"""Run the QA lifecycle and existing closer release in the same Railway service."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess

import psycopg
import requests

from close_status_policy import COMPANY, ORG
from qa_lifecycle_runtime import load_configuration, railway_owns_qa, run_configured


@contextmanager
def cohort_fence():
    connection = psycopg.connect(host=os.environ['LAKEBASE_HOST'],
        dbname=os.environ['LAKEBASE_DATABASE'], user=os.environ['LAKEBASE_USER'],
        password=os.environ['LAKEBASE_PASSWORD'], autocommit=True,
        sslmode='verify-full', sslrootcert=os.environ.get('LAKEBASE_SSLROOTCERT') or requests.certs.where(),
        connect_timeout=20)
    key = f'qa-cohort-job|{COMPANY}|{ORG}'
    try:
        locked = connection.execute('select pg_try_advisory_lock(hashtextextended(%s,0))', (key,)).fetchone()[0]
        try:
            yield locked
        finally:
            if locked and connection.execute('select pg_advisory_unlock(hashtextextended(%s,0))', (key,)).fetchone()[0] is not True:
                raise RuntimeError('qa_cohort_unlock_unverified')
    finally:
        connection.close()


def launch_mode(env):
    mode = env.get('CLOSER_RELEASE_MODE')
    if mode not in ('observe', 'apply'):
        raise ValueError('qa_release_mode_invalid')
    config = load_configuration(env)
    release = json.loads(env['CLOSER_RELEASE_CONFIG'])
    delegated = release['policy']['release'].get('closedLostHandledByQaLifecycle') is True
    if not config or not config['enabled'] or not delegated or not railway_owns_qa():
        raise ValueError('qa_coordinated_cutover_required')
    return mode


def main():
    mode = launch_mode(os.environ)
    import lane2_state
    with cohort_fence() as locked:
        if not locked:
            print(json.dumps({'event': 'qa_release_waiting_for_cohort_job', 'verified_writes': 0}))
            return
        run_configured(lane2_state, apply=mode == 'apply')
    # The existing TypeScript job independently reacquires this same fence and
    # retains all original owner-history, census and staging checks.
    subprocess.run(['node', '--import', 'tsx', 'src/release/runCloserReleaseJob.ts'],
                   cwd=Path(__file__).resolve().parent.parent / 'release', check=True)


if __name__ == '__main__':
    main()
