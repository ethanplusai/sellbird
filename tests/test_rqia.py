"""RQIA register (Northern Ireland) and its inspection reports.

Every business and person here is invented. The spreadsheets, sitemap, report
page and PDF are built by hand in the shapes OpenDataNI and RQIA publish,
not copied.
"""

import json
import zipfile
import zlib
from io import BytesIO
from xml.sax.saxutils import escape

import httpx
import pytest

from sellbird.collectors import cqc, rqia

# ── fixtures: the three spreadsheets ──

ODNI_HEADER = ["Service Name", "Address Line1", "Address Line2", "Town", "Postcode", "Phone",
               "Service Type", "Max Approved Places", "Categories of Care",
               "Conditions (As per Certificate)", "Current Manager", "Provider", "Last Inspected"]
RQIA_HEADER = ["Service ID", "Service", "Address Line1", "Address Line2", "Town", "Postcode",
               "Phone", "", "Category", "Max Approved Places", "Categories Of Care",
               "Categories Definition", "Conditions", "Responsible Person", "Manager", "Provider"]
INSP_HEADER = ["(Do Not Modify) Inspection", "Service", "Specialism", "Address 1: Postcode",
               "Completed", "Service Type (Service) ", "HSC Trust (Service)", "Provider (Service) "]


def odni(name, typ, town="Exampletown", postcode="BT0 0AA", sid="", **kw):
    # OpenDataNI writes the RQIA ID after the name, after a no-break space.
    row = {"Service Name": f"{name} ({sid})" if sid else name, "Address Line1": "1 Example Road", "Town": town,
           "Postcode": postcode, "Phone": "2800000001", "Service Type": typ,
           "Current Manager": "Ms Jane Example", "Provider": "Example Care Ltd"}
    row.update(kw)
    return [row.get(h, "") for h in ODNI_HEADER]


def reg(sid, name, typ, postcode="BT0 0AA", **kw):
    row = {"Service ID": sid, "Service": name, "Postcode": postcode, "Category": typ,
           "Town": "Exampletown", "Responsible Person": "Mr Sam Sample",
           "Manager": "Ms Jane Example", "Provider": "Example Care Ltd"}
    row.update(kw)
    return [row.get(h, "") for h in RQIA_HEADER]


ODNI_ROWS = [
    odni("Willow Lodge", "Residential (RC)", sid="011001", **{"Max Approved Places": "30",
         "Address Line2": "Upper Example", "Current Manager": "Mrs Helen Jane Marsh"}),
    odni("Brook Nursing Home", "Nursing (NH)", town="Sampleford", postcode="BT0 0AB", sid="11002",
         **{"Max Approved Places": "45", "Provider": "Northern HSC Trust",
            "Current Manager": "Mr Arun Patel - not registered"}),
    odni("Hill View Home Care", "Domiciliary Care Agency (DCA)", postcode="BT0 0AC", sid="11003",
         **{"Provider": "Mrs Ann Example & Mr Ben Example"}),
    odni("Meadow Supported Living Service", "Domiciliary Care Agency (DCA)", postcode="BT0 0AD",
         sid="11004",
         **{"Provider": "Mr Carl Sample", "Current Manager": "Registration pending"}),
    # The rows below carry no ID (OpenDataNI always writes one today; these
    # check the fallbacks). Moved since RQIA's own file was made: matched on
    # its name alone.
    odni("River House", "Residential (RC)", postcode="BT0 0ZZ"),
    # Registered since RQIA's file was made: its ID comes from its report's name.
    odni("New Dawn Care", "Domiciliary Care Agency (DCA)", postcode="BT0 0AF"),
    # Neither file nor a report knows it yet.
    odni("Fresh Start Care", "Domiciliary Care Agency (DCA)", postcode="BT0 0AG"),
    # Not adult social care that Sellbird sells to: never read.
    odni("Smile Dental", "Independent Hospital (IH) - Dental Treatment", postcode="BT0 0AH"),
    odni("Sunny Day Centre", "Day Care Setting (DCS)", postcode="BT0 0AJ"),
]
RQIA_ROWS = [
    reg("011001", "Willow Lodge", "Residential (RC)", **{"Responsible Person": "Mr Sam Sample"}),
    reg("11002", "Brook Nursing Home", "Nursing (NH)", postcode="BT0 0AB",
        **{"Responsible Person": "Ms Rita Example"}),
    reg("11003", "Hill View Home Care", "Domiciliary Care Agency (DCA)", postcode="BT0 0AC"),
    reg("11004", "Meadow Supported Living Service", "Domiciliary Care Agency (DCA)",
        postcode="BT0 0AD"),
    reg("11005", "River House", "Residential (RC)", postcode="BT0 0AE"),
]
INSP_ROWS = [
    ["x", "Willow Lodge (011001)", "Care", "BT0 0AA", "01/10/2026", "Residential (RC)", "BHSCT", "p"],
    ["x", "Brook Nursing Home", "Care", "BT0 0AB", "01/09/2026", "Nursing (NH)", "NHSCT", "p"],
]


