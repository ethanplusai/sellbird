"""Housing-support charities from the CCEW and CCNI registers.

Every charity, person and address here is invented. The files are built by
hand in the shape each register publishes (CCEW: zipped tab-separated text;
CCNI: one CSV), not copied.
"""

import csv
import io
import os
import time
import zipfile

import httpx
import pytest

from harvey.collectors import charities as C

# ── fixtures ─────────────────────────────────────────────────────────────

CHARITY_HEADER = [
    "date_of_extract", "organisation_number", "registered_charity_number",
    "linked_charity_number", "charity_name", "charity_type",
    "charity_registration_status", "date_of_registration", "date_of_removal",
    "charity_reporting_status", "latest_acc_fin_period_start_date",
    "latest_acc_fin_period_end_date", "latest_income", "latest_expenditure",
    "charity_contact_address1", "charity_contact_address2", "charity_contact_address3",
    "charity_contact_address4", "charity_contact_address5", "charity_contact_postcode",
    "charity_contact_phone", "charity_contact_email", "charity_contact_web",
    "charity_company_registration_number", "charity_insolvent",
    "charity_in_administration", "charity_previously_excepted", "charity_is_cdf_or_cif",
    "charity_is_cio", "cio_is_dissolved", "date_cio_dissolution_notice",
    "charity_activities", "charity_gift_aid", "charity_has_land",
]


def _charity(org, number, name, income, activities="", **kw):
    row = {
        "organisation_number": org, "registered_charity_number": number,
        "linked_charity_number": "0", "charity_name": name,
        "charity_registration_status": "Registered",
        "charity_reporting_status": "Submission Received",
        "latest_acc_fin_period_end_date": "2026-03-31 00:00:00.0000000",
        "latest_income": str(income), "charity_contact_address1": "1 Example Street",
        "charity_contact_address2": "Exampleton", "charity_contact_postcode": "ex1 1aa",
        "charity_contact_phone": "01000 000000", "charity_contact_email": "Office@Example.org",
        "charity_contact_web": "www.example.org", "charity_insolvent": "False",
        "charity_in_administration": "False", "cio_is_dissolved": "False",
        "charity_activities": activities,
    }
    row.update(kw)
    return row


CHARITIES = [
    # 1: says floating support and supported housing itself
    _charity("1", "1000001", "Example Housing Trust", 2_500_000,
             "We provide supported housing and floating support to adults.",
             charity_company_registration_number="1234567"),
    # 2: housing classification + support words in its objects
    _charity("2", "1000002", "Example Shelter Project", 400_000,
             "Helping people in our town."),
    # 3: almshouse, housing class only
    _charity("3", "1000003", "Example Almshouse Charity", 300_000,
             "Provides almshouses for older residents."),
    # 4: too small
    _charity("4", "1000004", "Tiny Example Hostel Fund", 5_000,
             "Runs a homeless hostel with support workers."),
    # 5: removed
    _charity("5", "1000005", "Gone Example Housing", 900_000,
             "Supported accommodation.", charity_registration_status="Removed"),
    # 6: not housing at all
    _charity("6", "1000006", "Example Village Hall", 150_000, "Runs a village hall."),
    # 7: a youth hostel is not housing support
    _charity("7", "1000007", "Example Youth Hostels", 800_000, "Operates youth hostels."),
    # 8: a linked (subsidiary) charity is skipped, even when it matches
    _charity("8", "1000001", "Example Housing Trust Fund", 600_000,
             "Supported housing.", linked_charity_number="1"),
    # 9: two years behind with its accounts
    _charity("9", "1000009", "Lapsed Example Refuge", 700_000,
             "Women's refuge.", charity_reporting_status="Submission Double Default"),
    # 10: classed as housing, provides services to disabled people, no words
    _charity("10", "1000010", "Example Homes", 250_000, "General purposes."),
]

CLASSIFICATIONS = [
    ("2", "107"), ("2", "306"),
    ("3", "107"),
    ("10", "107"), ("10", "306"), ("10", "203"),
    ("6", "306"),
]

OBJECTS = [
    ("2", "To relieve homelessness and support vulnerable people into a home."),
    ("3", "To provide almshouses for poor persons of the parish."),
    ("6", "To provide a village hall for the inhabitants."),
]


