"""CQC register discovery and Companies House directors.

Every business and person here is invented. The spreadsheet is built by
hand in the shape CQC publishes (an .ods zip with content.xml), not copied.
"""

import os
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import cqc
from harvey.collectors import discover as D
from harvey.integrations import companies_house as CH
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager

HEADER = [
    "Location ID", "Dormant (Y/N)", "Care home?", "Location Name",
    "Location Telephone Number", "Registered manager", "Location Web Address",
    "Care homes beds", "Location Inspection Directorate",
    "Location Latest Overall Rating", "Location Local Authority",
    "Location Street Address", "Location Address Line 2", "Location City",
    "Location Postal Code", "Location Latitude", "Location Longitude",
    "Provider Companies House Number", "Provider ID", "Provider Name",
    "Provider Ownership Type", "Provider Telephone Number", "Provider Web Address",
    "Provider Nominated Individual Name",
    "Service type - Care home service with nursing",
    "Service type - Care home service without nursing",
    "Service type - Domiciliary care service",
    "Service type - Supported living service",
]


def _row(**kw):
    base = {
        "Dormant (Y/N)": "N", "Location Inspection Directorate": "Adult social care",
        "Provider Ownership Type": "Organisation", "Location City": "Exampleton",
        "Location Local Authority": "Exampleshire",
    }
    base.update(kw)
    return [base.get(h, "") for h in HEADER]


ROWS = [
    _row(**{"Location ID": "1-100", "Location Name": "Willow Lodge",
            "Location Web Address": "www.willowlodge.example/home",
            "Location Telephone Number": "01000 000001",
            "Registered manager": "Marsh, Helen Jane", "Care homes beds": "32",
            "Location Latest Overall Rating": "Requires improvement",
            "Location Street Address": "1 Elm Road", "Location Postal Code": "EX1 1AA",
            "Location Latitude": "53.4800", "Location Longitude": "-2.2400",
            "Provider Companies House Number": "01234567", "Provider ID": "1-P1",
            "Provider Name": "Willow Care Ltd", "Provider Nominated Individual Name": "Marsh, Helen",
            "Service type - Care home service without nursing": "Y"}),
    _row(**{"Location ID": "1-200", "Location Name": "Brook Home Care",
            "Location Web Address": "", "Provider Web Address": "brookcare.example",
            "Registered manager": "Patel, Arun", "Location Latitude": "53.5000",
            "Location Longitude": "-2.2000", "Provider ID": "1-P2",
            "Provider Nominated Individual Name": "Okafor, Grace",
            "Service type - Domiciliary care service": "Y"}),
    # A partnership: PECR needs consent, so discovery leaves it out.
    _row(**{"Location ID": "1-300", "Location Name": "Hill View Care",
            "Location Web Address": "hillview.example", "Provider ID": "1-P3",
            "Provider Ownership Type": "Partnership", "Location Latitude": "53.4810",
            "Location Longitude": "-2.2410", "Service type - Care home service without nursing": "Y"}),
    # Dormant, and a dentist: never read.
    _row(**{"Location ID": "1-400", "Location Name": "Sleeping Oak", "Dormant (Y/N)": "Y",
            "Service type - Care home service with nursing": "Y"}),
    _row(**{"Location ID": "1-500", "Location Name": "Smile Studio",
            "Location Inspection Directorate": "Primary medical services"}),
    # Far away (about 160 km south).
    _row(**{"Location ID": "1-600", "Location Name": "Distant Grange",
            "Location Web Address": "distantgrange.example", "Provider ID": "1-P1",
            "Location Latitude": "52.0000", "Location Longitude": "-2.2400",
            "Service type - Care home service with nursing": "Y"}),
]


def _cell(value):
    return (f'<table:table-cell office:value-type="string"><text:p>{escape(value)}'
            f'</text:p></table:table-cell>' if value else "<table:table-cell/>")


def make_ods(path: Path, rows=ROWS):
    def row_xml(cells):
        # A blank tail written the way spreadsheets write it: one repeated cell.
        return ("<table:table-row>" + "".join(_cell(c) for c in cells)
                + '<table:table-cell table:number-columns-repeated="16000"/>'
                + "</table:table-row>")
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        '<office:body><office:spreadsheet>'
        '<table:table table:name="README"><table:table-row>'
        + _cell("About this file") + '</table:table-row></table:table>'
        '<table:table table:name="HSCA_Active_Locations">'
        + row_xml(HEADER) + "".join(row_xml(r) for r in rows)
        + '<table:table-row table:number-rows-repeated="1000"><table:table-cell/></table:table-row>'
        '</table:table></office:spreadsheet></office:body></office:document-content>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        z.writestr("content.xml", content)
    return path


