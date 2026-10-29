"""Pull LinkedIn's standardized degrees and fields of study into the repo snapshots.

LinkedIn serves these lists only through the Standardized Data API, which needs an approved
developer token. The token is read from an environment variable, never an argument or a file:

    LINKEDIN_TOKEN=... python scripts/sync_linkedin_catalogs.py --token-env LINKEDIN_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from icp_chatbot.linkedin.catalog import DATA_DIR, Option  # noqa: E402

API = "https://api.linkedin.com/v2"
PAGE_SIZE = 100
MAX_PAGES = 1000
LOCALE = "en_US"

# kind -> (API path, snapshot file)
KINDS = {
    "degree": ("/degrees", "degrees.json"),
    "field_of_study": ("/fieldsOfStudy", "fields_of_study.json"),
}


class SyncError(Exception):
    """A sync failure whose message is safe to print (it never carries the token)."""


def redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def fetch_all(
    path: str, token: str, client: httpx.Client, count: int = PAGE_SIZE, max_pages: int = MAX_PAGES
) -> list[dict]:
    """All elements of a paged Standardized Data list, following `paging` until it is exhausted.
    An API that ignores `start` and gives no total is caught: a page with no new ids, or more
    than `max_pages` pages, raises SyncError instead of looping forever."""
    headers = {"Authorization": f"Bearer {token}"}
    rows: list[dict] = []
    seen: set[str] = set()
    start = 0
    for _ in range(max_pages):
        resp = client.get(
            f"{API}{path}", params={"start": start, "count": count}, headers=headers, timeout=30
        )
        resp.raise_for_status()
        body = resp.json()
        page = body.get("elements") or []
        keys = {_row_key(row) for row in page}
        if page and keys <= seen:
            raise SyncError(f"{path}: page at start={start} repeats earlier rows; stopping")
        seen |= keys
        rows.extend(page)
        total = (body.get("paging") or {}).get("total")
        start += len(page)
        if not page or (total is not None and start >= total):
            return rows
    raise SyncError(f"{path}: still paging after {max_pages} pages; stopping")


def _row_key(row: Any) -> str:
    if isinstance(row, dict) and row.get("id") is not None:
        return str(row["id"])
    return json.dumps(row, sort_keys=True, default=str)


def _text(value: Any) -> str | None:
    """English text of a plain string or a LinkedIn localized-string object."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        localized = value.get("localized")
        if isinstance(localized, dict):
            english = localized.get(LOCALE) or next(
                (v for k, v in localized.items() if str(k).startswith("en")), None
            )
            return _text(english)
    return None


def _id(value: Any) -> str | None:
    """Numeric id of an int, a numeric string or a `urn:li:...:N` URN."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).rsplit(":", 1)[-1].strip()
    return text or None


def to_options(kind: str, rows: list[dict]) -> list[Option]:
    """Catalog options for `kind` ("degree" or "field_of_study"). Rows without an id or an
    English name are skipped. A degree's parent is its rollup id; a field of study's is the
    first of its rollup ids. The first row for an id wins."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    options: list[Option] = []
    seen: set[str] = set()
    for row in rows:
        oid = _id(row.get("id"))
        label = _text(row.get("name"))
        if oid is None or label is None or oid in seen:
            continue  # unusable, or an id already taken (first wins)
        seen.add(oid)
        if kind == "degree":
            parent = _id(row.get("rollup"))
            aliases = tuple(a for a in (_text(x) for x in row.get("aliases") or []) if a)
        else:
            rollups = row.get("rollupIds") or []
            parent = _id(rollups[0]) if rollups else None
            aliases = ()
        options.append(Option(facet=kind, id=oid, label=label, parent=parent, match=aliases))
    return options


def option_rows(options: list[Option]) -> list[dict]:
    """The catalog file shape: `{id, label, parent?, match}`."""
    return [
        {
            "id": o.id,
            "label": o.label,
            **({"parent": o.parent} if o.parent else {}),
            "match": list(o.match),
        }
        for o in options
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        allow_abbrev=False,  # no `--token` shorthand
    )
    parser.add_argument(
        "--token-env",
        default="LINKEDIN_TOKEN",
        help="name of the environment variable holding the LinkedIn API token",
    )
    parser.add_argument("--out-dir", type=Path, default=DATA_DIR, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        sys.exit(f"Set the {args.token_env} environment variable to a LinkedIn API token.")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in token):
        # Never echo the value: it is a secret and may not even be one.
        sys.exit(f"{args.token_env} holds whitespace or control characters; fix the token.")

    try:
        with httpx.Client() as client:
            fetched = {
                kind: to_options(kind, fetch_all(path, token, client))
                for kind, (path, _) in KINDS.items()
            }
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        msg = f"LinkedIn returned HTTP {status} for {exc.request.url.path}."
        if status == 429:
            retry = exc.response.headers.get("Retry-After")
            msg += f" Rate limited; Retry-After: {retry}." if retry else " Rate limited."
        sys.exit(redact(msg, token))
    except (httpx.HTTPError, SyncError) as exc:
        # Nothing is written unless both lists came back. httpx can quote header values in its
        # messages, so every printed error has the token replaced.
        sys.exit(redact(f"LinkedIn sync failed: {type(exc).__name__}: {exc}", token))

    for kind, options in fetched.items():
        if not options:
            sys.exit(f"LinkedIn returned no usable {kind} rows; nothing written.")
    files = {
        args.out_dir / KINDS[kind][1]: json.dumps(option_rows(options), indent=2) + "\n"
        for kind, options in fetched.items()
    }
    try:
        write_all(files)
    except OSError as exc:
        sys.exit(redact(f"Could not write the snapshots, old files kept: {exc}", token))
    for kind, options in fetched.items():
        print(f"{args.out_dir / KINDS[kind][1]}: {len(options)} rows")
    return 0


def _write_temp(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def write_all(files: dict[Path, str]) -> None:
    """Write every file or none: each goes to `<name>.tmp` beside its target, then all are
    moved into place with os.replace. Temp files are removed on any error or interrupt."""
    temps = {target: target.with_name(target.name + ".tmp") for target in files}
    try:
        next(iter(files)).parent.mkdir(parents=True, exist_ok=True)
        for target, text in files.items():
            _write_temp(temps[target], text)
        for target, temp in temps.items():
            os.replace(temp, target)
    finally:
        for temp in temps.values():
            temp.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
