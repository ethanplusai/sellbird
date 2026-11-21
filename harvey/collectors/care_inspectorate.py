"""Care Inspectorate: Scotland's register of every care service, free.

The Care Inspectorate publishes a monthly "Datastore" CSV under the Open
Government Licence: every registered care service in Scotland, with its
address, phone, manager, provider, service type, client group, beds, its
latest grades by key question, and the requirements made at its recent
inspections. No key, no account, no per-record cost. Its robots.txt allows
every path.

Only adult services are kept: care homes, care at home, housing support,
adult day care, adult placement (Shared Lives) and nurse agencies. Each
becomes a ``cqc.Location`` so discovery treats Scotland the same way as
England. What the file doesn't have, and so stays empty: latitude and
longitude (it has postcodes only), websites, Companies House numbers and
whether a care home provides nursing (Scotland registers "care homes" with
no nursing split).

The CSV is about 10 MB and quick to read, so the adult rows are kept as a
small JSON file in ``data_dir`` and refreshed when older than
``MAX_AGE_DAYS``.

Inspection reports are PDFs only. They are read with the standard library:
the reports are encrypted with an empty password (RC4, text extraction
permitted), and their fonts carry ToUnicode maps, so a small reader below
is enough. No PDF package is needed.
"""

from __future__ import annotations

import csv
import hashlib
import html as _html
import io
import json
import logging
import re
import time
import zlib
from dataclasses import asdict
from pathlib import Path

from harvey.collectors.cqc import Location, search  # noqa: F401  (search re-exported)

logger = logging.getLogger(__name__)

SITE = "https://www.careinspectorate.scot"
DATA_PAGE = SITE + "/resources-data/data-and-statistics/datastore"
LOCATION_URL = SITE + "/find-care/search-for-care/care-services/{id}"
MAX_AGE_DAYS = 35
CACHE_NAME = "adult_services.json"

_LIBRARY_LINK = re.compile(
    r'href="((?:https://www\.careinspectorate\.(?:scot|com))?'
    r'/resources-data/publications-and-statistics/library/datastore[^"]*)"', re.I)
_CSV_LINK = re.compile(r'href="(https://[^"]+/datastore/[^"]*datastore[^"]*\.csv)"', re.I)

# PECR: a sole trader or a partnership is an individual subscriber, so cold
# email needs their consent first.
CONSENT_NEEDED = frozenset({"Individual", "Partnership"})

# Grades are 1-6 on every key question.
GRADE_WORDS = {1: "Unsatisfactory", 2: "Weak", 3: "Adequate", 4: "Good",
               5: "Very Good", 6: "Excellent"}
GRADE_COLUMNS = (
    ("KQ_Support_Wellbeing", "Wellbeing"),
    ("KQ_Care_and_Support_Planning", "Care and support planning"),
    ("KQ_Setting", "Setting"),
    ("KQ_Staff_Team", "Staff team"),
    ("KQ_Leadership", "Leadership"),
)
_INCORPORATED = re.compile(
    r"\b(ltd|limited|plc|llp|lp|cic|scio|c\.i\.c|incorporated|inc|l\.t\.d)\b\.?", re.I)
_JOINT = re.compile(r"\bpartnership\b|&|\band\b", re.I)


# ── reading the datastore ────────────────────────────────────────────────


def _int(value: str) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def services_from_row(row: dict) -> list[str]:
    """The short service names (as cqc uses them) for one datastore row."""
    kind = (row.get("CareService") or "").strip()
    sub = (row.get("Subtype") or "").strip().lower()
    extra = (row.get("AdditionalSubtypes") or "").lower()
    out: list[str] = []
    if kind == "Care Home Service":
        out.append("care home")
    elif kind == "Support Service":
        if sub == "care at home":
            out.append("home care")
        else:
            out.append("day care")
    elif kind == "Housing Support Service":
        # Supported housing, supported tenancies, floating and sheltered
        # housing support, with or without personal care. Only the name can
        # say it's supported living; the datastore has no subtype for it.
        out.append("housing support")
        if re.search(r"supported living", row.get("ServiceName") or "", re.I):
            out.append("supported living")
    elif kind == "Adult Placement Service":
        out.append("shared lives")
    elif kind == "Nurse Agency":
        out.append("nurse agency")
    if out and "care at home" in extra and "home care" not in out:
        out.append("home care")
    if out and "adult daycare" in extra and "day care" not in out:
        out.append("day care")
    return out


