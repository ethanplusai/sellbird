"""Sellbird never asks you to approve an email its own pre-send gate will refuse.

The gate fails a cold email to a catch-all ("risky") address unless
channels.email.send_to_risky is on, and always to a guessed one. The Sender's
staging already skipped such prospects, but the Writer drafted a personal
first email for every prospect with a verified or risky address and staged it
as pending_review. The user approved it, and fifteen minutes later it failed
with "gate: email_status 'risky' is not deliverable".

Now one rule (gate.sendable_email_statuses) decides who is written for,
staged and sent to. A prospect the rule leaves out stays 'new', is not
counted as writable by the heartbeat, and is picked up once the address is
verified or the rule changes. Drafts staged before this that the gate would
refuse are withdrawn with the reason, and shown under Didn't send.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird.agents.writer import Writer
from sellbird.gate import pre_send_check, sendable_email_statuses, undeliverable_reason
from sellbird.main import decide_next_action
from sellbird.models.prospect import Prospect
from sellbird.state import WITHDRAWN_PREFIX, StateManager
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from tests.test_outbox_native import FakeProvider, _now_iso, make_sender, seed_campaign

SEQUENCE = [
    {"step": 1, "subject": "quick one", "body": "Hi {{first_name}}, who answers at {{company}}?",
     "delay_days": 0},
    {"step": 2, "subject": "one more", "body": "Hi {{first_name}}, still curious.", "delay_days": 3},
]
PERSONAL = {"subject": "your phones", "body": "Hi there, who answers the phones at your office?"}


class FakeBrain:
    """A sequence for the campaign prompt, a personal draft for email 1."""

    def __init__(self):
        self.calls = []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.calls.append(kw.get("task", ""))
        return PERSONAL if kw.get("task") == "personal_email" else SEQUENCE


def config(send_to_risky=False, provider="gmail"):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            provider=provider, require_approval=True, send_to_risky=send_to_risky)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing="", offer=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        humanizer=SimpleNamespace(enabled=False),
    )


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "u.db"))
    await s.init_db()
    return s


async def add(state, n, email_status, status="new"):
    return await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        industry="Roofing", email=f"p{n}@co{n}.example", email_status=email_status,
        email_verified=email_status == "verified", status=status,
    ))


async def prospect_status(state, pid):
    return (await state.get_prospect(pid)).status


# ── one rule ──


def test_the_rule_matches_the_gate():
    assert sendable_email_statuses(False) == {"verified"}
    assert sendable_email_statuses(True) == {"verified", "risky"}
    assert undeliverable_reason("verified", False) == ""
    assert undeliverable_reason("risky", True) == ""
    assert undeliverable_reason("risky", False) == "email_status 'risky' is not deliverable"
    assert undeliverable_reason("", False) == "email_status 'guess' is not deliverable"
    assert undeliverable_reason(None, True) == "email_status 'guess' is not deliverable"
    for status, allow in (("risky", False), ("guess", True), ("invalid", True)):
        p = Prospect(first_name="J", last_name="D", title="VP", company="Co",
                     email="j@co.example", email_status=status)
        gate = pre_send_check("j@co.example", "hi", "Short body. Question?",
                              prospect=p, allow_risky=allow)
        assert not gate and undeliverable_reason(status, allow) in gate.reasons


# ── the Writer ──


@pytest.mark.asyncio
async def test_a_catch_all_prospect_gets_no_draft_to_approve(state, caplog):
    pid = await add(state, 1, "risky")
    brain = FakeBrain()
    caplog.set_level(logging.INFO, logger="sellbird.writer")
    await Writer(brain, state, config(send_to_risky=False), None).run()

    assert await state.get_outbox() == []
    assert brain.calls == []                       # no Claude call either
    assert await prospect_status(state, pid) == "new"
    assert "catch-all (risky) email: channels.email.send_to_risky is off" in caplog.text
    # The heartbeat doesn't count them as writable, so it moves on to
    # prospecting instead of choosing write_campaign every cycle.
    summary = await state.get_state_summary(allow_risky=False)
    assert summary["writable_prospects"] == 0
    assert await decide_next_action(None, state, config(False), summary=summary) == "prospect"


@pytest.mark.asyncio
async def test_a_verified_prospect_still_gets_a_draft_to_approve(state):
    pid = await add(state, 1, "verified")
    await Writer(FakeBrain(), state, config(send_to_risky=False), None).run()
    [row] = await state.get_outbox()
    assert (row["status"], row["step"], row["subject"]) == ("pending_review", 1, "your phones")
    assert await prospect_status(state, pid) == "queued"


@pytest.mark.asyncio
async def test_with_send_to_risky_on_a_catch_all_prospect_is_written_for(state):
    pid = await add(state, 1, "risky")
    await Writer(FakeBrain(), state, config(send_to_risky=True), None).run()
    [row] = await state.get_outbox()
    assert row["status"] == "pending_review"
    assert await prospect_status(state, pid) == "queued"
    assert (await state.get_state_summary(allow_risky=True))["writable_prospects"] == 0


@pytest.mark.asyncio
async def test_the_rule_is_checked_again_right_before_a_personal_draft(state):
    """Handed a prospect the rule leaves out, the Writer writes nothing."""
    risky = await state.get_prospect(await add(state, 1, "risky", status="queued"))
    fine = await state.get_prospect(await add(state, 2, "verified", status="queued"))
    brain = FakeBrain()
    writer = Writer(brain, state, config(send_to_risky=False), None)
    writer.skills = ""
    await writer._personalize_first_emails("c1", [risky, fine])
    [row] = await state.get_outbox()
    assert row["prospect_id"] == fine.id
    assert brain.calls == ["personal_email"]


@pytest.mark.asyncio
async def test_once_the_address_is_verified_the_writer_picks_them_up(state):
    pid = await add(state, 1, "risky")
    writer = Writer(FakeBrain(), state, config(send_to_risky=False), None)
    await writer.run()
    assert await state.get_outbox() == []
    await state.update_prospect_email(pid, "p1@co1.example", "verified")
    await writer.run()
    assert len(await state.get_outbox(status="pending_review")) == 1
    assert await prospect_status(state, pid) == "queued"


# ── the Sender ──


@pytest.mark.asyncio
async def test_waiting_drafts_the_gate_would_refuse_are_withdrawn_with_the_reason(state):
    """Drafts staged before this change, or whose address was downgraded
    since: the Outbox stops asking, and says why under Didn't send."""
    risky = await add(state, 1, "risky", status="queued")
    fine = await add(state, 2, "verified", status="queued")
    campaign = await seed_campaign(state, [risky, fine])
    for pid, email in ((risky, "p1@co1.example"), (fine, "p2@co2.example")):
        await state.add_outbox_item(
            prospect_id=pid, campaign_id=campaign.id, step=1, to_email=email,
            subject="hi", body="Hi there. Question?", send_at=_now_iso())
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=True)

    await sender._run_native()

    pending = await state.get_outbox(status="pending_review")
    assert {r["prospect_id"] for r in pending} >= {fine}
    assert all(r["prospect_id"] != risky for r in pending)
    [gone] = [r for r in await state.get_outbox(status="cancelled")]
    assert gone["prospect_id"] == risky
    assert gone["error"] == WITHDRAWN_PREFIX + "email_status 'risky' is not deliverable"
    # Approve-all never touches it, and nothing went out.
    await state.approve_outbox()
    assert (await state.get_outbox_item(gone["id"]))["status"] == "cancelled"
    await sender._drain_due()
    assert [s["to"] for s in provider.sent] == ["p2@co2.example"]   # the verified one goes
    # Back in the pool for when the address is verified or the rule changes.
    assert await prospect_status(state, risky) == "new"
    assert await prospect_status(state, fine) == "contacted"


