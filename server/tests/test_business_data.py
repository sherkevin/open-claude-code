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


@pytest.fixture
def typed_browser(browser):
    client, catalog, db, cid = browser
    with sqlite3.connect(db) as conn:
        conn.executescript('ALTER TABLE orders ADD COLUMN amount_cents INTEGER; ALTER TABLE orders ADD COLUMN order_date TEXT; ALTER TABLE orders ADD COLUMN recorded_at TEXT; ALTER TABLE orders ADD COLUMN note TEXT; ALTER TABLE orders ADD COLUMN approved INTEGER;')
        conn.executemany('UPDATE orders SET amount_cents=?,order_date=?,recorded_at=?,note=?,approved=? WHERE id=?', [
            (i * 100 + 10, f'2026-11-{(i-1)//5+1:02d}', f'2026-11-{(i-1)//5+1:02d}T10:15:00Z', None if i % 4 == 0 else '车间_100%', i % 2, i)
            for i in range(1, 121)])
    value = json.loads(catalog.read_text())
    columns = value['tables'][0]['columns']
    columns[0]['type'] = columns[3]['type'] = 'integer'
    columns.extend([
        {'key': 'amount_cents', 'label': '金额（元）', 'format': 'currency_cents'},
        {'key': 'order_date', 'label': '订单日期', 'type': 'date'},
        {'key': 'recorded_at', 'label': '记录时间', 'type': 'datetime'},
        {'key': 'note', 'label': '备注', 'type': 'text'},
        {'key': 'approved', 'label': '已审核', 'type': 'boolean'},
    ])
    catalog.write_text(json.dumps(value))
    return browser


def query_conditions(client, conditions, **kwargs):
    return client.get('/api/v1/business-data/orders/rows', params={'filters': json.dumps(conditions), **kwargs})


def test_multiple_and_conditions_and_legacy_intersection(typed_browser):
    c, _, _, _ = typed_browser
    conditions = [{'field': 'quantity', 'operator': 'gte', 'value': '50'}, {'field': 'customer', 'operator': 'eq', 'value': '客户1'}]
    result = query_conditions(c, conditions, sort='quantity', direction='desc').json()
    assert result['total_rows'] == 110 and result['matched_rows'] == 20
    assert [r['id'] for r in result['rows']] == list(range(109, 51, -3))
    assert result['query']['filters'] == conditions and result['query']['filter_logic'] == 'and'
    assert query_conditions(c, conditions, filter_column='customer', filter_value='客户2').json()['matched_rows'] == 0


def test_user_or_never_bypasses_admin_scope_or_keyword_search(typed_browser):
    c, _, _, _ = typed_browser
    conditions = [{'field': 'quantity', 'operator': 'gte', 'value': '109'}, {'field': 'organization', 'operator': 'eq', 'value': 'B'}]
    result = query_conditions(c, conditions, filter_logic='or').json()
    assert [r['id'] for r in result['rows']] == [109, 110]
    conditions[1] = {'field': 'quantity', 'operator': 'between', 'value': '1', 'value_to': '5'}
    result = query_conditions(c, conditions, filter_logic='or', q='客户2').json()
    assert [r['id'] for r in result['rows']] == [2, 5, 110]


def test_money_is_decimal_yuan_only_in_new_filters(typed_browser):
    c, _, _, _ = typed_browser
    result = query_conditions(c, [{'field': 'amount_cents', 'operator': 'eq', 'value': '1.10'}]).json()
    assert [r['id'] for r in result['rows']] == [1] and result['rows'][0]['amount_cents'] == 110
    result = query_conditions(c, [{'field': 'amount_cents', 'operator': 'between', 'value': '1.10', 'value_to': '3.10'}]).json()
    assert [r['id'] for r in result['rows']] == [1, 2, 3]
    result = c.get('/api/v1/business-data/orders/rows', params={'filter_column': 'amount_cents', 'filter_value': '110'}).json()
    assert [r['id'] for r in result['rows']] == [1]  # Old API remains stored-unit exact matching.
    result = query_conditions(c, [{'field': 'amount_cents', 'operator': 'lt', 'value': '1.10'}]).json()
    assert result['matched_rows'] == 0


def test_date_ranges_datetime_zones_empty_boolean_and_literals(typed_browser):
    c, _, _, _ = typed_browser
    result = query_conditions(c, [{'field': 'order_date', 'operator': 'between', 'value': '2026-11-01', 'value_to': '2026-11-02'}]).json()
    assert [r['id'] for r in result['rows']] == list(range(1, 11))
    result = query_conditions(c, [{'field': 'recorded_at', 'operator': 'eq', 'value': '2026-11-01T18:15:00+08:00'}]).json()
    assert [r['id'] for r in result['rows']] == list(range(1, 6))
    result = query_conditions(c, [{'field': 'note', 'operator': 'is_empty'}, {'field': 'approved', 'operator': 'eq', 'value': 'false'}]).json()
    assert result['matched_rows'] == 27 and all(r['note'] is None for r in result['rows'])
    result = query_conditions(c, [{'field': 'note', 'operator': 'contains', 'value': '_100%'}]).json()
    assert result['matched_rows'] == 83
    assert query_conditions(c, [{'field': 'note', 'operator': 'is_not_empty'}]).json()['matched_rows'] == 83
    assert query_conditions(c, [{'field': 'note', 'operator': 'not_contains', 'value': '_100%'}]).json()['matched_rows'] == 0
    assert query_conditions(c, [{'field': 'customer', 'operator': 'starts_with', 'value': '客户1'}]).json()['matched_rows'] == 37
    assert query_conditions(c, [{'field': 'customer', 'operator': 'contains', 'value': "' OR 1=1 --"}]).json()['matched_rows'] == 0


