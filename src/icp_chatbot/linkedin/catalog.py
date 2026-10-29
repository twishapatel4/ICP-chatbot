"""Static LinkedIn option catalogs (industry, size, seniority, function, country, ...)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

log = logging.getLogger(__name__)

FACETS = (
    "industry",
    "company_size",
    "seniority",
    "function",
    "country",
    "degree",
    "field_of_study",
)

SLOT_FACETS: dict[str, str] = {
    "industries": "industry",
    "current_industries": "industry",
    "company_size": "company_size",
    "current_company_size": "company_size",
    "buyer_seniority": "seniority",
    "target_seniority": "seniority",
    "buyer_departments": "function",
    "target_functions": "function",
    "lead_geographies": "country",
    "company_hq_geographies": "country",
    "candidate_locations": "country",
    "current_company_hq": "country",
    "education_level": "degree",
    "field_of_study": "field_of_study",
}

_FILES = {
    "industry": "industries.json",
    "company_size": "company_sizes.json",
    "seniority": "seniorities.json",
    "function": "functions.json",
    "country": "countries.json",
    "degree": "degrees.json",
    "field_of_study": "fields_of_study.json",
}

# Snapshots that only exist after scripts/sync_linkedin_catalogs.py ran with a LinkedIn token.
_OPTIONAL = frozenset({"degree", "field_of_study"})

# Two industry labels differ between LinkedIn's Label column and its Hierarchy path.
_INDUSTRY_ALIASES = {
    "3251": ("Climate Technology Products Manufacturing",),
    "3133": ("Media and Telecommunications",),
}


@dataclass(frozen=True)
class Option:
    facet: str
    id: str
    label: str
    level: int = 1
    parent: str | None = None
    match: tuple[str, ...] = ()
    # False for a row the data marks `"pick": false`: kept for matching, not offered as a
    # quick-pick in a picker (the "Other territories" region).
    selectable: bool = True


def _norm(text: str) -> str:
    return " ".join(str(text).casefold().split())


class OptionCatalog:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._options: dict[str, list[Option]] = {}
        self._lookup: dict[str, dict[str, Option]] = {}
        self._by_id: dict[str, dict[str, Option]] = {}
        for facet, filename in _FILES.items():
            path = data_dir / filename
            if not path.exists():
                continue
            if facet in _OPTIONAL:
                opts = self._load_optional(facet, path)
                if not opts:
                    continue  # empty or malformed: treated as absent, so has() stays False
            else:
                rows = json.loads(path.read_text(encoding="utf-8"))
                opts = [self._build(facet, row) for row in rows]
            self._options[facet] = opts
            lookup: dict[str, Option] = {}
            for opt in opts:
                for key in (opt.label, *opt.match):
                    lookup.setdefault(_norm(key), opt)
            self._lookup[facet] = lookup
            self._by_id[facet] = {opt.id: opt for opt in opts}

    @classmethod
    def _load_optional(cls, facet: str, path: Path) -> list[Option]:
        """Options of a synced snapshot, or [] when the file is empty, not a JSON list, or has
        a row without an id and a label (a half-written or hand-edited file must not enable
        a card that cannot be answered). An unusable file is logged and treated as absent."""
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(rows, list)
                and rows
                and all(
                    isinstance(r, dict) and r.get("id") not in (None, "") and r.get("label")
                    for r in rows
                )
            ):
                return [cls._build(facet, row) for row in rows]
        except (ValueError, TypeError, OSError):
            pass
        log.warning(
            "%s is empty or malformed; the %s catalog is treated as absent", path.name, facet
        )
        return []

    @staticmethod
    def _build(facet: str, row: dict) -> Option:
        oid = str(row["id"])
        match = tuple(str(m) for m in row.get("match", ()))
        if facet == "industry":
            match += _INDUSTRY_ALIASES.get(oid, ())
        return Option(
            facet=facet,
            id=oid,
            label=row["label"],
            level=int(row.get("level", 1)),
            parent=row.get("parent"),
            match=match,
            selectable=bool(row.get("pick", True)),
        )

    def has(self, facet: str) -> bool:
        return facet in self._options

    def options(self, facet: str) -> list[Option]:
        return list(self._options.get(facet, []))

    def search(self, facet: str, q: str, limit: int = 50) -> list[Option]:
        needle = _norm(q)
        opts = self._options.get(facet, [])
        if not needle:
            return opts[:limit]
        starts = [o for o in opts if _norm(o.label).startswith(needle)]
        rest = [o for o in opts if needle in _norm(o.label) and o not in starts]
        return (starts + rest)[:limit]

    def region_countries(self, text: str) -> list[Option] | None:
        """The countries of a region ("Europe"), or None when `text` is not a region."""
        found = self.resolve("country", text)
        if found is None or found.level != 1:
            return None
        return [o for o in self._options["country"] if o.parent == found.label]

    def resolve(self, facet: str, text: str) -> Option | None:
        """Labels and match aliases match case-insensitively; ids match exactly ("IN", not "in"),
        so short free text such as "in" or "no" is not read as a code."""
        found = self._lookup.get(facet, {}).get(_norm(text))
        return found or self._by_id.get(facet, {}).get(str(text).strip())


@lru_cache(maxsize=1)
def default_catalog() -> OptionCatalog:
    return OptionCatalog()
