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
LOCALE = "en_US"

# kind -> (API path, snapshot file)
KINDS = {
    "degree": ("/degrees", "degrees.json"),
    "field_of_study": ("/fieldsOfStudy", "fields_of_study.json"),
}


def fetch_all(path: str, token: str, client: httpx.Client, count: int = PAGE_SIZE) -> list[dict]:
    """All elements of a paged Standardized Data list, following `paging` until it is exhausted."""
    headers = {"Authorization": f"Bearer {token}"}
    rows: list[dict] = []
    start = 0
    while True:
        resp = client.get(
            f"{API}{path}", params={"start": start, "count": count}, headers=headers, timeout=30
        )
        resp.raise_for_status()
        body = resp.json()
        page = body.get("elements") or []
        rows.extend(page)
        total = (body.get("paging") or {}).get("total")
        start += len(page)
        if not page or (total is not None and start >= total):
            return rows


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
    first of its rollup ids."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    options: list[Option] = []
    for row in rows:
        oid = _id(row.get("id"))
        label = _text(row.get("name"))
        if oid is None or label is None:
            continue
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

    try:
        with httpx.Client() as client:
            fetched = {
                kind: to_options(kind, fetch_all(path, token, client))
                for kind, (path, _) in KINDS.items()
            }
    except httpx.HTTPError as exc:
        # Nothing is written unless both lists came back; the message never carries the token.
        sys.exit(f"LinkedIn request failed: {type(exc).__name__}: {exc}")

    for kind, options in fetched.items():
        if not options:
            sys.exit(f"LinkedIn returned no usable {kind} rows; nothing written.")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for kind, options in fetched.items():
        path = args.out_dir / KINDS[kind][1]
        path.write_text(json.dumps(option_rows(options), indent=2) + "\n", encoding="utf-8")
        print(f"{path}: {len(options)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
