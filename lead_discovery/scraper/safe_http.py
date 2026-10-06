"""Public-network-only HTTP transport, including redirect and DNS-rebinding protection."""
import asyncio
import ipaddress
import socket

import httpx


class WebsiteFetchError(ValueError):
    """Expected website rejection; report it without a Python traceback."""


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.transports = {}

    async def aclose(self):
        await asyncio.gather(*(transport.aclose() for transport in self.transports.values()))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        port = url.port or (443 if url.scheme == "https" else 80)
        if url.scheme not in {"http", "https"} or port not in {80, 443}:
            raise WebsiteFetchError("Website requests require HTTP(S) on ports 80 or 443")
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                url.host, port, type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise httpx.ConnectError(f"DNS lookup failed for {url.host}", request=request) from exc
        ips = [item[4][0] for item in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise WebsiteFetchError("Blocked non-public website address")
        # Separate pools retain TLS identity when different sites share an IP.
        if url.host not in self.transports:
            self.transports[url.host] = httpx.AsyncHTTPTransport()
        # Prefer IPv4, then try other checked addresses if one cannot connect.
        ips = sorted(set(ips), key=lambda ip: ipaddress.ip_address(ip).version)[:4]
        for index, ip in enumerate(ips):
            pinned = httpx.Request(
                request.method, url.copy_with(host=ip), headers=request.headers,
                stream=request.stream, extensions={**request.extensions, "sni_hostname": url.host},
            )
            try:
                return await self.transports[url.host].handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(ips) - 1:
                    raise


async def bounded_get(client, url: str, max_bytes: int) -> httpx.Response:
    """Bound retained response bytes before parsing."""
    async with client.stream("GET", url, follow_redirects=False) as response:
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > max_bytes:
                raise WebsiteFetchError(f"Website response exceeded {max_bytes:,}-byte limit")
        headers = {key: value for key, value in response.headers.items()
                   if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
        return httpx.Response(response.status_code, headers=headers,
                              content=bytes(body), request=response.request)