@pytest.fixture
def locations(tmp_path):
    return cqc.read_locations(make_ods(tmp_path / "active.ods"))


# ── reading the spreadsheet ──

def test_reads_only_active_adult_social_care(locations):
    assert [l.id for l in locations] == ["1-100", "1-200", "1-300", "1-600"]


def test_location_fields(locations):
    willow = locations[0]
    assert willow.name == "Willow Lodge"
    assert willow.website == "https://www.willowlodge.example/home"
    assert willow.services == ["care home"]
    assert willow.beds == 32
    assert willow.rating == "Requires improvement"
    assert (willow.lat, willow.lng) == (53.48, -2.24)
    assert willow.company_number == "01234567"
    assert willow.provider_locations == 2       # Willow Lodge and Distant Grange
    assert willow.url == "https://www.cqc.org.uk/location/1-100"


def test_cache_round_trip(tmp_path, locations):
    path = tmp_path / "cache.json"
    cqc.save_cache(locations, path)
    assert cqc.load_cache(path) == locations


def test_finds_the_monthly_file_link():
    html = ('<a href="https://www.cqc.org.uk/system/files/2026-11/x_CQC_directory.csv">csv</a>'
            '<a href="https://www.cqc.org.uk/system/files/2026-11/2_November_2026_HSCA_Active_Locations.ods">ods</a>')
    assert cqc.find_file_url(html).endswith("HSCA_Active_Locations.ods")
    assert cqc.find_file_url("<p>nothing</p>") == ""


# ── searching it ──

@pytest.mark.parametrize("term,expected", [
    ("care home", {"care home", "nursing home"}),
    ("residential care home", {"care home", "nursing home"}),
    ("nursing home", {"nursing home"}),
    ("home care agency", {"home care"}),
    ("domiciliary care agency", {"home care"}),
    ("supported living provider", {"supported living"}),
    ("roofer", set()),
])
def test_phrases_map_to_service_types(term, expected):
    assert cqc.services_for(term) == expected


def test_search_by_radius_nearest_first(locations):
    found = cqc.search(locations, "care home", coordinate="53.48,-2.24,15")
    assert [l.name for l in found] == ["Willow Lodge", "Hill View Care"]


def test_search_by_place_name_without_a_coordinate(locations):
    assert len(cqc.search(locations, "home care agency", place="Exampleton, United Kingdom")) == 1
    assert cqc.search(locations, "home care agency", place="Nowhere") == []


# ── people ──

@pytest.mark.parametrize("raw,expected", [
    ("Marsh, Helen Jane", ("Helen", "Marsh")),
    ("O'NEILL-BRANDT, SEAN", ("Sean", "O'Neill-Brandt")),
    ("Patel, Arun", ("Arun", "Patel")),
    ("Helen Marsh", None),
    ("*", None),
    ("Marsh,", None),
])
def test_split_registry_name(raw, expected):
    assert cqc.split_registry_name(raw) == expected


def test_registry_people_lists_each_person_once(locations):
    people = cqc.registry_people(locations[0])
    assert [(p["first_name"], p["last_name"], p["title"]) for p in people] == [
        ("Helen", "Marsh", "Registered Manager")]
    assert people[0]["evidence_url"] == "https://www.cqc.org.uk/location/1-100"
    brook = cqc.registry_people(locations[1])
    assert [p["title"] for p in brook] == ["Registered Manager", "Nominated Individual"]


def test_people_from_observations_reads_the_newest_run_only():
    rows = [
        {"value_text": "Arun Patel | Registered Manager | CQC", "run_id": "r2",
         "observed_at": "2026-11-16 10:00:00", "evidence_url": "u"},
        {"value_text": "Old Manager | Registered Manager | CQC", "run_id": "r1",
         "observed_at": "2026-10-01 10:00:00"},
    ]
    people = cqc.people_from_observations(rows)
    assert [(p["first_name"], p["last_name"]) for p in people] == [("Arun", "Patel")]


# ── the discovery provider ──

@pytest.mark.asyncio
async def test_provider_turns_services_into_businesses(locations):
    provider = D.CQCRegister()
    provider._locations = locations
    found = await provider.fetch(None, {}, D.DiscoveryQuery(
        term="care home", location="Exampleton", coordinate="53.48,-2.24,15"))
    # The partnership nearby is left out (PECR).
    assert [b.name for b in found] == ["Willow Lodge"]
    willow = found[0]
    assert willow.domain == "willowlodge.example"
    assert willow.external_id == "cqc:1-100"
    assert willow.company_number == "01234567"
    assert willow.register_rating == "Requires improvement"
    assert willow.people[0]["title"] == "Registered Manager"


