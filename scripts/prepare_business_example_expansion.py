"""Build synthetic examples or prepare an append-only private catalog candidate.

No service changes, model requests, account updates, MCP changes or writes to
existing databases. Candidate activation/rollback belongs to the release owner.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess

DATASET = 'business-examples-20261116'
COUNTS = {'sales_orders': 360, 'inventory': 96, 'quality_batches': 240}
NAMES = {'sales_orders': '销售订单示例', 'inventory': '库存台账示例', 'quality_batches': '质量批次示例'}
BASE = Path('/data/dengkw/workspace/huawi-preview-20261115')
SERVICE = 'zhixing-preview-20261115.service'
GENERATOR_SHA = '0c36805175152c78eec8de48e8bf66119473391972ada63213b6334fa76f9e48'


class PreparationError(ValueError):
    pass


def require(value, code):
    if not value:
        raise PreparationError(code)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_new(path, value):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if os.name == 'posix':
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n'); output.flush(); os.fsync(output.fileno())


def ordinary(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts and not any(p.is_symlink() for p in (path, *path.parents)), 'ABSOLUTE_NON_SYMLINK_PATH_REQUIRED')
    return path


def build_package(repo, destination):
    destination = ordinary(destination)
    require(not destination.exists(), 'NEW_PACKAGE_DIRECTORY_REQUIRED')
    source = Path(repo) / 'examples/business_scenario.py'
    # Canonical Git line endings allow the same reviewed generator on Linux.
    digest = hashlib.sha256(source.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    require(digest == GENERATOR_SHA, 'GENERATOR_CHANGED_REVIEW_REQUIRED')
    spec = importlib.util.spec_from_file_location('expansion_scenario', source)
    scenario = importlib.util.module_from_spec(spec); spec.loader.exec_module(scenario)
    destination.mkdir(mode=0o700)
    db_path = scenario.seed(destination)
    provenance = {'dataset_id': DATASET, 'synthetic': True, 'dataset_purpose': scenario.PURPOSE,
                  'source': 'examples/business_scenario.py', 'generator_sha256': digest,
                  'represents_real_company_records': False}
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE dataset_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)')
        db.executemany('INSERT INTO dataset_metadata VALUES (?,?)', [(k, json.dumps(v, ensure_ascii=False)) for k, v in provenance.items()])
    tables = scenario.catalog(db_path, 'BOUND_DURING_PREPARATION')['tables']
    for table in tables:
        table.pop('path')
        table['id'] = 'example_' + table['table']
        table['name'] = NAMES[table['table']]
        table['dataset_metadata'] = copy.deepcopy(provenance)
    write_new(destination / 'tables.json', {'version': 1, 'tables': tables})
    manifest = {**provenance, 'counts': COUNTS, 'business_rows': sum(COUNTS.values()),
                'files': {name: sha(destination / name) for name in ('business-v2.sqlite3', 'tables.json')}}
    write_new(destination / 'package-manifest.json', manifest)
    return manifest


def validate_package(directory, expected_sha):
    directory = ordinary(directory)
    require(sha(directory / 'package-manifest.json') == expected_sha, 'PACKAGE_MANIFEST_CAS_FAILED')
    manifest = read(directory / 'package-manifest.json')
    require(manifest.get('dataset_id') == DATASET and manifest.get('synthetic') is True
            and manifest.get('counts') == COUNTS and manifest.get('represents_real_company_records') is False,
            'SYNTHETIC_PACKAGE_REQUIRED')
    require(set(manifest['files']) == {'business-v2.sqlite3', 'tables.json'}, 'PACKAGE_SCOPE_MISMATCH')
    for name, digest in manifest['files'].items():
        ordinary(directory / name)
        require(sha(directory / name) == digest, 'PACKAGE_FILE_CAS_FAILED')
    path = directory / 'business-v2.sqlite3'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'EXAMPLE_DATABASE_INVALID')
        objects = set(db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        require(objects == {(k, 'table') for k in (*COUNTS, 'dataset_metadata')}, 'UNEXPECTED_SQLITE_OBJECT')
        require({name: db.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0] for name in COUNTS} == COUNTS, 'EXAMPLE_COUNTS_MISMATCH')
        metadata = {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM dataset_metadata')}
        require(metadata.get('synthetic') is True and metadata.get('dataset_id') == DATASET, 'DATABASE_PROVENANCE_MISSING')
    return manifest, read(directory / 'tables.json')


def merge_catalog(old, additions, database_path, connection_id):
    require(old.get('version') == 1 and isinstance(old.get('tables'), list), 'INVALID_OLD_CATALOG')
    old_ids = [t['id'] for t in old['tables']]
    require(len(set(old_ids)) == len(old_ids), 'DUPLICATE_OLD_ID')
    tables = copy.deepcopy(additions['tables'])
    require({t['table'] for t in tables} == set(COUNTS) and len(tables) == 3, 'EXACT_THREE_TABLES_REQUIRED')
    for table in tables:
        require(table['id'] == 'example_' + table['table'] and table['id'] not in old_ids, 'EXAMPLE_ID_COLLISION')
        require(table['scope'] == 'demo' and table['name'] == NAMES[table['table']]
                and table['dataset_metadata'].get('synthetic') is True, 'SYNTHETIC_TABLE_METADATA_REQUIRED')
        table['path'] = str(database_path)
        table['connection_id'] = connection_id
    result = copy.deepcopy(old)
    result['tables'].extend(tables)
    require(len(result['tables']) <= 40, 'CATALOG_TABLE_LIMIT_EXCEEDED')
    return result


def source_state(baseline):
    current = subprocess.run(['systemctl', 'show', SERVICE, '--property=ExecStart', '--value'],
                             check=True, capture_output=True, text=True).stdout
    paths = re.findall(r'--env-file\s+([^\s;]+)', current)
    require(paths == [baseline['env_path']], 'ACTIVE_ENV_PATH_CHANGED')
    env_path = ordinary(baseline['env_path'])
    require(sha(env_path) == baseline['env_sha256'], 'ENV_CAS_FAILED')
    env = read(env_path)
    catalog_path = ordinary(baseline['catalog_path'])
    require(env['AGENT_BUSINESS_DATA_CATALOG'] == str(catalog_path)
            and sha(catalog_path) == baseline['catalog_sha256'], 'CATALOG_CAS_FAILED')
    old = read(catalog_path)
    expected_databases = {t['path']: t['database_sha256'] for t in baseline['tables']}
    require({t['path'] for t in old['tables']} == set(expected_databases), 'OLD_DATABASE_SET_CHANGED')
    for path, digest in expected_databases.items():
        path = ordinary(path)
        require(sha(path) == digest, 'OLD_DATABASE_CAS_FAILED')
        require(not Path(str(path) + '-wal').exists(), 'LIVE_WAL_DATABASE_REQUIRES_REVIEW')
    return env, old, expected_databases


def prepare_candidate(package, package_sha, baseline_path, destination, connection_id, *, prepare=False):
    require(os.name == 'posix', 'LINUX_PRIVATE_PREPARATION_ONLY')
    destination = ordinary(destination)
    require(destination.parent == BASE / '.private/config-releases' and not destination.exists(), 'NEW_PRIVATE_CONFIG_RELEASE_REQUIRED')
    require(destination.parent.is_dir() and destination.parent.stat().st_mode & 0o077 == 0, 'PRIVATE_PARENT_REQUIRED')
    baseline = read(baseline_path)
    manifest, additions = validate_package(package, package_sha)
    env, old, hashes = source_state(baseline)
    require(connection_id in {t['connection_id'] for t in old['tables']}, 'EXISTING_CATALOG_CONNECTION_REQUIRED')
    app_path = Path(env['AGENT_DATA_DIR']) / 'app.sqlite3'
    with sqlite3.connect(app_path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT enabled,deleted,scope FROM connections WHERE id=?', (connection_id,)).fetchone()
        require(row == (1, 0, 'team'), 'EXISTING_ENABLED_TEAM_CONNECTION_REQUIRED')
    new_catalog = merge_catalog(old, additions, destination / 'business-v2.sqlite3', connection_id)
    new_env = copy.deepcopy(env)
    new_env['AGENT_BUSINESS_DATA_CATALOG'] = str(destination / 'business-data-catalog.json')
    require([k for k in env if new_env[k] != env[k]] == ['AGENT_BUSINESS_DATA_CATALOG'], 'ENV_CHANGE_SCOPE_VIOLATION')
    receipt = {'status': 'prepared' if prepare else 'plan', 'dataset_id': DATASET, 'model_calls': 0,
               'source_env': baseline['env_path'], 'source_env_sha256': baseline['env_sha256'],
               'source_catalog': baseline['catalog_path'], 'source_catalog_sha256': baseline['catalog_sha256'],
               'old_database_hashes': hashes, 'old_tables_preserved': len(old['tables']),
               'new_tables': [{'id': t['id'], 'name': t['name'], 'scope': t['scope'], 'rows': COUNTS[t['table']]} for t in new_catalog['tables'][len(old['tables']):]],
               'candidate_env_path': str(destination / 'release-env.json'), 'candidate_catalog_path': new_env['AGENT_BUSINESS_DATA_CATALOG'],
               'catalog_path': new_env['AGENT_BUSINESS_DATA_CATALOG'],
               'package_manifest_sha256': package_sha, 'existing_connections_modified': False,
               'mcp_tools_added': False, 'service_switched': False}
    if prepare:
        source_state(baseline)
        destination.mkdir(mode=0o700)
        shutil.copyfile(Path(package) / 'business-v2.sqlite3', destination / 'business-v2.sqlite3')
        (destination / 'business-v2.sqlite3').chmod(0o600)
        require(sha(destination / 'business-v2.sqlite3') == manifest['files']['business-v2.sqlite3'], 'COPIED_DATABASE_MISMATCH')
        write_new(destination / 'business-data-catalog.json', new_catalog)
        write_new(destination / 'release-env.json', new_env)
        source_state(baseline)  # Every old catalog/env/database byte must still match.
        receipt['candidate_sha256'] = {name: sha(destination / name) for name in ('business-v2.sqlite3', 'business-data-catalog.json', 'release-env.json')}
        receipt['file_hashes'] = {str(destination / name): sha(destination / name)
                                  for name in ('business-data-catalog.json', 'business-v2.sqlite3')}
        write_new(destination / 'preparation-manifest.json', receipt)  # Marker last.
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build'); build.add_argument('--repo', type=Path, required=True); build.add_argument('--destination', type=Path, required=True)
    plan = commands.add_parser('candidate')
    for name in ('package', 'baseline', 'destination'):
        plan.add_argument('--' + name, type=Path, required=True)
    plan.add_argument('--package-sha256', required=True); plan.add_argument('--connection-id', required=True)
    plan.add_argument('--prepare', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'build':
            result = build_package(args.repo, args.destination)
        else:
            result = prepare_candidate(args.package, args.package_sha256, args.baseline, args.destination,
                                       args.connection_id, prepare=args.prepare)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        # Private paths/values from third-party exceptions are never printed.
        print(json.dumps({'status': 'blocked', 'error_type': type(error).__name__,
                          'reason': str(error) if isinstance(error, PreparationError) else 'SOURCE_OR_IO_ERROR',
                          'model_calls': 0}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
