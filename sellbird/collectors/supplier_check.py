"""Skip companies whose own website mentions a brand you don't want to pitch.

The use case: a kit retailer for one brand shouldn't cold-email a club that
already wears that brand. They're either a customer already (through this
store's own club shop) or buy it elsewhere, and a pitch reads as if nobody
looked at their site. ``icp.exclude_if_site_mentions`` lists the brands
("acmekit"); the check reads the company's homepage and up to a handful of
its own kit / shop / sponsor / about pages, and any whole-word mention of a
brand, in text or in a link or image address, excludes the company.

A site that can't be read gives no answer either way: the company is not
excluded, and the check is tried again next time instead of cached. A
result is cached per company for ``RECHECK_AFTER`` so a running Sellbird reads
each site once, not every cycle.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("sellbird.collectors.supplier_check")

CACHE_KEY = "supplier_check:"
RECHECK_AFTER = timedelta(days=30)
MAX_EXTRA_PAGES = 6
MAX_BYTES = 400_000

# Pages on their own site most likely to name a kit supplier.
_LINK_HINT = re.compile(
    r"kit|shop|store|sponsor|partner|merch|uniform|supplier|about|club-?shop", re.I)
_HREF = re.compile(r'<a\s[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.I | re.S)


def _pattern(brand: str) -> re.Pattern:
    # Whole word, so "acmekit" matches "Acmekit", "acmekit.example" and
    # "acmekit-logo.png" but not "acmekitchen". "<brand>store" counts too:
    # a link to a brand store's shop is the clearest sign of all.
    return re.compile(r"(?<![a-z0-9])" + re.escape(brand.strip().lower())
                      + r"(?!(?!store)[a-z])", re.I)


def find_mention(html: str, brands) -> tuple[str, str] | None:
    """The first brand mentioned in ``html``, with a short snippet, or None."""
    if not html:
        return None
    html = html[:MAX_BYTES]
    for brand in brands or []:
        if not str(brand).strip():
            continue
        m = _pattern(str(brand)).search(html)
        if m:
            start, end = max(0, m.start() - 60), min(len(html), m.end() + 60)
            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[start:end])).strip()
            return str(brand).strip(), (snippet or m.group(0))[:160]
    return None


def candidate_links(html: str, base_url: str, limit: int = MAX_EXTRA_PAGES) -> list[str]:
    """Same-site links whose address or text suggests kit, shop or sponsors."""
    host = urlparse(base_url).netloc.lower()
    out: list[str] = []
    for href, text in _HREF.findall(html[:MAX_BYTES]):
        url = urljoin(base_url, href.strip())
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host:
            continue
        if not _LINK_HINT.search(href + " " + re.sub(r"<[^>]+>", " ", text)):
            continue
        url = url.split("#")[0]
        if url not in out and url.rstrip("/") != base_url.rstrip("/"):
            out.append(url)
        if len(out) >= limit:
            break
    return out


async def check_site(website: str, brands, client=None) -> dict | None:
    """Read the site and return {'brand', 'url', 'evidence'} on a mention,
    {} when the site was read and is clear, or None if it couldn't be read."""
    if not website or not brands:
        return {}
    if not website.startswith(("http://", "https://")):
        website = "https://" + website
    own_client = client is None
    if own_client:
        from sellbird.public_http import public_http_client
        client = public_http_client(timeout=15.0, headers={"User-Agent": "Mozilla/5.0"})
    try:
        try:
            resp = await client.get(website)
        except Exception as e:
            logger.debug(f"Supplier check: couldn't read {website}: {e}")
            return None
        if resp.status_code >= 400:
            return None
        home, base = resp.text, str(resp.url)
        hit = find_mention(home, brands)
        if hit:
            return {"brand": hit[0], "url": base, "evidence": hit[1]}
        for url in candidate_links(home, base):
            try:
                page = await client.get(url)
            except Exception:
                continue
            if page.status_code >= 400:
                continue
            hit = find_mention(page.text, brands)
            if hit:
                return {"brand": hit[0], "url": str(page.url), "evidence": hit[1]}
        return {}
    finally:
        if own_client:
            await client.aclose()


async def excluded_supplier(state, company, brands, client=None) -> dict | None:
    """The cached-or-fresh mention for ``company``, or None if it's clear
    (or its site couldn't be read)."""
    brands = [b for b in (brands or []) if str(b).strip()]
    if not brands or company is None:
        return None
    website = getattr(company, "website", "") or (
        f"https://{company.domain}" if getattr(company, "domain", "") else "")
    if not website:
        return None
    key = CACHE_KEY + company.id
    wanted = sorted(b.lower() for b in brands)
    try:
        cached = json.loads(await state.get_setting(key) or "null")
        checked = datetime.fromisoformat(cached["checked_at"])
        if (cached.get("brands") == wanted
                and datetime.now(timezone.utc) - checked < RECHECK_AFTER):
            return cached.get("hit") or None
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        pass
    hit = await check_site(website, brands, client=client)
    if hit is None:
        return None          # unreadable: no verdict, try again next time
    await state.set_setting(key, json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "brands": wanted, "hit": hit or None,
    }))
    return hit or None
