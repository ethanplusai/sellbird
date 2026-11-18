"""HTTP for untrusted public websites, with a boundary around the local network.

Each request (including redirects) resolves and checks every address, then
connects to a checked numeric IP. Host and TLS SNI retain the original domain.
Checking DNS and then connecting to the hostname would allow DNS rebinding.
Environment proxies are disabled so they cannot resolve or reroute the target.
Fixed mail/API integrations keep their own clients and authentication settings.
"""

import asyncio
import ipaddress
import os
import socket
import ssl
from time import monotonic

import httpx


class UnsafeURLError(httpx.RequestError):
    """The requested destination is outside the public website boundary."""


_TRANSLATION_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


def _public_address(value: str) -> str:
    """Reject special-use addresses, including IPv6 routes to embedded IPv4."""
    if "%" in value:
        raise ValueError("Scoped addresses are not public website targets")
    address = ipaddress.ip_address(value)
    if (not address.is_global or address.is_multicast or address.is_reserved
            or address.is_unspecified):
        raise ValueError("Address is not public")
    if isinstance(address, ipaddress.IPv6Address):
        if (address.is_site_local or address.ipv4_mapped or address.sixtofour or address.teredo
                or any(address in network for network in _TRANSLATION_NETWORKS)):
            raise ValueError("Translated IPv6 addresses are not supported")
    return str(address)


class PublicHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        # Different domains may resolve to the same IP. Do not reuse a TLS
        # connection checked for one domain for a different domain's request.
        self._transport = httpx.AsyncHTTPTransport(
            trust_env=False, limits=httpx.Limits(max_keepalive_connections=0),
        )
        # LOCAL PATCH (cloud container only, not upstream): this machine has no
        # DNS and no direct route out, only an egress proxy outside the
        # container. With SELLBIRD_WEBSITE_PROXY set, websites are fetched
        # through it. The URL checks below still run; the address check cannot
        # (nothing resolves here), and the proxy resolves names on its own
        # network, which has no route back into this container.
        self._proxy_transport = None
        proxy = os.environ.get("SELLBIRD_WEBSITE_PROXY", "").strip()
        if proxy == "session":
            # The session proxy's port can change when the container restarts.
            proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "").strip()
        if proxy:
            context = ssl.create_default_context(
                cafile=os.environ.get("SSL_CERT_FILE") or None)
            self._proxy_transport = httpx.AsyncHTTPTransport(
                proxy=proxy, verify=context, trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host = url.raw_host.decode("ascii")
        if (url.scheme not in {"http", "https"} or not host
                or url.username or url.password or "%" in host
                or host.rstrip(".").lower() == "localhost"
                or host.rstrip(".").lower().endswith(".localhost")):
            raise UnsafeURLError("Only public HTTP(S) websites are allowed", request=request)
        port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise UnsafeURLError("Invalid website port", request=request)
        timeout = request.extensions.get("timeout", {}).get("connect")
        connect_budget = timeout if timeout is not None else 10.0
        deadline = monotonic() + connect_budget

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if self._proxy_transport is not None:
            if literal is not None:
                try:
                    _public_address(host)
                except ValueError as exc:
                    raise UnsafeURLError(
                        "Website resolves to a non-public destination", request=request) from exc
            if url.scheme == "http":
                # The proxy only tunnels HTTPS.
                if port != 80:
                    raise httpx.ConnectError("Plain HTTP on a custom port", request=request)
                request = httpx.Request(
                    method=request.method, url=url.copy_with(scheme="https", port=None),
                    headers=request.headers, stream=request.stream,
                    extensions=request.extensions,
                )
            return await self._proxy_transport.handle_async_request(request)

        try:
            if literal is not None:
                addresses = [_public_address(host)]
            else:
                # Include DNS time in the client's connect budget, with a
                # finite default even if the caller disables other timeouts.
                answers = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                    timeout=connect_budget,
                )
                addresses = list(dict.fromkeys(_public_address(row[4][0]) for row in answers))
                if not addresses:
                    raise ValueError("DNS returned no addresses")
                # Many operator machines have no IPv6 route even when DNS
                # publishes AAAA first. Prefer a validated IPv4 answer.
                addresses.sort(key=lambda value: ipaddress.ip_address(value).version)
        except ValueError as exc:
            raise UnsafeURLError("Website resolves to a non-public destination", request=request) from exc
        except asyncio.TimeoutError as exc:
            raise httpx.ConnectTimeout("Website DNS resolution timed out", request=request) from exc
        except OSError as exc:
            raise httpx.ConnectError("Website DNS resolution failed", request=request) from exc

        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        for index, address in enumerate(addresses):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise httpx.ConnectTimeout("Website connection budget exhausted", request=request)
            extensions = {
                **request.extensions, "sni_hostname": host,
                "timeout": {**request.extensions.get("timeout", {}), "connect": remaining},
            }
            pinned = httpx.Request(
                method=request.method, url=url.copy_with(host=address),
                headers=headers, stream=request.stream, extensions=extensions,
            )
            try:
                return await self._transport.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # The request stream has not been sent. Retry only connection
                # establishment, never a read/write failure that could duplicate
                # an API operation. Every candidate was validated above.
                if index == len(addresses) - 1:
                    raise

    async def aclose(self) -> None:
        await self._transport.aclose()
        if self._proxy_transport is not None:
            await self._proxy_transport.aclose()


def public_http_client(
    *, timeout: float | httpx.Timeout = 15.0, headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """A client whose every connection, redirect and TLS identity is checked."""
    return httpx.AsyncClient(
        transport=PublicHTTPTransport(), trust_env=False,
        timeout=timeout, headers=headers, follow_redirects=True, max_redirects=10,
    )
