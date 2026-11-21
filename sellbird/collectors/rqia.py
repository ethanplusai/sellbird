"""RQIA — Northern Ireland's register of adult care services, free.

The Regulation and Quality Improvement Authority registers every care home,
nursing home, domiciliary care agency (home care and supported living) and
adult placement agency in Northern Ireland. Three free files make the register:

* OpenDataNI's "RQIA Registered Services" spreadsheet, Open Government
  Licence, refreshed monthly: name with the RQIA ID after it, address,
  phone, service type, places, Registered Manager and provider.
* RQIA's own "All Registered Services" spreadsheet (rqia.org.uk/register):
  the same rows plus the Responsible Individual, refreshed less often.
  Joined on the ID (name + postcode when a row has none). If OpenDataNI
  can't be reached it is the register on its own.
* RQIA's list of completed inspections, for each service's HSC Trust.

None of them has a website, coordinates or a company number, so ``lat`` and
``lng`` are always None (a radius search finds nothing; search by town or
Trust instead) and the website is left for Scout to find. RQIA doesn't rate
services, so ``rating`` stays empty.

The register is kept as one small JSON file in ``data_dir`` and rebuilt when
it is older than ``MAX_AGE_DAYS``. Reports are PDFs: the latest care
inspection of each service is found through RQIA's sitemap (report pages
are named "<RQIA ID>_<service>_care_<date>"), downloaded once and read with
the small standard-library PDF reader at the bottom of this file. Its
quality improvement plan lists the areas for improvement, which is what the
writer uses to pick a course.

Everything is read with the standard library, like ``cqc``.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import re
import time
import zipfile
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from sellbird.collectors import cqc
from sellbird.collectors.cqc import CONSENT_NEEDED, search, services_for  # noqa: F401  (same searching as CQC)

logger = logging.getLogger(__name__)

ODNI_PAGE = "https://www.opendatani.gov.uk/dataset/rqia-regulated-services"
REGISTER_PAGE = "https://www.rqia.org.uk/register/"
INSPECTIONS_PAGE = "https://www.rqia.org.uk/inspections/recently-completed-inspection-reports/"
SITEMAP = "https://www.rqia.org.uk/wp-sitemap.xml"
# RQIA has no page per service. A service's public page is its latest
# inspection report's page (see RqiaLocation.url); this is the fallback.
LOCATION_URL = REGISTER_PAGE

MAX_AGE_DAYS = 35
INDEX_MAX_AGE_DAYS = 7
REPORT_MAX_AGE_DAYS = 30
REPORT_CHARS = 2500
MAX_PDF_BYTES = 20 * 1024 * 1024
# OpenDataNI refuses requests without a browser-like User-Agent.
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Sellbird)"}

_ODNI_LINK = re.compile(
    r'(https://admin\.opendatani\.gov\.uk/dataset/[^"\s]+/download/[^"\s]+\.xlsx)', re.I)
_REGISTER_LINK = re.compile(
    r'href="(https://www\.rqia\.org\.uk/download/[^"]*/all-registered-services\.xlsx)"', re.I)
_INSPECTIONS_LINK = re.compile(
    r'href="(https://www\.rqia\.org\.uk/download/[^"]*recently-completed-inspection-reports/[^"]+\.xlsx)"',
    re.I)
_SITEMAP_PART = re.compile(r"<loc>(https://www\.rqia\.org\.uk/wp-sitemap-posts-wpfd_file-\d+\.xml)</loc>")
_REPORT_SLUG = re.compile(
    r"<loc>(https://www\.rqia\.org\.uk/wpfd_file/(\d+)_([a-z0-9-]+?)_care_"
    r"(\d{1,2})-([a-z]+)-(\d{4})(?:-\d+)?/?)</loc>")
_NAME_ID = re.compile(r"[\s\xa0]*\((\d{3,8})\)\s*$")
_PDF_LINK = re.compile(r'href="(https://www\.rqia\.org\.uk/download/[^"]+\.pdf)"', re.I)

# RQIA's service type -> the short names Sellbird stores and searches (the
# same names as cqc.SERVICE_TYPES). Day care settings, nursing agencies,
# hospitals and clinics are left out.
SERVICE_TYPES = {
    "Nursing (NH)": "nursing home",
    "Residential (RC)": "care home",
    "Domiciliary Care Agency (DCA)": "home care",
    "Adult Placement Agency (APA)": "shared lives",
}
TRUSTS = {
    "BHSCT": "Belfast HSC Trust",
    "NHSCT": "Northern HSC Trust",
    "SEHSCT": "South Eastern HSC Trust",
    "SHSCT": "Southern HSC Trust",
    "WHSCT": "Western HSC Trust",
}

# A provider that is a person is written with a title: "Mr A Example", or
# "Mrs A Example & Mr B Example" for a partnership.
_TITLE = r"(?:mr|mrs|ms|miss|mx|dr|rev)\.?"
_PERSON = re.compile(rf"^\s*{_TITLE}\s+\S", re.I)
_TITLES = re.compile(rf"(?:^|[\s&,]){_TITLE}\s+(?=[A-Z])", re.I)
_PUBLIC = re.compile(r"\bHSC\b|Health and Social Care Trust", re.I)
_NOT_A_NAME = re.compile(
    r"\d|pending|vacan|applicab|acting|interim|none\b|registration|manager|tbc|n/a",
    re.I)


@dataclass
class RqiaLocation(cqc.Location):
    """A cqc.Location from RQIA's register.

    ``id`` is the RQIA service ID (``"NI-..."`` when RQIA's own file doesn't
    list the service yet, and then there's no report to read). ``page`` is the
    latest care inspection report's page, RQIA's nearest thing to a page for
    the service.
    """

    page: str = ""

    @property
    def url(self) -> str:
        return self.page or LOCATION_URL


# ── reading the spreadsheets ─────────────────────────────────────────────

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _col(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def _first_sheet(z: zipfile.ZipFile) -> str:
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheet = next(wb.iter(_NS + "sheet"))
        rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(_RELS + "Relationship"):
            if rel.get("Id") == rid:
                target = rel.get("Target", "").lstrip("/")
                return target if target.startswith("xl/") else "xl/" + target
    except (KeyError, StopIteration, ET.ParseError):
        pass
    return "xl/worksheets/sheet1.xml"


def xlsx_rows(data: bytes, header_cell: str):
    """Yield the first sheet's rows as dicts keyed by the header row.

    The header row is the first row with a cell equal to ``header_cell``
    (RQIA puts a title line above it). Header names are stripped.
    """
    z = zipfile.ZipFile(BytesIO(data))
    strings: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        strings = ["".join(t.text or "" for t in si.iter(_NS + "t"))
                   for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_NS + "si")]
    header: list[str] | None = None
    with z.open(_first_sheet(z)) as f:
        for _, el in ET.iterparse(f):
            if el.tag != _NS + "row":
                continue
            cells: dict[int, str] = {}
            for c in el.iter(_NS + "c"):
                kind, v = c.get("t"), c.find(_NS + "v")
                if kind == "s" and v is not None:
                    value = strings[int(v.text)]
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(_NS + "t"))
                else:
                    value = v.text if v is not None and v.text else ""
                cells[_col(c.get("r", "A"))] = value.strip()
            el.clear()
            if not cells:
                continue
            row = [cells.get(i, "") for i in range(max(cells) + 1)]
            if header is None:
                if header_cell in row:
                    header = row
                continue
            if any(row):
                yield {h: (row[i] if i < len(row) else "") for i, h in enumerate(header) if h}


def _key(*parts: str) -> str:
    return "|".join(re.sub(r"[^a-z0-9]", "", (p or "").lower()) for p in parts)


def ownership_of(provider: str) -> str:
    """Individual / Partnership / NHS Body / Organisation from a provider name."""
    if _PERSON.match(provider or ""):
        people = len(_TITLES.findall(provider))
        return "Partnership" if people > 1 or re.search(r"&| and ", provider) else "Individual"
    if _PUBLIC.search(provider or ""):
        return "NHS Body"
    return "Organisation" if provider else ""


def registry_name(value: str) -> str:
    """"Surname, Forename" (as cqc writes names) from "Ms Forename Surname".

    Several people are joined with ";". Notes like "– not registered" are
    dropped, and anything that isn't a person's name gives "".
    """
    out = []
    for one in re.split(r";|/|&|\band\b", value or ""):
        one = re.split(r"\s[-–—]\s|\(", one)[0]
        one = re.sub(rf"^\s*{_TITLE}\s+", "", one.strip(), flags=re.I).strip(" ,.")
        if not one or _NOT_A_NAME.search(one):
            continue
        words = one.split()
        if len(words) < 2:
            continue
        out.append(f"{words[-1]}, {' '.join(words[:-1])}")
    return "; ".join(out)


def _int(value: str) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def _phone(value: str) -> str:
    value = (value or "").strip()
    # Spreadsheets drop a leading zero from numbers stored as numbers.
    if re.fullmatch(r"\d{10}", value) and not value.startswith("0"):
        value = "0" + value
    return value


def _services(service_type: str, name: str) -> list[str]:
    short = SERVICE_TYPES.get((service_type or "").strip())
    if not short:
        return []
    if short == "home care" and "supported living" in (name or "").lower():
        # NI registers supported living as a domiciliary care agency of the
        # "supported living type"; the name is the only place the register says so.
        return ["supported living"]
    return [short]


def build_register(odni: list[dict], rqia: list[dict], inspections: list[dict],
                   index: dict | None = None) -> list[RqiaLocation]:
    """Join the three files into Locations (adult services only).

    ``odni`` (current) is the base; ``rqia`` adds the Responsible Individual
    (and the ID when an OpenDataNI name has none); ``inspections`` the HSC Trust. With no OpenDataNI
    rows, RQIA's own file is the base. ``index`` (from the report sitemap)
    adds each service's latest report page and fills a missing ID from the
    report's name.
    """
    index = index or {}
    by_id: dict[str, dict] = {}
    by_name_pc: dict[str, dict] = {}
    by_name: dict[str, list[dict]] = {}
    for r in rqia:
        name = r.get("Service", "")
        if r.get("Service ID"):
            by_id[r["Service ID"].lstrip("0")] = r
        by_name_pc[_key(name, r.get("Postcode", ""))] = r
        by_name.setdefault(_key(name), []).append(r)
    by_slug: dict[str, list[str]] = {}
    for sid, entry in index.items():
        by_slug.setdefault(_key(entry.get("name", "")), []).append(entry.get("id") or sid)
    trusts: dict[str, str] = {}
    for r in inspections:
        code = r.get("HSC Trust (Service)", "")
        if code not in TRUSTS:
            continue
        name = r.get("Service", "")
        tagged = _NAME_ID.search(name)
        if tagged:
            name = name[:tagged.start()]
            trusts["id:" + tagged.group(1).lstrip("0")] = TRUSTS[code]
        trusts[_key(name, r.get("Address 1: Postcode", ""))] = TRUSTS[code]
        trusts.setdefault(_key(name), TRUSTS[code])

    base = odni or [dict(r, **{"Service Name": r.get("Service", ""),
                               "Service Type": r.get("Category", ""),
                               "Current Manager": r.get("Manager", "")}) for r in rqia]
    out: list[RqiaLocation] = []
    for row in base:
        # OpenDataNI writes the RQIA ID after the name: "Willow Lodge (11001)".
        name, sid = row.get("Service Name", "").strip(), row.get("Service ID", "").strip()
        tagged = _NAME_ID.search(name)
        if tagged:
            name, sid = name[:tagged.start()].strip(), tagged.group(1)
        services = _services(row.get("Service Type", ""), name)
        if not name or not services:
            continue
        postcode = row.get("Postcode", "").strip()
        match = by_id.get(sid.lstrip("0")) if sid else None
        if match is None:
            match = by_name_pc.get(_key(name, postcode))
        if match is None and len(by_name.get(_key(name), [])) == 1:
            match = by_name[_key(name)][0]
        sid = sid or (match or {}).get("Service ID", "")
        if not sid and len(by_slug.get(_key(name), [])) == 1:
            sid = by_slug[_key(name)][0]
        if not sid:
            sid = "NI-" + hashlib.sha1(_key(name, postcode).encode()).hexdigest()[:10]
        provider = row.get("Provider", "").strip()
        street = ", ".join(x for x in (row.get("Address Line1", "").strip(),
                                       row.get("Address Line2", "").strip()) if x)
        is_home = services[0] in {"care home", "nursing home"}
        out.append(RqiaLocation(
            id=sid,
            name=name,
            phone=_phone(row.get("Phone", "")),
            address=street,
            city=row.get("Town", "").strip(),
            postcode=postcode,
            local_authority=(trusts.get("id:" + sid.lstrip("0")) or trusts.get(_key(name, postcode))
                             or trusts.get(_key(name), "")),
            services=services,
            beds=_int(row.get("Max Approved Places", "")) if is_home else 0,
            manager=registry_name(row.get("Current Manager", "")),
            provider_id=_key(provider),
            provider_name=provider,
            ownership=ownership_of(provider),
            nominated_individual=registry_name((match or {}).get("Responsible Person", "")),
            page=index.get(sid.lstrip("0"), {}).get("page", ""),
        ))
    per_provider: dict[str, int] = {}
    for loc in out:
        per_provider[loc.provider_id] = per_provider.get(loc.provider_id, 0) + 1
    for loc in out:
        loc.provider_locations = per_provider.get(loc.provider_id, 1)
    return out


# ── the local copy ───────────────────────────────────────────────────────


def _fresh(path: Path, max_age_days: float) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400


def save_cache(locations: list[RqiaLocation], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(loc) for loc in locations]), encoding="utf-8")
    tmp.replace(path)


def load_cache(path: Path) -> list[RqiaLocation]:
    return [RqiaLocation(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


async def _get(client, url: str):
    resp = await client.get(url, headers=HEADERS, follow_redirects=True)
    resp.raise_for_status()
    return resp


async def _linked_xlsx(client, page_url: str, pattern: re.Pattern, header_cell: str) -> list[dict]:
    page = await _get(client, page_url)
    match = pattern.search(page.text)
    if not match:
        raise RuntimeError(f"{page_url} no longer links the spreadsheet")
    url = _html.unescape(match.group(1))
    logger.info("rqia: downloading %s", url)
    data = (await _get(client, url)).content
    return list(xlsx_rows(data, header_cell))


async def load_register(client, data_dir: Path) -> list[RqiaLocation]:
    """The register, from the local copy when it's fresh, else downloaded.

    Raises RuntimeError with a plain reason when nothing can be downloaded
    and there is no copy at all; an old copy is used with a warning.
    """
    cache = data_dir / "register.json"
    if _fresh(cache, MAX_AGE_DAYS):
        return load_cache(cache)
    sources = {}
    for name, page, pattern, header in (
            ("opendatani", ODNI_PAGE, _ODNI_LINK, "Service Name"),
            ("rqia", REGISTER_PAGE, _REGISTER_LINK, "Service ID"),
            ("inspections", INSPECTIONS_PAGE, _INSPECTIONS_LINK, "Service")):
        try:
            sources[name] = await _linked_xlsx(client, page, pattern, header)
        except Exception as e:
            logger.warning("rqia: couldn't read the %s file: %s", name, e)
            sources[name] = []
    if not sources["opendatani"] and not sources["rqia"]:
        if cache.exists():
            logger.warning("rqia: couldn't refresh the register; using the copy from before")
            return load_cache(cache)
        raise RuntimeError("Couldn't download the RQIA register from OpenDataNI or RQIA")
    index = await report_index(client, data_dir)
    locations = build_register(sources["opendatani"], sources["rqia"],
                               sources["inspections"], index)
    if not locations and cache.exists():
        logger.warning("rqia: the downloaded register had no adult services; keeping the old copy")
        return load_cache(cache)
    save_cache(locations, cache)
    logger.info("rqia: %d registered adult care services", len(locations))
    return locations


# ── finding a service's latest report ────────────────────────────────────


def parse_report_index(sitemaps: list[str]) -> dict[str, dict]:
    """{RQIA ID without leading zeros: {"id", "page", "date", "name"}} for
    each service's latest care inspection report, from RQIA's file sitemaps.
    ``id`` is the ID as the report writes it."""
    out: dict[str, dict] = {}
    for xml in sitemaps:
        for url, raw_id, slug, day, month, year in _REPORT_SLUG.findall(xml):
            try:
                date = datetime.strptime(f"{day} {month} {year}", "%d %B %Y").date().isoformat()
            except ValueError:
                continue
            sid = raw_id.lstrip("0")
            if sid not in out or date > out[sid]["date"]:
                out[sid] = {"id": raw_id, "page": url, "date": date,
                            "name": slug.replace("-", " ")}
    return out


async def report_index(client, data_dir: Path) -> dict[str, dict]:
    """The report index, kept in data_dir/report_index.json for a week ({} when
    RQIA can't be reached and there is no copy)."""
    path = data_dir / "report_index.json"
    if _fresh(path, INDEX_MAX_AGE_DAYS):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    try:
        root = (await _get(client, SITEMAP)).text
        parts = [(await _get(client, url)).text for url in _SITEMAP_PART.findall(root)]
        index = parse_report_index(parts)
        if not index:
            raise RuntimeError("the sitemap lists no inspection reports")
    except Exception as e:
        logger.warning("rqia: couldn't read the report sitemap: %s", e)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index), encoding="utf-8")
    tmp.replace(path)
    return index


