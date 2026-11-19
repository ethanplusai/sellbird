"""CIW register and inspection reports.

Every service, provider and person here is invented. The CSV is built by
hand in the shape CIW's data extract uses, and the report PDF is written
here byte by byte, not copied.
"""

import csv
import io
import os
import time
import zlib

import httpx
import pytest

from harvey.collectors import ciw, cqc

HEADER = [
    "Service URN", "Service name", "Known as", "Service type", "Service sub-type",
    "Care and support for", "Service address line 1", "Service address line 2",
    "Service address line 3", "Service town/city", "Service postcode",
    "Local authority", "Domiciliary care area", "Primary telephone number",
    "Primary email address", "Website", "Maximum no. of places", "Service ratings",
    "Responsible Individual names", "Registered Person names", "Provider URN",
    "Provider name", "Provider type", "Company number", "Charity number",
    "Provider website", "Last updated on",
]

ROWS = [
    {"Service URN": "SIN-001", "Service name": "Willow Lodge", "Service type": "Care Home Service",
     "Service sub-type": "Adults Without Nursing", "Care and support for": "People living with dementia",
     "Service address line 1": "1 Elm Road", "Service town/city": "Exampleton",
     "Service postcode": "EX1 1AA", "Local authority": "Exampleshire",
     "Primary telephone number": "01000 000001", "Primary email address": "hello@willow.example",
     "Website": "www.willow.example", "Maximum no. of places": "32",
     "Service ratings": "Well-being: Good; Care and Support: Good; "
                       "Environment: Requires Improvement; Leadership and Management: Good",
     "Responsible Individual names": "Helen Jane Marsh", "Provider URN": "PROV-1",
     "Provider name": "Willow Care Ltd", "Provider type": "Limited Company",
     "Company number": "01234567", "Provider website": "willowcare.example"},
    {"Service URN": "SIN-002", "Service name": "Brook Home Care",
     "Service type": "Domiciliary Support Service", "Service sub-type": "None",
     "Care and support for": "Adults, Supported living", "Service town/city": "Exampleton",
     "Domiciliary care area": "Example Vale",
     "Service ratings": "Well-being: Excellent; Care and Support: Excellent; "
                       "Leadership and Management: Excellent",
     "Responsible Individual names": "Grace Okafor", "Provider URN": "PROV-1",
     "Provider name": "Willow Care Ltd", "Provider type": "Limited Company"},
    {"Service URN": "SIN-003", "Service name": "Hill View Nursing", "Service type": "Care Home Service",
     "Service sub-type": "Adults With Nursing", "Service town/city": "Otherby",
     "Local authority": "Otherfordshire", "Provider URN": "PROV-2",
     "Provider name": "Hill Partners", "Provider type": "Partnership"},
    # A sole trader: CIW leaves the provider type blank.
    {"Service URN": "SIN-004", "Service name": "Rose Cottage Care", "Service type": "Care Home Service",
     "Service sub-type": "Adults Without Nursing", "Service town/city": "Otherby",
     "Provider URN": "PROV-3", "Provider name": "Alex Sample"},
    # Not adult care: a children's home, a childminder and children-only home care.
    {"Service URN": "SIN-005", "Service name": "Little Acorns", "Service type": "Care Home Service",
     "Service sub-type": "Childrens Home", "Provider URN": "PROV-4", "Provider type": "Limited Company"},
    {"Service URN": "SIN-006", "Service name": "Busy Bees", "Service type": "Child Minder"},
    {"Service URN": "SIN-007", "Service name": "Kids At Home", "Service type": "Domiciliary Support Service",
     "Care and support for": "Children", "Provider URN": "PROV-4", "Provider type": "Limited Company"},
]


def _csv(rows=ROWS) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER, quoting=csv.QUOTE_ALL)
    w.writeheader()
    for row in rows:
        w.writerow({h: row.get(h, "") for h in HEADER})
    return "﻿" + buf.getvalue()


@pytest.fixture
def parsed():
    return ciw.read_extract(_csv())


@pytest.fixture
def locations(parsed):
    return {loc.id: loc for loc in parsed[0]}


# ── the register ─────────────────────────────────────────────────────────


def test_reads_only_adult_care_services(locations):
    assert set(locations) == {"SIN-001", "SIN-002", "SIN-003", "SIN-004"}


