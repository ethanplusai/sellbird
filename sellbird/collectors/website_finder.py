"""FIND WEBSITE — the official site of a company stored without one.

Several registers (Scotland's Care Inspectorate, Northern Ireland's RQIA,
Jersey's Care Commission, Companies House) publish no websites, and some CQC
services don't either. Without a domain the scout can find no contacts and no
email pattern, so those companies sit in the database doing nothing.

For each one, this stage runs ONE web search (the company's name plus its town
or postcode) through a search provider Sellbird already supports, then reads the
top few results' own pages and asks: is this really them? A page counts as
theirs only on evidence the register gave us — the name on the page together
with the postcode, the phone number or a registration/company number, or the
name with a domain that spells it and the town on the page. Directories and
aggregators never count. When the best candidate is not clearly theirs, the
company is left alone: a wrong website means emailing a stranger.

Search engines are never scraped directly. Spending follows discovery's rules:
estimate first, a daily cap on searches and on dollars, the discovery kill
switch, and nothing at all (one plain log line) when no search key is set.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from sellbird.collectors.discover import (
    PROVIDERS,
    DiscoveryQuery,
    ProviderExhausted,
    is_junk,
    normalize_domain,
)

logger = logging.getLogger("sellbird.collectors.website_finder")

# Free first: TinyFish costs nothing (30 a minute, 500 an hour), Serper has
# 2,500 free searches, DataForSEO SERP is paid from the first search.
PROVIDER_ORDER = ("tinyfish", "serper", "dataforseo_serp")

PAGE_TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=8.0, pool=8.0)
SEARCH_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
MAX_PAGE_CHARS = 600_000
CANDIDATES_PER_COMPANY = 3
# Between searches on providers that don't pace themselves (TinyFish does).
MIN_SEARCH_INTERVAL = 0.5
# Stop starting new companies after this long, well inside the heartbeat's
# 600-second bound on a whole cycle.
TIME_BUDGET_SECONDS = 300

DAY_KEY = "website_finder:day"
_warned: set[str] = set()


# ── What a register told us, to check a page against ───────────────────

@dataclass
class Evidence:
    """Facts a candidate page must agree with."""

    company_id: str
    name: str
    other_names: tuple[str, ...] = ()     # e.g. the service's provider
    town: str = ""
    postcode: str = ""
    phone: str = ""
    numbers: tuple[str, ...] = ()         # registration and company numbers

    def names(self) -> list[str]:
        out = []
        for n in (self.name, *self.other_names):
            for variant in (n, re.split(r"\s+[-–—]\s+", n or "")[0]):
                variant = (variant or "").strip()
                if variant and variant.lower() not in (x.lower() for x in out):
                    out.append(variant)
        return out

    def query(self) -> str:
        place = self.town or self.postcode
        return f'"{self.name}" {place}'.strip() if self.name else ""


# Words that say what kind of organisation it is, not which one.
_GENERIC = frozenset("""
the and of at in for on a an to by with
care carer carers caring home homes house service services support supported
supporting housing ltd limited plc llp cic company co group holdings trust
association society charity community communities nursing residential agency
health healthcare social scotland scottish wales welsh england english uk gb
northern ireland jersey guernsey living independent day centre center project
projects team branch office adult adults older people person persons young
specialist domiciliary respite home-care sheltered accommodation lodge
private enterprises solutions partnership partners
""".split())

_LEGAL_SUFFIX = re.compile(r"\b(ltd|limited|plc|llp|cic|c\.i\.c\.|inc)\b\.?", re.I)


def _norm_words(text: str) -> list[str]:
    text = (text or "").lower().replace("&", " and ")
    return re.findall(r"[a-z0-9]+", text)


def distinctive_tokens(name: str) -> list[str]:
    return [w for w in _norm_words(_LEGAL_SUFFIX.sub(" ", name or ""))
            if w not in _GENERIC and len(w) >= 3 and not w.isdigit()]


def name_on_page(name: str, page_words: set[str]) -> bool:
    """Every distinctive word of the name appears on the page. A name made of
    generic words only ("Care at Home Services") can never match by name."""
    tokens = distinctive_tokens(name)
    return bool(tokens) and all(t in page_words for t in tokens)


def domain_spells_name(name: str, domain: str) -> bool:
    """The domain carries the name's distinctive words or its initials."""
    host = normalize_domain(domain)
    labels = host.split(".")
    # The registrable label: "acme" in acme.co.uk, sunnyside in sunnyside.org
    stem = re.sub(r"[^a-z0-9]", "", "".join(
        lbl for lbl in labels[:-1] if lbl not in ("co", "org", "ltd", "plc", "me", "net", "ac", "gov")))
    if not stem:
        return False
    tokens = distinctive_tokens(name)
    if any(len(t) >= 4 and t in stem for t in tokens):
        return True
    words = [w for w in _norm_words(_LEGAL_SUFFIX.sub(" ", name)) if w not in ("of", "the", "and")]
    initials = "".join(w[0] for w in words)
    return len(initials) >= 3 and stem.startswith(initials)


