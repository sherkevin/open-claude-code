import { describe, expect, it, vi } from 'vitest';
import { recoveryMessagePayload, sendChatMessage } from './ChatPage';

describe('task recovery request', () => {
  it('identifies the prior run and reuses its request id when the response is lost', async () => {
    const fetch = vi.fn().mockRejectedValueOnce(new Error('response lost')).mockResolvedValue(new Response(JSON.stringify({ run_id: 'new-run' }), {
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetch);
    try {
      const request = recoveryMessagePayload('prior-run', 'request-123');
      await expect(sendChatMessage('conversation', request)).rejects.toThrow();
      await sendChatMessage('conversation', request);
      expect(fetch.mock.calls[0][0]).toBe('/api/v1/conversations/conversation/messages');
      expect(fetch.mock.calls[1][1].body).toBe(fetch.mock.calls[0][1].body);
      expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
        client_request_id: 'request-123',
        recovery_run_id: 'prior-run',
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
