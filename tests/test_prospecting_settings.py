"""The `prospecting:` settings: per-cycle limits that used to be constants,
and prospect_while_writing, which lets a writing cycle prospect too.

Defaults are the old constants, so a config without the block behaves as
before. No network: the heartbeat runs with stub agents (see
tests/test_send_window.py) and Scout's sources are called with fake state.
"""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError

import sellbird.agents.scout as scout_mod
import sellbird.main as main_mod
from sellbird.agents.scout import Scout
from sellbird.config import (
    PROSPECTING_LIMIT_MAX, ProspectingConfig, SellbirdConfig, prospecting_of,
)
from sellbird.main import (
    PROFILE_LIMIT, PROSPECT_BACKLOG, decide_next_action,
    prospect_backlog, prospects_while_writing, unusable_backlog_notice,
)
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager
from tests.test_send_window import BASE, _patch_heartbeat

LIMITS = {
    "max_queries_per_cycle": 3,
    "max_companies_per_cycle": 10,
    "max_prospects_per_cycle": 25,
    "hunter_domains_per_cycle": 1,
    "max_contacts_per_company": 2,
    "max_profiles_per_cycle": 25,
    "prospect_backlog": 20,
}


def config(**prospecting) -> SellbirdConfig:
    extra = {"prospecting": prospecting} if prospecting else {}
    return SellbirdConfig(**BASE, channels={"email": {"provider": "gmail"}}, **extra)


# ── Defaults and overrides ──


def test_defaults_are_the_old_limits():
    p = config().prospecting
    for name, value in LIMITS.items():
        assert getattr(p, name) == value, name
    assert p.prospect_while_writing is False


def test_module_constants_still_name_the_defaults():
    assert scout_mod.MAX_QUERIES_PER_CYCLE == 3
    assert scout_mod.MAX_COMPANIES_PER_CYCLE == 10
    assert scout_mod.MAX_PROSPECTS_PER_CYCLE == 25
    assert scout_mod.HUNTER_DOMAINS_PER_CYCLE == 1
    assert scout_mod.MAX_CONTACTS_PER_COMPANY == 2
    assert PROSPECT_BACKLOG == 20
    assert PROFILE_LIMIT == 25


def test_every_setting_can_be_overridden():
    wanted = {name: value * 4 for name, value in LIMITS.items()}
    p = config(**wanted, prospect_while_writing=True).prospecting
    for name, value in wanted.items():
        assert getattr(p, name) == value, name
    assert p.prospect_while_writing is True


def test_one_override_leaves_the_rest_at_their_defaults():
    p = config(max_prospects_per_cycle=100).prospecting
    assert p.max_prospects_per_cycle == 100
    assert p.max_queries_per_cycle == 3 and p.prospect_backlog == 20
    assert p.prospect_while_writing is False


def test_blank_values_mean_the_default():
    # `max_queries_per_cycle:` with nothing after it reads as None in YAML.
    p = ProspectingConfig(max_queries_per_cycle=None, prospect_backlog=None,
                          prospect_while_writing=None)
    assert p.max_queries_per_cycle == 3 and p.prospect_backlog == 20
    assert p.prospect_while_writing is False


@pytest.mark.parametrize("name", sorted(LIMITS))
def test_limits_out_of_range_are_refused(name):
    with pytest.raises(ValidationError, match=f"prospecting.{name}"):
        ProspectingConfig(**{name: PROSPECTING_LIMIT_MAX + 1})
    with pytest.raises(ValidationError):
        ProspectingConfig(**{name: -1})
    with pytest.raises(ValidationError):
        ProspectingConfig(**{name: True})
    with pytest.raises(ValidationError):
        ProspectingConfig(**{name: "lots"})


@pytest.mark.parametrize("name", ["max_queries_per_cycle", "hunter_domains_per_cycle"])
def test_zero_switches_a_paid_source_off(name):
    assert getattr(ProspectingConfig(**{name: 0}), name) == 0


@pytest.mark.parametrize("name", ["max_companies_per_cycle", "max_prospects_per_cycle",
                                  "max_contacts_per_company", "max_profiles_per_cycle",
                                  "prospect_backlog"])
def test_zero_is_refused_where_it_would_stop_prospecting(name):
    with pytest.raises(ValidationError):
        ProspectingConfig(**{name: 0})


def test_prospecting_of_a_hand_built_config_is_the_defaults():
    assert prospecting_of(SimpleNamespace()) == ProspectingConfig()
    real = config(max_queries_per_cycle=9)
    assert prospecting_of(real).max_queries_per_cycle == 9


# ── Scout honours the limits ──


class LimitState:
    def __init__(self):
        self.limits = []

    async def companies_needing_prospects(self, limit=10, **kw):
        self.limits.append(limit)
        return []


def scout_with(cfg, env=None):
    return Scout(brain=None, state=LimitState(), config=cfg,
                 env=env or SimpleNamespace(hunter_api_key=""))