@pytest.mark.asyncio
async def test_provider_falls_back_to_the_provider_website(locations):
    provider = D.CQCRegister()
    provider._locations = locations
    found = await provider.fetch(None, {}, D.DiscoveryQuery(
        term="home care agency", coordinate="53.48,-2.24,15"))
    assert [b.domain for b in found] == ["brookcare.example"]


@pytest.mark.asyncio
async def test_provider_refuses_a_term_it_cannot_answer():
    with pytest.raises(RuntimeError, match="no service type"):
        await D.CQCRegister().fetch(None, {}, D.DiscoveryQuery(term="roofer"))


@pytest.mark.asyncio
async def test_provider_respects_the_limit(locations):
    provider = D.CQCRegister()
    provider._locations = locations
    found = await provider.fetch(None, {}, D.DiscoveryQuery(
        term="care home", coordinate="53.48,-2.24,500", limit=1))
    assert len(found) == 1


def test_observations_carry_rating_number_and_people(locations):
    biz = D.Business(name="Willow Lodge", provider="cqc", source_url="https://cqc.example/1",
                     register_rating="Good", company_number="01234567",
                     people=tuple(cqc.registry_people(locations[0])))
    confirmed = {"CQC_RATING", "COMPANY_NUMBER", "REGISTRY_VERIFIED"}
    rows = {r["signal_code"]: r for r in D._observations(biz, "c1", confirmed, "run")}
    assert rows["CQC_RATING"]["value_text"] == "Good"
    assert rows["COMPANY_NUMBER"]["value_text"] == "01234567"
    assert rows["REGISTRY_VERIFIED"]["value_text"] == "Helen Marsh | Registered Manager | CQC"
    # Unconfirmed signals are not recorded.
    assert D._observations(biz, "c1", set(), "run") == []


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


@pytest.mark.asyncio
async def test_load_register_downloads_then_uses_the_local_copy(tmp_path):
    ods_bytes = make_ods(tmp_path / "source.ods").read_bytes()
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith(".ods"):
            return httpx.Response(200, content=ods_bytes)
        return httpx.Response(200, text='<a href="https://www.cqc.org.uk/system/files/'
                                        '2026-11/1_HSCA_Active_Locations.ods">x</a>')

    data_dir = tmp_path / "cqc"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await cqc.load_register(client, data_dir)
        again = await cqc.load_register(client, data_dir)
    assert len(first) == 4 and again == first
    assert len(calls) == 2          # the page and the file, once


# ── Companies House ──

OFFICERS = {"items": [
    {"name": "MARSH, Helen Jane", "officer_role": "director",
     "date_of_birth": {"month": 1, "year": 1970}},
    {"name": "GREY, Tom", "officer_role": "director", "resigned_on": "2024-01-01",
     "date_of_birth": {"month": 2, "year": 1960}},
    {"name": "BLUE, Ann", "officer_role": "secretary", "date_of_birth": {"month": 3, "year": 1980}},
    {"name": "EXAMPLE HOLDINGS LIMITED", "officer_role": "corporate-director"},
]}


@pytest.mark.parametrize("raw,expected", [
    ("1234567", "01234567"), ("sc123456", "SC123456"), (" 01234567 ", "01234567"),
    ("", ""), ("not a number", ""),
])
def test_normalize_company_number(raw, expected):
    assert CH.normalize_number(raw) == expected


def test_only_current_people_directors_count():
    people = CH.directors_from_officers(OFFICERS, "01234567")
    assert [(p["first_name"], p["last_name"], p["title"]) for p in people] == [
        ("Helen", "Marsh", "Director")]