# ── what the report says ─────────────────────────────────────────────────

_AREA = re.compile(r"Area for improvement \d+", re.I)
_SHALL = re.compile(r"The registered (?:person|provider|manager)s? (?:shall|must|should)", re.I)
_AREA_END = re.compile(
    r"Ref:\s*\d+(?:\.\d+)+|Response by registered person|Action required to ensure"
    r"|\*\s*Please ensure|\d\.0 Quality Improvement Plan", re.I)
_NOISE = re.compile(
    r"\d\.0 Quality Improvement Plan/Areas for Improvement|RQIA ID:\s*\d+\s*Inspection ID:\s*IN\d+\s*\d*"
    r"|To be completed by:\s*(?:\d{1,2} \w+ \d{4}|immediate\w*[^.]*?(?=The |$))?", re.I)
_REF = re.compile(r"Ref:\s*((?:Regulation|Standard)s?\s+[\w().,&\s]+?)\s+Stated:\s*(\w+)", re.I)
_FINDING_START = ("The inspection established", "The inspection evidenced", "The inspection found",
                  "This inspection established", "The inspection identified", "Overall")
_FINDING_END = ("Full details", "Information on legislation",
                "1.0 Service information", "As a result of this inspection")


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def areas_for_improvement(text: str) -> list[str]:
    """Each area for improvement in a report's quality improvement plan, as
    "what must be done [regulation or standard, stated N times]"."""
    start = text.find("Quality Improvement Plan\n")
    if start < 0:
        start = text.rfind("Quality Improvement Plan")
    if start < 0:
        return []
    qip = text[start:]
    marks = [m.start() for m in _AREA.finditer(qip)] + [len(qip)]
    out = []
    for a, b in zip(marks, marks[1:]):
        block = _NOISE.sub(" ", qip[a:b])
        ref = _REF.search(_one_line(block))
        shall = _SHALL.search(block)
        if not shall:
            continue
        body = block[shall.start():]
        end = _AREA_END.search(body)
        body = _one_line(body[:end.start()] if end else body)
        if ref:
            tag = _one_line(ref.group(1))
            if ref.group(2).lower() not in {"first", "1st"}:
                tag += f", stated {ref.group(2).lower()} time"
            body += f" [{tag}]"
        out.append(body)
    return out


