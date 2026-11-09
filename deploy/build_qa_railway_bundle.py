"""Build a private Railway context from a pinned existing release and QA sources.

Only import-reachable local Python modules and two hashed exclusion manifests
are included. Environment files, launch cohorts, CSVs and customer exports are
never part of the source bundle.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = ('run_railway_qa_release', 'run_qa_lifecycle', 'qa_tier_tag_adapter')
DATA = ('close_status_held_leads.json', 'close_status_deferred_leads.json', 'qa_lifecycle_deployment.json')


def source_modules():
    pending, found = list(ENTRIES), set()
    while pending:
        name = pending.pop()
        path = ROOT / (name + '.py')
        if name in found or not path.is_file():
            continue
        found.add(name)
        for node in ast.walk(ast.parse(path.read_text())):
            imports = [node.module] if isinstance(node, ast.ImportFrom) else (
                [item.name for item in node.names] if isinstance(node, ast.Import) else [])
            pending.extend(item.split('.')[0] for item in imports if item)
    return sorted(name + '.py' for name in found)


def build(base, output):
    if output.exists():
        raise ValueError('qa_bundle_output_must_be_new')
    manifest = json.loads((ROOT / 'deploy/qa_railway_base_sha256.json').read_text())
    for relative, digest in manifest.items():
        path = base / relative
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('qa_existing_source_manifest_mismatch')
    output.mkdir(mode=0o700, parents=True)
    for relative in manifest:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base / relative, target)
    # Apply only the reviewed producer delegation change to the pinned sources.
    subprocess.run(['git', 'apply', '--include=release/src/release/recurringRelease.ts',
                    str(ROOT / 'deploy/qa_lost_railway_producer.patch')],
                   cwd=output, check=True)
    support = output / 'qa-lifecycle'
    support.mkdir()
    for relative in [*source_modules(), *DATA]:
        shutil.copyfile(ROOT / relative, support / relative)
    docker = output / 'Dockerfile'
    docker.write_text(docker.read_text() + '\nCOPY qa-lifecycle /app/qa-lifecycle\n')
    railway = output / 'railway.toml'
    text = railway.read_text().replace('startCommand = "node --import tsx src/release/runCloserReleaseJob.ts"',
        'startCommand = "/app/.local-automation-venv/bin/python /app/qa-lifecycle/run_railway_qa_release.py"')
    railway.write_text(text)
    hashes = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(output.rglob('*')) if path.is_file()}
    (output / 'source-manifest.json').write_text(json.dumps(hashes, indent=2) + '\n')
    print(json.dumps({'source_files': len(hashes), 'python_modules': len(source_modules()),
                      'private_cohort_in_bundle': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.base.resolve(), args.output.resolve())
