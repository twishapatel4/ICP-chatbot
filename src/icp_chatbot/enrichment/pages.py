"""Choosing which pages of a company's website are worth reading (plain code, no model call).

The sitemap lists every page; a handful say what the company sells, who buys it and how it
works. Pages are ranked by what their address says, one page per theme first, so the few we
read cover different ground.
"""

import re
from urllib.parse import urlsplit

from icp_chatbot.enrichment.fetcher import same_site

# (theme, weight, words in the address). Customers and offering say the most about the ICP.
THEMES = (
    (
        "customers",
        5,
        ("customer", "case-stud", "client", "success-stor", "who-we-serve", "testimonial"),
    ),
    (
        "offering",
        5,
        ("product", "solution", "service", "platform", "what-we-do", "offering", "feature"),
    ),
    ("audience", 4, ("industr", "use-case", "sector", "segment", "for-")),
    ("about", 3, ("about", "company", "our-story", "who-we-are")),
    ("pricing", 3, ("pricing", "plans")),
    ("process", 2, ("how-it-works", "how-we-work", "process", "approach")),
)
SKIP = re.compile(
    r"(blog|news|press|career|job|privacy|terms|legal|cookie|login|sign-?in|sign-?up|register|"
    r"contact|support|help|docs|faq|events?|webinar|podcast|tag|categor|author|sitemap|"
    r"\.(pdf|jpe?g|png|gif|svg|zip|xml)$)",
    re.IGNORECASE,
)
MAX_DEPTH = 3
LANGUAGE_PREFIX = re.compile(r"^/[a-z]{2}(-[a-z]{2})?/")


def score_page(url: str) -> tuple[str, float] | None:
    """The page's best theme and score, or None when it is not worth reading."""
    path = urlsplit(url).path.lower().rstrip("/")
    path = LANGUAGE_PREFIX.sub("/", path)
    depth = len([p for p in path.split("/") if p])
    if not path or depth > MAX_DEPTH or SKIP.search(path):
        return None
    best: tuple[str, float] | None = None
    for theme, weight, words in THEMES:
        if any(word in path for word in words):
            score = weight - 0.5 * (depth - 1)  # shallow pages are the main ones
            if best is None or score > best[1]:
                best = (theme, score)
    return best


def rank_pages(urls: list[str], skip: str, limit: int) -> list[str]:
    """Up to `limit` pages to read: the best page of each theme first, then the best of the rest."""
    home = urlsplit(skip)
    scored: list[tuple[str, str, float]] = []
    for url in dict.fromkeys(urls):
        parts = urlsplit(url)
        if not same_site(parts.hostname, home.hostname):
            continue  # only this company's own site
        if parts.path.rstrip("/") == home.path.rstrip("/"):
            continue  # the page we already have
        if (hit := score_page(url)) is not None:
            scored.append((url, hit[0], hit[1]))
    scored.sort(key=lambda t: (-t[2], len(t[0])))
    chosen: list[str] = []
    themes: set[str] = set()
    for url, theme, _ in scored:  # one page per theme first
        if theme not in themes and len(chosen) < limit:
            chosen.append(url)
            themes.add(theme)
    for url, _, _ in scored:
        if url not in chosen and len(chosen) < limit:
            chosen.append(url)
    return chosen
