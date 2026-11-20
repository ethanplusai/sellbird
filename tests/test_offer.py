"""Designing the first-email offer: suggestions, a preview, saving it."""

from types import SimpleNamespace

import pytest
import yaml

from sellbird.offer import save_offer, suggest_offers


def _config():
    return SimpleNamespace(
        product=SimpleNamespace(name="Brightline", description="Dental websites", pricing="$6,500",
                                key_benefits=["Online booking"], offer=SimpleNamespace(entry="", entry_ideas=[])),
        icp=SimpleNamespace(titles=["Practice owner"], industries=["Dental"], company_size="1-20"))


class Brain:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.answer


@pytest.mark.asyncio
async def test_suggestions_come_with_their_finding_and_why():
    brain = Brain([{"offer": "a homepage mockup with booking", "finding": "no online booking", "why": "they see it"},
                   {"offer": ""}, "junk"])
    ideas = await suggest_offers(brain, _config())
    assert ideas == [{"offer": "a homepage mockup with booking", "finding": "no online booking", "why": "they see it"}]
    assert "never a discount" in brain.prompts[0] and "Dental websites" in brain.prompts[0]


@pytest.mark.asyncio
async def test_suggestions_survive_a_wrapped_answer():
    ideas = await suggest_offers(Brain({"offers": [{"offer": "a ranking report"}]}), _config())
    assert [i["offer"] for i in ideas] == ["a ranking report"]


class _FakeWriter:
    made = []

    def __init__(self, brain, state, config, env):
        self.config = config
        _FakeWriter.made.append(self)

    async def _write_personal_email(self, prospect):
        return {"subject": "s", "body": "b"}

    async def _keep_to_rules(self, draft, step, names):
        return draft


class _SkillsBrain:
    def load_skills_for_agent(self, name):
        return ""


def _preview_setup(monkeypatch, tmp_path, rules_text="Schreiben Sie auf Deutsch."):
    import sellbird.agents.writer as writer_module
    import sellbird.offer as offer_module
    rules = tmp_path / "rules.md"
    rules.write_text(rules_text, encoding="utf-8")
    prospect = SimpleNamespace(full_name=lambda: "Ana Doe", title="Owner", company="Acme Co")

    async def pick(state):
        return prospect
    monkeypatch.setattr(offer_module, "preview_prospect", pick)
    monkeypatch.setattr(writer_module, "Writer", _FakeWriter)
    monkeypatch.setattr(writer_module, "_names", lambda p: ())
    _FakeWriter.made = []
    config = _config()
    config.writer = SimpleNamespace(humanizer=False, email_rules=str(rules))
    return config


@pytest.mark.asyncio
async def test_the_preview_leaves_the_operators_rules_out_by_default(monkeypatch, tmp_path):
    from sellbird.offer import preview_email
    config = _preview_setup(monkeypatch, tmp_path)
    result = await preview_email(_SkillsBrain(), None, config, "a homepage mockup")
    assert result["ok"] and _FakeWriter.made[-1].email_rules == ""


@pytest.mark.asyncio
async def test_the_preview_can_write_with_the_operators_rules(monkeypatch, tmp_path):
    # Rules in another language change the whole email; a preview without them
    # shows an email the operator will never send.
    from sellbird.offer import preview_email
    config = _preview_setup(monkeypatch, tmp_path)
    result = await preview_email(_SkillsBrain(), None, config, "a homepage mockup", with_rules=True)
    assert result["ok"] and _FakeWriter.made[-1].email_rules == "Schreiben Sie auf Deutsch."
    assert _FakeWriter.made[-1].config.product.offer.entry == "a homepage mockup"


@pytest.mark.asyncio
async def test_a_preview_with_unusable_rules_says_so(monkeypatch, tmp_path):
    from sellbird.offer import preview_email
    config = _preview_setup(monkeypatch, tmp_path, rules_text="")
    result = await preview_email(_SkillsBrain(), None, config, "a homepage mockup", with_rules=True)
    assert not result["ok"] and "rules" in result["message"].lower()
    assert not _FakeWriter.made


def test_saving_writes_the_offer_and_keeps_everything_else(tmp_path):
    f = tmp_path / "sellbird.local.yaml"
    f.write_text("persona:\n  name: Maya\nproduct:\n  name: Brightline\n  offer:\n    goal: book_call\n")
    save_offer(f, "  a written plan   for page one ", ["a plan", "", "a mockup"])
    data = yaml.safe_load(f.read_text())
    assert data["product"]["offer"] == {"goal": "book_call", "entry": "a written plan for page one",
                                        "entry_ideas": ["a plan", "a mockup"]}
    assert data["persona"]["name"] == "Maya"
    assert (tmp_path / "sellbird.local.yaml.bak").read_text().startswith("persona:")