def test_location_fields(locations):
    loc = locations["SIN-001"]
    assert loc.name == "Willow Lodge"
    assert loc.website == "https://www.willow.example"
    assert loc.provider_website == "https://willowcare.example"
    assert loc.address == "1 Elm Road" and loc.postcode == "EX1 1AA"
    assert loc.city == "Exampleton" and loc.local_authority == "Exampleshire"
    assert loc.lat is None and loc.lng is None
    assert loc.services == ["care home"] and loc.beds == 32
    assert loc.company_number == "01234567" and loc.ownership == "Organisation"
    assert loc.nominated_individual == "Marsh, Helen Jane"
    assert loc.provider_locations == 2
    assert loc.url == "https://digital.careinspectorate.wales/directory/service/SIN-001"
    assert loc.rating == "Requires Improvement (Environment); others Good"


def test_home_care_and_supported_living(locations):
    loc = locations["SIN-002"]
    assert loc.services == ["home care", "supported living"]
    assert loc.local_authority == "Example Vale"     # its region
    assert loc.beds == 0 and loc.rating == "Excellent"
    assert locations["SIN-003"].services == ["nursing home"]


def test_sole_traders_and_partnerships_need_consent(locations):
    assert locations["SIN-003"].ownership == "Partnership"
    assert locations["SIN-004"].ownership == "Individual"
    assert {loc.ownership for loc in locations.values()} & cqc.CONSENT_NEEDED == {
        "Partnership", "Individual"}


def test_details_keep_ratings_by_theme_and_email(parsed):
    details = parsed[1]["SIN-001"]
    assert details["ratings"] == {"Well-being": "Good", "Care and Support": "Good",
                                  "Environment": "Requires Improvement",
                                  "Leadership and Management": "Good"}
    assert details["email"] == "hello@willow.example"


def test_ratings_listed_twice_count_once():
    value = ("Care and Support: Good; Well-being: Excellent; "
             "Well-being: Good; Care and Support: Excellent")
    assert ciw.parse_ratings(value) == {"Well-being": "Excellent", "Care and Support": "Good"}
    assert ciw.rating_summary({}) == ""


def test_registry_people(locations):
    people = ciw.registry_people(locations["SIN-001"])
    assert people == [{"first_name": "Helen", "last_name": "Marsh",
                       "title": "Responsible Individual", "source": "CIW",
                       "evidence_url": locations["SIN-001"].url}]
    assert ciw.registry_people(locations["SIN-004"]) == []


def test_search_by_place_name(parsed):
    hits = ciw.search(parsed[0], "care home", "Otherby, Wales")
    assert [h.id for h in hits] == ["SIN-003", "SIN-004"]
    assert [h.id for h in ciw.search(parsed[0], "home care", "Example Vale")] == ["SIN-002"]
    # No coordinates in the extract, so a radius search finds nothing.
    assert ciw.search(parsed[0], "care home", "", "51.5,-3.2,25") == []


@pytest.mark.asyncio
async def test_load_register_downloads_then_uses_the_local_copy(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=_csv(), headers={"content-type": "text/csv"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await ciw.load_register(client, tmp_path)
        second = await ciw.load_register(client, tmp_path)
    assert calls == [ciw.EXPORT_URL]
    assert len(first) == len(second) == 4
    assert ciw.load_details(tmp_path)["SIN-002"]["care_for"] == "Adults, Supported living"


@pytest.mark.asyncio
async def test_load_register_falls_back_to_an_old_copy(tmp_path):
    def ok(request):
        return httpx.Response(200, text=_csv())

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as client:
        await ciw.load_register(client, tmp_path)
    old = time.time() - 30 * 86400
    os.utime(tmp_path / ciw.REGISTER_FILE, (old, old))

    def down(request):
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        assert len(await ciw.load_register(client, tmp_path)) == 4
        with pytest.raises(RuntimeError):
            await ciw.load_register(client, tmp_path / "empty")


@pytest.mark.asyncio
async def test_load_register_refuses_a_page_that_is_not_the_extract(tmp_path):
    def handler(request):
        return httpx.Response(200, text="<html>Sorry</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            await ciw.load_register(client, tmp_path)


# ── reading a report PDF ─────────────────────────────────────────────────

REPORT_LINES = [
    ("plain", "Inspection Report on"),
    ("plain", "Fictional House"),
    ("cid", "About Fictional House"),
    ("cid", "Type of care provided Care Home Service"),
    ("plain", "Summary"),
    ("cid", "People are happy with the care they receive. Staff know people well."),
    ("cid", "Medication records are not always accurate (see the areas for action)."),
    ("plain", "Well-being"),
    ("cid", "People have choice about how they spend their day."),
    ("plain", "Area(s) for Improvement"),
    ("cid", "Regulation 58: medicines are not always given as prescribed."),
    ("plain", "Date Published 12/03/2026"),
]


def _cid(text: str) -> bytes:
    # The CID font maps code n to chr(n + 31): "A" (65) is 0x0022.
    return b"<" + "".join(f"{ord(c) - 31:04X}" for c in text).encode() + b">"


def _pdf(lines=REPORT_LINES) -> bytes:
    content = []
    for kind, text in lines:
        if kind == "plain":
            esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content.append(f"BT /F2 11 Tf 72 700 Td ({esc}) Tj ET".encode())
        else:
            # Kerned TJ array, split mid-word, as word processors write it.
            mid = len(text) // 2
            content.append(b"BT /F1 11 Tf 72 680 Td [" + _cid(text[:mid]) + b" 12 "
                           + _cid(text[mid:]) + b"] TJ ET")
    stream = zlib.compress(b"\n".join(content))
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
            b"1 beginbfrange <0001> <005F> <0020> endbfrange\n"
            b"endcmap CMapName currentdict /CMap defineresource pop end end")
    font2 = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objstm_body = b"5 0 " + font2
    objs = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> "
            b"/Contents 6 0 R >>"),
        4: b"<< /Type /Font /Subtype /Type0 /BaseFont /Fictional /ToUnicode 7 0 R >>",
        6: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream
           + b"\nendstream",
        7: b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream",
        # Object 5 (a font) lives in an object stream, as newer PDFs do.
        8: (b"<< /Type /ObjStm /N 1 /First 4 /Length %d >>\nstream\n" % len(objstm_body)
            + objstm_body + b"\nendstream"),
    }
    out = b"%PDF-1.7\n"
    for num, body in objs.items():
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    return out + b"trailer << /Root 1 0 R >>\n%%EOF\n"


