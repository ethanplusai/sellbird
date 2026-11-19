"""Companies House by SIC code — UK care companies no regulator lists.

CQC (and the Scottish, Welsh and Northern Irish regulators) list the care
services they register. Plenty of care businesses need no registration:
companionship and home-help agencies, sitting services, day centres and
introductory agencies. The one free UK-wide list of them is Companies House,
where every company declares what it does as SIC codes.

This reads the public data API's advanced company search
(``/advanced-search/companies``), with the same free key as
``harvey.integrations.companies_house`` (``COMPANIES_HOUSE_API_KEY``). It asks
for active companies with the care SIC codes, optionally near a place, and
pages through the answer while staying under the API's limit of 600
requests per 5 minutes. Each answer is kept for ``CACHE_DAYS`` in
``data/companies_house_sic/`` so a rerun asks nothing.

What it can't tell you:

* No website, phone or email: Companies House doesn't hold them. Scout has
  to find the website from the company name.
* The place is the registered office, which is often the accountant's or a
  formation agent's address, not where the care is given. The ``location``
  filter is approximate: it matches words in that address.
* SIC codes are self-declared and never checked. 86900 especially ("other
  human health activities") is mostly therapists and clinics, so it is off
  by default.

Sole traders and ordinary partnerships aren't on Companies House at all,
which suits PECR: they need consent before a cold email, and every record
here is a corporate body (company, LLP or CIO).

Every record is a ``cqc.Location`` so discovery can treat it like a
register entry: id ``ch-<number>``, ``company_number`` filled, the
registered office as the address, ``ownership`` "Organisation", and the
SIC codes' meanings as ``services``. ``drop_known`` removes companies a
regulator already listed, matched by company number.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import httpx

from harvey.collectors.cqc import Location
from harvey.integrations.companies_house import API, normalize_number

logger = logging.getLogger(__name__)

SEARCH_URL = f"{API}/advanced-search/companies"
COMPANY_URL = "https://find-and-update.company-information.service.gov.uk/company/{number}"
CACHE_DAYS = 7
TIMEOUT = httpx.Timeout(60.0)

# The SIC codes a care business uses, and the plain name Harvey stores.
SIC_SERVICES = {
    "87100": "residential nursing care",
    "87200": "residential care (learning disability, mental health, substance misuse)",
    "87300": "residential care (elderly and disabled)",
    "88100": "non-residential care (elderly and disabled)",
    "88990": "other social work without accommodation",
    "86900": "other human health activities",
}
# 88100 is the home-help, companionship, sitting and day-centre code; the
# residential codes catch care homes a regulator missed or hasn't listed
# yet. 86900 is opt-in (see the module docstring).
DEFAULT_SIC_CODES = ("88100", "88990", "87300", "87100", "87200")

# Company types that are a corporate body, so B2B cold email rules apply.
# Anything else (overseas entities, royal charters, unregistered bodies)
# is left out as unlikely to be a care business Harvey can email.
CORPORATE_TYPES = frozenset({
    "ltd", "private-unlimited", "private-limited-guarant-nsc",
    "private-limited-guarant-nsc-limited-exemption",
    "private-limited-shares-section-30-exemption", "private-unlimited-nsc",
    "plc", "llp", "charitable-incorporated-organisation",
    "scottish-charitable-incorporated-organisation",
    "registered-society-non-jurisdictional",
})

# The API returns at most this many per request, and answers 500 when
# start_index + size passes MAX_RESULTS; a bigger answer is split by
# incorporation date (EARLIEST to today) until each part fits.
PAGE_SIZE = 5000
MAX_RESULTS = 10000
EARLIEST = date(1850, 1, 1)

# 600 requests per 5 minutes, shared with every other Companies House call
# the key makes. Stay well under it.
RATE_LIMIT = 500
RATE_WINDOW = 300.0


class RateLimiter:
    """At most ``limit`` calls in any ``window`` seconds (sliding window)."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW,
                 clock=time.monotonic, sleep=asyncio.sleep):
        self.limit, self.window = limit, window
        self._clock, self._sleep = clock, sleep
        self._calls: deque[float] = deque()

    async def wait(self) -> None:
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.window:
                self._calls.popleft()
            if len(self._calls) < self.limit:
                self._calls.append(now)
                return
            await self._sleep(self.window - (now - self._calls[0]) + 0.01)