def main_findings(text: str) -> str:
    """The inspectors' overall finding from the report's summary.

    The summary opens with the same sentence in every report ("...to
    determine if the home is delivering safe, effective and compassionate
    care and if the service is well led."); what follows is the finding.
    """
    head = text[: max(len(text) // 2, 4000)]
    ends = [i for i in (head.find(m) for m in _FINDING_END) if i > 0]
    head = head[:min(ends)] if ends else head
    opener = re.search(r"well[- ]led\.", head)
    visit = re.search(r"An (?:un)?announced inspection (?:took place|was undertaken).*?\.\s+(?=[A-Z])", head, re.S)
    starts = [i for i in (head.find(m) for m in _FINDING_START) if i >= 0]
    if opener:
        start = opener.end()
    elif visit:
        start = visit.end()
    elif starts:
        start = min(starts)
    else:
        return ""
    body = head[start:start + 1500]
    return _one_line(re.sub(r"Refer to [Ss]ection [\d.]+ for (?:more|further) details\.?", "", body))


def report_summary_from(text: str, limit: int = REPORT_CHARS) -> str:
    """Areas for improvement first, then the main findings, trimmed."""
    areas = areas_for_improvement(text)
    parts = []
    if areas:
        parts.append("Areas for improvement: "
                     + " ".join(f"({i}) {a}" for i, a in enumerate(areas, 1)))
    elif re.search(r"No (?:new )?areas for improvement were identified", text, re.I):
        parts.append("No areas for improvement were identified.")
    findings = main_findings(text)
    if findings:
        parts.append("Findings: " + findings)
    summary = " ".join(parts)
    if len(summary) > limit:
        summary = summary[:limit].rsplit(". ", 1)[0] + "."
    return summary


async def latest_report(client, service_id: str, data_dir: Path) -> dict:
    """{"ratings", "summary", "url", "published", "areas"} for a service's
    latest care inspection report.

    ``ratings`` is always {} (RQIA doesn't rate). ``published`` is the
    inspection date (RQIA's report name carries no publication date), as
    "16 September 2026". ``url`` is the report PDF. {} when there's no
    report or RQIA can't be reached. Cached per service in
    data_dir/reports.json for REPORT_MAX_AGE_DAYS.
    """
    sid = str(service_id or "").strip().lstrip("0")
    if not sid.isdigit():
        return {}
    cache_path = data_dir / "reports.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(sid)
    if hit and time.time() - hit.get("at", 0) < REPORT_MAX_AGE_DAYS * 86400:
        return hit.get("report", {})
    report = {}
    try:
        entry = (await report_index(client, data_dir)).get(sid)
        if entry:
            page = await _get(client, entry["page"])
            link = _PDF_LINK.search(page.text)
            if link:
                url = _html.unescape(link.group(1))
                pdf = (await _get(client, url)).content
                if len(pdf) > MAX_PDF_BYTES:
                    raise RuntimeError(f"report is {len(pdf)} bytes")
                text = pdf_text(pdf)
                summary = report_summary_from(text)
                if summary:
                    when = datetime.strptime(entry["date"], "%Y-%m-%d")
                    report = {"ratings": {}, "summary": summary, "url": url,
                              "published": f"{when.day} {when:%B %Y}",
                              "areas": areas_for_improvement(text)}
    except Exception as e:
        logger.warning("rqia: couldn't read the report for %s: %s", service_id, e)
        return {}
    cache[sid] = {"at": time.time(), "report": report}
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as e:
        logger.warning("rqia: couldn't save the report cache: %s", e)
    return report


# ── people ───────────────────────────────────────────────────────────────


def registry_people(loc: cqc.Location) -> list[dict]:
    """The Registered Manager and Responsible Individual RQIA lists for a
    service (cqc.registry_people with RQIA's titles and source)."""
    people = cqc.registry_people(loc)
    for person in people:
        if person["title"] == "Nominated Individual":
            person["title"] = "Responsible Individual"
        person["source"] = "RQIA"
    return people


# ── reading a PDF's text ─────────────────────────────────────────────────
#
# RQIA's reports are made in Microsoft Word: Flate-compressed content streams,
# object streams, simple TrueType fonts in WinAnsi encoding and the odd
# Identity-H font with a ToUnicode map. This reads exactly that much of PDF;
# anything else (encryption, scanned pages) gives little or no text, and the
# report is then skipped.

_OBJ = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", re.S)
_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.S)
_TOKEN = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/\[\]()<>]+"
                    rb"|[-+]?\d*\.?\d+|[A-Za-z'\"*]+", re.S)
