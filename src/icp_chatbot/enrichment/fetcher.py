"""Fetching a company's public website safely (spec section 11.8).

The address comes from the user, so this is a security boundary: only public http/https hosts,
re-checked on every redirect, with limits on time, size and content type, and robots.txt
respected. One risk is not closed here: a hostile DNS server could answer differently between our
check and the connection (DNS rebinding). The spec's mitigation applies: run this fetcher in an
isolated network that cannot reach internal services or credentials.
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import SplitResult, urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "LeadNestICPBot/1.0"
ALLOWED_PORTS = {None, 80, 443}
TEXT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
XML_TYPES = {"application/xml", "text/xml"}
MAX_SITEMAP_URLS = 500
MAX_CHILD_SITEMAPS = 2
_LOC = re.compile(
    r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.IGNORECASE | re.DOTALL
)
ROBOTS_LIMIT_BYTES = 100_000
MAX_LINKS = 30

Resolver = Callable[[str], Awaitable[list[str]]]


def same_site(a: str | None, b: str | None) -> bool:
    """The same website, counting example.com and www.example.com as one."""
    strip = lambda h: (h or "").lower().removeprefix("www.")  # noqa: E731
    return bool(a) and strip(a) == strip(b)


class FetchBlocked(Exception):
    """The page was not fetched. reason is a short code: scheme, host, userinfo, port, dns,
    private_address, robots, redirect, too_many_redirects, http_status, content_type, network."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FetchedPage:
    url: str
    text: str
    links: list[str]
    truncated: bool


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str) -> SplitResult:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise FetchBlocked("scheme", parts.scheme or "none")
    if not parts.hostname:
        raise FetchBlocked("host", "missing host")
    if parts.username or parts.password:
        raise FetchBlocked("userinfo", "credentials in the address")
    try:
        port = parts.port
    except ValueError as exc:
        raise FetchBlocked("port", "invalid port") from exc
    if port not in ALLOWED_PORTS:
        raise FetchBlocked("port", str(port))
    return parts


async def check_host(host: str, resolver: Resolver) -> None:
    """Every address the host resolves to must be a public one."""
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await resolver(host)
        except OSError as exc:
            raise FetchBlocked("dns", host) from exc
    if not addresses:
        raise FetchBlocked("dns", host)
    for address in addresses:
        if not is_public_ip(address):
            raise FetchBlocked("private_address", f"{host} -> {address}")


def html_to_text(html: str, base_url: str, max_chars: int) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"})
    description = (meta.get("content") or "").strip() if meta else ""

    host = urlsplit(base_url).hostname
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        absolute = urldefrag(urljoin(base_url, a["href"]))[0]
        link = urlsplit(absolute)
        if (
            link.scheme in ("http", "https")
            and same_site(link.hostname, host)
            and absolute not in links
        ):
            links.append(absolute)
        if len(links) >= MAX_LINKS:
            break

    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "form"]):
        tag.decompose()
    body = " ".join(soup.get_text(" ", strip=True).split())
    text = " ".join(part for part in (title, description, body) if part)
    return text[:max_chars], links


class SafeFetcher:
    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        max_bytes: int = 500_000,
        timeout_s: float = 5.0,
        max_redirects: int = 3,
        max_chars: int = 15_000,
        user_agent: str = USER_AGENT,
    ):
        self._resolver = resolver
        self._max_bytes = max_bytes
        self._timeout = timeout_s
        self._max_redirects = max_redirects
        self._max_chars = max_chars
        self._user_agent = user_agent

    @staticmethod
    async def _read(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                chunks.append(chunk[: len(chunk) - (size - limit)])
                return b"".join(chunks), True
            chunks.append(chunk)
        return b"".join(chunks), False

    async def _robots(
        self, client: httpx.AsyncClient, parts: SplitResult, cache: dict
    ) -> robotparser.RobotFileParser | None:
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in cache:
            return cache[origin]
        parser = None
        try:
            async with client.stream("GET", origin + "/robots.txt") as response:
                if response.status_code == 200:
                    body, _ = await self._read(response, ROBOTS_LIMIT_BYTES)
                    parser = robotparser.RobotFileParser()
                    parser.parse(body.decode("utf-8", errors="replace").splitlines())
        except httpx.HTTPError:
            parser = None  # no readable robots.txt: allowed
        cache[origin] = parser
        return parser

    async def fetch(self, url: str) -> FetchedPage:
        final, html, truncated = await self._get(url, TEXT_TYPES)
        text, links = html_to_text(html, final, self._max_chars)
        return FetchedPage(final, text, links, truncated)

    async def sitemap(self, site: str) -> list[str]:
        """Page addresses from the site's /sitemap.xml (and up to two child sitemaps), on the same
        host only. Every request goes through the same checks as a page. Empty if there is none."""
        parts = urlsplit(site)
        origin = f"{parts.scheme}://{parts.netloc}"
        try:
            _, xml, _ = await self._get(origin + "/sitemap.xml", TEXT_TYPES | XML_TYPES)
            found = _LOC.findall(xml)
            children = [u for u in found if u.lower().endswith(".xml")][:MAX_CHILD_SITEMAPS]
            for child in children:
                try:
                    _, more, _ = await self._get(child, TEXT_TYPES | XML_TYPES)
                except FetchBlocked:
                    continue
                found += _LOC.findall(more)
        except FetchBlocked:
            return []
        host = parts.hostname
        urls = [
            u
            for u in found
            if not u.lower().endswith(".xml") and same_site(urlsplit(u).hostname, host)
        ]
        return list(dict.fromkeys(urls))[:MAX_SITEMAP_URLS]

    async def _get(self, url: str, allowed: set[str]) -> tuple[str, str, bool]:
        """The final address, the decoded body and whether it was cut at the size limit."""
        robots_cache: dict = {}
        headers = {"User-Agent": self._user_agent, "Accept": "text/html,text/plain"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False, headers=headers
            ) as client:
                current = url
                for _ in range(self._max_redirects + 1):
                    parts = validate_url(current)
                    await check_host(parts.hostname, self._resolver)
                    robots = await self._robots(client, parts, robots_cache)
                    if robots is not None and not robots.can_fetch(self._user_agent, current):
                        raise FetchBlocked("robots", current)
                    async with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise FetchBlocked("redirect", "no location header")
                            current = urljoin(current, location)
                            continue
                        if response.status_code >= 400:
                            raise FetchBlocked("http_status", str(response.status_code))
                        ctype = (
                            response.headers.get("content-type", "").split(";")[0].strip().lower()
                        )
                        if ctype not in allowed:
                            raise FetchBlocked("content_type", ctype or "none")
                        body, truncated = await self._read(response, self._max_bytes)
                        encoding = response.charset_encoding or "utf-8"
                        html = body.decode(encoding, errors="replace")
                        return current, html, truncated
                raise FetchBlocked("too_many_redirects", url)
        except httpx.HTTPError as exc:
            raise FetchBlocked("network", type(exc).__name__) from exc
