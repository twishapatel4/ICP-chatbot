import httpx
import pytest
import respx

from icp_chatbot.enrichment.fetcher import (
    FetchBlocked,
    SafeFetcher,
    is_public_ip,
    validate_url,
)

PUBLIC = "93.184.216.34"
HTML = """<html><head><title>Acme Payroll</title>
<meta name="description" content="Payroll for small businesses">
<script>var secret = 1;</script><style>.x{}</style></head>
<body><nav>Home Pricing</nav><h1>Run payroll in minutes</h1>
<a href="/about">About</a><a href="https://other.test/x">Elsewhere</a><a href="mailto:a@b.c">mail</a>
<footer>Copyright</footer></body></html>"""


def resolver_for(mapping=None):
    mapping = mapping or {}

    async def resolve(host):
        return mapping.get(host, [PUBLIC])

    return resolve


def page(router, url, *, body=HTML, status=200, ctype="text/html; charset=utf-8", **kw):
    return router.get(url).respond(status, text=body, headers={"content-type": ctype}, **kw)


def no_robots(router, host="https://acme.test"):
    router.get(f"{host}/robots.txt").respond(404)


@pytest.mark.parametrize(
    "address,expected",
    [
        ("8.8.8.8", True), (PUBLIC, True), ("2606:4700:4700::1111", True),
        ("127.0.0.1", False), ("10.0.0.5", False), ("172.16.0.1", False),
        ("192.168.1.1", False), ("169.254.169.254", False), ("100.64.0.1", False),
        ("0.0.0.0", False), ("224.0.0.1", False), ("::1", False), ("fe80::1", False),
        ("fc00::1", False), ("::ffff:127.0.0.1", False), ("not-an-ip", False),
    ],
)  # fmt: skip
def test_only_public_addresses_are_allowed(address, expected):
    assert is_public_ip(address) is expected


@pytest.mark.parametrize(
    "url,reason",
    [
        ("file:///etc/passwd", "scheme"), ("ftp://acme.test/", "scheme"),
        ("javascript:alert(1)", "scheme"), ("https://user:pw@acme.test/", "userinfo"),
        ("https://acme.test:8080/", "port"), ("https:///path", "host"), ("", "scheme"),
    ],
)  # fmt: skip
def test_unsafe_addresses_are_rejected(url, reason):
    with pytest.raises(FetchBlocked) as err:
        validate_url(url)
    assert err.value.reason == reason


def test_a_normal_address_is_accepted():
    assert validate_url("https://acme.test/path?q=1").hostname == "acme.test"


@respx.mock
async def test_fetch_extracts_readable_text_and_same_site_links():
    no_robots(respx)
    page(respx, "https://acme.test/")
    result = await SafeFetcher(resolver=resolver_for()).fetch("https://acme.test/")
    assert result.text.startswith("Acme Payroll Payroll for small businesses")
    assert "Run payroll in minutes" in result.text
    for hidden in ("secret", "Home Pricing", "Copyright"):
        assert hidden not in result.text
    assert result.links == ["https://acme.test/about"]  # other host and mailto: dropped
    assert not result.truncated


@respx.mock
async def test_a_host_resolving_to_a_private_address_is_blocked_before_any_request():
    fetcher = SafeFetcher(resolver=resolver_for({"acme.test": ["10.0.0.7"]}))
    with pytest.raises(FetchBlocked) as err:
        await fetcher.fetch("https://acme.test/")
    assert err.value.reason == "private_address"
    assert respx.calls.call_count == 0


@respx.mock
async def test_one_private_address_among_public_ones_blocks_the_host():
    fetcher = SafeFetcher(resolver=resolver_for({"acme.test": [PUBLIC, "192.168.0.9"]}))
    with pytest.raises(FetchBlocked):
        await fetcher.fetch("https://acme.test/")


@respx.mock
@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/", "http://169.254.169.254/latest/", "http://[::1]/"]
)
async def test_literal_private_addresses_are_blocked(url):
    with pytest.raises(FetchBlocked) as err:
        await SafeFetcher(resolver=resolver_for()).fetch(url)
    assert err.value.reason == "private_address"


