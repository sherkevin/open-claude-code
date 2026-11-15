import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { bindModalLayer } from './components';

// Minimal DOM surface for the production layer manager. Native EventTarget
// preserves listener ordering and stopImmediatePropagation without a browser.
class TestDocument extends EventTarget {
  body = { style: { overflow: 'clip' } };
  activeElement: TestElement | null = null;
}
class TestElement {
  children: TestElement[] = [];
  isConnected = true;
  offsetParent: object | null = {};
  constructor(readonly ownerDocument: TestDocument, readonly button = false) {}
  append(child: TestElement) { this.children.push(child); return child; }
  contains(target: unknown): boolean { return target === this || this.children.some(child => child.contains(target)); }
  querySelectorAll(): TestElement[] { return this.children.flatMap(child => [...(child.button ? [child] : []), ...child.querySelectorAll()]); }
  focus() { this.ownerDocument.activeElement = this; }
  disconnect() { this.isConnected = false; this.children.forEach(child => child.disconnect()); }
}
const element = (node: TestElement) => node as unknown as HTMLElement;
const cleanups: (() => void)[] = [];
function mount(node: TestElement, close = vi.fn()) {
  const cleanup = bindModalLayer(element(node), close);
  cleanups.push(cleanup);
  return cleanup;
}
function key(doc: TestDocument, value: string, shiftKey = false) {
  const event = new Event('keydown', { cancelable: true });
  Object.defineProperties(event, { key: { value }, shiftKey: { value: shiftKey } });
  doc.dispatchEvent(event);
  return event;
}
function fixture() {
  const doc = new TestDocument();
  const page = new TestElement(doc, true); page.focus();
  const history = new TestElement(doc);
  const historyClose = history.append(new TestElement(doc, true));
  const sourceLink = history.append(new TestElement(doc, true));
  return { doc, page, history, historyClose, sourceLink };
}
function sourceIn(history: TestElement) {
  const source = history.append(new TestElement(history.ownerDocument));
  const first = source.append(new TestElement(history.ownerDocument, true));
  const last = source.append(new TestElement(history.ownerDocument, true));
  return { source, first, last };
}
beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal('window', { setTimeout, clearTimeout }); });
afterEach(() => { cleanups.splice(0).reverse().forEach(cleanup => cleanup()); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('nested result and source dialogs', () => {
  it('one Escape closes only the source even when its close immediately unmounts it', () => {
    const { doc, history, sourceLink } = fixture();
    const closeHistory = vi.fn(); mount(history, closeHistory);
    sourceLink.focus();
    const { source } = sourceIn(history);
    let unmountSource: () => void;
    const closeSource = vi.fn(() => unmountSource());
    unmountSource = mount(source, closeSource);
    expect(key(doc, 'Escape').defaultPrevented).toBe(true);
    expect(closeSource).toHaveBeenCalledTimes(1);
    expect(closeHistory).not.toHaveBeenCalled();
    expect(doc.activeElement).toBe(sourceLink);
    key(doc, 'Escape');
    expect(closeHistory).toHaveBeenCalledTimes(1);
  });

  it('keeps the nested source topmost if child effects are registered before parent effects', () => {
    const { doc, history } = fixture();
    const { source, first } = sourceIn(history);
    const closeSource = vi.fn(), closeHistory = vi.fn();
    mount(source, closeSource); mount(history, closeHistory);
    vi.runAllTimers();
    expect(doc.activeElement).toBe(first);
    key(doc, 'Escape');
    expect(closeSource).toHaveBeenCalledOnce();
    expect(closeHistory).not.toHaveBeenCalled();
  });

  it('wraps Tab only within the top source and recovers focus that escaped behind it', () => {
    const { doc, history, historyClose } = fixture(); mount(history);
    const { source, first, last } = sourceIn(history); mount(source);
    last.focus(); expect(key(doc, 'Tab').defaultPrevented).toBe(true);
    expect(doc.activeElement).toBe(first);
    expect(key(doc, 'Tab', true).defaultPrevented).toBe(true);
    expect(doc.activeElement).toBe(last);
    historyClose.focus(); key(doc, 'Tab');
    expect(doc.activeElement).toBe(first);
    historyClose.focus(); key(doc, 'Tab', true);
    expect(doc.activeElement).toBe(last);
  });

  it('retains the shared scroll lock and restores each opener in order', () => {
    const { doc, page, history, sourceLink } = fixture();
    const closeHistory = mount(history); vi.runAllTimers();
    sourceLink.focus();
    const { source } = sourceIn(history); const closeSource = mount(source); vi.runAllTimers();
    expect(doc.body.style.overflow).toBe('hidden');
    closeSource();
    expect(doc.body.style.overflow).toBe('hidden');
    expect(doc.activeElement).toBe(sourceLink);
    closeHistory();
    expect(doc.body.style.overflow).toBe('clip');
    expect(doc.activeElement).toBe(page);
  });

  it('restores the page after route teardown removes parent before child', () => {
    const { doc, page, history, sourceLink } = fixture();
    const closeHistory = mount(history); sourceLink.focus();
    const { source } = sourceIn(history); const closeSource = mount(source);
    history.disconnect(); closeHistory();
    expect(doc.body.style.overflow).toBe('hidden');
    closeSource();
    expect(doc.body.style.overflow).toBe('clip');
    expect(doc.activeElement).toBe(page);
  });

  it('keeps focus in an empty dialog and does not focus it after unmount', () => {
    const { doc, page, history } = fixture();
    const empty = new TestElement(doc); const unmount = mount(empty);
    vi.runAllTimers(); expect(doc.activeElement).toBe(empty);
    expect(key(doc, 'Tab').defaultPrevented).toBe(true);
    unmount(); expect(doc.activeElement).toBe(page);
    const closeHistory = mount(history); closeHistory();
    vi.runAllTimers(); expect(doc.activeElement).toBe(page);
  });

  it('does not accumulate locks or listeners during mount cleanup and remount', () => {
    const { doc, history } = fixture(); const close = vi.fn();
    mount(history, close)(); const finalCleanup = mount(history, close);
    key(doc, 'Escape'); expect(close).toHaveBeenCalledOnce();
    finalCleanup(); finalCleanup();
    expect(doc.body.style.overflow).toBe('clip');
    key(doc, 'Escape'); expect(close).toHaveBeenCalledOnce();
  });
});