_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b")
_PHONE_RE = re.compile(r"(?:\+\s?44\s?(?:\(0\)\s?)?|\b0)\d[\d\s().-]{7,14}\d")


def norm_postcode(pc: str) -> str:
    return re.sub(r"\s+", "", pc or "").upper()


def norm_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    if digits.startswith("00"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else ""


def page_postcodes(text: str) -> set[str]:
    return {a + b for a, b in _POSTCODE_RE.findall((text or "").upper())}


def page_phones(text: str) -> set[str]:
    return {p for p in (norm_phone(m) for m in _PHONE_RE.findall(text or "")) if p}


def number_on_page(number: str, upper_text: str) -> bool:
    number = (number or "").strip().upper()
    if len(number) < 5:
        return False
    pattern = r"(?<![A-Z0-9])" + re.escape(number) + r"(?![A-Z0-9])"
    return re.search(pattern, upper_text) is not None


def html_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    from html import unescape
    return re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", html)))


# ── Directories and aggregators: never someone's own website ──────────
# On top of discovery's junk filter, the UK-specific ones a care provider's
# name search turns up first.
EXTRA_JUNK = tuple(re.compile(p, re.I) for p in (
    r"\.(gov|nhs|ac|police|mod|sch)\.uk$",
    r"(^|\.)(gov\.scot|gov\.wales|gov\.je|gov\.gg|gov\.im)$",
    r"(^|\.)(cqc\.org\.uk|careinspectorate\.(com|scot)|careinspectorate\.wales|rqia\.org\.uk|carecommission\.je)$",
    r"(^|\.)(company-information\.service\.gov\.uk|opencorporates|endole|companycheck|companieslist"
    r"|bizdb|checkcompany|companyhub|find-and-update|suite\.endole|dnb|kompass|globaldatabase)\.",
    r"(^|\.)(carehome|homecare|carehomes|lottie|autumna|caresearch|careuk|carechoices"
    r"|housingcare|elderlyaccommodation|eac|nursinghomes|carehomeadvice|careinfoscotland"
    r"|scottishcare|ukhca|homecareassociation|carelistings|helpandcare)\.",
    r"(^|\.)(yell|thomsonlocal|scoot|192|cylex|freeindex|hotfrog|bark|checkatrade|ratedpeople"
    r"|misterwhat|brownbook|tuugo|businessmagnet|smartguy|uksmallbusinessdirectory|applegate"
    r"|192enterprise|thebestof|touchlocal|lacartes|opendi|infobel|n49|bizify|zaubee)\.",
    r"(^|\.)(charitycommission|oscr|charitycommissionni|charitybase|charitychoice|justgiving)\.",
    r"(^|\.)(totaljobs|reed|cv-library|jobsite|guardianjobs|caterer|s1jobs|nijobs|careerjet"
    r"|adzuna|jooble|neuvoo|talent|simplyhired|monster|caretrade|jobs\.ac)\.",
    r"(^|\.)(wikipedia|bbc|theguardian|dailyrecord|heraldscotland|scotsman|belfasttelegraph"
    r"|jerseyeveningpost|bailiwickexpress|pressandjournal|thecourier)\.",
    r"(^|\.)(google|goo\.gl|bing|maps\.apple|waze|what3words|streetcheck|zoopla|rightmove|onthemarket)\.",
))


def is_directory(domain: str, title: str = "") -> bool:
    return is_junk(domain, title) or any(p.search(domain) for p in EXTRA_JUNK)


# ── Scoring a candidate ─────────────────────────────────────────────────

MIN_CONFIDENCE = 0.8


@dataclass
class Match:
    domain: str
    url: str
    confidence: float
    reasons: list[str] = field(default_factory=list)


def score_page(ev: Evidence, domain: str, text: str) -> tuple[float, list[str]]:
    """How sure we are that ``domain`` (showing ``text``) is this company's."""
    words = set(_norm_words(text))
    upper = (text or "").upper()
    reasons = []
    named = any(name_on_page(n, words) for n in ev.names())
    if named:
        reasons.append("name")
    postcode = bool(ev.postcode) and norm_postcode(ev.postcode) in page_postcodes(text)
    if postcode:
        reasons.append("postcode")
    phone = bool(norm_phone(ev.phone)) and norm_phone(ev.phone) in page_phones(text)
    if phone:
        reasons.append("phone")
    number = next((n for n in ev.numbers if number_on_page(n, upper)), "")
    if number:
        reasons.append(f"number {number}")
    spelled = any(domain_spells_name(n, domain) for n in ev.names())
    if spelled:
        reasons.append("domain")
    town = bool(ev.town) and all(w in words for w in _norm_words(ev.town))
    if town:
        reasons.append("town")

    identifier = postcode or phone or bool(number)
    if named and identifier:
        conf = 0.95
    elif identifier and spelled:
        conf = 0.9
    elif named and spelled and town:
        conf = 0.8
    elif named and spelled:
        conf = 0.65
    elif identifier:
        conf = 0.55
    elif named:
        conf = 0.4
    else:
        conf = 0.0
    return conf, reasons


def choose(matches: list[Match], min_confidence: float = MIN_CONFIDENCE) -> tuple[Match | None, str]:
    """The match to save, or (None, why not)."""
    if not matches:
        return None, "no candidate"
    ranked = sorted(matches, key=lambda m: m.confidence, reverse=True)
    best = ranked[0]
    if best.confidence < min_confidence:
        return None, f"unsure (best {best.domain} at {best.confidence:.2f})"
    rival = next((m for m in ranked[1:] if m.domain != best.domain
                  and m.confidence >= best.confidence), None)
    if rival:
        return None, f"ambiguous ({best.domain} and {rival.domain})"
    return best, ""


# ── Register lookups: the postcode, phone and numbers behind each row ─

class RegisterLookup:
    """Finds the register record behind a company's ``external_id``.

    Reads the local copies discovery already keeps in data/<register>/; a
    company from a source with no local copy falls back to what the row
    itself holds (name, town, phone).
    """

    def __init__(self, data_root: Path | None = None):
        self.data_root = data_root
        self._index: dict[str, dict] = {}

    async def _load(self, client, prefix: str) -> dict:
        if prefix in self._index:
            return self._index[prefix]
        records: dict = {}
        try:
            if prefix == "companies_house":
                records = self._companies_house()
            else:
                provider = PROVIDERS.get(prefix)
                if provider is not None and hasattr(provider, "_register"):
                    for loc in await provider._register(client) or []:
                        records[str(loc.id)] = loc
        except Exception as e:
            logger.warning("website finder: couldn't read the %s register: %s", prefix, e)
        self._index[prefix] = records
        return records

    def _companies_house(self) -> dict:
        from sellbird.collectors.cqc import Location
        root = self.data_root
        if root is None:
            from sellbird.paths import PROJECT_ROOT
            root = PROJECT_ROOT / "data"
        out = {}
        for path in sorted((root / "companies_house_sic").glob("*.json")):
            try:
                for item in json.loads(path.read_text(encoding="utf-8")):
                    loc = Location(**item)
                    for key in (loc.company_number, loc.id):
                        if key:
                            out[str(key)] = loc
            except (OSError, ValueError, TypeError):
                continue
        return out

    async def evidence(self, client, company: dict) -> Evidence:
        ext = company.get("external_id") or ""
        prefix, _, ident = ext.partition(":")
        loc = (await self._load(client, prefix)).get(ident) if ident else None
        numbers = []
        if loc is not None:
            numbers += [str(loc.id), getattr(loc, "company_number", "") or "",
                        getattr(loc, "provider_id", "") or ""]
        if prefix == "companies_house":
            numbers.append(ident)
        numbers += company.get("numbers") or []
        others = tuple(n for n in (getattr(loc, "provider_name", "") if loc else "",)
                       if n and n.lower() != (company.get("name") or "").lower())
        return Evidence(
            company_id=company["id"],
            name=company.get("name") or "",
            other_names=others,
            town=(getattr(loc, "city", "") if loc else "") or company.get("location") or "",
            postcode=getattr(loc, "postcode", "") if loc else "",
            phone=(getattr(loc, "phone", "") if loc else "") or company.get("phone") or "",
            numbers=tuple(dict.fromkeys(n.strip() for n in numbers
                                        if n and len(n.strip()) >= 5)),
        )


# ── The run ────────────────────────────────────────────────────────────

@dataclass
class WebsiteReport:
    provider: str = ""
    pending: int = 0
    searched: int = 0
    found: int = 0
    unsure: int = 0
    shared: int = 0
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    stopped: str = ""
    errors: list[str] = field(default_factory=list)
    matches: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "provider", "pending", "searched", "found", "unsure", "shared",
            "estimated_cost", "actual_cost", "stopped", "errors")}


