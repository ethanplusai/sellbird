"""Care services in the Crown Dependencies: Jersey, Guernsey, the Isle of Man.

None of the three islands is covered by CQC, and each regulates adult care
under its own law. What each one publishes, and what Harvey can use:

- **Jersey** — the Jersey Care Commission registers care homes, home care and
  adult day care under the Regulation of Care (Jersey) Law 2014 and inspects
  them at least once a year. Its "Find a Care Service" search
  (carecommission.je) lists every registered service with its parish,
  postcode, phone and provider, and each service's page links its inspection
  reports as PDFs. robots.txt allows everything with ``Crawl-delay: 10``, which
  this module keeps to. Read here.
- **Isle of Man** — the Registration and Inspection Unit (Department of Health
  and Social Care) publishes reports as PDFs on gov.im, but gov.im answers
  automated requests with a bot challenge. Harvey doesn't get around those, so
  the island is left out (``ISLANDS["im"]["usable"]`` is False).
- **Guernsey** — care homes are still registered and inspected by Health &
  Social Care's Registration and Inspection unit under the Nursing and
  Residential Homes (Guernsey) Law 1976. It publishes no register and no
  reports on gov.gg (homes post their own reports on their own sites), and
  home care isn't registered there at all, so the island is left out.

The Jersey register is small (about a hundred adult services), so it is
read whole and kept as JSON in ``data_dir`` for ``MAX_AGE_DAYS``.

The Commission publishes no coordinates. Each service gets its parish's
centre instead (Jersey is about 14 km across), so a radius search works to
within a parish; ``search`` by place ("St Helier", "Jersey") is exact.

Reports are PDFs, and no PDF library is installed, so ``pdf_text`` reads
them with the standard library: Word-made PDFs, Flate streams, ToUnicode maps.
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re
import time
import zlib
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from harvey.collectors.cqc import Location, search  # noqa: F401  (search is re-exported)

logger = logging.getLogger(__name__)

MAX_AGE_DAYS = 14
REPORT_MAX_AGE_DAYS = 30
REPORT_CHARS = 2500
PDF_MAX_BYTES = 15 * 1024 * 1024
CRAWL_DELAY = 10.0          # seconds between requests to one host (robots.txt)
CACHE_FILE = "crown_dependencies.json"
REPORTS_FILE = "crown_dependencies_reports.json"

ISLANDS = {
    "je": {"name": "Jersey", "regulator": "Jersey Care Commission", "usable": True},
    "gg": {"name": "Guernsey", "usable": False,
           "regulator": "Health & Social Care, Registration and Inspection unit",
           "why": "no register or reports published on gov.gg"},
    "im": {"name": "Isle of Man", "usable": False,
           "regulator": "DHSC Registration and Inspection Unit",
           "why": "gov.im answers automated requests with a bot challenge"},
}

# ── Jersey ───────────────────────────────────────────────────────────────

JE_BASE = "https://carecommission.je"
JE_SEARCH = JE_BASE + "/?sfid=4744"
JE_SERVICE = JE_BASE + "/care_service_search/{slug}/"
JE_PER_PAGE = 25
JE_MAX_PAGES = 12

# The Commission's service types -> the short names cqc.search understands.
# Adult day care, children's homes, fostering and the rest are left out.
JE_SERVICE_TYPES = {"Care Home": "care home", "Home Care": "home care"}

# Parish centres, approximate (the register gives a parish, not a point).
JE_PARISHES = {
    "st helier": (49.186, -2.107), "st saviour": (49.197, -2.085),
    "st clement": (49.172, -2.064), "grouville": (49.188, -2.040),
    "st martin": (49.215, -2.050), "trinity": (49.232, -2.095),
    "st john": (49.243, -2.137), "st lawrence": (49.215, -2.135),
    "st mary": (49.235, -2.170), "st ouen": (49.228, -2.205),
    "st peter": (49.200, -2.185), "st brelade": (49.185, -2.200),
}

_COMPANY = re.compile(
    r"\b(ltd|limited|llp|llc|plc|inc|cic|incorporated|trust|charity|association|"
    r"society|foundation|government of jersey|minister)\b\.?", re.I)
_HOLDER = re.compile(
    r'<div class="post-holder[^"]*\bcare_service_search\b.*?'
    r'(?=<div class="post-holder|<div id="sidebar"|</section>|$)', re.S)


def _text(fragment: str) -> str:
    """Visible text of an HTML fragment, on one line."""
    fragment = re.sub(r"<br\s*/?>", "\n", fragment or "")
    fragment = _html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", x).strip() for x in fragment.split("\n")]
    return ", ".join(x for x in lines if x)


def _slug(url: str) -> str:
    match = re.search(r"/care_service_search/([^/?#]+)", url or "")
    return match.group(1) if match else ""


def ownership_from_name(provider: str) -> str:
    """"Organisation" when the provider's name says it's a company, charity or
    the States; "" when it doesn't (the Commission doesn't publish the legal
    form, so a sole trader or partnership can't be told apart)."""
    return "Organisation" if _COMPANY.search(provider or "") else ""


