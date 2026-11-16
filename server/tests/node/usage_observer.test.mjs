import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { installUsageObserver } from '../../harness/usage_observer.ts'

const endpoint = 'https://synthetic.invalid/v1/messages'
const sessionId = 'synthetic-session'
const headers = { 'x-deepseek-harness-session-id': sessionId }
const noNetwork = async () => { throw new Error('REAL_NETWORK_FORBIDDEN') }
globalThis.fetch = noNetwork
let observer
afterEach(() => { observer?.restore(); observer = undefined; globalThis.fetch = noNetwork })
const encode = value => new TextEncoder().encode(value)
const frame = value => `event: ${value.type}\ndata: ${JSON.stringify(value)}\n\n`
const start = usage => frame({ type: 'message_start', message: { usage } })
const delta = usage => frame({ type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage })
const stop = () => frame({ type: 'message_stop' })
const response = (text, options = {}) => new Response(text, { headers: { 'content-type': 'text/event-stream' }, ...options })
function bind(mock, onReceipt) {
  globalThis.fetch = mock
  observer = installUsageObserver({ endpoint, sessionId, onReceipt })
  observer.begin('run-1')
  return observer
}
const post = (overrides = {}) => fetch(endpoint, { method: 'POST', headers, ...overrides })
const receipt = () => observer.snapshot('run-1').receipts.at(-1)

test('exact endpoint POST/session only; no begin, Files, probe and wrong session bypass', async () => {
  let calls = 0
  globalThis.fetch = async () => { calls++; return response('unchanged') }
  observer = installUsageObserver({ endpoint, sessionId })
  await post()
  observer.begin('run-1')
  for (const [url, init] of [
    [endpoint + '?probe=1', { method: 'POST', headers }],
    [endpoint + '/extra', { method: 'POST', headers }],
    ['https://other.invalid/v1/messages', { method: 'POST', headers }],
    ['https://synthetic.invalid/v1/files', { method: 'POST', headers }],
    [endpoint, { method: 'GET', headers }],
    [endpoint, { method: 'POST', headers: { 'x-deepseek-harness-session-id': 'other-session' } }],
  ]) assert.equal(await (await fetch(url, init)).text(), 'unchanged')
  assert.equal(calls, 7)
  assert.deepEqual(observer.snapshot('run-1'), { runId: 'run-1', receipts: [], overflowed: false })
})

test('explicit zero is real, absent cache remains null; raw prompt total independent', async () => {
  const text = start({ input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, prompt_tokens: 9,
    total_tokens: 9999, reasoning_tokens: 8888 }) + delta({ output_tokens: 0 }) + stop()
  const seen = []
  bind(async () => response(text), item => seen.push(item))
  assert.equal(await (await post()).text(), text)
  const final = receipt()
  assert.equal(final.outcome, 'completed')
  assert.equal(final.terminalObserved, true)
  assert.deepEqual(final.usage, { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0,
    cache_creation_input_tokens: null, reported_prompt_tokens: 9 })
  assert.equal(seen[0].phase, 'started')
  assert.ok(Object.values(seen[0].usage).every(v => v === null))
  assert.equal(seen.at(-1).phase, 'finished')
  assert.ok(seen.every(r => r.attemptId === final.attemptId))
  assert.ok(seen.every((r, i) => i === 0 || r.revision > seen[i - 1].revision))
})

test('message_start output zero alone cannot prove final output zero', async () => {
  bind(async () => response(start({ input_tokens: 8, output_tokens: 0 }) + delta({}) + stop()))
  await (await post()).text()
  assert.equal(receipt().outcome, 'completed')
  assert.equal(receipt().usage.input_tokens, 8)
  assert.equal(receipt().usage.output_tokens, null)
})

test('cumulative message_delta replaces prior usage instead of adding totals/reasoning', async () => {
  bind(async () => response(start({ input_tokens: 8, cache_read_input_tokens: 3, cache_creation_input_tokens: 2 })
    + delta({ output_tokens: 4, total_tokens: 999 }) + delta({ output_tokens: 7, reasoning_tokens: 123 }) + stop()))
  await (await post()).text()
  assert.deepEqual(receipt().usage, { input_tokens: 8, output_tokens: 7, cache_read_input_tokens: 3,
    cache_creation_input_tokens: 2, reported_prompt_tokens: null })
})

test('one-byte UTF8 chunks and CRLF framing forward the identical bytes', async () => {
  const text = (start({ input_tokens: 11 })
    + frame({ type: 'content_block_delta', delta: { type: 'text_delta', text: '业务数据🙂' } })
    + delta({ output_tokens: 2 }) + stop()).replaceAll('\n', '\r\n')
  const original = encode(text)
  let index = 0
  bind(async () => new Response(new ReadableStream({ pull(c) {
    if (index === original.length) c.close(); else c.enqueue(original.subarray(index, ++index))
  } }, { highWaterMark: 0 }), { headers: { 'content-type': 'text/event-stream; charset=utf-8' } }))
  const actual = new Uint8Array(await (await post()).arrayBuffer())
  assert.deepEqual(actual, original)
  assert.equal(receipt().usage.output_tokens, 2)
})

