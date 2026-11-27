/** Fixed-price loopback protocol, installed only for an explicitly capped
 * round. The provider's final charge is available at HTTP response headers,
 * including for a rejected request. Missing headers cannot release a hold.
 */
import { createHash } from 'node:crypto'

export type AttemptExchange = (message: {
  action: 'reserve' | 'settle' | 'retryable'
  runId: string
  attemptId: string
  retryOf?: string
  costMicros?: number
  retryable?: boolean
}) => Promise<void>

export function goalAttemptBudget(exchange: AttemptExchange) {
  let failed: { id: string, digest: string } | null = null
  let blocked = false
  let awaitingHeaders = false

  return {
    async reserve(runId: string, attemptId: string, init?: RequestInit) {
      if (blocked || awaitingHeaders || typeof init?.body !== 'string') {
        throw new Error('Goal request has no eligible budget reservation')
      }
      const digest = createHash('sha256').update(init.body).digest('hex')
      if (failed && failed.digest !== digest) throw new Error('Goal retry does not match its failed request')
      awaitingHeaders = true
      try {
        await exchange({ action: 'reserve', runId, attemptId, ...(failed ? { retryOf: failed.id } : {}) })
      } catch (error) {
        blocked = true
        throw error
      }
      failed = null
      const headers = new Headers(init.headers)
      headers.set('x-kaiwu-attempt-id', attemptId)
      return {
        init: { ...init, headers, redirect: 'error' as const },
        async transportError() {
          // Delivery is uncertain, so the held amount stays reserved.
          failed = { id: attemptId, digest }
          try { await exchange({ action: 'retryable', runId, attemptId }) }
          catch (error) { blocked = true; throw error }
          finally { awaitingHeaders = false }
        },
        async settle(response: Response) {
          try {
            const amount = response.headers.get('x-kaiwu-cost-micros') ?? ''
            if (response.headers.get('x-kaiwu-attempt-id') !== attemptId
                || !/^(0|[1-9][0-9]{0,12})$/.test(amount)) {
              throw new Error('Goal invoice is missing or does not match this attempt')
            }
            const code = response.status
            const retryable = code === 408 || code === 429 || code >= 500
            await exchange({ action: 'settle', runId, attemptId, costMicros: Number(amount), retryable })
            if (retryable) failed = { id: attemptId, digest }
          } catch (error) {
            blocked = true
            await response.body?.cancel().catch(() => {})
            throw error
          } finally { awaitingHeaders = false }
        },
      }
    },
  }
}
