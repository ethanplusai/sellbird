"""Companies House — a UK company's current directors, free with a key.

The public data API (https://developer.company-information.service.gov.uk)
is free: register, create an application, and put its REST key in .env as
COMPANIES_HOUSE_API_KEY. It allows 600 requests per 5 minutes; Sellbird makes
one per company, and keeps each answer for ``CACHE_DAYS`` in
``data/companies_house.json`` so a company is never asked about twice in a
month.

Used by Scout for companies a register (CQC) linked to a company number:
the active directors of a small care provider are usually its owners, and
the people who sign off training spend.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx

from sellbird.collectors.cqc import split_registry_name

logger = logging.getLogger(__name__)

API = "https://api.company-information.service.gov.uk"
CACHE_DAYS = 30
TIMEOUT = httpx.Timeout(15.0)
# Roles that run the company. Secretaries and corporate officers don't.
DIRECTOR_ROLES = frozenset({"director", "llp-designated-member", "llp-member"})
_NUMBER = re.compile(r"^[A-Z0-9]{8}$")


def normalize_number(value: str) -> str:
    """"6944493" -> "06944493"; "sc123456" -> "SC123456"; junk -> ""."""
    value = re.sub(r"\s+", "", value or "").upper()
    if value.isdigit() and len(value) < 8:
        value = value.zfill(8)
    return value if _NUMBER.match(value) else ""


def directors_from_officers(payload: dict, number: str) -> list[dict]:
    """Active natural-person directors from an /officers response."""
    out: list[dict] = []
    for item in (payload or {}).get("items") or []:
        if item.get("resigned_on"):
            continue
        if (item.get("officer_role") or "").lower() not in DIRECTOR_ROLES:
            continue
        # Corporate directors have no date of birth; a person always does.
        if not item.get("date_of_birth"):
            continue
        name = split_registry_name(item.get("name") or "")
        if not name:
            continue
        out.append({
            "first_name": name[0], "last_name": name[1], "title": "Director",
            "source": "Companies House",
            "evidence_url": f"https://find-and-update.company-information.service.gov.uk"
                            f"/company/{number}/officers",
        })
    return out


class CompaniesHouse:
    def __init__(self, api_key: str, cache_path: Path | None = None):
        self.api_key = (api_key or "").strip()
        if cache_path is None:
            from sellbird.paths import PROJECT_ROOT
            cache_path = PROJECT_ROOT / "data" / "companies_house.json"
        self.cache_path = cache_path
        self._cache: dict | None = None

    def _load(self) -> dict:
        if self._cache is None:
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache or {}), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError as e:
            logger.debug(f"Companies House cache not saved: {e}")

    async def directors(self, company_number: str,
                        client: httpx.AsyncClient | None = None) -> list[dict]:
        """The company's current directors ([] when unknown or on any failure)."""
        number = normalize_number(company_number)
        if not number or not self.api_key:
            return []
        cache = self._load()
        hit = cache.get(number)
        if hit and time.time() - hit.get("at", 0) < CACHE_DAYS * 86400:
            return hit.get("directors") or []
        url = f"{API}/company/{number}/officers"
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=TIMEOUT) as own:
                    resp = await own.get(url, params={"items_per_page": 50},
                                         auth=(self.api_key, ""))
            else:
                resp = await client.get(url, params={"items_per_page": 50},
                                        auth=(self.api_key, ""), timeout=TIMEOUT)
        except httpx.HTTPError as e:
            logger.debug(f"Companies House request failed for {number}: {e}")
            return []
        if resp.status_code == 404:
            people: list[dict] = []
        elif resp.status_code != 200:
            # 401 is a bad key, 429 the rate limit: neither is an answer to cache.
            logger.warning(f"Companies House answered {resp.status_code} for {number}")
            return []
        else:
            try:
                people = directors_from_officers(resp.json(), number)
            except ValueError:
                return []
        cache[number] = {"at": time.time(), "directors": people}
        self._save()
        return people