def test_scout_reads_each_limit_from_the_config():
    wanted = {"max_queries_per_cycle": 7, "max_companies_per_cycle": 40,
              "max_prospects_per_cycle": 90, "hunter_domains_per_cycle": 5,
              "max_contacts_per_company": 4}
    scout = scout_with(config(**wanted))
    for name, value in wanted.items():
        assert scout._limit(name, -1) == value, name


def test_scout_without_the_block_uses_the_defaults():
    scout = scout_with(config())
    assert scout._query_limit() == 3
    assert scout._prospect_limit() == 25
    assert scout._limit("max_companies_per_cycle", scout_mod.MAX_COMPANIES_PER_CYCLE) == 10


def test_a_hand_built_config_falls_back_to_the_module_constant(monkeypatch):
    scout = scout_with(SimpleNamespace())
    monkeypatch.setattr(scout_mod, "MAX_PROSPECTS_PER_CYCLE", 7)
    assert scout._prospect_limit() == 7


@pytest.mark.asyncio
async def test_query_limit_stops_web_search_before_any_request():
    scout = scout_with(config(max_queries_per_cycle=0))
    assert await scout._web_search("roofers in springfield") == []
    assert scout._queries_this_cycle == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("prospecting, expected", [({}, 10), ({"max_companies_per_cycle": 60}, 60)])
async def test_backfill_reads_as_many_known_companies_as_configured(prospecting, expected):
    scout = scout_with(config(**prospecting))
    assert await scout._prospect_via_known_companies() == 0
    assert scout.state.limits == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("prospecting, expected", [({}, 1), ({"hunter_domains_per_cycle": 6}, 6)])
async def test_hunter_paces_as_configured(monkeypatch, prospecting, expected):
    import sellbird.integrations.email_finder as ef

    async def plenty(state):
        return 25
    monkeypatch.setattr(ef, "hunter_searches_remaining", plenty)
    scout = scout_with(config(**prospecting), env=SimpleNamespace(hunter_api_key="fake-key"))
    assert await scout._prospect_via_hunter() == 0
    assert scout.state.limits == [expected]


# ── The heartbeat's own limits ──


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "settings.db"))
    await state.init_db()
    return state


async def add(state, n, email_status):
    await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        email=f"p{n}@co{n}.test", email_status=email_status,
    ))


@pytest.mark.asyncio
async def test_prospect_backlog_is_configurable(state):
    for n in range(5):
        await add(state, n, "guess")
    summary = await state.get_state_summary()
    # Default backlog (20): five guesses don't pause anything.
    assert await decide_next_action(None, state, config(), summary=summary) == "prospect"
    assert unusable_backlog_notice(summary, prospect_backlog(config())) == ""
    # Backlog of 5: the same five pause prospecting for the day.
    low = config(prospect_backlog=5)
    assert await decide_next_action(None, state, low, summary=summary) == "idle"
    assert "5 prospects" in unusable_backlog_notice(summary, prospect_backlog(low))


def test_backlog_of_a_hand_built_config_is_the_default():
    assert prospect_backlog(SimpleNamespace()) == PROSPECT_BACKLOG
    assert prospect_backlog(None) == PROSPECT_BACKLOG


def run_heartbeat(monkeypatch, cfg, state, *, unprofiled=0):
    """One heartbeat cycle with stub agents outside quiet hours; returns the
    agents that ran, and the profile limit it was given (if it ran)."""
    calls, stop = [], asyncio.Event()
    patched = _patch_heartbeat(monkeypatch, cfg, state, calls, stop)
    monkeypatch.setattr(patched, "in_quiet_hours", lambda c: False)
    profiled = []

    async def profile(state, limit=25, **kw):
        profiled.append(limit)
        calls.append("profile")
    monkeypatch.setattr(patched, "run_profile_stage", profile)
    real_summary = state.get_state_summary

    async def summary():
        return {**await real_summary(), "unprofiled": unprofiled}
    monkeypatch.setattr(state, "get_state_summary", summary)
    return patched, calls, stop, profiled


@pytest.mark.asyncio
@pytest.mark.parametrize("prospecting, expected", [({}, 25), ({"max_profiles_per_cycle": 80}, 80)])
async def test_the_heartbeat_reads_as_many_sites_as_configured(state, monkeypatch, prospecting, expected):
    patched, calls, stop, profiled = run_heartbeat(monkeypatch, config(**prospecting), state,
                                                   unprofiled=3)
    await asyncio.wait_for(patched.heartbeat(stop), timeout=30)
    assert profiled == [expected]


# ── Prospecting while writing ──


WRITE = {"prospects": {"new": 3}, "writable_prospects": 3, "draft_campaigns": 0}


@pytest.mark.asyncio
async def test_off_by_default_a_writing_cycle_only_writes():
    cfg = config()
    assert await decide_next_action(None, None, cfg, summary=WRITE) == "write_campaign"
    assert prospects_while_writing(cfg, WRITE, "write_campaign") is False