def make_xlsx(header, rows, title=None, shared=True) -> bytes:
    """A minimal .xlsx: shared strings (OpenDataNI) or inline strings (RQIA)."""
    strings: list[str] = []

    def cell(ref, value):
        if not value:
            return ""
        if shared:
            if value not in strings:
                strings.append(value)
            return f'<c r="{ref}" t="s"><v>{strings.index(value)}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'

    def row(n, values):
        cells = "".join(cell(f"{chr(65 + i)}{n}", v) for i, v in enumerate(values))
        return f'<row r="{n}">{cells}</row>'

    all_rows = ([[title], []] if title else []) + [header] + rows
    sheet = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns='
             '"http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + "".join(row(i + 1, r) for i, r in enumerate(all_rows) if r)
             + "</sheetData></worksheet>")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml",
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/data.xml" Type="worksheet"/>'
                   '</Relationships>')
        z.writestr("xl/worksheets/data.xml", sheet)
        if shared:
            z.writestr("xl/sharedStrings.xml",
                       '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")
    return buf.getvalue()


def report_link(sid, slug, date):
    return f"<url><loc>https://www.rqia.org.uk/wpfd_file/{sid}_{slug}_care_{date}/</loc></url>"


SITEMAP_PART = "<urlset>" + "".join([
    report_link("011001", "willow-lodge", "3-march-2025"),
    report_link("011001", "willow-lodge", "16-september-2026"),
    report_link("11001", "willow-lodge", "2-may-2024"),
    "<url><loc>https://www.rqia.org.uk/wpfd_file/011001_willowlodge_pharmacy_1-october-2026/</loc></url>",
    report_link("11002", "brook-nursing-home", "1-september-2026"),
    report_link("021009", "new-dawn-care", "5-october-2026"),
    "<url><loc>https://www.rqia.org.uk/wpfd_file/guidance-for-providers-2026/</loc></url>",
]) + "</urlset>"
SITEMAP_ROOT = ("<sitemapindex><sitemap><loc>https://www.rqia.org.uk/wp-sitemap-posts-page-1.xml</loc>"
                "</sitemap><sitemap><loc>https://www.rqia.org.uk/wp-sitemap-posts-wpfd_file-1.xml"
                "</loc></sitemap></sitemapindex>")


@pytest.fixture
def index():
    return rqia.parse_report_index([SITEMAP_PART])


@pytest.fixture
def locations(index):
    rows = lambda header, data, **kw: list(rqia.xlsx_rows(make_xlsx(header, data, **kw), header[0]))
    return rqia.build_register(
        rows(ODNI_HEADER, ODNI_ROWS),
        rows(RQIA_HEADER, RQIA_ROWS, title="Current Register (exc Childrens)", shared=False),
        list(rqia.xlsx_rows(make_xlsx(INSP_HEADER, INSP_ROWS), "Service")),
        index)


def by_name(locations, name):
    return next(loc for loc in locations if loc.name == name)


# ── reading the register ──

def test_xlsx_rows_skip_the_title_line_and_read_inline_strings():
    data = make_xlsx(RQIA_HEADER, RQIA_ROWS[:1], title="Current Register", shared=False)
    rows = list(rqia.xlsx_rows(data, "Service ID"))
    assert len(rows) == 1
    assert rows[0]["Service ID"] == "011001" and rows[0]["Service"] == "Willow Lodge"
    assert "" not in rows[0]                      # the blank header column is dropped


def test_reads_only_adult_care_services(locations):
    assert [loc.name for loc in locations] == [
        "Willow Lodge", "Brook Nursing Home", "Hill View Home Care",
        "Meadow Supported Living Service", "River House", "New Dawn Care", "Fresh Start Care"]


def test_location_fields(locations):
    willow = by_name(locations, "Willow Lodge")      # the "(011001)" is taken off the name
    assert isinstance(willow, cqc.Location)
    assert willow.id == "011001"
    assert willow.services == ["care home"]
    assert willow.beds == 30
    assert willow.address == "1 Example Road, Upper Example"
    assert (willow.city, willow.postcode) == ("Exampletown", "BT0 0AA")
    assert willow.phone == "02800000001"           # the dropped leading zero is back
    assert willow.local_authority == "Belfast HSC Trust"
    assert willow.lat is None and willow.lng is None
    assert willow.rating == "" and willow.website == "" and willow.company_number == ""
    assert willow.manager == "Marsh, Helen Jane"
    assert willow.nominated_individual == "Sample, Sam"
    assert willow.ownership == "Organisation"
    assert willow.provider_name == "Example Care Ltd"
    assert willow.provider_locations == 4           # every service run by Example Care Ltd
    # The newest care report, not the older one or the pharmacy inspection.
    assert willow.url == "https://www.rqia.org.uk/wpfd_file/011001_willow-lodge_care_16-september-2026/"


def test_service_types_and_ownership(locations):
    brook = by_name(locations, "Brook Nursing Home")
    assert brook.services == ["nursing home"] and brook.beds == 45
    assert brook.ownership == "NHS Body"
    assert brook.local_authority == "Northern HSC Trust"
    assert brook.manager == "Patel, Arun"           # the "- not registered" note is dropped
    hill = by_name(locations, "Hill View Home Care")
    assert hill.services == ["home care"] and hill.beds == 0
    assert hill.ownership == "Partnership"
    meadow = by_name(locations, "Meadow Supported Living Service")
    assert meadow.services == ["supported living"]
    assert meadow.ownership == "Individual"
    assert meadow.manager == ""


def test_ids_from_name_then_report_then_made_up(locations):
    assert by_name(locations, "River House").id == "11005"
    assert by_name(locations, "New Dawn Care").id == "021009"
    fresh = by_name(locations, "Fresh Start Care")
    assert fresh.id.startswith("NI-")
    assert fresh.url == rqia.REGISTER_PAGE


def test_rqia_file_alone_still_makes_a_register(index):
    rows = list(rqia.xlsx_rows(make_xlsx(RQIA_HEADER, RQIA_ROWS, shared=False), "Service ID"))
    locations = rqia.build_register([], rows, [], index)
    assert [loc.id for loc in locations] == ["011001", "11002", "11003", "11004", "11005"]
    assert by_name(locations, "Willow Lodge").manager == "Example, Jane"


@pytest.mark.parametrize("raw, expected", [
    ("Mrs Helen Jane Marsh", "Marsh, Helen Jane"),
    ("Ms Ann O'Example", "O'Example, Ann"),
    ("Dr Kim Sample (Acting)", "Sample, Kim"),
    ("Mr Lee Example – not registered", "Example, Lee"),
    ("Mr Lee Example / Ms Ann Sample", "Example, Lee; Sample, Ann"),
    ("Registration pending", ""),
    ("Vacant", ""),
    ("", ""),
])
def test_registry_name(raw, expected):
    assert rqia.registry_name(raw) == expected


@pytest.mark.parametrize("provider, expected", [
    ("Example Care Ltd", "Organisation"),
    ("Western HSC Trust", "NHS Body"),
    ("Mr Carl Sample", "Individual"),
    ("Mrs Ann Example & Mr Ben Example", "Partnership"),
    ("Mr Ben Example and Mr Carl Sample", "Partnership"),
    ("Example Healthcare", "Organisation"),
])
def test_ownership_of(provider, expected):
    assert rqia.ownership_of(provider) == expected
    assert (expected in rqia.CONSENT_NEEDED) == (expected in {"Individual", "Partnership"})


def test_cache_round_trip(tmp_path, locations):
    path = tmp_path / "register.json"
    rqia.save_cache(locations, path)
    loaded = rqia.load_cache(path)
    assert loaded == locations
    assert loaded[0].url == locations[0].url


def test_search_by_town_and_trust_but_not_by_radius(locations):
    assert [l.name for l in rqia.search(locations, "care home", "Exampletown")] == [
        "Willow Lodge", "River House"]
    assert [l.name for l in rqia.search(locations, "nursing home", "Northern")] == [
        "Brook Nursing Home"]
    assert [l.name for l in rqia.search(locations, "supported living")] == [
        "Meadow Supported Living Service"]
    # No coordinates in RQIA's files, so a radius search finds nothing.
    assert rqia.search(locations, "care home", coordinate="54.6,-5.9,50") == []


def test_registry_people_use_rqia_titles(locations):
    people = rqia.registry_people(by_name(locations, "Willow Lodge"))
    assert [(p["first_name"], p["last_name"], p["title"], p["source"]) for p in people] == [
        ("Helen", "Marsh", "Registered Manager", "RQIA"),
        ("Sam", "Sample", "Responsible Individual", "RQIA")]
    assert people[0]["evidence_url"].endswith("_care_16-september-2026/")


def test_report_index_keeps_the_latest_care_report(index):
    assert set(index) == {"11001", "11002", "21009"}
    assert index["11001"]["date"] == "2026-09-16"
    assert index["21009"]["name"] == "new dawn care"


# ── downloading ──

ODNI_FILE = "https://admin.opendatani.gov.uk/dataset/abc/resource/def/download/rqia_register_01112026.xlsx"
RQIA_FILE = "https://www.rqia.org.uk/download/32/services-registered-with-rqia/4452/all-registered-services.xlsx"
INSP_FILE = ("https://www.rqia.org.uk/download/4/recently-completed-inspection-reports/1003/"
             "all-rqia-inspections_2024-25-and-2025-26.xlsx")


def register_handler(calls, odni_ok=True):
    def handler(request):
        url = str(request.url)
        calls.append(url)
        if url == rqia.ODNI_PAGE:
            if not odni_ok:
                return httpx.Response(503)
            return httpx.Response(200, text=f'<a href="{ODNI_FILE}">XLSX</a>')
        if url == "https://admin.opendatani.gov.uk/dataset/abc/resource/def/download/r2":
            return httpx.Response(200, content=make_xlsx(ODNI_HEADER, ODNI_ROWS))
        if url == ODNI_FILE:
            # OpenDataNI redirects to its file store.
            return httpx.Response(302, headers={"Location": url.rsplit("/", 1)[0] + "/r2"})
        if url == rqia.REGISTER_PAGE:
            return httpx.Response(200, text=f'<a class="wpfd_downloadlink" href="{RQIA_FILE}">x</a>')
        if url == RQIA_FILE:
            return httpx.Response(200, content=make_xlsx(RQIA_HEADER, RQIA_ROWS, title="t", shared=False))
        if url == rqia.INSPECTIONS_PAGE:
            return httpx.Response(200, text=f'<a href="{INSP_FILE}">x</a>')
        if url == INSP_FILE:
            return httpx.Response(200, content=make_xlsx(INSP_HEADER, INSP_ROWS))
        if url == rqia.SITEMAP:
            return httpx.Response(200, text=SITEMAP_ROOT)
        if url == "https://www.rqia.org.uk/wp-sitemap-posts-wpfd_file-1.xml":
            return httpx.Response(200, text=SITEMAP_PART)
        return httpx.Response(404)
    return handler


@pytest.mark.asyncio
async def test_load_register_downloads_then_uses_the_local_copy(tmp_path):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(register_handler(calls))) as client:
        first = await rqia.load_register(client, tmp_path)
        assert len(first) == 7
        assert by_name(first, "Willow Lodge").local_authority == "Belfast HSC Trust"
        assert by_name(first, "Willow Lodge").url.endswith("_care_16-september-2026/")
        n = len(calls)
        again = await rqia.load_register(client, tmp_path)
    assert again == first
    assert len(calls) == n                           # read from data_dir, nothing fetched
    assert (tmp_path / "register.json").exists() and (tmp_path / "report_index.json").exists()