def _parish_point(parish: str) -> tuple[float | None, float | None]:
    key = re.sub(r"[.\s]+", " ", (parish or "").lower().replace("saint", "st")).strip()
    return JE_PARISHES.get(key, (None, None))


def parse_jersey_listing(page_html: str) -> list[Location]:
    """The services on one page of the Commission's search results."""
    out = []
    for block in _HOLDER.findall(page_html or ""):
        link = re.search(r'<h4 class="post-title"[^>]*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>',
                         block, re.S)
        if not link:
            continue
        slug = _slug(link.group(1))
        kind = re.search(r'<div class="care_home">(.*?)</div>', block, re.S)
        short = JE_SERVICE_TYPES.get(_text(kind.group(1)) if kind else "")
        if not slug or not short:
            continue
        street = re.search(r'<div class="addr_cols">(.*?)</div>(.*?)</div>', block, re.S)
        address = _text(street.group(1)) if street else ""
        tail = [x.strip() for x in _text(street.group(2)).split(",")] if street else []
        parish = tail[0] if tail else ""
        postcode = tail[1] if len(tail) > 1 else ""
        phone = re.search(r'href="tel:([^"]*)"', block)
        provider = re.search(r"<b>Provider:\s*</b>.*?<span[^>]*>(.*?)</span>", block, re.S)
        provider_name = _text(provider.group(1)) if provider else ""
        lat, lng = _parish_point(parish)
        out.append(Location(
            id=f"je-{slug}",
            name=_text(link.group(2)),
            phone=(phone.group(1).strip() if phone else ""),
            address=address,
            city=parish,
            postcode=postcode,
            local_authority="Jersey",
            lat=lat, lng=lng,
            services=[short],
            provider_id=f"je-provider:{provider_name.lower()}" if provider_name else "",
            provider_name=provider_name,
            ownership=ownership_from_name(provider_name),
        ))
    return out


def jersey_result_count(page_html: str) -> int:
    match = re.search(r"We found\s*<b>\s*(\d+)\s*</b>", page_html or "")
    return int(match.group(1)) if match else 0


def jersey_search_url(service_type: str, nursing: bool = False, page: int = 1) -> str:
    url = JE_SEARCH + "&_sfm_service_type=" + quote(service_type)
    if nursing:
        url += "&_sfm_type_of_care=Nursing"
    if page > 1:
        url += f"&sf_paged={page}"
    return url


# ── polite fetching ──────────────────────────────────────────────────────

_last_request: dict[str, float] = {}
_locks: dict[tuple[int, str], asyncio.Lock] = {}


async def _get(client, url: str):
    """GET, keeping CRAWL_DELAY seconds between requests to one host."""
    host = re.sub(r"^https?://([^/]+).*", r"\1", url)
    # A lock belongs to one event loop; Harvey may run several over its life.
    lock = _locks.setdefault((id(asyncio.get_running_loop()), host), asyncio.Lock())
    async with lock:
        wait = _last_request.get(host, 0) + CRAWL_DELAY - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await client.get(url)
        finally:
            _last_request[host] = time.monotonic()
    resp.raise_for_status()
    return resp