test('multiline data, lone CR, comments and terminal reason are observed', async () => {
  const first = 'event: message_start\rdata: {"type":"message_start",\rdata: "message":{"usage":{"input_tokens":4}}}\r\r'
  bind(async () => response(': ping\r\r' + first + delta({ output_tokens: 1 }).replaceAll('\n', '\r') + stop().replaceAll('\n', '\r')))
  await (await post()).text()
  assert.equal(receipt().outcome, 'completed')
  assert.equal(receipt().usage.input_tokens, 4)
})

test('EOF without message_stop retains input/cache but final output becomes unknown', async () => {
  bind(async () => response(start({ input_tokens: 12, cache_read_input_tokens: 0 }) + delta({ output_tokens: 5 })))
  await (await post()).text()
  assert.equal(receipt().outcome, 'incomplete')
  assert.equal(receipt().usage.output_tokens, null)
  assert.equal(receipt().usage.input_tokens, 12)
  assert.equal(receipt().usage.cache_read_input_tokens, 0)
})

test('unterminated tail is not treated as a terminal SSE event', async () => {
  bind(async () => response(start({ input_tokens: 3 }) + delta({ output_tokens: 5 }) + stop().trimEnd()))
  await (await post()).text()
  assert.equal(receipt().terminalObserved, false)
  assert.equal(receipt().usage.output_tokens, null)
})

test('actual consumer cancellation forwards reason and retains only known input', async () => {
  let cancelled
  bind(async () => new Response(new ReadableStream({ start(c) { c.enqueue(encode(start({ input_tokens: 6, output_tokens: 0 }))) },
    cancel(reason) { cancelled = reason } }), { headers: { 'content-type': 'text/event-stream' } }))
  const res = await post()
  const reader = res.body.getReader()
  await reader.read()
  const reason = new Error('SYNTHETIC_PRIVATE_CANCEL_TEXT')
  await reader.cancel(reason)
  assert.equal(cancelled, reason)
  assert.equal(receipt().outcome, 'cancelled')
  assert.equal(receipt().usage.input_tokens, 6)
  assert.equal(receipt().usage.output_tokens, null)
  assert.ok(!JSON.stringify(receipt()).includes(reason.message))
})

test('transport exception is rethrown unchanged; attempt survives without error text', async () => {
  const error = new Error('SYNTHETIC_SECRET_TRANSPORT_TEXT')
  bind(async () => { throw error })
  await assert.rejects(post(), e => e === error)
  assert.equal(receipt().outcome, 'transport_error')
  assert.ok(Object.values(receipt().usage).every(v => v === null))
  assert.ok(!JSON.stringify(receipt()).includes(error.message))
})

test('HTTP failure response object and body are unchanged; no fabricated usage', async () => {
  const failure = response('{"secret":"error detail"}', { status: 402 })
  bind(async () => failure)
  assert.equal(await post(), failure)
  assert.equal(receipt().outcome, 'http_error')
  assert.equal(receipt().httpStatus, 402)
  assert.ok(Object.values(receipt().usage).every(v => v === null))
})

test('read failure does not erase earlier known input or swallow original exception', async () => {
  const error = new Error('SYNTHETIC_READ_ERROR')
  let first = true
  bind(async () => new Response(new ReadableStream({ pull(c) {
    if (first) { first = false; c.enqueue(encode(start({ input_tokens: 13 }))) } else c.error(error)
  } }, { highWaterMark: 0 }), { headers: { 'content-type': 'text/event-stream' } }))
  await assert.rejects((await post()).text(), e => e === error)
  assert.equal(receipt().usage.input_tokens, 13)
  assert.equal(receipt().outcome, 'transport_error')
})

test('Request + init header replacement follows fetch semantics; compaction and retries each get own ID', async () => {
  let calls = 0
  bind(async () => { calls++; return response(start({ input_tokens: 2 }) + delta({ output_tokens: 1 }) + stop()) })
  const request = new Request(endpoint, { method: 'POST', headers: { ...headers, 'x-deepseek-harness-compact': '1' }, body: 'SYNTHETIC_PRIVATE_BODY' })
  await (await fetch(request, { headers: {} })).text() // init replaces original headers, no match.
  await (await fetch(request)).text()
  await (await post()).text()
  const items = observer.snapshot('run-1').receipts
  assert.equal(calls, 3)
  assert.equal(items.length, 2)
  assert.deepEqual(items.map(r => r.purpose), ['compaction', 'answer'])
  assert.equal(new Set(items.map(r => r.attemptId)).size, 2)
})

