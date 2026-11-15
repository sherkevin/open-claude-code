"""Deterministic non-production business dataset for offline acceptance."""
from datetime import date, timedelta
from pathlib import Path
import sqlite3

PURPOSE = 'synthetic_acceptance_fixture'

def seed(directory):
    path = Path(directory) / 'business-v2.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS sales_orders (id TEXT PRIMARY KEY NOT NULL, customer TEXT, region TEXT, product TEXT, quantity INTEGER, amount_cents INTEGER, order_date TEXT, status TEXT, owner TEXT, note TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS inventory (id TEXT PRIMARY KEY NOT NULL, warehouse TEXT, product TEXT, stock INTEGER, safety_stock INTEGER, unit_cost_cents INTEGER, updated_on TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS quality_batches (id TEXT PRIMARY KEY NOT NULL, line TEXT, product TEXT, inspection_date TEXT, inspected INTEGER, passed INTEGER, result TEXT)')
        customers=['辰光设备','远川制造','北辰工业','海岳科技','青禾机电','云帆装备','恒川电子','启明自动化']
        products=['控制模块','工业传感器','电源组件','精密轴承','伺服驱动器','连接器']
        regions=['华东','华南','华北','西部']; states=['待确认','处理中','已发货','已完成','已取消']
        orders=[]
        for i in range(1,361):
            quantity=10+(i*17)%490; price=[18000,8400,12600,3500,28800,1600][i%6]
            orders.append((f'SO-2026-{i:04}',customers[(i//3)%8],regions[(i//7)%4],products[i%6],quantity,quantity*price,(date(2026,1,1)+timedelta(days=(i*7)%318)).isoformat(),states[(i//5)%5],['林悦','陈远','周宁','李晨'][i%4],None if i%4 else '交付前核对批次'))
        db.executemany('INSERT OR IGNORE INTO sales_orders VALUES(?,?,?,?,?,?,?,?,?,?)',orders)
        db.executemany('INSERT OR IGNORE INTO inventory VALUES(?,?,?,?,?,?,?)',[(f'SK-{i:03}', ['上海仓','深圳仓','天津仓','成都仓'][i%4],products[(i//4)%6],(i*73)%800,100+(i%3)*50, [18000,8400,12600,3500,28800,1600][(i//4)%6],f'2026-11-{10+i%5:02}') for i in range(1,97)])
        db.executemany('INSERT OR IGNORE INTO quality_batches VALUES(?,?,?,?,?,?,?)',[(f'QC-{i:04}',f'{i%4+1}号线',products[i%6],(date(2026,8,1)+timedelta(days=i%106)).isoformat(),200+i%40,200+i%40-(i%13),'合格' if i%13<8 else '待复核') for i in range(1,241)])
    return path

def catalog(path, connection_id):
    definitions=[('sales_orders','销售订单','订单状态、区域、客户、日期与金额', [('id','订单编号','text'),('customer','客户','text'),('region','区域','text'),('product','产品','text'),('quantity','数量','integer'),('amount_cents','订单金额','money'),('order_date','下单日期','date'),('status','状态','text'),('owner','负责人','text'),('note','备注','text')]),('inventory','库存台账','四个仓库的库存与安全库存', [('id','库存编号','text'),('warehouse','仓库','text'),('product','产品','text'),('stock','在库数量','integer'),('safety_stock','安全库存','integer'),('unit_cost_cents','单位成本','money'),('updated_on','更新日期','date')]),('quality_batches','质量批次','产线、批次与检验结果', [('id','批次编号','text'),('line','产线','text'),('product','产品','text'),('inspection_date','检验日期','date'),('inspected','检验数','integer'),('passed','合格数','integer'),('result','结论','text')])]
    return {'version':1,'tables':[{'id':key,'name':name,'description':description,'scope':'demo','path':str(Path(path).resolve()),'table':key,'primary_key':'id','connection_id':connection_id,'columns':[{'key':field,'label':label,'type':'number' if kind=='money' else kind,**({'format':'currency_cents'} if kind=='money' else {}),'searchable':kind=='text'} for field,label,kind in columns]} for key,name,description,columns in definitions]}

def query_orders(directory, region='', status='', date_from='', date_to='', min_amount=0, limit=50, offset=0):
    if not 1<=limit<=100 or not 0<=offset<=100000 or min_amount<0:
        raise ValueError('Invalid page or amount')
    clauses=[];params=[]
    for key,value in [('region',region),('status',status)]:
        if value:clauses.append(f'{key}=?');params.append(value)
    for comparison,value in [('>=',date_from),('<=',date_to)]:
        if value:date.fromisoformat(value);clauses.append(f'order_date{comparison}?');params.append(value)
    if min_amount:clauses.append('amount_cents>=?');params.append(round(min_amount*100))
    where=' AND '.join(clauses) or '1=1'
    with sqlite3.connect(seed(directory)) as db:
        db.row_factory=sqlite3.Row
        count=db.execute(f'SELECT COUNT(*) FROM sales_orders WHERE {where}',params).fetchone()[0]
        rows=[dict(row) for row in db.execute(f'SELECT * FROM sales_orders WHERE {where} ORDER BY order_date DESC,id LIMIT ? OFFSET ?',params+[limit,offset])]
    return {'rows':rows,'matched_rows':count,'offset':offset,'limit':limit,'dataset_purpose':PURPOSE}
