"""Build the job-title snapshot (src/icp_chatbot/linkedin/data/titles.json) from ESCO.

ESCO is the European Commission's open occupation classification (free to reuse with
attribution, https://esco.ec.europa.eu). Every occupation's preferred English name and each of
its English alternative labels becomes one title ("software developer", "software engineer"), so
the list reads like the titles people put on a profile. No token is needed:

    python scripts/sync_esco_titles.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from icp_chatbot.linkedin.catalog import DATA_DIR  # noqa: E402

EXTRAS = Path(__file__).with_name("linkedin_extra_titles.txt")
API = "https://ec.europa.eu/esco/api/search"
PAGE = 10  # a full page is about 35 KB per occupation, bigger pages time out
RETRIES = 8
WORKERS = 3
# fetched pages are kept here, so a run that dies on a flaky page resumes instead of starting over
CACHE = Path(tempfile.gettempdir()) / "esco_title_pages"
OUT = DATA_DIR / "titles.json"


def fetch_page(client: httpx.Client, offset: int) -> dict:
    cached = CACHE / f"{PAGE}-{offset}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    try:
        body = _fetch(client, offset)
    except httpx.HTTPError:
        body = _by_item(client, offset)
    CACHE.mkdir(exist_ok=True)
    cached.write_text(json.dumps(body), encoding="utf-8")
    return body


def _by_item(client: httpx.Client, page: int) -> dict:
    """A page ESCO cannot serve, asked one occupation at a time (with limit 1 the offset is the
    item's position): the one that fails is skipped, the rest are kept."""
    results = []
    for i in range(page * PAGE, (page + 1) * PAGE):
        try:
            results += _fetch(client, i, limit=1)["_embedded"]["results"]
        except (httpx.HTTPError, KeyError):
            print(f"skipped occupation {i}: ESCO fails on it", file=sys.stderr)
    return {"_embedded": {"results": results}}


def _fetch(client: httpx.Client, offset: int, limit: int = PAGE) -> dict:
    params = {"language": "en", "type": "occupation", "text": "", "full": "true",
              "limit": limit, "offset": offset}
    for attempt in range(RETRIES):
        try:
            resp = client.get(API, params=params, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError):
            if attempt == RETRIES - 1:
                raise
            time.sleep(3 * (attempt + 1))
    raise AssertionError("unreachable")


def tidy(label: str) -> str:
    text = " ".join(label.split())
    return text[:1].upper() + text[1:]


def extra_titles() -> list[str]:
    """Titles common on LinkedIn that ESCO lacks (Founder, VP of Sales, ...), ranked first."""
    lines = EXTRAS.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


ONET = "https://www.onetcenter.org/dl_files/database/db_29_1_text/"  # O*NET, CC BY 4.0


def onet_titles(client: httpx.Client) -> tuple[list[str], list[str]]:
    """(occupation titles, alternate/reported titles) from the O*NET database."""
    def column(name: str, col: str) -> list[str]:
        r = client.get(ONET + name.replace(" ", "%20"), timeout=120, follow_redirects=True)
        r.raise_for_status()
        lines = r.text.splitlines()
        i = lines[0].split("\t").index(col)
        return [ln.split("\t")[i] for ln in lines[1:] if ln.strip()]

    return column("Occupation Data.txt", "Title"), column("Alternate Titles.txt", "Alternate Title")


def merge_extras() -> int:
    """Rebuild the snapshot order without calling ESCO: curated extras, O*NET occupation
    titles, the existing ESCO titles, then O*NET alternate titles."""
    old = [r for r in json.loads(OUT.read_text(encoding="utf-8")) if r["id"].split("~")[0] not in ("extra", "onet")]
    with httpx.Client() as client:
        primary, alternate = onet_titles(client)
    rows: list[dict] = []
    seen: set[str] = set()

    def add(oid: str, label: str) -> None:
        label = tidy(label)
        key = label.casefold()
        if label and len(label) <= 80 and key not in seen:
            seen.add(key)
            rows.append({"id": oid, "label": label})

    for i, t in enumerate(extra_titles()):
        add(f"extra~{i}", t)
    for i, t in enumerate(primary):
        add(f"onet~p{i}", t)
    for r in old:
        add(r["id"], r["label"])
    for i, t in enumerate(alternate):
        add(f"onet~a{i}", t)
    OUT.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{OUT}: {len(rows)} titles")
    return 0


def to_rows(occupations: list[dict]) -> list[dict]:
    """One row per distinct title: all preferred names first, then alternative labels. The
    first row for a title (compared ignoring case) wins."""
    rows: list[dict] = []
    seen: set[str] = set()

    def add(oid: str, label: str) -> None:
        label = tidy(label)
        key = label.casefold()
        if label and len(label) <= 80 and key not in seen:
            seen.add(key)
            rows.append({"id": oid, "label": label})

    for i, label in enumerate(extra_titles()):
        add(f"extra~{i}", label)
    for occ in occupations:
        add(occ["uri"].rsplit("/", 1)[-1], (occ.get("preferredLabel") or {}).get("en") or occ["title"])
    for occ in occupations:
        uid = occ["uri"].rsplit("/", 1)[-1]
        for i, alt in enumerate((occ.get("alternativeLabel") or {}).get("en") or []):
            add(f"{uid}~{i}", alt)
    return rows


def main() -> int:
    if "--extras-only" in sys.argv:
        return merge_extras()
    with httpx.Client() as client:
        first = fetch_page(client, 0)
        total = int(first["total"])
        offsets = range(1, -(-total // PAGE))  # ESCO's offset counts pages, not items
        with ThreadPoolExecutor(WORKERS) as pool:
            pages = [first, *pool.map(lambda o: fetch_page(client, o), offsets)]
    occupations = [r for p in pages for r in p["_embedded"]["results"]]
    if len(occupations) < total * 0.98:  # a few that ESCO cannot serve are skipped; many is a failure
        sys.exit(f"ESCO gave {len(occupations)} of {total} occupations; nothing written.")
    rows = to_rows(occupations)
    OUT.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{OUT}: {len(rows)} titles from {len(occupations)} occupations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
