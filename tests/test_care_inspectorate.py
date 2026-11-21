"""Care Inspectorate (Scotland) register and inspection reports.

Every service, provider and person here is invented. The datastore CSV and
the report PDF are built by hand in the shapes the Care Inspectorate
publishes (a Windows-1252 CSV; an RC4-encrypted PDF whose fonts carry
ToUnicode maps), not copied.
"""

import csv
import hashlib
import io
import os
import time
import zlib

import httpx
import pytest

from sellbird.collectors import care_inspectorate as ci
from sellbird.collectors import cqc

HEADER = [
    "CSNumber", "Combined_Service_", "CaseNumber_Combined", "CareService", "Subtype",
    "AdditionalSubtypes", "ServiceType", "ServiceName", "Address_line_1",
    "Address_line_2", "Address_line_3", "Address_line_4", "Service_town",
    "Service_Postcode", "Manager_Name", "Service_Phone_Number", "Eforms_email_address",
    "SP_number", "ServiceProvider", "ServiceStatus", "Council_Area_Name", "TotalBeds",
    "Registered_Places", "Client_group", "PublicList", "Publication_of_Latest_Grading",
    "KQ_Support_Wellbeing", "KQ_Care_and_Support_Planning", "KQ_Setting",
    "KQ_Staff_Team", "KQ_Leadership", "first_reqs_2627", "first_reqs_2526",
]


def _row(**kw):
    base = {"ServiceStatus": "Active", "Client_group": "Adults", "PublicList": "Yes",
            "ServiceType": "Private", "Service_town": "Exampleton",
            "Council_Area_Name": "Exampleshire"}
    base.update(kw)
    return base


ROWS = [
    _row(CSNumber="CS2001000001", CareService="Care Home Service", Subtype="Older People",
         ServiceName="Willow Lodge", Address_line_1="1 Elm Road", Address_line_2="Exampleton",
         Service_Postcode="EX1 1AA", Manager_Name="Helen Marsh",
         Service_Phone_Number="01000 000001", Eforms_email_address="office@willow.example",
         SP_number="SP2001000001", ServiceProvider="Willow Care Ltd", TotalBeds="32",
         Publication_of_Latest_Grading="22/09/2026", KQ_Support_Wellbeing="2",
         KQ_Care_and_Support_Planning="3", KQ_Setting="4", KQ_Staff_Team="3",
         KQ_Leadership="2",
         first_reqs_2627="1. By 1 May 2026, the provider must ensure that medication is "
                         "managed safely.  In order to achieve this, the provider must: "
                         "a) audit records.",
         first_reqs_2526="By 1 May 2025, the provider must ensure people’s plans are "
                         "reviewed."),
    # A housing support and care at home service registered together: two
    # rows, each naming the other. One Location with both services.
    _row(CSNumber="CS2001000002", Combined_Service_="Yes", CaseNumber_Combined="CS2001000003",
         CareService="Housing Support Service", ServiceName="Brook Support",
         SP_number="SP2001000002", ServiceProvider="Brook Example Limited",
         Eforms_email_address="hello@brook.example"),
    _row(CSNumber="CS2001000003", Combined_Service_="Yes", CaseNumber_Combined="CS2001000002",
         CareService="Support Service", Subtype="Care at Home", ServiceName="Brook Support",
         SP_number="SP2001000002", ServiceProvider="Brook Example Limited",
         Service_Phone_Number="01000 000003"),
    # A sole trader and a partnership: PECR needs consent before cold email.
    _row(CSNumber="CS2001000004", CareService="Support Service", Subtype="Care at Home",
         ServiceName="Hill View Care at Home", SP_number="SP2001000004",
         ServiceProvider="Alex Example trading as Hill View"),
    _row(CSNumber="CS2001000005", CareService="Care Home Service", Subtype="Learning Disabilities",
         ServiceName="Oak House", SP_number="SP2001000005",
         ServiceProvider="Sam Example & Jo Example", Registered_Places="6"),
    _row(CSNumber="CS2001000006", CareService="Adult Placement Service",
         Subtype="Adult Placement Continuing Care", ServiceName="Shared Homes Exampleshire",
         ServiceType="Local Authority", SP_number="SP2001000006",
         ServiceProvider="Exampleshire Council", Service_town="Otherton"),
    _row(CSNumber="CS2001000007", CareService="Support Service",
         Subtype="Other than Care at home", AdditionalSubtypes="Adult Daycare",
         ServiceName="Riverside Day Centre", ServiceType="Voluntary or Not for Profit",
         SP_number="SP2001000007", ServiceProvider="Riverside Example Association"),
    # Housing support with no personal care (floating support, supported
    # tenancies) is kept; "supported living" only where the record says so.
    _row(CSNumber="CS2001000012", CareService="Housing Support Service",
         ServiceName="Exampleton Floating Support", SP_number="SP2001000012",
         ServiceProvider="Example Housing Association Limited"),
    _row(CSNumber="CS2001000013", CareService="Housing Support Service",
         ServiceName="Elm Court Supported Living", SP_number="SP2001000012",
         ServiceProvider="Example Housing Association Limited"),
    # Never read: children's services, inactive, off the public list,
    # offender accommodation.
    _row(CSNumber="CS2001000008", CareService="Child Minding", ServiceName="Little Steps",
         Client_group="Children"),
    _row(CSNumber="CS2001000009", CareService="Care Home Service", ServiceName="Closed Grange",
         ServiceStatus="Inactive"),
    _row(CSNumber="CS2001000010", CareService="Housing Support Service",
         ServiceName="Unlisted Refuge", PublicList="No"),
    _row(CSNumber="CS2001000011", CareService="Offender Accommodation Service",
         ServiceName="Example Hostel"),
]


