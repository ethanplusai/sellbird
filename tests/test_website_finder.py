"""FIND WEBSITE — companies a register listed without a website.

Fictional companies and mocked HTTP only. What matters most: a wrong website
is never saved (it would mean emailing a stranger), directories never count,
and nothing is searched or spent without a key and inside the caps.
"""

import json
import logging
import os
import tempfile

import httpx
import pytest
import pytest_asyncio

from sellbird.collectors import website_finder as W
from sellbird.collectors.cqc import Location
from sellbird.config import WebsiteFinderConfig
from sellbird.models import Company
from sellbird.signals import SIGNAL_CATALOG, seed_signal_catalog
from sellbird.state import StateManager

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    W._warned.clear()

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(W.asyncio, "sleep", no_sleep)


class Cfg:
    def __init__(self, **kw):
        self.website_finder = WebsiteFinderConfig(**kw)


SERPER_ENV = {"serper_api_key": "sk-test-serper-123456"}

GLENHOLM = Location(
    id="CS2099000001", name="Glenholm Lodge - Care Home Service",
    phone="01234 567890", address="1 Example Road", city="Exampletown",
    postcode="EX1 2MP", provider_name="Glenholm Care Partners Ltd",
    services=["care home"])


class FixedLookup(W.RegisterLookup):
    def __init__(self, records):
        super().__init__()
        self._index = {"care_inspectorate": records, "companies_house": {}}


def page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def serper_answer(links):
    return {"organic": [{"title": t, "link": u, "position": i + 1}
                        for i, (t, u) in enumerate(links)]}


def clients(search_links, pages, seen=None):
    seen = seen if seen is not None else []

    def search(request):
        seen.append(json.loads(request.content)["q"])
        return httpx.Response(200, json=serper_answer(search_links))

    def site(request):
        html = pages.get(str(request.url))
        if html is None:
            return httpx.Response(404, text="")
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    return (httpx.AsyncClient(transport=httpx.MockTransport(search)),
            httpx.AsyncClient(transport=httpx.MockTransport(site)))


async def add(state, name, ext, location="Exampletown", phone=""):
    return await state.add_company(Company(name=name, external_id=ext,
                                           location=location, phone=phone,
                                           source="discover:care_inspectorate"))


# ── Scoring ─────────────────────────────────────────────────────────────

def ev(**kw):
    base = dict(company_id="c1", name="Glenholm Lodge", town="Exampletown",
                postcode="EX1 2MP", phone="01234 567890", numbers=("CS2099000001",))
    base.update(kw)
    return W.Evidence(**base)


def test_name_and_postcode_is_a_sure_match():
    conf, reasons = W.score_page(ev(), "example-care.co.uk",
                                 "Welcome to Glenholm Lodge, 1 Example Road, EX12MP")
    assert conf == 0.95 and "postcode" in reasons and "name" in reasons


def test_name_and_phone_in_international_form():
    conf, _ = W.score_page(ev(postcode=""), "example-care.co.uk",
                           "Glenholm Lodge. Call +44 (0)1234 567 890")
    assert conf == 0.95


def test_registration_number_counts():
    conf, reasons = W.score_page(ev(postcode="", phone=""), "example-care.co.uk",
                                 "Glenholm Lodge is registered with the Care "
                                 "Inspectorate (CS2099000001).")
    assert conf == 0.95 and "number CS2099000001" in reasons


def test_name_alone_is_not_enough():
    conf, _ = W.score_page(ev(), "somewhere-else.co.uk", "Glenholm Lodge reviews")
    assert conf < W.MIN_CONFIDENCE


def test_name_with_matching_domain_and_town_is_enough():
    conf, _ = W.score_page(ev(postcode="", phone="", numbers=()), "glenholmlodge.co.uk",
                           "Glenholm Lodge, a care home in Exampletown")
    assert conf == 0.8


def test_generic_name_never_matches_by_name():
    assert W.distinctive_tokens("Care at Home Services Ltd") == []
    conf, _ = W.score_page(ev(name="Care at Home Services Ltd", postcode="", phone="",
                              numbers=()), "careathomeservices.co.uk",
                           "Care at Home Services Ltd in Exampletown")
    assert conf < W.MIN_CONFIDENCE


def test_ambiguous_tie_is_left_alone():
    a = W.Match("one.example.co.uk", "https://one.example.co.uk", 0.95)
    b = W.Match("two.example.co.uk", "https://two.example.co.uk", 0.95)
    best, why = W.choose([a, b])
    assert best is None and why.startswith("ambiguous")
    best, _ = W.choose([a, W.Match("three.example.co.uk", "", 0.8)])
    assert best is a


