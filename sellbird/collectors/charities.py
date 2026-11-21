"""Charities that provide supported housing, from the UK charity registers.

Many supported housing, floating support and hostel providers are charities
that no care regulator registers: they give housing-related support, not
personal care. The charity registers list them all, with their income,
contact details and what they say they do. No key, no account, no
per-record cost.

Sources
-------
* Charity Commission for England and Wales (CCEW): the full register
  extract, daily, as zipped tab-separated text on the Commission's blob
  store. Open Government Licence v3.0; the register site's robots.txt
  allows everything. Three files are read: ``charity`` (name, status,
  income, contact details, activities), ``charity_classification`` (what /
  who / how codes, 107 being "Accommodation/housing") and
  ``charity_governing_document`` (charitable objects).
* Charity Commission for Northern Ireland (CCNI): the "Export to CSV" of its
  register search, every charity in one CSV with income, contact details,
  classifications and charitable purposes. The site has no robots.txt and
  states no licence for the export; it is public register data.
* OSCR (Scotland) is deliberately NOT read. Its download page makes every
  user agree "I will not use this information for direct marketing", and
  Sellbird uses what it finds for cold email. Scottish providers come from the
  Care Inspectorate reader instead.

Matching
--------
A charity is kept when what it says about itself (name, activities, objects,
purposes) names housing support (supported housing, floating support,
tenancy sustainment, hostels, move-on or exempt accommodation...) or when it
is a housing charity that also describes support work. Each gets a ``score``
and a short ``match_reason``; almshouses and housing-only charities score
lowest. Only registered, active charities are kept (not removed, insolvent,
in administration, dissolved, or two years behind with their accounts).

The extracts are large (CCEW is about 90 MB zipped, 480 MB of text), so they
are streamed line by line, only a few small numbers are kept per charity
until the last pass, and the downloads are deleted once read. What matched
is kept as a small JSON file per register in ``data_dir`` and refreshed when
older than ``MAX_AGE_DAYS``.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from sellbird.collectors.cqc import Location, _website

logger = logging.getLogger(__name__)

MAX_AGE_DAYS = 30
MIN_INCOME = 100_000

CCEW_BASE = "https://ccewuksprdoneregsadata1.blob.core.windows.net/data/txt/"
CCEW_FILES = ("charity", "charity_classification", "charity_governing_document")
CCEW_URL = ("https://register-of-charities.charitycommission.gov.uk/charity-search/"
            "-/charity-details/{number}")
CCNI_EXPORT = ("https://www.charitycommissionni.org.uk/api/charity-search/"
               "exportSearchResultsToCsv/?pageNumber=1&sortOption=Rank%3Basc&searchText=")
CCNI_URL = "https://www.charitycommissionni.org.uk/charity-details/?regId={number}&subId=0"

SERVICES = ["supported housing"]

# ── what counts as housing support ───────────────────────────────────────

# Phrases that on their own mean housing-related support, with the label
# that goes into match_reason. Matched on lower-case text.
STRONG = (
    ("supported housing", re.compile(r"\bsupported[- ]housing\b")),
    ("supported accommodation", re.compile(r"\bsupported[- ]accommodation\b")),
    ("supported tenancies", re.compile(r"\bsupported[- ]tenanc")),
    ("supported lodgings", re.compile(r"\bsupported[- ]lodging")),
    ("supported living", re.compile(r"\bsupported[- ]living\b")),
    ("floating support", re.compile(r"\bfloating[- ]support")),
    ("tenancy sustainment", re.compile(r"\btenancy[- ]sustain")),
    ("tenancy support", re.compile(r"\btenancy[- ]support")),
    ("housing related support", re.compile(r"\bhousing[- ]related[- ]support")),
    ("housing support", re.compile(r"\bhousing[- ]support")),
    ("exempt accommodation", re.compile(r"\bexempt[- ]accommodation")),
    ("move-on accommodation", re.compile(r"\bmove[- ]on[- ](?:accommodation|housing|flats?)")),
    ("hostels", re.compile(r"\b(?:homeless(?:ness)?|direct[- ]access)[- ]hostels?\b")),
    ("refuge", re.compile(r"\b(?:women'?s|domestic (?:abuse|violence)) refuges?\b"
                          r"|\brefuge accommodation")),
)
# Housing words that need a support word too before they count.
HOUSING = re.compile(r"\b(?:homeless\w*|hostels?|rough[- ]sleep\w*|tenan\w+|"
                     r"housing|accommodation for|refuges?|foyers?)\b")
# Who the support is for, or what it is. Plain "support" is left out: most
# charities "support their community" somewhere in their objects.
SUPPORT = re.compile(r"\b(?:vulnerable|mental (?:health|ill\w*)|learning disabilit\w*|"
                     r"addiction\w*|substance (?:mis)?use|alcohol\w*|drugs?|"
                     r"(?:ex-?)?offenders?|prison\w*|care leavers?|"
                     r"domestic (?:abuse|violence)|independent living|rough[- ]sleep\w*|"
                     r"resettle\w*|homeless\w*|asylum|refugees?|support workers?|"
                     r"key ?workers?|life skills|living skills)\b")
ALMSHOUSE = re.compile(r"\balms[- ]?house")
# Not housing support, and they'd otherwise match "hostel" / "accommodation".
NOISE = re.compile(r"\byouth hostels?\b|\bstudent (?:accommodation|hostels?)\b|"
                   r"\bhalls? of residence\b|\bholiday accommodation\b")

# Every pattern above needs one of these word stems; a text with none of
# them sets no bits.
_ANY = re.compile(r"support|hous|accommodation|hostel|homeless|tenan|refuge|alms|"
                  r"rough|foyer|lodging|vulnerab|mental|learning|addict|substance|"
                  r"alcohol|drug|offender|prison|leaver|domestic|independent|resettl|"
                  r"asylum|key ?worker|skills")

# Feature bits kept per charity between passes (an int each keeps memory low).
_BIT_STRONG = {label: 1 << i for i, (label, _) in enumerate(STRONG)}
B_HOUSING_TEXT = 1 << 20
B_SUPPORT_TEXT = 1 << 21
B_ALMSHOUSE = 1 << 22
B_HOUSING_CLASS = 1 << 23    # CCEW 107 / CCNI "Homelessness" or "Tenants"
B_SERVICES_CLASS = 1 << 24   # "Provides services"
B_VULNERABLE_CLASS = 1 << 25  # CCEW 203 / 206: disabled people, other defined groups
_ALL_STRONG = sum(_BIT_STRONG.values())


def text_features(text: str) -> int:
    """The feature bits a piece of free text sets."""
    low = (text or "").lower()
    if not _ANY.search(low):   # most charities: one quick look and done
        return 0
    low = NOISE.sub(" ", low)
    bits = 0
    for label, pattern in STRONG:
        if pattern.search(low):
            bits |= _BIT_STRONG[label]
    if HOUSING.search(low):
        bits |= B_HOUSING_TEXT
    if SUPPORT.search(low):
        bits |= B_SUPPORT_TEXT
    if ALMSHOUSE.search(low):
        bits |= B_ALMSHOUSE
    return bits


def score(bits: int) -> tuple[int, str]:
    """(score, match_reason) for a charity's feature bits; score 0 = not a match.

    5-3: names housing support itself (more phrases, higher).
    2:   a housing charity (by text or classification) that describes support work.
    1:   a housing charity with no support work stated, or an almshouse.
    """
    labels = [label for label, bit in _BIT_STRONG.items() if bits & bit]
    almshouse = bool(bits & B_ALMSHOUSE)
    if labels:
        shown = ", ".join(labels[:3])
        return min(5, 2 + len(labels)), f"says {shown}"
    housing_class = bool(bits & B_HOUSING_CLASS)
    housing = housing_class or bool(bits & B_HOUSING_TEXT)
    if not housing:
        return 0, ""
    where = "classed as accommodation/housing" if housing_class else "describes housing work"
    if almshouse:
        return 1, f"almshouse charity ({where})"
    if bits & B_SUPPORT_TEXT:
        return 2, f"{where}, with support work"
    if housing_class and bits & B_SERVICES_CLASS and bits & B_VULNERABLE_CLASS:
        return 1, f"{where}, provides services to vulnerable groups"
    if housing_class:
        return 1, f"{where}, no support work stated"
    return 0, ""


# ── the record ───────────────────────────────────────────────────────────


@dataclass
class Charity(Location):
    """A registered charity, in the shape of a CQC location.

    ``income`` is the latest annual income in whole pounds; ``score`` and
    ``match_reason`` say why it was picked (see ``score()``).
    """

    register: str = ""            # "CCEW" | "CCNI"
    charity_number: str = ""
    income: int = 0
    income_year_end: str = ""     # YYYY-MM-DD of the accounts the income is from
    email: str = ""
    score: int = 0
    match_reason: str = ""

    @property
    def url(self) -> str:
        template = CCNI_URL if self.register == "CCNI" else CCEW_URL
        return template.format(number=self.charity_number)


def _int(value: str) -> int:
    try:
        return int(float((value or "").replace(",", "").replace("£", "").strip() or 0))
    except ValueError:
        return 0


def _company_number(value: str) -> str:
    value = (value or "").strip().upper()
    if not value or value.strip("0") == "":
        return ""
    return value.zfill(8) if value.isdigit() else value


# ── CCEW ─────────────────────────────────────────────────────────────────


def tsv_rows(path: Path):
    """Each row of the one tab-separated file in a CCEW zip, as a dict."""
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        with z.open(name) as raw:
            lines = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
            header = lines.readline().rstrip("\r\n").split("\t")
            for line in lines:
                cells = line.rstrip("\r\n").split("\t")
                if len(cells) == len(header):
                    yield dict(zip(header, cells))


CLASS_BITS = {"107": B_HOUSING_CLASS, "306": B_SERVICES_CLASS,
              "203": B_VULNERABLE_CLASS, "206": B_VULNERABLE_CLASS}


def _ccew_active(row: dict) -> bool:
    return (row.get("charity_registration_status") == "Registered"
            and row.get("linked_charity_number") == "0"
            and row.get("charity_insolvent") != "True"
            and row.get("charity_in_administration") != "True"
            and row.get("cio_is_dissolved") != "True"
            and row.get("charity_reporting_status") != "Submission Double Default")


def read_ccew(charity_zip: Path, classification_zip: Path,
              governing_zip: Path) -> list[Charity]:
    """Every active CCEW main charity that matches, at any income."""
    bits: dict[str, int] = {}
    for row in tsv_rows(classification_zip):
        if row.get("linked_charity_number") != "0":
            continue
        bit = CLASS_BITS.get(row.get("classification_code", ""))
        if bit:
            org = row["organisation_number"]
            bits[org] = bits.get(org, 0) | bit
    for row in tsv_rows(governing_zip):
        if row.get("linked_charity_number") != "0":
            continue
        found = text_features(row.get("charitable_objects", ""))
        if found:
            org = row["organisation_number"]
            bits[org] = bits.get(org, 0) | found
    out: list[Charity] = []
    for row in tsv_rows(charity_zip):
        if not _ccew_active(row):
            continue
        org = row["organisation_number"]
        found = bits.get(org, 0) | text_features(
            row.get("charity_name", "") + " . " + row.get("charity_activities", ""))
        points, reason = score(found)
        if not points:
            continue
        lines = [row.get(f"charity_contact_address{i}", "").strip() for i in range(1, 6)]
        lines = [x for x in lines if x]
        city = lines.pop() if len(lines) > 1 else ""
        number = row["registered_charity_number"]
        out.append(Charity(
            id=f"ccew-{number}",
            name=row.get("charity_name", "").strip(),
            phone=row.get("charity_contact_phone", "").strip(),
            website=_website(row.get("charity_contact_web", "")),
            email=row.get("charity_contact_email", "").strip().lower(),
            address=", ".join(lines),
            city=city,
            postcode=row.get("charity_contact_postcode", "").strip().upper(),
            services=list(SERVICES),
            provider_id=f"ccew-{number}",
            provider_name=row.get("charity_name", "").strip(),
            company_number=_company_number(row.get("charity_company_registration_number", "")),
            ownership="Organisation",
            provider_locations=1,
            register="CCEW",
            charity_number=number,
            income=_int(row.get("latest_income", "")),
            income_year_end=(row.get("latest_acc_fin_period_end_date", "") or "")[:10],
            score=points,
            match_reason=reason,
        ))
    return out


# ── CCNI ─────────────────────────────────────────────────────────────────

_BT_POSTCODE = re.compile(r"\bBT\d{1,2}\s*\d[A-Z]{2}\b", re.I)
_OVERDUE = re.compile(r"Overdue:\s*(\d+)\s*days", re.I)
MAX_OVERDUE_DAYS = 365


def _ccni_active(status: str) -> bool:
    status = (status or "").strip()
    if not status or status.lower().startswith("removed"):
        return False
    late = _OVERDUE.search(status)
    return not (late and int(late.group(1)) > MAX_OVERDUE_DAYS)


def _ccni_address(value: str) -> tuple[str, str, str]:
    """(street, town, postcode) from CCNI's one-line address."""
    value = (value or "").strip().rstrip(".")
    match = _BT_POSTCODE.search(value)
    postcode = ""
    if match:
        postcode = re.sub(r"\s+", " ", match.group(0).upper())
        if " " not in postcode:
            postcode = postcode[:-3] + " " + postcode[-3:]
        value = (value[:match.start()] + value[match.end():])
    parts = [p.strip(" .") for p in value.split(",") if p.strip(" .")]
    town = parts.pop() if len(parts) > 1 else ""
    return ", ".join(parts), town, postcode


