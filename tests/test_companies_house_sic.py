"""Companies House SIC search: fictional companies only, no network."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from harvey.collectors import companies_house_sic as chs
from harvey.collectors.cqc import Location


def _item(number, name="Example Home Help Ltd", sic=("88100",), status="active",
          ctype="ltd", locality="Exampletown", postcode="ex1 1aa"):
    return {
        "company_name": name, "company_number": number, "company_status": status,
        "company_type": ctype, "sic_codes": list(sic), "kind": "search-results#company",
        "registered_office_address": {
            "address_line_1": "1 Example Street", "address_line_2": "Unit 2",
            "locality": locality, "postal_code": postcode, "region": "Example County",
        },
    }


class NoWait(chs.RateLimiter):
    def __init__(self):
        super().__init__()
        self.sleeps = []

        async def sleep(s):
            self.sleeps.append(s)
        self._sleep = sleep


def _reader(tmp_path=None, **kw):
    return chs.CompaniesHouseSic("test-key", cache_dir=tmp_path,
                                 use_cache=tmp_path is not None, limiter=NoWait(), **kw)


def _run(coro):
    return asyncio.run(coro)


# ── mapping ──


def test_location_from_item_fills_the_register_fields():
    loc = chs.location_from_item(_item("1234567", sic=("88100", "87300", "62020")))
    assert isinstance(loc, Location)
    assert loc.id == "ch-01234567"
    assert loc.company_number == "01234567"
    assert loc.ownership == "Organisation"
    assert loc.name == loc.provider_name == "Example Home Help Ltd"
    assert loc.address == "1 Example Street, Unit 2"
    assert loc.city == "Exampletown" and loc.postcode == "EX1 1AA"
    assert loc.services == [chs.SIC_SERVICES["88100"], chs.SIC_SERVICES["87300"]]
    assert loc.website == "" and loc.phone == ""
    assert chs.company_url(loc).endswith("/company/01234567")


@pytest.mark.parametrize("item", [
    _item("01234567", status="dissolved"),
    _item("01234567", ctype="overseas-entity"),
    _item("01234567", sic=("62020",)),
    _item("not a number"),
])
def test_location_from_item_skips_what_does_not_fit(item):
    assert chs.location_from_item(item) is None


def test_llps_and_cios_are_kept():
    assert chs.location_from_item(_item("OC123456", ctype="llp"))
    assert chs.location_from_item(_item("CE012345", ctype="charitable-incorporated-organisation"))


def test_drop_known_matches_numbers_in_any_format():
    found = [chs.location_from_item(_item(n)) for n in ("01234567", "SC123456", "07654321")]
    cqc = [Location(id="1-000", name="Example Care", company_number="1234567"),
           Location(id="1-001", name="No Number")]
    kept = chs.drop_known(found, chs.known_numbers(cqc) | {"sc123456"})
    assert [x.company_number for x in kept] == ["07654321"]


# ── the API ──


def test_search_sends_codes_status_place_and_pages(tmp_path):
    seen = []
    items = [_item(f"{n:08d}") for n in range(1, 8)]

    def handler(request: httpx.Request):
        seen.append(request)
        start = int(request.url.params["start_index"])
        size = int(request.url.params["size"])
        return httpx.Response(200, json={"hits": len(items),
                                         "items": items[start:start + size]})

    reader = _reader()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(chs, "PAGE_SIZE", 3)

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                return await reader.search(c, sic_codes=("88100", "87300", "99999"),
                                           place="Exampletown, Example County")
        out = _run(go())

    assert len(out) == 7 and len(seen) == 3 == reader.requests_made
    p = seen[0].url.params
    assert p["sic_codes"] == "88100,87300"
    assert p["company_status"] == "active"
    assert p["location"] == "Exampletown"
    assert [r.url.params["start_index"] for r in seen] == ["0", "3", "6"]
    assert seen[0].headers["authorization"].startswith("Basic ")


def test_no_results_is_an_empty_list():
    def handler(request):
        return httpx.Response(404, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await _reader().search(c, place="Nowhere")
    assert _run(go()) == []


def test_bad_key_and_missing_key_raise_plainly():
    def handler(request):
        return httpx.Response(401, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await _reader().search(c)
    with pytest.raises(RuntimeError, match="401"):
        _run(go())
    with pytest.raises(RuntimeError, match="COMPANIES_HOUSE_API_KEY"):
        _run(chs.CompaniesHouseSic("", use_cache=False).search())


def test_rate_limited_answer_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json={"hits": 1, "items": [_item("01234567")]})

    reader = _reader()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await reader.search(c)
    assert len(_run(go())) == 1
    assert reader.limiter.sleeps == [5.0]


def test_rate_limiter_holds_calls_inside_the_window():
    now = [0.0]
    slept = []

    async def sleep(s):
        slept.append(s)
        now[0] += s

    lim = chs.RateLimiter(limit=2, window=10.0, clock=lambda: now[0], sleep=sleep)

    async def go():
        for _ in range(3):
            await lim.wait()
    _run(go())
    assert len(slept) == 1 and slept[0] == pytest.approx(10.01)


def test_complete_answers_are_cached_and_cut_ones_are_not(tmp_path):
    calls = []
    items = [_item(f"{n:08d}") for n in range(1, 6)]

    def handler(request):
        calls.append(1)
        start = int(request.url.params["start_index"])
        size = int(request.url.params["size"])
        return httpx.Response(200, json={"hits": 5, "items": items[start:start + size]})

    async def go(limit):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await _reader(tmp_path).search(c, place="Exampletown", limit=limit)

    assert len(_run(go(2))) == 2          # cut short: not cached
    assert len(_run(go(100))) == 5        # complete: cached
    n = len(calls)
    assert len(_run(go(100))) == 5 and len(calls) == n
    assert len(_run(go(3))) == 3 and len(calls) == n


def test_find_care_companies_drops_regulator_numbers(tmp_path):
    def handler(request):
        return httpx.Response(200, json={"hits": 2, "items": [
            _item("01234567"), _item("07654321", name="Example Companions Ltd")]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await chs.find_care_companies("test-key", place="Exampletown",
                                                 known={"1234567"}, client=c,
                                                 cache_dir=tmp_path)
    out = _run(go())
    assert [x.name for x in out] == ["Example Companions Ltd"]


def test_an_answer_too_big_to_page_is_split_by_incorporation_date():
    seen = []

    def handler(request):
        p = request.url.params
        seen.append(dict(p))
        assert int(p["start_index"]) + int(p["size"]) <= chs.MAX_RESULTS
        if "incorporated_from" not in p:
            return httpx.Response(200, json={"hits": chs.MAX_RESULTS + 1,
                                             "items": [_item("09999999")]})
        number = "0" + p["incorporated_from"].replace("-", "")[1:8]
        return httpx.Response(200, json={"hits": 1, "items": [_item(number)]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await _reader().search(c)
    out = _run(go())
    assert len(out) == 2 and len(seen) == 3
    halves = [(s["incorporated_from"], s["incorporated_to"]) for s in seen[1:]]
    assert halves[0][0] == chs.EARLIEST.isoformat()
    assert halves[0][1] < halves[1][0]
    assert "09999999" not in {x.company_number for x in out}
