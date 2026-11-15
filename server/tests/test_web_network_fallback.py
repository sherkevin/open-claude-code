"""Try validated DNS alternatives only before HTTP/TLS application traffic."""
import asyncio
import socket
import time
from unittest.mock import AsyncMock

import httpcore
import pytest

from app.security import AppError
from app.web_tools import PublicNetworkBackend


def resolved(monkeypatch, addresses):
    resolver = AsyncMock(return_value=[
        (socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))
        for address in addresses
    ])
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolver)
    return resolver


@pytest.mark.parametrize('failure', [httpcore.ConnectError('unreachable'), httpcore.ConnectTimeout('timeout'), OSError('unreachable')])
def test_connection_failure_tries_remaining_pinned_ip_without_resolving_again(monkeypatch, failure):
    async def scenario():
        resolver = resolved(monkeypatch, ['2606:4700::1111', '1.1.1.1'])
        backend = PublicNetworkBackend()
        backend._connect_approved_ip = AsyncMock(side_effect=[failure, 'public-stream'])
        assert await backend.connect_tcp('example.com', 443, 1) == 'public-stream'
        assert [call.args[0] for call in backend._connect_approved_ip.await_args_list] == ['2606:4700::1111', '1.1.1.1']
        assert resolver.await_count == 1
    asyncio.run(scenario())


def test_stalled_first_ip_leaves_time_for_second_ip(monkeypatch):
    async def scenario():
        resolved(monkeypatch, ['1.1.1.1', '8.8.8.8'])
        attempts = []
        async def connect(address, port, timeout, *args):
            attempts.append(address)
            if address == '1.1.1.1':
                await asyncio.Event().wait()
            return 'public-stream'
        backend = PublicNetworkBackend()
        backend._connect_approved_ip = connect
        started = time.monotonic()
        assert await backend.connect_tcp('example.com', 443, .1) == 'public-stream'
        assert attempts == ['1.1.1.1', '8.8.8.8']
        assert time.monotonic() - started < .5
    asyncio.run(scenario())


def test_all_stalled_addresses_share_one_connection_deadline(monkeypatch):
    async def scenario():
        resolved(monkeypatch, ['1.1.1.1', '8.8.8.8', '9.9.9.9'])
        async def stalled(*args):
            await asyncio.Event().wait()
        backend = PublicNetworkBackend()
        backend._connect_approved_ip = AsyncMock(side_effect=stalled)
        started = time.monotonic()
        with pytest.raises(httpcore.ConnectTimeout):
            await backend.connect_tcp('example.com', 443, .12)
        assert time.monotonic() - started < .5
        assert backend._connect_approved_ip.await_count == 3
    asyncio.run(scenario())


def test_private_answer_denies_whole_set_before_any_connection(monkeypatch):
    async def scenario():
        resolved(monkeypatch, ['1.1.1.1', '8.8.8.8', '169.254.169.254'])
        backend = PublicNetworkBackend()
        backend._connect_approved_ip = AsyncMock()
        with pytest.raises(AppError) as caught:
            await backend.connect_tcp('example.com', 443, 1)
        assert caught.value.code == 'WEB_URL_DENIED'
        backend._connect_approved_ip.assert_not_awaited()
    asyncio.run(scenario())


def test_host_denial_and_cancellation_never_try_another_address(monkeypatch):
    async def scenario():
        resolved(monkeypatch, ['1.1.1.1', '8.8.8.8'])
        for failure in (AppError(403, 'CAPABILITY_DOMAIN_DENIED', 'denied'), asyncio.CancelledError()):
            backend = PublicNetworkBackend()
            backend._connect_approved_ip = AsyncMock(side_effect=failure)
            with pytest.raises(type(failure)):
                await backend.connect_tcp('example.com', 443, 1)
            assert backend._connect_approved_ip.await_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [403, 429, 500])
def test_http_error_never_retries_another_address(monkeypatch, status):
    from test_web_egress import SyntheticStream
    async def scenario():
        resolved(monkeypatch, ['1.1.1.1', '8.8.8.8'])
        stream = SyntheticStream(f'HTTP/1.1 {status} Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode())
        backend = PublicNetworkBackend()
        backend._connect_approved_ip = AsyncMock(return_value=stream)
        async with httpcore.AsyncConnectionPool(network_backend=backend) as pool:
            response = await pool.request('GET', 'https://example.com/article')
        assert response.status == status
        assert backend._connect_approved_ip.await_count == 1
        assert stream.closed
    asyncio.run(scenario())


def test_doh_uses_explicit_relay_for_literal_resolver_with_tls_and_no_recursive_dns(monkeypatch):
    import json
    import ssl
    import httpx
    from types import SimpleNamespace
    from app.web_tools import public_dns_answers
    from test_web_egress import SyntheticStream
    monkeypatch.setenv('AGENT_WEB_EGRESS_SOCKS5', '127.0.0.1:17893')
    monkeypatch.setenv('AGENT_WEB_DNS_PROVIDER', 'default')
    async def scenario():
        body = json.dumps({'Status': 0, 'Answer': [{'type': 1, 'data': '93.184.216.34'}]}).encode()
        stream = SyntheticStream()
        stream.data.extend(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                           + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
        underlying = SimpleNamespace(connect_tcp=AsyncMock(return_value=stream))
        monkeypatch.setattr('app.web_tools.AutoBackend', lambda: underlying)
        original = httpx.AsyncHTTPTransport
        def transport(**kwargs):
            value = original(**kwargs)
            # An implementation that ignores the relay must fail locally,
            # never silently make a real network request during this test.
            value._pool._network_backend = SimpleNamespace(
                connect_tcp=AsyncMock(side_effect=AssertionError('DNS bypassed explicit egress')))
            return value
        monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', transport)
        async def resolve(host, port, **kwargs):
            assert host == '1.1.1.1'  # Resolver bootstrap is numeric, not recursive.
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (host, port))]
        resolver = AsyncMock(side_effect=resolve)
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolver)
        assert await public_dns_answers('example.com', timeout=1) == ['93.184.216.34']
        assert resolver.await_count == 1
        assert underlying.connect_tcp.await_args.args[:2] == ('127.0.0.1', 17893)
        assert stream.writes[1] == b'\x05\x01\x00\x01\x01\x01\x01\x01\x01\xbb'
        context, name = stream.tls[0]
        assert name == '1.1.1.1' and context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert b'Host: 1.1.1.1' in b''.join(stream.writes)
        assert b'name=example.com' in b''.join(stream.writes)
        assert stream.closed
    asyncio.run(scenario())


def test_invalid_doh_egress_never_falls_back_to_direct(monkeypatch):
    import httpx
    from types import SimpleNamespace
    from app.web_tools import public_dns_answers
    monkeypatch.setenv('AGENT_WEB_EGRESS_SOCKS5', '10.0.0.1:1080')
    called = []
    def handler(request):
        called.append(request)
        return httpx.Response(200, json={'Status': 0, 'Answer': [{'type': 1, 'data': '93.184.216.34'}]})
    def transport(**kwargs):
        value = httpx.MockTransport(handler)
        value._pool = SimpleNamespace(_network_backend=None)
        return value
    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', transport)
    with pytest.raises(AppError) as caught:
        asyncio.run(public_dns_answers('example.com'))
    assert caught.value.code == 'WEB_EGRESS_CONFIG_INVALID'
    assert called == []
