"""Company research: read a company's own site and decide whether it fits,
before anyone writes to it.

The Writer used to draft from the industry name, a description that was
often empty and a few homepage checks. That produced emails that misdescribed
the business, guessed at processes the company had already automated, wrote
in the wrong language, ignored the managers the site names, and went to
competitors. This module reads up to MAX_PAGES of the company's own pages
(homepage, legal notice, services, about, contact), asks the brain for a
short profile and a fit verdict, and keeps only what the pages support:

    what_they_do, customers, size, language      who they are
    people                                       named managers, with roles
    intake                                       how requests and orders reach them
    already_has, competitor                      what they already use, whether they sell what you sell
    fit (good | weak | reject), reason           whether to write at all
    hooks                                        observations, each with a quote

A hook whose quote is not on any page read is dropped: the model may
summarise, but it may not invent what the company's site says.

The result is cached per company in the settings table (RESEARCH_KEY + id)
for RESEARCH_STALE_DAYS, so a rejected company is never paid for twice and a
new contact at a known company reuses the profile.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urljoin, urlparse

import httpx

from sellbird.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("sellbird.research")

RESEARCH_KEY = "research:"
RESEARCH_STALE_DAYS = 90
MAX_PAGES = 6
PAGE_CHARS = 5000
TIMEOUT = 15.0
FITS = ("good", "weak", "reject")

# Pages worth reading, by what their path or menu label says. Higher first.
_WANT = (
    (5, r"impressum|imprint|legal[- ]notice|mentions[- ]legales|aviso[- ]legal"),
    (4, r"leistungen|services?|angebot|kompetenzen|what[- ]we[- ]do|products?|"
        r"solutions?|loesungen|sortiment|so[- ]funktionierts?|how[- ]it[- ]works"),
    (3, r"ueber[- ]uns|uber[- ]uns|about(?:[- ]us)?|kanzlei|unternehmen|company|"
        r"team|wir|who[- ]we[- ]are"),
    (3, r"kontakt|contact|termin\w*|appointment|booking|anfrage"),
    (2, r"digital\w*|for[- ]sellers|for[- ]buyers|verkaufen|einkaufen"),
)
_SKIP = re.compile(
    r"(?i)\.(?:pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4|mp3)(?:$|\?)|"
    r"/(?:blog|news|aktuelles|presse|posts?|articles?|category|tag|author|feed|wp-json|"
    r"cart|checkout|login|account|warenkorb|karriere|careers?|jobs?|datenschutz|privacy|agb|terms)(?:/|$)|"
    r"/\d{4}/\d{2}/")
# Tried when the homepage has no readable menu (one built by JavaScript).
GUESS_PATHS = ("/impressum/", "/imprint/", "/leistungen/", "/services/", "/kanzlei/",
               "/ueber-uns/", "/about/", "/kontakt/", "/contact/")
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}


# ── reading the site ─────────────────────────────────────────────────────

def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def pick_links(html: str, page_url: str, limit: int = MAX_PAGES - 1) -> list[str]:
    """The site's own links to its legal notice, services, about and contact
    pages, best first. Same site only; no files, blog, careers or privacy."""
    from sellbird.collectors.profile import fold_accents, nfc
    from sellbird.collectors.people import _strip_code

    host = _host(page_url)
    seen = {urlparse(page_url).path.rstrip("/") or "/"}
    scored = []
    for i, m in enumerate(re.finditer(
            r'(?is)<a\b[^>]*?href\s*=\s*["\']([^"\'#][^"\']*)["\'][^>]*>(.*?)</a>',
            _strip_code(html or ""))):
        href = html_lib.unescape(m.group(1)).strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        p = urlparse(url)
        if p.scheme not in ("http", "https") or _host(url) != host or _SKIP.search(url):
            continue
        path = p.path.rstrip("/") or "/"
        if path in seen or path in ("/", "/index.html", "/index.php", "/home"):
            continue
        label = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(2)))).strip()
        hay = fold_accents(nfc(f"{unquote(path)} {label if len(label.split()) <= 4 else ''}")).lower()
        score = max((w for w, pat in _WANT if re.search(rf"(?:^|[^a-z])(?:{pat})(?:$|[^a-z])", hay)),
                    default=0)
        if score:
            seen.add(path)
            scored.append((-score, len(path), i, url))
    scored.sort()
    picked, groups = [], set()
    for score, _, _, url in scored:
        # One page per kind first (one services page, one contact page...).
        if score in groups and len(scored) > limit:
            continue
        groups.add(score)
        picked.append(url)
        if len(picked) >= limit:
            break
    for score, _, _, url in scored:
        if len(picked) >= limit:
            break
        if url not in picked:
            picked.append(url)
    return picked


def page_text(html: str) -> str:
    """Visible text of a page, one line per element, without menus repeated."""
    from sellbird.collectors.people import page_lines
    lines, seen = [], set()
    for line in page_lines(html or ""):
        if len(line) < 3 or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return "\n".join(lines)


async def _get(client, url: str) -> str:
    from sellbird.collectors.profile import looks_like_challenge
    try:
        resp = await client.get(url, headers=_HEADERS)
    except (httpx.HTTPError, ValueError) as e:
        logger.debug(f"research: could not read {url[:80]}: {e}")
        return ""
    except Exception as e:  # the public client refuses private addresses
        logger.debug(f"research: refused {url[:80]}: {e}")
        return ""
    text = getattr(resp, "text", "") or ""
    if resp.status_code != 200 or looks_like_challenge(text, resp.status_code):
        return ""
    if "html" not in (resp.headers.get("content-type") or "html").lower():
        return ""
    return text


async def read_site(domain: str, website: str = "", client=None) -> list[tuple[str, str]]:
    """[(url, text)] for the homepage and the most useful linked pages."""
    from sellbird.public_http import public_http_client

    if not domain:
        return []
    starts = [f"https://{domain}/"]
    if website and _host(website) == domain.removeprefix("www.") and website.rstrip("/") != starts[0].rstrip("/"):
        starts.append(website)
    own = client is None
    if own:
        client = public_http_client(timeout=TIMEOUT)
    pages: list[tuple[str, str]] = []
    try:
        home_html = await _get(client, starts[0])
        if not home_html:
            home_html = await _get(client, f"https://www.{domain.removeprefix('www.')}/")
        links: list[str] = []
        if home_html:
            pages.append((starts[0], page_text(home_html)))
            links = pick_links(home_html, starts[0])
        if home_html and len(links) < 2:
            links += [f"https://{_host(starts[0])}{path}" for path in GUESS_PATHS]
        for url in [*starts[1:], *links]:
            if len(pages) >= MAX_PAGES:
                break
            if any(u.rstrip("/") == url.rstrip("/") for u, _ in pages):
                continue
            html = await _get(client, url)
            if html:
                pages.append((url, page_text(html)))
                if not links and len(pages) == 1:
                    # The homepage failed but a subpage worked: follow its menu.
                    links = pick_links(html, url)
    finally:
        if own:
            await client.aclose()
    return [(u, t) for u, t in pages if t.strip()]


# ── asking the brain ─────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(str(text or ""))).strip().lower()


def research_prompt(company: str, pages: list[tuple[str, str]], config) -> str:
    product = getattr(config, "product", None)
    offer = getattr(getattr(product, "offer", None), "primary", "") if product else ""
    entry = getattr(getattr(product, "offer", None), "entry", "") if product else ""
    persona = getattr(config, "persona", None)
    icp = getattr(config, "icp", None)
    body = "\n\n".join(f"=== {url} ===\n{text[:PAGE_CHARS]}" for url, text in pages)
    return f"""You research companies before {getattr(persona, 'company', 'we')} writes to them.

