"""AI/ML API web search: the discovery provider and Scout's backend.

The key is a bearer token for a paid balance, so most of these pin down
where it may go: tools.aimlapi.com, and no other host, redirects included.
"""

from types import SimpleNamespace

import httpx
import pytest

import sellbird.agents.scout as scout_mod
from sellbird.agents.scout import Scout
from sellbird.collectors import discover as D
from sellbird.config import EnvConfig, load_env

KEY = "test-aimlapi-key"

RUN = {
    "id": "run_1",
    "tool": "web/search",
    "status": "completed",
    "is_error": False,
    "output": {"results": [
        {"url": "https://www.summitroofing.example/denver", "title": "Summit Roofing | Denver Roofer",
         "snippet": "Roof repair in Denver"},
        {"title": "No URL", "snippet": "skipped"},
        "not an object",
        {"url": "https://directory.example/roofers", "title": "Roofers near you", "snippet": "A list"},
    ]},
    "cost": {"quoted_usd": 0.0091, "actual_usd": 0.0091},
}


def _client(handler, follow_redirects=True):
    # follow_redirects=True on purpose: the shared Discover client follows them.
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             follow_redirects=follow_redirects)


def test_provider_is_registered_but_not_the_default():
    assert "aimlapi" in D.PROVIDERS
    assert D.DEFAULT_PROVIDER == "osm"
    provider = D.PROVIDERS["aimlapi"]
    assert provider.kind == "serp" and provider.rank_codes == ()
    menu = {m["key"]: m for m in D.provider_menu({"aimlapi_api_key": KEY})}
    assert menu["aimlapi"]["configured"] is True
    assert menu["aimlapi"]["env_keys"] == ["AIMLAPI_API_KEY"]
    assert {m["key"]: m for m in D.provider_menu({})}["aimlapi"]["configured"] is False


def test_startup_and_hiring_presets_run_on_it():
    assert D.preset_problem("yc", "aimlapi") is None


def test_env_reads_the_key(monkeypatch):
    monkeypatch.setenv("AIMLAPI_API_KEY", "  " + KEY + "  ")
    assert load_env().aimlapi_api_key == KEY
    assert EnvConfig().aimlapi_api_key == ""


def test_estimate_follows_the_per_result_price():
    p = D.AIMLAPISearch()
    # depth 30 is capped at 20 results: 20 x $0.0013
    assert p.estimate([D.DiscoveryQuery(term="roofer", depth=30)]) == pytest.approx(0.026)
    # small depths pay the minimum
    assert p.estimate([D.DiscoveryQuery(term="roofer", depth=3)]) == pytest.approx(0.0091)


@pytest.mark.asyncio
async def test_fetch_sends_the_key_to_tools_aimlapi_and_maps_results():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=RUN)

    provider = D.AIMLAPISearch()
    async with _client(handler) as client:
        out = await provider.fetch(client, {"aimlapi_api_key": KEY},
                                   D.DiscoveryQuery(term="roofer", location="Denver", depth=30))

    (request,) = seen
    assert request.method == "POST"
    assert request.url.host == "tools.aimlapi.com"
    assert request.url.scheme == "https"
    assert request.url.path == "/v1/tools/run"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    import json
    body = json.loads(request.content)
    assert body == {"tool": "web/search", "input": {"query": "roofer Denver", "max_results": 20}}

    assert [b.domain for b in out] == ["summitroofing.example", "directory.example"]
    assert out[0].rank is None
    assert out[0].provider == "aimlapi"
    assert out[0].keyword == "roofer Denver"
    assert provider.last_cost == pytest.approx(0.0091)


@pytest.mark.asyncio
async def test_redirect_never_carries_the_key_to_another_host():
    hosts = []

    def handler(request):
        hosts.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "tools.aimlapi.com":
            return httpx.Response(307, headers={"Location": "https://collector.example/v1/tools/run"})
        return httpx.Response(200, json=RUN)

    async with _client(handler) as client:
        with pytest.raises(RuntimeError, match="redirect"):
            await D.AIMLAPISearch().fetch(client, {"aimlapi_api_key": KEY},
                                          D.DiscoveryQuery(term="roofer"))
    assert hosts == [("tools.aimlapi.com", f"Bearer {KEY}")]