_NUMBER = re.compile(rb"[-+]?\d*\.?\d+$")
_ESC = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


def _inflate(raw: bytes) -> bytes:
    try:
        return zlib.decompressobj().decompress(raw)
    except zlib.error:
        return b""


def pdf_objects(data: bytes) -> dict[int, bytes]:
    """Each object's dictionary, followed by "\\nstream\\n" and its inflated
    stream when it has one. Objects inside object streams are unpacked."""
    out: dict[int, bytes] = {}
    for m in _OBJ.finditer(data):
        num, body = int(m.group(1)), m.group(2)
        s = _STREAM.search(body)
        if not s:
            out[num] = body
            continue
        head, raw = body[:s.start()], s.group(1)
        content = _inflate(raw) if b"FlateDecode" in head else raw
        if b"/ObjStm" in head:
            n = re.search(rb"/N\s+(\d+)", head)
            first = re.search(rb"/First\s+(\d+)", head)
            if not n or not first:
                continue
            first_at = int(first.group(1))
            nums = [int(x) for x in content[:first_at].split()]
            pairs = list(zip(nums[0::2], nums[1::2]))[: int(n.group(1))]
            for i, (onum, off) in enumerate(pairs):
                end = pairs[i + 1][1] if i + 1 < len(pairs) else len(content) - first_at
                out.setdefault(onum, content[first_at + off:first_at + end])
            continue
        out[num] = head + b"\nstream\n" + content
    return out


