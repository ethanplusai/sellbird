"""CQC — England's register of every adult social care service, free.

The Care Quality Commission publishes a monthly "HSCA Active Locations"
spreadsheet under the Open Government Licence: every registered care home,
home care agency and supported living service in England, with its phone,
website, latitude and longitude, latest rating, number of beds, the name of
its Registered Manager, and its provider's Companies House number, ownership
type and Nominated Individual. No key, no account, no per-record cost.

The file is about 24 MB of OpenDocument XML and takes half a minute to
read, so the adult social care rows are kept as a small JSON file next to
it in ``data/cqc/``. Both are refreshed when they are older than
``MAX_AGE_DAYS``; CQC publishes once a month.

Read with the standard library only: an .ods file is a zip holding
``content.xml``, and streaming it with iterparse keeps memory flat.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

DATA_PAGE = "https://www.cqc.org.uk/about-us/transparency/using-cqc-data"
SHEET = "HSCA_Active_Locations"
MAX_AGE_DAYS = 35
LOCATION_URL = "https://www.cqc.org.uk/location/{id}"

_T = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
# A blank run at the end of a row is written as one cell repeated thousands
# of times; nothing real repeats more than this.
_MAX_REPEAT = 300
_FILE_LINK = re.compile(
    r'href="(https://www\.cqc\.org\.uk/[^"]*HSCA_Active_Locations\.ods)"', re.I)

# Service type columns -> the short names Harvey stores and searches.
SERVICE_TYPES = {
    "Service type - Care home service with nursing": "nursing home",
    "Service type - Care home service without nursing": "care home",
    "Service type - Domiciliary care service": "home care",
    "Service type - Supported living service": "supported living",
    "Service type - Extra Care housing services": "extra care housing",
    "Service type - Shared Lives": "shared lives",
}

# A search phrase -> the services that answer it. First match wins, so the
# specific phrases come first ("nursing home" before "home").
TERM_SERVICES = (
    # Housing-related support (other registers use these service names).
    ("floating support", {"floating support", "housing support"}),
    ("tenancy", {"floating support", "housing support"}),
    ("housing related support", {"housing support", "floating support", "supported housing"}),
    ("housing support", {"housing support", "floating support", "supported housing"}),
    ("supported housing", {"supported housing", "housing support", "supported living"}),
    ("supported accommodation", {"supported housing", "housing support", "supported living"}),
    ("exempt accommodation", {"supported housing"}),
    ("nursing", {"nursing home"}),
    ("residential", {"care home", "nursing home"}),
    ("care home", {"care home", "nursing home"}),
    ("domiciliary", {"home care"}),
    ("home care", {"home care"}),
    ("homecare", {"home care"}),
    ("supported living", {"supported living"}),
    ("extra care", {"extra care housing"}),
    ("shared lives", {"shared lives"}),
)

# PECR: a sole trader or a partnership is an individual subscriber, so cold
# email needs their consent first. CQC says which providers those are.
CONSENT_NEEDED = frozenset({"Individual", "Partnership"})


@dataclass
class Location:
    """One registered adult social care service (a CQC "location")."""

    id: str
    name: str
    phone: str = ""
    website: str = ""
    address: str = ""
    city: str = ""
    postcode: str = ""
    local_authority: str = ""
    lat: float | None = None
    lng: float | None = None
    services: list[str] = field(default_factory=list)
    beds: int = 0
    rating: str = ""
    manager: str = ""                # "Surname, Forename" as CQC writes it
    provider_id: str = ""
    provider_name: str = ""
    provider_website: str = ""
    provider_phone: str = ""
    company_number: str = ""
    ownership: str = ""              # Organisation | Individual | Partnership | NHS Body
    nominated_individual: str = ""
    provider_locations: int = 0      # how many services the provider runs

    @property
    def url(self) -> str:
        return LOCATION_URL.format(id=self.id)


# ── reading the spreadsheet ──────────────────────────────────────────────


def ods_rows(path: Path, sheet: str = SHEET):
    """Yield each row of one sheet as a dict keyed by its header row."""
    header: list[str] | None = None
    in_sheet = False
    with zipfile.ZipFile(path) as z, zipfile.Path(z, "content.xml").open(mode="rb") as f:
        for event, el in ET.iterparse(f, events=("start", "end")):
            if event == "start":
                if el.tag == _T + "table":
                    in_sheet = el.get(_T + "name") == sheet
                continue
            if el.tag == _T + "table-row":
                if in_sheet:
                    cells: list[str] = []
                    for cell in el.findall(_T + "table-cell"):
                        repeat = int(cell.get(_T + "number-columns-repeated", "1"))
                        cells.extend(["".join(cell.itertext()).strip()]
                                     * min(repeat, _MAX_REPEAT))
                    if header is None:
                        header = cells
                    elif any(cells):
                        yield dict(zip(header, cells))
                el.clear()
            elif el.tag == _T + "table":
                if in_sheet:
                    return
                el.clear()


def _blank(value: str) -> str:
    """CQC writes "*" or "-" for "none"."""
    value = (value or "").strip()
    return "" if value in {"*", "-"} else value


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _website(value: str) -> str:
    value = _blank(value)
    if value and not value.lower().startswith(("http://", "https://")):
        value = "https://" + value
    return value


def location_from_row(row: dict) -> Location | None:
    """The adult social care service in one spreadsheet row, or None."""
    if row.get("Location Inspection Directorate") != "Adult social care":
        return None
    if row.get("Dormant (Y/N)") == "Y" or not row.get("Location ID"):
        return None
    try:
        beds = int(float(row.get("Care homes beds") or 0))
    except ValueError:
        beds = 0
    street = ", ".join(x for x in (_blank(row.get("Location Street Address", "")),
                                   _blank(row.get("Location Address Line 2", ""))) if x)
    return Location(
        id=row["Location ID"],
        name=_blank(row.get("Location Name", "")),
        phone=_blank(row.get("Location Telephone Number", "")),
        website=_website(row.get("Location Web Address", "")),
        address=street,
        city=_blank(row.get("Location City", "")),
        postcode=_blank(row.get("Location Postal Code", "")),
        local_authority=_blank(row.get("Location Local Authority", "")),
        lat=_float(row.get("Location Latitude", "")),
        lng=_float(row.get("Location Longitude", "")),
        services=[short for col, short in SERVICE_TYPES.items() if row.get(col) == "Y"],
        beds=beds,
        rating=_blank(row.get("Location Latest Overall Rating", "")),
        manager=_blank(row.get("Registered manager", "")),
        provider_id=_blank(row.get("Provider ID", "")),
        provider_name=_blank(row.get("Provider Name", "")),
        provider_website=_website(row.get("Provider Web Address", "")),
        provider_phone=_blank(row.get("Provider Telephone Number", "")),
        company_number=_blank(row.get("Provider Companies House Number", "")),
        ownership=_blank(row.get("Provider Ownership Type", "")),
        nominated_individual=_blank(row.get("Provider Nominated Individual Name", "")),
    )


def read_locations(path: Path) -> list[Location]:
    """Every active adult social care service in the spreadsheet."""
    out = [loc for row in ods_rows(path) if (loc := location_from_row(row))]
    per_provider: dict[str, int] = {}
    for loc in out:
        per_provider[loc.provider_id] = per_provider.get(loc.provider_id, 0) + 1
    for loc in out:
        loc.provider_locations = per_provider.get(loc.provider_id, 1)
    return out


# ── the local copy ───────────────────────────────────────────────────────


def find_file_url(page_html: str) -> str:
    """The current month's spreadsheet link on CQC's data page ("" if gone)."""
    match = _FILE_LINK.search(page_html or "")
    return match.group(1) if match else ""


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
    """The register, from the local copy when it's fresh, else downloaded.

    Raises RuntimeError with a plain reason when CQC can't be reached and
    there is no copy at all; an old copy is better than none and is used
    with a warning.
    """
    cache = data_dir / "adult_social_care.json"
    if _fresh(cache):
        return load_cache(cache)
    ods = data_dir / "active_locations.ods"
    try:
        page = await client.get(DATA_PAGE)
        page.raise_for_status()
        url = find_file_url(page.text)
        if not url:
            raise RuntimeError("CQC's data page no longer links the active locations file")
        logger.info("cqc: downloading %s", url)
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = ods.with_suffix(".part")
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with tmp.open(mode="wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
        tmp.replace(ods)
    except Exception as e:
        if cache.exists():
            logger.warning("cqc: couldn't refresh the register (%s); using the copy "
                           "from before", e)
            return load_cache(cache)
        raise RuntimeError(f"Couldn't download the CQC register: {e}") from e
    locations = read_locations(ods)
    save_cache(locations, cache)
    logger.info("cqc: %d active adult social care services", len(locations))
    return locations


# ── searching it ─────────────────────────────────────────────────────────


def services_for(term: str) -> set[str]:
    """The services a search phrase means, or an empty set when it means none."""
    low = (term or "").lower()
    for needle, services in TERM_SERVICES:
        if needle in low:
            return services
    return set()


def km_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _place_matches(loc: Location, place: str) -> bool:
    city = (place or "").split(",")[0].strip().lower()
    if not city:
        return True
    return city in loc.city.lower() or city in loc.local_authority.lower()


def search(locations: list[Location], term: str, place: str = "",
           coordinate: str = "") -> list[Location]:
    """The services matching a phrase near a place, nearest first.

    ``coordinate`` is "lat,lng,radius_km" and wins over the place name; with
    neither, the whole of England matches.
    """
    wanted = services_for(term)
    if not wanted:
        return []
    lat = lng = radius = None
    if coordinate:
        parts = (coordinate.split(",") + ["", "", ""])[:3]
        lat, lng, radius = _float(parts[0]), _float(parts[1]), _float(parts[2]) or 25.0
    hits: list[tuple[float, Location]] = []
    for loc in locations:
        if not wanted.intersection(loc.services):
            continue
        if lat is not None and lng is not None:
            if loc.lat is None or loc.lng is None:
                continue
            d = km_between(lat, lng, loc.lat, loc.lng)
            if d > radius:
                continue
            hits.append((d, loc))
        elif _place_matches(loc, place):
            hits.append((0.0, loc))
    hits.sort(key=lambda h: h[0])
    return [loc for _, loc in hits]


# ── people ───────────────────────────────────────────────────────────────


def split_registry_name(value: str) -> tuple[str, str] | None:
    """("Forename", "Surname") from CQC's "Surname, Forename Middle".

    Companies House writes names the same way, with the surname in capitals.
    Returns None when there's no usable first and last name.
    """
    value = _blank(value)
    if "," not in value:
        return None
    last, _, rest = value.partition(",")
    first = rest.strip().split()[0] if rest.strip() else ""
    last = last.strip()
    if not first or not last:
        return None
    if last.isupper():
        last = " ".join(w.capitalize() for w in last.split())
        last = re.sub(r"(?<=[-'’])(\w)", lambda m: m.group(1).upper(), last)
    if first.isupper():
        first = first.capitalize()
    return first, last


def people_from_observations(rows: list[dict]) -> list[dict]:
    """Registry people back out of REGISTRY_VERIFIED observations.

    Discovery writes "First Last | Title | Register". Only the newest run's
    rows count: a manager who left is in older runs and not in the newest.
    """
    if not rows:
        return []
    newest = max(rows, key=lambda r: str(r.get("observed_at") or ""))
    run = newest.get("run_id")
    out, seen = [], set()
    for row in rows:
        if run and row.get("run_id") != run:
            continue
        parts = [p.strip() for p in (row.get("value_text") or "").split("|")]
        if len(parts) < 2 or " " not in parts[0]:
            continue
        first, _, last = parts[0].partition(" ")
        key = parts[0].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"first_name": first, "last_name": last.strip(), "title": parts[1],
                    "source": parts[2] if len(parts) > 2 else "",
                    "evidence_url": row.get("evidence_url") or ""})
    return out


def registry_people(loc: Location) -> list[dict]:
    """The named people CQC holds for a service: its Registered Manager and
    the provider's Nominated Individual (the same person is listed once)."""
    out: list[dict] = []
    seen: set[str] = set()
    for raw, title in ((loc.manager, "Registered Manager"),
                       (loc.nominated_individual, "Nominated Individual")):
        # A service with two managers lists them separated by ";".
        for one in _blank(raw).split(";"):
            name = split_registry_name(one)
            if not name:
                continue
            key = " ".join(name).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"first_name": name[0], "last_name": name[1], "title": title,
                        "source": "CQC", "evidence_url": loc.url})
    return out


