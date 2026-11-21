"""CIW — Wales's register of adult care services, free.

Care Inspectorate Wales publishes its whole service directory as one CSV
("Access our data" on the directory, https://digital.careinspectorate.wales/
directory/data-extract), refreshed continuously. CIW's site is Crown
copyright, re-usable under the Open Government Licence. Every registered
service is in it: care homes, domiciliary support, childcare and the rest,
with phone, email, website, the latest ratings by theme, the Responsible
Individual's name and the provider's type and Companies House number.

The file has no coordinates (only UPRNs), so ``lat``/``lng`` are None and a
radius search finds nothing; search Welsh services by place name.

The adult services are kept as a small JSON file in ``data_dir`` and the CSV
is downloaded again when that is older than ``MAX_AGE_DAYS``.

Inspection reports are PDFs only. Their text is read with pypdf when it is
installed, else with the small reader at the bottom of this file, which
handles the plain text PDFs CIW publishes (Flate streams, ToUnicode maps).
The ratings themselves come from the register, so a report that can't be
read still gives the ratings.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
import zlib
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from sellbird.collectors.cqc import CONSENT_NEEDED, Location, search, services_for  # noqa: F401

logger = logging.getLogger(__name__)

DATA_PAGE = "https://digital.careinspectorate.wales/directory/data-extract"
EXPORT_URL = "https://digital.careinspectorate.wales/api/DataExport"
LOCATION_URL = "https://digital.careinspectorate.wales/directory/service/{id}"
REPORT_URL = ("https://digital.careinspectorate.wales/backend/directory/service/"
              "{id}/LatestInspectionReport")
MAX_AGE_DAYS = 7
REGISTER_FILE = "adult_social_care.json"
DETAILS_FILE = "details.json"
REPORTS_FILE = "reports.json"

# CIW's four themes, in the order its reports use. Domiciliary support has
# no Environment rating.
THEMES = ("Well-being", "Care and Support", "Environment", "Leadership and Management")
RATING_ORDER = ("Requires Significant Improvement", "Requires Improvement",
                "Good", "Excellent")



class CiwLocation(Location):
    """A cqc.Location whose ``url`` is its CIW directory page. Same fields,
    so discovery treats it like any CQC location."""

    @property
    def url(self) -> str:
        return LOCATION_URL.format(id=self.id)


# CIW provider types -> the ownership values CQC uses (cqc.CONSENT_NEEDED).
_ORG_TYPES = {"Local Health Board": "NHS Body", "Partnership": "Partnership"}


# ── reading the extract ──────────────────────────────────────────────────


def _clean(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value.lower() in {"none", "n/a", "not applicable", "-"} else value


def _website(value: str) -> str:
    value = _clean(value)
    if value and not value.lower().startswith(("http://", "https://")):
        value = "https://" + value
    return value


def _int(value: str) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def services_from(row: dict) -> list[str]:
    """The short service names Sellbird searches by, [] for anything that isn't
    an adult care service."""
    kind = _clean(row.get("Service type"))
    sub = _clean(row.get("Service sub-type")).lower()
    care_for = _clean(row.get("Care and support for")).lower()
    if kind == "Care Home Service":
        if sub == "adults with nursing":
            return ["nursing home"]
        if sub == "adults without nursing":
            return ["care home"]
        return []
    if kind == "Domiciliary Support Service":
        # Children only: not an adult service.
        if care_for and "child" in care_for and not re.search(
                r"adult|people|supported living|older", care_for):
            return []
        out = ["home care"]
        if "supported living" in care_for:
            out.append("supported living")
        return out
    if kind == "Adult Placement Service":
        return ["shared lives"]
    return []


def parse_ratings(value: str) -> dict[str, str]:
    """{"Well-being": "Good", ...} from "Well-being: Good; Care and Support: ...".

    A few services list their ratings twice; the first of each theme counts.
    """
    out: dict[str, str] = {}
    for part in (value or "").split(";"):
        theme, sep, rating = part.partition(":")
        theme, rating = theme.strip(), rating.strip()
        if sep and theme and rating and theme not in out:
            out[theme] = rating
    return {t: out[t] for t in THEMES if t in out} | {
        t: r for t, r in out.items() if t not in THEMES}


def rating_summary(ratings: dict[str, str]) -> str:
    """One short line: "Good" when every theme agrees, else the lowest rating
    and where, e.g. "Requires Improvement (Environment); others Good"."""
    if not ratings:
        return ""
    values = set(ratings.values())
    if len(values) == 1:
        return values.pop()
    rank = {r: i for i, r in enumerate(RATING_ORDER)}
    worst = min(values, key=lambda r: rank.get(r, 99))
    themes = [t for t, r in ratings.items() if r == worst]
    rest = sorted(values - {worst}, key=lambda r: rank.get(r, 99))
    return f"{worst} ({', '.join(themes)}); others {'/'.join(rest)}"


def ownership_from(row: dict) -> str:
    """CQC's ownership words for a CIW provider type.

    The extract leaves the type blank for a provider that is a person (a sole
    trader); with no company or charity number that is an Individual.
    """
    kind = _clean(row.get("Provider type"))
    if kind in _ORG_TYPES:
        return _ORG_TYPES[kind]
    if not kind:
        if _clean(row.get("Company number")) or _clean(row.get("Charity number")):
            return "Organisation"
        return "Individual" if _clean(row.get("Provider URN")) else ""
    return "Organisation"


def registry_name(name: str) -> str:
    """CIW's "Forename Surname" as CQC's "Surname, Forename" ("" if unusable)."""
    parts = _clean(name).split()
    if len(parts) < 2:
        return ""
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def location_from_row(row: dict) -> CiwLocation | None:
    """The adult care service in one extract row, or None."""
    services = services_from(row)
    if not services or not _clean(row.get("Service URN")):
        return None
    street = ", ".join(x for x in (_clean(row.get("Service address line 1")),
                                   _clean(row.get("Service address line 2")),
                                   _clean(row.get("Service address line 3"))) if x)
    people = [registry_name(n) for n in _clean(row.get("Responsible Individual names")).split(",")]
    return CiwLocation(
        id=row["Service URN"].strip(),
        name=_clean(row.get("Service name")),
        phone=_clean(row.get("Primary telephone number")),
        website=_website(row.get("Website", "")),
        address=street,
        city=_clean(row.get("Service town/city")),
        postcode=_clean(row.get("Service postcode")),
        # Domiciliary services have no local authority, only a region.
        local_authority=_clean(row.get("Local authority")) or _clean(row.get("Domiciliary care area")),
        lat=None,
        lng=None,
        services=services,
        beds=_int(row.get("Maximum no. of places", "")) if "care home" in services
        or "nursing home" in services else 0,
        rating=rating_summary(parse_ratings(row.get("Service ratings", ""))),
        manager="",  # the extract names no registered manager
        provider_id=_clean(row.get("Provider URN")),
        provider_name=_clean(row.get("Provider name")),
        provider_website=_website(row.get("Provider website", "")),
        provider_phone="",
        company_number=_clean(row.get("Company number")),
        ownership=ownership_from(row),
        nominated_individual=";".join(p for p in people if p),
        provider_locations=0,
    )


def read_extract(text: str) -> tuple[list[CiwLocation], dict[str, dict]]:
    """Every adult care service in the CSV text, and per service the details a
    Location has no field for (ratings by theme, email, who it cares for)."""
    locations: list[Location] = []
    details: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        loc = location_from_row(row)
        if not loc:
            continue
        locations.append(loc)
        details[loc.id] = {
            "ratings": parse_ratings(row.get("Service ratings", "")),
            "email": _clean(row.get("Primary email address")),
            "care_for": _clean(row.get("Care and support for")),
            "provider_type": _clean(row.get("Provider type")),
            "updated": _clean(row.get("Last updated on")),
        }
    per_provider: dict[str, int] = {}
    for loc in locations:
        per_provider[loc.provider_id] = per_provider.get(loc.provider_id, 0) + 1
    for loc in locations:
        loc.provider_locations = per_provider.get(loc.provider_id, 1)
    return locations, details


def registry_people(loc: Location) -> list[dict]:
    """The provider's Responsible Individual(s) for a service, as CIW names them."""
    from sellbird.collectors.cqc import split_registry_name
    out, seen = [], set()
    for one in (loc.nominated_individual or "").split(";"):
        name = split_registry_name(one)
        if not name or " ".join(name).lower() in seen:
            continue
        seen.add(" ".join(name).lower())
        out.append({"first_name": name[0], "last_name": name[1],
                    "title": "Responsible Individual", "source": "CIW",
                    "evidence_url": LOCATION_URL.format(id=loc.id)})
    return out


