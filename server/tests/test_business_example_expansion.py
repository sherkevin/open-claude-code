"""Synthetic package/candidate and actual BusinessData HTTP acceptance; no model."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3

import pytest

from test_business_data import browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('business_example_expansion', ROOT / 'scripts/prepare_business_example_expansion.py')
expansion = importlib.util.module_from_spec(spec); spec.loader.exec_module(expansion)


@pytest.fixture
def package(tmp_path):
    directory = tmp_path / 'package'
    expansion.build_package(ROOT, directory)
    manifest, additions = expansion.validate_package(directory, expansion.sha(directory / 'package-manifest.json'))
    return directory, manifest, additions


def test_only_new_sqlite_created_with_provenance_and_expected_counts(package):
    directory, manifest, additions = package
    assert manifest['business_rows'] == 696
    assert all(t['scope'] == 'demo' and '示例' in t['name'] and t['dataset_metadata']['synthetic'] for t in additions['tables'])
    with sqlite3.connect(directory / 'business-v2.sqlite3') as db:
        assert json.loads(db.execute("SELECT value FROM dataset_metadata WHERE key='represents_real_company_records'").fetchone()[0]) is False
    with pytest.raises(expansion.PreparationError, match='NEW_PACKAGE_DIRECTORY_REQUIRED'):
        expansion.build_package(ROOT, directory)


def test_merge_preserves_original_records_metadata_and_config(package):
    directory, _, additions = package
    old = {'version': 1, 'custom_policy': {'keep': True}, 'tables': [{'id': 'sales_orders', 'path': '/old/real.sqlite3',
           'scope': 'business', 'filters': {'organization': 'real'}, 'connection_id': 'existing'}]}
    before = copy.deepcopy(old)
    merged = expansion.merge_catalog(old, additions, directory / 'business-v2.sqlite3', 'existing')
    assert old == before and merged['tables'][:1] == old['tables']
    assert merged['custom_policy'] == old['custom_policy']
    assert len(merged['tables']) == 4 and all(t['connection_id'] == 'existing' for t in merged['tables'])
    assert all(t['scope'] == 'demo' for t in merged['tables'][1:])


@pytest.mark.parametrize('fault', ['collision', 'scope', 'purpose', 'name'])
def test_append_refuses_existing_id_or_false_provenance(package, fault):
    directory, _, additions = package
    old = {'version': 1, 'tables': []}
    if fault == 'collision': old['tables'] = [{'id': 'example_sales_orders'}]
    if fault == 'scope': additions['tables'][0]['scope'] = 'business'
    if fault == 'purpose': additions['tables'][0]['dataset_metadata']['synthetic'] = False
    if fault == 'name': additions['tables'][0]['name'] = '真实公司订单'
    with pytest.raises(expansion.PreparationError):
        expansion.merge_catalog(old, additions, directory / 'business-v2.sqlite3', 'existing')


def test_package_changed_bytes_fail_before_candidate(package):
    directory, _, _ = package
    expected = expansion.sha(directory / 'package-manifest.json')
    (directory / 'tables.json').write_text('{}', encoding='utf-8')
    with pytest.raises(expansion.PreparationError, match='PACKAGE_FILE_CAS_FAILED'):
        expansion.validate_package(directory, expected)


def test_actual_api_old_data_preserved_and_three_typed_queries(browser, package):
    client, catalog_path, old_db, connection_id = browser
    directory, _, additions = package
    old_catalog = json.loads(catalog_path.read_text())
    old_db_hash = expansion.sha(old_db)
    merged = expansion.merge_catalog(old_catalog, additions, directory / 'business-v2.sqlite3', connection_id)
    catalog_path.write_text(json.dumps(merged, ensure_ascii=False), encoding='utf-8')
    visible = client.get('/api/v1/business-data').json()
    assert len(visible) == 4 and [t['id'] for t in visible][0] == 'orders'
    assert all('path' not in t for t in visible)
    old = client.get('/api/v1/business-data/orders/rows').json()
    assert old['total_rows'] == 110 and expansion.sha(old_db) == old_db_hash
    orders = [
        {'field': 'region', 'operator': 'eq', 'value': '华东'},
        {'field': 'status', 'operator': 'eq', 'value': '处理中'},
        {'field': 'order_date', 'operator': 'between', 'value': '2026-01-01', 'value_to': '2026-11-15'},
        {'field': 'amount_cents', 'operator': 'gte', 'value': '1000'},
    ]
    queries = [
        ('sales_orders', orders, 'order_date', "SELECT id FROM sales_orders WHERE region='华东' AND status='处理中' AND order_date BETWEEN '2026-01-01' AND '2026-11-15' AND amount_cents>=100000 ORDER BY order_date DESC,id"),
        ('inventory', [{'field': 'warehouse', 'operator': 'eq', 'value': '深圳仓'}, {'field': 'stock', 'operator': 'lt', 'value': '200'}], 'stock', "SELECT id FROM inventory WHERE warehouse='深圳仓' AND stock<200 ORDER BY stock DESC,id"),
        ('quality_batches', [{'field': 'result', 'operator': 'eq', 'value': '待复核'}, {'field': 'inspection_date', 'operator': 'gte', 'value': '2026-10-01'}], 'inspection_date', "SELECT id FROM quality_batches WHERE result='待复核' AND inspection_date>='2026-10-01' ORDER BY inspection_date DESC,id"),
    ]
    reports = []
    for table, filters, sort, sql in queries:
        with sqlite3.connect(directory / 'business-v2.sqlite3') as db:
            expected = [r[0] for r in db.execute(sql)]
        assert len(expected) > 3
        pages, ids, offset = [], [], 0
        while offset < len(expected):
            response = client.get('/api/v1/business-data/example_' + table + '/rows', params={
                'filters': json.dumps(filters), 'sort': sort, 'direction': 'desc', 'limit': 3, 'offset': offset})
            assert response.status_code == 200, response.text
            page = response.json()
            assert page['matched_rows'] == len(expected) and page['total_rows'] == expansion.COUNTS[table]
            assert page['read_only'] is True and page['model_called'] is False
            assert page['table']['scope'] == 'demo'
            ids.extend(row['id'] for row in page['rows']); pages.append(page); offset += 3
        assert ids == expected and len(set(ids)) == len(ids)
        reports.append({'table': table, 'total': expansion.COUNTS[table], 'matched': len(expected), 'pages': len(pages),
                        'first_page': pages[0], 'all_page_ids_equal_direct_sql': True})
    # Metadata and data are not mutated by browse/filter/pagination.
    assert expansion.sha(old_db) == old_db_hash
    assert expansion.sha(directory / 'business-v2.sqlite3') == package[1]['files']['business-v2.sqlite3']
    # Same original private permission gates apply to appended tables.
    client.post('/api/v1/auth/login', json={'username': 'other', 'password': 'Data-browser-tests-2026'})
    assert client.get('/api/v1/business-data').json() == []
    assert client.get('/api/v1/business-data/example_sales_orders/rows').status_code == 404
    target = os.getenv('BUSINESS_EXAMPLE_TEST_REPORT')
    if target:
        Path(target).write_text(json.dumps({'status': 'passed', 'model_calls': 0, 'scope': 'local synthetic actual HTTP API',
            'old_database_bytes_preserved': True, 'example_database_bytes_preserved': True,
            'old_catalog_entry_preserved': True, 'original_permission_enforced': True, 'queries': reports}, ensure_ascii=False, indent=2), encoding='utf-8')