@pytest.mark.asyncio
async def test_on_a_writing_cycle_also_prospects():
    cfg = config(prospect_while_writing=True)
    # The main job is unchanged.
    assert await decide_next_action(None, None, cfg, summary=WRITE) == "write_campaign"
    assert prospects_while_writing(cfg, WRITE, "write_campaign") is True


@pytest.mark.parametrize("action", ["prospect", "send_campaign", "idle", "quiet_hours_send"])
def test_only_a_writing_cycle_gains_a_prospect_step(action):
    assert prospects_while_writing(config(prospect_while_writing=True), WRITE, action) is False


def test_the_backlog_pause_also_stops_prospecting_while_writing():
    cfg = config(prospect_while_writing=True, prospect_backlog=10)
    assert prospects_while_writing(cfg, {**WRITE, "recent_unusable_prospects": 9},
                                   "write_campaign") is True
    assert prospects_while_writing(cfg, {**WRITE, "recent_unusable_prospects": 10},
                                   "write_campaign") is False


def test_a_hand_built_config_never_prospects_while_writing():
    assert prospects_while_writing(SimpleNamespace(), WRITE, "write_campaign") is False


@pytest.mark.asyncio
async def test_heartbeat_off_writes_without_prospecting(state, monkeypatch):
    await add(state, 1, "verified")
    patched, calls, stop, _ = run_heartbeat(monkeypatch, config(), state)
    await asyncio.wait_for(patched.heartbeat(stop), timeout=30)
    assert "write" in calls and "prospect" not in calls
    assert calls[-1] == "send"


@pytest.mark.asyncio
async def test_heartbeat_on_writes_and_prospects_in_the_same_cycle(state, monkeypatch):
    await add(state, 1, "verified")
    patched, calls, stop, _ = run_heartbeat(
        monkeypatch, config(prospect_while_writing=True), state)
    await asyncio.wait_for(patched.heartbeat(stop), timeout=30)
    first_phase = calls[:-1]
    assert sorted(first_phase) == ["prospect", "replies", "write"]
    assert calls[-1] == "send"  # sending still comes after replies were read


@pytest.mark.asyncio
async def test_heartbeat_on_still_prospects_alone_with_nothing_to_write(state, monkeypatch):
    patched, calls, stop, _ = run_heartbeat(
        monkeypatch, config(prospect_while_writing=True), state)
    await asyncio.wait_for(patched.heartbeat(stop), timeout=30)
    assert "prospect" in calls and "write" not in calls


@pytest.mark.asyncio
async def test_heartbeat_on_respects_quiet_hours(state, monkeypatch):
    await add(state, 1, "verified")
    patched, calls, stop, _ = run_heartbeat(
        monkeypatch, config(prospect_while_writing=True), state)
    monkeypatch.setattr(patched, "in_quiet_hours", lambda c: True)

    async def stop_now(seconds, event):
        event.set()
        return True
    monkeypatch.setattr(patched, "_interruptible_sleep", stop_now)
    await asyncio.wait_for(patched.heartbeat(stop), timeout=30)
    assert calls == []


@pytest.mark.asyncio
async def test_heartbeat_on_respects_the_usage_budget(state, monkeypatch):
    await add(state, 1, "verified")
    patched, calls, stop, _ = run_heartbeat(
        monkeypatch, config(prospect_while_writing=True), state)

    class OverBudget(patched.Brain):
        async def is_within_budget(self, *a, **k):
            return False
    monkeypatch.setattr(patched, "Brain", OverBudget)
    await asyncio.wait_for(patched.heartbeat(stop), timeout=30)
    assert calls == []


@pytest.mark.asyncio
async def test_a_slow_ride_along_search_does_not_cost_the_cycle(state, monkeypatch):
    # The extra prospecting has its own deadline inside the cycle's: when it
    # runs long, it is cut off and recorded as failed, while the writer and
    # the reply reader keep their results, so sending isn't held.
    import sellbird.agents.scout

    await add(state, 1, "verified")
    patched, calls, stop, _ = run_heartbeat(
        monkeypatch, config(prospect_while_writing=True), state)
    monkeypatch.setattr(patched, "CYCLE_TIMEOUT_SECONDS", 1)
    real_run_agents = patched._run_agents
    outcomes = {}

    async def recording_run_agents(tasks):
        result = await real_run_agents(tasks)
        outcomes.update(result)
        return result
    monkeypatch.setattr(patched, "_run_agents", recording_run_agents)

    class SlowScout:
        def __init__(self, *a, **k):
            pass

        async def run(self):
            calls.append("prospect")
            await asyncio.sleep(60)
    monkeypatch.setattr(sellbird.agents.scout, "Scout", SlowScout)
    await asyncio.wait_for(patched.heartbeat(stop), timeout=30)
    assert "write" in calls and "replies" in calls
    assert calls[-1] == "send"
    assert isinstance(outcomes["prospect"], TimeoutError)
    assert "stopped after" in str(outcomes["prospect"])
    assert outcomes["write_campaign"] is True and outcomes["handle_replies"] is True