def ownership_for(row: dict) -> str:
    """Organisation | Individual | Partnership | Local Authority | NHS Body.

    The datastore says only "Private", "Voluntary or Not for Profit", "Local
    Authority" or "Health Board". A private provider whose name shows no
    incorporated body (Ltd, LLP, CIC, SCIO...) is a sole trader or a
    partnership, and is marked so: cold email to them needs consent first.
    """
    kind = (row.get("ServiceType") or "").strip()
    name = (row.get("ServiceProvider") or "").strip()
    if kind == "Local Authority":
        return "Local Authority"
    if kind == "Health Board":
        return "NHS Body"
    if kind == "Private" and not _INCORPORATED.search(name):
        return "Partnership" if _JOINT.search(name) else "Individual"
    return "Organisation"


def grades_from_row(row: dict) -> dict[str, int]:
    return {label: g for col, label in GRADE_COLUMNS if (g := _int(row.get(col, "")))}


def rating_summary(grades: dict[str, int], when: str = "") -> str:
    """"Weak: lowest grade 2 of 6 (22/09/2026)", or "" when never graded."""
    if not grades:
        return ""
    low = min(grades.values())
    text = f"{GRADE_WORDS.get(low, low)}: lowest grade {low} of 6"
    return f"{text} ({when})" if when else text


def registry_name(value: str) -> str:
    """"Surname, Forename" (cqc's form) from the datastore's "Forename Surname"."""
    value = (value or "").strip()
    if not value or "," in value or " " not in value:
        return value
    first, _, last = value.rpartition(" ")
    return f"{last}, {first}"


def requirements_from_row(row: dict) -> list[str]:
    """The requirement texts of the latest inspections in the file."""
    out = []
    for year in ("2627", "2526"):
        for nth in ("first", "second", "third", "fourth", "fifth", "sixth"):
            text = re.sub(r"\s+", " ", row.get(f"{nth}_reqs_{year}") or "").strip()
            if text and text not in out:
                out.append(text)
    return out


def location_from_row(row: dict) -> Location | None:
    """The adult service in one datastore row, or None."""
    if (row.get("Client_group") or "").strip() != "Adults":
        return None
    if (row.get("ServiceStatus") or "").strip() != "Active" or not row.get("CSNumber"):
        return None
    services = services_from_row(row)
    if not services:
        return None
    lines = [(row.get(f"Address_line_{i}") or "").strip() for i in range(1, 5)]
    town = (row.get("Service_town") or "").strip()
    street = ", ".join(x for x in lines if x and x.lower() != town.lower())
    grades = grades_from_row(row)
    beds = _int(row.get("TotalBeds", "")) or (
        _int(row.get("Registered_Places", "")) if "care home" in services else 0)
    return Location(
        id=row["CSNumber"].strip(),
        name=(row.get("ServiceName") or "").strip(),
        phone=(row.get("Service_Phone_Number") or "").strip(),
        address=street,
        city=town,
        postcode=(row.get("Service_Postcode") or "").strip(),
        local_authority=(row.get("Council_Area_Name") or "").strip(),
        services=services,
        beds=beds,
        rating=rating_summary(grades, (row.get("Publication_of_Latest_Grading") or "").strip()),
        manager=registry_name(row.get("Manager_Name", "")),
        provider_id=(row.get("SP_number") or "").strip(),
        provider_name=(row.get("ServiceProvider") or "").strip(),
        ownership=ownership_for(row),
    )


