from icp_chatbot.config import Settings
from icp_chatbot.domain.signup import SignupInfo
from icp_chatbot.enrichment.fetcher import FetchedPage, SafeFetcher
from icp_chatbot.enrichment.pages import rank_pages, score_page
from icp_chatbot.enrichment.service import EnrichmentService, SeedItem, SeedProfile
from icp_chatbot.llm.fake import FakeLLM
from icp_chatbot.versions import default_config

SITE = "https://acme.test/"
URLS = [
    "https://acme.test/",
    "https://acme.test/blog/ten-tips",
    "https://acme.test/careers",
    "https://acme.test/privacy",
    "https://acme.test/customers",
    "https://acme.test/customers/globex-case-study",
    "https://acme.test/products/payroll",
    "https://acme.test/products/payroll/features/overview/deep/page",
    "https://acme.test/about-us",
    "https://acme.test/pricing",
    "https://acme.test/industries/retail",
    "https://other.test/product",
    "https://acme.test/brochure.pdf",
]


def test_rank_pages_picks_one_page_per_theme_and_skips_noise():
    chosen = rank_pages(URLS, SITE, 4)
    assert len(chosen) == 4
    assert "https://acme.test/customers" in chosen
    assert "https://acme.test/products/payroll" in chosen
    assert not any(x in " ".join(chosen) for x in ("blog", "careers", "privacy", "brochure"))
    assert SITE not in chosen


def test_rank_pages_fills_up_from_the_best_of_the_rest_when_themes_run_out():
    urls = ["https://a.test/products/a", "https://a.test/products/b", "https://a.test/services"]
    assert len(rank_pages(urls, "https://a.test/", 3)) == 3


def test_deep_and_irrelevant_pages_score_nothing():
    assert score_page("https://a.test/blog/customers-love-us") is None
    assert score_page("https://a.test/a/b/c/d/products") is None
    assert score_page("https://a.test/fr/produits") is None
    assert score_page("https://a.test/en/pricing")[0] == "pricing"


class SitemapFetcher:
    def __init__(self, sitemap_urls, pages):
        self._sitemap, self._pages, self.fetched = sitemap_urls, pages, []

    async def sitemap(self, site):
        return self._sitemap

    async def fetch(self, url):
        self.fetched.append(url)
        return self._pages[url]


async def test_the_pages_to_read_come_from_the_sitemap_when_there_is_one():
    pages = {
        u: FetchedPage(u, f"text of {u}", [], False) for u in (SITE, *rank_pages(URLS, SITE, 3))
    }
    pages["https://acme.test"] = pages[SITE]
    fetcher = SitemapFetcher(URLS, pages)
    llm = FakeLLM({"seed": [SeedProfile(items=[SeedItem(slot="product", values=["Payroll"])])]})
    config = default_config(Settings(_env_file=None))
    outcome = await EnrichmentService(llm, fetcher).run(
        SignupInfo("Acme", website="acme.test"), config
    )
    assert outcome.status == "ok" and len(outcome.pages) == 4
    assert "https://acme.test/blog/ten-tips" not in fetcher.fetched


class FakeResponseFetcher(SafeFetcher):
    def __init__(self, bodies):
        super().__init__()
        self._bodies = bodies

    async def _get(self, url, allowed):
        if url not in self._bodies:
            from icp_chatbot.enrichment.fetcher import FetchBlocked

            raise FetchBlocked("http_status", "404")
        return url, self._bodies[url], False


async def test_sitemap_reads_locations_follows_an_index_and_keeps_only_this_host():
    index = "<sitemapindex><sitemap><loc>https://acme.test/pages.xml</loc></sitemap></sitemapindex>"
    child = (
        "<urlset><url><loc>https://acme.test/product</loc></url>"
        "<url><loc><![CDATA[https://acme.test/about]]></loc></url>"
        "<url><loc>https://evil.test/x</loc></url></urlset>"
    )
    fetcher = FakeResponseFetcher(
        {"https://acme.test/sitemap.xml": index, "https://acme.test/pages.xml": child}
    )
    assert await fetcher.sitemap("https://acme.test/") == [
        "https://acme.test/product",
        "https://acme.test/about",
    ]


async def test_a_missing_sitemap_gives_an_empty_list():
    assert await FakeResponseFetcher({}).sitemap("https://acme.test/") == []


def test_www_and_the_bare_domain_count_as_the_same_site():
    urls = ["https://acme.test/products/a", "https://www.acme.test/about"]
    assert len(rank_pages(urls, "https://www.acme.test/", 3)) == 2


async def test_a_sitemap_written_for_the_bare_domain_is_used_from_the_www_site():
    xml = "<urlset><url><loc>https://acme.test/product</loc></url></urlset>"
    fetcher = FakeResponseFetcher({"https://www.acme.test/sitemap.xml": xml})
    assert await fetcher.sitemap("https://www.acme.test/") == ["https://acme.test/product"]