WHAT WE SELL
- {getattr(product, 'name', '')}: {getattr(product, 'description', '')}
- Main offer: {offer}
- Free first step: {entry}
- Industries we target: {', '.join(getattr(icp, 'industries', None) or [])}

Read the pages of {company!r} below and return what they say, nothing more.

{fence(body, "company website")}

{UNTRUSTED_RULE}

Return ONE JSON object with these keys:
- "what_they_do": one or two plain sentences on what the company actually sells or does.
- "customers": who their customers are, as the site says ("" if it doesn't).
- "size": team size or other size figures the site states ("" if none).
- "language": ISO 639-1 code of the site's default language (the language of
  its homepage, not of a legally required page such as an Impressum).
- "people": managers or owners the site names, as [{{"name": "...", "role": "...",
  "salutation": "..."}}]. salutation is a courtesy title in the site's language
  ("Mr", "Ms", "Herr", "Frau", "M.", "Mme", "Sr.", "Sra." ...) ONLY when the site
  itself states it or a gendered job title makes it plain; otherwise "".
  At most 4, most senior first.
- "intake": how new enquiries, orders or documents reach them, as the site
  describes it (contact form and what it asks, an email link, a booking tool,
  a portal, sellers emailing offers ...), "" if the site doesn't say.
- "already_has": tools, suppliers or in-house solutions the site says they
  already use for what we sell ("" if none).
- "competitor": true if they themselves sell what we sell (the same kind of
  product or service as WHAT WE SELL above) to companies like our targets,
  else false. A company in a related field is not automatically a competitor:
  judge whether a buyer would choose between us and them.
- "fit": "good", "weak" or "reject" for our offer.
  reject = a competitor, a directory/marketplace listing page rather than a
  company, a dead or parked site, or clearly no manual process we could help with.
  weak = real company, but our offer is unlikely to land (e.g. they already
  have what we sell, or nothing on the site points to a need for it).
  good = a real company in or near our target industries where the site points
  to a need our offer meets.