@pytest.mark.asyncio
async def test_withdrawal_leaves_someone_already_emailed_where_they_are(state):
    pid = await add(state, 1, "risky", status="contacted")
    await state.add_outbox_item(prospect_id=pid, campaign_id="c", step=1, to_email="p1@co1.example",
                                subject="hi", body="b", send_at=_now_iso(), status="sent")
    await state.add_outbox_item(prospect_id=pid, campaign_id="c", step=2, to_email="p1@co1.example",
                                subject="again", body="b", send_at=_now_iso())
    assert await state.withdraw_undeliverable_drafts(sendable_email_statuses(False)) == 1
    assert (await state.get_outbox(status="sent"))[0]["step"] == 1
    assert await prospect_status(state, pid) == "contacted"


@pytest.mark.asyncio
async def test_with_send_to_risky_on_nothing_is_withdrawn(state):
    pid = await add(state, 1, "risky", status="queued")
    await state.add_outbox_item(prospect_id=pid, campaign_id="c", step=1, to_email="p1@co1.example",
                                subject="hi", body="b", send_at=_now_iso())
    assert await state.withdraw_undeliverable_drafts(sendable_email_statuses(True)) == 0
    assert len(await state.get_outbox(status="pending_review")) == 1


@pytest.mark.asyncio
async def test_replies_are_never_withdrawn(state):
    """A reply goes back to someone who wrote in: deliverability is proven."""
    pid = await add(state, 1, "guess", status="replied")
    await state.add_outbox_item(prospect_id=pid, kind="reply", to_email="p1@co1.example",
                                subject="Re: hi", body="b", send_at=_now_iso())
    assert await state.withdraw_undeliverable_drafts(sendable_email_statuses(False)) == 0


# ── the Outbox shows why ──


def test_the_outbox_lists_a_withdrawn_draft_under_didnt_send_with_the_reason(client):
    async def seed(db):
        s = StateManager(str(db))
        await s.init_db()
        pid = await add(s, 1, "risky", status="queued")
        await s.add_outbox_item(prospect_id=pid, campaign_id="c", step=1, to_email="p1@co1.example",
                                subject="hi", body="b", send_at=_now_iso())
        await s.withdraw_undeliverable_drafts(sendable_email_statuses(False))

    asyncio.run(seed(client.db_path))
    data = client.get("/api/outbox").json()
    assert data["pending"] == [] and data["pending_total"] == 0
    [row] = data["failed"]
    assert row["status"] == "cancelled"
    assert row["error"] == WITHDRAWN_PREFIX + "email_status 'risky' is not deliverable"