def pick_provider(env: dict, wanted: str = "auto"):
    """The search provider to use, or None when no key is set."""
    keys = PROVIDER_ORDER if wanted in ("", "auto") else (wanted,)
    for key in keys:
        provider = PROVIDERS.get(key)
        if provider is not None and provider.configured(env):
            return provider
    return None


def cost_per_search(provider) -> float:
    return provider.estimate([DiscoveryQuery(term="x", depth=10)])


async def _spent_today(state) -> tuple[int, float]:
    raw = await state.get_setting(DAY_KEY)
    day, _, rest = (raw or "").partition("|")
    if day != date.today().isoformat():
        return 0, 0.0
    searches, _, spend = rest.partition("|")
    try:
        return int(searches or 0), float(spend or 0)
    except ValueError:
        return 0, 0.0


async def _record_spend(state, searches: int, spend: float):
    await state.set_setting(DAY_KEY, f"{date.today().isoformat()}|{searches}|{spend:.4f}")


def _warn_once(key: str, message: str):
    if key not in _warned:
        _warned.add(key)
        logger.info(message)


async def _fetch(client, url: str) -> str:
    try:
        resp = await client.get(url)
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("website finder: fetch failed %s: %s", url, e)
        return ""
    if resp.status_code != 200:
        return ""
    ctype = resp.headers.get("content-type", "")
    if ctype and "html" not in ctype and "text" not in ctype:
        return ""
    return (resp.text or "")[:MAX_PAGE_CHARS]