def make_csv(rows=ROWS) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER, restval="")
    w.writeheader()
    for row in rows:
        w.writerow(row)
    return buf.getvalue().encode("cp1252")


@pytest.fixture
def parsed():
    return ci.read_services(ci.decode_csv(make_csv()))


@pytest.fixture
def by_id(parsed):
    return {loc.id: loc for loc in parsed[0]}


def test_keeps_only_active_adult_services(by_id):
    assert set(by_id) == {"CS2001000001", "CS2001000003", "CS2001000004",
                          "CS2001000005", "CS2001000006", "CS2001000007",
                          "CS2001000010", "CS2001000012", "CS2001000013"}


def test_unlisted_services_are_kept_and_flagged(by_id, parsed):
    assert by_id["CS2001000010"].services == ["housing support"]
    assert parsed[1]["CS2001000010"]["unlisted"] is True
    assert parsed[1]["CS2001000001"]["unlisted"] is False


def test_location_fields(by_id):
    loc = by_id["CS2001000001"]
    assert isinstance(loc, cqc.Location)
    assert loc.name == "Willow Lodge"
    assert loc.address == "1 Elm Road"           # the town isn't repeated
    assert (loc.city, loc.postcode, loc.local_authority) == (
        "Exampleton", "EX1 1AA", "Exampleshire")
    assert loc.lat is None and loc.lng is None   # the datastore has postcodes only
    assert loc.website == ""
    assert loc.services == ["care home"]
    assert loc.beds == 32
    assert loc.rating == "Weak: lowest grade 2 of 6 (22/09/2026)"
    assert loc.manager == "Marsh, Helen"          # cqc's "Surname, Forename"
    assert (loc.provider_id, loc.provider_name, loc.ownership) == (
        "SP2001000001", "Willow Care Ltd", "Organisation")
    assert loc.url == cqc.LOCATION_URL.format(id=loc.id)  # cqc's property, so:
    assert ci.url(loc.id) == ("https://www.careinspectorate.scot/find-care/"
                              "search-for-care/care-services/CS2001000001")


def test_registry_people_work_on_scottish_managers(by_id):
    people = cqc.registry_people(by_id["CS2001000001"])
    assert [(p["first_name"], p["last_name"], p["title"]) for p in people] == [
        ("Helen", "Marsh", "Registered Manager")]


def test_combined_registrations_become_one_service(by_id, parsed):
    loc = by_id["CS2001000003"]
    assert sorted(loc.services) == ["home care", "housing support"]
    assert loc.provider_locations == 1
    assert parsed[1]["CS2001000003"]["email"] == "hello@brook.example"


@pytest.mark.parametrize("sid, services", [
    ("CS2001000005", ["care home"]),
    ("CS2001000006", ["shared lives"]),
    ("CS2001000007", ["day care"]),
    ("CS2001000012", ["housing support"]),
    ("CS2001000013", ["housing support", "supported living"]),
])
def test_service_types(by_id, sid, services):
    assert by_id[sid].services == services


def test_care_home_places_stand_in_for_beds(by_id):
    assert by_id["CS2001000005"].beds == 6


