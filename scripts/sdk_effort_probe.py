"""Capture SDK model/effort wire fields against a loopback fake provider.

No credentials or real inference. Does not establish provider support.
"""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading


async def probe():
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            markers = ('PROBE_ID_ALPHA', 'PROBE_ID_BETA', 'PROBE_ID_GAMMA')
            captured.append({'path': self.path, **{key: body.get(key) for key in
                ('model', 'output_config', 'thinking', 'reasoning_effort', 'max_tokens', 'stream')},
                'system_markers': [m for m in markers if m in json.dumps(body.get('system'))],
                'message_markers': [m for m in markers if m in json.dumps(body.get('messages'))]})
            if 'count_tokens' in self.path:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"input_tokens":42}')
                return
            message = {'id': 'msg_fake_probe', 'type': 'message', 'role': 'assistant',
                       'model': body.get('model'), 'content': [], 'stop_reason': None,
                       'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 0}}
            events = [('message_start', {'type': 'message_start', 'message': message}),
                      ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                               'content_block': {'type': 'text', 'text': ''}}),
                      ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                               'delta': {'type': 'text_delta', 'text': 'OK'}}),
                      ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                      ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                                         'usage': {'output_tokens': 1}}),
                      ('message_stop', {'type': 'message_stop'})]
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
            self.end_headers()
            if body.get('stream'):
                self.wfile.write(''.join(f'event: {name}\ndata: {json.dumps(data)}\n\n' for name, data in events).encode())
            else:
                message.update(content=[{'type': 'text', 'text': 'OK'}], stop_reason='end_turn')
                self.wfile.write(json.dumps(message).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix='agent-effort-probe-') as name:
            root = Path(name)
            session_id = None
            for index, effort in enumerate(('low', 'high', 'max')):
                home = root / ('resumed' if ARGS.resume else effort)
                home.mkdir(exist_ok=True)
                marker = ('PROBE_ID_ALPHA', 'PROBE_ID_BETA', 'PROBE_ID_GAMMA')[index]
                model = ('deepseek-v4.1-flash-1017', 'deepseek-v4.1-flash', 'glm-5.3')[index] if ARGS.switch_models else 'deepseek-v4.1-flash-1017'
                options = ClaudeAgentOptions(model=model, effort=effort,
                    system_prompt={'type': 'custom', 'snapshot': False, 'prompt': f'You are {marker}. Reply OK.'},
                    resume=session_id if ARGS.resume else None,
                    tools=[], setting_sources=[], strict_mcp_config=True,
                    cwd=str(home), env={'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}',
                        'ANTHROPIC_AUTH_TOKEN': 'fake-loopback-probe', 'ANTHROPIC_API_KEY': '',
                        'ANTHROPIC_MODEL': model,
                        'CLAUDE_CONFIG_DIR': str(home / 'config'), 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
                        'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'}, max_turns=1, stderr=lambda _: None)
                client = ClaudeSDKClient(options)
                offset = len(captured)
                try:
                    await client.connect()
                    async def composed():
                        yield {'type': 'user', 'message': {'role': 'user', 'content': 'Only reply OK.'}, 'parent_tool_use_id': None, 'client_composed': True}
                    await client.query(composed() if ARGS.client_composed else 'Only reply OK.')
                    async for event in client.receive_response():
                        if type(event).__name__ == 'ResultMessage':
                            session_id = event.session_id
                            results.append({'requested_effort': effort, 'is_error': event.is_error,
                                            'wire': captured[offset:]})
                finally:
                    await client.disconnect()
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps({'probe': 'loopback-fake-provider', 'results': results}, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--client-composed', action='store_true')
    parser.add_argument('--switch-models', action='store_true')
    ARGS = parser.parse_args()
    asyncio.run(probe())
