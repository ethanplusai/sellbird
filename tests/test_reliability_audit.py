"""Regression checks for retry starvation and fail-closed decisions."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sellbird.agents.sender import Sender
from sellbird.main import decide_next_action


def instant_config():
    email = SimpleNamespace(
        enabled=True,
        provider="instantly",
        require_approval=False,
        max_daily_sends=100,
    )
    return SimpleNamespace(channels=SimpleNamespace(email=email))


@pytest.mark.asyncio
async def test_instantly_retry_cooldown_yields_to_prospecting():
    state = SimpleNamespace(
        get_setting=AsyncMock(return_value=(
            datetime.utcnow() + timedelta(minutes=30)).isoformat()),
    )
    summary = {"draft_campaigns": 2, "prospects": {"new": 0}, "writable_prospects": 0}
    result = await decide_next_action(None, state, instant_config(), summary)
    assert result == "prospect"


@pytest.mark.asyncio
async def test_instantly_expired_retry_remains_deployable():
    state = SimpleNamespace(
        get_setting=AsyncMock(return_value=(
            datetime.utcnow() - timedelta(minutes=1)).isoformat()),
    )
    summary = {"draft_campaigns": 1, "prospects": {"new": 0}, "writable_prospects": 0}
    result = await decide_next_action(None, state, instant_config(), summary)
    assert result == "send_campaign"


@pytest.mark.asyncio
async def test_instantly_failed_cycle_records_bounded_backoff():
    campaign = SimpleNamespace(name="Demo", id="one")
    state = SimpleNamespace(
        get_campaigns_by_status=AsyncMock(return_value=[campaign]),
        get_setting=AsyncMock(return_value="0"),
        set_setting=AsyncMock(),
    )
    sender = Sender.__new__(Sender)
    sender.config = instant_config()
    sender.state = state
    sender._count_sends_today = AsyncMock(return_value=0)
    sender._deploy_campaign = AsyncMock(return_value=None)
    await sender.run()
    saved = [call.args for call in state.set_setting.await_args_list]
    assert ("instantly_deploy_failures", "1") in saved
    backoff = dict(saved)["instantly_deploy_retry_after"]
    assert datetime.fromisoformat(backoff) > datetime.utcnow()


@pytest.mark.asyncio
async def test_security_sensitive_missing_db_raises(tmp_path):
    import sellbird.dashboard as dash
    from sellbird.workspace_home import Home, use_home

    with use_home(Home.for_root(tmp_path / "empty")):
        with pytest.raises(RuntimeError, match="database unavailable"):
            await dash.query_db("SELECT * FROM outbox", strict=True)