@pytest.mark.asyncio
async def test_directors_are_fetched_once_and_cached(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json=OFFICERS)

    ch = CH.CompaniesHouse("test-key", cache_path=tmp_path / "ch.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await ch.directors("1234567", client)
        again = await CH.CompaniesHouse("test-key", cache_path=tmp_path / "ch.json") \
            .directors("01234567", client)
    assert first == again and len(first) == 1
    assert len(calls) == 1 and calls[0].startswith("Basic ")


@pytest.mark.asyncio
async def test_a_bad_key_is_not_cached(tmp_path):
    def handler(request):
        return httpx.Response(401, json={"error": "Invalid Authorization"})

    ch = CH.CompaniesHouse("wrong", cache_path=tmp_path / "ch.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await ch.directors("01234567", client) == []
    assert not (tmp_path / "ch.json").exists()


@pytest.mark.asyncio
async def test_no_key_means_no_request(tmp_path):
    assert await CH.CompaniesHouse("", cache_path=tmp_path / "ch.json").directors("01234567") == []


# ── Scout puts registry people first ──

@pytest.mark.asyncio
async def test_scout_reads_registry_people_and_directors(state, tmp_path, monkeypatch):
    from harvey.agents.scout import Scout
    from harvey.config import EnvConfig
    from harvey.models import Company

    company_id = await state.add_company(Company(name="Willow Lodge", domain="willowlodge.example"))
    await state.add_observations([
        {"company_id": company_id, "signal_code": "REGISTRY_VERIFIED",
         "value_text": "Arun Patel | Nominated Individual | CQC", "run_id": "r1"},
        {"company_id": company_id, "signal_code": "REGISTRY_VERIFIED",
         "value_text": "Helen Marsh | Registered Manager | CQC", "run_id": "r1"},
        {"company_id": company_id, "signal_code": "COMPANY_NUMBER",
         "value_text": "01234567", "run_id": "r1"},
    ])

    async def fake_directors(self, number, client=None):
        assert number == "01234567"
        return CH.directors_from_officers(
            {"items": [OFFICERS["items"][0],
                       {"name": "KERR, Duncan", "officer_role": "director",
                        "date_of_birth": {"month": 5, "year": 1965}}]}, number)
    monkeypatch.setattr(CH.CompaniesHouse, "directors", fake_directors)

    scout = Scout.__new__(Scout)
    scout.state = state
    scout.env = EnvConfig(companies_house_api_key="test-key")
    people = await scout._registry_people(company_id)
    assert [(p["first_name"], p["title"]) for p in people] == [
        ("Helen", "Registered Manager"), ("Arun", "Nominated Individual"),
        ("Duncan", "Director")]

    scout.env = EnvConfig()
    assert len(await scout._registry_people(company_id)) == 2


# ── the latest inspection report ─────────────────────────────────────────

NEW_LOCATION = """<html><main><h1>Example Lodge</h1>
<p>Report published: 3 March 2026</p>
<p>Safe</p><p>Requires improvement</p><p>Effective</p><p>Good</p>
<p>Caring</p><p>Good</p><p>Responsive</p><p>Good</p><p>Well-led</p><p>Good</p>
<a href="/location/1-123/reports/AP1/overall">Read the latest assessment report</a>
</main></html>"""
NEW_REPORT = """<html><main><p>Ratings Overall Requires improvement Safe Requires
improvement Effective Good Caring Good Responsive Good Well-led Good</p>
<p>Our view of the service Records did not always show medicines were given
as prescribed.</p><p>People's experience of this service Staff were kind.</p>
</main></html>"""
OLD_LOCATION = """<html><main><p>Safe Good Effective Good Caring Good
Responsive Good Well-led Good</p>
<a href="/location/1-456/inspection-summary">Latest inspection summary</a>
</main></html>"""
OLD_SUMMARY = """<html><main><p>On this page Background to this inspection
Overall inspection</p><p>Background to this inspection Updated 1 May 2021 The
inspection was routine.</p><p>Overall inspection Good Updated 1 May 2021
People were supported by trained staff.</p><p>Rating at last inspection Good
</p></main></html>"""


def _report_client(pages: dict[str, str]):
    def handler(request):
        body = pages.get(request.url.path)
        return httpx.Response(200, text=body) if body else httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://www.cqc.org.uk")


@pytest.mark.asyncio
async def test_latest_report_reads_a_new_style_report(tmp_path):
    pages = {"/location/1-123": NEW_LOCATION,
             "/location/1-123/reports/AP1/overall": NEW_REPORT}
    async with _report_client(pages) as client:
        report = await cqc.latest_report(client, "1-123", tmp_path)
    assert report["ratings"]["Safe"] == "Requires improvement"
    assert report["ratings"]["Effective"] == "Good"
    assert "medicines were given" in report["summary"]
    assert "Staff were kind" not in report["summary"]
    assert report["published"] == "3 March 2026"


@pytest.mark.asyncio
async def test_latest_report_reads_an_old_style_summary(tmp_path):
    pages = {"/location/1-456": OLD_LOCATION,
             "/location/1-456/inspection-summary": OLD_SUMMARY}
    async with _report_client(pages) as client:
        report = await cqc.latest_report(client, "1-456", tmp_path)
    assert report["ratings"]["Safe"] == "Good"
    assert report["summary"].startswith("Overall inspection Good")
    assert "routine" not in report["summary"]
    assert "Rating at last inspection" not in report["summary"]


@pytest.mark.asyncio
async def test_latest_report_is_cached_and_quiet_on_failure(tmp_path):
    pages = {"/location/1-123": NEW_LOCATION,
             "/location/1-123/reports/AP1/overall": NEW_REPORT}
    async with _report_client(pages) as client:
        first = await cqc.latest_report(client, "1-123", tmp_path)
    async with _report_client({}) as client:  # CQC unreachable now
        assert await cqc.latest_report(client, "1-123", tmp_path) == first
        assert await cqc.latest_report(client, "1-999", tmp_path) == {}
