"""The UK's registers of social landlords: housing associations and other
registered providers, free.

Five official, public sources, each downloaded once and kept in ``data_dir``:

* **England** — the Regulator of Social Housing's monthly "List of registered
  providers" (.xlsx on GOV.UK, Open Government Licence). It has only the name,
  RSH registration number, designation and corporate form: no address, no
  website, no supported housing column. So it is joined, on the registration
  number, to RSH's yearly stock "look-up tool" (.xlsx, same licence), whose
  ``STOCK_BY_LA`` sheet gives each provider's units in each council area,
  including ``LA_SHHOP``: "supported housing / housing for older people"
  units. RSH reports those two together, so a provider with any such unit is
  marked "supported housing" and ``supported_units`` says how many. Councils
  (designation "Local Authority") are left out: they're councils, not
  housing associations.
* **Scotland** — the Scottish Housing Regulator's Register of Social
  Landlords, one HTML page (OGL). "Clients" is General, Supported or Mixed;
  Supported and Mixed are marked "supported housing".
* **Wales** — the Welsh Government's register of social landlords (.xlsx on
  gov.wales, OGL): the CURRENT and De_Minimis sheets. No supported housing
  column, no website.
* **Northern Ireland, housing associations** — the Department for
  Communities' page for each registered housing association (Crown copyright,
  OGL). A page whose description mentions supported or sheltered housing is
  marked "supported housing".
* **Northern Ireland, Supporting People** — the Housing Executive's
  Supporting People Service Directory, read from the public ArcGIS layer
  behind its map. One record per funded *service* (not per organisation),
  with postcode, website, council and coordinates. The layer states no
  licence. Floating support services get "floating support" as well as
  "supported housing".

Only the Supporting People records carry coordinates; every other record has
``lat``/``lng`` None, so search those by nation, council, town or postcode
rather than by radius (``search`` does both).
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from sellbird.collectors.cqc import Location, km_between

logger = logging.getLogger(__name__)

MAX_AGE_DAYS = 35

# England
RSH_LIST_PAGE = "https://www.gov.uk/api/content/government/publications/registered-providers-of-social-housing"
RSH_STOCK_COLLECTION = ("https://www.gov.uk/api/content/government/collections/"
                        "registered-provider-social-housing-stock-and-rents-in-england")
RSH_PAGE = "https://www.gov.uk/government/publications/registered-providers-of-social-housing"
# Scotland
SHR_REGISTER = "https://www.housingregulator.gov.scot/register-of-social-landlords/"
# Wales
WG_PAGE = "https://www.gov.wales/registered-social-landlords"
# Northern Ireland
DFC_BASE = "https://www.communities-ni.gov.uk"
DFC_TOPIC = DFC_BASE + "/topics/registered-housing-associations"
NIHE_SP_LAYER = ("https://services1.arcgis.com/FGUXPreBedGXDRFM/arcgis/rest/services/"
                 "Supporting_People_Service_Directory_View/FeatureServer/0/query")
NIHE_SP_PAGE = "https://www.nihe.gov.uk/article/1639/How-to-find-support-in-your-area"

_X = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
_HONORIFIC = re.compile(r"^(mr|mrs|ms|miss|mx|dr|prof|professor|sir|dame|rev|cllr)\.?\s+", re.I)

SUPPORTED = "supported housing"
SOCIAL = "social housing"
FLOATING = "floating support"


@dataclass
class HousingLocation(Location):
    """A registered social landlord (or, in Northern Ireland, one Supporting
    People service). ``.url`` is the register's own page for it."""

    nation: str = ""            # England | Scotland | Wales | Northern Ireland
    source: str = ""            # RSH | SHR | WG | DfC | NIHE
    register_number: str = ""   # the regulator's own number
    page_url: str = ""
    supported_units: int = 0    # England: supported housing / older people units
    client_group: str = ""      # NI Supporting People: e.g. "Older People"
    senior_officer: str = ""    # "Forename Surname", honorific removed
    senior_title: str = ""      # "Senior Officer" | "Chief Executive"

    @property
    def url(self) -> str:
        return self.page_url


