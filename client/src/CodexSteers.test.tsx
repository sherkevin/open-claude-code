import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { codexRequestKey, type CodexThread } from './ConversationCodex';
import { CodexSteerReceipts, pendingSteers, rememberSteer, sameSteer, steerableTurn, type SteerReceipt } from './CodexSteers';

const active = { turn_id: 'turn-A', request_id: 'original', can_interrupt: true, interrupt_requested: false };
const live: CodexThread = {
  bound: true, active_turn: active, recovery_required: true, last_turn: null,
  operation: { phase: 'output', request_id: 'original', text: '旧首段' },
};
const baseId = { turn_id: active.turn_id, client_request_id: 'extra-A' };
const view = (state: SteerReceipt['state'], activeTurn = active.turn_id) =>
  renderToStaticMarkup(<CodexSteerReceipts activeTurn={activeTurn} lastTurn="turn-previous" receipts={[{ ...baseId, state }]}
    unconfirmed={[]} submitting={null} readFailed={false}/>);

describe('补充到正在执行的原生轮次', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('allows the real active view where recovery_required is true; requires a live bound target', () => {
    expect(steerableTurn(live)).toBe('turn-A');
    expect(steerableTurn({ ...live, bound: false })).toBeNull();
    expect(steerableTurn({ ...live, active_turn: null })).toBeNull();
    expect(steerableTurn({ ...live, active_turn: { ...active, can_interrupt: false } })).toBeNull();
    expect(steerableTurn({ ...live, active_turn: { ...active, interrupt_requested: true } })).toBeNull();
    const last = { turn_id: 'turn-A', status: 'completed', text_truncated: false };
    expect(steerableTurn({ ...live, last_turn: last })).toBeNull();
    expect(steerableTurn({ ...live, last_turn: { ...last, turn_id: 'previous' } })).toBe('turn-A');
  });

  it('matches the immutable request and target together', () => {
    expect(sameSteer(baseId, { ...baseId })).toBe(true);
    expect(sameSteer(baseId, { ...baseId, turn_id: 'new-target' })).toBe(false);
    expect(sameSteer(baseId, { ...baseId, client_request_id: 'new-input' })).toBe(false);
  });

  it('uses separate owner and conversation keys and writes only identifiers', () => {
    const entries = new Map<string,string>();
    vi.stubGlobal('localStorage', {
      get length() { return entries.size; },
      key: (i: number) => [...entries.keys()][i],
      getItem: (k: string) => entries.get(k),
      setItem: (k: string,v: string) => entries.set(k,v),
    });
    const scope = codexRequestKey('user:A', 'chat');
    rememberSteer(scope, baseId);
    rememberSteer(scope, { ...baseId, client_request_id: 'extra-B' });
    expect(entries.size).toBe(2);
    expect([...entries.values()]).toEqual(['turn-A','turn-A']);
    expect(pendingSteers(scope).map(id=>id.client_request_id)).toEqual(['extra-A','extra-B']);
    expect(pendingSteers(codexRequestKey('user','A:chat'))).toEqual([]);
    expect(pendingSteers(codexRequestKey('user:A','another'))).toEqual([]);
  });

  it('shows server-confirmed dispositions separately from a missing or unreadable receipt', () => {
    expect(view('delivered')).toContain('已送达目标轮次的执行器');
    expect(view('delivered')).toContain('模型是否采用');
    expect(view('delivered')).toContain('当前轮次');
    expect(view('delivered','new-turn')).toContain('所属轮次');
    expect(view('delivered','new-turn')).not.toContain('当前轮次');
    expect(view('closed')).toContain('未送达');
    expect(view('rejected')).toContain('未送达');
    expect(view('uncertain')).toContain('送达结果待核对');
    const missing = (readFailed: boolean) => renderToStaticMarkup(<CodexSteerReceipts
      receipts={[]} unconfirmed={[baseId]} submitting={null} readFailed={readFailed}/>);
    expect(missing(false)).toContain('查不到该次回执，送达未确认');
    expect(missing(true)).toContain('暂时无法读取');
    expect(missing(true)).not.toContain('查不到该次回执');
    expect(missing(false)).not.toContain('未送达');
  });
});