def _stream_of(body: bytes) -> bytes:
    i = body.find(b"\nstream\n")
    return body[i + 8:] if i >= 0 else b""


def _ref(body: bytes, key: bytes) -> int | None:
    m = re.search(rb"/" + key + rb"\s+(\d+)\s+\d+\s+R", body)
    return int(m.group(1)) if m else None


def _cmap(text: bytes) -> dict[int, str]:
    out: dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            out[int(src, 16)] = bytes.fromhex(dst.decode()).decode("utf-16-be", "ignore")
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", text, re.S):
        for a, b, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            start = int(dst, 16)
            for i, code in enumerate(range(int(a, 16), min(int(b, 16), int(a, 16) + 65535) + 1)):
                out[code] = chr(start + i) if start + i < 0x110000 else ""
    return out


def _fonts(objs: dict[int, bytes]) -> dict[int, tuple[dict, bool]]:
    """{font object: (ToUnicode map, two-byte codes?)}"""
    out = {}
    for num, body in objs.items():
        flat = body.split(b"\nstream\n", 1)[0].replace(b" ", b"")
        if b"/Type/Font" not in flat or b"/Subtype/CIDFont" in flat:
            continue
        tu = _ref(body, b"ToUnicode")
        out[num] = (_cmap(_stream_of(objs.get(tu, b""))) if tu else {}, b"Identity-H" in flat)
    return out