- "reason": one sentence explaining the fit.
- "hooks": up to 3 concrete observations a cold email could open with, each
  about THIS company and tied to a need our offer meets, as
  [{{"observation": "...", "evidence": "...", "url": "..."}}]. evidence must be
  copied word for word from the pages above (a short phrase, max 15 words) and
  url is the page it is on. No hook about the industry in general.
"""


def _clean(result, pages: list[tuple[str, str]]) -> dict | None:
    """The model's profile with types fixed and unsupported hooks dropped."""
    if not isinstance(result, dict):
        return None
    out = {k: str(result.get(k) or "").strip()[:600]
           for k in ("what_they_do", "customers", "size", "intake", "already_has", "reason")}
    lang = str(result.get("language") or "").strip().lower()[:5]
    out["language"] = lang.split("-")[0] if re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", lang) else ""
    out["competitor"] = result.get("competitor") is True
    fit = str(result.get("fit") or "").strip().lower()
    out["fit"] = fit if fit in FITS else "weak"
    if out["competitor"]:
        out["fit"] = "reject"

    corpus = _norm(" ".join(t for _, t in pages))
    people = []
    for p in result.get("people") or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()[:80]
        # A name the pages don't contain is not a person we can greet.
        if not name or _norm(name) not in corpus:
            continue
        sal = str(p.get("salutation") or "").strip()
        # A courtesy title is a word or two ("Frau", "Mme", "Sr."), never a sentence.
        sal = sal if re.fullmatch(r"[^\W\d_]{1,6}\.?", sal) else ""
        people.append({"name": name, "role": str(p.get("role") or "").strip()[:120],
                       "salutation": sal})
    out["people"] = people[:4]

    hooks = []
    for h in result.get("hooks") or []:
        if not isinstance(h, dict):
            continue
        evidence = str(h.get("evidence") or "").strip().strip('"„“”\'')
        obs = str(h.get("observation") or "").strip()[:400]
        if obs and evidence and len(evidence) >= 6 and _norm(evidence) in corpus:
            hooks.append({"observation": obs, "evidence": evidence[:200],
                          "url": str(h.get("url") or "")[:300]})
    out["hooks"] = hooks[:3]
    out["pages"] = [u for u, _ in pages]
    out["researched_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    return out


async def research_company(brain, company, config, *, client=None) -> dict | None:
    """A fresh profile for ``company`` (a Company), or None when the site
    can't be read or the brain gave nothing usable."""
    pages = await read_site(company.domain, company.website, client=client)
    if not pages:
        return None
    result = await brain.think_json(
        research_prompt(company.name or company.domain, pages, config),
        session_id="sellbird-research", agent="writer", task="research",
    )
    return _clean(result, pages)


async def cached(state, company_id: str) -> dict | None:
    """The stored profile while it is fresh, else None."""
    raw = await state.get_setting(RESEARCH_KEY + company_id)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        when = datetime.fromisoformat(data.get("researched_at", ""))
    except (ValueError, TypeError):
        return None
    if datetime.now(timezone.utc).replace(tzinfo=None) - when > timedelta(days=RESEARCH_STALE_DAYS):
        return None
    return data


async def save(state, company_id: str, profile: dict) -> None:
    await state.set_setting(RESEARCH_KEY + company_id, json.dumps(profile, ensure_ascii=False))


LANGUAGES = {"de": "German", "en": "English", "fr": "French", "es": "Spanish", "it": "Italian",
             "nl": "Dutch", "pl": "Polish", "pt": "Portuguese", "he": "Hebrew"}


def language_name(code: str) -> str:
    return LANGUAGES.get(code or "", code or "")


def greeting_person(profile: dict | None) -> dict | None:
    """The person a general-inbox email should greet: the first named manager."""
    for p in (profile or {}).get("people") or []:
        if p.get("name"):
            return p
    return None


def fact_lines(profile: dict | None) -> list[str]:
    """The profile as FACTS lines for the Writer's prompt."""
    if not profile:
        return []
    lines = ["- Research from their own website (read today; every line below is what the site says):"]
    for key, label in (("what_they_do", "What they do"), ("customers", "Their customers"),
                       ("size", "Size"), ("intake", "How enquiries/orders reach them"),
                       ("already_has", "What they already use for this")):
        if profile.get(key):
            lines.append(f"  - {label}: {profile[key]}")
    if profile.get("language"):
        lines.append(f"  - Website language: {language_name(profile['language'])}")
    people = profile.get("people") or []
    if people:
        lines.append("  - People the site names: " + "; ".join(
            f"{(p['salutation'] + ' ') if p.get('salutation') else ''}{p['name']} ({p.get('role') or 'role not stated'})"
            for p in people))
    for h in profile.get("hooks") or []:
        lines.append(f"  - Possible opening: {h['observation']} (the site says: \"{h['evidence']}\")")
    return lines
