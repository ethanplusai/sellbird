"""Council contract award notices -> the suppliers that deliver support.

Every council, supplier and person here is invented, and the notices are
built by hand in the shapes Contracts Finder, Find a Tender and Public
Contracts Scotland publish, not copied.
"""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from sellbird.collectors import contracts as C
from sellbird.collectors.cqc import Location

TODAY = date(2026, 11, 19)


# ── fixtures ─────────────────────────────────────────────────────────────


def cf_hit(nid, title, desc="", cpv="85000000", sub=False):
    return {"score": 1, "item": {"id": nid, "title": title, "description": desc,
                                 "cpvCodes": cpv, "isSubNotice": sub,
                                 "organisationName": "Exampleshire County Council"}}


def cf_notice(nid, title, buyer, awards, desc="", cpv="85000000"):
    return {"id": nid,
            "notice": {"id": nid, "title": title, "description": desc,
                       "organisationName": buyer, "publishedDate": "2025-03-04T10:00:00Z"},
            "cpvCodes": [{"code": cpv}],
            "awards": awards}


def cf_award(name, address="", ref_type="NONE", ref="", value=0,
             awarded="2025-03-01T00:00:00Z"):
    return {"supplierName": name, "supplierAddress": address, "referenceType": ref_type,
            "reference": ref, "value": value, "supplierAwardedValue": None,
            "awardedDate": awarded}


CF_NOTICES = {
    "cf-1": cf_notice(
        "cf-1", "Floating Support Service - AWARD", "Exampleshire County Council",
        [cf_award("Northbrook Housing Trust Ltd",
                  "Unit 4, Mill Lane, Northbrook, United Kingdom, NB1 2XY",
                  "COMPANIES_HOUSE", "1234567", 450000, "2025-03-01T00:00:00Z")]),
    "cf-2": cf_notice(
        "cf-2", "Young People's Supported Accommodation", "Westford Borough Council",
        [cf_award("Northbrook Housing Trust", value=120000,
                  awarded="2023-06-15T00:00:00Z"),
         cf_award("Riverside Steps CIC", "2 Quay Road, Westford, WF3 9ZZ",
                  awarded="2023-06-15T00:00:00Z"),
         cf_award("Various")]),
    # Matched by the keyword search on "housing support", but it's a repairs job.
    "cf-3": cf_notice(
        "cf-3", "Roof repairs to council housing stock", "Westford Borough Council",
        [cf_award("Example Roofing Ltd")], desc="Housing support beams", cpv="45261000"),
    # Published in the window, but awarded long before it.
    "cf-4": cf_notice(
        "cf-4", "Floating support (variation)", "Westford Borough Council",
        [cf_award("Old Example Support Ltd", awarded="2019-01-10T00:00:00Z")]),
}
CF_SEARCH_HITS = [
    cf_hit("cf-1", "Floating Support Service - AWARD"),
    cf_hit("cf-2", "Young People's Supported Accommodation"),
    cf_hit("cf-3", "Roof repairs to council housing stock", "Housing support beams",
           cpv="45261000"),
    cf_hit("cf-sub", "Floating support lot 2", sub=True),
    cf_hit("cf-4", "Floating support (variation)"),
]


def ocds_release(rid, title, buyer, suppliers, cpv="85312000", value=90000,
                 when="2026-10-02T00:00:00Z", desc=""):
    parties = [{"id": "b1", "name": buyer, "roles": ["buyer"]}]
    award_suppliers = []
    for i, sup in enumerate(suppliers, 2):
        pid = f"org-{i}"
        parties.append({"id": pid, "name": sup["name"], "roles": ["supplier"],
                        "identifier": sup.get("identifier", {"legalName": sup["name"]}),
                        "address": sup.get("address", {}),
                        "contactPoint": sup.get("contact", {}),
                        "details": {"url": sup.get("url", "http://")}})
        award_suppliers.append({"id": pid, "name": sup["name"]})
    return {"id": rid, "ocid": f"ocds-x-{rid}", "date": when, "tag": ["award", "contract"],
            "tender": {"title": title, "description": desc,
                       "classification": {"scheme": "CPV", "id": cpv}},
            "parties": parties, "buyer": {"id": "b1", "name": buyer},
            "awards": [{"id": f"{rid}-1", "status": "active", "suppliers": award_suppliers,
                        "date": when}],
            "contracts": [{"id": f"{rid}-1", "awardID": f"{rid}-1",
                           "value": {"amount": value, "currency": "GBP"},
                           "dateSigned": when}]}


