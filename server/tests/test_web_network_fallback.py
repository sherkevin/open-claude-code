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
