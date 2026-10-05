import asyncio
import socket

import httpx
import pytest

from scraper.safe_http import PublicTransport, bounded_get


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1'])
def test_private_dns_answers_never_connect(monkeypatch, ip):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        async with httpx.AsyncClient(transport=PublicTransport()) as client:
            with pytest.raises(ValueError, match='non-public'):
                await client.get('https://public.example/')
    asyncio.run(run())


def test_checked_ip_is_pinned_and_tls_identity_preserved(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
    async def send(self, request):
        assert request.url.host == '93.184.216.34'
        assert request.headers['host'] == 'public.example'
        assert request.extensions['sni_hostname'] == 'public.example'
        return httpx.Response(200, content=b'ok')
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        async with httpx.AsyncClient(transport=PublicTransport()) as client:
            response = await client.get('https://public.example/')
            assert str(response.url) == 'https://public.example/'
    asyncio.run(run())


def test_response_size_limit():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'x' * 101))) as client:
            with pytest.raises(ValueError, match='byte limit'):
                await bounded_get(client, 'https://public.example/', 100)
    asyncio.run(run())


def test_connection_failure_tries_another_public_address(monkeypatch):
    calls = []
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))
                for ip in ('93.184.216.34', '93.184.216.35')]
    async def send(self, request):
        calls.append(request.url.host)
        if len(calls) == 1:
            raise httpx.ConnectError('address unavailable')
        return httpx.Response(200, content=b'ok')
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', send)
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        async with httpx.AsyncClient(transport=PublicTransport()) as client:
            assert (await client.get('https://public.example/')).status_code == 200
    asyncio.run(run())
    assert set(calls) == {'93.184.216.34', '93.184.216.35'}


def test_dns_failure_becomes_a_handled_network_error(monkeypatch):
    async def resolve(*args, **kwargs):
        raise socket.gaierror(11001, 'getaddrinfo failed')
    async def run():
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
        async with httpx.AsyncClient(transport=PublicTransport()) as client:
            with pytest.raises(httpx.ConnectError, match='DNS lookup failed for public.example'):
                await client.get('https://public.example/')
    asyncio.run(run())
