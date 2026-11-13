import { afterEach, describe, expect, it, vi } from 'vitest';
import { deleteMemory, loadMemorySessions, memorySessionQuery, readMemoryFile, readMemoryIndex, writeMemoryFile, memoryMutationKey, memoryMutationMessage, persistPendingMemoryMutation, readPendingMemoryMutation, clearPendingMemoryMutation, readMemoryOperation } from './memory';
import type { MemoryIndex } from './types';

const index:MemoryIndex={conversation_id:'session-one',assistant_id:'shared-assistant',scope:'conversation',revision:5,busy:false,files:[],max_file_bytes:131072,max_total_bytes:1048576};
afterEach(()=>vi.unstubAllGlobals());
describe('independent session memory',()=>{
  it('loads both current and archived sessions with an encoded assistant filter and cancellable request',async()=>{
    const sessions=[{id:'one',archived:false},{id:'two',archived:true}];const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify(sessions)));vi.stubGlobal('fetch',fetch);const controller=new AbortController();
    expect(await loadMemorySessions('assistant & archived=false',controller.signal)).toEqual(sessions);
    const [url,request]=fetch.mock.calls[0];const query=new URLSearchParams(String(url).split('?')[1]);expect(query.get('assistant_id')).toBe('assistant & archived=false');expect(query.has('archived')).toBe(false);expect(request.signal).toBe(controller.signal);expect(memorySessionQuery('')).toBe('/conversations');
  });
  it('never reads or writes the assistant-wide path and preserves optimistic revision in the selected session',async()=>{
    const file={path:'topics/quality & status.md',content:'会话一的质量要求',revision:5,updated_at:'2026-11-05T10:00:00Z'};const fetch=vi.fn().mockImplementation(()=>Promise.resolve(new Response(JSON.stringify(file))));vi.stubGlobal('fetch',fetch);
    await readMemoryFile('session-one',file.path);await writeMemoryFile('session-two',{path:file.path,content:'会话二的独立要求',revision:2});
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/conversations/session-one/memory/file?path=topics%2Fquality%20%26%20status.md');
    const [url,request]=fetch.mock.calls[1];expect(url).toBe('/api/v1/conversations/session-two/memory/file');expect(request.method).toBe('PUT');expect(JSON.parse(request.body)).toEqual({path:file.path,content:'会话二的独立要求',revision:2});expect(fetch.mock.calls.every(([target])=>!String(target).includes('/assistants/'))).toBe(true);
  });
  it('rejects cross-session or legacy index results instead of displaying a shared memory workspace',async()=>{
    const fetch=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(index))).mockResolvedValueOnce(new Response(JSON.stringify({...index,conversation_id:'another-session'}))).mockResolvedValueOnce(new Response(JSON.stringify({...index,scope:'legacy_user_assistant'})));vi.stubGlobal('fetch',fetch);
    expect(await readMemoryIndex('session-one')).toEqual(index);await expect(readMemoryIndex('session-one')).rejects.toThrow('不属于所选会话');await expect(readMemoryIndex('session-one')).rejects.toThrow('不属于所选会话');
  });
  it('targets deletion and clearing only at the selected session and forwards the exact revision',async()=>{
    const fetch=vi.fn().mockImplementation(()=>Promise.resolve(new Response(JSON.stringify(index))));vi.stubGlobal('fetch',fetch);
    await deleteMemory('session-one',5,'topics/quality.md');await deleteMemory('session-one',6);
    expect(fetch.mock.calls.map(([url])=>url)).toEqual(['/api/v1/conversations/session-one/memory/file?path=topics%2Fquality.md&revision=5','/api/v1/conversations/session-one/memory?revision=6']);expect(fetch.mock.calls.every(([,request])=>request.method==='DELETE')).toBe(true);
  });
  it('keeps a conflicting draft unchanged and never retries or falls back to shared memory',async()=>{
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({error:{code:'MEMORY_REVISION_CONFLICT',message:'记忆已更新'}}),{status:409}));vi.stubGlobal('fetch',fetch);const draft={path:'MEMORY.md',content:'待保留的草稿',revision:2};
    await expect(writeMemoryFile('session-one',draft)).rejects.toMatchObject({code:'MEMORY_REVISION_CONFLICT',status:409});expect(fetch).toHaveBeenCalledTimes(1);expect(draft).toEqual({path:'MEMORY.md',content:'待保留的草稿',revision:2});
  });
  it('reuses the same operation key after uncertain responses and separates changed payloads',()=>{
    vi.stubGlobal('crypto',undefined);
    const payload={conversation_id:'one',kind:'all',revision:3};const first=memoryMutationKey(null,payload);
    expect(first.id.length).toBeGreaterThan(8);expect(memoryMutationKey(first,{...payload})).toBe(first);
    expect(memoryMutationKey(first,{...payload,conversation_id:'two'}).id).not.toBe(first.id);
    expect(memoryMutationKey(first,{...payload,revision:4}).id).not.toBe(first.id);
    expect(memoryMutationKey(null,payload).id).not.toBe(first.id);
  });
  it('forwards stable operation keys for both edits and explicit empty clears',async()=>{
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify(index)));vi.stubGlobal('fetch',fetch);
    await writeMemoryFile('session-one',{path:'MEMORY.md',content:'draft',revision:5},'stable-write');
    fetch.mockResolvedValue(new Response(JSON.stringify(index)));
    await deleteMemory('session-one',5,undefined,'stable & clear');
    expect(JSON.parse(fetch.mock.calls[0][1].body).operation_id).toBe('stable-write');
    expect(fetch.mock.calls[1][0]).toContain('revision=5&operation_id=stable%20%26%20clear');
  });
  it('only claims context reset when acknowledged by the server',()=>{
    expect(memoryMutationMessage('save',{})).toBe('记忆文件已保存。');
    expect(memoryMutationMessage('all',{context_reset:true})).toContain('不再自动带入旧消息、旧记忆和旧附件');
    expect(memoryMutationMessage('save',{mutation_status:'unchanged'})).toBe('记忆内容未变化。');
  });
  it('restores only recovery metadata across reload, never a draft body',()=>{
    const storage=new Map<string,string>();vi.stubGlobal('sessionStorage',{getItem:(key:string)=>storage.get(key)??null,setItem:(key:string,value:string)=>storage.set(key,value),removeItem:(key:string)=>storage.delete(key)});
    const op={operation_id:'stable-one',conversation_id:'session-one',kind:'save' as const,path:'MEMORY.md',revision:3};
    const withDraft={...op,content:'PRIVATE-DRAFT-MUST-NOT-PERSIST'};
    persistPendingMemoryMutation(withDraft);
    expect([...storage.values()].join('')).not.toContain('PRIVATE-DRAFT');
    expect(readPendingMemoryMutation('session-one')).toEqual(op);expect(readPendingMemoryMutation('session-two')).toBeNull();
    clearPendingMemoryMutation('session-one');expect(readPendingMemoryMutation('session-one')).toBeNull();
  });
  it('refuses to send mutations when the recovery key cannot be persisted',()=>{
    vi.stubGlobal('sessionStorage',{setItem:()=>{throw new Error('storage disabled');}});
    expect(()=>persistPendingMemoryMutation({operation_id:'one',conversation_id:'session-one',kind:'all',path:'',revision:3})).toThrow('本次未发送修改');
  });
  it('queries the exact owner-scoped key without assuming missing means never received',async()=>{
    const response={operation_id:'one & two',status:'not_found',result:null};const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify(response)));vi.stubGlobal('fetch',fetch);
    expect(await readMemoryOperation('session-one',response.operation_id)).toEqual(response);
    expect(fetch.mock.calls[0][0]).toBe('/api/v1/conversations/session-one/memory/operations/one%20%26%20two');
  });
});