@pytest.mark.asyncio
async def test_load_register_falls_back_to_rqias_own_file(tmp_path):
    calls = []
    handler = register_handler(calls, odni_ok=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        locations = await rqia.load_register(client, tmp_path)
    assert [loc.id for loc in locations] == ["011001", "11002", "11003", "11004", "11005"]


@pytest.mark.asyncio
async def test_load_register_says_why_when_nothing_downloads(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
        with pytest.raises(RuntimeError, match="RQIA register"):
            await rqia.load_register(client, tmp_path)


# ── the report ──

REPORT_LINES = [
    "Name of Service: Willow Lodge",
    "Provider: Example Care Ltd",
    "An unannounced inspection took place on 16 September 2026 between 9.30 a.m. and 3.00 p.m. The",
    "inspection was undertaken to determine if the home is delivering safe, effective and",
    "compassionate care and if the service is well led.",
    "The inspection established that residents were well cared for. However, improvements were",
    "required in medicines management. Refer to Section 3.2 for more details.",
    "Full details, including areas for improvement identified, can be found in Section 4.",
    "Quality Improvement Plan",
    "Action required to ensure compliance with The Residential Care Homes Regulations",
    "Area for improvement 1",
    "Ref: Regulation 13 (4)",
    "Stated: Second time",
    "To be completed by:",
    "16 October 2026",
    "The registered person shall ensure that medicines are administered as prescribed.",
    "Ref: 3.3.1",
    "Response by registered person detailing the actions taken:",
    "Training has been booked for all staff.",
    "RQIA ID: 011001 Inspection ID: IN000001",
    "Area for improvement 2",
    "Ref: Standard 23.3",
    "Stated: First time",
    "The registered person shall ensure that staff complete dementia awareness training.",
    "Response by registered person detailing the actions taken:",
    "Done.",
]


def pdf_string(text: str) -> bytes:
    raw = text.encode("cp1252")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def make_pdf(lines=REPORT_LINES) -> bytes:
    """A Word-like PDF: Flate content, one Tm per line, the font object inside
    an object stream, and one word drawn in two pieces on the same line."""
    ops = [b"BT /F1 11 Tf"]
    y = 800
    for line in lines:
        if line.startswith("Training"):
            # "Training" drawn as "Train" + "ing" on the same line, the rest with
            # a kerning gap instead of a space.
            ops.append(f"1 0 0 1 50 {y} Tm".encode() + b" [(Train)] TJ")
            ops.append(f"1 0 0 1 80 {y} Tm".encode() + b" [(ing) -300 "
                       + pdf_string(line[len("Training "):]) + b"] TJ")
        else:
            ops.append(f"1 0 0 1 50 {y} Tm ".encode() + pdf_string(line) + b" Tj")
        y -= 14
    ops.append(b"ET")
    content = zlib.compress(b"\n".join(ops))
    font = b"<</Type/Font/Subtype/TrueType/BaseFont/Arial/Encoding/WinAnsiEncoding>>"
    header = b"5 0 "
    objstm = header + font
    objs = [
        b"1 0 obj\n<</Type/Catalog/Pages 2 0 R>>\nendobj\n",
        b"2 0 obj\n<</Type/Pages/Kids[3 0 R]/Count 1>>\nendobj\n",
        b"3 0 obj\n<</Type/Page/Parent 2 0 R/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>\nendobj\n",
        b"4 0 obj\n<</Filter/FlateDecode/Length %d>>\nstream\n" % len(content) + content
        + b"\nendstream\nendobj\n",
        b"6 0 obj\n<</Type/ObjStm/N 1/First %d/Filter/FlateDecode>>\nstream\n" % len(header)
        + zlib.compress(objstm) + b"\nendstream\nendobj\n",
    ]
    return b"%PDF-1.7\n" + b"".join(objs) + b"trailer\n<</Root 1 0 R>>\n%%EOF\n"


def test_pdf_text_reads_a_word_style_pdf():
    text = rqia.pdf_text(make_pdf())
    lines = text.splitlines()
    assert lines[0] == "Name of Service: Willow Lodge"
    assert "Training has been booked for all staff." in lines
    assert "Quality Improvement Plan" in lines


def test_pdf_text_maps_two_byte_fonts_through_tounicode():
    cmap = (b"beginbfchar <0003> <0020> endbfchar beginbfrange <0010> <0012> <0041> endbfrange")
    fonts = {b"F2": (rqia._cmap(cmap), True)}
    assert rqia.content_text(b"BT /F2 12 Tf <0010001100030012> Tj ET", fonts) == "AB C"


def test_pdf_text_gives_up_quietly_on_what_it_cannot_read():
    assert rqia.pdf_text(b"not a pdf") == ""


def test_report_summary_lists_areas_then_findings():
    text = rqia.pdf_text(make_pdf())
    areas = rqia.areas_for_improvement(text)
    assert areas == [
        "The registered person shall ensure that medicines are administered as prescribed. "
        "[Regulation 13 (4), stated second time]",
        "The registered person shall ensure that staff complete dementia awareness training. "
        "[Standard 23.3]"]
    summary = rqia.report_summary_from(text)
    assert summary.startswith("Areas for improvement: (1) The registered person shall ensure that medicines")
    assert "Findings: The inspection established that residents were well cared for." in summary
    assert "Refer to Section" not in summary and "Training has been booked" not in summary


def test_report_summary_without_areas():
    text = "\n".join(REPORT_LINES[:8] + ["No areas for improvement were identified."])
    summary = rqia.report_summary_from(text)
    assert summary.startswith("No areas for improvement were identified. Findings: The inspection")


PAGE = "https://www.rqia.org.uk/wpfd_file/011001_willow-lodge_care_16-september-2026/"
PDF = ("https://www.rqia.org.uk/download/31/care-home-inspection-reports/9001/"
       "011001_willow-lodge_care_16-september-2026.pdf")


def report_client(calls, broken=False):
    def handler(request):
        url = str(request.url)
        calls.append(url)
        if broken:
            return httpx.Response(500)
        if url == rqia.SITEMAP:
            return httpx.Response(200, text=SITEMAP_ROOT)
        if url.endswith("wpfd_file-1.xml"):
            return httpx.Response(200, text=SITEMAP_PART)
        if url == PAGE:
            return httpx.Response(200, text=f'<a class="wpfd_downloadlink" href="{PDF}">Download</a>')
        if url == PDF:
            return httpx.Response(200, content=make_pdf())
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_latest_report_reads_the_newest_care_report(tmp_path):
    calls = []
    async with report_client(calls) as client:
        report = await rqia.latest_report(client, "011001", tmp_path)
    assert report["ratings"] == {}
    assert report["url"] == PDF
    assert report["published"] == "16 September 2026"
    assert report["summary"].startswith("Areas for improvement: (1)")
    assert len(report["areas"]) == 2


@pytest.mark.asyncio
async def test_latest_report_is_cached_and_quiet_on_failure(tmp_path):
    calls = []
    async with report_client(calls) as client:
        first = await rqia.latest_report(client, "11001", tmp_path)
        n = len(calls)
        assert await rqia.latest_report(client, "011001", tmp_path) == first
        assert len(calls) == n                       # leading zeros are the same service
        assert await rqia.latest_report(client, "99999", tmp_path) == {}   # no report
        assert await rqia.latest_report(client, "NI-0123abcd", tmp_path) == {}
    cached = json.loads((tmp_path / "reports.json").read_text(encoding="utf-8"))
    assert set(cached) == {"11001", "99999"}
    async with report_client([], broken=True) as client:
        assert await rqia.latest_report(client, "11002", tmp_path / "other") == {}


# ── wiring: discovery and the writer ──

@pytest.mark.asyncio
async def test_discovery_finds_rqia_services_by_trust_and_nation(locations):
    from sellbird.collectors.discover import PROVIDERS, DiscoveryQuery
    provider = type(PROVIDERS["rqia"])()
    provider._locations = locations
    found = await provider.fetch(None, {}, DiscoveryQuery("care home", "Belfast HSC Trust"))
    assert [b.name for b in found] == ["Willow Lodge"]
    willow = found[0]
    assert willow.external_id == "rqia:011001"
    assert willow.source_url == PAGE                # the latest report's page
    assert willow.has_website is False and willow.domain == ""
    assert willow.address == "1 Example Road, Upper Example, BT0 0AA"
    assert {p["title"] for p in willow.people} >= {"Registered Manager"}
    everywhere = await provider.fetch(None, {}, DiscoveryQuery("care home", "Northern Ireland"))
    assert "Willow Lodge" in [b.name for b in everywhere]
    # A sole trader's supported living service needs consent first (PECR).
    alone = await provider.fetch(None, {}, DiscoveryQuery("supported living", "Northern Ireland"))
    assert "Meadow Supported Living Service" not in [b.name for b in alone]


@pytest.mark.asyncio
async def test_writer_uses_the_rqia_report(monkeypatch):
    from types import SimpleNamespace

    from sellbird.agents.writer import Writer

    async def fake_report(client, service_id, data_dir):
        assert service_id == "011001" and data_dir.name == "rqia"
        return {"ratings": {}, "summary": "Areas for improvement: (1) Care plans.",
                "url": PDF, "published": "16 September 2026", "areas": ["Care plans."]}

    monkeypatch.setattr(rqia, "latest_report", fake_report)
    facts = await Writer._inspection_facts(object.__new__(Writer),
                                           SimpleNamespace(external_id="rqia:011001"))
    assert facts[0] == "- Regulator: RQIA (Northern Ireland)"
    assert len(facts) == 2                           # no ratings line: RQIA doesn't rate
    assert "inspected 16 September 2026" in facts[1]
    assert facts[1].endswith("Areas for improvement: (1) Care plans.")