def test_ownership_marks_sole_traders_and_partnerships(by_id):
    assert by_id["CS2001000004"].ownership == "Individual"
    assert by_id["CS2001000005"].ownership == "Partnership"
    assert by_id["CS2001000006"].ownership == "Local Authority"
    assert by_id["CS2001000007"].ownership == "Organisation"
    assert {"Individual", "Partnership"} == set(ci.CONSENT_NEEDED)


def test_extras_carry_email_grades_and_requirements(parsed):
    extras = parsed[1]["CS2001000001"]
    assert extras["email"] == "office@willow.example"
    assert extras["grades"] == {"Wellbeing": 2, "Care and support planning": 3,
                                "Setting": 4, "Staff team": 3, "Leadership": 2}
    assert extras["graded"] == "22/09/2026"
    assert len(extras["requirements"]) == 2
    assert "people’s plans" in extras["requirements"][1]   # Windows-1252 read right


def test_unrated_service_has_no_rating(by_id):
    assert by_id["CS2001000004"].rating == ""


def test_search_reuses_cqc_search(parsed):
    locs = parsed[0]
    assert [x.id for x in ci.search(locs, "home care", "Exampleton")] == [
        "CS2001000003", "CS2001000004"]
    assert [x.id for x in ci.search(locs, "care homes", "")] == [
        "CS2001000001", "CS2001000005"]
    assert [x.id for x in ci.search(locs, "shared lives", "Otherton")] == ["CS2001000006"]
    assert [x.id for x in ci.search(locs, "supported living", "Exampleton")] == [
        "CS2001000013"]
    # No coordinates in the datastore, so a radius search finds nothing.
    assert ci.search(locs, "care home", coordinate="56.0,-3.2,25") == []


# ── the download ─────────────────────────────────────────────────────────

DATA_HTML = ('<a href="/resources-data/publications-and-statistics/library/'
             'datastore-30-sep-2026">Datastore</a>')
LIBRARY_URL = ("https://www.careinspectorate.scot/resources-data/publications-and-"
               "statistics/library/datastore-30-sep-2026")
CSV_URL = "https://bucket.example/assets/resources/datastore/260930datastoreexternal.csv"
LIBRARY_HTML = f'<a href="{CSV_URL}">CSV</a><a href="{CSV_URL[:-3]}xlsx">XLSX</a>'


def test_finds_the_links():
    assert ci.find_library_links(DATA_HTML) == [LIBRARY_URL]
    assert ci.find_csv_url(LIBRARY_HTML) == CSV_URL
    assert ci.find_csv_url("<p>nothing</p>") == ""


def _register_client(seen, fail=False):
    def handler(request):
        seen.append(str(request.url))
        if fail:
            return httpx.Response(503)
        if str(request.url) == ci.DATA_PAGE:
            return httpx.Response(200, text=DATA_HTML)
        if str(request.url) == LIBRARY_URL:
            return httpx.Response(200, text=LIBRARY_HTML)
        if str(request.url) == CSV_URL:
            return httpx.Response(200, content=make_csv())
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_load_register_downloads_then_uses_the_local_copy(tmp_path):
    seen = []
    async with _register_client(seen) as client:
        locs = await ci.load_register(client, tmp_path)
        assert len(locs) == 9
        assert seen == [ci.DATA_PAGE, LIBRARY_URL, CSV_URL]
        again = await ci.load_register(client, tmp_path)
    assert len(seen) == 3                                    # no second download
    assert [x.id for x in again] == [x.id for x in locs]
    assert ci.service_extras(tmp_path)["CS2001000001"]["email"] == "office@willow.example"


@pytest.mark.asyncio
async def test_an_old_copy_beats_none(tmp_path):
    async with _register_client([]) as client:
        await ci.load_register(client, tmp_path)
    cache = tmp_path / ci.CACHE_NAME
    old = time.time() - (ci.MAX_AGE_DAYS + 1) * 86400
    os.utime(cache, (old, old))
    seen = []
    async with _register_client(seen, fail=True) as client:
        locs = await ci.load_register(client, tmp_path)
    assert seen and len(locs) == 9


@pytest.mark.asyncio
async def test_no_copy_and_no_connection_says_why(tmp_path):
    async with _register_client([], fail=True) as client:
        with pytest.raises(RuntimeError, match="Care Inspectorate"):
            await ci.load_register(client, tmp_path)