# ── small helpers ────────────────────────────────────────────────────────


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _website(value: str) -> str:
    value = _clean(value)
    if value in {"", "-", "*", "N/A", "n/a"}:
        return ""
    if not value.lower().startswith(("http://", "https://")):
        value = "https://" + value
    return value


def _postcode(text: str) -> str:
    matches = list(_POSTCODE.finditer(text or ""))
    if not matches:
        return ""
    m = matches[-1]
    return f"{m.group(1).upper()} {m.group(2).upper()}"


def _split_address(parts: list[str]) -> tuple[list[str], str]:
    """(the address parts with the postcode taken out, the postcode)."""
    postcode = _postcode(" ".join(parts))
    cleaned = []
    for part in parts:
        part = _clean(_POSTCODE.sub("", part)).strip(", ")
        if part:
            cleaned.append(part)
    return cleaned, postcode


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _person(value: str) -> str:
    """"Mr Barry Example" -> "Barry Example"."""
    return _HONORIFIC.sub("", _clean(value))


def _text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment or "", flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>|</p>|</dd>|</li>", "\n", fragment, flags=re.I)
    return _html.unescape(re.sub(r"<[^>]+>", " ", fragment))


def _lines(fragment: str) -> list[str]:
    return [x for x in (_clean(line) for line in _text(fragment).split("\n")) if x]


# ── reading .xlsx with the standard library ──────────────────────────────


def _col(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in letters.group(0) if letters else "":
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_sheets(path: Path) -> dict[str, str]:
    """{sheet name: its part name inside the zip}."""
    with zipfile.ZipFile(path) as z:
        book = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target", "") for r in rels}
    out = {}
    for sheet in book.iter(_X + "sheet"):
        target = targets.get(sheet.get(_R + "id"), "")
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        out[sheet.get("name")] = target
    return out


def xlsx_rows(path: Path, sheet: str):
    """Yield each row of one sheet as a list of cell strings (by column)."""
    with zipfile.ZipFile(path) as z:
        try:
            strings = ["".join(si.itertext()) for si in
                       ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_X + "si")]
        except KeyError:
            strings = []
        part = xlsx_sheets(path).get(sheet)
        if not part:
            raise KeyError(f"no sheet called {sheet!r}")
        with z.open(part) as f:
            for _, el in ET.iterparse(f):
                if el.tag != _X + "row":
                    continue
                cells: dict[int, str] = {}
                for c in el.iter(_X + "c"):
                    kind, v = c.get("t"), c.find(_X + "v")
                    if kind == "s" and v is not None:
                        value = strings[int(v.text)]
                    elif kind == "inlineStr":
                        value = "".join(c.itertext())
                    else:
                        value = v.text if v is not None and v.text else ""
                    cells[_col(c.get("r", ""))] = value
                el.clear()
                if cells:
                    yield [cells.get(i, "") for i in range(max(cells) + 1)]


def xlsx_dicts(path: Path, sheet: str, header_has: str = ""):
    """Rows as dicts keyed by the header row: the first row containing
    ``header_has`` (or the first row)."""
    header = None
    for row in xlsx_rows(path, sheet):
        if header is None:
            if not header_has or any(header_has.lower() in _clean(c).lower() for c in row):
                header = [_clean(c) for c in row]
            continue
        if any(_clean(c) for c in row):
            yield dict(zip(header, row))


# ── the local copies ─────────────────────────────────────────────────────


def _fresh(path: Path, max_age_days: int = MAX_AGE_DAYS) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400


def save_cache(locations: list[HousingLocation], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(loc) for loc in locations]), encoding="utf-8")
    tmp.replace(path)