# ── the latest inspection report ─────────────────────────────────────────
#
# What a provider most needs is in CQC's own words: the summary of its latest
# report says what inspectors found (staffing, medicines, falls, dementia
# care...). The writer reads it to pick training that answers it. One location
# page and one report page per company, kept for REPORT_MAX_AGE_DAYS.

REPORT_MAX_AGE_DAYS = 30
REPORT_CHARS = 2500
KEY_QUESTIONS = ("Safe", "Effective", "Caring", "Responsive", "Well-led")
_RATING_WORDS = r"(Outstanding|Good|Requires improvement|Inadequate|Not rated)"
_REPORT_LINK = re.compile(r'href="(/location/[^"/]+/reports/[^"/]+/overall)"')
_START_MARKERS = ("Our view of the service", "Summary of findings",
                  "Overall summary", "Date of assessment")
_END_MARKERS = ("People's experience of this service", "In instances where CQC",
                "Download full inspection report", "Rating at last inspection",
                "Full report")
_PUBLISHED = re.compile(r"(?:Published|Report published:?|Updated)\s+(\d{1,2} \w+ \d{4})")


def page_text(page_html: str) -> str:
    """The visible text of a CQC page's <main>, on one line."""
    import html as _html
    match = re.search(r"<main.*?</main>", page_html or "", re.S)
    text = match.group(0) if match else (page_html or "")
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S)
    text = _html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def key_ratings(text: str) -> dict[str, str]:
    """{"Overall": ..., "Safe": ..., ...} from a page's ratings block."""
    out = {}
    for name in ("Overall",) + KEY_QUESTIONS:
        match = re.search(rf"\b{re.escape(name)}\s*:?\s*{_RATING_WORDS}", text)
        if match:
            out[name] = match.group(1)
    return out


