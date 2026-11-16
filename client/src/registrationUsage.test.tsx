import {renderToStaticMarkup} from 'react-dom/server';
import {describe,expect,it} from 'vitest';
import {Login,Registration,registrationPasswordError} from './AuthPages';
import {UsageReport,usageQuery,usageDisplay} from './UsagePage';
import type {AppConfig,UsageSummary} from './types';
const config:AppConfig={brand_name:'开物',upload_max_bytes:1,auth_types:[],model_label:''};
const noop=()=>{};
describe('invited self registration',()=>{
 it('only offers joining when enabled without altering existing login API',()=>{expect(renderToStaticMarkup(<Login config={config} onLogin={noop}/>)).not.toContain('注册并加入');expect(renderToStaticMarkup(<Login config={{...config,registration_enabled:true}} onLogin={noop}/>)).toContain('注册并加入工作空间');});
 it('requires invitation and confirmed password without a role selector',()=>{const html=renderToStaticMarkup(<Registration config={config} onBack={noop} onRegistered={noop}/>);expect(html).toContain('邀请码');expect(html).toContain('确认密码');expect(html).toContain('普通成员');expect(html).not.toContain('<select');expect(html).not.toContain('value="admin"');});
 it('keeps short, all-digit or mismatched passwords from being submitted',()=>{expect(registrationPasswordError('123456789012','123456789012')).toContain('字母');expect(registrationPasswordError('Strong-Password42','other')).toContain('不一致');expect(registrationPasswordError('Strong-Password42','Strong-Password42')).toBe('');});
});
const usage:UsageSummary={total_runs:2,successful_runs:1,usage_records:3,auxiliary_calls:1,model_call_count:null,known_model_calls:2,unknown_call_records:1,input_tokens:null,output_tokens:null,estimated_cost_usd:null,currency:'USD',users:[],scope:'self',timezone:'Asia/Shanghai',known_subtotals:{input_tokens:432,output_tokens:21},field_coverage:{input_tokens:{known_runs:2,unknown_runs:1},output_tokens:{known_runs:2,unknown_runs:1}},by_model:[],by_day:[]};
describe('personal usage and incomplete receipts',()=>{
 it('shows the explicitly reported total without filling unknown input buckets',()=>{const value={...usage,total_input_tokens:100,total_input_coverage:{known_runs:3,unknown_runs:0}};const html=renderToStaticMarkup(<UsageReport usage={value}/>);expect(html).toContain('总输入 Token');expect(html).toContain('>100<');expect(html).toContain('未缓存输入 Token');expect(html).toContain('模型请求尝试');expect(usageDisplay(value,'cache_read_input_tokens')).toBe('未报告');});
 it('forces member requests to self scope and encodes model/date filters',()=>{const f={scope:'all' as const,model_id:'A & B',date_from:'2026-11-01',date_to:'2026-11-15'};const url=usageQuery(f,false);expect(url).toContain('scope=self');expect(url).toContain('model_id=A+%26+B');expect(usageQuery(f,true)).toContain('scope=all');});
 it('shows incomplete and measured partial data without claiming a zero bill',()=>{const html=renderToStaticMarkup(<UsageReport usage={usage}/>);expect(html).toContain('>432<');expect(html).toContain('另有 1 条记录未完整报告');expect(html).toContain('未提供');expect(html).toContain('北京时间');expect(html).not.toContain('$0.0000');expect(html).not.toContain('成员用量');});
 it('keeps full unknown and known partial zero separate',()=>{expect(usageDisplay({...usage,known_subtotals:{input_tokens:0},field_coverage:{input_tokens:{known_runs:0,unknown_runs:3}}},'input_tokens')).toBe('未报告');expect(usageDisplay({...usage,known_subtotals:{input_tokens:0}},'input_tokens')).toBe('0');expect(usageDisplay({...usage,known_subtotals:{estimated_cost_usd:0}},'estimated_cost_usd')).toBe('未提供');});
 it('shows no-record state rather than a made-up usage chart',()=>{const html=renderToStaticMarkup(<UsageReport usage={{...usage,total_runs:0,usage_records:0}}/>);expect(html).toContain('当前范围暂无用量记录');expect(html).not.toContain('用量明细');});
});