def decode_csv(raw: bytes) -> str:
    """The datastore is Windows-1252, not UTF-8; accept either."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def read_services(text: str) -> tuple[list[Location], dict[str, dict]]:
    """(locations, extras) from the datastore CSV's text.

    A service registered as both housing support and care at home is two
    rows pointing at each other (``CaseNumber_Combined``); they become one
    Location carrying both services, under the care at home row's number.

    ``extras`` holds what Location has no field for, by service number:
    ``email`` (the service's registered contact address), ``grades``
    ({key question: 1-6}), ``graded`` (date), ``requirements`` and
    ``unlisted``: True for a service the Care Inspectorate leaves off its
    public search (often a confidential address, such as a refuge), so
    don't show or write to its address.
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    by_id: dict[str, Location] = {}
    extras: dict[str, dict] = {}
    partner: dict[str, str] = {}
    for row in rows:
        loc = location_from_row(row)
        if not loc:
            continue
        by_id[loc.id] = loc
        extras[loc.id] = {
            "email": (row.get("Eforms_email_address") or "").strip(),
            "grades": grades_from_row(row),
            "graded": (row.get("Publication_of_Latest_Grading") or "").strip(),
            "requirements": requirements_from_row(row),
            "unlisted": (row.get("PublicList") or "").strip() == "No",
        }
        other = (row.get("CaseNumber_Combined") or "").strip()
        if other and other != loc.id:
            partner[loc.id] = other
    for a, b in partner.items():
        if a not in by_id or b not in by_id:
            continue
        keep, drop = (a, b) if "home care" in by_id[a].services else (b, a)
        if "home care" not in by_id[keep].services:
            keep, drop = min(a, b), max(a, b)
        for s in by_id[drop].services:
            if s not in by_id[keep].services:
                by_id[keep].services.append(s)
        ex, dx = extras[keep], extras.pop(drop)
        ex["email"] = ex["email"] or dx["email"]
        ex["grades"] = ex["grades"] or dx["grades"]
        ex["requirements"] = ex["requirements"] or dx["requirements"]
        ex["unlisted"] = ex["unlisted"] or dx["unlisted"]
        del by_id[drop]
    out = list(by_id.values())
    per_provider: dict[str, int] = {}
    for loc in out:
        per_provider[loc.provider_id] = per_provider.get(loc.provider_id, 0) + 1
    for loc in out:
        loc.provider_locations = per_provider.get(loc.provider_id, 1)
    return out, extras


# ── the local copy ───────────────────────────────────────────────────────


def find_library_links(page_html: str) -> list[str]:
    """Links to the monthly datastore pages, newest (first listed) first."""
    out = []
    for link in _LIBRARY_LINK.findall(page_html or ""):
        url = link if link.startswith("http") else SITE + link
        if url not in out:
            out.append(url)
    return out


def find_csv_url(page_html: str) -> str:
    match = _CSV_LINK.search(page_html or "")
    return _html.unescape(match.group(1)) if match else ""


def _fresh(path: Path, max_age_days: int = MAX_AGE_DAYS) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400


def save_cache(locations: list[Location], extras: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"locations": [asdict(x) for x in locations],
                               "extras": extras}), encoding="utf-8")
    tmp.replace(path)


