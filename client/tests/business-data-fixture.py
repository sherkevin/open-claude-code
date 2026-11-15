"""Local browser fixture: real BusinessData query engine, temporary SQLite only.

The stdlib HTTP shell substitutes framework/auth dependencies. It does not test
FastAPI authentication or a live service; test_business_data.py covers those.
No customer data, credentials, model requests, or installed dependencies.
"""
import importlib.util
import json
import sqlite3
import signal
import sys
import tempfile
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class AppError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


fastapi = types.ModuleType('fastapi')
fastapi.APIRouter = object
fastapi.Depends = lambda *a, **k: None
fastapi.Query = lambda value=None, **k: value
sys.modules['fastapi'] = fastapi
security = types.ModuleType('app.security')
security.AppError = AppError
sys.modules['app'] = types.ModuleType('app')
sys.modules['app.security'] = security
spec = importlib.util.spec_from_file_location('app.business_data', Path(__file__).resolve().parents[2] / 'server/app/business_data.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory(prefix='kaiwu-data-browser-') as directory:
    root = Path(directory)
    db = root / 'orders.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY,organization TEXT,customer TEXT,quantity INTEGER,amount_cents INTEGER,order_date TEXT,note TEXT)')
        connection.executemany('INSERT INTO orders VALUES(?,?,?,?,?,?,?)', [
            (i, 'A' if i <= 110 else 'B', f'客户{i % 3}', i, i * 100 + 10, f'2026-11-{(i-1)//5+1:02d}', None if i % 4 == 0 else '车间_100%')
            for i in range(1, 121)])
    columns = [
        {'key':'id','label':'订单编号','type':'integer','searchable':True}, {'key':'organization','label':'组织','type':'text'},
        {'key':'customer','label':'客户','type':'text','searchable':True}, {'key':'quantity','label':'数量（件）','type':'integer'},
        {'key':'amount_cents','label':'订单金额（元）','format':'currency_cents'}, {'key':'order_date','label':'订单日期','type':'date'}, {'key':'note','label':'备注','type':'text'},
    ]
    catalog = root / 'catalog.json'
    catalog.write_text(json.dumps({'version':1,'tables':[{'id':'orders','name':'销售订单','description':'查询订单明细，核对数量、金额和日期。','scope':'demo','path':str(db),'table':'orders','primary_key':'id','connection_id':'fixture','filters':{'organization':'A'},'columns':columns}]}))
    class Service:
        settings = types.SimpleNamespace(business_data_catalog=str(catalog))
        def require_resource(self, kind, identifier, user):
            if kind != 'connections' or identifier != 'fixture':
                raise AppError(404, 'NOT_FOUND', '无访问权限')
            return {'enabled':True}
    business = module.BusinessData(Service())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            route = urlsplit(self.path)
            status = 200
            try:
                if route.path == '/api/v1/business-data':
                    result = business.tables({'id':'local-fixture'})
                elif route.path == '/api/v1/business-data/orders/rows':
                    query = {k:v[0] for k,v in parse_qs(route.query, keep_blank_values=True).items()}
                    for key in ('offset','limit'):
                        if key in query:
                            query[key] = int(query[key])
                    result = business.rows('orders', {'id':'local-fixture'}, **query)
                else:
                    raise AppError(404, 'NOT_FOUND', '未定义的本地夹具接口')
            except AppError as error:
                status = error.status
                result = {'error':{'code':error.code,'message':error.message}}
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    def stop_fixture(*args):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop_fixture)
    print(json.dumps({'port':server.server_port}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
