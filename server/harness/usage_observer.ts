import { goalAttemptBudget, type AttemptExchange } from './goal_attempt_budget.ts'

/** Product-owned raw Messages usage observer; never creates or retries a request.
 * The pinned adapter defaults absent input/output to zero. Observe the exact SSE
 * bytes before translation, retaining explicit zero and unknown independently.
 */

export interface RawUsage {
  input_tokens: number | null
  output_tokens: number | null
  cache_read_input_tokens: number | null
  cache_creation_input_tokens: number | null
  reported_prompt_tokens: number | null
}
export type UsageOutcome = 'pending' | 'completed' | 'incomplete' | 'http_error'
  | 'transport_error' | 'cancelled' | 'observer_limited'
export interface UsageReceipt {
  schema: 'workspace-usage/v1'
  sessionId: string
  runId: string
  attemptId: string
  revision: number
  purpose: 'answer' | 'compaction'
  phase: 'started' | 'usage' | 'finished'
  outcome: UsageOutcome
  httpStatus: number | null
  terminalObserved: boolean
  usage: RawUsage
}
export interface UsageSnapshot {
  runId: string
  receipts: UsageReceipt[]
  overflowed: boolean
}
export interface UsageObserver {
  begin(runId: string): void
  snapshot(runId: string): UsageSnapshot
  restore(): void
}

const MAX_ATTEMPTS = 4096
const MAX_LINE = 65_536
const MAX_FRAME = 262_144
const MAX_TOKENS = 1_000_000_000_000
const identifier = (value: unknown): value is string =>
  typeof value === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(value)
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
const emptyUsage = (): RawUsage => ({ input_tokens: null, output_tokens: null,
  cache_read_input_tokens: null, cache_creation_input_tokens: null, reported_prompt_tokens: null })
const copyReceipt = (receipt: UsageReceipt): UsageReceipt => ({ ...receipt, usage: { ...receipt.usage } })

/** Small independent SSE framer. It never retains model content after a frame,
 * never flushes an unterminated event at EOF, and bounds partial lines/frames.
 * Observation errors only make accounting incomplete; bytes still pass through.
 */
class UsageFrames {
  private decoder = new TextDecoder('utf-8', { fatal: true })
  private line = ''
  private data: string[] = []
  private event = ''
  private frameSize = 0
  private skipFrame = false
  private afterCR = false
  private receive: (event: Record<string, unknown>) => void
  private limited: () => void
  constructor(receive: (event: Record<string, unknown>) => void, limited: () => void) {
    this.receive = receive
    this.limited = limited
  }

  private invalid(): void {
    this.limited()
    this.skipFrame = true
    this.line = ''
    this.data = []
    this.event = ''
  }

  feed(bytes: Uint8Array): void {
    try {
      // Do not decode an arbitrarily large upstream chunk into another large string.
      for (let offset = 0; offset < bytes.byteLength; offset += 8192) {
        const text = this.decoder.decode(bytes.subarray(offset, offset + 8192), { stream: true })
        for (const char of text) {
          if (this.afterCR) {
            this.afterCR = false
            if (char === '\n') continue
          }
          if (char === '\r' || char === '\n') {
            this.endLine()
            this.afterCR = char === '\r'
          } else if (this.line.length < MAX_LINE) {
            this.line += char
          } else {
            this.invalid()
          }
        }
      }
    } catch {
      this.invalid()
      this.decoder = new TextDecoder('utf-8', { fatal: true })
    }
  }

  private endLine(): void {
    const line = this.line
    this.line = ''
    if (line === '') {
      if (!this.skipFrame && this.data.length > 0) {
        try {
          const value: unknown = JSON.parse(this.data.join('\n'))
          if (!record(value) || typeof value.type !== 'string'
              || (this.event !== '' && this.event !== value.type)) this.limited()
          else this.receive(value)
        } catch { this.limited() }
      }
      this.data = []
      this.event = ''
      this.frameSize = 0
      this.skipFrame = false
      return
    }
    if (this.skipFrame || line.startsWith(':')) return
    this.frameSize += line.length
    if (this.frameSize > MAX_FRAME) { this.invalid(); return }
    const colon = line.indexOf(':')
    const key = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (key === 'data') this.data.push(value)
    else if (key === 'event') this.event = value
  }
}