def _read_cache(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cache(path: Path) -> list[Location]:
    return [Location(**item) for item in _read_cache(path)["locations"]]


def service_extras(data_dir: Path) -> dict[str, dict]:
    """{service number: {"email", "grades", "graded", "requirements", "unlisted"}} from
    the local copy ({} before the first download)."""
    try:
        return _read_cache(data_dir / CACHE_NAME).get("extras", {})
    except (OSError, ValueError):
        return {}


async def _csv_url(client) -> str:
    page = await client.get(DATA_PAGE)
    page.raise_for_status()
    url = find_csv_url(page.text)
    if url:
        return url
    for link in find_library_links(page.text)[:3]:
        resp = await client.get(link)
        if resp.status_code == 200 and (url := find_csv_url(resp.text)):
            return url
    raise RuntimeError("the Care Inspectorate's datastore page no longer links a CSV file")


async def load_register(client, data_dir: Path) -> list[Location]:
    """Scotland's adult care services, from the local copy when it's fresh,
    else downloaded.

    Raises RuntimeError with a plain reason when the Care Inspectorate can't
    be reached and there is no copy at all; an old copy is used with a
    warning.
    """
    cache = data_dir / CACHE_NAME
    if _fresh(cache):
        return load_cache(cache)
    try:
        url = await _csv_url(client)
        logger.info("care inspectorate: downloading %s", url)
        resp = await client.get(url)
        resp.raise_for_status()
        locations, extras = read_services(decode_csv(resp.content))
        if not locations:
            raise RuntimeError("the datastore had no adult services in it")
    except Exception as e:
        if cache.exists():
            logger.warning("care inspectorate: couldn't refresh the register (%s); "
                           "using the copy from before", e)
            return load_cache(cache)
        raise RuntimeError(f"Couldn't download the Care Inspectorate register: {e}") from e
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "datastore.csv").write_bytes(resp.content)
    save_cache(locations, extras, cache)
    logger.info("care inspectorate: %d active adult care services", len(locations))
    return locations


def url(service_id: str) -> str:
    return LOCATION_URL.format(id=service_id)


# ── reading a report PDF ─────────────────────────────────────────────────
#
# Enough of PDF for the Care Inspectorate's reports: objects found by
# regex, the standard security handler (RC4, revisions 2-3, empty user
# password), Flate streams, simple fonts with ToUnicode maps, and the text
# operators. Spaces come from the gaps between runs, measured with the
# fonts' /Widths, since the reports position words rather than spacing them.

_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b(.*?)\bendobj", re.S)
_TOKEN = re.compile(
    rb"<([0-9A-Fa-f\s]*)>|\(((?:\\.|[^\\)])*)\)|(\[)|(\])"
    rb"|([-+]?\d*\.?\d+)|/([^\s/<>\[\]()]+)|([A-Za-z'\"*]+)", re.S)
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
PDF_MAX_BYTES = 8_000_000


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 255
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, byte in enumerate(data):
        i = (i + 1) & 255
        j = (j + s[i]) & 255
        s[i], s[j] = s[j], s[i]
        out[n] = byte ^ s[(s[i] + s[j]) & 255]
    return bytes(out)


def _literal(raw: bytes) -> bytes:
    def esc(m):
        c = m.group(1)
        if c in _ESCAPES:
            return _ESCAPES[c]
        if c.isdigit():
            return bytes([int(m.group(0)[1:], 8) & 255])
        return c
    return re.sub(rb"\\([0-7]{1,3}|.)", esc, raw, flags=re.S)