def _literal(tok: bytes) -> bytes:
    s, out, i = tok[1:-1], bytearray(), 0
    while i < len(s):
        c = s[i:i + 1]
        if c == b"\\" and i + 1 < len(s):
            n = s[i + 1:i + 2]
            if n in _ESC:
                out += _ESC[n]
                i += 2
            elif n.isdigit():
                octal = re.match(rb"[0-7]{1,3}", s[i + 1:])
                if octal:
                    out.append(int(octal.group(0), 8) & 255)
                    i += 1 + len(octal.group(0))
                else:
                    out += n
                    i += 2
            elif n in b"\r\n":
                i += 2
            else:
                out += n
                i += 2
        else:
            out += c
            i += 1
    return bytes(out)


def _raw(tok: bytes) -> bytes:
    if tok.startswith(b"("):
        return _literal(tok)
    if tok.startswith(b"<"):
        h = re.sub(rb"\s", b"", tok[1:-1])
        return bytes.fromhex((h + b"0" if len(h) % 2 else h).decode())
    return b""


def _decode(raw: bytes, font) -> str:
    cmap, two = font if font else ({}, False)
    if two:
        return "".join(cmap.get(int.from_bytes(raw[i:i + 2], "big"), "")
                       for i in range(0, len(raw) - 1, 2))
    return "".join(cmap.get(b) or bytes([b]).decode("cp1252", "ignore") for b in raw)


