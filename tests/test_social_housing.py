"""Registers of social landlords (England, Scotland, Wales, Northern Ireland).

Every organisation, person and address here is invented. The spreadsheets,
pages and JSON are built by hand in the shape each regulator publishes, not
copied from them.
"""

import json
import os
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest

from sellbird.collectors import social_housing as SH

ASSETS = "https://assets.publishing.service.gov.uk/media"


# ── fixture builders ─────────────────────────────────────────────────────


def _col_name(i: int) -> str:
    name = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        name = chr(65 + r) + name
    return name


def make_xlsx(path: Path, sheets: dict[str, list[list]]) -> Path:
    """A minimal .xlsx: shared strings for text, plain values for numbers."""
    strings: list[str] = []

    def sid(text: str) -> int:
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    parts = {}
    for n, (name, rows) in enumerate(sheets.items(), 1):
        xml_rows = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, value in enumerate(row):
                if value in ("", None):
                    continue
                ref = f"{_col_name(c)}{r}"
                if isinstance(value, (int, float)):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="s"><v>{sid(str(value))}</v></c>')
            xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
        parts[f"xl/worksheets/sheet{n}.xml"] = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    book = "".join(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>'
                   for n, name in enumerate(sheets, 1))
    rels = "".join(
        f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/'
        f'2006/relationships/worksheet" Target="worksheets/sheet{n}.xml"/>'
        for n in range(1, len(sheets) + 1))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml",
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<sheets>{book}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   f'relationships">{rels}</Relationships>')
        z.writestr("xl/sharedStrings.xml",
                   '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")
        for part, xml in parts.items():
            z.writestr(part, xml)
    return path


RSH_LIST = {"Registered providers": [
    ["Organisation name", "Registration number", "Registration date", "Designation",
     "Corporate form"],
    ["Exampleton Homes Limited", "L9001", 30000, "Non-profit", "Registered society"],
    ["Sample Support Housing Ltd", "9002", 41000, "Non-profit", "Company "],
    ["Exampleshire Borough Council", "45UX", 39000, "Local Authority", ""],
    ["Brand New Homes Limited", "5999", 46000, "Profit", "Company"],
], "Hidden": [["junk"]]}

RSH_STOCK = {"Introduction": [["Read me"]], "STOCK_BY_LA": [
    ["RP_Name", "RP_Code", "RP_Type", "SDR_Size", "Survey_Status", "LA_Nm", "LA_Code",
     "Concat", "Total Social Stock", "LA_GN_SC_Own", "LA_GN_BSp_Own", "LA_SHHOP"],
    ["Exampleton Homes Limited", "L9001", "Large", "Long Form", "Signed Off", "Exampleshire",
     "E07000001", "x", 120, 120, 0, 0],
    ["Sample Support Housing Ltd", "9002", "Small", "Short Form", "Signed Off", "Exampleshire",
     "E07000001", "x", 12, 0, 0, 12],
    ["Sample Support Housing Ltd", "9002", "Small", "Short Form", "Signed Off", "Sampleborough",
     "E07000002", "x", 3, 0, 0, 3],
]}

WG_REGISTER = {
    "CURRENT": [
        ["Legal Name of Registered Social Landlord", "RSL Registration Number", "Legal Status",
         "Registered Subsidiaries", "Other registered Social Landlords Managed",
         "Date of Regulatory Judgement", "Governance", "Financial Viability", "Address",
         "", "", "", "", "Notes"],
        ["Tai Enghraifft Cyfyngedig", "L901", "Registered Society number 30999R", "", "",
         46000, "Green", "Green", "Uned 1", "Parc Enghraifft", "Exampletown", "Exampleshire",
         "", "Formerly Example Homes"],
        ["Sample Valleys Housing", "L902", "Charitable company number 1234567", "", "",
         46000, "Green", "Yellow", "2 Sample Street", "Sampleford", "Sampleshire",
         "CF99 1AA", "", ""],
        [],
    ],
    "De_Mimimus": [
        ["Name of Registered Social Landlord", "Registration \nNumber", "Legal Status",
         "Address", "", "", "", "", "Notes"],
        ["Abbey Example Society Ltd", "H901", "Charity Commission Standard Registration 999999",
         "Example House", "Example Road", "Littleton", "Exampleshire", "", ""],
    ],
    "REMOVED": [
        ["Legal Name of Registered Social Landlord", "RSL Registration Number"],
        ["Gone Away Housing", "L800"],
    ],
}

SHR_PAGE = """<html><body><main id="main"><h1>Register of Social Landlords</h1>
<div class="landlord-register__item">
    <h2>Exampleburgh Housing Association Ltd</h2>
    <dl class="description-list">
        <dt>RSL Registration Number</dt>
        <dd>901</dd>
        <dt>Senior Officer</dt>
        <dd>Mrs Morag Example</dd>
        <dt>FCA / Company Number</dt>
        <dd>9901RS</dd>
        <dt>Clients</dt>
        <dd>General</dd>
        <dt>Registered address</dt>
        <dd>1 Example Street, Exampleburgh, EH99 9ZZ</dd>
        <dt>Email</dt>
        <dd> <a href="mailto:hello@exampleburgh.example">hello@exampleburgh.example</a></dd>
        <dt>Website</dt>
        <dd> <a href="https://www.exampleburgh.example">https://www.exampleburgh.example</a> </dd>
    </dl>
</div>
<div class="landlord-register__item">
    <h2>Sample Supported Homes</h2>
    <dl class="description-list">
        <dt>RSL Registration Number</dt>
        <dd>902</dd>
        <dt>Senior Officer</dt>
        <dd>Mr Angus Sample</dd>
        <dt>FCA / Company Number</dt>
        <dd>SC999999</dd>
        <dt>Clients</dt>
        <dd>Supported</dd>
        <dt>Registered address</dt>
        <dd>Unit 2, Sample Park, Samplegow G99 1AB</dd>
        <dt>Website</dt>
        <dd> <a href="https://samplesupported.example">https://samplesupported.example</a> </dd>
    </dl>
</div>
</main></body></html>"""

DFC_TOPIC = """<html><main>
<a href="/articles/example-housing-association">Example Housing Association</a>
<a href="/articles/sample-homes-limited">Sample Homes Limited</a>
<a href="/articles/housing-regulation-and-registered-housing-associations">About</a>
<a href="/articles/cookies-communities">Cookies</a>
<a href="/articles/example-housing-association">Example Housing Association</a>
</main></html>"""

DFC_EXAMPLE = """<html><body><main>
<h1 class="title">Example Housing Association</h1>
<p>Example Housing Association provides general needs homes and supported and sheltered
accommodation across Northern Ireland.</p>
<h2>Example Housing Association information</h2>
<p><strong>I&amp;P Registration Number</strong> - IP 901<br>
<strong>DfC Registration Number</strong> - R 91</p>
<p><strong>Chief Executive Officer</strong><br>Ms. Niamh Example</p>
<p><strong>Chairperson</strong><br>Mr Ciaran Sample</p>
<address><p>9 Example Street<br>Exampleville<br>BT99 9XX<br>Tel: 028 9000 0000<br>
Fax: 028 9000 0001</p></address>
<p><a href="http://www.examplehousing.example">www.examplehousing.example</a><br>
<a href="mailto:info@examplehousing.example">info@examplehousing.example</a></p>
<a href="https://twitter.com/share?url=x">Share</a>
</main></body></html>"""

DFC_SAMPLE = """<html><body><main>
<h1>Sample Homes Limited</h1>
<p>Sample Homes Limited provides homes for rent in Sampletown.</p>
<p><strong>I&amp;P Registration Number</strong> - IP 902<br>
<strong>DfC Registration Number</strong> - R 92</p>
<p><strong>General Manager</strong><br>Mr Sample Person</p>
<address><p>1 Sample Row<br>Sampletown<br>BT98 1AA</p></address>
</main></body></html>"""


def sp_feature(sid, provider, service, kind, x=-5.9, y=54.6, units=10):
    return {"attributes": {
        "Provider": provider, "SID": float(sid), "ServiceName": service, "Units": float(units),
        "ThematicGroup": "Homeless", "PrimaryClientGroup": "Young people",
        "ServiceType": kind, "TelephoneNo": "028 9000 1111", "Postcode": "bt99 1zz",
        "Website": "www.spexample.example", "LGDNAME": "Example and District",
        "NIHE_Area": "Example Area"}, "geometry": {"x": x, "y": y}}


SP_PAGES = [
    {"features": [sp_feature(1001, "Example Support Trust", "Example Hostel",
                             "Accommodation Based Service"),
                  sp_feature(1002, "Example Support Trust", "Example Floating Support",
                             "Floating Support Service", x=-6.5, y=55.0)],
     "exceededTransferLimit": True},
    {"features": [sp_feature(1003, "Sample Care NI", "Sample Peripatetic Service",
                             "Peripatetic service ")]},
]


class Remote:
    """A fake internet for the five sources. ``down`` lists hosts that fail."""

    def __init__(self, tmp: Path):
        self.files = {
            f"{ASSETS}/aa/List_of_registered_providers_1_January_2030.xlsx":
                make_xlsx(tmp / "rsh_list_fixture.xlsx", RSH_LIST).read_bytes(),
            f"{ASSETS}/bb/RP_COMBINED_TOOL_2030.xlsx":
                make_xlsx(tmp / "rsh_stock_fixture.xlsx", RSH_STOCK).read_bytes(),
            "https://www.gov.wales/sites/default/files/publications/2030-01/"
            "registered-social-landlords.xlsx":
                make_xlsx(tmp / "wg_fixture.xlsx", WG_REGISTER).read_bytes(),
        }
        self.down: set[str] = set()
        self.calls: list[str] = []
        self.sp_offsets: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        self.calls.append(url)
        if request.url.host in self.down:
            return httpx.Response(503)
        if url in self.files:
            return httpx.Response(200, content=self.files[url])
        if url == SH.RSH_LIST_PAGE:
            return httpx.Response(200, json={"details": {"attachments": [
                {"title": "Guidance", "url": f"{ASSETS}/aa/guide.pdf"},
                {"title": "List of registered providers 1 January 2030",
                 "url": f"{ASSETS}/aa/List_of_registered_providers_1_January_2030.xlsx"}]}})
        if url == SH.RSH_STOCK_COLLECTION:
            return httpx.Response(200, json={"links": {"documents": [
                {"base_path": "/government/statistics/rp-stock-2028", "public_updated_at": "2028-10-01"},
                {"base_path": "/government/statistics/rp-stock-2029", "public_updated_at": "2029-10-01"},
            ]}})
        if url == "https://www.gov.uk/api/content/government/statistics/rp-stock-2029":
            return httpx.Response(200, json={"details": {"attachments": [
                {"title": "Registered providers additional tables", "url": f"{ASSETS}/bb/extra.xlsx"},
                {"title": "Registered providers look-up tool",
                 "url": f"{ASSETS}/bb/RP_COMBINED_TOOL_2030.xlsx"}]}})
        if url == SH.SHR_REGISTER:
            return httpx.Response(200, text=SHR_PAGE)
        if url == SH.WG_PAGE:
            return httpx.Response(200, text='<a href="/sites/default/files/publications/2030-01/'
                                            'registered-social-landlords.xlsx">Register</a>')
        if url == SH.DFC_TOPIC:
            return httpx.Response(200, text=DFC_TOPIC)
        if url == SH.DFC_BASE + "/articles/example-housing-association":
            return httpx.Response(200, text=DFC_EXAMPLE)
        if url == SH.DFC_BASE + "/articles/sample-homes-limited":
            return httpx.Response(200, text=DFC_SAMPLE)
        if url == SH.NIHE_SP_LAYER:
            offset = request.url.params.get("resultOffset", "0")
            self.sp_offsets.append(offset)
            return httpx.Response(200, json=SP_PAGES[0] if offset == "0" else SP_PAGES[1])
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(SH, "PAGE_DELAY", 0)


@pytest.fixture
def remote(tmp_path):
    return Remote(tmp_path)


async def _load(remote, data_dir):
    async with remote.client() as client:
        return await SH.load_register(client, data_dir)


def _by_id(locations):
    return {loc.id: loc for loc in locations}


# ── reading each register ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_england_joins_the_list_to_the_stock_figures(remote, tmp_path):
    locs = _by_id(await _load(remote, tmp_path / "sh"))
    assert {"rsh-L9001", "rsh-9002", "rsh-5999"} <= set(locs)
    assert "rsh-45UX" not in locs                      # councils are left out
    plain, supported, unmatched = locs["rsh-L9001"], locs["rsh-9002"], locs["rsh-5999"]
    assert plain.services == ["social housing"] and plain.supported_units == 0
    assert supported.services == ["supported housing"] and supported.supported_units == 15
    assert supported.local_authority == "Exampleshire; Sampleborough"
    assert unmatched.services == ["social housing"] and unmatched.local_authority == ""
    assert supported.company_number == ""               # an RSH number isn't a company number
    assert supported.register_number == "9002" and supported.nation == "England"
    assert supported.ownership == "Organisation" and supported.lat is None
    assert supported.url == SH.RSH_PAGE
    # The newest stock release is the one read.
    assert "https://www.gov.uk/api/content/government/statistics/rp-stock-2028" not in remote.calls


@pytest.mark.asyncio
async def test_scotland_reads_clients_address_and_website(remote, tmp_path):
    locs = _by_id(await _load(remote, tmp_path / "sh"))
    general, supported = locs["shr-901"], locs["shr-902"]
    assert general.name == "Exampleburgh Housing Association Ltd"
    assert general.services == ["social housing"]
    assert (general.address, general.city, general.postcode) == (
        "1 Example Street", "Exampleburgh", "EH99 9ZZ")
    assert general.website == "https://www.exampleburgh.example"
    assert general.company_number == "9901RS"            # FCA mutuals register number
    assert supported.services == ["supported housing"]
    assert supported.company_number == "SC999999"        # Companies House number
    assert (supported.city, supported.postcode) == ("Samplegow", "G99 1AB")
    assert SH.registry_people(general) == [{
        "first_name": "Morag", "last_name": "Example", "title": "Senior Officer",
        "source": "SHR", "evidence_url": SH.SHR_REGISTER}]


@pytest.mark.asyncio
async def test_wales_reads_current_and_de_minimis_not_removed(remote, tmp_path):
    locs = _by_id(await _load(remote, tmp_path / "sh"))
    assert {"wg-L901", "wg-L902", "wg-H901"} <= set(locs)
    assert "wg-L800" not in locs
    society, company, small = locs["wg-L901"], locs["wg-L902"], locs["wg-H901"]
    assert society.company_number == "30999R"
    assert company.company_number == "01234567"
    assert small.company_number == ""                     # a charity number isn't one
    assert (society.address, society.city, society.local_authority) == (
        "Uned 1, Parc Enghraifft", "Exampletown", "Exampleshire")
    assert company.postcode == "CF99 1AA" and company.city == "Sampleford"
    assert (small.city, small.local_authority) == ("Littleton", "Exampleshire")
    assert all(loc.services == ["social housing"] for loc in (society, company, small))


@pytest.mark.asyncio
async def test_northern_ireland_associations_from_their_pages(remote, tmp_path):
    locs = _by_id(await _load(remote, tmp_path / "sh"))
    example, sample = locs["ni-R91"], locs["ni-R92"]
    assert example.name == "Example Housing Association"
    assert example.company_number == "IP901"
    assert example.services == ["supported housing"]
    assert (example.address, example.city, example.postcode) == (
        "9 Example Street", "Exampleville", "BT99 9XX")
    assert example.phone == "028 9000 0000"
    assert example.website == "http://www.examplehousing.example"
    assert example.url == SH.DFC_BASE + "/articles/example-housing-association"
    assert SH.registry_people(example)[0] == {
        "first_name": "Niamh", "last_name": "Example", "title": "Chief Executive Officer",
        "source": "DfC", "evidence_url": example.url}
    assert sample.services == ["social housing"] and sample.website == ""
    assert SH.registry_people(sample) == []
    assert not any("cookies" in c or "housing-regulation" in c for c in remote.calls)


@pytest.mark.asyncio
async def test_supporting_people_services_page_through_the_layer(remote, tmp_path):
    locs = _by_id(await _load(remote, tmp_path / "sh"))
    assert remote.sp_offsets == ["0", "2"]
    hostel, floating, peripatetic = locs["ni-sp-1001"], locs["ni-sp-1002"], locs["ni-sp-1003"]
    assert hostel.services == ["supported housing"]
    assert floating.services == ["supported housing", "floating support"]
    assert peripatetic.services == ["supported housing", "floating support"]
    assert (hostel.lat, hostel.lng) == (54.6, -5.9)
    assert hostel.postcode == "BT99 1ZZ" and hostel.beds == 10
    assert hostel.website == "https://www.spexample.example"
    assert hostel.provider_name == "Example Support Trust" and hostel.provider_locations == 2
    assert hostel.local_authority == "Example and District"
    assert hostel.client_group == "Homeless / Young people"


# ── the local copy ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fresh_copy_is_used_without_downloading(remote, tmp_path):
    data_dir = tmp_path / "sh"
    first = await _load(remote, data_dir)
    for key, _ in SH.SOURCES:
        assert (data_dir / f"{key}.json").exists()
    remote.calls.clear()
    second = await _load(remote, data_dir)
    assert remote.calls == []
    assert [loc.id for loc in second] == [loc.id for loc in first]
    assert isinstance(second[0], SH.HousingLocation)


@pytest.mark.asyncio
async def test_a_stale_copy_is_refreshed_or_kept_when_the_source_is_down(remote, tmp_path):
    data_dir = tmp_path / "sh"
    await _load(remote, data_dir)
    old = time.time() - (SH.MAX_AGE_DAYS + 1) * 86400
    for key, _ in SH.SOURCES:
        os.utime(data_dir / f"{key}.json", (old, old))
    remote.down = {"www.housingregulator.gov.scot"}
    remote.calls.clear()
    locs = _by_id(await _load(remote, data_dir))
    assert "shr-901" in locs                             # the old copy, kept
    assert SH.SHR_REGISTER in remote.calls
    assert SH.WG_PAGE in remote.calls                    # the others were refreshed


@pytest.mark.asyncio
async def test_one_source_down_with_no_copy_skips_only_that_source(remote, tmp_path):
    remote.down = {"services1.arcgis.com"}
    locs = await _load(remote, tmp_path / "sh")
    assert not any(loc.source == "NIHE" for loc in locs)
    assert {loc.source for loc in locs} == {"RSH", "SHR", "WG", "DfC"}


@pytest.mark.asyncio
async def test_everything_down_with_no_copy_raises(remote, tmp_path):
    remote.down = {"www.gov.uk", "www.housingregulator.gov.scot", "www.gov.wales",
                   "www.communities-ni.gov.uk", "services1.arcgis.com"}
    with pytest.raises(RuntimeError):
        await _load(remote, tmp_path / "sh")


@pytest.mark.asyncio
async def test_england_without_stock_figures_still_lists_providers(remote, tmp_path):
    remote.files.pop(f"{ASSETS}/bb/RP_COMBINED_TOOL_2030.xlsx")
    locs = _by_id(await _load(remote, tmp_path / "sh"))
    assert locs["rsh-9002"].services == ["social housing"]


# ── searching ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_by_term_and_place(remote, tmp_path):
    locs = await _load(remote, tmp_path / "sh")
    ids = lambda hits: {h.id for h in hits}

    assert SH.search(locs, "dentist") == []
    supported_scotland = ids(SH.search(locs, "supported housing", "Scotland"))
    assert supported_scotland == {"shr-902"}
    assert ids(SH.search(locs, "housing association", "Scotland")) == {"shr-901", "shr-902"}
    assert ids(SH.search(locs, "floating support", "Northern Ireland")) == {
        "ni-sp-1002", "ni-sp-1003"}
    assert ids(SH.search(locs, "supported housing", "Sampleborough")) == {"rsh-9002"}
    assert ids(SH.search(locs, "social housing", "EH99")) == {"shr-901"}
    assert ids(SH.search(locs, "social housing", "BT")) >= {"ni-R91", "ni-R92", "ni-sp-1001"}
    assert "rsh-L9001" in ids(SH.search(locs, "social landlord", "UK"))
    # Radius search only finds what has coordinates, nearest first.
    near = SH.search(locs, "supported accommodation", coordinate="54.6,-5.9,10")
    assert [h.id for h in near] == ["ni-sp-1001", "ni-sp-1003"]


def test_welsh_legal_status_numbers():
    assert SH._wg_company_number("Registered Society number 12345R") == "12345R"
    assert SH._wg_company_number("Registered Society number 12345 R") == "12345R"
    assert SH._wg_company_number("Charitable company number 07654321") == "07654321"
    assert SH._wg_company_number("Company limited by guarantee 123456") == "00123456"
    assert SH._wg_company_number("Charity Commission Standard Registration 999999") == ""
    assert SH._wg_company_number("") == ""
