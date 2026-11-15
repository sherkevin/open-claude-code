import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ModelRoutingControl, ModelSettingsDisclosure, RunModelIdentity } from './ModelRouting';
import { createNewSession, newSessionPayload } from './NewSession';
import { saveModelRouting, saveModelSelection } from './models';
import { chatMessagePayload } from './ChatPage';
import type { ModelOption, Run } from './types';
const selection={model_id:'dano',reasoning_level:'high'};
const models:ModelOption[]=[{id:'dano',label:'Dano',available:true,default_reasoning_level:'high',reasoning_levels:[{id:'high',label:'high'}]}, {id:'deepseek',label:'Deepseek',available:true,default_reasoning_level:'default',reasoning_levels:[{id:'default',label:'默认'}]}];
const run:Run={id:'run',conversation_id:'chat',status:'running',assistant_message_id:null,error:null,usage:null,approvals:[],created_at:'2026-11-15',completed_at:null,model_id:'deepseek',model:'Deepseek',reasoning_level:'default'};
afterEach(()=>vi.unstubAllGlobals());

describe('explicit model routing mode',()=>{
  it('keeps the manual model and level visible in a native closed disclosure',()=>{
    const html=renderToStaticMarkup(<ModelSettingsDisclosure automatic={false} selection={selection} models={models}><ModelRoutingControl automatic={false} onChange={()=>{}}/></ModelSettingsDisclosure>);
    expect(html).toContain('class="chat-model-settings fw-model-settings"');
    expect(html).toContain('<summary class="fw-model-pill" aria-label="模型设置：Dano · 高">');
    expect(html).toContain('Dano · 高');expect(html).not.toContain('调整模型');
    expect(html).not.toMatch(/<details[^>]*\bopen=/);expect(html).toContain('aria-label="选模方式"');
  });
  it('summarizes automatic selection without pretending the base model will execute',()=>{
    const html=renderToStaticMarkup(<ModelSettingsDisclosure automatic selection={selection} models={models}><span>设置内容</span></ModelSettingsDisclosure>);
    expect(html).toContain('>自动选择</span>');expect(html).not.toContain('Dano');
    const loading=renderToStaticMarkup(<ModelSettingsDisclosure automatic={false} loading selection={selection} models={models}><span>设置内容</span></ModelSettingsDisclosure>);
    expect(loading).toContain('正在读取模型设置');expect(loading).not.toContain('Dano');
  });
  it('labels automatic selection without presenting a fixed Dano selection',()=>{
    const html=renderToStaticMarkup(<ModelRoutingControl automatic onChange={()=>{}}/>);
    expect(html).toContain('value="auto" selected=""');expect(html).toContain('实际使用的模型显示在会话顶部');
    expect(html).not.toContain('Dano');expect(html).toContain('手动固定');
  });
  it('locks mode while pending and states that manual mode cannot auto-switch',()=>{
    const html=renderToStaticMarkup(<ModelRoutingControl automatic={false} disabled onChange={()=>{}}/>);
    expect(html).toContain('value="manual" selected=""');expect(html).toMatch(/<select[^>]*disabled=""/);expect(html).toContain('不自动切换');
  });
  it('preserves explicit automatic choice but never routes a manual or trial creation',async()=>{
    expect(newSessionPayload(false,'a',selection,true).auto_model_routing).toBe(true);
    expect(newSessionPayload(false,'a',selection,false).auto_model_routing).toBe(false);
    expect(newSessionPayload(true,'a',selection,true).auto_model_routing).toBe(false);
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({id:'chat'})));vi.stubGlobal('fetch',fetch);
    await createNewSession(false,'a',selection,false);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({assistant_id:'a',...selection,auto_model_routing:false});
    expect(chatMessagePayload({requestId:'r',text:'问题',fileIds:[],skillIds:[],trial:false,autoModelRouting:false}).auto_route).toBe(false);
  });
  it('changes mode only with PATCH and fixes an unchanged manual model explicitly',async()=>{
    const fetch=vi.fn().mockImplementation(()=>Promise.resolve(new Response(JSON.stringify({id:'chat',...selection,auto_model_routing:false}))));vi.stubGlobal('fetch',fetch);
    await saveModelRouting('chat',false);await saveModelSelection('chat',selection,selection,true);
    expect(fetch.mock.calls.map(([,init])=>init.method)).toEqual(['PATCH','PATCH']);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({auto_model_routing:false});
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({...selection,auto_model_routing:false});
    expect(fetch.mock.calls.every(([url])=>url==='/api/v1/conversations/chat')).toBe(true);
  });
});
describe('run identity is authoritative',()=>{
  it('shows the task receipt model independently of a current configured model',()=>{
    const html=renderToStaticMarkup(<RunModelIdentity run={run} models={models}/>);
    expect(html).toContain('本次任务模型');expect(html).toContain('Deepseek · 默认');expect(html).not.toContain('Dano');
  });
  it('does not invent identity or echo unlisted internal model labels',()=>{
    const unknown=renderToStaticMarkup(<RunModelIdentity run={{...run,model_id:'private-provider-id',model:'internal-secret-label'}} models={models}/>);
    expect(unknown).toContain('模型信息暂不可用');expect(unknown).not.toContain('private-provider-id');expect(unknown).not.toContain('internal-secret-label');
    const pending=renderToStaticMarkup(<RunModelIdentity run={{...run,model_id:undefined}} models={models}/>);
    expect(pending).toContain('模型信息待确认');
    const metadata=renderToStaticMarkup(<RunModelIdentity run={{...run,execution_kind:'product_metadata',model_call_count:0}} models={models}/>);
    expect(metadata).toContain('本次无模型调用');expect(metadata).not.toContain('Deepseek');
  });
});