def _number(tok: bytes) -> float | None:
    try:
        return float(tok)
    except ValueError:
        return None


def content_text(stream: bytes, fonts: dict) -> str:
    """The text a page's content stream draws, a newline where the line moves."""
    out: list[str] = []
    stack: list[bytes] = []
    font, last_y = None, None
    for tok in _TOKEN.findall(stream):
        if tok[:1] in (b"(", b"<", b"/", b"[", b"]") or _NUMBER.match(tok):
            stack.append(tok)
            continue
        if tok == b"Tf" and len(stack) >= 2:
            font = fonts.get(stack[-2][1:])
        elif tok == b"Tj" and stack:
            out.append(_decode(_raw(stack[-1]), font))
        elif tok in (b"'", b'"') and stack:
            out.append("\n" + _decode(_raw(stack[-1]), font))
        elif tok == b"TJ":
            i = len(stack) - 1
            while i >= 0 and stack[i] != b"[":
                i -= 1
            for t in stack[i + 1:]:
                if t[:1] in (b"(", b"<"):
                    out.append(_decode(_raw(t), font))
                elif (n := _number(t)) is not None and n < -250:
                    out.append(" ")
        elif tok in (b"Td", b"TD") and stack:
            dy = _number(stack[-1])
            if dy is None or abs(dy) > 0.5:
                out.append("\n")
        elif tok == b"Tm" and stack:
            y = _number(stack[-1])
            if y is None or last_y is None or abs(y - last_y) > 0.5:
                out.append("\n")
            last_y = y
        elif tok == b"T*":
            out.append("\n")
        stack = []
    return "".join(out)