def read_ccni(path: Path) -> list[Charity]:
    """Every active CCNI charity in the export that matches, at any income."""
    out: list[Charity] = []
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            if not _ccni_active(row.get("Status", "")):
                continue
            who = row.get("Who the charity helps", "")
            how = row.get("How the charity works", "")
            found = text_features(" . ".join(
                row.get(k, "") for k in ("Charity name", "Other name", "Charitable purposes")))
            if "Homelessness" in who or "Tenants" in who:
                found |= B_HOUSING_CLASS
            if "Provides services" in how:
                found |= B_SERVICES_CLASS
            if any(w in who for w in ("disabilities", "Mental health", "Addictions",
                                      "Ex-offenders", "Homelessness")):
                found |= B_VULNERABLE_CLASS
            points, reason = score(found)
            if not points:
                continue
            number = (row.get("Reg charity number") or "").strip()
            if not number:
                continue
            street, town, postcode = _ccni_address(row.get("Public address", ""))
            year_end = (row.get("Date for financial year ending") or "").strip()
            out.append(Charity(
                id=f"ccni-{number}",
                name=(row.get("Charity name") or "").strip(),
                phone=(row.get("Telephone") or "").strip(),
                website=_website(row.get("Website", "")),
                email=(row.get("Email") or "").strip().lower(),
                address=street,
                city=town,
                postcode=postcode,
                services=list(SERVICES),
                provider_id=f"ccni-{number}",
                provider_name=(row.get("Charity name") or "").strip(),
                company_number=_company_number(row.get("Company number", "")),
                ownership="Organisation",
                provider_locations=1,
                register="CCNI",
                charity_number=number,
                income=_int(row.get("Total income", "")),
                income_year_end=_iso_date(year_end),
                score=points,
                match_reason=reason,
            ))
    return out


