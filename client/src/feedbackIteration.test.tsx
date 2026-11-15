import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { groupConversations } from './conversationProjects';
import { queueCanAdvance, type QueueState } from './MessageQueue';
import Brand from './Brand';
import type { Conversation } from './types';

describe('feedback iteration contracts',()=>{
 it('groups all conversations once and keeps per-project order',()=>{
  const items=[{id:'1',category:'交付'},{id:'2',category:''},{id:'3',category:'交付'},{id:'4',category:'  销售  '}] as Conversation[];
  expect(groupConversations(items).map(g=>[g.name,g.conversations.map(c=>c.id)])).toEqual([['交付',['1','3']],['',['2']],['销售',['4']]]);
 });
 it('only advances a loaded, idle, writable and unpaused queue',()=>{
  const state={items:[{id:'draft'}],paused:false,archived:false,active_run_id:null,latest_run:null} as QueueState;
  expect(queueCanAdvance(state,true)).toBe(true);
  for(const next of [null,{...state,items:[]},{...state,paused:true},{...state,archived:true},{...state,active_run_id:'running'}])expect(queueCanAdvance(next,true)).toBe(false);
  expect(queueCanAdvance(state,false)).toBe(false);
 });
 it('renders the provided wordmark as an image without an opaque mask',()=>{
  const html=renderToStaticMarkup(<Brand/>);
  expect(html).toContain('flint-wordmark-light.svg');expect(html).toContain('alt="Flint"');expect(html).not.toContain('mask');
 });
});