def load_cache(path: Path) -> list[HousingLocation]:
    return [HousingLocation(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


async def _download(client, url: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with tmp.open(mode="wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)
    tmp.replace(path)
    return path


async def _get_text(client, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


async def _get_json(client, url: str, params: dict | None = None):
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


# ── England: Regulator of Social Housing ─────────────────────────────────


def _attachment(content: dict, pattern: str) -> str:
    """The first .xlsx attachment on a GOV.UK content item whose title or
    file name matches ``pattern``."""
    rx = re.compile(pattern, re.I)
    for att in (content.get("details") or {}).get("attachments") or []:
        url = att.get("url") or ""
        if url.lower().endswith(".xlsx") and (rx.search(att.get("title") or "") or rx.search(url)):
            return url
    return ""


def rsh_stock(path: Path) -> dict[str, dict]:
    """{RP code: {"supported": units, "areas": [council, ...]}} from the
    look-up tool's STOCK_BY_LA sheet."""
    out: dict[str, dict] = {}
    for row in xlsx_dicts(path, "STOCK_BY_LA", header_has="RP_Code"):
        code = _clean(row.get("RP_Code")).upper()
        if not code:
            continue
        entry = out.setdefault(code, {"supported": 0, "areas": []})
        entry["supported"] += _int(row.get("LA_SHHOP"))
        area = _clean(row.get("LA_Nm"))
        if area and _int(row.get("Total Social Stock")) > 0 and area not in entry["areas"]:
            entry["areas"].append(area)
    return out


def read_rsh(list_path: Path, stock: dict[str, dict]) -> list[HousingLocation]:
    out = []
    for row in xlsx_dicts(list_path, next(iter(xlsx_sheets(list_path))),
                          header_has="Registration number"):
        name = _clean(row.get("Organisation name"))
        number = _clean(row.get("Registration number")).upper()
        if not name or not number:
            continue
        if _clean(row.get("Designation")).lower().startswith("local authority"):
            continue
        info = stock.get(number, {})
        supported = info.get("supported", 0)
        out.append(HousingLocation(
            id=f"rsh-{number}", name=name, ownership="Organisation",
            local_authority="; ".join(info.get("areas", [])),
            services=[SUPPORTED] if supported > 0 else [SOCIAL],
            provider_id=f"rsh-{number}", provider_name=name, provider_locations=1,
            nation="England", source="RSH", register_number=number,
            page_url=RSH_PAGE, supported_units=supported,
        ))
    return out


async def load_england(client, data_dir: Path) -> list[HousingLocation]:
    data_dir.mkdir(parents=True, exist_ok=True)
    content = await _get_json(client, RSH_LIST_PAGE)
    list_url = _attachment(content, r"registered.providers")
    if not list_url:
        raise RuntimeError("GOV.UK no longer links RSH's list of registered providers")
    list_path = await _download(client, list_url, data_dir / "rsh_providers.xlsx")
    stock: dict[str, dict] = {}
    try:
        collection = await _get_json(client, RSH_STOCK_COLLECTION)
        docs = sorted(((d.get("public_updated_at") or "", d.get("base_path") or "")
                       for d in (collection.get("links") or {}).get("documents") or []),
                      reverse=True)
        for _, base_path in docs:
            if not base_path:
                continue
            doc = await _get_json(client, "https://www.gov.uk/api/content" + base_path)
            stock_url = _attachment(doc, r"look.?up")
            if stock_url:
                stock_path = await _download(client, stock_url, data_dir / "rsh_stock.xlsx")
                stock = rsh_stock(stock_path)
                break
    except Exception as e:  # the list alone is still worth having
        logger.warning("social housing: no RSH stock figures (%s); every English "
                       "provider is marked social housing", e)
    return read_rsh(list_path, stock)


# ── Scotland: Scottish Housing Regulator ─────────────────────────────────

_SHR_ITEM = re.compile(r'<div class="landlord-register__item">(.*?)</dl>', re.S)
_DT_DD = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)


def read_shr(page: str) -> list[HousingLocation]:
    out = []
    for block in _SHR_ITEM.findall(page or ""):
        h2 = re.search(r"<h2>(.*?)</h2>", block, re.S)
        name = _clean(_text(h2.group(1))) if h2 else ""
        fields = {_clean(_text(k)): (v, _clean(_text(v))) for k, v in _DT_DD.findall(block)}
        number = fields.get("RSL Registration Number", ("", ""))[1]
        if not name or not number:
            continue
        address = fields.get("Registered address", ("", ""))[1]
        town, postcode = _split_address(address.split(","))
        site_html = fields.get("Website", ("", ""))[0]
        link = re.search(r'href="(https?://[^"]+)"', site_html)
        clients = fields.get("Clients", ("", ""))[1].lower()
        officer = _person(fields.get("Senior Officer", ("", ""))[1])
        out.append(HousingLocation(
            id=f"shr-{number}", name=name, ownership="Organisation",
            website=_website(link.group(1) if link else fields.get("Website", ("", ""))[1]),
            address=", ".join(town[:-1]) if len(town) > 1 else address,
            city=town[-1] if town else "", postcode=postcode,
            services=[SUPPORTED] if clients in {"supported", "mixed"} else [SOCIAL],
            company_number=fields.get("FCA / Company Number", ("", ""))[1],
            provider_id=f"shr-{number}", provider_name=name, provider_locations=1,
            nation="Scotland", source="SHR", register_number=number,
            page_url=SHR_REGISTER, senior_officer=officer,
            senior_title="Senior Officer" if officer else "",
        ))
    return out


async def load_scotland(client, data_dir: Path) -> list[HousingLocation]:
    page = await _get_text(client, SHR_REGISTER)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "shr_register.html").write_text(page, encoding="utf-8")
    return read_shr(page)


# ── Wales: Welsh Government ──────────────────────────────────────────────

_WG_FILE = re.compile(r'href="([^"]*registered-social-landlords[^"]*\.xlsx)"', re.I)


def _wg_company_number(legal_status: str) -> str:
    """The FCA Mutuals Register number ("30776R") for a registered society,
    or the Companies House number for a company; "" for a charity number."""
    status = _clean(legal_status)
    society = re.search(r"registered society\D*(\d+\s*R)\b", status, re.I)
    if society:
        return society.group(1).replace(" ", "").upper()
    company = re.search(r"company\D*?\b([A-Z]{2}\d{6}|\d{8}|\d{6,7})\b", status, re.I)
    if company and "charity commission" not in status.lower():
        number = company.group(1).upper()
        return number.zfill(8) if number.isdigit() else number
    return ""


def read_wg(path: Path) -> list[HousingLocation]:
    out = []
    sheets = xlsx_sheets(path)
    for sheet in sheets:
        if sheet.strip().upper().startswith("REMOVED"):
            continue
        for row in xlsx_rows_with_header(path, sheet):
            name = _clean(row.get("name"))
            number = _clean(row.get("number")).upper()
            if not name or not number:
                continue
            parts, postcode = _split_address(row.get("address", []))
            out.append(HousingLocation(
                id=f"wg-{number}", name=name, ownership="Organisation",
                address=", ".join(parts[:-2]) if len(parts) > 2 else ", ".join(parts[:1]),
                city=parts[-2] if len(parts) > 1 else "",
                local_authority=parts[-1] if parts else "",
                postcode=postcode, services=[SOCIAL],
                company_number=_wg_company_number(row.get("legal", "")),
                provider_id=f"wg-{number}", provider_name=name, provider_locations=1,
                nation="Wales", source="WG", register_number=number, page_url=WG_PAGE,
            ))
    return out


def xlsx_rows_with_header(path: Path, sheet: str):
    """The Welsh register's rows: its header names vary by sheet, and the
    address runs across several unnamed columns after "Address"."""
    header: list[str] | None = None
    for row in xlsx_rows(path, sheet):
        cells = [_clean(c) for c in row]
        if header is None:
            if any(c.lower().startswith(("legal name", "name of")) for c in cells):
                header = [c.lower() for c in cells]
            continue
        if not any(cells):
            continue

        def col(*starts: str) -> int:
            for i, h in enumerate(header):
                if h.startswith(starts):
                    return i
            return -1

        i_name, i_num = col("legal name", "name of"), col("rsl registration", "registration")
        i_legal, i_addr, i_notes = col("legal status"), col("address"), col("notes")
        end = i_notes if i_notes > i_addr else len(cells)
        yield {
            "name": cells[i_name] if 0 <= i_name < len(cells) else "",
            "number": cells[i_num] if 0 <= i_num < len(cells) else "",
            "legal": cells[i_legal] if 0 <= i_legal < len(cells) else "",
            "address": cells[i_addr:end] if i_addr >= 0 else [],
        }


async def load_wales(client, data_dir: Path) -> list[HousingLocation]:
    page = await _get_text(client, WG_PAGE)
    match = _WG_FILE.search(page)
    if not match:
        raise RuntimeError("gov.wales no longer links the register of social landlords")
    url = match.group(1)
    if url.startswith("/"):
        url = "https://www.gov.wales" + url
    return read_wg(await _download(client, url, data_dir / "wg_register.xlsx"))


# ── Northern Ireland: Department for Communities ─────────────────────────

_DFC_LINK = re.compile(r'href="(/articles/[a-z0-9-]*(?:housing|homes|co-ownership)[a-z0-9-]*)"')
_DFC_SKIP = ("housing-regulation",)
_SUPPORTED_WORDS = re.compile(r"\b(supported|sheltered|supporting people|specialist)\b", re.I)


def dfc_links(topic_html: str) -> list[str]:
    seen: list[str] = []
    for path in _DFC_LINK.findall(topic_html or ""):
        if any(s in path for s in _DFC_SKIP) or path in seen:
            continue
        seen.append(path)
    return seen


def read_dfc(page: str, url: str) -> HousingLocation | None:
    """One registered housing association's DfC page, or None if it isn't one."""
    main = re.search(r"<main\b.*?</main>", page or "", re.S)
    body = main.group(0) if main else (page or "")
    text = _clean(_text(body))
    number = re.search(r"DfC Registration Number\s*-?\s*(R\s*\d+)", text)
    if not number:
        return None
    reg = number.group(1).replace(" ", "").upper()
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", body, re.S)
    name = _clean(_text(h1.group(1))) if h1 else ""
    ip = re.search(r"I&P Registration Number\s*-?\s*(IP\s*\d+)", text, re.I)
    ceo = re.search(r"<strong>([^<]{0,20}Chief Executive[^<]{0,20})</strong>\s*"
                    r"(?:<br\s*/?>)?\s*([^<]+)", body)
    lines = []
    address = re.search(r"<address\b[^>]*>(.*?)</address>", body, re.S)
    if address:
        lines = [x for x in _lines(address.group(1))
                 if not re.match(r"(tel|fax|phone|e-?mail|textphone)\b", x, re.I)]
    phone = re.search(r"Tel:?\s*([\d ]{10,16})", text)
    # The association's own site is the first link after its address.
    after = body[address.end():] if address else body
    site = ""
    for link in re.findall(r'href="(https?://[^"]+)"', after)[:1]:
        if "gov.uk" not in link:
            site = link
    lines, postcode = _split_address(lines)
    intro = text[:text.find("Registration Number")]
    officer = _person(ceo.group(2)) if ceo else ""
    return HousingLocation(
        id=f"ni-{reg}", name=name, ownership="Organisation",
        phone=_clean(phone.group(1)) if phone else "", website=_website(site),
        address=", ".join(lines[:-1]) if len(lines) > 1 else ", ".join(lines),
        city=lines[-1] if len(lines) > 1 else "", postcode=postcode,
        services=[SUPPORTED] if _SUPPORTED_WORDS.search(intro) else [SOCIAL],
        company_number=ip.group(1).replace(" ", "").upper() if ip else "",
        provider_id=f"ni-{reg}", provider_name=name, provider_locations=1,
        nation="Northern Ireland", source="DfC", register_number=reg, page_url=url,
        senior_officer=officer, senior_title=_clean(ceo.group(1)) if officer else "",
    )


async def load_ni_associations(client, data_dir: Path) -> list[HousingLocation]:
    topic = await _get_text(client, DFC_TOPIC)
    out = []
    for i, path in enumerate(dfc_links(topic)):
        if i:
            await asyncio.sleep(PAGE_DELAY)
        url = DFC_BASE + path
        try:
            loc = read_dfc(await _get_text(client, url), url)
        except Exception as e:
            logger.warning("social housing: couldn't read %s: %s", url, e)
            continue
        if loc:
            out.append(loc)
    if not out:
        raise RuntimeError("DfC's housing association pages no longer have registration numbers")
    return out


PAGE_DELAY = 1.0  # seconds between DfC pages, to be polite


# ── Northern Ireland: Housing Executive Supporting People ────────────────

SP_FIELDS = ("Provider,SID,ServiceName,Units,ThematicGroup,PrimaryClientGroup,"
             "ServiceType,TelephoneNo,Postcode,Website,LGDNAME,NIHE_Area")
SP_PAGE_SIZE = 1000


def read_sp(features: list[dict]) -> list[HousingLocation]:
    out = []
    for feat in features:
        a = feat.get("attributes") or {}
        sid = _int(a.get("SID"))
        provider = _clean(a.get("Provider"))
        if not sid or not provider:
            continue
        kind = _clean(a.get("ServiceType")).lower()
        services = [SUPPORTED]
        if any(w in kind for w in ("floating", "peripatetic", "outreach")):
            services.append(FLOATING)
        geom = feat.get("geometry") or {}
        group = " / ".join(x for x in (_clean(a.get("ThematicGroup")),
                                       _clean(a.get("PrimaryClientGroup"))) if x)
        out.append(HousingLocation(
            id=f"ni-sp-{sid}", name=_clean(a.get("ServiceName")) or provider,
            phone=_clean(a.get("TelephoneNo")), website=_website(a.get("Website")),
            postcode=_clean(a.get("Postcode")).upper(), city=_clean(a.get("NIHE_Area")),
            local_authority=_clean(a.get("LGDNAME")),
            lat=geom.get("y"), lng=geom.get("x"), services=services,
            beds=_int(a.get("Units")), ownership="Organisation",
            provider_id="ni-sp-" + re.sub(r"[^a-z0-9]+", "-", provider.lower()).strip("-"),
            provider_name=provider, provider_website=_website(a.get("Website")),
            nation="Northern Ireland", source="NIHE", register_number=str(sid),
            page_url=NIHE_SP_PAGE, client_group=group,
        ))
    per_provider: dict[str, int] = {}
    for loc in out:
        per_provider[loc.provider_id] = per_provider.get(loc.provider_id, 0) + 1
    for loc in out:
        loc.provider_locations = per_provider[loc.provider_id]
    return out


async def load_ni_supporting_people(client, data_dir: Path) -> list[HousingLocation]:
    features: list[dict] = []
    offset = 0
    while True:
        page = await _get_json(client, NIHE_SP_LAYER, params={
            "where": "1=1", "outFields": SP_FIELDS, "outSR": "4326", "f": "json",
            "orderByFields": "OBJECTID_12", "resultOffset": str(offset),
            "resultRecordCount": str(SP_PAGE_SIZE)})
        if "error" in page:
            raise RuntimeError(f"ArcGIS said: {page['error'].get('message')}")
        batch = page.get("features") or []
        features.extend(batch)
        if not batch or not page.get("exceededTransferLimit"):
            break
        offset += len(batch)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "nihe_sp.json").write_text(json.dumps(features), encoding="utf-8")
    return read_sp(features)


# ── all of them ──────────────────────────────────────────────────────────

# (cache key, loader). Each source is cached and refreshed on its own, so one
# regulator's site being down never costs the others.
SOURCES = (
    ("rsh", load_england),
    ("shr", load_scotland),
    ("wg", load_wales),
    ("dfc", load_ni_associations),
    ("nihe_sp", load_ni_supporting_people),
)


async def load_source(client, data_dir: Path, key: str, loader) -> list[HousingLocation]:
    """One source, from its local copy when fresh, else downloaded; an old
    copy is used (with a warning) when the download fails."""
    cache = data_dir / f"{key}.json"
    if _fresh(cache):
        return load_cache(cache)
    try:
        locations = await loader(client, data_dir)
        if not locations:
            raise RuntimeError("the register came back empty")
    except Exception as e:
        if cache.exists():
            logger.warning("social housing: couldn't refresh %s (%s); using the copy "
                           "from before", key, e)
            return load_cache(cache)
        raise RuntimeError(f"Couldn't download the {key} register: {e}") from e
    save_cache(locations, cache)
    logger.info("social housing: %s has %d records", key, len(locations))
    return locations


async def load_register(client, data_dir: Path) -> list[HousingLocation]:
    """Every source's records together. A source that fails with no copy at
    all is skipped with a warning; RuntimeError only when every one fails."""
    out: list[HousingLocation] = []
    errors = []
    for key, loader in SOURCES:
        try:
            out.extend(await load_source(client, data_dir, key, loader))
        except Exception as e:
            logger.warning("social housing: %s", e)
            errors.append(str(e))
    if not out and errors:
        raise RuntimeError("; ".join(errors))
    return out


# ── searching it ─────────────────────────────────────────────────────────

# A search phrase -> the services that answer it. First match wins.
TERM_SERVICES = (
    ("floating", {FLOATING}),
    ("housing related support", {SUPPORTED, FLOATING}),
    ("housing-related support", {SUPPORTED, FLOATING}),
    ("supporting people", {SUPPORTED, FLOATING}),
    ("supported", {SUPPORTED}),
    ("exempt", {SUPPORTED}),
    ("sheltered", {SUPPORTED}),
    ("housing association", {SUPPORTED, SOCIAL}),
    ("social housing", {SUPPORTED, SOCIAL}),
    ("social landlord", {SUPPORTED, SOCIAL}),
    ("registered provider", {SUPPORTED, SOCIAL}),
)

NATIONS = ("England", "Scotland", "Wales", "Northern Ireland")


def services_for(term: str) -> set[str]:
    low = (term or "").lower()
    for needle, services in TERM_SERVICES:
        if needle in low:
            return services
    return set()


def _place_matches(loc: HousingLocation, place: str) -> bool:
    """A nation, a council, a town or a postcode area ("BT48", "EH")."""
    first = (place or "").split(",")[0].strip().lower()
    if not first:
        return True
    if first in {n.lower() for n in NATIONS} or first in {"uk", "united kingdom"}:
        return first in {"uk", "united kingdom"} or loc.nation.lower() == first
    if re.fullmatch(r"[a-z]{1,2}\d[a-z\d]?|[a-z]{1,2}", first):
        area = loc.postcode.lower().split(" ")[0]
        return bool(area) and (area == first or (first.isalpha() and
                                                 re.match(first + r"\d", area) is not None))
    return (first in loc.city.lower() or first in loc.local_authority.lower()
            or first in loc.address.lower())


def search(locations: list[HousingLocation], term: str, place: str = "",
           coordinate: str = "") -> list[HousingLocation]:
    """The records matching a phrase near a place, nearest first.

    ``coordinate`` ("lat,lng,radius_km") only finds records with coordinates
    (NI Supporting People services); everything else needs ``place``.
    """
    wanted = services_for(term)
    if not wanted:
        return []
    lat = lng = radius = None
    if coordinate:
        parts = (coordinate.split(",") + ["", "", ""])[:3]
        try:
            lat, lng = float(parts[0]), float(parts[1])
            radius = float(parts[2]) if parts[2] else 25.0
        except ValueError:
            lat = lng = None
    hits: list[tuple[float, HousingLocation]] = []
    for loc in locations:
        if not wanted.intersection(loc.services):
            continue
        if lat is not None and lng is not None:
            if loc.lat is None or loc.lng is None:
                continue
            d = km_between(lat, lng, loc.lat, loc.lng)
            if d <= radius:
                hits.append((d, loc))
        elif _place_matches(loc, place):
            hits.append((0.0, loc))
    hits.sort(key=lambda h: h[0])
    return [loc for _, loc in hits]


def registry_people(loc: HousingLocation) -> list[dict]:
    """The named person the register holds: Scotland's Senior Officer or a
    Northern Ireland housing association's Chief Executive."""
    name = _clean(loc.senior_officer)
    if " " not in name:
        return []
    first, _, last = name.partition(" ")
    last = last.split()[-1] if last.split() else ""
    if not first or not last:
        return []
    return [{"first_name": first, "last_name": last, "title": loc.senior_title,
             "source": loc.source, "evidence_url": loc.url}]