def _iso_date(value: str) -> str:
    """YYYY-MM-DD from "DD/MM/YYYY" (or the value's first ten characters)."""
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", value or "")
    if match:
        d, m, y = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return (value or "")[:10]


# ── the local copy ───────────────────────────────────────────────────────


def _fresh(path: Path, max_age_days: int = MAX_AGE_DAYS) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400


def save_cache(charities: list[Charity], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(c) for c in charities]), encoding="utf-8")
    tmp.replace(path)


def load_cache(path: Path) -> list[Charity]:
    return [Charity(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


async def _download(client, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with tmp.open(mode="wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)
    tmp.replace(dest)


def _remove(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


async def _refresh_ccew(client, data_dir: Path) -> list[Charity]:
    zips = [data_dir / f"ccew_{name}.zip" for name in CCEW_FILES]
    try:
        for name, dest in zip(CCEW_FILES, zips):
            logger.info("charities: downloading the CCEW %s extract", name)
            await _download(client, f"{CCEW_BASE}publicextract.{name}.zip", dest)
        # Minutes of parsing: off the event loop so Sellbird keeps running.
        return await asyncio.to_thread(read_ccew, *zips)
    finally:
        _remove(*zips)


async def _refresh_ccni(client, data_dir: Path) -> list[Charity]:
    dest = data_dir / "ccni_export.csv"
    try:
        logger.info("charities: downloading the CCNI register export")
        await _download(client, CCNI_EXPORT, dest)
        with dest.open(mode="rb") as f:
            if f.read(64).lstrip().lower().startswith((b"<!doctype", b"<html")):
                raise RuntimeError("CCNI returned a web page, not the CSV export")
        return await asyncio.to_thread(read_ccni, dest)
    finally:
        _remove(dest)


REGISTERS = (("CCEW", "ccew.json", _refresh_ccew), ("CCNI", "ccni.json", _refresh_ccni))


async def load_register(client, data_dir: Path, min_income: int = MIN_INCOME,
                        min_score: int = 1) -> list[Charity]:
    """Housing-support charities from CCEW and CCNI, best match first.

    Each register comes from its local copy when that is fresh, else is
    downloaded. A register that can't be reached falls back to its old copy
    (with a warning), or is skipped when there is none; RuntimeError only
    when no register could be read at all. Filtered to ``income >=
    min_income`` and ``score >= min_score``, sorted by score, then income.
    """
    found: list[Charity] = []
    errors: list[str] = []
    for label, filename, refresh in REGISTERS:
        cache = data_dir / filename
        if _fresh(cache):
            found.extend(load_cache(cache))
            continue
        try:
            charities = await refresh(client, data_dir)
        except Exception as e:
            if cache.exists():
                logger.warning("charities: couldn't refresh %s (%s); using the copy "
                               "from before", label, e)
                found.extend(load_cache(cache))
            else:
                logger.warning("charities: couldn't read %s: %s", label, e)
                errors.append(f"{label}: {e}")
            continue
        save_cache(charities, cache)
        logger.info("charities: %d %s charities match housing support", len(charities), label)
        found.extend(charities)
    if errors and len(errors) == len(REGISTERS):
        raise RuntimeError("Couldn't download the charity registers: " + "; ".join(errors))
    picked = [c for c in found if c.income >= min_income and c.score >= min_score]
    picked.sort(key=lambda c: (-c.score, -c.income, c.name))
    return picked
