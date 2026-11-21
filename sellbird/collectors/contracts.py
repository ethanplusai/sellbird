"""Council contracts — who gets paid to run floating support and supported housing.

Floating support, housing-related support and most supported or exempt
accommodation isn't CQC registered, so the care registers miss it. Councils
buy it on contract, though, and publish every award as open data under the
Open Government Licence. The winning suppliers are the organisations doing
the work.

Sources (all free, no key, no account):

* **Contracts Finder** (England, below and above threshold until Feb 2025,
  below threshold since): a keyword search API, then one notice per award for
  the supplier's address and Companies House number.
* **Find a Tender** (UK-wide above threshold): OCDS release packages of award
  notices, read a month at a time and filtered here, since the API has no
  keyword search. A month is a few hundred to a few thousand notices, so the
  history fills in ``FTS_MONTHS_PER_RUN`` months per run, newest first.
* **Public Contracts Scotland**: monthly OCDS packages of award notices.
* **Sell2Wales** runs the same software as Public Contracts Scotland, so it is
  read the same way; its API was returning server errors when this was
  written, and a month that fails is skipped with a warning and tried again
  next run. Welsh and Northern Irish above-threshold awards are on Find a
  Tender either way. eTendersNI publishes no open feed.

None of these sources has a robots.txt that covers the APIs (Public Contracts
Scotland's website disallows crawling, which is why only its documented API
is used). Every finished month and every Contracts Finder notice is cached in
``data_dir`` for good; only the current month and the keyword searches are
refreshed, after ``MAX_AGE_DAYS``.

Each supplier comes back as a ``ContractSupplier`` (a ``cqc.Location`` with
its contracts attached): id ``cf-<supplier key>``, the latest contract's
facts, and how many matching contracts it holds.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re
import ssl
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sellbird.collectors.cqc import Location

logger = logging.getLogger(__name__)

YEARS_BACK = 4
MAX_AGE_DAYS = 7
FTS_MONTHS_PER_RUN = 6
RETRIES = 5
CF_NOTICE_DELAY = 0.5          # seconds between Contracts Finder notice reads (it rate limits)

CF_SEARCH = "https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json"
CF_NOTICE_API = "https://www.contractsfinder.service.gov.uk/api/rest/2/get_published_notice/json/{id}"
CF_NOTICE_URL = "https://www.contractsfinder.service.gov.uk/Notice/{id}"
FTS_API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
FTS_NOTICE_URL = "https://www.find-tender.service.gov.uk/Notice/{id}"
PCS_API = "https://api.publiccontractsscotland.gov.uk/v1/Notices"
PCS_NOTICE_URL = ("https://www.publiccontractsscotland.gov.uk/search/show/"
                  "search_view.aspx?ID={id}")
# Public Contracts Scotland's API sends its own certificate without the CA's
# intermediate, so strict clients (httpx with certifi) refuse it. Browsers
# fetch the missing intermediate from the address the certificate names; so
# does this, and only after a certificate failure. Verification stays full:
# the intermediate must still chain to a root in certifi.
PCS_INTERMEDIATE = "http://crt.sectigo.com/SectigoPublicServerAuthenticationCADVR36.crt"
S2W_API = "https://api.sell2wales.gov.wales/v1/Notices"
S2W_NOTICE_URL = "https://www.sell2wales.gov.wales/search/show/search_view.aspx?ID={id}"
AWARD_NOTICE_TYPE = 3          # Public Contracts Scotland / Sell2Wales "Contract Award Notice"

# A phrase that alone makes a notice ours -> what the work is called.
# Checked in order, so the specific phrases come first.
STRONG_PHRASES = (
    ("floating support", "floating support"),
    ("floating housing support", "floating support"),
    ("floating housing related support", "floating support"),
    ("floating housing-related support", "floating support"),
    ("tenancy sustainment", "tenancy sustainment"),
    ("exempt accommodation", "exempt accommodation"),
    ("housing related support", "housing-related support"),
    ("housing-related support", "housing-related support"),
    ("supported housing", "supported housing"),
    ("supported accommodation", "supported accommodation"),
    ("supported lodgings", "supported lodgings"),
    ("homelessness support", "homelessness support"),
)
# Words that count only when the notice is also classed as social work or
# accommodation services, since alone they turn up in all sorts of notices.
WEAK_PHRASES = (
    ("housing support", "housing support"),
    ("tenancy support", "tenancy support"),
    ("homeless", "homelessness support"),
    ("hostel", "supported accommodation"),
    ("resettlement", "housing support"),
)
# CPV: social work and related services (853...: with and without
# accommodation, welfare, community action) and accommodation services.
SUPPORT_CPV_PREFIXES = ("853", "98341")

# The Contracts Finder keyword searches (quoted, so they match as phrases).
CF_KEYWORDS = tuple(f'"{p}"' for p in (
    "floating support", "housing related support", "supported housing",
    "supported accommodation", "tenancy sustainment", "homelessness support",
    "exempt accommodation", "supported lodgings", "housing support"))

# What the writer says the contract was for.
FLOATING = {"floating support", "tenancy sustainment", "tenancy support",
            "housing-related support", "housing support", "homelessness support"}

_JUNK_SUPPLIER = re.compile(
    r"^(various|multiple|see (attached|below|above|description)|n/?a|none|tbc|"
    r"framework|several|refer to|please see|\d+ suppliers?)\b", re.I)
_ORG_WORDS = re.compile(
    r"\b(ltd|limited|plc|llp|cic|cio|charity|trust|association|housing|group|"
    r"society|foundation|council|services|partnership|church|project|community|"
    r"care|support|homes?|centre|network|uk|t/a)\b", re.I)
_PERSON_TITLE = re.compile(r"^(mr|mrs|ms|miss|dr)\.?\s", re.I)
_SUFFIXES = re.compile(
    r"\b(the|ltd|limited|plc|llp|cic|cio|uk|co|company|incorporated|inc)\b\.?", re.I)
_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)


@dataclass
class ContractSupplier(Location):
    """A supplier holding at least one matching contract, with the latest one."""

    contract_title: str = ""
    contract_buyer: str = ""
    contract_value: float = 0.0
    contract_awarded: str = ""        # ISO date
    contract_url: str = ""
    contract_source: str = ""         # Contracts Finder | Find a Tender | ...
    contract_kind: str = ""           # "floating support", "supported housing", ...
    contract_count: int = 0           # distinct matching contracts, all buyers
    contract_buyers: list[str] = field(default_factory=list)
    email: str = ""                   # a contact email on the notice, if any

    @property
    def url(self) -> str:
        return self.contract_url

    @property
    def facts(self) -> str:
        return facts_text(self)


@dataclass
class Award:
    """One supplier on one award notice."""

    source: str
    notice_id: str
    url: str
    title: str
    description: str
    buyer: str
    awarded: str                      # ISO date, "" if unknown
    value: float
    supplier: str
    address: str = ""
    postcode: str = ""
    city: str = ""
    company_number: str = ""
    website: str = ""
    email: str = ""
    phone: str = ""
    kind: str = ""


# ── matching notices ─────────────────────────────────────────────────────


def _clean(text) -> str:
    text = _html.unescape(_html.unescape(str(text or "")))
    return re.sub(r"\s+", " ", text).strip()


def classify(text: str, cpvs=()) -> str:
    """What kind of support a notice is for ("" when it isn't ours)."""
    low = _clean(text).lower()
    for phrase, kind in STRONG_PHRASES:
        if phrase in low:
            return kind
    if any(str(c).startswith(SUPPORT_CPV_PREFIXES) for c in cpvs if c):
        for phrase, kind in WEAK_PHRASES:
            if phrase in low:
                return kind
    return ""


def services_for_kind(kind: str) -> list[str]:
    return ["floating support"] if kind in FLOATING else ["supported housing"]


# ── suppliers ────────────────────────────────────────────────────────────


def company_number(value) -> str:
    """A Companies House number in its 8-character form, or ""."""
    value = re.sub(r"\s", "", str(value or "")).upper()
    if re.fullmatch(r"\d{1,8}", value):
        return value.zfill(8)
    if re.fullmatch(r"(SC|NI|OC|SO|NC|R0|IP|SP|RS|CE|CS)\d{4,6}", value):
        prefix, digits = value[:2], value[2:]
        return prefix + digits.zfill(6)
    return ""


def name_key(name: str) -> str:
    """A supplier's name reduced for matching ("The Example Trust Ltd" ->
    "example-trust")."""
    low = _clean(name).lower().replace("&", " and ")
    low = re.split(r"\s+(?:t/a|trading as|operating as)\s+|\(", low)[0]
    low = _SUFFIXES.sub(" ", low)
    return re.sub(r"[^a-z0-9]+", "-", low).strip("-")


def ownership_for(name: str) -> str:
    """"Individual" when the notice names a sole trader, else "Organisation"."""
    name = _clean(name)
    if _PERSON_TITLE.match(name):
        return "Individual"
    if re.search(r"\s(t/a|trading as)\s", name, re.I):
        before = re.split(r"\s(?:t/a|trading as)\s", name, flags=re.I)[0]
        if not _ORG_WORDS.search(before):
            return "Individual"
    return "Organisation"


def _usable_supplier(name: str) -> bool:
    name = _clean(name)
    return len(name) > 2 and not _JUNK_SUPPLIER.match(name)


def _website(value) -> str:
    value = str(value or "").strip()
    if value.lower() in {"", "http://", "https://", "http://www.", "www."}:
        return ""
    if not value.lower().startswith(("http://", "https://")):
        value = "https://" + value
    return value


def split_address(text: str) -> tuple[str, str, str]:
    """(street, town, postcode) out of a one-line address."""
    text = _clean(text)
    match = _POSTCODE.search(text)
    postcode = f"{match.group(1)} {match.group(2)}".upper() if match else ""
    parts = [p.strip() for p in (text[:match.start()] if match else text).split(",")]
    parts = [p for p in parts if p and p.lower() not in {"united kingdom", "uk", "england",
                                                         "scotland", "wales",
                                                         "northern ireland"}]
    town = parts[-1] if len(parts) > 1 else ""
    street = ", ".join(parts[:-1] if town else parts)
    return street, town, postcode


def _postcode(value) -> str:
    match = _POSTCODE.search(str(value or ""))
    return f"{match.group(1)} {match.group(2)}".upper() if match else ""


def _iso(value) -> str:
    value = str(value or "")[:10]
    return value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else ""


def _amount(value) -> float:
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ── Contracts Finder ─────────────────────────────────────────────────────


def awards_from_cf_notice(data: dict, kind: str = "") -> list[Award]:
    """The suppliers on one Contracts Finder notice (get_published_notice)."""
    notice = data.get("notice") or {}
    nid = data.get("id") or notice.get("id") or ""
    title = _clean(notice.get("title"))
    desc = _clean(notice.get("description"))
    cpvs = [c.get("code") for c in data.get("cpvCodes") or []]
    kind = kind or classify(f"{title} {desc}", cpvs)
    if not kind:
        return []
    buyer = _clean(notice.get("organisationName")
                   or (data.get("organisation") or {}).get("name"))
    out = []
    for a in data.get("awards") or []:
        name = _clean(a.get("supplierName"))
        if not _usable_supplier(name):
            continue
        street, town, postcode = split_address(a.get("supplierAddress") or "")
        number = ""
        if "COMPANIES" in str(a.get("referenceType") or "").upper():
            number = company_number(a.get("reference"))
        value = _amount(a.get("supplierAwardedValue")) or _amount(a.get("value"))
        out.append(Award(
            source="Contracts Finder", notice_id=nid, url=CF_NOTICE_URL.format(id=nid),
            title=re.sub(r"\s*-\s*AWARD$", "", title, flags=re.I), description=desc,
            buyer=buyer, awarded=_iso(a.get("awardedDate") or a.get("startDate")
                                      or notice.get("publishedDate")),
            value=value, supplier=name, address=street, city=town, postcode=postcode,
            company_number=number, kind=kind))
    return out


async def _post_json(client, url: str, body: dict) -> dict:
    resp = await client.post(url, json=body, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


async def _get_json(client, url: str, params: dict | None = None,
                    retries: int = RETRIES) -> dict:
    """GET JSON, waiting and trying again when the server says 429."""
    for attempt in range(retries + 1):
        resp = await client.get(url, params=params, headers={"Accept": "application/json"})
        if resp.status_code == 429 and attempt < retries:
            try:
                wait = float(resp.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0
            await asyncio.sleep(min(wait or 5 * 2 ** attempt, 120))
            continue
        resp.raise_for_status()
        return resp.json()
    return {}


async def cf_notice_ids(client, since: date) -> dict[str, str]:
    """{notice id: kind} for every awarded Contracts Finder notice that matches."""
    found: dict[str, str] = {}
    for keyword in CF_KEYWORDS:
        body = {"searchCriteria": {"types": ["Contract"], "statuses": ["Awarded"],
                                   "keyword": keyword,
                                   "publishedFrom": f"{since.isoformat()}T00:00:00"},
                "size": 1000}
        data = await _post_json(client, CF_SEARCH, body)
        for hit in data.get("noticeList") or []:
            item = hit.get("item") or {}
            text = f"{item.get('title', '')} {item.get('description', '')}"
            cpvs = str(item.get("cpvCodes") or "").replace(",", " ").split()
            kind = classify(text, cpvs)
            if kind and item.get("id") and not item.get("isSubNotice"):
                found.setdefault(item["id"], kind)
    return found


async def read_contracts_finder(client, data_dir: Path, since: date) -> list[Award]:
    """Every matching supplier on Contracts Finder since a date.

    The keyword searches are cached for MAX_AGE_DAYS and each notice for good
    (an awarded notice doesn't change).
    """
    index_path = data_dir / "cf_search.json"
    notices_path = data_dir / "cf_notices.json"
    notices = _read_json(notices_path, {})
    index = _read_json(index_path, {})
    if not (_fresh(index_path) and index):
        try:
            index = await cf_notice_ids(client, since)
            _write_json(index_path, index)
        except Exception as e:
            if not index:
                raise
            logger.warning("contracts: Contracts Finder search failed (%s); "
                           "using the last results", e)
    fetched = tried = 0
    for nid in index:
        if nid in notices:
            continue
        if tried:
            await asyncio.sleep(CF_NOTICE_DELAY)
        tried += 1
        try:
            notices[nid] = await _get_json(client, CF_NOTICE_API.format(id=nid))
            fetched += 1
        except Exception as e:
            logger.warning("contracts: couldn't read Contracts Finder notice %s: %s", nid, e)
        if fetched and fetched % 50 == 0:
            _write_json(notices_path, notices)
    if fetched:
        _write_json(notices_path, notices)
    out = []
    for nid, kind in index.items():
        if nid in notices:
            out.extend(awards_from_cf_notice(notices[nid], kind))
    return out


# ── OCDS (Find a Tender, Public Contracts Scotland, Sell2Wales) ──────────


def _ocds_company_number(party: dict) -> str:
    ids = [party.get("identifier") or {}] + list(party.get("additionalIdentifiers") or [])
    for ident in ids:
        if str(ident.get("scheme") or "").upper() == "GB-COH":
            number = company_number(ident.get("id"))
            if number:
                return number
    return ""


def _release_url(source: str, release: dict) -> str:
    rid = str(release.get("id") or "")
    if source == "Find a Tender":
        return FTS_NOTICE_URL.format(id=rid)
    short = rid.rsplit("-", 1)[-1] if rid.startswith("rls-") else rid
    template = PCS_NOTICE_URL if source == "Public Contracts Scotland" else S2W_NOTICE_URL
    return template.format(id=short)


def awards_from_release(release: dict, source: str) -> list[Award]:
    """The suppliers on one OCDS award release that matches, else []."""
    tender = release.get("tender") or {}
    title = _clean(tender.get("title"))
    desc = _clean(tender.get("description") or release.get("description"))
    award_titles = " ".join(_clean(a.get("title")) for a in release.get("awards") or [])
    cpvs = [(tender.get("classification") or {}).get("id")]
    for item in tender.get("items") or []:
        cpvs.append((item.get("classification") or {}).get("id"))
        cpvs.extend(c.get("id") for c in item.get("additionalClassifications") or [])
    kind = classify(f"{title} {award_titles} {desc}", cpvs)
    if not kind:
        return []
    parties = {p.get("id"): p for p in release.get("parties") or []}
    buyer = _clean((release.get("buyer") or {}).get("name"))
    if not buyer:
        buyer = next((_clean(p.get("name")) for p in parties.values()
                      if "buyer" in (p.get("roles") or [])), "")
    contracts = {c.get("awardID") or c.get("id"): c for c in release.get("contracts") or []}
    url = _release_url(source, release)
    out, seen = [], set()
    for award in release.get("awards") or []:
        if str(award.get("status") or "").lower() in {"cancelled", "unsuccessful"}:
            continue
        contract = contracts.get(award.get("id")) or {}
        awarded = _iso(award.get("date") or contract.get("dateSigned")
                       or release.get("date"))
        value = _amount(award.get("value")) or _amount(contract.get("value"))
        for sup in award.get("suppliers") or []:
            party = parties.get(sup.get("id")) or {}
            name = _clean(sup.get("name") or party.get("name"))
            if not _usable_supplier(name) or name.lower() in seen:
                continue
            seen.add(name.lower())
            addr = party.get("address") or {}
            contact = party.get("contactPoint") or {}
            details = party.get("details") or {}
            out.append(Award(
                source=source, notice_id=str(release.get("id") or ""), url=url,
                title=title or award_titles, description=desc, buyer=buyer,
                awarded=awarded, value=value, supplier=name,
                address=_clean(addr.get("streetAddress")),
                city=_clean(addr.get("locality")),
                postcode=_postcode(addr.get("postalCode")),
                company_number=_ocds_company_number(party),
                website=_website(details.get("url") or contact.get("url")),
                email=_clean(contact.get("email")),
                phone=_clean(contact.get("telephone")), kind=kind))
    return out


def _months(since: date, today: date) -> list[tuple[int, int]]:
    """(year, month) from since to today, newest first."""
    out, y, m = [], today.year, today.month
    while (y, m) >= (since.year, since.month):
        out.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


def _month_done(year: int, month: int, today: date) -> bool:
    return (year, month) < (today.year, today.month)


async def fts_month(client, year: int, month: int) -> list[Award]:
    """Every matching supplier among one month's Find a Tender award notices."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    params = {"stages": "award", "limit": 100,
              "updatedFrom": start.strftime("%Y-%m-%dT%H:%M:%S"),
              "updatedTo": end.strftime("%Y-%m-%dT%H:%M:%S")}
    out: list[Award] = []
    data = await _get_json(client, FTS_API, params)
    pages = 0
    while data:
        releases = data.get("releases") or []
        for release in releases:
            out.extend(awards_from_release(release, "Find a Tender"))
        nxt = (data.get("links") or {}).get("next")
        pages += 1
        if not nxt or not releases or pages > 2000:
            break
        data = await _get_json(client, nxt)
    return out


async def millstream_month(client, api: str, source: str, year: int, month: int
                           ) -> list[Award]:
    """One month of award notices from Public Contracts Scotland or Sell2Wales."""
    params = {"dateFrom": f"{month:02d}-{year}", "outputType": 0,
              "noticeType": AWARD_NOTICE_TYPE}
    data = await _get_json(client, api, params)
    out: list[Award] = []
    for release in data.get("releases") or []:
        out.extend(awards_from_release(release, source))
    return out


def _is_cert_error(e: Exception) -> bool:
    return "CERTIFICATE_VERIFY_FAILED" in str(e) or isinstance(e, ssl.SSLCertVerificationError)


async def completed_chain_client(client, data_dir: Path, intermediate_url: str):
    """A client that also knows a server's missing intermediate certificate.

    The certificate is downloaded once and kept in data_dir. It is added to
    certifi's roots for chain building only: partial chains stay off, so it
    can't stand in for a root.
    """
    import certifi
    import httpx

    pem_path = data_dir / ("intermediate-" + intermediate_url.rsplit("/", 1)[-1] + ".pem")
    if not pem_path.exists():
        resp = await client.get(intermediate_url)
        resp.raise_for_status()
        body = resp.content
        pem = body.decode("ascii") if body.lstrip().startswith(b"-----BEGIN")             else ssl.DER_cert_to_PEM_cert(body)
        data_dir.mkdir(parents=True, exist_ok=True)
        pem_path.write_text(pem, encoding="ascii")
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(pem_path))
    if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    return httpx.AsyncClient(verify=ctx, timeout=client.timeout,
                             headers={"User-Agent": client.headers.get("User-Agent", "")})


async def read_scotland(client, data_dir: Path, since: date, today: date) -> list[Award]:
    """Public Contracts Scotland, switching to a completed-chain client if its
    certificate is refused."""
    state: dict = {"client": client, "own": None}

    async def fetch(year: int, month: int) -> list[Award]:
        try:
            return await millstream_month(state["client"], PCS_API,
                                          "Public Contracts Scotland", year, month)
        except Exception as e:
            if state["own"] is not None or not _is_cert_error(e):
                raise
            logger.info("contracts: Public Contracts Scotland's certificate chain is "
                        "incomplete; fetching its intermediate")
            state["own"] = state["client"] = await completed_chain_client(
                client, data_dir, PCS_INTERMEDIATE)
            return await millstream_month(state["client"], PCS_API,
                                          "Public Contracts Scotland", year, month)

    try:
        return await read_monthly(client, data_dir, "pcs", since, today, fetch)
    finally:
        if state["own"] is not None:
            await state["own"].aclose()


async def read_monthly(client, data_dir: Path, name: str, since: date, today: date,
                       fetch, max_new: int | None = None) -> list[Award]:
    """Awards from a monthly source, caching each finished month for good.

    ``fetch(year, month)`` reads one month. At most ``max_new`` months that
    aren't cached yet are read this run (newest first); the rest wait for the
    next one. A month that fails is skipped and tried again next time.
    """
    folder = data_dir / name
    out: list[Award] = []
    new = 0
    for year, month in _months(since, today):
        path = folder / f"{year}-{month:02d}.json"
        done = _month_done(year, month, today)
        if path.exists():
            cached = _read_json(path, None)
            if cached is not None and (cached.get("complete") or _fresh(path, 1)):
                out.extend(Award(**a) for a in cached.get("awards", []))
                continue
        if max_new is not None and new >= max_new:
            continue
        new += 1
        try:
            awards = await fetch(year, month)
        except Exception as e:
            logger.warning("contracts: %s %d-%02d failed (%s): %s", name, year, month,
                           type(e).__name__, e)
            continue
        _write_json(path, {"complete": done, "awards": [asdict(a) for a in awards]})
        out.extend(awards)
    return out


# ── putting it together ──────────────────────────────────────────────────


def suppliers_from_awards(awards: list[Award]) -> list[ContractSupplier]:
    """One record per supplier: its latest contract, plus how many it holds."""
    # A supplier named with a company number once is that company everywhere.
    numbers: dict[str, str] = {}
    for a in awards:
        if a.company_number:
            numbers.setdefault(name_key(a.supplier), a.company_number)
    groups: dict[str, list[Award]] = {}
    for a in awards:
        nk = name_key(a.supplier)
        if not nk:
            continue
        number = a.company_number or numbers.get(nk, "")
        key = f"ch{number.lower()}" if number else nk
        groups.setdefault(key, []).append(a)
    out = []
    for key, group in groups.items():
        group.sort(key=lambda a: (a.awarded, a.source == "Find a Tender"), reverse=True)
        latest = group[0]
        contracts = {(name_key(a.buyer), re.sub(r"[^a-z0-9]", "", a.title.lower())[:60])
                     for a in group}
        buyers = []
        for a in group:
            if a.buyer and a.buyer not in buyers:
                buyers.append(a.buyer)

        def first(attr: str) -> str:
            return next((getattr(a, attr) for a in group if getattr(a, attr)), "")

        # The address that goes with the postcode, from one notice, not a mix.
        addressed = next((a for a in group if a.postcode), latest)
        kinds = {a.kind for a in group}
        services = sorted({s for k in kinds for s in services_for_kind(k)})
        out.append(ContractSupplier(
            id=f"cf-{key}", name=latest.supplier,
            phone=first("phone"), website=first("website"),
            address=addressed.address, city=addressed.city, postcode=addressed.postcode,
            local_authority=latest.buyer, services=services,
            provider_id=f"cf-{key}", provider_name=latest.supplier,
            provider_website=first("website"), provider_phone=first("phone"),
            company_number=first("company_number"),
            ownership=ownership_for(latest.supplier),
            contract_title=latest.title, contract_buyer=latest.buyer,
            contract_value=latest.value, contract_awarded=latest.awarded,
            contract_url=latest.url, contract_source=latest.source,
            contract_kind=latest.kind, contract_count=len(contracts),
            contract_buyers=buyers, email=first("email")))
    out.sort(key=lambda s: s.contract_awarded, reverse=True)
    return out


def _money(value: float) -> str:
    if value >= 1_000_000:
        return f"£{value / 1_000_000:.1f}m".replace(".0m", "m")
    if value >= 1_000:
        return f"£{round(value / 1_000):,}k"
    return f"£{value:,.0f}"


def facts_text(s: ContractSupplier) -> str:
    """One or two sentences the writer can use about a supplier's contracts."""
    if not s.contract_buyer:
        return ""
    when = ""
    if s.contract_awarded:
        when = ", awarded " + datetime.strptime(s.contract_awarded, "%Y-%m-%d").strftime("%B %Y")
    worth = f" worth {_money(s.contract_value)}" if s.contract_value >= 1000 else ""
    article = "an" if s.contract_kind[:1] in "aeiou" else "a"
    text = f"Holds {article} {s.contract_kind} contract with {s.contract_buyer}{worth}{when}."
    if s.contract_count > 1:
        others = len(s.contract_buyers) - 1
        text += (f" {s.contract_count} such contracts in the last {YEARS_BACK} years"
                 + (f", with {others} other buyer{'s' * (others != 1)}" if others > 0 else "")
                 + ".")
    return text


def _fresh(path: Path, max_age_days: float = MAX_AGE_DAYS) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


_FIELDS = {f.name for f in fields(ContractSupplier)}


def load_cache(path: Path) -> list[ContractSupplier]:
    return [ContractSupplier(**{k: v for k, v in item.items() if k in _FIELDS})
            for item in json.loads(path.read_text(encoding="utf-8"))]


SOURCES = ("contracts_finder", "find_a_tender", "scotland", "wales")


async def load_register(client, data_dir: Path, *, sources=SOURCES,
                        years_back: int = YEARS_BACK,
                        fts_months_per_run: int = FTS_MONTHS_PER_RUN,
                        today: date | None = None) -> list[ContractSupplier]:
    """Every supplier holding a matching contract, newest award first.

    From ``data_dir/suppliers.json`` when it's under MAX_AGE_DAYS old, else
    rebuilt from the sources (each cached in ``data_dir`` on its own). One
    source failing doesn't stop the others; RuntimeError only when nothing
    at all could be read and there is no earlier copy.
    """
    cache = data_dir / "suppliers.json"
    if _fresh(cache):
        return load_cache(cache)
    today = today or date.today()
    since = today - timedelta(days=round(365.25 * years_back))
    awards: list[Award] = []
    errors: list[str] = []
    readers = {
        "contracts_finder": lambda: read_contracts_finder(client, data_dir, since),
        "find_a_tender": lambda: read_monthly(
            client, data_dir, "fts", since, today,
            lambda y, m: fts_month(client, y, m), max_new=fts_months_per_run),
        "scotland": lambda: read_scotland(client, data_dir, since, today),
        "wales": lambda: read_monthly(
            client, data_dir, "s2w", since, today,
            lambda y, m: millstream_month(client, S2W_API, "Sell2Wales", y, m),
            max_new=3),
    }
    for name in sources:
        try:
            got = await readers[name]()
            awards.extend(got)
            logger.info("contracts: %s gave %d matching awards", name, len(got))
        except Exception as e:
            logger.warning("contracts: %s failed: %s", name, e)
            errors.append(f"{name}: {e}")
    if not awards and errors:
        if cache.exists():
            logger.warning("contracts: couldn't refresh (%s); using the copy from before",
                           "; ".join(errors))
            return load_cache(cache)
        raise RuntimeError("Couldn't read any contract award source: " + "; ".join(errors))
    # Contracts Finder's search is by publication date; an award signed
    # before the window but published in it is left out.
    awards = [a for a in awards if not a.awarded or a.awarded >= since.isoformat()]
    suppliers = suppliers_from_awards(awards)
    _write_json(cache, [asdict(s) for s in suppliers])
    logger.info("contracts: %d suppliers from %d awards", len(suppliers), len(awards))
    return suppliers


def contract_facts(data_dir: Path) -> dict[str, str]:
    """{supplier id: facts text} from the last load_register in data_dir."""
    path = data_dir / "suppliers.json"
    if not path.exists():
        return {}
    return {s.id: facts_text(s) for s in load_cache(path)}


def search(suppliers: list[ContractSupplier], term: str = "", place: str = ""
           ) -> list[ContractSupplier]:
    """Suppliers whose work matches a phrase, near a place (by buyer or town).

    No coordinates: notices give a postcode, not a position. An empty term
    matches every supplier; "floating" matches only floating support.
    """
    low = (term or "").lower()
    wanted: set[str] = set()
    if "floating" in low or "tenancy" in low:
        wanted = {"floating support"}
    elif any(w in low for w in ("supported housing", "supported accommodation",
                                "exempt", "hostel")):
        wanted = {"supported housing"}
    city = (place or "").split(",")[0].strip().lower()
    out = []
    for s in suppliers:
        if wanted and not wanted.intersection(s.services):
            continue
        if city and not any(city in x.lower() for x in
                            [s.city, s.contract_buyer, *s.contract_buyers]):
            continue
        out.append(s)
    return out
