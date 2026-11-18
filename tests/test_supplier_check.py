"""icp.exclude_if_site_mentions: a company whose own site names the brand is
never written to or emailed."""

import asyncio
import json
import os
import tempfile
from types import SimpleNamespace

import httpx

from sellbird.agents.sender import STOP_STATUSES
from sellbird.collectors.supplier_check import (
    CACHE_KEY, candidate_links, check_site, excluded_supplier, find_mention,
)
from sellbird.config import ICPConfig
from sellbird.state import StateManager


def _client(pages: dict[str, str], seen: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        if url in pages:
            return httpx.Response(200, text=pages[url])
        return httpx.Response(404, text="")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_find_mention_whole_word_text_links_and_images():
    assert find_mention("<p>Our kit is by Acmekit this season</p>", ["acmekit"])[0] == "acmekit"
    assert find_mention('<a href="https://www.acmekit.example/uk/shop">Shop</a>', ["acmekit"])
    assert find_mention('<img src="/img/acmekit-logo.png">', ["acmekit"])
    assert find_mention('<a href="https://acmekitstore.example/club">Club shop</a>', ["acmekit"])
    assert find_mention("Visit the acmekitchen blog", ["acmekit"]) is None
    assert find_mention("Kit by another brand", ["acmekit"]) is None
    assert find_mention("Acmekit kit", []) is None
    assert find_mention("", ["acmekit"]) is None


def test_candidate_links_same_site_kit_pages_only():
    html = ('<a href="/club-shop">Club shop</a><a href="/news">News</a>'
            '<a href="https://other.example/kit">Kit</a><a href="/sponsors">Our sponsors</a>')
    links = candidate_links(html, "https://club.example/")
    assert links == ["https://club.example/club-shop", "https://club.example/sponsors"]


def test_check_site_finds_mention_on_kit_page():
    pages = {
        "https://club.example/": '<a href="/kit">Kit</a>',
        "https://club.example/kit": "<p>Order your Acmekit home shirt here</p>",
    }

    async def go():
        async with _client(pages) as c:
            return await check_site("https://club.example/", ["acmekit"], client=c)
    hit = asyncio.run(go())
    assert hit["brand"] == "acmekit"
    assert hit["url"] == "https://club.example/kit"
    assert "Acmekit" in hit["evidence"]


def test_check_site_clear_and_unreadable():
    async def go(pages):
        async with _client(pages) as c:
            return await check_site("https://club.example/", ["acmekit"], client=c)
    assert asyncio.run(go({"https://club.example/": "<p>Welcome</p>"})) == {}
    assert asyncio.run(go({})) is None      # 404: no verdict


def test_excluded_supplier_caches_verdict_and_retries_unreadable():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            state = StateManager(os.path.join(tmp, "t.db"))
            await state.init_db()
            company = SimpleNamespace(id="c1", website="https://club.example/", domain="club.example")
            seen: list = []
            pages = {"https://club.example/": "<p>Kit supplier: Acmekit</p>"}
            async with _client(pages, seen) as c:
                first = await excluded_supplier(state, company, ["acmekit"], client=c)
                second = await excluded_supplier(state, company, ["acmekit"], client=c)
            assert first and second and first["brand"] == "acmekit"
            assert len(seen) == 1                      # cached after the first read
            cached = json.loads(await state.get_setting(CACHE_KEY + "c1"))
            assert cached["hit"]["brand"] == "acmekit"

            other = SimpleNamespace(id="c2", website="https://down.example/", domain="down.example")
            async with _client({}) as c:
                assert await excluded_supplier(state, other, ["acmekit"], client=c) is None
            assert await state.get_setting(CACHE_KEY + "c2") == ""   # not cached

            assert await excluded_supplier(state, company, [], client=None) is None
    asyncio.run(go())


def test_config_default_and_excluded_stops_sequences():
    base = dict(industries=["football club"], titles=["secretary"],
                geography=["Example"], company_size="1-50")
    assert ICPConfig(**base).exclude_if_site_mentions == []
    assert ICPConfig(**base, exclude_if_site_mentions=["acmekit"]).exclude_if_site_mentions == ["acmekit"]
    assert "excluded" in STOP_STATUSES


def test_writer_and_sender_mark_prospect_excluded(monkeypatch):
    from sellbird.agents.sender import Sender
    from sellbird.agents.writer import Writer
    import sellbird.collectors.supplier_check as sc

    hit = {"brand": "acmekit", "url": "https://club.example/kit", "evidence": "Acmekit kit"}

    async def fake_excluded(state, company, brands, client=None):
        return hit if company.id == "c1" else None
    monkeypatch.setattr(sc, "excluded_supplier", fake_excluded)

    class FakeState:
        def __init__(self):
            self.statuses, self.actions = {}, []

        async def get_company(self, cid):
            return SimpleNamespace(id=cid, website="https://club.example/", domain="club.example")

        async def update_prospect_status(self, pid, status):
            self.statuses[pid] = status

        async def log_action(self, **kw):
            self.actions.append(kw)

    state = FakeState()
    config = SimpleNamespace(icp=SimpleNamespace(exclude_if_site_mentions=["acmekit"]))
    me = SimpleNamespace(state=state, config=config)
    p1 = SimpleNamespace(id="p1", company_id="c1", company="Example FC")
    p2 = SimpleNamespace(id="p2", company_id="c2", company="Other FC")

    assert asyncio.run(Writer._exclude_for_supplier(me, p1, ["acmekit"])) is True
    assert asyncio.run(Writer._exclude_for_supplier(me, p2, ["acmekit"])) is False
    assert state.statuses == {"p1": "excluded"}
    assert state.actions[0]["action_type"] == "excluded_supplier"

    state.statuses.clear()
    assert asyncio.run(Sender._excluded_supplier(me, p1)) == hit
    assert asyncio.run(Sender._excluded_supplier(me, p2)) is None
    assert state.statuses == {"p1": "excluded"}
    me.config.icp.exclude_if_site_mentions = []
    assert asyncio.run(Sender._excluded_supplier(me, p1)) is None