@pytest.mark.parametrize("domain", [
    "carehome.co.uk", "www.yell.com", "find-and-update.company-information.service.gov.uk",
    "careinspectorate.com", "example.gov.uk", "facebook.com", "endole.co.uk",
])
def test_directories_never_count(domain):
    assert W.is_directory(W.normalize_domain(domain))


def test_own_site_is_not_a_directory():
    assert not W.is_directory("glenholmlodge.co.uk")


# ── The run ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finds_and_saves_the_right_site(state):
    cid = await add(state, GLENHOLM.name, "care_inspectorate:CS2099000001")
    queries = []
    search, pages = clients(
        [("Glenholm Lodge - carehome.co.uk", "https://www.carehome.co.uk/glenholm"),
         ("Glenholm Lodge | Home", "https://www.glenholmlodge.example.co.uk/")],
        {"https://www.glenholmlodge.example.co.uk/": page(
            "Glenholm Lodge", "Glenholm Lodge, 1 Example Road, Exampletown EX1 2MP")},
        queries)
    report = await W.run_website_finder(
        state, Cfg(), env=SERPER_ENV, search_client=search, page_client=pages,
        lookup=FixedLookup({"CS2099000001": GLENHOLM}))
    assert report.found == 1 and report.searched == 1 and report.provider == "serper"
    assert queries == ['"Glenholm Lodge - Care Home Service" Exampletown']
    company = await state.get_company(cid)
    assert company.domain == "glenholmlodge.example.co.uk"
    rows = await state.get_observations(company_id=cid, signal_code="WEBSITE_FOUND")
    assert rows and rows[0]["value_num"] == 0.95
    no_site = await state.get_observations(company_id=cid, signal_code="NO_WEBSITE")
    assert no_site and no_site[0]["value_num"] == 0.0
    # Now it is a company the profile stage and the scout can work on.
    assert await state.count_companies_needing_website() == 0
    assert any(c["id"] == cid for c in await state.companies_needing_profile())


@pytest.mark.asyncio
async def test_unsure_leaves_the_company_alone_and_moves_on(state):
    cid = await add(state, GLENHOLM.name, "care_inspectorate:CS2099000001")
    search, pages = clients(
        [("Lodges in Exampletown", "https://otherlodge.example.com/")],
        {"https://otherlodge.example.com/": page("Other", "Glenholm Lodge is nearby")})
    report = await W.run_website_finder(
        state, Cfg(), env=SERPER_ENV, search_client=search, page_client=pages,
        lookup=FixedLookup({"CS2099000001": GLENHOLM}))
    assert report.found == 0 and report.unsure == 1
    assert (await state.get_company(cid)).domain == ""
    # Checked, so the next batch takes the next company, not this one again.
    assert await state.count_companies_needing_website() == 0


@pytest.mark.asyncio
async def test_contact_page_supplies_the_postcode(state):
    await add(state, GLENHOLM.name, "care_inspectorate:CS2099000001")
    home = "https://glenholm.example.org/"
    search, pages = clients(
        [("Glenholm Lodge", home)],
        {home: page("Welcome", 'Glenholm Lodge <a href="/contact-us">Contact</a>'),
         "https://glenholm.example.org/contact-us": page("Contact", "Find us at EX1 2MP")})
    report = await W.run_website_finder(
        state, Cfg(), env=SERPER_ENV, search_client=search, page_client=pages,
        lookup=FixedLookup({"CS2099000001": GLENHOLM}))
    assert report.found == 1


@pytest.mark.asyncio
async def test_domain_of_another_company_is_not_taken(state):
    await state.add_company(Company(name="Glenholm Care Partners Ltd",
                                    domain="glenholm.example.org"))
    cid = await add(state, GLENHOLM.name, "care_inspectorate:CS2099000001")
    home = "https://glenholm.example.org/"
    search, pages = clients([("Glenholm", home)],
                            {home: page("Glenholm", "Glenholm Lodge EX1 2MP")})
    report = await W.run_website_finder(
        state, Cfg(), env=SERPER_ENV, search_client=search, page_client=pages,
        lookup=FixedLookup({"CS2099000001": GLENHOLM}))
    assert report.shared == 1 and (await state.get_company(cid)).domain == ""


