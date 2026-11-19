"""Learning writing rules from the owner's Outbox edits (harvey.edits)."""
import os
import tempfile

import pytest
import pytest_asyncio

from harvey import edits as E
from harvey.state import StateManager

DRAFT = ("Hi Sam,\n\nI wanted to reach out because training is a big part of running a "
         "care home and many managers find it hard.\n\nWould you be open to a chat?\n\nStewart")
REWRITE = ("Sam, your team can try our safeguarding course free for 7 days.\n\n"
           "Shall I set it up?\n\nStewart")


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        yield sm


async def _edit(state, old_subject, old_body, new_subject, new_body, step=1, kind="sequence"):
    """A draft, then the owner's edit exactly as the dashboard records it."""
    item = await state.add_outbox_item(
        prospect_id="p1", to_email="sam@example.com", subject=old_subject,
        body=old_body, send_at="2026-01-01T09:00:00", step=step, kind=kind)
    await state.edit_outbox_draft(item, new_subject, new_body)
    await state.log_action("outbox_edited", "dashboard", {
        "outbox_id": item, "old_subject": old_subject, "old_body": old_body})
    return item


class FakeBrain:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def think_json(self, prompt, **_):
        self.prompts.append(prompt)
        return self.reply


def test_rules_file_round_trips(tmp_path):
    path = tmp_path / "learned_rules.md"
    rules = [{"applies": "email 1", "rule": "Open with the offer.", "edits": 3},
             {"applies": "all", "rule": "Keep it under 60 words.", "edits": 1}]
    E.save_rules(rules, path)
    assert E.load_rules(path) == rules
    assert path.read_text(encoding="utf-8").startswith("# Learned from your Outbox edits")


def test_a_hand_edited_rules_file_is_still_read(tmp_path):
    path = tmp_path / "learned_rules.md"
    path.write_text("# notes\n- [reply] Sign off with first name only\n"
                    "- [nonsense] Be brief (edits: 2)\nnot a rule\n", encoding="utf-8")
    assert E.load_rules(path) == [
        {"applies": "reply", "rule": "Sign off with first name only", "edits": 1},
        {"applies": "all", "rule": "Be brief", "edits": 2},
    ]


def test_rules_block_picks_the_rules_for_each_kind_of_email(tmp_path):
    path = tmp_path / "learned_rules.md"
    E.save_rules([{"applies": "all", "rule": "No exclamation marks.", "edits": 1},
                  {"applies": "email 1", "rule": "Open with the offer.", "edits": 4},
                  {"applies": "reply", "rule": "Answer in two lines.", "edits": 1}], path)
    first = E.rules_block("email 1", path)
    assert "Open with the offer." in first and "No exclamation marks." in first
    assert "Answer in two lines." not in first
    assert first.index("Open with the offer.") < first.index("No exclamation marks."), \
        "better-backed rules come first"
    reply = E.rules_block("reply", path)
    assert "Answer in two lines." in reply and "Open with the offer." not in reply
    assert E.rules_block("email 2", tmp_path / "missing.md") == ""


def test_a_typo_fix_is_trivial():
    assert E.is_trivial("hello", DRAFT, "hello", DRAFT.replace("chat", "chta"))
    assert not E.is_trivial("hello", DRAFT, "hello", REWRITE)
    assert not E.is_trivial("hello", DRAFT, "a new subject", DRAFT)


@pytest.mark.asyncio
async def test_an_edit_becomes_a_rule_and_is_learned_once(state, tmp_path):
    path = tmp_path / "learned_rules.md"
    await _edit(state, "training at your home", DRAFT, "safeguarding course, free", REWRITE)
    assert await E.has_pending_edits(state)

    brain = FakeBrain({"rules": [
        {"applies": "email 1", "rule": "Open with the free course offer.", "edits": 1}]})
    assert await E.learn_from_edits(state, brain, path) == 1
    assert E.load_rules(path) == [
        {"applies": "email 1", "rule": "Open with the free course offer.", "edits": 1}]
    prompt = brain.prompts[0]
    assert DRAFT.split("\n")[2] in prompt and "Shall I set it up?" in prompt
    assert "email 1 of a sequence" in prompt

    # Learned once: the next cycle has nothing to do.
    assert not await E.has_pending_edits(state)
    assert await E.learn_from_edits(state, brain, path) == 0
    assert len(brain.prompts) == 1


@pytest.mark.asyncio
async def test_existing_rules_are_given_to_the_model_to_update(state, tmp_path):
    path = tmp_path / "learned_rules.md"
    E.save_rules([{"applies": "all", "rule": "Keep it short.", "edits": 2}], path)
    await _edit(state, "s", DRAFT, "s2", REWRITE, step=2)
    brain = FakeBrain({"rules": [
        {"applies": "all", "rule": "Keep it under 50 words.", "edits": 3}]})
    await E.learn_from_edits(state, brain, path)
    assert "[all] Keep it short. (edits: 2)" in brain.prompts[0]
    assert E.load_rules(path) == [
        {"applies": "all", "rule": "Keep it under 50 words.", "edits": 3}]


@pytest.mark.asyncio
async def test_a_failed_model_call_keeps_the_edit_for_next_time(state, tmp_path):
    path = tmp_path / "learned_rules.md"
    E.save_rules([{"applies": "all", "rule": "Keep it short.", "edits": 2}], path)
    await _edit(state, "s", DRAFT, "s2", REWRITE)
    assert await E.learn_from_edits(state, FakeBrain(None), path) == 0
    assert await E.has_pending_edits(state), "not marked learned"
    assert E.load_rules(path)[0]["rule"] == "Keep it short.", "rules untouched"


@pytest.mark.asyncio
async def test_typo_fixes_are_skipped_without_a_model_call(state, tmp_path):
    path = tmp_path / "learned_rules.md"
    await _edit(state, "s", DRAFT, "s", DRAFT.replace("chat", "chta"))
    brain = FakeBrain({"rules": []})
    assert await E.learn_from_edits(state, brain, path) == 0
    assert brain.prompts == []
    assert not await E.has_pending_edits(state)


@pytest.mark.asyncio
async def test_the_model_cannot_write_junk_rules(state, tmp_path):
    path = tmp_path / "learned_rules.md"
    await _edit(state, "s", DRAFT, "s2", REWRITE)
    brain = FakeBrain({"rules": [
        {"applies": "everything", "rule": "  Lead   with the offer. ", "edits": "x"},
        {"applies": "all", "rule": ""}, "not a dict",
        {"applies": "all", "rule": "x" * 400}]})
    await E.learn_from_edits(state, brain, path)
    assert E.load_rules(path) == [{"applies": "all", "rule": "Lead with the offer.", "edits": 1}]