def _page_order(objs: dict[int, bytes]) -> list[int]:
    root = next((b for b in objs.values()
                 if b"/Type/Catalog" in b.split(b"\nstream\n", 1)[0].replace(b" ", b"")), b"")
    pages: list[int] = []

    def walk(num: int | None, depth: int = 0):
        body = objs.get(num, b"") if num is not None else b""
        if depth > 20 or not body:
            return
        kids = re.search(rb"/Kids\s*\[([^\]]*)\]", body)
        if kids:
            for ref in re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1)):
                walk(int(ref), depth + 1)
        elif b"/Contents" in body:
            pages.append(num)

    walk(_ref(root, b"Pages"))
    if pages:
        return pages
    return sorted(n for n, b in objs.items()
                  if re.search(rb"/Type\s*/Page\b", b.split(b"\nstream\n", 1)[0]))


def _page_fonts(page: bytes, objs: dict, fonts: dict) -> dict[bytes, tuple]:
    res = page
    ref = _ref(page, b"Resources")
    if ref:
        res = objs.get(ref, b"")
    inline = re.search(rb"/Font\s*<<(.*?)>>", res, re.S)
    if inline:
        table = inline.group(1)
    else:
        font_ref = _ref(res, b"Font")
        table = objs.get(font_ref, b"") if font_ref else b""
    return {name: fonts.get(int(num))
            for name, num in re.findall(rb"/([^\s/<>\[\]]+)\s+(\d+)\s+\d+\s+R", table)}


def pdf_text(data: bytes) -> str:
    """The text of a Word-made PDF, page by page, one line per drawn line."""
    if b"/Encrypt" in data[-4096:] or not data.startswith(b"%PDF"):
        return ""
    objs = pdf_objects(data)
    fonts = _fonts(objs)
    pages = []
    for num in _page_order(objs):
        page = objs.get(num, b"")
        contents = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", page)
        refs = [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", contents.group(1))] if contents else []
        page_fonts = _page_fonts(page, objs, fonts)
        pages.append("".join(content_text(_stream_of(objs.get(r, b"")), page_fonts) for r in refs))
    text = "\n".join(pages)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r" *\n[\s]*", "\n", text).strip()