@pytest.mark.asyncio
async def test_no_key_does_nothing_and_says_so_once(state, caplog):
    await add(state, "Glenholm Lodge", "care_inspectorate:CS2099000001")

    def boom(request):
        raise AssertionError("no request may be made without a key")
    search = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    with caplog.at_level(logging.INFO, logger="sellbird.collectors.website_finder"):
        for _ in range(3):
            report = await W.run_website_finder(state, Cfg(), env={},
                                                search_client=search, page_client=search)
            assert report.stopped == "no search key" and report.searched == 0
    lines = [r for r in caplog.records if "no search key is set" in r.getMessage()]
    assert len(lines) == 1 and "1 companies have no website" in lines[0].getMessage()


@pytest.mark.asyncio
async def test_placeholder_key_counts_as_no_key(state):
    await add(state, "Glenholm Lodge", "care_inspectorate:CS2099000001")
    report = await W.run_website_finder(state, Cfg(),
                                        env={"serper_api_key": "your_api_key_here"})
    assert report.stopped == "no search key"


@pytest.mark.asyncio
async def test_free_provider_preferred():
    env = {"serper_api_key": "sk-test-serper-123456", "tinyfish_api_key": "tf-test-123456"}
    assert W.pick_provider(env).key == "tinyfish"
    assert W.pick_provider(env, "serper").key == "serper"
    assert W.pick_provider({}) is None


@pytest.mark.asyncio
async def test_daily_search_cap(state):
    for i in range(5):
        await add(state, f"Fernbrae House {i}", f"care_inspectorate:CS20990000{i:02d}")
    search, pages = clients([], {})
    report = await W.run_website_finder(
        state, Cfg(max_daily_searches=3, batch_size=10), env=SERPER_ENV,
        search_client=search, page_client=pages, lookup=FixedLookup({}))
    assert report.searched == 3
    again = await W.run_website_finder(
        state, Cfg(max_daily_searches=3, batch_size=10), env=SERPER_ENV,
        search_client=search, page_client=pages, lookup=FixedLookup({}))
    assert again.stopped == "daily cap reached" and again.searched == 0


@pytest.mark.asyncio
async def test_dollar_cap_limits_paid_searches(state):
    for i in range(5):
        await add(state, f"Fernbrae House {i}", f"care_inspectorate:CS20990000{i:02d}")
    search, pages = clients([], {})
    # Serper is estimated at $0.001 a search: $0.002 buys two.
    report = await W.run_website_finder(
        state, Cfg(max_daily_spend=0.002), env=SERPER_ENV,
        search_client=search, page_client=pages, lookup=FixedLookup({}))
    assert report.searched == 2 and report.actual_cost <= 0.002 + 1e-9


@pytest.mark.asyncio
async def test_dry_run_estimates_without_searching(state):
    await add(state, "Fernbrae House", "care_inspectorate:CS2099000002")

    def boom(request):
        raise AssertionError("dry run must not search")
    search = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    report = await W.run_website_finder(state, Cfg(), env=SERPER_ENV, dry_run=True,
                                        search_client=search, page_client=search)
    assert report.searched == 0 and report.estimated_cost == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_kill_switch_stops_it(state):
    await add(state, "Fernbrae House", "care_inspectorate:CS2099000002")
    await state.set_setting("discovery_paused", "1")
    report = await W.run_website_finder(state, Cfg(), env=SERPER_ENV)
    assert report.stopped == "paused"


@pytest.mark.asyncio
async def test_disabled_in_config(state):
    report = await W.run_website_finder(state, Cfg(enabled=False), env=SERPER_ENV)
    assert report.stopped == "disabled"


@pytest.mark.asyncio
async def test_refused_key_stops_without_marking_companies(state):
    await add(state, "Fernbrae House", "care_inspectorate:CS2099000002")
    search = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"message": "Unauthorized"})))
    report = await W.run_website_finder(state, Cfg(), env=SERPER_ENV,
                                        search_client=search, page_client=search,
                                        lookup=FixedLookup({}))
    assert report.stopped == "search failed"
    assert await state.count_companies_needing_website() == 1


@pytest.mark.asyncio
async def test_companies_house_cache_supplies_evidence(tmp_path):
    folder = tmp_path / "companies_house_sic"
    folder.mkdir()
    loc = Location(id="ch-SC999001", name="Fernbrae Support Ltd", city="Exampleton",
                   postcode="EX9 9ZZ", company_number="SC999001")
    from dataclasses import asdict
    (folder / "abc.json").write_text(json.dumps([asdict(loc)]), encoding="utf-8")
    lookup = W.RegisterLookup(data_root=tmp_path)
    ev = await lookup.evidence(None, {"id": "c9", "name": "Fernbrae Support Ltd",
                                      "external_id": "companies_house:SC999001"})
    assert ev.postcode == "EX9 9ZZ" and "SC999001" in ev.numbers and ev.town == "Exampleton"