def report_summary_from(text: str, limit: int = REPORT_CHARS) -> str:
    """The inspectors' summary out of a report page's text, trimmed."""
    # Old-style summary pages open with "Background to this inspection"; the
    # findings come after it, under "Overall inspection".
    starts = [i for i in (text.find(m) for m in _START_MARKERS) if i >= 0]
    old = re.search(rf"Overall inspection {_RATING_WORDS}", text)
    if old:
        starts.append(old.start())
    start = min(starts, default=0)
    body = text[start:]
    ends = [i for i in (body.find(m) for m in _END_MARKERS) if i > 0]
    if ends:
        body = body[:min(ends)]
    if len(body) > limit:
        body = body[:limit].rsplit(". ", 1)[0] + "."
    return body.strip()


async def latest_report(client, location_id: str, data_dir: Path) -> dict:
    """{"ratings", "summary", "url"} for a location's latest CQC report.

    {} when CQC can't be reached or the page has no report. Cached per
    location in data_dir/reports.json for REPORT_MAX_AGE_DAYS.
    """
    cache_path = data_dir / "reports.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(location_id)
    if hit and time.time() - hit.get("at", 0) < REPORT_MAX_AGE_DAYS * 86400:
        return hit.get("report", {})
    report = {}
    try:
        page = await client.get(LOCATION_URL.format(id=location_id))
        page.raise_for_status()
        location_text = page_text(page.text)
        ratings = key_ratings(location_text)
        link = _REPORT_LINK.search(page.text)
        summary, url, published = "", "", ""
        if link:
            url = "https://www.cqc.org.uk" + link.group(1)
        elif "/inspection-summary" in page.text:
            # Reports from before June 2024 have a summary page instead.
            url = LOCATION_URL.format(id=location_id) + "/inspection-summary"
        if url:
            resp = await client.get(url)
            resp.raise_for_status()
            text = page_text(resp.text)
            if link:
                ratings = key_ratings(text) or ratings
            summary = report_summary_from(text)
            date = _PUBLISHED.search(location_text) or _PUBLISHED.search(text)
            published = date.group(1) if date else ""
        if ratings or summary:
            report = {"ratings": ratings, "summary": summary, "url": url,
                      "published": published}
    except Exception as e:
        logger.warning("cqc: couldn't read the report for %s: %s", location_id, e)
        return {}
    cache[location_id] = {"at": time.time(), "report": report}
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as e:
        logger.warning("cqc: couldn't save the report cache: %s", e)
    return report