def _zip_tsv(path, header, rows):
    buf = io.StringIO()
    buf.write("\t".join(header) + "\r\n")
    for row in rows:
        buf.write("\t".join(row.get(h, "") for h in header) + "\r\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(path.stem + ".txt", buf.getvalue())
    return path.read_bytes()


def ccew_files(tmp_path):
    base = ["date_of_extract", "organisation_number", "registered_charity_number",
            "linked_charity_number"]
    charity = _zip_tsv(tmp_path / "publicextract.charity.zip", CHARITY_HEADER, CHARITIES)
    classification = _zip_tsv(
        tmp_path / "publicextract.charity_classification.zip",
        base + ["classification_code", "classification_type", "classification_description"],
        [{"organisation_number": o, "linked_charity_number": "0",
          "classification_code": code} for o, code in CLASSIFICATIONS])
    governing = _zip_tsv(
        tmp_path / "publicextract.charity_governing_document.zip",
        base + ["governing_document_description", "charitable_objects", "area_of_benefit"],
        [{"organisation_number": o, "linked_charity_number": "0",
          "charitable_objects": text} for o, text in OBJECTS])
    return {"charity": charity, "charity_classification": classification,
            "charity_governing_document": governing}


CCNI_HEADER = ["Reg charity number", "Charity name", "Date registered", "Status",
               "Date for financial year ending", "Total income", "Public address",
               "Website", "Email", "Telephone", "Company number",
               "What the charity does", "Who the charity helps",
               "How the charity works", "Charitable purposes", "Other name", ""]


def ccni_csv():
    rows = [
        {"Reg charity number": "100001", "Charity name": "Example Homes NI",
         "Status": "Received: on time", "Date for financial year ending": "31/03/2026",
         "Total income": "350000", "Public address": "2 Example Road, Exampletown, BT1 1AA",
         "Website": "www.example.net", "Email": "hello@example.net", "Telephone": "02800 000000",
         "Company number": "0", "Who the charity helps": "Homelessness",
         "How the charity works": "Provides services",
         "Charitable purposes": "Tenancy sustainment and floating support for young people."},
        {"Reg charity number": "100002", "Charity name": "Example Removed Hostel",
         "Status": "Removed", "Total income": "500000",
         "Charitable purposes": "Supported accommodation."},
        {"Reg charity number": "100003", "Charity name": "Example Late Hostel",
         "Status": "Overdue: 800 days", "Total income": "500000",
         "Charitable purposes": "Supported accommodation."},
        {"Reg charity number": "100004", "Charity name": "Example Choir",
         "Status": "Registered", "Total income": "200000",
         "Charitable purposes": "Promoting choral music."},
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CCNI_HEADER)
    w.writeheader()
    for row in rows:
        w.writerow({h: row.get(h, "") for h in CCNI_HEADER})
    return ("﻿" + buf.getvalue()).encode("utf-8")


def mock_client(tmp_path, calls, ccni_body=None, fail=()):
    files = ccew_files(tmp_path)
    body = ccni_csv() if ccni_body is None else ccni_body

    def handler(request):
        url = str(request.url)
        calls.append(url)
        for name in fail:
            if name in url:
                return httpx.Response(503)
        if "charitycommissionni" in url:
            return httpx.Response(200, content=body, headers={"content-type": "text/csv"})
        for name, content in files.items():
            if url.endswith(f"publicextract.{name}.zip"):
                return httpx.Response(200, content=content)
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── matching ─────────────────────────────────────────────────────────────


def test_strong_phrases_score_highest():
    bits = C.text_features("We run supported housing and floating support services.")
    points, reason = C.score(bits)
    assert points == 4
    assert "supported housing" in reason and "floating support" in reason


def test_housing_with_support_beats_housing_only():
    with_support = C.score(C.text_features("Housing for homeless people, with support."))
    housing_only = C.score(C.B_HOUSING_CLASS)
    assert with_support[0] == 2
    assert housing_only[0] == 1
    assert "no support work" in housing_only[1]


def test_almshouse_ranks_low_and_noise_is_ignored():
    points, reason = C.score(C.text_features("Almshouses for poor persons") | C.B_HOUSING_CLASS)
    assert points == 1 and "almshouse" in reason
    assert C.score(C.text_features("We operate youth hostels for walkers."))[0] == 0
    assert C.score(C.text_features("A village hall and playing field."))[0] == 0


def test_ccni_address_split():
    assert C._ccni_address("2 Example Road, Exampletown, bt1 1aa.") == (
        "2 Example Road", "Exampletown", "BT1 1AA")
    assert C._ccni_address("Unit 3, Example Park, Exampletown BT12 3CD")[2] == "BT12 3CD"


def test_company_number_padding():
    assert C._company_number("1234567") == "01234567"
    assert C._company_number("SC123456") == "SC123456"
    assert C._company_number("0") == ""


# ── load_register ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_register_reads_both_registers(tmp_path):
    calls = []
    data_dir = tmp_path / "data"
    async with mock_client(tmp_path, calls) as client:
        found = await C.load_register(client, data_dir)
    ids = [c.id for c in found]
    # best first: strong phrases, then housing+support, then housing-only
    assert ids == ["ccew-1000001", "ccni-100001", "ccew-1000002",
                   "ccew-1000003", "ccew-1000010"]
    top = found[0]
    assert isinstance(top, C.Location)
    assert top.name == "Example Housing Trust"
    assert top.income == 2_500_000 and top.income_year_end == "2026-03-31"
    assert top.website == "https://www.example.org"
    assert top.email == "office@example.org"
    assert top.address == "1 Example Street" and top.city == "Exampleton"
    assert top.postcode == "EX1 1AA"
    assert top.company_number == "01234567"
    assert top.ownership == "Organisation"
    assert top.services == ["supported housing"]
    assert top.register == "CCEW" and top.charity_number == "1000001"
    assert top.url.endswith("/charity-details/1000001")
    ni = found[1]
    assert ni.register == "CCNI" and ni.postcode == "BT1 1AA" and ni.city == "Exampletown"
    assert ni.income == 350_000 and ni.income_year_end == "2026-03-31"
    assert ni.company_number == "" and "regId=100001" in ni.url
    assert "tenancy sustainment" in ni.match_reason
    # downloads are deleted once read; only the small JSON copies stay
    assert sorted(p.name for p in data_dir.iterdir()) == ["ccew.json", "ccni.json"]


@pytest.mark.asyncio
async def test_min_income_and_min_score(tmp_path):
    calls = []
    async with mock_client(tmp_path, calls) as client:
        small = await C.load_register(client, tmp_path / "d", min_income=0)
    assert "ccew-1000004" in {c.id for c in small}
    # the second call reads the fresh local copies, with no downloads
    calls.clear()
    async with mock_client(tmp_path, calls) as client:
        strong = await C.load_register(client, tmp_path / "d", min_score=3)
        big = await C.load_register(client, tmp_path / "d", min_income=1_000_000)
    assert calls == []
    assert {c.id for c in strong} == {"ccew-1000001", "ccni-100001"}
    assert [c.id for c in big] == ["ccew-1000001"]


@pytest.mark.asyncio
async def test_stale_copy_used_when_download_fails(tmp_path):
    data_dir = tmp_path / "data"
    async with mock_client(tmp_path, []) as client:
        first = await C.load_register(client, data_dir)
    old = time.time() - (C.MAX_AGE_DAYS + 1) * 86400
    for p in data_dir.iterdir():
        os.utime(p, (old, old))
    async with mock_client(tmp_path, [], fail=("blob.core", "charitycommissionni")) as client:
        again = await C.load_register(client, data_dir)
    assert [c.id for c in again] == [c.id for c in first]


@pytest.mark.asyncio
async def test_one_register_down_still_returns_the_other(tmp_path):
    async with mock_client(tmp_path, [], fail=("charitycommissionni",)) as client:
        found = await C.load_register(client, tmp_path / "data")
    assert found and all(c.register == "CCEW" for c in found)
    assert not (tmp_path / "data" / "ccni.json").exists()


@pytest.mark.asyncio
async def test_ccni_html_instead_of_csv_is_an_error(tmp_path):
    page = b"<!DOCTYPE html><html><body>Error</body></html>"
    async with mock_client(tmp_path, [], ccni_body=page) as client:
        found = await C.load_register(client, tmp_path / "data")
    assert all(c.register == "CCEW" for c in found)


@pytest.mark.asyncio
async def test_no_register_and_no_copy_raises(tmp_path):
    async with mock_client(tmp_path, [], fail=("blob.core", "charitycommissionni")) as client:
        with pytest.raises(RuntimeError, match="charity registers"):
            await C.load_register(client, tmp_path / "data")