def test_filtered_pages_have_stable_sort_tiebreaker(typed_browser):
    c, _, _, _ = typed_browser
    conditions = [{'field': 'quantity', 'operator': 'gte', 'value': '1'}]
    a = query_conditions(c, conditions, sort='customer', limit=37).json()
    b = query_conditions(c, conditions, sort='customer', limit=37, offset=37).json()
    assert [r['id'] for r in a['rows']] == [*range(3, 111, 3), 1]
    assert [r['id'] for r in b['rows']] == [*range(4, 110, 3), 2]
    assert not {r['id'] for r in a['rows']} & {r['id'] for r in b['rows']}


@pytest.mark.parametrize('condition', [
    {'field': 'quantity', 'operator': 'contains', 'value': '1'},
    {'field': 'customer', 'operator': 'gt', 'value': 'a'},
    {'field': 'quantity', 'operator': 'eq', 'value': '1.5'},
    {'field': 'quantity', 'operator': 'eq', 'value': 'NaN'},
    {'field': 'quantity', 'operator': 'eq', 'value': '1e100'},
    {'field': 'quantity', 'operator': 'eq', 'value': True},
    {'field': 'quantity', 'operator': 'eq', 'value': '9007199254740992'},
    {'field': 'quantity', 'operator': 'between', 'value': '5', 'value_to': '2'},
    {'field': 'quantity', 'operator': 'between', 'value': '5'},
    {'field': 'amount_cents', 'operator': 'eq', 'value': '0.001'},
    {'field': 'amount_cents', 'operator': 'eq', 'value': '0.010000000000000000000000000000000000000001'},
    {'field': 'order_date', 'operator': 'eq', 'value': '2026-02-30'},
    {'field': 'order_date', 'operator': 'eq', 'value': '20261101'},
    {'field': 'recorded_at', 'operator': 'eq', 'value': '2026-11-01'},
    {'field': 'recorded_at', 'operator': 'eq', 'value': '9999-12-31T23:59:59-23:00'},
    {'field': 'recorded_at', 'operator': 'eq', 'value': '0001-01-01T00:00+23:00'},
    {'field': 'approved', 'operator': 'eq', 'value': '1'},
    {'field': 'customer', 'operator': 'eq', 'value': ''},
    {'field': 'customer', 'operator': 'eq', 'value': {'sql': '1=1'}},
    {'field': 'customer', 'operator': 'eq', 'value': 'a' * 101},
    {'field': 'customer', 'operator': 'eq', 'value': 'a', 'sql': '1=1'},
    {'field': 'id; DROP TABLE orders', 'operator': 'eq', 'value': '1'},
    {'field': 'password', 'operator': 'eq', 'value': '1'},
    {'field': [], 'operator': 'eq', 'value': '1'},
    {'field': 'id', 'operator': ['eq'], 'value': '1'},
    {'field': 'id', 'operator': 'eq OR 1=1', 'value': '1'},
])
def test_reject_malformed_typed_conditions(typed_browser, condition):
    result = query_conditions(typed_browser[0], [condition])
    assert result.status_code == 422 and result.json()['error']['code'] == 'INVALID_DATA_FILTER'
    assert '第 1 条' in result.json()['error']['message']


@pytest.mark.parametrize('filters', ['{bad', '{}', 'null', '[null]', json.dumps([{'field':'id','operator':'is_empty'}] * 13)])
def test_reject_invalid_condition_arrays(typed_browser, filters):
    assert typed_browser[0].get('/api/v1/business-data/orders/rows', params={'filters': filters}).status_code == 422


def test_multi_filter_permissions_and_invalid_logic(typed_browser):
    c, _, _, cid = typed_browser
    filters = [{'field':'quantity','operator':'gte','value':'1'}]
    assert query_conditions(c, filters, filter_logic='xor').status_code == 422
    c.post('/api/v1/auth/login', json={'username':'other','password':PASSWORD})
    assert query_conditions(c, filters, filter_logic='or').status_code == 404
    c.post('/api/v1/auth/login', json={'username':'admin','password':PASSWORD})
    c.patch('/api/v1/connections/' + cid, json={'enabled':False})
    assert query_conditions(c, filters).status_code == 404
