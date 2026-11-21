"""The heartbeat's parallel agent runner (sellbird.main._run_agents)."""
import asyncio

import pytest

from sellbird import main as M


async def _returns(value, delay=0.0):
    await asyncio.sleep(delay)
    return value


async def _raises():
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_outcomes_are_what_each_agent_returned_or_raised():
    outcomes = await M._run_agents([("a", _returns(1)), ("b", _raises())])
    assert outcomes["a"] == 1
    assert isinstance(outcomes["b"], RuntimeError)


@pytest.mark.asyncio
async def test_a_timeout_keeps_the_agents_that_already_finished(monkeypatch):
    """A slow profile used to erase the finished reply check, so the heartbeat
    held sending as if replies couldn't be read."""
    monkeypatch.setattr(M, "CYCLE_TIMEOUT_SECONDS", 0.2)
    outcomes = await M._run_agents([
        ("handle_replies", _returns(True)),
        ("profile", _returns("late", delay=5)),
    ])
    assert outcomes == {"handle_replies": True}


@pytest.mark.asyncio
async def test_a_timed_out_agent_is_cancelled(monkeypatch):
    monkeypatch.setattr(M, "CYCLE_TIMEOUT_SECONDS", 0.1)
    cancelled = asyncio.Event()

    async def hang():
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    assert await M._run_agents([("profile", hang())]) == {}
    assert cancelled.is_set()