@pytest.mark.asyncio
async def test_key_is_never_sent_when_the_endpoint_is_not_tools_aimlapi(monkeypatch):
    sent = []
    monkeypatch.setattr(D, "AIMLAPI_TOOLS_RUN", "https://tools.aimlapi.com.collector.example/v1/tools/run")
    async with _client(lambda r: sent.append(r) or httpx.Response(200, json=RUN)) as client:
        with pytest.raises(RuntimeError, match="another host"):
            await D.aimlapi_web_search(client, KEY, "roofer")
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 429])
async def test_refusals_stop_the_run(status):
    async with _client(lambda r: httpx.Response(status, json={"error": {}})) as client:
        with pytest.raises(D.ProviderExhausted):
            await D.AIMLAPISearch().fetch(client, {"aimlapi_api_key": KEY},
                                          D.DiscoveryQuery(term="roofer"))


@pytest.mark.asyncio
async def test_failed_run_is_an_error_not_empty_results():
    failed = dict(RUN, status="failed", is_error=True)
    async with _client(lambda r: httpx.Response(200, json=failed)) as client:
        with pytest.raises(RuntimeError, match="did not complete"):
            await D.AIMLAPISearch().fetch(client, {"aimlapi_api_key": KEY},
                                          D.DiscoveryQuery(term="roofer"))


@pytest.mark.asyncio
async def test_missing_key_is_named():
    async with _client(lambda r: httpx.Response(200, json=RUN)) as client:
        with pytest.raises(RuntimeError, match="AIMLAPI_API_KEY"):
            await D.AIMLAPISearch().fetch(client, {}, D.DiscoveryQuery(term="roofer"))


# ── Scout ──


def _scout(env):
    config = SimpleNamespace(
        icp=SimpleNamespace(titles=["Owner"], industries=["Roofing"], geography=[]),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)))
    return Scout(brain=None, state=SimpleNamespace(), config=config, env=env)


@pytest.mark.asyncio
async def test_scout_uses_aimlapi_only_when_the_key_is_set():
    called = []

    async def fake(query, key):
        called.append((query, key))
        return [("https://summit.example", "Roofing in Denver")]

    async def nothing(*a, **kw):
        return []

    without = _scout(SimpleNamespace())
    without._search_aimlapi = fake
    without._search_duckduckgo = without._search_bing = without._search_google = nothing
    await without._web_search("Denver roofers")
    assert called == []

    scout = _scout(SimpleNamespace(aimlapi_api_key=KEY))
    scout._search_aimlapi = fake
    results = await scout._web_search("Denver roofers")
    assert called == [("Denver roofers", KEY)]
    assert results == [("https://summit.example", "Roofing in Denver")]


@pytest.mark.asyncio
async def test_scout_search_does_not_follow_a_redirect_with_the_key(monkeypatch):
    hosts = []

    def handler(request):
        hosts.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "tools.aimlapi.com":
            return httpx.Response(302, headers={"Location": "https://collector.example/"})
        return httpx.Response(200, json=RUN)

    monkeypatch.setattr(scout_mod, "public_http_client",
                        lambda timeout=None: _client(handler))
    results = await _scout(SimpleNamespace())._search_aimlapi("Denver roofers", KEY)
    assert results == []
    assert hosts == [("tools.aimlapi.com", f"Bearer {KEY}")]


@pytest.mark.asyncio
async def test_scout_search_maps_results(monkeypatch):
    monkeypatch.setattr(scout_mod, "public_http_client",
                        lambda timeout=None: _client(lambda r: httpx.Response(200, json=RUN)))
    results = await _scout(SimpleNamespace())._search_aimlapi("Denver roofers", KEY)
    assert results == [
        ("https://www.summitroofing.example/denver", "Roof repair in Denver"),
        ("https://directory.example/roofers", "A list"),
    ]
