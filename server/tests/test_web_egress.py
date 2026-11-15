"""The optional local relay may transport only already validated public IPs."""
import asyncio
import socket
import ssl
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

from app.security import AppError
from app.web_tools import PublicNetworkBackend, public_dns_answers


class SyntheticStream:
    def __init__(self, data=b'\x05\x00\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00'):
        self.data = bytearray(data)
        self.writes = []
        self.tls = []
        self.closed = False

    async def read(self, size, timeout=None):
        value = bytes(self.data[:size])
        del self.data[:size]
        return value

    async def write(self, data, timeout=None):
        self.writes.append(data)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((ssl_context, server_hostname))
        return self

    def get_extra_info(self, name):
        return None

    async def aclose(self):
        self.closed = True


def public_resolver(monkeypatch, addresses=('93.184.216.34',)):
    resolve = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses])
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    return resolve


def test_relay_connects_literal_validated_ip_but_tls_and_host_keep_original_domain(monkeypatch):
    monkeypatch.setenv('AGENT_WEB_EGRESS_SOCKS5', '127.0.0.1:17893')

    async def scenario():
        resolver = public_resolver(monkeypatch)
        stream = SyntheticStream()
        stream.data.extend(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
        backend = PublicNetworkBackend()
        backend.backend.connect_tcp = AsyncMock(return_value=stream)
        async with httpcore.AsyncConnectionPool(network_backend=backend) as pool:
            result = await pool.request('GET', 'https://example.com/reference')
            assert result.status == 200 and result.content == b'ok'
        assert backend.backend.connect_tcp.call_args.args[:2] == ('127.0.0.1', 17893)
        assert stream.writes[:2] == [b'\x05\x01\x00', b'\x05\x01\x00\x01' + socket.inet_aton('93.184.216.34') + b'\x01\xbb']
        assert resolver.await_count == 1
        assert len(stream.tls) == 1
        context, hostname = stream.tls[0]
        assert hostname == 'example.com'
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert b'Host: example.com' in b''.join(stream.writes)
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize('addresses', [('127.0.0.1',), ('169.254.169.254',), ('93.184.216.34', '10.0.0.1')])
def test_relay_does_not_receive_private_or_mixed_dns_answers(monkeypatch, addresses):
    monkeypatch.setenv('AGENT_WEB_EGRESS_SOCKS5', '127.0.0.1:17893')

    async def scenario():
        public_resolver(monkeypatch, addresses)
        backend = PublicNetworkBackend()
        backend.backend.connect_tcp = AsyncMock()
        with pytest.raises(AppError) as error:
            await backend.connect_tcp('example.com', 443)
        assert error.value.code == 'WEB_URL_DENIED'
        backend.backend.connect_tcp.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize('reply', [b'', b'\x05\xff', b'\x05\x00\x05\x05\x00\x01', b'\x05\x00\x05\x00\x00\xff'])
def test_relay_errors_close_connection_and_never_continue_to_http(monkeypatch, reply):
    monkeypatch.setenv('AGENT_WEB_EGRESS_SOCKS5', '127.0.0.1:17893')

    async def scenario():
        public_resolver(monkeypatch)
        stream = SyntheticStream(reply)
        backend = PublicNetworkBackend()
        backend.backend.connect_tcp = AsyncMock(return_value=stream)
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp('example.com', 443, 1)
        assert stream.closed and stream.tls == []
        assert len(stream.writes) <= 2

    asyncio.run(scenario())


def test_relay_cancellation_closes_handshake(monkeypatch):
    monkeypatch.setenv('AGENT_WEB_EGRESS_SOCKS5', '127.0.0.1:17893')

    async def scenario():
        public_resolver(monkeypatch)
        entered = asyncio.Event()
        stream = SyntheticStream()

        async def waiting(*args):
            entered.set()
            await asyncio.Event().wait()

        stream.read = waiting
        backend = PublicNetworkBackend()
        backend.backend.connect_tcp = AsyncMock(return_value=stream)
        task = asyncio.create_task(backend.connect_tcp('example.com', 443, 1))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize('value', ['localhost:17893', '0.0.0.0:17893', '10.0.0.1:17893',
                                  '127.0.0.1:0', '127.0.0.1:65536', 'socks5://127.0.0.1:17893', 'u:p@127.0.0.1:17893'])
def test_relay_configuration_must_be_explicit_literal_loopback(monkeypatch, value):
    monkeypatch.setenv('AGENT_WEB_EGRESS_SOCKS5', value)
    with pytest.raises(AppError) as error:
        PublicNetworkBackend()
    assert error.value.code == 'WEB_EGRESS_CONFIG_INVALID'


def test_alidns_is_explicit_https_and_never_sends_search_text(monkeypatch):
    monkeypatch.setenv('AGENT_WEB_DNS_PROVIDER', 'alidns')
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'Status': 0, 'Answer': [{'type': 1, 'data': '93.184.216.34'}]})

    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', lambda **kwargs: httpx.MockTransport(handler))
    assert asyncio.run(public_dns_answers('example.com')) == ['93.184.216.34']
    assert len(requests) == 1
    assert str(requests[0].url).startswith('https://223.5.5.5/resolve?')
    assert dict(requests[0].url.params) == {'name': 'example.com', 'type': 'A', 'edns_client_subnet': '0.0.0.0/0'}


def test_alidns_private_answer_remains_denied_without_trying_other_resolvers(monkeypatch):
    monkeypatch.setenv('AGENT_WEB_DNS_PROVIDER', 'alidns')
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'Status': 0, 'Answer': [{'type': 1, 'data': '10.0.0.1'}]})

    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', lambda **kwargs: httpx.MockTransport(handler))
    with pytest.raises(AppError) as error:
        asyncio.run(public_dns_answers('example.com'))
    assert error.value.code == 'WEB_URL_DENIED' and len(calls) == 1