_CONTACT_HREF = re.compile(r"""href=["']([^"'#]*contact[^"'#]*)["']""", re.I)


async def check_candidate(page_client, ev: Evidence, domain: str, url: str) -> Match:
    """Read the result page, the homepage and, if still unsure, a contact page."""
    pages, seen = [], set()
    home = f"https://{domain}/"
    best, reasons = 0.0, []
    homepage_html = ""
    for target in (url, home):
        if not target or target.rstrip("/") in seen:
            continue
        seen.add(target.rstrip("/"))
        html = await _fetch(page_client, target)
        if target == home:
            homepage_html = html
        if html:
            pages.append(html_text(html))
            best, reasons = score_page(ev, domain, " ".join(pages))
            if best >= 0.95:
                break
    if best < 0.95 and pages:
        m = _CONTACT_HREF.search(homepage_html or "")
        if m:
            contact = urljoin(home, m.group(1))
            if normalize_domain(contact) == domain and contact.rstrip("/") not in seen:
                html = await _fetch(page_client, contact)
                if html:
                    pages.append(html_text(html))
                    best, reasons = score_page(ev, domain, " ".join(pages))
    return Match(domain=domain, url=url or home, confidence=best, reasons=reasons)


async def find_website(page_client, ev: Evidence, results) -> list[Match]:
    """Check the top results that aren't directories."""
    matches, seen = [], set()
    for biz in results:
        domain = normalize_domain(biz.domain or biz.website)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        if is_directory(domain, biz.raw_title or ""):
            continue
        matches.append(await check_candidate(page_client, ev, domain, biz.website))
        if len(matches) >= CANDIDATES_PER_COMPANY or matches[-1].confidence >= 0.95:
            break
    return matches