class _Pdf:
    def __init__(self, data: bytes):
        self.data = data
        self.objs = {int(m.group(1)): (int(m.group(2)), m.group(3))
                     for m in _OBJ.finditer(data)}
        self.key = self._key()

    def _key(self) -> bytes | None:
        enc = re.search(rb"/Encrypt\s+(\d+)\s+\d+\s+R", self.data)
        if not enc:
            return None
        body = self.objs[int(enc.group(1))][1]
        if not re.search(rb"/Filter\s*/Standard", body):
            raise ValueError("unsupported PDF encryption")
        v = int((re.search(rb"/V\s+(\d+)", body) or [0, b"1"])[1])
        r = int(re.search(rb"/R\s+(\d+)", body).group(1))
        if v > 2 or r > 3:
            raise ValueError("unsupported PDF encryption (AES)")
        owner = re.search(rb"/O\s*(<[0-9A-Fa-f\s]+>|\((?:\\.|[^\\)])*\))", body, re.S).group(1)
        owner = (bytes.fromhex(re.sub(rb"\s", b"", owner[1:-1]).decode())
                 if owner.startswith(b"<") else _literal(owner[1:-1]))
        p = int(re.search(rb"/P\s+(-?\d+)", body).group(1))
        length = int((re.search(rb"/Length\s+(\d+)", body) or [0, b"40"])[1]) // 8 if r >= 3 else 5
        file_id = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>", self.data)
        h = hashlib.md5(_PAD + owner[:32] + (p & 0xFFFFFFFF).to_bytes(4, "little")
                        + (bytes.fromhex(file_id.group(1).decode()) if file_id else b"")).digest()
        if r >= 3:
            for _ in range(50):
                h = hashlib.md5(h[:length]).digest()
        return h[:length]

    def stream(self, num: int) -> bytes:
        gen, body = self.objs[num]
        m = re.search(rb"stream\r?\n(.*?)\r?\n?endstream", body, re.S)
        if not m:
            return b""
        raw = m.group(1)
        if self.key:
            k = hashlib.md5(self.key + num.to_bytes(3, "little")
                            + gen.to_bytes(2, "little")).digest()
            raw = _rc4(k[:min(len(self.key) + 5, 16)], raw)
        if b"FlateDecode" in body[:m.start()]:
            raw = zlib.decompressobj().decompress(raw)
        return raw

    def pages(self) -> list[int]:
        """Page objects in reading order, through the /Kids tree."""
        root = re.search(rb"/Root\s+(\d+)", self.data)
        out: list[int] = []

        def walk(num: int, depth: int = 0):
            if num not in self.objs or depth > 20:
                return
            body = self.objs[num][1]
            if re.search(rb"/Type\s*/Page\b", body):
                out.append(num)
            elif kids := re.search(rb"/Kids\s*\[([^\]]*)\]", body):
                for ref in re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1)):
                    walk(int(ref), depth + 1)
        if root and (pages := re.search(rb"/Pages\s+(\d+)", self.objs.get(
                int(root.group(1)), (0, b""))[1])):
            walk(int(pages.group(1)))
        return out or sorted(n for n, (_, b) in self.objs.items()
                             if re.search(rb"/Type\s*/Page\b", b))

    def font(self, num: int) -> tuple[dict[int, str] | None, dict[int, float]]:
        body = self.objs.get(num, (0, b""))[1]
        cmap = None
        if b"MacRomanEncoding" in body:
            cmap = {b: bytes([b]).decode("mac_roman") for b in range(256)}
        if tu := re.search(rb"/ToUnicode\s+(\d+)", body):
            cmap = {}
            s = self.stream(int(tu.group(1))).decode("latin-1")
            for block in re.findall(r"beginbfchar(.*?)endbfchar", s, re.S):
                for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                    cmap[int(a, 16)] = bytes.fromhex(b).decode("utf-16-be", "replace")
            for block in re.findall(r"beginbfrange(.*?)endbfrange", s, re.S):
                for a, b, c in re.findall(
                        r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                    lo, hi, base = int(a, 16), int(b, 16), int(c, 16)
                    for code in range(lo, min(hi, lo + 512) + 1):
                        cmap[code] = chr(base + code - lo)
        widths: dict[int, float] = {}
        first = re.search(rb"/FirstChar\s+(\d+)", body)
        w = re.search(rb"/Widths\s*\[([^\]]*)\]", body)
        if first and w:
            for i, value in enumerate(w.group(1).split()):
                try:
                    widths[int(first.group(1)) + i] = float(value)
                except ValueError:
                    pass
        return cmap, widths

    def page_text(self, num: int) -> str:
        body = self.objs[num][1]
        fonts = {}
        if block := re.search(rb"/Font\s*<<(.*?)>>", body, re.S):
            for name, ref in re.findall(rb"/([^\s/]+)\s+(\d+)\s+\d+\s+R", block.group(1)):
                fonts[name] = self.font(int(ref))
        contents = re.search(rb"/Contents\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", body)
        if not contents:
            return ""
        content = b"\n".join(self.stream(int(n)) for n in
                             re.findall(rb"(\d+)\s+\d+\s+R", contents.group(1)))
        out: list[str] = []
        cmap, widths, size = None, {}, 12.0
        run = 0.0           # width of the text since the last line start
        nums: list[float] = []
        name = b""
        in_array = False

        def show(raw: bytes):
            nonlocal run
            out.append("".join(cmap.get(b, "") for b in raw) if cmap is not None
                       else raw.decode("latin-1"))
            run += sum(widths.get(b, 500.0) for b in raw) * size / 1000

        for m in _TOKEN.finditer(content):
            hexs, lit, open_, close, num, nm, op = m.groups()
            if hexs is not None:
                show(bytes.fromhex(re.sub(rb"\s", b"", hexs).decode() or ""))
            elif lit is not None:
                show(_literal(lit))
            elif open_:
                in_array = True
            elif close:
                in_array = False
            elif num is not None:
                value = float(num)
                if in_array:
                    if value < -200:
                        out.append(" ")
                    run -= value * size / 1000
                else:
                    nums.append(value)
                continue
            elif nm is not None:
                name = nm
                continue
            elif op:
                if op == b"Tf":
                    cmap, widths = fonts.get(name, (None, {}))
                    size = nums[-1] if nums else size
                elif op in (b"Td", b"TD") and len(nums) >= 2:
                    tx, ty = nums[-2], nums[-1]
                    if abs(ty) > 0.01:
                        out.append("\n")
                    elif tx - run > size * 0.15:
                        out.append(" ")
                    run = 0.0
                elif op in (b"T*", b"'", b'"', b"Tm", b"ET", b"BT"):
                    out.append("\n")
                    run = 0.0
            nums = []
        return "".join(out)

    def text(self) -> str:
        pages = []
        for num in self.pages():
            try:
                pages.append(self.page_text(num))
            except Exception as e:  # one bad page shouldn't lose the report
                logger.debug("care inspectorate: page %s unreadable: %s", num, e)
        text = "\n".join(pages)
        text = re.sub(r"[ \t ]+", " ", text)
        return re.sub(r"\s*\n\s*", "\n", text).strip()


def pdf_text(data: bytes) -> str:
    """The text of a Care Inspectorate report PDF, a line per text line."""
    return _Pdf(data).text()


# ── the latest inspection report ─────────────────────────────────────────

REPORT_MAX_AGE_DAYS = 30
REPORT_CHARS = 2500
_REPORT_LINK = re.compile(
    r'(https://[^"\s]+/inspection-reports/[^"\s/]+/[^"\s/]+\.pdf)', re.I)
_RECENT = re.compile(r"Most recent inspection:\s*(?:<[^>]+>\s*)*(\d{1,2} \w+ \d{4})", re.I)
_GRADE_LINE = re.compile(
    r"^(How (?:well|good)[^?\n]*\?)\n([1-6]) - (Unsatisfactory|Weak|Adequate|Good|"
    r"Very Good|Excellent)\b", re.M | re.I)
# Running heads: "Inspection report for ...", drawn twice over itself.
_HEADER = re.compile(r"^(?:Inspection r.*|p ?age \d+ of \d+)$", re.M | re.I)


def report_ratings(text: str) -> dict[str, str]:
    """{"How well do we support people's wellbeing?": "2 - Weak", ...}."""
    out: dict[str, str] = {}
    for q, grade, word in _GRADE_LINE.findall(text):
        out.setdefault(q, f"{grade} - {word}")
    return out


def _section(text: str, start: str, ends: tuple[str, ...]) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    body = text[i + len(start):]
    cut = [j for j in (body.find(e) for e in ends) if j > 0]
    return body[:min(cut)] if cut else body


def _first_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:\d+[.)]|[a-z]\))\s+", "", text)      # "1. By 14 August..."
    text = re.split(r"\s(?:In order to achieve this|To do this|This is to comply)\b", text)[0]
    match = re.match(r"(.+?[.!?])(?:\s|$)", text)
    return (match.group(1) if match else text)[:400]


