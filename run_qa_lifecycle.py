"""Run only the configured QA lifecycle; defaults to a read-only preview."""
import argparse
import json
import os
from pathlib import Path

from qa_lifecycle_setup import load_env_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--config', type=Path)
    source.add_argument('--from-env', action='store_true')
    parser.add_argument('--env-file', action='append', default=[])
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-writes', type=int)
    parser.add_argument('--max-examined', type=int, default=100)
    parser.add_argument('--phase', choices=('all', 'lost', 'backlog'), default='all')
    args = parser.parse_args()
    if args.max_writes is not None and args.max_writes < 0:
        parser.error('max writes must be nonnegative')
    if not 1 <= args.max_examined <= 100:
        parser.error('max examined must be between 1 and 100')
    for path in args.env_file:
        load_env_file(path)
    config = json.loads(args.config.read_text() if args.config else os.environ['QA_LIFECYCLE_CONFIG'])
    config['max_examined'] = args.max_examined
    if args.phase != 'all':
        if args.apply:
            parser.error('phase selection is for read-only previews only')
        config['observe_phase'] = args.phase
    os.environ['QA_LIFECYCLE_CONFIG'] = json.dumps(config)
    from qa_lifecycle_runtime import run_configured
    import lane2_state
    result = run_configured(lane2_state, apply=args.apply, max_writes=args.max_writes)
    if result.get('verified_writes', 0) and not args.apply:
        raise RuntimeError('observe_mode_write_invariant')


if __name__ == '__main__':
    main()
