import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PASSWORD = 'Data-browser-tests-2026'


@pytest.fixture
def browser(tmp_path):
    db = tmp_path / 'orders.sqlite3'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY, organization TEXT, customer TEXT, quantity INTEGER)')
        c.executemany('INSERT INTO orders VALUES(?,?,?,?)', [(i, 'A' if i <= 110 else 'B', f'客户{i % 3}', i) for i in range(1, 121)])
    catalog = tmp_path / 'catalog.json'
    app = create_app(Settings(data_dir=tmp_path / 'app', business_data_catalog=str(catalog), origins=('http://testserver',)))
    with TestClient(app) as client:
        for username, role in [('admin', 'admin'), ('other', 'member')]:
            app.state.service.create_user({'username': username, 'display_name': username, 'password': PASSWORD, 'role': role})
        client.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
        conn = client.post('/api/v1/connections', json={'name': '订单示例', 'url': 'http://127.0.0.1:18877/mcp', 'auth_type': 'none', 'scope': 'private'}).json()
        assert 'id' in conn
        content = {'version': 1, 'tables': [{'id': 'orders', 'name': '销售订单', 'description': '演示订单', 'scope': 'demo', 'path': str(db), 'table': 'orders', 'primary_key': 'id', 'connection_id': conn['id'], 'filters': {'organization': 'A'}, 'columns': [{'key': 'id', 'label': '编号', 'searchable': True}, {'key': 'organization', 'label': '组织'}, {'key': 'customer', 'label': '客户', 'searchable': True}, {'key': 'quantity', 'label': '数量'}]}]}
        catalog.write_text(json.dumps(content))
        yield client, catalog, db, conn['id']


def test_browse_exact_rows_scope_and_stable_pages(browser):
    c, _, db, _ = browser
    catalog = c.get('/api/v1/business-data').json()
    assert len(catalog) == 1 and 'path' not in catalog[0] and 'filters' not in catalog[0]
    a = c.get('/api/v1/business-data/orders/rows').json()
    b = c.get('/api/v1/business-data/orders/rows?offset=50').json()
    assert a['total_rows'] == a['matched_rows'] == 110
    assert [r['id'] for r in a['rows']] == list(range(1, 51))
    assert [r['id'] for r in b['rows']] == list(range(51, 101))
    assert a['model_called'] is False and a['read_only'] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT count(*) FROM orders').fetchone()[0] == 120


def test_filter_search_sort_no_sql_injection(browser):
    c, _, _, _ = browser
    r = c.get('/api/v1/business-data/orders/rows', params={'filter_column': 'customer', 'filter_value': '客户1', 'sort': 'quantity', 'direction': 'desc'}).json()
    assert r['matched_rows'] == 37 and r['rows'][0]['quantity'] == 109
    assert {x['customer'] for x in r['rows']} == {'客户1'}
    for text in ["' OR 1=1 --", '%', '_']:
        r = c.get('/api/v1/business-data/orders/rows', params={'q': text})
        assert r.status_code == 200 and r.json()['matched_rows'] == 0
    assert c.get('/api/v1/business-data/orders/rows', params={'sort': 'id; DELETE FROM orders'}).status_code == 422
    assert c.get('/api/v1/business-data/orders/rows', params={'filter_column': 'password'}).status_code == 422


@pytest.mark.parametrize('params', [{'limit': 101}, {'limit': 0}, {'offset': -1}, {'offset': 1000001}, {'q': 'a' * 101}, {'direction': 'sideways'}])
def test_reject_unbounded_requests(browser, params):
    assert browser[0].get('/api/v1/business-data/orders/rows', params=params).status_code == 422


def test_private_connection_visibility_and_revocation(browser):
    c, _, _, cid = browser
    c.post('/api/v1/auth/login', json={'username': 'other', 'password': PASSWORD})
    assert c.get('/api/v1/business-data').json() == []
    assert c.get('/api/v1/business-data/orders/rows').status_code == 404
    c.post('/api/v1/auth/login', json={'username': 'admin', 'password': PASSWORD})
    c.patch('/api/v1/connections/' + cid, json={'enabled': False})
    assert c.get('/api/v1/business-data').json() == []
    assert c.get('/api/v1/business-data/orders/rows').status_code == 404


def test_auth_and_invalid_catalog_fail_closed(browser):
    c, p, _, _ = browser
    p.write_text('{bad')
    r = c.get('/api/v1/business-data')
    assert r.status_code == 503 and str(p) not in r.text
    c.post('/api/v1/auth/logout')
    assert c.get('/api/v1/business-data').status_code == 401
    assert c.get('/api/v1/business-data/orders/rows').status_code == 401


def test_hidden_view_and_missing_table_fail_closed(browser):
    c, _, db, _ = browser
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE orders')
        conn.execute('CREATE VIEW orders AS SELECT 1 AS id')
    r = c.get('/api/v1/business-data/orders/rows')
    assert r.status_code == 503 and 'SELECT' not in r.text
    assert c.get('/api/v1/business-data/unknown/rows').status_code == 404


def test_schema_drift_cannot_turn_scope_into_literal(browser):
    c, p, _, _ = browser
    config = json.loads(p.read_text())
    config['tables'][0]['columns'][1]['key'] = 'missing_organization'
    config['tables'][0]['filters'] = {'missing_organization': 'missing_organization'}
    p.write_text(json.dumps(config))
    r = c.get('/api/v1/business-data/orders/rows')
    assert r.status_code == 503 and 'rows' not in r.json()


def test_non_unique_paging_key_fails_closed(browser):
    c, p, _, _ = browser
    config = json.loads(p.read_text())
    config['tables'][0]['primary_key'] = 'customer'
    p.write_text(json.dumps(config))
    assert c.get('/api/v1/business-data/orders/rows').status_code == 503


def test_disabled_catalog_empty_and_trial_route_still_closed(tmp_path):
    for mode, expected in [('normal', 200), ('trial', 403)]:
        app = create_app(Settings(data_dir=tmp_path / mode, deployment_mode=mode))
        with TestClient(app) as c:
            app.state.service.create_user({'username':'admin', 'display_name':'管理员', 'password':PASSWORD, 'role':'admin'})
            c.post('/api/v1/auth/login', json={'username':'admin', 'password':PASSWORD})
            r = c.get('/api/v1/business-data')
            assert r.status_code == expected
            if expected == 200:
                assert r.json() == []
