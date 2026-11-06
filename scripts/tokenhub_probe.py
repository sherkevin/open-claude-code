"""Small, explicit live provider capability probe. Keeps credentials private.

Only synthetic requests; no product conversations or business tools. Successful
HTTP acceptance does not prove internal reasoning effort or model quality.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider-file', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--glm-only', action='store_true')
    args = parser.parse_args()
    provider = json.loads(args.provider_file.read_text())
    env = provider.get('env', {})
    base = env['ANTHROPIC_BASE_URL'].rstrip('/')
    credential = env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY')
    report = {'scope': 'Synthetic provider parameter acceptance; no internal-compute guarantee', 'checks': []}

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    with httpx.Client(base_url=base, timeout=90, follow_redirects=False,
        headers={'Authorization': 'Bearer ' + credential, 'x-api-key': credential,
                 'anthropic-version': '2023-06-01'}) as client:
        response = client.get('/v1/models')
        report['catalog_status'] = response.status_code
        if response.is_success:
            entries = response.json().get('data', [])
            report['catalog_matches'] = [e for e in entries if any(s in str(e.get('id', '')).lower() for s in ('qwen', 'deepseek-v4.1'))]
        save()
        checks = [('glm-5.3', None)] if args.glm_only else [('deepseek-v4.1-flash', None), ('glm-5.3', None),
                              ('deepseek-v4.1-flash-1017', 'low'),
                              ('deepseek-v4.1-flash-1017', 'high'),
                              ('deepseek-v4.1-flash-1017', 'max')]
        for model, effort in checks:
            payload = {'model': model, 'max_tokens': 256, 'messages': [{'role': 'user', 'content': 'Reply only PROBE_OK.'}]}
            if effort:
                payload.update(thinking={'type': 'adaptive'}, output_config={'effort': effort})
            start = time.monotonic()
            try:
                response = client.post('/v1/messages', json=payload)
                data = response.json()
                item = {'model': model, 'effort': effort, 'status': response.status_code,
                        'seconds': round(time.monotonic()-start, 3),
                        'response_model': data.get('model'), 'stop_reason': data.get('stop_reason'),
                        'usage': data.get('usage'),
                        'sentinel_present': 'PROBE_OK' in ''.join(b.get('text', '') for b in data.get('content', []))}
                if not response.is_success:
                    error = data.get('error', {})
                    item['error_type'] = error.get('type') if isinstance(error, dict) else 'provider_error'
            except Exception as exc:
                item = {'model': model, 'effort': effort, 'error_type': type(exc).__name__}
            report['checks'].append(item)
            save()
            print(json.dumps(item), flush=True)
    print('Report saved; credentials and raw provider errors excluded.', flush=True)


if __name__ == '__main__':
    main()
