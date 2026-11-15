import importlib.util
import json
import sqlite3
from pathlib import Path
from test_business_data import browser  # noqa: F401

spec=importlib.util.spec_from_file_location('business_scenario',Path(__file__).resolve().parents[2]/'examples'/'business_scenario.py')
scenario=importlib.util.module_from_spec(spec);spec.loader.exec_module(scenario)

def test_rich_fixture_query_paging_and_parameter_binding(tmp_path):
    path=scenario.seed(tmp_path)
    scenario.seed(tmp_path)
    with sqlite3.connect(path) as db:
        assert [db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('sales_orders','inventory','quality_batches')]==[360,96,240]
    first=scenario.query_orders(tmp_path,region='华东',min_amount=1000,limit=7)
    second=scenario.query_orders(tmp_path,region='华东',min_amount=1000,limit=7,offset=7)
    assert first['matched_rows']>14 and len(first['rows'])==len(second['rows'])==7
    assert not set(r['id'] for r in first['rows'])&set(r['id'] for r in second['rows'])
    assert all(r['region']=='华东' and r['amount_cents']>=100000 for r in first['rows']+second['rows'])
    assert scenario.query_orders(tmp_path,region="' OR 1=1 --")['rows']==[]
    assert first['dataset_purpose']=='synthetic_acceptance_fixture'

def test_scenario_uses_real_business_browser_filter_and_pagination(browser,tmp_path):
    client, catalog_path, _, connection=browser
    # This fixture's tuple is inspected below rather than assuming production permissions.
    original=json.loads(catalog_path.read_text())
    source_id=original['tables'][0]['connection_id']
    catalog_path.write_text(json.dumps(scenario.catalog(scenario.seed(tmp_path/'rich'),source_id),ensure_ascii=False),encoding='utf-8')
    conditions=[{'field':'region','operator':'eq','value':'华东'},{'field':'amount_cents','operator':'gte','value':'1000'},{'field':'order_date','operator':'between','value':'2026-01-01','value_to':'2026-11-15'}]
    params={'filters':json.dumps(conditions),'sort':'order_date','direction':'desc','limit':10}
    a=client.get('/api/v1/business-data/sales_orders/rows',params=params)
    assert a.status_code==200,a.text
    b=client.get('/api/v1/business-data/sales_orders/rows',params={**params,'offset':10}).json()
    a=a.json()
    assert a['matched_rows']==b['matched_rows'] and a['matched_rows']>20
    assert not set(r['id'] for r in a['rows'])&set(r['id'] for r in b['rows'])
    assert all(r['region']=='华东' and r['amount_cents']>=100000 for r in a['rows']+b['rows'])