@respx.mock
async def test_a_redirect_to_a_private_address_is_blocked():
    no_robots(respx)
    respx.get("https://acme.test/").respond(302, headers={"location": "http://internal.test/admin"})
    fetcher = SafeFetcher(resolver=resolver_for({"internal.test": ["10.1.1.1"]}))
    with pytest.raises(FetchBlocked) as err:
        await fetcher.fetch("https://acme.test/")
    assert err.value.reason == "private_address"


@respx.mock
async def test_redirects_are_followed_within_the_limit_and_then_stopped():
    no_robots(respx)
    respx.get("https://acme.test/a").respond(302, headers={"location": "/b"})
    respx.get("https://acme.test/b").respond(302, headers={"location": "/a"})
    with pytest.raises(FetchBlocked) as err:
        await SafeFetcher(resolver=resolver_for(), max_redirects=3).fetch("https://acme.test/a")
    assert err.value.reason == "too_many_redirects"


@respx.mock
async def test_a_redirect_is_followed_to_the_final_page():
    no_robots(respx)
    respx.get("https://acme.test/").respond(301, headers={"location": "/home"})
    page(respx, "https://acme.test/home")
    result = await SafeFetcher(resolver=resolver_for()).fetch("https://acme.test/")
    assert result.url == "https://acme.test/home"


@respx.mock
async def test_oversized_pages_are_truncated():
    no_robots(respx)
    page(respx, "https://acme.test/", body="<p>" + "word " * 5000 + "</p>")
    result = await SafeFetcher(resolver=resolver_for(), max_bytes=1000).fetch("https://acme.test/")
    assert result.truncated and len(result.text) <= 1000


@respx.mock
async def test_text_is_capped_in_characters():
    no_robots(respx)
    page(respx, "https://acme.test/", body="<p>" + "abc " * 5000 + "</p>")
    result = await SafeFetcher(resolver=resolver_for(), max_chars=100).fetch("https://acme.test/")
    assert len(result.text) == 100


@respx.mock
@pytest.mark.parametrize("ctype", ["application/pdf", "image/png", "application/octet-stream", ""])
async def test_non_text_content_is_rejected(ctype):
    no_robots(respx)
    page(respx, "https://acme.test/", ctype=ctype)
    with pytest.raises(FetchBlocked) as err:
        await SafeFetcher(resolver=resolver_for()).fetch("https://acme.test/")
    assert err.value.reason == "content_type"


@respx.mock
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_error_statuses_are_reported(status):
    no_robots(respx)
    page(respx, "https://acme.test/", status=status)
    with pytest.raises(FetchBlocked) as err:
        await SafeFetcher(resolver=resolver_for()).fetch("https://acme.test/")
    assert (err.value.reason, err.value.detail) == ("http_status", str(status))


@respx.mock
async def test_robots_txt_disallow_is_respected():
    respx.get("https://acme.test/robots.txt").respond(200, text="User-agent: *\nDisallow: /")
    with pytest.raises(FetchBlocked) as err:
        await SafeFetcher(resolver=resolver_for()).fetch("https://acme.test/")
    assert err.value.reason == "robots"


@respx.mock
async def test_robots_txt_can_allow_some_paths_and_a_missing_file_allows_all():
    respx.get("https://acme.test/robots.txt").respond(200, text="User-agent: *\nDisallow: /private")
    page(respx, "https://acme.test/public")
    fetcher = SafeFetcher(resolver=resolver_for())
    assert (await fetcher.fetch("https://acme.test/public")).text
    with pytest.raises(FetchBlocked):
        await fetcher.fetch("https://acme.test/private/x")


@respx.mock
async def test_network_errors_become_blocked_results():
    no_robots(respx)
    respx.get("https://acme.test/").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(FetchBlocked) as err:
        await SafeFetcher(resolver=resolver_for()).fetch("https://acme.test/")
    assert err.value.reason == "network"
