"""The loop tops up the company list by itself (discovery.auto)."""
import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

import sellbird.collectors.discover as D
import sellbird.pipeline as pipeline
from sellbird import auto_discovery as A
from sellbird.config import DiscoveryConfig, load_config
from sellbird.models import Company
from sellbird.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "a.db"))
    await s.init_db()
    return s


class Fake:
    key = "fake"
    label = "Fake source"

    def __init__(self, per_query=0.0, missing=()):
        self.per_query, self.missing = per_query, list(missing)

    def estimate(self, queries):
        return self.per_query * len(queries)

    def missing_keys(self, env):
        return self.missing


@pytest.fixture
def source(monkeypatch):
    fake = Fake()
    monkeypatch.setitem(D.PROVIDERS, "fake", fake)
    return fake


@pytest.fixture
def runs(monkeypatch):
    seen = []

    async def run_prospecting(state, config, provider, queries, max_spend=1.0, **k):
        seen.append({"provider": provider, "queries": list(queries), "max_spend": max_spend})
        return SimpleNamespace(discover={"queries_ran": len(queries), "new_companies": 3,
                                         "found": 9, "actual_cost": 0.0, "stopped": ""},
                               errors=[])
    monkeypatch.setattr(pipeline, "run_prospecting", run_prospecting)
    return seen


def _config(**overrides):
    config = load_config()
    fields = {"auto": True, "provider": "fake", "queries_per_run": 2, "min_backlog": 5}
    fields.update(overrides)
    return config.model_copy(update={"discovery": DiscoveryConfig(**fields)})


async def _status(state):
    return A.parse(await state.get_setting(A.AUTO_KEY))


async def _tick_and_finish(auto, state, config):
    await auto.tick(state, config)
    if auto.task:
        await auto.task


async def _companies(state, n):
    for i in range(n):
        await state.add_company(Company(name=f"Shop {i}", domain=f"shop{i}.example"))


@pytest.mark.asyncio
async def test_off_by_default_does_nothing(state, source, runs):
    config = load_config()
    assert config.discovery.auto is False
    await A.AutoDiscovery().tick(state, config)
    assert runs == []
    assert A.describe(await _status(state))[0] == "off"


@pytest.mark.asyncio
async def test_tops_up_in_chunks_and_remembers_its_place(state, source, runs):
    config = _config()
    plan = D.build_queries(config, depth=30, limit=100)
    assert len(plan) >= 4
    auto = A.AutoDiscovery()
    await _tick_and_finish(auto, state, config)
    await _tick_and_finish(auto, state, config)
    assert [q.keyword() for q in runs[0]["queries"]] == [q.keyword() for q in plan[0:2]]
    assert [q.keyword() for q in runs[1]["queries"]] == [q.keyword() for q in plan[2:4]]
    saved = await _status(state)
    assert saved["cursor"] == 4 and saved["last"]["new"] == 3
    # A new loop (a restart) carries on from the saved place.
    await _tick_and_finish(A.AutoDiscovery(), state, config)
    assert [q.keyword() for q in runs[2]["queries"]] == [q.keyword() for q in plan[4:6]]


@pytest.mark.asyncio
async def test_waits_while_the_scout_has_plenty_to_check(state, source, runs):
    await _companies(state, 6)
    await _tick_and_finish(A.AutoDiscovery(), state, _config(min_backlog=5))
    assert runs == []
    state_name, line = A.describe(await _status(state))
    assert state_name == "waiting" and "6 companies not checked yet" in line and "below 5" in line