FTS_RELEASES = [
    ocds_release("100001-2026", "Housing Related Support for Single Homeless People",
                 "Eastmere City Council",
                 [{"name": "Northbrook Housing Trust Limited",
                   "identifier": {"scheme": "GB-COH", "id": "01234567"},
                   "address": {"streetAddress": "Unit 4, Mill Lane", "locality": "Northbrook",
                               "postalCode": "NB12XY"},
                   "contact": {"email": "bids@northbrook.example"},
                   "url": "www.northbrook.example"}], value=2_400_000),
    ocds_release("100002-2026", "Office cleaning", "Eastmere City Council",
                 [{"name": "Shiny Example Cleaners Ltd"}], cpv="90910000"),
    # Weak wording counts only with a social work CPV.
    ocds_release("100003-2026", "Tenancy support for care leavers", "Eastmere City Council",
                 [{"name": "Mr Alan Example t/a Example Tenancy Help"}], cpv="85311000"),
    ocds_release("100004-2026", "Tenancy support software licence", "Eastmere City Council",
                 [{"name": "Example Software Ltd"}], cpv="48000000"),
]

PCS_RELEASES = [
    ocds_release("rls-4-OCT000111", "Supported Housing for Young People",
                 "Glenexample Council",
                 [{"name": "Thistle Example Housing Association",
                   "address": {"streetAddress": "1 Example Street", "locality": "Glenexample",
                               "postalCode": "GX1 1AA"}}],
                 when="2026-10-10T00:00:00Z"),
]


def handler_factory(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        url = str(request.url)
        if url.startswith(C.CF_SEARCH):
            body = json.loads(request.content)
            assert body["searchCriteria"]["statuses"] == ["Awarded"]
            return httpx.Response(200, json={"hitCount": len(CF_SEARCH_HITS),
                                             "noticeList": CF_SEARCH_HITS})
        if "get_published_notice" in url:
            nid = url.rsplit("/", 1)[-1]
            return httpx.Response(200, json=CF_NOTICES[nid])
        if url.startswith(C.FTS_API):
            if "updatedFrom=2026-11" in url:
                return httpx.Response(200, json={"releases": [], "links": {}})
            if "cursor=" in url:
                return httpx.Response(200, json={"releases": FTS_RELEASES[2:], "links": {}})
            if "updatedFrom=2026-10" in url:
                return httpx.Response(200, json={
                    "releases": FTS_RELEASES[:2],
                    "links": {"next": C.FTS_API + "?stages=award&cursor=abc"}})
            return httpx.Response(200, json={"releases": [], "links": {}})
        if url.startswith(C.PCS_API):
            if "dateFrom=10-2026" in url:
                return httpx.Response(200, json={"releases": PCS_RELEASES})
            return httpx.Response(200, json={"releases": []})
        if url.startswith(C.S2W_API):
            return httpx.Response(500, text="server error")
        return httpx.Response(404)
    return handler


async def _load(tmp_path, calls=None, **kw):
    calls = [] if calls is None else calls
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_factory(calls))) as c:
        return await C.load_register(c, tmp_path, today=TODAY, fts_months_per_run=2, **kw)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(C, "CF_NOTICE_DELAY", 0)


# ── matching ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,cpvs,kind", [
    ("Floating Support Service", [], "floating support"),
    ("Housing-related support for older people", [], "housing-related support"),
    ("Supported accommodation for young people", [], "supported accommodation"),
    ("Exempt accommodation review", [], "exempt accommodation"),
    ("Tenancy support for care leavers", ["85311000"], "tenancy support"),
    ("Tenancy support software", ["48000000"], ""),
    ("Office cleaning", ["85312000"], ""),
    ("Floating&amp;nbsp;support", [], "floating support"),
])
def test_classify(text, cpvs, kind):
    assert C.classify(text, cpvs) == kind


def test_floating_kinds_map_to_floating_support():
    assert C.services_for_kind("tenancy sustainment") == ["floating support"]
    assert C.services_for_kind("exempt accommodation") == ["supported housing"]