# ── a report PDF, built by hand ──────────────────────────────────────────

REPORT_LINES = [
    ("Willow", "Lodge"),            # two runs with a gap: one space between
    ("Medi", "cation review"),      # two runs touching: no space
    ("Key messages",),
    ("• Staff were kind and knew people well.",),
    ("• Medication records needed to",),
    ("improve.",),
    ("From this inspection we evaluated this service as:",),
    ("How well do we support people's wellbeing?",),
    ("2 - Weak",),
    ("How good is our leadership?",),
    ("3 - Adequate",),
    ("Inspection report for Willow Lodge",),
    ("page 2 of 3",),
    ("Requirements",),
    ("Requirement 1",),
    ("By 1 May 2026, the provider must ensure that medication is managed",),
    ("safely",),
    ("In order to achieve this, the provider must audit records.",),
    ("Areas for improvement",),
    ("Area for improvement 1",),
    ("The service should review moving and handling training. Staff said so.",),
]
SIZE = 12
GLYPH_WIDTH = 500                  # every glyph: 6 points wide at 12 points


def _pdf(lines=REPORT_LINES, encrypt=True) -> bytes:
    chars = sorted({c for line in lines for run in line for c in run})
    code = {c: i + 1 for i, c in enumerate(chars)}
    cmap = "/CIDInit /ProcSet findresource begin\nbegincmap\n%d beginbfchar\n%s\nendbfchar\nendcmap\n" % (
        len(chars), "\n".join(f"<{code[c]:02X}> <{ord(c):04X}>" for c in chars))
    ops = ["BT", f"/F1 {SIZE} Tf", "50 800 Td"]
    for line in lines:
        x = 0.0
        for i, run in enumerate(line):
            if i:
                width = len(line[i - 1]) * GLYPH_WIDTH * SIZE / 1000
                gap = 4 if line[0] == "Willow" else 0
                ops.append(f"{width + gap:.2f} 0 Td")
                x += width + gap
            ops.append("<" + "".join(f"{code[c]:02X}" for c in run) + "> Tj")
        ops.append(f"{-x:.2f} -14 Td")
    ops.append("ET")
    content = zlib.compress("\n".join(ops).encode("latin-1"))
    cmap_data = zlib.compress(cmap.encode("latin-1"))
    file_id = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
    owner = bytes(range(32))
    p = -12
    key = None
    if encrypt:
        h = hashlib.md5(ci._PAD + owner + (p & 0xFFFFFFFF).to_bytes(4, "little")
                        + file_id).digest()
        for _ in range(50):
            h = hashlib.md5(h[:16]).digest()
        key = h[:16]

    def stream(num, data, extra=""):
        if key:
            k = hashlib.md5(key + num.to_bytes(3, "little") + b"\0\0").digest()[:16]
            data = ci._rc4(k, data)
        return (f"<</Length {len(data)} /Filter /FlateDecode{extra}>>\nstream\n".encode()
                + data + b"\nendstream")

    widths = " ".join([str(GLYPH_WIDTH)] * len(chars))
    objs = {
        1: b"<</Type /Catalog /Pages 2 0 R>>",
        2: b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
        3: b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 6 0 R "
           b"/Resources <</Font <</F1 4 0 R>>>>>>",
        4: (f"<</Type /Font /Subtype /TrueType /BaseFont /ExampleSans /FirstChar 1 "
            f"/LastChar {len(chars)} /Widths [{widths}] /ToUnicode 5 0 R>>").encode(),
        5: stream(5, cmap_data),
        6: stream(6, content),
    }
    if encrypt:
        objs[7] = (b"<</Filter /Standard /V 2 /R 3 /Length 128 /O <" + owner.hex().encode()
                   + b"> /U <" + bytes(32).hex().encode() + b"> /P -12>>")
    out = b"%PDF-1.4\n"
    for num, body in objs.items():
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    out += (b"trailer\n<</Size 8 /Root 1 0 R" + (b" /Encrypt 7 0 R" if encrypt else b"")
            + b" /ID [<" + file_id.hex().encode() + b"> <" + file_id.hex().encode()
            + b">]>>\n%%EOF\n")
    return out


@pytest.mark.parametrize("encrypt", [True, False])
def test_pdf_text_reads_encrypted_and_plain_reports(encrypt):
    text = ci.pdf_text(_pdf(encrypt=encrypt))
    lines = text.split("\n")
    assert lines[0] == "Willow Lodge"
    assert lines[1] == "Medication review"
    assert "• Staff were kind and knew people well." in lines