def test_simple_pdf_reads_both_kinds_of_font():
    text = ciw.SimplePdf(_pdf()).text()
    assert "Inspection Report on" in text
    assert "People are happy with the care they receive." in text
    assert "(see the areas for action)" in text
    assert "Date Published 12/03/2026" in text


def test_report_summary_takes_the_summary_and_areas_for_improvement():
    text = ciw.SimplePdf(_pdf()).text()
    summary = ciw.report_summary_from(text)
    assert summary.startswith("People are happy with the care they receive.")
    assert "Medication records" in summary
    assert "spend their day" not in summary
    assert summary.endswith("Areas for improvement: Regulation 58: medicines are not "
                            "always given as prescribed.")


def _report_client(calls, status=200, body=None):
    def handler(request):
        calls.append(str(request.url))
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, content=_pdf() if body is None else body,
                              headers={"content-type": "application/pdf"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_latest_report_reads_the_pdf_and_register_ratings(tmp_path):
    locations, details = ciw.read_extract(_csv())
    ciw.save_cache(locations, details, tmp_path)
    calls = []
    async with _report_client(calls) as client:
        report = await ciw.latest_report(client, "SIN-001", tmp_path)
        again = await ciw.latest_report(client, "SIN-001", tmp_path)
    assert calls == [ciw.REPORT_URL.format(id="SIN-001")]
    assert report == again
    assert report["ratings"]["Environment"] == "Requires Improvement"
    assert list(report["ratings"]) == list(ciw.THEMES)
    assert report["summary"].startswith("People are happy")
    assert report["published"] == "12 March 2026"
    assert report["url"] == ciw.REPORT_URL.format(id="SIN-001")


@pytest.mark.asyncio
async def test_latest_report_failure_keeps_ratings_and_is_not_cached(tmp_path):
    locations, details = ciw.read_extract(_csv())
    ciw.save_cache(locations, details, tmp_path)
    calls = []
    async with _report_client(calls, status=503) as client:
        report = await ciw.latest_report(client, "SIN-001", tmp_path)
        assert report["summary"] == "" and report["ratings"]["Well-being"] == "Good"
        # Nothing known at all: {}.
        assert await ciw.latest_report(client, "SIN-004", tmp_path) == {}
    assert not (tmp_path / ciw.REPORTS_FILE).exists()
    async with _report_client(calls, body=b"<html>not a pdf</html>") as client:
        assert (await ciw.latest_report(client, "SIN-001", tmp_path))["summary"] == ""


@pytest.mark.asyncio
async def test_latest_report_for_a_service_never_inspected(tmp_path):
    calls = []
    async with _report_client(calls, status=404) as client:
        assert await ciw.latest_report(client, "SIN-004", tmp_path) == {}
        await ciw.latest_report(client, "SIN-004", tmp_path)
    assert len(calls) == 1      # the "no report" answer is cached
