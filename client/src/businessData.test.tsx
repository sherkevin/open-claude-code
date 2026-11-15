import { describe, expect, it } from 'vitest';
import { dataCell, dataPageCsv, type DataPage } from './BusinessDataPage';

describe('read-only business data presentation',()=>{
  it('shows currency in yuan without changing stored cents',()=>{
    expect(dataCell(12345,{key:'amount_cents',label:'金额（元）',format:'currency_cents'})).toBe('123.45');
    expect(dataCell(0,{key:'quantity',label:'数量'})).toBe('0');
    expect(dataCell(null,{key:'customer',label:'客户'})).toBe('—');
  });
  it('exports only returned rows and prevents spreadsheet formula execution',()=>{
    const page:DataPage={table:{id:'orders',name:'订单',description:'演示订单',scope:'demo',connection_id:'demo',columns:[{key:'customer',label:'客户'},{key:'amount',label:'金额（元）',format:'currency_cents'}]},rows:[{customer:'=HYPERLINK("https://example.test")',amount:10001},{customer:'\t+CMD',amount:0}],total_rows:100000,matched_rows:2,offset:0,limit:50,duration_ms:1};
    const result=dataPageCsv(page);
    expect(result.split('\r\n')).toHaveLength(3);
    expect(result).toContain('"\'=HYPERLINK(""https://example.test"")"');
    expect(result).toContain('"\'\t+CMD"');
    expect(result).toContain('"100.01"');
  });
});