def location_url(service_id: str) -> str:
    return LOCATION_URL.format(id=service_id)


# ── the local copy ───────────────────────────────────────────────────────


def _fresh(path: Path, max_age_days: int = MAX_AGE_DAYS) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    tmp.replace(path)


def save_cache(locations: list[Location], details: dict, data_dir: Path) -> None:
    _write_json(data_dir / DETAILS_FILE, details)
    _write_json(data_dir / REGISTER_FILE, [asdict(loc) for loc in locations])


def load_cache(data_dir: Path) -> list[Location]:
    items = json.loads((data_dir / REGISTER_FILE).read_text(encoding="utf-8"))
    return [CiwLocation(**item) for item in items]


def load_details(data_dir: Path) -> dict[str, dict]:
    """{service id: {"ratings", "email", "care_for", ...}} from the local copy."""
    try:
        return json.loads((data_dir / DETAILS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def load_register(client, data_dir: Path) -> list[CiwLocation]:
    """The adult services, from the local copy when it's fresh, else downloaded.

    Raises RuntimeError with a plain reason when CIW can't be reached and
    there is no copy at all; an old copy is used with a warning.
    """
    cache = data_dir / REGISTER_FILE
    if _fresh(cache):
        return load_cache(data_dir)
    path = data_dir / "ciw_services.csv"
    try:
        logger.info("ciw: downloading %s", EXPORT_URL)
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        async with client.stream("GET", EXPORT_URL) as resp:
            resp.raise_for_status()
            with tmp.open(mode="wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
        text = tmp.read_bytes().decode("utf-8-sig", errors="replace")
        if "Service URN" not in text[:2000]:
            raise RuntimeError("CIW's data extract no longer has the columns Sellbird reads")
        tmp.replace(path)
    except Exception as e:
        if cache.exists():
            logger.warning("ciw: couldn't refresh the register (%s); using the copy "
                           "from before", e)
            return load_cache(data_dir)
        raise RuntimeError(f"Couldn't download the CIW register: {e}") from e
    locations, details = read_extract(text)
    save_cache(locations, details, data_dir)
    logger.info("ciw: %d adult care services", len(locations))
    return locations


# ── the latest inspection report ─────────────────────────────────────────

REPORT_MAX_AGE_DAYS = 30
REPORT_CHARS = 2500
_SECTION_END = ("Well-being", "Care and Support", "Environment",
                "Leadership and Management", "Summary of inspection",
                "Areas for improvement", "Date Published")
_PUBLISHED = re.compile(r"Date Published:?\s+(\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{1,2} \w+ \d{4})", re.I)
_IMPROVE = re.compile(r"Area\(?s\)? for Improvement|Areas for improvement and action", re.I)


def _date(value: str) -> str:
    """"12/03/2026" -> "12 March 2026" (as written when it isn't numeric)."""
    for fmt in ("%d/%m/%Y", "%d.%m.%Y"):
        try:
            day = datetime.strptime(value, fmt)
            return f"{day.day} {day:%B %Y}"
        except ValueError:
            continue
    return value


def _trim(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) > limit:
        text = text[:limit].rsplit(". ", 1)[0] + "."
    return text


def report_summary_from(text: str, limit: int = REPORT_CHARS) -> str:
    """The "Summary" section of a CIW report's text, plus its areas for
    improvement when there's room, trimmed."""
    text = re.sub(r"\s+", " ", text or "").strip()
    about = text.find("About ")
    match = re.compile(r"\bSummary\b(?! of)").search(text, max(about, 0))
    summary = ""
    if match:
        body = text[match.end():]
        ends = [m.start() for m in (re.search(rf"\b{re.escape(h)}\b", body)
                                    for h in _SECTION_END) if m and m.start() > 0]
        summary = body[:min(ends)] if ends else body
        summary = _trim(summary, limit)
    improve = _IMPROVE.search(text)
    if improve and len(summary) < limit - 200:
        tail = text[improve.end():]
        stop = re.search(r"Date Published|Priority Action Notice|Inspection Report on", tail)
        tail = tail[:stop.start()] if stop else tail
        tail = _trim(tail, limit - len(summary) - 30)
        if tail and len(tail) > 20:
            summary = (summary + " " if summary else "") + "Areas for improvement: " + tail
    return summary.strip()


def _read_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


async def latest_report(client, service_id: str, data_dir: Path) -> dict:
    """{"ratings", "summary", "url", "published"} for a service's latest report.

    ``ratings`` (by theme) come from the register; ``summary`` from the
    report PDF. {} when CIW knows no ratings and the report can't be read.
    Cached per service in data_dir/reports.json for REPORT_MAX_AGE_DAYS; a
    report that couldn't be fetched is not cached, so it's tried again.
    """
    cache_path = data_dir / REPORTS_FILE
    cache = _read_cache(cache_path)
    hit = cache.get(service_id)
    if hit and time.time() - hit.get("at", 0) < REPORT_MAX_AGE_DAYS * 86400:
        return hit.get("report", {})
    ratings = (load_details(data_dir).get(service_id) or {}).get("ratings") or {}
    url = REPORT_URL.format(id=service_id)
    summary = published = ""
    fetched = False
    try:
        resp = await client.get(url)
        if resp.status_code == 404:
            url, fetched = "", True          # never inspected under this registration
        else:
            resp.raise_for_status()
            if not resp.content.startswith(b"%PDF"):
                raise RuntimeError("the report link didn't return a PDF")
            text = pdf_text(resp.content)
            summary = report_summary_from(text)
            date = _PUBLISHED.search(re.sub(r"\s+", " ", text))
            published = _date(date.group(1)) if date else ""
            fetched = True
    except Exception as e:
        logger.warning("ciw: couldn't read the report for %s: %s", service_id, e)
    if not fetched:
        return {"ratings": ratings, "summary": "", "url": url, "published": ""} if ratings else {}
    report = ({"ratings": ratings, "summary": summary, "url": url, "published": published}
              if ratings or summary else {})
    cache[service_id] = {"at": time.time(), "report": report}
    try:
        _write_json(cache_path, cache)
    except OSError as e:
        logger.warning("ciw: couldn't save the report cache: %s", e)
    return report


# ── PDF text ─────────────────────────────────────────────────────────────


def pdf_text(data: bytes) -> str:
    """The text of a PDF: pypdf when installed, else the reader below."""
    try:
        from pypdf import PdfReader  # optional
    except ImportError:
        return SimplePdf(data).text()
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return SimplePdf(data).text()


_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
_REF = re.compile(rb"(\d+)\s+\d+\s+R")
_TOKEN = re.compile(rb"""
    (?P<str>\()                     # literal string, scanned by hand
  | <(?P<hex>[0-9A-Fa-f\s]*)>       # hex string
  | (?P<arr_open>\[) | (?P<arr_close>\])
  | (?P<dict><<|>>)
  | /(?P<name>[^\s/<>\[\]()%{}]+)
  | (?P<num>[-+]?(?:\d+\.?\d*|\.\d+))
  | (?P<op>[A-Za-z'"*][A-Za-z0-9'"*]*)
  | %[^\r\n]*
""", re.X)
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


class SimplePdf:
    """Just enough of PDF to get the words out of a text report: objects
    (including object streams), Flate streams, the page tree, fonts'
    ToUnicode maps and the text operators. Not a general PDF reader."""

    def __init__(self, data: bytes):
        self.objects: dict[int, tuple[bytes, bytes | None]] = {}
        for m in _OBJ.finditer(data):
            self.objects[int(m.group(1))] = self._split(m.group(3))
        for num, (head, stream) in list(self.objects.items()):
            if stream is not None and re.search(rb"/Type\s*/ObjStm", head):
                self._unpack_objstm(head, stream)
        self._cmaps: dict[int, tuple[dict[int, str], int]] = {}

    # objects

    @staticmethod
    def _split(body: bytes) -> tuple[bytes, bytes | None]:
        m = re.search(rb"\bstream\r?\n", body)
        if not m:
            return body.strip(), None
        head = body[:m.start()]
        raw = body[m.end():body.rfind(b"endstream")]
        if re.search(rb"/FlateDecode", head):
            try:
                raw = zlib.decompressobj().decompress(raw)
            except zlib.error:
                raw = b""
        elif re.search(rb"/Filter", head):
            raw = b""                      # images and the like
        return head, raw

    def _unpack_objstm(self, head: bytes, stream: bytes) -> None:
        n = re.search(rb"/N\s+(\d+)", head)
        first = re.search(rb"/First\s+(\d+)", head)
        if not n or not first:
            return
        nums = [int(x) for x in stream[:int(first.group(1))].split()]
        pairs = list(zip(nums[0::2], nums[1::2]))[:int(n.group(1))]
        base = int(first.group(1))
        for i, (num, off) in enumerate(pairs):
            end = base + pairs[i + 1][1] if i + 1 < len(pairs) else len(stream)
            self.objects.setdefault(num, (stream[base + off:end].strip(), None))

    def _value(self, src: bytes, key: bytes) -> bytes:
        """The value of /key in a dictionary: a nested dict, an array or a ref
        (resolved to the referenced object's dictionary)."""
        m = re.search(rb"/" + key + rb"(?![A-Za-z0-9])\s*", src)
        if not m:
            return b""
        rest = src[m.end():]
        ref = re.match(rb"(\d+)\s+\d+\s+R", rest)
        if ref:
            return self.objects.get(int(ref.group(1)), (b"", None))[0]
        for opener, closer in ((b"<<", b">>"), (b"[", b"]")):
            if rest.startswith(opener):
                depth, i = 0, 0
                while i < len(rest):
                    if rest.startswith(opener, i):
                        depth, i = depth + 1, i + len(opener)
                    elif rest.startswith(closer, i):
                        depth, i = depth - 1, i + len(closer)
                        if depth == 0:
                            return rest[:i]
                    else:
                        i += 1
                return rest
        return re.match(rb"[^\s/>\]]*", rest).group(0)

    def _ref(self, src: bytes, key: bytes) -> int | None:
        m = re.search(rb"/" + key + rb"(?![A-Za-z0-9])\s*(\d+)\s+\d+\s+R", src)
        return int(m.group(1)) if m else None

    # pages

    def _pages(self) -> list[int]:
        root = next((n for n, (h, _) in self.objects.items()
                     if re.search(rb"/Type\s*/Catalog", h)), None)
        out: list[int] = []
        if root is not None:
            top = self._ref(self.objects[root][0], b"Pages")
            seen: set[int] = set()

            def walk(num):
                if num in seen or num not in self.objects:
                    return
                seen.add(num)
                head = self.objects[num][0]
                if re.search(rb"/Type\s*/Pages", head):
                    for kid in _REF.findall(self._value(head, b"Kids")):
                        walk(int(kid))
                elif re.search(rb"/Type\s*/Page\b", head):
                    out.append(num)
            if top is not None:
                walk(top)
        if not out:
            out = sorted(n for n, (h, _) in self.objects.items()
                         if re.search(rb"/Type\s*/Page(?![s\w])", h))
        return out

    def _fonts(self, page_head: bytes) -> dict[bytes, tuple[dict[int, str], int]]:
        resources = self._value(page_head, b"Resources")
        fonts = self._value(resources, b"Font")
        out = {}
        for name, num in re.findall(rb"/([^\s/<>\[\]]+)\s+(\d+)\s+\d+\s+R", fonts):
            out[name] = self._cmap(int(num))
        return out

    def _cmap(self, font_num: int) -> tuple[dict[int, str], int]:
        if font_num in self._cmaps:
            return self._cmaps[font_num]
        head = self.objects.get(font_num, (b"", None))[0]
        width = 2 if re.search(rb"/Subtype\s*/Type0", head) else 1
        mapping: dict[int, str] = {}
        ref = self._ref(head, b"ToUnicode")
        stream = self.objects.get(ref, (b"", None))[1] if ref is not None else None
        if stream:
            space = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", stream)
            if space:
                width = max(1, len(space.group(1)) // 2)
            for block in re.findall(rb"beginbfchar(.*?)endbfchar", stream, re.S):
                for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
                    mapping[int(src, 16)] = _utf16(dst)
            for block in re.findall(rb"beginbfrange(.*?)endbfrange", stream, re.S):
                for lo, hi, dst in re.findall(
                        rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
                    lo_i, hi_i = int(lo, 16), int(hi, 16)
                    if dst.startswith(b"["):
                        for i, d in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", dst)):
                            mapping[lo_i + i] = _utf16(d)
                    else:
                        start = _utf16(dst[1:-1])
                        for i in range(min(hi_i - lo_i + 1, 65536)):
                            mapping[lo_i + i] = (start[:-1] + chr(ord(start[-1]) + i)
                                                 if start else "")
        self._cmaps[font_num] = (mapping, width)
        return mapping, width

    # text

    def text(self) -> str:
        pages = []
        for num in self._pages():
            head = self.objects[num][0]
            fonts = self._fonts(head)
            refs = _REF.findall(self._contents(head))
            content = b"\n".join(self.objects.get(int(r), (b"", None))[1] or b"" for r in refs)
            pages.append(self._run(content, fonts))
        return "\n".join(pages)

    def _contents(self, head: bytes) -> bytes:
        m = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", head)
        if not m:
            return b""
        value = m.group(1)
        if value.startswith(b"["):
            return value
        # A ref may point at an array object rather than a stream.
        num = int(_REF.match(value).group(1))
        obj_head, stream = self.objects.get(num, (b"", None))
        return value if stream is not None else obj_head

    def _run(self, content: bytes, fonts: dict) -> str:
        out: list[str] = []
        stack: list = []
        font: tuple[dict[int, str], int] = ({}, 1)
        array: list | None = None
        i = 0
        while i < len(content):
            m = _TOKEN.match(content, i)
            if not m:
                i += 1
                continue
            i = m.end()
            if m.group("str") is not None:
                value, i = _literal(content, i)
                (array if array is not None else stack).append(value)
            elif m.group("hex") is not None:
                hx = re.sub(rb"\s", b"", m.group("hex"))
                value = bytes.fromhex((hx + b"0" * (len(hx) % 2)).decode())
                (array if array is not None else stack).append(value)
            elif m.group("arr_open"):
                array = []
            elif m.group("arr_close"):
                stack.append(array or [])
                array = None
            elif m.group("num") is not None:
                value = float(m.group("num"))
                (array if array is not None else stack).append(value)
            elif m.group("name") is not None:
                stack.append(m.group("name"))
            elif m.group("op"):
                op = m.group("op")
                if op == b"Tf" and len(stack) >= 2 and isinstance(stack[-2], bytes):
                    font = fonts.get(stack[-2], ({}, 1))
                elif op in (b"Tj", b"'", b'"') and stack and isinstance(stack[-1], bytes):
                    if op != b"Tj":
                        out.append("\n")
                    out.append(_decode(stack[-1], font))
                elif op == b"TJ" and stack and isinstance(stack[-1], list):
                    for item in stack[-1]:
                        if isinstance(item, bytes):
                            out.append(_decode(item, font))
                        elif isinstance(item, float) and item < -200:
                            out.append(" ")
                elif op in (b"Td", b"TD", b"T*", b"Tm"):
                    out.append(" ")
                elif op == b"ET":
                    out.append("\n")
                stack = []
        text = "".join(out)
        text = re.sub(r"[ \t]+", " ", text)
        return re.sub(r" ?\n ?", "\n", text)


def _utf16(hexstr: bytes) -> str:
    try:
        return bytes.fromhex(hexstr.decode()).decode("utf-16-be", errors="ignore")
    except ValueError:
        return ""


def _decode(raw: bytes, font: tuple[dict[int, str], int]) -> str:
    mapping, width = font
    if not mapping:
        return raw.decode("cp1252", errors="replace")
    out = []
    for i in range(0, len(raw) - width + 1, width):
        code = int.from_bytes(raw[i:i + width], "big")
        out.append(mapping.get(code, ""))
    return "".join(out)


def _literal(data: bytes, i: int) -> tuple[bytes, int]:
    """A PDF literal string starting just after its "(": (bytes, next index)."""
    out = bytearray()
    depth = 1
    while i < len(data):
        c = data[i:i + 1]
        if c == b"\\":
            nxt = data[i + 1:i + 2]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
                i += 2
            elif nxt and nxt in b"01234567":
                oct_m = re.match(rb"[0-7]{1,3}", data[i + 1:i + 4])
                out.append(int(oct_m.group(0), 8) & 0xFF)
                i += 1 + len(oct_m.group(0))
            elif nxt in (b"\r", b"\n"):
                i += 2
            else:
                out += nxt
                i += 2
            continue
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
        out += c
        i += 1
    return bytes(out), i