/** endpoint is the complete configured /messages URL, not a provider hostname.
 * begin binds future attempts to one authorized run. Revisions replace an
 * attempt's previous receipt; never sum notifications from the same attempt.
 */
export function installUsageObserver(config: {
  endpoint: string
  sessionId: string
  exchange?: AttemptExchange
  onReceipt?: (receipt: UsageReceipt) => void | Promise<void>
}): UsageObserver {
  let endpoint: URL
  try { endpoint = new URL(config.endpoint) } catch { throw new Error('Invalid usage observer endpoint') }
  if (!['https:', 'http:'].includes(endpoint.protocol) || !endpoint.pathname.endsWith('/messages')
      || endpoint.username || endpoint.password || endpoint.search || endpoint.hash || !identifier(config.sessionId)) {
    throw new Error('Invalid usage observer binding')
  }
  const budget = config.exchange ? goalAttemptBudget(config.exchange) : undefined
  const expectedUrl = endpoint.href
  const sessionId = config.sessionId
  const previousFetch = globalThis.fetch
  if (typeof previousFetch !== 'function') throw new Error('Fetch unavailable for usage observer')
  let runId: string | null = null
  let active = 0
  let disposed = false
  let overflowed = false
  const attempts = new Map<string, { receipt: UsageReceipt, finish: (outcome: UsageOutcome) => void }>()

  const publish = (receipt: UsageReceipt): void => {
    if (!config.onReceipt) return
    const copy = copyReceipt(receipt)
    Object.freeze(copy.usage); Object.freeze(copy)
    try { void Promise.resolve(config.onReceipt(copy)).catch(() => {}) } catch { /* Observability cannot break I/O. */ }
  }

  const matching = (input: RequestInfo | URL, init?: RequestInit): { purpose: 'answer' | 'compaction', signal?: AbortSignal | null } | undefined => {
    try {
      const request = input instanceof Request ? input : undefined
      const url = new URL(request?.url ?? String(input))
      const method = init?.method ?? request?.method ?? 'GET'
      const headers = new Headers(init?.headers !== undefined ? init.headers : request?.headers)
      if (url.href !== expectedUrl || method.toUpperCase() !== 'POST'
          || headers.get('x-deepseek-harness-session-id') !== sessionId) return
      return { purpose: headers.get('x-deepseek-harness-compact') === '1' ? 'compaction' : 'answer',
        signal: init?.signal !== undefined ? init.signal : request?.signal }
    } catch { return }
  }

  const wrappedFetch: typeof fetch = async (input, init) => {
    const match = !disposed && runId !== null ? matching(input, init) : undefined
    if (!match) {
      if (budget) throw new Error('Goal budget does not cover this endpoint or session')
      return Reflect.apply(previousFetch, globalThis, [input, init])
    }
    const attemptId = globalThis.crypto.randomUUID()
    const reserved = budget ? await budget.reserve(runId!, attemptId, init) : undefined
    const receipt: UsageReceipt = { schema: 'workspace-usage/v1', sessionId, runId: runId!,
      attemptId, revision: 1, purpose: match.purpose, phase: 'started', outcome: 'pending',
      httpStatus: null, terminalObserved: false, usage: emptyUsage() }
    let finished = false
    let limited = false
    let startedMessage = false
    let stopReasonSeen = false
    let finalOutputSeen = false
    let signalListener: (() => void) | undefined
    active += 1
    const notify = (): void => { receipt.revision += 1; publish(receipt) }
    const finish = (outcome: UsageOutcome): void => {
      if (finished) return
      finished = true
      active -= 1
      if (signalListener) match.signal?.removeEventListener('abort', signalListener)
      receipt.phase = 'finished'
      receipt.outcome = limited ? 'observer_limited'
        : receipt.terminalObserved ? 'completed' : outcome
      if (!receipt.terminalObserved || !finalOutputSeen || limited) receipt.usage.output_tokens = null
      notify()
    }
    if (attempts.size < MAX_ATTEMPTS) attempts.set(receipt.attemptId, { receipt, finish })
    else overflowed = true // Continue exact I/O and notifications; snapshot explicitly declares incomplete capture.
    publish(receipt)
    signalListener = () => finish('cancelled')
    match.signal?.addEventListener('abort', signalListener, { once: true })
    if (match.signal?.aborted) finish('cancelled')

    const updateUsage = (raw: unknown, finalDelta: boolean): boolean => {
      if (!record(raw)) return false
      let changed = false
      const mappings = { input_tokens: 'input_tokens', output_tokens: 'output_tokens',
        cache_read_input_tokens: 'cache_read_input_tokens', cache_creation_input_tokens: 'cache_creation_input_tokens',
        prompt_tokens: 'reported_prompt_tokens' } as const
      for (const [wire, local] of Object.entries(mappings)) {
        if (!Object.hasOwn(raw, wire)) continue
        const number = raw[wire]
        const valid = Number.isSafeInteger(number) && (number as number) >= 0 && (number as number) <= MAX_TOKENS
        const value = valid ? number as number : null
        if (!valid) limited = true
        if (local === 'output_tokens' && finalDelta && valid) finalOutputSeen = true
        if (receipt.usage[local] !== value) { receipt.usage[local] = value; changed = true }
      }
      return changed
    }
    const frames = new UsageFrames(event => {
      if (finished) return
      let changed = false
      if (event.type === 'message_start') {
        if (startedMessage || !record(event.message)) { limited = true; return }
        startedMessage = true
        changed = updateUsage(event.message.usage, false)
      } else if (event.type === 'message_delta') {
        if (!startedMessage) { limited = true; return }
        if (record(event.delta) && ['end_turn', 'stop_sequence', 'tool_use', 'max_tokens'].includes(String(event.delta.stop_reason))) stopReasonSeen = true
        changed = updateUsage(event.usage, true)
      } else if (event.type === 'message_stop') {
        if (!startedMessage || !stopReasonSeen) { limited = true; return }
        receipt.terminalObserved = true
        finish('completed')
        return
      } else if (event.type === 'error') {
        finish('transport_error')
        return
      }
      if (changed) { receipt.phase = 'usage'; notify() }
    }, () => { limited = true })

    let response: Response
    try { response = await Reflect.apply(previousFetch, globalThis, [input, reserved?.init ?? init]) }
    catch (error) {
      finish(match.signal?.aborted ? 'cancelled' : 'transport_error')
      await reserved?.transportError()
      throw error
    }
    if (reserved) {
      try { await reserved.settle(response) }
      catch (error) { finish('incomplete'); throw error }
    }
    if (finished) return response
    receipt.httpStatus = response.status
    if (!response.ok) { finish('http_error'); return response }
    if (response.body === null || !/^text\/event-stream(?:\s*;|\s*$)/i.test(response.headers.get('content-type') ?? '')) {
      finish('incomplete'); return response
    }
    // One reader, one output stream. No tee/clone, no background draining and no
    // read-ahead queue; every Uint8Array is forwarded without modification.
    let reader: ReadableStreamDefaultReader<Uint8Array>
    try { reader = response.body.getReader() }
    catch { finish('incomplete'); return response }
    let released = false
    const release = (): void => { if (!released) { released = true; try { reader.releaseLock() } catch {} } }
    const body = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const chunk = await reader.read()
          if (chunk.done) { finish('incomplete'); release(); controller.close(); return }
          if (!finished) frames.feed(chunk.value)
          controller.enqueue(chunk.value)
        } catch (error) {
          finish(match.signal?.aborted ? 'cancelled' : 'transport_error')
          release(); controller.error(error)
        }
      },
      async cancel(reason) {
        finish('cancelled')
        try { await reader.cancel(reason) } finally { release() }
      },
    }, { highWaterMark: 0 })
    const observed = new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers })
    for (const key of ['url', 'redirected', 'type'] as const) {
      Object.defineProperty(observed, key, { get: () => response[key], configurable: true })
    }
    return observed
  }
  globalThis.fetch = wrappedFetch
  return {
    begin(nextRunId) {
      if (disposed || !identifier(nextRunId) || active !== 0) throw new Error('Usage observer run binding unavailable')
      if (runId === nextRunId) return
      attempts.clear(); overflowed = false; runId = nextRunId
    },
    snapshot(expectedRunId) {
      if (runId !== expectedRunId) throw new Error('Usage observer run mismatch')
      return { runId: expectedRunId, receipts: [...attempts.values()].map(value => copyReceipt(value.receipt)), overflowed }
    },
    restore() {
      if (disposed) return
      disposed = true
      for (const attempt of attempts.values()) attempt.finish('incomplete')
      if (globalThis.fetch === wrappedFetch) globalThis.fetch = previousFetch
    },
  }
}