def services_for_codes(codes: Iterable[str]) -> list[str]:
    """The plain names of the care SIC codes in ``codes``, in code order."""
    seen = []
    for code in codes or ():
        name = SIC_SERVICES.get(str(code).strip())
        if name and name not in seen:
            seen.append(name)
    return seen


def location_from_item(item: dict) -> Location | None:
    """One advanced-search result as a Location, or None when it doesn't fit."""
    if (item.get("company_status") or "").lower() != "active":
        return None
    if (item.get("company_type") or "").lower() not in CORPORATE_TYPES:
        return None
    number = normalize_number(item.get("company_number") or "")
    if not number:
        return None
    services = services_for_codes(item.get("sic_codes") or [])
    if not services:
        return None
    office = item.get("registered_office_address") or {}
    street = ", ".join(x for x in (
        (office.get("premises") or "").strip(),
        (office.get("address_line_1") or "").strip(),
        (office.get("address_line_2") or "").strip(),
    ) if x)
    return Location(
        id=f"ch-{number}",
        name=(item.get("company_name") or "").strip(),
        address=street,
        city=(office.get("locality") or "").strip(),
        postcode=(office.get("postal_code") or "").strip().upper(),
        local_authority=(office.get("region") or "").strip(),
        services=services,
        provider_name=(item.get("company_name") or "").strip(),
        company_number=number,
        ownership="Organisation",
    )


def company_url(loc: Location) -> str:
    """The company's public Companies House page."""
    return COMPANY_URL.format(number=loc.company_number)


def known_numbers(locations: Iterable[Location]) -> set[str]:
    """The normalised company numbers of records a regulator already gave."""
    out = set()
    for loc in locations or ():
        number = normalize_number(getattr(loc, "company_number", "") or "")
        if number:
            out.add(number)
    return out


def drop_known(locations: Iterable[Location], known: Iterable[str]) -> list[Location]:
    """``locations`` minus companies whose number is in ``known``.

    ``known`` is any iterable of company numbers, in any format ("6944493",
    "06944493", "sc123456"); pass ``known_numbers(cqc_locations)`` to drop
    everything a register already listed.
    """
    have = {n for n in (normalize_number(str(x)) for x in known or ()) if n}
    return [loc for loc in locations if normalize_number(loc.company_number) not in have]


def _place(place: str) -> str:
    """"Leeds, West Yorkshire" -> "Leeds": the API matches address words."""
    return (place or "").split(",")[0].strip()