test('observer does not read ahead, clone, tee, or alter original chunk object', async () => {
  let pulls = 0
  const bytes = encode(start({ input_tokens: 1 }))
  const underlying = new ReadableStream({ pull(c) { pulls++; c.enqueue(bytes) } }, { highWaterMark: 0 })
  underlying.tee = () => { throw new Error('TEE_FORBIDDEN') }
  const source = new Response(underlying, { headers: { 'content-type': 'text/event-stream' } })
  source.clone = () => { throw new Error('CLONE_FORBIDDEN') }
  bind(async () => source)
  const res = await post()
  await new Promise(r => setImmediate(r))
  assert.equal(pulls, 0)
  const reader = res.body.getReader()
  assert.equal((await reader.read()).value, bytes)
  assert.equal(pulls, 1)
  await reader.cancel()
})

test('oversized frame forwards bytes but produces explicitly limited accounting', async () => {
  const text = start({ input_tokens: 3 }) + 'data: ' + 'x'.repeat(300_000) + '\n\n' + delta({ output_tokens: 6 }) + stop()
  bind(async () => response(text))
  assert.equal(await (await post()).text(), text)
  assert.equal(receipt().outcome, 'observer_limited')
  assert.equal(receipt().usage.input_tokens, 3)
  assert.equal(receipt().usage.output_tokens, null)
})

test('invalid usage and mismatched event type never manufacture counters', async () => {
  bind(async () => response(start({ input_tokens: -1, output_tokens: '0', cache_read_input_tokens: 1e15 })
    + 'event: wrong\ndata: {"type":"message_delta","usage":{"output_tokens":10}}\n\n' + delta({}) + stop()))
  await (await post()).text()
  assert.equal(receipt().outcome, 'observer_limited')
  assert.ok(Object.values(receipt().usage).every(v => v === null))
})

test('abort before fetch settles finalizes once; late response cannot mutate its revision', async () => {
  let resolveFetch
  const abort = new AbortController()
  bind(() => new Promise(resolve => { resolveFetch = resolve }))
  const pending = post({ signal: abort.signal })
  abort.abort()
  const before = receipt()
  resolveFetch(response(start({ input_tokens: 99 }) + delta({ output_tokens: 8 }) + stop()))
  await (await pending).text()
  assert.deepEqual(receipt(), before)
  assert.equal(receipt().outcome, 'cancelled')
})

test('snapshot copies are isolated; run changes cannot happen during an active attempt', async () => {
  bind(async () => response(start({ input_tokens: 2 }) + delta({ output_tokens: 1 }) + stop()))
  const res = await post()
  assert.throws(() => observer.begin('run-2'), /binding unavailable/)
  await res.text()
  const copy = observer.snapshot('run-1')
  copy.receipts[0].usage.input_tokens = 999
  assert.equal(receipt().usage.input_tokens, 2)
  observer.begin('run-2')
  assert.throws(() => observer.snapshot('run-1'), /run mismatch/)
  assert.equal(observer.snapshot('run-2').receipts.length, 0)
})

test('sink failures never change HTTP bytes, and receipts contain only allowlisted metadata', async () => {
  const text = start({ input_tokens: 4, private_value: 'SYNTHETIC_PRIVATE_RAW_USAGE' })
    + frame({ type: 'content_block_delta', delta: { text: 'SYNTHETIC_PRIVATE_ANSWER' } })
    + delta({ output_tokens: 1 }) + stop()
  bind(async () => response(text), () => { throw new Error('SYNTHETIC_SINK_ERROR') })
  assert.equal(await (await post({ headers: { ...headers, authorization: 'SYNTHETIC_SECRET_TOKEN' }, body: 'SYNTHETIC_PRIVATE_REQUEST' })).text(), text)
  const value = receipt()
  assert.deepEqual(Object.keys(value).sort(), ['schema', 'sessionId', 'runId', 'attemptId', 'revision', 'purpose', 'phase', 'outcome', 'httpStatus', 'terminalObserved', 'usage'].sort())
  assert.ok(!JSON.stringify(value).includes('SYNTHETIC_'))
  assert.ok(!JSON.stringify(value).includes('https://'))
})

test('snapshot capacity is bounded and explicitly overflowed without changing fetch count', async () => {
  let calls = 0
  bind(async () => { calls++; return new Response(null, { status: 503 }) })
  for (let i = 0; i < 4097; i++) await post()
  const snap = observer.snapshot('run-1')
  assert.equal(calls, 4097)
  assert.equal(snap.receipts.length, 4096)
  assert.equal(snap.overflowed, true)
})

test('restore never overwrites a newer fetch wrapper and finishes pending accounting only', async () => {
  const mock = async () => response(start({ input_tokens: 2 }))
  bind(mock)
  const res = await post()
  const reader = res.body.getReader()
  await reader.read()
  const newer = async () => new Response('newer')
  globalThis.fetch = newer
  observer.restore()
  assert.equal(globalThis.fetch, newer)
  assert.equal(receipt().outcome, 'incomplete')
  await reader.cancel()
})
