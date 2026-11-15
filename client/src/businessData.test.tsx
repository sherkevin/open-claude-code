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

import { dataColumnType, dataOperators, prepareDataConditions, MAX_DATA_CONDITIONS, type DataColumn } from './BusinessDataPage';
const columns:DataColumn[]=[
  {key:'customer',label:'客户'}, {key:'quantity',label:'数量',type:'integer'},
  {key:'amount_cents',label:'金额（元）',format:'currency_cents'},
  {key:'order_date',label:'订单日期',type:'date'}, {key:'approved',label:'已审核',type:'boolean'},
];
describe('typed multi-condition data filters',()=>{
  it('does not infer arbitrary metadata from names',()=>{
    expect(dataColumnType({key:'order_date',label:'日期'})).toBe('text');
    expect(dataColumnType({key:'amount_cents',label:'金额'})).toBe('text');
    expect(dataOperators(columns[0]).map(o=>o.value)).toContain('contains');
    expect(dataOperators(columns[1]).map(o=>o.value)).not.toContain('contains');
    expect(dataOperators(columns[1]).map(o=>o.value)).toContain('between');
    expect(dataOperators(columns[3]).find(o=>o.value==='gte')?.label).toBe('不早于');
    expect(dataOperators(columns[4]).map(o=>o.value)).toEqual(['eq','ne','is_empty','is_not_empty']);
  });
  it('preserves yuan strings for precise conversion on the server',()=>{
    const result=prepareDataConditions([{field:'amount_cents',operator:'between',value:' 0.29 ',value_to:'1200.50'}],columns);
    expect(result).toEqual([{field:'amount_cents',operator:'between',value:'0.29',value_to:'1200.50'}]);
    expect(()=>prepareDataConditions([{field:'amount_cents',operator:'eq',value:'0.001'}],columns)).toThrow('最多两位小数');
  });
  it('keeps independent field/operator/value rows and removes irrelevant old range values',()=>{
    expect(prepareDataConditions([
      {field:'customer',operator:'contains',value:'客户',value_to:'旧值'},
      {field:'quantity',operator:'gte',value:'50'},
      {field:'order_date',operator:'between',value:'2026-11-01',value_to:'2026-11-14'},
      {field:'approved',operator:'is_empty',value:'false'},
    ],columns)).toEqual([
      {field:'customer',operator:'contains',value:'客户'}, {field:'quantity',operator:'gte',value:'50'},
      {field:'order_date',operator:'between',value:'2026-11-01',value_to:'2026-11-14'}, {field:'approved',operator:'is_empty'},
    ]);
  });
  it.each([
    [{field:'unknown',operator:'eq',value:'1'},'请选择字段'],
    [{field:'customer',operator:'gt',value:'a'},'适用的条件'],
    [{field:'customer',operator:'eq',value:''},'请填写'],
    [{field:'quantity',operator:'gte',value:'1,000'},'数字'],
    [{field:'quantity',operator:'eq',value:'1.1'},'整数'],
    [{field:'quantity',operator:'between',value:'5',value_to:'2'},'起点不能大于终点'],
    [{field:'quantity',operator:'between',value:'5'},'完整范围'],
    [{field:'order_date',operator:'eq',value:'2026-02-30'},'有效日期'],
    [{field:'order_date',operator:'between',value:'2026-11-14',value_to:'2026-11-01'},'起点不能大于终点'],
    [{field:'approved',operator:'eq',value:'1'},'是或否'],
  ])('rejects invalid input with a row-specific message (%j)',(condition,message)=>{
    expect(()=>prepareDataConditions([condition],columns)).toThrow(`第 1 条条件`);
    expect(()=>prepareDataConditions([condition],columns)).toThrow(message);
  });
  it('limits filter rows without silently dropping any',()=>{
    expect(()=>prepareDataConditions(Array.from({length:MAX_DATA_CONDITIONS+1},()=>({field:'customer',operator:'is_empty'})),columns)).toThrow('最多添加');
  });
});