async def run_website_finder(
    state,
    config=None,
    *,
    env: dict | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    search_client=None,
    page_client=None,
    lookup: RegisterLookup | None = None,
) -> WebsiteReport:
    """Find websites for up to ``limit`` companies stored without one."""
    settings = getattr(config, "website_finder", None)
    if settings is not None and not settings.enabled:
        return WebsiteReport(stopped="disabled")
    batch = limit if limit is not None else getattr(settings, "batch_size", 10)
    max_searches = getattr(settings, "max_daily_searches", 200)
    max_spend = getattr(settings, "max_daily_spend", 0.50)
    min_conf = getattr(settings, "min_confidence", MIN_CONFIDENCE)
    retry_days = getattr(settings, "retry_days", 60)

    report = WebsiteReport()
    report.pending = await state.count_companies_needing_website(retry_days)
    if not report.pending:
        return report

    if env is None:
        from sellbird.config import load_env
        env = load_env().model_dump()
    provider = pick_provider(env, getattr(settings, "provider", "auto"))
    if provider is None:
        _warn_once("nokey", (
            f"Website finder is idle: {report.pending} companies have no website, "
            "and no search key is set. Add TINYFISH_API_KEY (free) or "
            "SERPER_API_KEY to .env to let Sellbird look them up."))
        report.stopped = "no search key"
        return report
    report.provider = provider.key

    if await state.get_setting("discovery_paused"):
        report.stopped = "paused"
        return report

    per_search = cost_per_search(provider)
    searches_today, spend_today = await _spent_today(state)
    room = max(0, max_searches - searches_today)
    if per_search > 0:
        room = min(room, int(max(0.0, max_spend - spend_today) / per_search + 1e-9))
    batch = min(batch, room)
    if batch <= 0:
        _warn_once(f"cap:{date.today()}", (
            f"Website finder: today's cap is reached ({searches_today} searches, "
            f"${spend_today:.2f}); it carries on tomorrow."))
        report.stopped = "daily cap reached"
        return report

    companies = await state.companies_needing_website(limit=batch, retry_days=retry_days)
    report.estimated_cost = per_search * len(companies)
    logger.info("website finder: %d of %d companies, %s, estimated $%.3f",
                len(companies), report.pending, provider.label, report.estimated_cost)
    if dry_run or not companies:
        return report

    confirmed = await state.confirmed_signal_codes()
    run_id = await state.start_run("find_website", provider=provider.key,
                                   params={"companies": len(companies),
                                           "estimate": report.estimated_cost})
    lookup = lookup or RegisterLookup()
    own_search = search_client is None
    own_pages = page_client is None
    if own_search:
        search_client = httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True)
    if own_pages:
        from sellbird.collectors.profile import USER_AGENTS
        from sellbird.public_http import public_http_client
        page_client = public_http_client(
            timeout=PAGE_TIMEOUT,
            headers={"User-Agent": USER_AGENTS[0],
                     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    started = time.monotonic()
    last_search = 0.0
    try:
        for company in companies:
            if time.monotonic() - started > TIME_BUDGET_SECONDS:
                report.stopped = "time budget"
                break
            if report.actual_cost + per_search > max_spend - spend_today + 1e-9 and per_search:
                report.stopped = "budget reached"
                break
            ev = await lookup.evidence(search_client, company)
            query = ev.query()
            if not query:
                await state.mark_company_website_checked(company["id"])
                continue
            if provider.key != "tinyfish":
                wait = last_search + MIN_SEARCH_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            last_search = time.monotonic()
            provider.last_cost = 0.0
            try:
                results = await provider.fetch(
                    search_client, env, DiscoveryQuery(
                        term=query,
                        location="United Kingdom" if provider.key == "dataforseo_serp" else "",
                        depth=10, limit=10))
            except ProviderExhausted as e:
                report.stopped = str(e)
                break
            except Exception as e:
                # A refused key or an outage fails every search the same way:
                # stop, leave the company unchecked, and try next cycle.
                logger.warning("website finder: %s search failed: %s", provider.key, e)
                report.errors.append(str(e))
                report.stopped = "search failed"
                break
            finally:
                report.actual_cost += provider.last_cost or per_search
            report.searched += 1

            matches = await find_website(page_client, ev, results)
            best, why = choose(matches, min_conf)
            outcome = await _save(state, company, best, why, confirmed, run_id)
            report.matches.append({"company_id": company["id"], "outcome": outcome,
                                   "domain": best.domain if best else "",
                                   "confidence": best.confidence if best else 0.0})
            if outcome == "found":
                report.found += 1
            elif outcome == "shared":
                report.shared += 1
            else:
                report.unsure += 1
        await state.finish_run(run_id, status="completed", records=report.found,
                               cost_usd=report.actual_cost)
    except Exception as e:
        logger.exception("website finder failed")
        report.errors.append(str(e))
        await state.finish_run(run_id, status="failed", records=report.found,
                               cost_usd=report.actual_cost, error=str(e))
    finally:
        await _record_spend(state, searches_today + report.searched,
                            spend_today + report.actual_cost)
        if own_search:
            await search_client.aclose()
        if own_pages:
            await page_client.aclose()
    if report.searched:
        logger.info("website finder: %d searched, %d found, %d shared with another "
                    "company, %d left alone (unsure)", report.searched, report.found,
                    report.shared, report.unsure)
    return report


async def _save(state, company: dict, best: Match | None, why: str,
                confirmed: set[str], run_id: str) -> str:
    rows = []

    def add(code, num, text, conf, url=""):
        if code in confirmed:
            rows.append({"company_id": company["id"], "signal_code": code,
                         "collector": "find_website", "value_num": num,
                         "value_text": text, "confidence": conf,
                         "evidence_url": url, "run_id": run_id})

    outcome = "unsure"
    if best is not None:
        saved = await state.set_company_website(company["id"], best.domain, best.url)
        if saved == "saved":
            outcome = "found"
            add("WEBSITE_FOUND", best.confidence,
                f"{best.domain} | {', '.join(best.reasons)}", best.confidence, best.url)
            add("NO_WEBSITE", 0.0, best.domain, best.confidence, best.url)
        elif saved == "taken":
            outcome = "shared"
            add("WEBSITE_FOUND", 0.0,
                f"{best.domain} belongs to another company here | {', '.join(best.reasons)}",
                best.confidence, best.url)
    else:
        add("WEBSITE_FOUND", 0.0, why, 1.0)
    await state.mark_company_website_checked(company["id"])
    if rows:
        await state.add_observations(rows, run_id=run_id)
    return outcome