@pytest.mark.parametrize("raw,expected", [
    ("1234567", "01234567"), ("SC123456", "SC123456"), ("sc 12345", "SC012345"),
    ("", ""), ("not a number", ""),
])
def test_company_number(raw, expected):
    assert C.company_number(raw) == expected


def test_name_key_ignores_suffixes_and_trading_names():
    assert C.name_key("The Northbrook Housing Trust Ltd") == "northbrook-housing-trust"
    assert C.name_key("Northbrook Housing Trust Limited") == "northbrook-housing-trust"
    assert C.name_key("Example & Co t/a Something Else") == "example-and"


@pytest.mark.parametrize("name,ownership", [
    ("Mr Alan Example t/a Example Tenancy Help", "Individual"),
    ("Alan Example t/a Example Help", "Individual"),
    ("Example Charity t/a Example Support", "Organisation"),
    ("Northbrook Housing Trust", "Organisation"),
])
def test_ownership(name, ownership):
    assert C.ownership_for(name) == ownership


def test_split_address():
    assert C.split_address("Unit 4, Mill Lane, Northbrook, United Kingdom, NB1 2XY") == (
        "Unit 4, Mill Lane", "Northbrook", "NB1 2XY")
    assert C.split_address("") == ("", "", "")


# ── reading notices ──────────────────────────────────────────────────────


def test_cf_notice_skips_junk_suppliers_and_reads_company_numbers():
    awards = C.awards_from_cf_notice(CF_NOTICES["cf-2"])
    assert [a.supplier for a in awards] == ["Northbrook Housing Trust", "Riverside Steps CIC"]
    first = C.awards_from_cf_notice(CF_NOTICES["cf-1"])[0]
    assert first.company_number == "01234567"
    assert first.postcode == "NB1 2XY" and first.city == "Northbrook"
    assert first.title == "Floating Support Service"
    assert first.url.endswith("/Notice/cf-1")


def test_cf_notice_that_does_not_match_gives_nothing():
    assert C.awards_from_cf_notice(CF_NOTICES["cf-3"]) == []


def test_ocds_release_reads_supplier_details():
    [a] = C.awards_from_release(FTS_RELEASES[0], "Find a Tender")
    assert a.company_number == "01234567"
    assert a.postcode == "NB1 2XY"
    assert a.website == "https://www.northbrook.example"
    assert a.email == "bids@northbrook.example"
    assert a.value == 2_400_000 and a.awarded == "2026-10-02"
    assert a.url == C.FTS_NOTICE_URL.format(id="100001-2026")
    assert C.awards_from_release(FTS_RELEASES[1], "Find a Tender") == []


def test_pcs_release_links_the_public_notice():
    [a] = C.awards_from_release(PCS_RELEASES[0], "Public Contracts Scotland")
    assert a.url == C.PCS_NOTICE_URL.format(id="OCT000111")
    assert a.website == ""        # "http://" alone is no website


# ── putting it together ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_register_merges_suppliers_across_sources(tmp_path):
    suppliers = await _load(tmp_path)
    by_name = {s.name: s for s in suppliers}
    assert set(by_name) == {"Northbrook Housing Trust Limited", "Riverside Steps CIC",
                            "Mr Alan Example t/a Example Tenancy Help",
                            "Thistle Example Housing Association"}
    trust = by_name["Northbrook Housing Trust Limited"]
    assert isinstance(trust, Location)
    assert trust.id == "cf-ch01234567"
    assert trust.company_number == "01234567"
    assert trust.contract_count == 3                      # 2 on Contracts Finder, 1 on FTS
    assert trust.contract_buyer == "Eastmere City Council"   # the latest award
    assert trust.contract_source == "Find a Tender"
    assert set(trust.contract_buyers) == {"Eastmere City Council",
                                          "Exampleshire County Council",
                                          "Westford Borough Council"}
    assert sorted(trust.services) == ["floating support", "supported housing"]
    assert trust.ownership == "Organisation"
    assert trust.website == "https://www.northbrook.example"
    assert by_name["Mr Alan Example t/a Example Tenancy Help"].ownership == "Individual"
    assert by_name["Riverside Steps CIC"].services == ["supported housing"]
    assert by_name["Riverside Steps CIC"].id == "cf-riverside-steps"