async def _jersey_pages(client, service_type: str, nursing: bool = False) -> list[Location]:
    first = (await _get(client, jersey_search_url(service_type, nursing))).text
    found = parse_jersey_listing(first)
    pages = min(JE_MAX_PAGES, -(-jersey_result_count(first) // JE_PER_PAGE))
    for page in range(2, pages + 1):
        more = parse_jersey_listing(
            (await _get(client, jersey_search_url(service_type, nursing, page))).text)
        if not more:
            break
        found.extend(more)
    return found


async def read_jersey(client) -> list[Location]:
    """Every registered care home and home care service in Jersey."""
    by_id: dict[str, Location] = {}
    for service_type in JE_SERVICE_TYPES:
        for loc in await _jersey_pages(client, service_type):
            by_id.setdefault(loc.id, loc)
    for loc in await _jersey_pages(client, "Care Home", nursing=True):
        if loc.id in by_id:
            by_id[loc.id].services = ["nursing home"]
    out = list(by_id.values())
    per_provider: dict[str, int] = {}
    for loc in out:
        per_provider[loc.provider_id] = per_provider.get(loc.provider_id, 0) + 1
    for loc in out:
        loc.provider_locations = per_provider.get(loc.provider_id, 1)
    return out


# ── the register ─────────────────────────────────────────────────────────


def _fresh(path: Path, max_age_days: int = MAX_AGE_DAYS) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400


def save_cache(locations: list[Location], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(loc) for loc in locations]), encoding="utf-8")
    tmp.replace(path)