def test_report_ratings_and_summary():
    text = ci.pdf_text(_pdf())
    assert ci.report_ratings(text) == {
        "How well do we support people's wellbeing?": "2 - Weak",
        "How good is our leadership?": "3 - Adequate"}
    summary = ci.report_summary_from(text)
    assert summary.startswith("Key messages: Staff were kind and knew people well. "
                              "Medication records needed to improve.\n")
    assert ("Requirements: By 1 May 2026, the provider must ensure that medication "
            "is managed safely\n") in summary
    assert "In order to achieve" not in summary
    assert summary.endswith("\nAreas for improvement: The service should review moving "
                            "and handling training.")
    assert "page 2 of 3" not in summary and "Inspection report" not in summary


def test_summary_is_trimmed_at_a_sentence():
    text = ci.pdf_text(_pdf())
    short = ci.report_summary_from(text, limit=60)
    assert len(short) <= 60 and short.endswith(".")


# ── the latest report ────────────────────────────────────────────────────

PDF_URL = ("https://ci-web-media-production-bucket.s3.eu-west-2.amazonaws.com/"
           "inspection-reports/CS2001000001/100001.pdf")
PROFILE = ('<main><strong>Most recent inspection:</strong>\n   07 Sep 2026\n'
           '<a href="' + PDF_URL + '">View latest inspection report</a>'
           '<script x-ref="historyData">[{"report_url":"'
           + PDF_URL.replace("/", "\\/") + '"}]</script></main>')


def _report_client(seen, pdf=None, fail=False):
    def handler(request):
        seen.append(str(request.url))
        if fail:
            return httpx.Response(500)
        if str(request.url) == ci.url("CS2001000001"):
            return httpx.Response(200, text=PROFILE)
        if str(request.url) == PDF_URL:
            return httpx.Response(200, content=pdf or _pdf())
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_latest_report_reads_the_pdf_and_caches_it(tmp_path):
    seen = []
    async with _report_client(seen) as client:
        report = await ci.latest_report(client, "CS2001000001", tmp_path)
        assert set(report) == {"ratings", "summary", "url", "published"}
        assert report["ratings"]["How good is our leadership?"] == "3 - Adequate"
        assert report["summary"].startswith("Key messages: Staff were kind")
        assert report["url"] == PDF_URL
        assert report["published"] == "07 Sep 2026"
        again = await ci.latest_report(client, "CS2001000001", tmp_path)
    assert again == report
    assert seen == [ci.url("CS2001000001"), PDF_URL]       # once only


@pytest.mark.asyncio
async def test_a_report_without_grades_takes_them_from_the_datastore(tmp_path):
    lines = [x for x in REPORT_LINES if not x[0].startswith(("How ", "2 -", "3 -"))]
    locs, extras = ci.read_services(ci.decode_csv(make_csv()))
    ci.save_cache(locs, extras, tmp_path / ci.CACHE_NAME)
    async with _report_client([], pdf=_pdf(lines)) as client:
        report = await ci.latest_report(client, "CS2001000001", tmp_path)
    assert report["ratings"] == {"Wellbeing": "2 - Weak",
                                 "Care and support planning": "3 - Adequate",
                                 "Setting": "4 - Good", "Staff team": "3 - Adequate",
                                 "Leadership": "2 - Weak"}
    assert report["summary"].startswith("Key messages:")


@pytest.mark.asyncio
async def test_offline_falls_back_to_the_datastore_without_caching(tmp_path):
    locs, extras = ci.read_services(ci.decode_csv(make_csv()))
    ci.save_cache(locs, extras, tmp_path / ci.CACHE_NAME)
    async with _report_client([], fail=True) as client:
        report = await ci.latest_report(client, "CS2001000001", tmp_path)
    assert report["summary"] == (
        "Requirements: By 1 May 2026, the provider must ensure that medication is "
        "managed safely. By 1 May 2025, the provider must ensure people’s plans "
        "are reviewed.")
    assert report["url"] == ci.url("CS2001000001")
    assert report["published"] == "22/09/2026"
    assert not (tmp_path / "reports.json").exists()       # try the page next time


@pytest.mark.asyncio
async def test_offline_with_nothing_known_is_quiet(tmp_path):
    async with _report_client([], fail=True) as client:
        assert await ci.latest_report(client, "CS2001000099", tmp_path) == {}