@pytest.mark.asyncio
async def test_facts_text(tmp_path):
    suppliers = await _load(tmp_path)
    trust = next(s for s in suppliers if s.company_number == "01234567")
    assert trust.facts == (
        "Holds a housing-related support contract with Eastmere City Council worth £2.4m, "
        "awarded October 2026. 3 such contracts in the last 4 years, with 2 other buyers.")
    riverside = next(s for s in suppliers if s.name == "Riverside Steps CIC")
    assert riverside.facts == ("Holds a supported accommodation contract with "
                               "Westford Borough Council, awarded June 2023.")
    assert C.contract_facts(tmp_path)[trust.id] == trust.facts


@pytest.mark.asyncio
async def test_months_and_notices_are_cached(tmp_path):
    calls: list[str] = []
    await _load(tmp_path, calls)
    assert any("get_published_notice" in u for u in calls)
    assert (tmp_path / "fts" / "2026-10.json").exists()
    assert json.loads((tmp_path / "fts" / "2026-10.json").read_text())["complete"] is True
    assert not json.loads((tmp_path / "fts" / "2026-11.json").read_text())["complete"]

    # A fresh suppliers.json is used as it is.
    calls.clear()
    again = await _load(tmp_path, calls)
    assert calls == [] and len(again) == 4

    # Rebuilding reads no finished month and no notice twice.
    (tmp_path / "suppliers.json").unlink()
    calls.clear()
    await _load(tmp_path, calls)
    assert not any("get_published_notice" in u for u in calls)
    assert not any(u.startswith(C.PCS_API) and "dateFrom=10-2026" in u for u in calls)
    assert not any("updatedFrom=2026-10" in u for u in calls)


@pytest.mark.asyncio
async def test_find_a_tender_fills_in_a_few_months_per_run(tmp_path):
    calls: list[str] = []
    await _load(tmp_path, calls, sources=("find_a_tender",))
    months = sorted(p.stem for p in (tmp_path / "fts").glob("*.json"))
    assert months == ["2026-10", "2026-11"]       # fts_months_per_run=2, newest first
    (tmp_path / "suppliers.json").unlink()
    await _load(tmp_path, calls, sources=("find_a_tender",))
    assert len(list((tmp_path / "fts").glob("*.json"))) == 4


@pytest.mark.asyncio
async def test_a_failing_source_is_skipped_and_retried(tmp_path):
    suppliers = await _load(tmp_path)            # Sell2Wales answers 500
    assert suppliers
    assert not (tmp_path / "s2w").exists() or not list((tmp_path / "s2w").glob("*.json"))


@pytest.mark.asyncio
async def test_rate_limited_notice_is_retried(tmp_path, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(C.asyncio, "sleep", no_sleep)
    seen = {"n": 0}

    def handler(request):
        seen["n"] += 1
        if seen["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await C._get_json(c, "https://example.test/x") == {"ok": True}
    assert seen["n"] == 3


@pytest.mark.asyncio
async def test_nothing_readable_and_no_copy_raises(tmp_path):
    def down(request):
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as c:
        with pytest.raises(RuntimeError):
            await C.load_register(c, tmp_path, today=TODAY, sources=("contracts_finder",))


@pytest.mark.asyncio
async def test_search_by_kind_and_place(tmp_path):
    suppliers = await _load(tmp_path)
    floating = C.search(suppliers, "floating support")
    assert {s.name for s in floating} == {"Northbrook Housing Trust Limited",
                                          "Mr Alan Example t/a Example Tenancy Help"}
    assert {s.name for s in C.search(suppliers, "supported housing", "Glenexample")} == {
        "Thistle Example Housing Association"}
    assert {s.name for s in C.search(suppliers, "", "Westford")} == {
        "Northbrook Housing Trust Limited", "Riverside Steps CIC"}


@pytest.mark.asyncio
async def test_scotland_switches_client_after_a_certificate_failure(tmp_path, monkeypatch):
    def refuses(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    calls: list[str] = []
    made = []

    async def fake_client(client, data_dir, url):
        assert url == C.PCS_INTERMEDIATE
        own = httpx.AsyncClient(transport=httpx.MockTransport(handler_factory(calls)))
        made.append(own)
        return own
    monkeypatch.setattr(C, "completed_chain_client", fake_client)
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuses)) as c:
        awards = await C.read_scotland(c, tmp_path, date(2026, 9, 1), TODAY)
    assert len(made) == 1                     # made once, reused for every month
    assert [a.supplier for a in awards] == ["Thistle Example Housing Association"]
    assert made[0].is_closed