def load_cache(path: Path) -> list[Location]:
    return [Location(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


async def load_register(client, data_dir: Path) -> list[Location]:
    """Adult care services across the Crown Dependencies (Jersey only today;
    see ISLANDS for why the others are left out).

    From the local copy when it's fresh, else read again. An old copy is used
    with a warning when the Commission can't be reached; with no copy at all
    it raises RuntimeError with a plain reason.
    """
    cache = data_dir / CACHE_FILE
    if _fresh(cache):
        return load_cache(cache)
    try:
        locations = await read_jersey(client)
        if not locations:
            raise RuntimeError("the Jersey Care Commission's search listed no services")
    except Exception as e:
        if cache.exists():
            logger.warning("crown dependencies: couldn't refresh the register (%s); "
                           "using the copy from before", e)
            return load_cache(cache)
        raise RuntimeError(f"Couldn't read the Jersey Care Commission register: {e}") from e
    save_cache(locations, cache)
    logger.info("crown dependencies: %d adult care services in Jersey", len(locations))
    return locations


def service_url(service_id: str) -> str:
    """The service's public page on its regulator's site ("" if unknown).

    Use this, not ``Location.url``: that property always builds a CQC link.
    """
    island, _, rest = (service_id or "").partition("-")
    if island == "je" and rest:
        return JE_SERVICE.format(slug=rest)
    return ""


# ── the latest inspection report ─────────────────────────────────────────

_REPORT_ROW = re.compile(
    r"<tr>\s*<td[^>]*>\s*([^<]*?\d{4})\s*</td>\s*<td[^>]*>\s*([^<]*?)\s*</td>.*?"
    r'href="([^"]+\.pdf)"', re.S)
_MANAGER = re.compile(r"Manager's Name:\s*</b>\s*</td>\s*<td[^>]*>\s*([^<]*?)\s*</td>", re.S)


def _date_key(value: str) -> tuple:
    match = re.search(r"(\d{1,2})\s+(\w+),?\s+(\d{4})", value or "")
    if not match:
        return (0, 0, 0)
    months = ["january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december"]
    month = match.group(2).lower()
    m = months.index(month) + 1 if month in months else 0
    return (int(match.group(3)), m, int(match.group(1)))


def jersey_reports(page_html: str) -> list[dict]:
    """[{"date", "kind", "url"}] from a service page, newest first."""
    section = page_html or ""
    # The sidebar links the sections too (scroll_id="..."), so match whole ids.
    start = re.search(r'(?<![\w-])id="insp_report"', section)
    if start:
        end = re.search(r'(?<![\w-])id="provider_details"', section[start.end():])
        section = section[start.start():start.end() + end.start() if end else None]
    rows = [{"date": re.sub(r",", "", d).strip(), "kind": k.strip(), "url": u}
            for d, k, u in _REPORT_ROW.findall(section)]
    rows.sort(key=lambda r: _date_key(r["date"]), reverse=True)
    return rows


def pick_report(rows: list[dict]) -> dict:
    """The newest inspection's summary report, else its full report."""
    if not rows:
        return {}
    newest = _date_key(rows[0]["date"])
    same = [r for r in rows if _date_key(r["date"]) == newest]
    for row in same:
        if "summary" in row["kind"].lower():
            return row
    return same[0]


_FINDINGS_START = ("The following is a summary of what we found",
                   "SUMMARY OF INSPECTION FINDINGS", "Summary of inspection findings")
_AREA = re.compile(r"Area for Improvement\s*(\d+)(.*?)(?=Area for Improvement\s*\d|"
                   r"IMPROVEMENT PLAN|$)", re.S | re.I)


def report_summary_from(text: str, limit: int = REPORT_CHARS) -> str:
    """What the inspectors found, then each area for improvement, trimmed.

    The provider's own response to each area is left out: it's their words,
    not the inspector's.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    starts = [i for i in (text.find(m) for m in _FINDINGS_START) if i >= 0]
    start = min(starts, default=0)
    first_area = re.search(r"Area for Improvement\s*\d", text[start:], re.I)
    overview = text[start:start + first_area.start()] if first_area else text[start:]
    overview = re.split(r"SUMMARY OF INSPECTION FINDINGS", overview, flags=re.I)[0]
    overview = re.sub(r"^The following is a summary of what we found during this "
                      r"inspection\.\s*(Further information about our findings is "
                      r"contained in the main body of this report\.)?\s*", "",
                      overview).strip()
    areas = []
    for number, body in _AREA.findall(text[start:]):
        body = re.split(r"Response by the Registered Provider", body, flags=re.I)[0]
        body = re.sub(r"^.*?To be completed:\s*(by\s*\d{1,2}/\d{1,2}/\d{4}|"
                      r"with immediate effect|[^.]*?\d{4})\s*", "", body, flags=re.I)
        body = body.strip(" .")
        if body:
            areas.append(f"Area for improvement {number}: {body}.")
    out = " ".join([overview] + areas).strip()
    if len(out) > limit:
        out = out[:limit].rsplit(". ", 1)[0] + "."
    return out


async def latest_report(client, service_id: str, data_dir: Path) -> dict:
    """{"ratings", "summary", "url", "published", "manager"} for a service's
    latest inspection, the same shape as ``cqc.latest_report``.

    ``ratings`` is always {}: none of the island regulators rate services.
    ``manager`` is the Registered Manager as the Commission writes it
    ("Forename Surname"), or "". {} for an island Harvey can't read, when the
    regulator can't be reached, or when there's no report yet. Cached per
    service in data_dir for REPORT_MAX_AGE_DAYS.
    """
    page = service_url(service_id)
    if not page:
        return {}
    cache_path = data_dir / REPORTS_FILE
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(service_id)
    if hit and time.time() - hit.get("at", 0) < REPORT_MAX_AGE_DAYS * 86400:
        return hit.get("report", {})
    report = {}
    try:
        resp = await _get(client, page)
        manager = _MANAGER.search(resp.text)
        chosen = pick_report(jersey_reports(resp.text))
        if chosen:
            pdf = await _get(client, chosen["url"])
            if len(pdf.content) > PDF_MAX_BYTES:
                raise RuntimeError("report PDF is too large")
            summary = report_summary_from(pdf_text(pdf.content))
            report = {"ratings": {}, "summary": summary, "url": chosen["url"],
                      "published": chosen["date"],
                      "manager": _text(manager.group(1)) if manager else ""}
    except Exception as e:
        logger.warning("crown dependencies: couldn't read the report for %s: %s",
                       service_id, e)
        return {}
    cache[service_id] = {"at": time.time(), "report": report}
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as e:
        logger.warning("crown dependencies: couldn't save the report cache: %s", e)
    return report


# ── PDF text, standard library only ──────────────────────────────────────
#
# Enough of PDF for reports written in Word: objects (also inside object
# streams), Flate-compressed content, fonts with a ToUnicode map (two-byte
# Identity-H or one-byte) or WinAnsi, and the text operators. Runs on the
# same line are joined as written; a move to a new line is a space.

_OBJ = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", re.S)
_REF = rb"(\d+)\s+\d+\s+R"
_NUM = re.compile(rb"-?\d*\.?\d+$")
_TOKENS = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/<>\[\]()]+|"
                     rb"-?\d*\.?\d+|[A-Za-z'\"*]+")


def _stream(body: bytes) -> bytes | None:
    match = re.search(rb"stream\r?\n", body)
    if not match:
        return None
    raw = body[match.end():]
    end = raw.rfind(b"endstream")
    raw = raw[:end] if end >= 0 else raw
    head = body[:match.start()]
    if b"/FlateDecode" in head:
        try:
            return zlib.decompressobj().decompress(raw)
        except zlib.error:
            return None
    return None if b"/Filter" in head else raw


def _objects(data: bytes) -> dict[int, bytes]:
    objs = {int(m.group(1)): m.group(2) for m in _OBJ.finditer(data)}
    for body in list(objs.values()):
        if not re.search(rb"/Type\s*/ObjStm", body):
            continue
        n = re.search(rb"/N\s+(\d+)", body)
        first = re.search(rb"/First\s+(\d+)", body)
        content = _stream(body)
        if not (n and first and content):
            continue
        head = content[:int(first.group(1))].split()
        pairs = [(int(head[i]), int(head[i + 1])) for i in range(0, len(head) - 1, 2)]
        base = int(first.group(1))
        for i, (num, off) in enumerate(pairs):
            end = pairs[i + 1][1] if i + 1 < len(pairs) else len(content) - base
            objs.setdefault(num, content[base + off:base + end])
    return objs


def _cmap(data: bytes) -> dict[int, str]:
    out: dict[int, str] = {}

    def uni(hexs: bytes) -> str:
        try:
            return bytes.fromhex(hexs.decode()).decode("utf-16-be", "replace")
        except ValueError:
            return ""
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            out[int(src, 16)] = uni(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, rest in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]+>|\[[^\]]*\])", block):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            if rest.startswith(b"["):
                for i, dst in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", rest)):
                    out[lo_i + i] = uni(dst)
            else:
                start = int(rest[1:-1], 16)
                for i in range(min(hi_i - lo_i, 0xFFFF) + 1):
                    out[lo_i + i] = chr(start + i) if start + i < 0x110000 else ""
    return out


def _dict_of(body: bytes, key: bytes, objs: dict[int, bytes]) -> bytes:
    """The << >> dictionary under /key, following an indirect reference."""
    match = re.search(rb"/" + key + rb"\s*(?:" + _REF + rb"|<<)", body)
    if not match:
        return b""
    if match.group(1):
        return objs.get(int(match.group(1)), b"")
    start = i = match.end() - 2
    depth = 0
    while i < len(body) - 1:
        pair = body[i:i + 2]
        if pair in (b"<<", b">>"):
            depth += 1 if pair == b"<<" else -1
            i += 2
            if depth == 0:
                return body[start:i]
            continue
        i += 1
    return body[start:]


def _fonts(page: bytes, objs: dict[int, bytes], cmaps: dict[int, dict]) -> dict:
    """{font resource name: (two-byte codes?, ToUnicode map)} for a page."""
    fonts = _dict_of(_dict_of(page, b"Resources", objs), b"Font", objs)
    out = {}
    for name, num in re.findall(rb"/([^\s/<>\[\]()]+)\s+" + _REF, fonts):
        font = objs.get(int(num), b"")
        cmap: dict[int, str] = {}
        tu = re.search(rb"/ToUnicode\s+" + _REF, font)
        if tu:
            key = int(tu.group(1))
            if key not in cmaps:
                cmaps[key] = _cmap(_stream(objs.get(key, b"")) or b"")
            cmap = cmaps[key]
        out[name] = (b"/Identity-H" in font or b"/Type0" in font, cmap)
    return out


def _unescape(s: bytes) -> bytes:
    out, i = bytearray(), 0
    simple = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
    while i < len(s):
        c = s[i:i + 1]
        if c != b"\\" or i + 1 >= len(s):
            out += c
            i += 1
            continue
        n = s[i + 1:i + 2]
        if n in simple:
            out += simple[n]
            i += 2
        elif n.isdigit():
            octal = re.match(rb"[0-7]{1,3}", s[i + 1:])
            if not octal:
                out += n
                i += 2
                continue
            out.append(int(octal.group(0), 8) & 0xFF)
            i += 1 + len(octal.group(0))
        elif n in (b"\r", b"\n"):
            i += 2
        else:
            out += n
            i += 2
    return bytes(out)


def _string(tok: bytes, font: tuple) -> str:
    if tok[:1] == b"(":
        raw = _unescape(tok[1:-1])
    else:
        hexs = re.sub(rb"\s", b"", tok[1:-1])
        raw = bytes.fromhex((hexs + b"0" * (len(hexs) % 2)).decode())
    two, cmap = font
    codes = ([int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]
             if two else list(raw))
    if cmap:
        return "".join(cmap.get(c, "") for c in codes)
    return "" if two else raw.decode("cp1252", "replace")


def _content_text(content: bytes, fonts: dict) -> str:
    out: list[str] = []
    font: tuple = (False, {})
    operands: list[bytes] = []
    array: list[bytes] | None = None
    in_array = False
    y: float | None = None
    for tok in _TOKENS.findall(content):
        if tok == b"[":
            array, in_array = [], True
            continue
        if tok == b"]":
            in_array = False
            continue
        if in_array:
            array.append(tok)
            continue
        if tok[:1] in (b"(", b"<", b"/") or _NUM.match(tok):
            operands.append(tok)
            continue
        op = tok
        if op == b"Tf" and len(operands) >= 2:
            font = fonts.get(operands[-2][1:], (False, {}))
        elif op in (b"Td", b"TD", b"Tm") and len(operands) >= 2:
            try:
                ny = float(operands[-1]) + (y or 0.0 if op != b"Tm" else 0.0)
            except ValueError:
                ny = None
            if y is not None and (ny is None or abs(ny - y) > 1):
                out.append(" ")
            y = ny
        elif op in (b"T*", b"'", b'"'):
            out.append(" ")
            if op != b"T*" and operands:
                out.append(_string(operands[-1], font))
        elif op == b"Tj" and operands:
            out.append(_string(operands[-1], font))
        elif op == b"TJ" and array is not None:
            for item in array:
                if item[:1] in (b"(", b"<"):
                    out.append(_string(item, font))
                elif _NUM.match(item) and float(item) < -200:
                    out.append(" ")
        operands, array = [], None
    return "".join(out)


def pdf_text(data: bytes) -> str:
    """The text of a PDF, pages in file order, on one line ("" if unreadable)."""
    objs = _objects(data or b"")
    cmaps: dict[int, dict] = {}
    parts = []
    for num in sorted(objs):
        page = objs[num]
        if not re.search(rb"/Type\s*/Page(?![a-zA-Z])", page):
            continue
        fonts = _fonts(page, objs, cmaps)
        contents = re.search(rb"/Contents\s*(?:" + _REF + rb"|\[(.*?)\])", page, re.S)
        if not contents:
            continue
        refs = ([int(contents.group(1))] if contents.group(1)
                else [int(r) for r in re.findall(_REF, contents.group(2))])
        for ref in refs:
            stream = _stream(objs.get(ref, b""))
            if stream:
                parts.append(_content_text(stream, fonts))
        parts.append(" ")
    return re.sub(r"\s+", " ", "".join(parts)).strip()