@pytest.mark.asyncio
async def test_paid_source_needs_a_spending_cap(state, source, runs):
    source.per_query = 0.01
    await _tick_and_finish(A.AutoDiscovery(), state, _config())
    assert runs == []
    state_name, line = A.describe(await _status(state))
    assert state_name == "blocked" and "max_daily_spend" in line
    # With a cap that covers it, it runs, and passes what's left as the cap.
    await state.set_setting(A.AUTO_KEY, "{}")
    await _tick_and_finish(A.AutoDiscovery(), state, _config(max_daily_spend=1.0))
    assert runs and runs[0]["max_spend"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_missing_key_is_shown_and_retried_later(state, source, runs):
    source.missing = ["fake_api_key"]
    auto = A.AutoDiscovery()
    await _tick_and_finish(auto, state, _config())
    saved = await _status(state)
    assert saved["state"] == "blocked" and "FAKE_API_KEY" in saved["reason"]
    source.missing = []
    await _tick_and_finish(auto, state, _config())
    assert runs == []  # waits out the retry window instead of asking every minute


@pytest.mark.asyncio
async def test_stop_on_the_discover_page_pauses_it(state, source, runs):
    await state.set_setting(A.PAUSED_KEY, "stopped from dashboard")
    await _tick_and_finish(A.AutoDiscovery(), state, _config())
    assert runs == []
    assert A.describe(await _status(state))[0] == "paused"


@pytest.mark.asyncio
async def test_a_run_from_the_discover_page_goes_first(state, source, runs):
    await state.start_run("discover", provider="fake")
    await _tick_and_finish(A.AutoDiscovery(), state, _config())
    assert runs == []
    assert "Discover page" in (await _status(state))["reason"]


@pytest.mark.asyncio
async def test_rests_after_every_search_has_run(state, source, runs):
    config = _config(queries_per_run=1000)
    auto = A.AutoDiscovery()
    await _tick_and_finish(auto, state, config)
    assert len(runs) == 1
    await _tick_and_finish(auto, state, config)
    assert len(runs) == 1
    state_name, line = A.describe(await _status(state))
    assert state_name == "waiting" and "every search has run" in line


@pytest.mark.asyncio
async def test_only_one_run_at_a_time(state, source, monkeypatch):
    gate = asyncio.Event()
    calls = []

    async def slow(state, config, provider, queries, **k):
        calls.append(1)
        await gate.wait()
        return SimpleNamespace(discover={"queries_ran": len(queries)}, errors=[])
    monkeypatch.setattr(pipeline, "run_prospecting", slow)
    auto = A.AutoDiscovery()
    await auto.tick(state, _config())
    await asyncio.sleep(0)
    await auto.tick(state, _config())
    assert len(calls) == 1
    assert (await _status(state))["state"] == "running"
    gate.set()
    await auto.task


def test_threshold_follows_the_scouts_pace():
    cfg = DiscoveryConfig(backlog_days=2, min_backlog=100)
    assert A.threshold(cfg, pace=0) == 100
    assert A.threshold(cfg, pace=280) == 560


def test_settings_are_checked():
    with pytest.raises(ValueError):
        DiscoveryConfig(queries_per_run=0)
    with pytest.raises(ValueError):
        DiscoveryConfig(max_daily_spend=-1)


# ── where it shows ──

from tests.test_dashboard_signals import client  # noqa: E402,F401  (fixture)

BLOCKED = json.dumps({"state": "blocked", "reason": "can't run: Fake source needs FAKE_API_KEY in .env"})


def test_today_and_the_header_show_a_blocked_run(client):
    st = StateManager(str(client.db_path))
    asyncio.run(st.init_db())
    asyncio.run(st.set_setting(A.AUTO_KEY, BLOCKED))
    items = client.get("/api/today").json()["items"]
    card = [i for i in items if i["key"] == "discovery"]
    assert card and "FAKE_API_KEY" in card[0]["detail"] and card[0]["tab"] == "discover"
    assert client.get("/api/stats").json()["discovery"]["state"] == "blocked"
    asyncio.run(st.set_setting(A.AUTO_KEY, json.dumps({"state": "waiting", "reason": "waiting: x"})))
    assert not [i for i in client.get("/api/today").json()["items"] if i["key"] == "discovery"]


def test_sellbird_status_shows_it(tmp_path, monkeypatch, capsys):
    import argparse

    import sellbird.state as state_mod
    from sellbird import cli
    db = tmp_path / "s.db"
    st = StateManager(str(db))
    asyncio.run(st.init_db())
    asyncio.run(st.set_setting(A.AUTO_KEY, BLOCKED))
    real = state_mod.StateManager
    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **k: real(str(db)))
    cli.cmd_status(argparse.Namespace(check=False))
    out = capsys.readouterr().out
    assert "Discovery:" in out and "FAKE_API_KEY" in out