def report_summary_from(text: str, limit: int = REPORT_CHARS) -> str:
    """Key messages, then each requirement and area for improvement in one
    sentence, out of a report's text, trimmed to ``limit``."""
    text = _HEADER.sub("", text)
    parts = []
    key = _section(text, "Key messages", ("From this inspection", "How well do we",
                                          "How good is", "Requirements", "Areas for improvement",
                                          "What the service has done"))
    if key.strip():
        # One bullet per message; a message's own lines are wrapped.
        items = re.split(r"\s*[•●]\s*", key)
        items = [re.sub(r"\s+", " ", x).strip() for x in items]
        parts.append("Key messages: " + " ".join(
            x if x.endswith((".", "!", "?")) else x + "." for x in items if x))
    for label, pattern in (("Requirements", r"\nRequirement \d+\n"),
                           ("Areas for improvement", r"\nArea for improvement \d+\n")):
        found = []
        for m in re.finditer(pattern, text):
            sentence = _first_sentence(text[m.end():m.end() + 1500])
            if sentence and sentence not in found:
                found.append(sentence)
        if found:
            parts.append(f"{label}: " + " ".join(found))
    body = "\n".join(parts)
    if len(body) > limit:
        body = body[:limit].rsplit(". ", 1)[0] + "."
    return body.strip()