class CompaniesHouseSic:
    """Active UK companies with care SIC codes, from the advanced search."""

    def __init__(self, api_key: str, cache_dir: Path | None = None,
                 limiter: RateLimiter | None = None, use_cache: bool = True):
        self.api_key = (api_key or "").strip()
        if cache_dir is None and use_cache:
            from harvey.paths import PROJECT_ROOT
            cache_dir = PROJECT_ROOT / "data" / "companies_house_sic"
        self.cache_dir = cache_dir if use_cache else None
        self.limiter = limiter or RateLimiter()
        self.requests_made = 0

    # ── cache ──

    def _cache_file(self, codes: tuple[str, ...], place: str) -> Path | None:
        if self.cache_dir is None:
            return None
        key = json.dumps([sorted(codes), place.lower()])
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")

    def _read_cache(self, path: Path | None) -> list[Location] | None:
        if path is None or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > CACHE_DAYS * 86400:
            return None
        try:
            return [Location(**x) for x in json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self, path: Path | None, locations: list[Location]) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(x) for x in locations]), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            logger.debug(f"companies_house_sic: cache not saved: {e}")

    # ── the API ──

    async def _get(self, client: httpx.AsyncClient, params: dict) -> dict | None:
        """One search page; None when there are no results; raises on failure."""
        for attempt in range(3):
            await self.limiter.wait()
            self.requests_made += 1
            resp = await client.get(SEARCH_URL, params=params,
                                    auth=(self.api_key, ""), timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                # The advanced search answers 404 when nothing matches.
                return None
            if resp.status_code == 429 and attempt < 2:
                wait = _retry_after(resp)
                logger.warning(f"companies_house_sic: rate limited, waiting {wait:.0f}s")
                await self.limiter._sleep(wait)
                continue
            if resp.status_code == 401:
                raise RuntimeError("Companies House refused the key (401). Check "
                                   "COMPANIES_HOUSE_API_KEY in .env.")
            raise RuntimeError(f"Companies House search answered {resp.status_code}")
        raise RuntimeError("Companies House kept rate-limiting the search")

    async def search(self, client: httpx.AsyncClient | None = None,
                     sic_codes: Iterable[str] = DEFAULT_SIC_CODES,
                     place: str = "", limit: int | None = None) -> list[Location]:
        """Active care companies with any of ``sic_codes``, near ``place``.

        ``place`` is matched against the registered office address ("Leeds"
        or "Leeds, West Yorkshire"; only the part before the first comma is
        sent). With no place it is the whole UK. The API pages through
        ``MAX_RESULTS`` at most per query, so a bigger answer is split by
        incorporation date until each part fits. ``limit`` stops early.
        Raises RuntimeError when there is no key or the API fails; returns
        [] when nothing matches.
        """
        if not self.api_key:
            raise RuntimeError("Companies House needs COMPANIES_HOUSE_API_KEY in .env "
                               "(free: developer.company-information.service.gov.uk).")
        codes = tuple(c for c in (str(x).strip() for x in sic_codes) if c in SIC_SERVICES)
        if not codes:
            raise RuntimeError(f"No care SIC codes given; use some of {sorted(SIC_SERVICES)}")
        where = _place(place)
        cache = self._cache_file(codes, where)
        cached = self._read_cache(cache)
        if cached is not None:
            return cached[:limit] if limit else cached

        if client is None:
            async with httpx.AsyncClient() as own:
                found, complete = await self._search_all(own, codes, where, limit)
        else:
            found, complete = await self._search_all(client, codes, where, limit)
        if complete:
            # A list cut short by ``limit`` would be wrong for a bigger ask.
            self._write_cache(cache, found)
        return found[:limit] if limit else found

    async def _search_all(self, client, codes, where,
                          limit: int | None) -> tuple[list[Location], bool]:
        """(locations, complete): complete when every match was paged through."""
        base = {"sic_codes": ",".join(codes), "company_status": "active"}
        if where:
            base["location"] = where
        out: list[Location] = []
        seen: set[str] = set()

        def full() -> bool:
            return bool(limit) and len(out) >= limit

        async def collect(lo: date | None, hi: date | None) -> bool:
            """Page through one date range; False when ``limit`` cut it short."""
            params = dict(base)
            if lo is not None:
                params["incorporated_from"] = lo.isoformat()
            if hi is not None:
                params["incorporated_to"] = hi.isoformat()
            start = 0
            while True:
                if full():
                    return False
                size = min(PAGE_SIZE, MAX_RESULTS - start)
                page = await self._get(client, {**params, "size": size,
                                                "start_index": start})
                if not page:
                    return True
                hits = int(page.get("hits") or 0)
                if start == 0 and hits > MAX_RESULTS:
                    lo2, hi2 = lo or EARLIEST, hi or date.today()
                    if lo2 < hi2:
                        mid = lo2 + (hi2 - lo2) // 2
                        return (await collect(lo2, mid)
                                and await collect(mid + timedelta(days=1), hi2))
                    logger.warning("companies_house_sic: %d companies on one date "
                                   "for %s; the API gives %d of them", hits,
                                   base["sic_codes"], MAX_RESULTS)
                items = page.get("items") or []
                for item in items:
                    loc = location_from_item(item)
                    if loc and loc.company_number not in seen:
                        seen.add(loc.company_number)
                        out.append(loc)
                start += len(items)
                if not items or len(items) < size or start >= min(hits, MAX_RESULTS):
                    return True

        complete = await collect(None, None)
        logger.info("companies_house_sic: %d active care companies for %s%s "
                    "(%d requests)", len(out), base["sic_codes"],
                    f" in {where}" if where else "", self.requests_made)
        return out, complete


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(1.0, min(float(resp.headers.get("Retry-After") or 0), 300.0)) \
            if resp.headers.get("Retry-After") else 60.0
    except ValueError:
        return 60.0


async def find_care_companies(api_key: str, place: str = "",
                              sic_codes: Iterable[str] = DEFAULT_SIC_CODES,
                              known: Iterable[str] = (),
                              client: httpx.AsyncClient | None = None,
                              limit: int | None = None,
                              cache_dir: Path | None = None) -> list[Location]:
    """One call for discovery: search, then drop numbers a regulator listed."""
    reader = CompaniesHouseSic(api_key, cache_dir=cache_dir)
    found = await reader.search(client, sic_codes=sic_codes, place=place, limit=limit)
    return drop_known(found, known)
