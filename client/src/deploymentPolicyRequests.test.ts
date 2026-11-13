import { afterEach, describe, expect, it, vi } from 'vitest';
import { chatMessagePayload, chatTrialFeatures, loadChatBootstrap, loadChatSkills, sendChatMessage } from './ChatPage';
import { createNewSession, loadNewSessionAssistant, newSessionPayload } from './NewSession';
import type { Assistant, Conversation, ModelSelection } from './types';

const selection:ModelSelection={model_id:'approved-model',reasoning_level:'medium'};
const assistant={id:'assistant',name:'试用助手'} as Assistant;
const conversation={id:'chat-id'} as Conversation;
const jsonResponse=(value:unknown)=>new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});

afterEach(()=>vi.unstubAllGlobals());

describe('deployment policy frontend requests',()=>{
  it('captures trial NewSession assistant loading and creation without closed API requests',async()=>{
    const fetch=vi.fn().mockImplementation((target:string)=>{
      if(target.endsWith('/assistants/assistant'))return Promise.resolve(jsonResponse(assistant));
      if(target.endsWith('/conversations'))return Promise.resolve(jsonResponse(conversation));
      return Promise.reject(new Error(`unexpected request ${target}`));
    });
    vi.stubGlobal('fetch',fetch);

    await loadNewSessionAssistant('assistant');
    await createNewSession(true,'assistant',selection);

    const paths=fetch.mock.calls.map(([target])=>String(target));
    expect(paths).toEqual(['/api/v1/assistants/assistant','/api/v1/conversations']);
    const [,request]=fetch.mock.calls[1];
    expect(request.method).toBe('POST');
    expect(JSON.parse(request.body)).toEqual({
      assistant_id:'assistant',model_id:'approved-model',reasoning_level:'medium',auto_model_routing:false,
    });
    expect(paths.some(path=>path.includes('/skills')||path.includes('/files')||path.includes('/models/')||path.includes('/context'))).toBe(false);
  });

  it('captures trial ChatPage bootstrap and message requests without background closed calls',async()=>{
    const fetch=vi.fn().mockImplementation((target:string,init?:RequestInit)=>{
      if(target.endsWith('/conversations/chat-id/messages')&&!init?.method)return Promise.resolve(jsonResponse([]));
      if(target.endsWith('/conversations/chat-id'))return Promise.resolve(jsonResponse({...conversation,active_run_id:null}));
      if(target.endsWith('/conversations/chat-id/messages'))return Promise.resolve(jsonResponse({message:{id:'message'},run_id:'run'}));
      return Promise.reject(new Error(`unexpected request ${target}`));
    });
    vi.stubGlobal('fetch',fetch);

    const bootstrap=await loadChatBootstrap('chat-id');
    expect(bootstrap.messages).toEqual([]);
    expect(await loadChatSkills(false,'assistant')).toEqual([]);
    await sendChatMessage('chat-id',chatMessagePayload({
      requestId:'request-id',text:'问题',fileIds:[],skillIds:[],trial:true,autoModelRouting:true,
    }));

    const paths=fetch.mock.calls.map(([target])=>String(target));
    expect(paths).toEqual([
      '/api/v1/conversations/chat-id/messages',
      '/api/v1/conversations/chat-id',
      '/api/v1/conversations/chat-id/messages',
    ]);
    const [,request]=fetch.mock.calls[2];
    expect(request.method).toBe('POST');
    expect(JSON.parse(request.body)).toEqual({
      client_request_id:'request-id',text:'问题',file_ids:[],skill_ids:[],auto_route:false,
    });
    expect(Object.values(chatTrialFeatures(true)).every(allowed=>allowed===false)).toBe(true);
  });

  it('preserves normal-mode routing payloads',()=>{
    expect(newSessionPayload(false,'assistant',selection).auto_model_routing).toBe(true);
    expect(chatMessagePayload({
      requestId:'normal-request',text:'问题',fileIds:[],skillIds:[],trial:false,autoModelRouting:true,
    }).auto_route).toBe(true);
    expect(Object.values(chatTrialFeatures(false)).every(allowed=>allowed===true)).toBe(true);
  });

  it('enables only the explicitly advertised Tab capability in a trial',()=>{
    const config={deployment_mode:'trial',capabilities:['models.complete']} as any;
    const features=chatTrialFeatures(true,config);
    expect(features.canUseCompletion).toBe(true);
    expect(features.canUseAttachments).toBe(false);
    expect(features.canUseSkills).toBe(false);
    expect(features.canUseContext).toBe(false);
    expect(features.canRecommend).toBe(false);
  });
});