def _summary_from_extras(extras: dict, limit: int = REPORT_CHARS) -> str:
    reqs = [_first_sentence(r) for r in extras.get("requirements") or []]
    if not reqs:
        return ""
    body = "Requirements: " + " ".join(reqs)
    if len(body) > limit:
        body = body[:limit].rsplit(". ", 1)[0] + "."
    return body


async def latest_report(client, service_id: str, data_dir: Path) -> dict:
    """{"ratings", "summary", "url", "published"} for a service's latest
    Care Inspectorate report, the same shape as ``cqc.latest_report``.

    ``ratings`` is {key question: "2 - Weak"} from the report (or, when the
    report has none, as with a follow-up visit, the datastore's latest
    grades as {"Wellbeing": "2 - Weak", ...}). ``summary`` is the key
    messages plus each requirement and area for improvement. ``url`` is the
    PDF. {} when the Care Inspectorate can't be reached and the datastore
    copy has nothing either. Cached per service in data_dir/reports.json
    for REPORT_MAX_AGE_DAYS.
    """
    cache_path = data_dir / "reports.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    hit = cache.get(service_id)
    if hit and time.time() - hit.get("at", 0) < REPORT_MAX_AGE_DAYS * 86400:
        return hit.get("report", {})
    extras = service_extras(data_dir).get(service_id, {})
    ratings: dict[str, str] = {}
    summary = report_url = published = ""
    failed = False
    try:
        page = await client.get(url(service_id))
        page.raise_for_status()
        link = _REPORT_LINK.search(page.text.replace("\\/", "/"))
        date = _RECENT.search(page.text)
        published = date.group(1) if date else ""
        if link:
            report_url = link.group(1)
            resp = await client.get(report_url)
            resp.raise_for_status()
            if len(resp.content) > PDF_MAX_BYTES:
                raise ValueError("report PDF is too large")
            text = pdf_text(resp.content)
            ratings = report_ratings(text)
            summary = report_summary_from(text)
    except Exception as e:
        logger.warning("care inspectorate: couldn't read the report for %s: %s",
                       service_id, e)
        if not extras:
            return {}
        failed = True      # answer from the datastore now, try the page next time
    if not ratings and extras.get("grades"):
        ratings = {k: f"{v} - {GRADE_WORDS.get(v, v)}" for k, v in extras["grades"].items()}
    if not summary:
        summary = _summary_from_extras(extras)
    report = {}
    if ratings or summary:
        report = {"ratings": ratings, "summary": summary,
                  "url": report_url or url(service_id),
                  "published": published or extras.get("graded", "")}
    if failed:
        return report
    cache[service_id] = {"at": time.time(), "report": report}
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError as e:
        logger.warning("care inspectorate: couldn't save the report cache: %s", e)
    return report
