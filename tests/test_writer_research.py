"""writer.research and writer.self_check: read each company's site before
writing to it, skip the ones that don't fit, and check drafts against what
the site says.

Without research the Writer drafted from an industry name, so emails
misdescribed businesses, wrote in the wrong language, ignored the managers a
site names and went to competitors. All data here is fictional.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from sellbird import research
from sellbird.agents.writer import CHECK_FAILED_STATUS, UNFIT_STATUS, Writer
from sellbird.config import WriterConfig
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager

PAGES = [
    ("https://acme.example/", "Acme Tax Advisers\nWe advise small firms on payroll and annual accounts.\n"
     "Termin vereinbaren\n15 staff"),
    ("https://acme.example/impressum", "Impressum\nAcme GbR\nGeschäftsführerin Erika Muster"),
]
SEQ = [{"step": n, "subject": f"betreff {n}", "body": f"Guten Tag,\n\ntext {n} {{{{company}}}}\n\nViele Grüße\nS",
        "delay_days": 0 if n == 1 else 3} for n in (1, 2, 3)]
DRAFT = {"subject": "terminanfragen", "body": "Guten Tag Frau Muster,\n\nIhr Termin-Link öffnet eine E-Mail. "
         "Soll ich eine Skizze schicken?\n\nViele Grüße\nS"}


def profile(fit="good", competitor=False, language="de"):
    return {
        "what_they_do": "Tax advice for small firms", "customers": "small firms", "size": "15 staff",
        "language": language, "intake": "appointment requests by email",
        "already_has": "", "competitor": competitor, "fit": fit,
        "reason": "manual appointment handling",
        "people": [{"name": "Erika Muster", "role": "Geschäftsführerin", "salutation": "Frau"}],
        "hooks": [{"observation": "appointments are requested by email",
                   "evidence": "Termin vereinbaren", "url": "https://acme.example/"}],
    }


class Brain:
    """Answers by task, and records every prompt."""

    def __init__(self, research_reply=None, checks=None):
        self.research_reply = research_reply if research_reply is not None else profile()
        self.checks = list(checks or [])
        self.prompts: list[tuple[str, str]] = []
        self.paused_reason = ""

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    def tasks(self):
        return [t for t, _ in self.prompts]

    async def think_json(self, prompt, **kw):
        task = kw.get("task", "")
        self.prompts.append((task, prompt))
        if task == "research":
            return self.research_reply
        if task == "write_sequence":
            return SEQ
        if task in ("personal_email", "self_check_fix", "fix_draft"):
            return dict(DRAFT)
        if task == "personal_follow_ups":
            return [{"step": 2, "subject": "noch ein Gedanke", "body": "Guten Tag Frau Muster,\n\nanderer Punkt.\n\nViele Grüße\nS"},
                    {"step": 3, "subject": "Angebot bleibt", "body": "Guten Tag Frau Muster,\n\nich lasse es dabei.\n\nViele Grüße\nS"}]
        if task == "self_check":
            return {"problems": self.checks.pop(0) if self.checks else []}
        return None


def config(tmp_path, research_on=True, self_check=False):
    rules = tmp_path / "rules.md"
    rules.write_text("Write in German with Sie. Sign off with Viele Grüße.", encoding="utf-8")
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="gmail", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing="",
                                offer=SimpleNamespace(primary="audit", entry="a free sketch")),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
        icp=SimpleNamespace(industries=["Steuerberatung"]),
        writer=SimpleNamespace(humanizer=False, email_rules=str(rules),
                               research=research_on, self_check=self_check),
    )


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "r.db"))
    await s.init_db()
    return s


@pytest.fixture(autouse=True)
def site(monkeypatch):
    calls = []

    async def fake_read_site(domain, website="", client=None):
        calls.append(domain)
        return [] if domain.startswith("down.") else PAGES

    monkeypatch.setattr(research, "read_site", fake_read_site)
    return calls


async def inbox(state, domain="acme.example"):
    cid = await state.add_company(Company(name="Acme", domain=domain, industry="Steuerberatung"))
    pid = await state.add_prospect(Prospect(
        company="Acme", industry="Steuerberatung", email=f"info@{domain}", email_status="verified",
        company_id=cid, source="company_inbox", title="General inbox"))
    return cid, pid


async def status(state, pid):
    return (await state.get_prospect(pid)).status


# ── settings ──

def test_both_options_are_off_by_default():
    assert WriterConfig().research is False and WriterConfig().self_check is False


# ── cleaning what the model returns ──

def test_a_hook_or_person_the_pages_dont_contain_is_dropped():
    reply = profile()
    reply["hooks"].append({"observation": "x", "evidence": "they won an award in 2020", "url": ""})
    reply["people"].append({"name": "Max Erfunden", "role": "CEO", "salutation": "Herr"})
    clean = research._clean(reply, PAGES)
    assert [h["evidence"] for h in clean["hooks"]] == ["Termin vereinbaren"]
    assert [p["name"] for p in clean["people"]] == ["Erika Muster"]


def test_a_competitor_is_always_rejected_and_unknown_values_are_tidied():
    clean = research._clean({**profile(fit="good", competitor=True), "language": "DE-de"}, PAGES)
    assert clean["fit"] == "reject" and clean["language"] == "de"
    assert research._clean({**profile(), "fit": "maybe"}, PAGES)["fit"] == "weak"


def test_a_courtesy_title_in_any_language_is_kept_but_not_a_sentence():
    for title, kept in (("Frau", "Frau"), ("Mme", "Mme"), ("Sr.", "Sr."), ("Ms", "Ms"),
                        ("the managing director", ""), ("<b>", "")):
        reply = profile()
        reply["people"][0]["salutation"] = title
        assert research._clean(reply, PAGES)["people"][0]["salutation"] == kept


def test_competitors_are_judged_against_what_the_operator_sells():
    cfg = SimpleNamespace(product=SimpleNamespace(name="Example Bakery Supply",
                                                  description="Flour delivered to bakeries", offer=None),
                          persona=SimpleNamespace(company="Example Co"), icp=None)
    prompt = research.research_prompt("Acme", PAGES, cfg)
    assert "Example Bakery Supply: Flour delivered to bakeries" in prompt
    assert "they themselves sell what we sell" in prompt
    assert "automation" not in prompt.lower()
    assert research._clean("not json", PAGES) is None


def test_links_to_the_legal_services_and_contact_pages_are_picked():
    html = """<a href="/impressum">Impressum</a><a href="/leistungen/">Leistungen</a>
    <a href="/kontakt">Kontakt</a><a href="/datenschutz">Datenschutz</a>
    <a href="/blog/post-1">Neu</a><a href="https://other.example/team">Team</a>
    <a href="/files/x.pdf">PDF</a><a href="/">Home</a>"""
    links = research.pick_links(html, "https://acme.example/")
    assert links == ["https://acme.example/impressum", "https://acme.example/leistungen/",
                     "https://acme.example/kontakt"]


@pytest.mark.asyncio
async def test_a_homepage_without_a_readable_menu_falls_back_to_the_usual_pages(monkeypatch):
    monkeypatch.undo()  # the real read_site, with a fake client
    pages = {"https://acme.example/": "<p>Acme, menu built by script</p>",
             "https://acme.example/impressum/": "<p>Impressum Acme GbR</p>",
             "https://acme.example/kontakt/": "<p>Kontakt</p>"}

    class Resp:
        def __init__(self, url):
            self.status_code = 200 if url in pages else 404
            self.text, self.headers = pages.get(url, ""), {"content-type": "text/html"}

    class Client:
        async def get(self, url, headers=None):
            return Resp(url)

    got = await research.read_site("acme.example", client=Client())
    assert [u for u, _ in got] == list(pages)


def test_fact_lines_carry_the_people_language_and_quoted_hooks():
    lines = "\n".join(research.fact_lines(profile()))
    assert "Frau Erika Muster (Geschäftsführerin)" in lines
    assert "Website language: German" in lines
    assert 'the site says: "Termin vereinbaren"' in lines


# ── the gate ──

@pytest.mark.asyncio
@pytest.mark.parametrize("fit, competitor, suppressed", [
    ("reject", True, True), ("weak", False, False)])
async def test_an_unfit_company_is_parked_and_never_drafted(state, tmp_path, fit, competitor, suppressed):
    _, pid = await inbox(state)
    brain = Brain(research_reply=profile(fit=fit, competitor=competitor))
    await Writer(brain, state, config(tmp_path), None).run()
    assert await status(state, pid) == UNFIT_STATUS
    assert brain.tasks() == ["research"]
    assert bool(await state.is_suppressed("info@acme.example")) is suppressed


@pytest.mark.asyncio
async def test_research_is_cached_so_a_rejected_company_costs_one_call(state, tmp_path, site):
    await inbox(state)
    brain = Brain(research_reply=profile(fit="reject", competitor=True))
    await Writer(brain, state, config(tmp_path), None).run()
    # A new contact at the same company later on.
    cid = (await state.get_prospects_by_status(UNFIT_STATUS))[0].company_id
    pid2 = await state.add_prospect(Prospect(
        first_name="Erika", last_name="Muster", title="CEO", company="Acme", email="em@acme.example",
        email_status="verified", company_id=cid))
    await Writer(brain, state, config(tmp_path), None).run()
    assert brain.tasks() == ["research"] and site == ["acme.example"]
    assert await status(state, pid2) == UNFIT_STATUS


@pytest.mark.asyncio
async def test_a_good_fit_is_written_from_the_research(state, tmp_path):
    _, pid = await inbox(state)
    brain = Brain()
    await Writer(brain, state, config(tmp_path), None).run()
    assert await status(state, pid) == "queued"
    personal = dict(brain.prompts)["personal_email"]
    assert "Research from their own website" in personal
    assert "Greet them by name" in personal and "Erika Muster" in personal
    assert "Write the whole email in German" in personal
    assert "Open with one of the 'Possible opening' facts" in personal
    assert "Write all three emails in German" in dict(brain.prompts)["write_sequence"]


@pytest.mark.asyncio
async def test_an_unreadable_site_is_written_for_as_before(state, tmp_path):
    _, pid = await inbox(state, domain="down.example")
    brain = Brain()
    await Writer(brain, state, config(tmp_path), None).run()
    assert await status(state, pid) == "queued"
    assert "Research from their own website" not in dict(brain.prompts)["personal_email"]


@pytest.mark.asyncio
async def test_off_nothing_is_researched(state, tmp_path, site):
    _, pid = await inbox(state)
    brain = Brain()
    await Writer(brain, state, config(tmp_path, research_on=False), None).run()
    assert "research" not in brain.tasks() and site == []
    assert await status(state, pid) == "queued"


@pytest.mark.asyncio
async def test_companies_in_different_languages_get_separate_campaigns(state, tmp_path, monkeypatch):
    await inbox(state, "acme.example")
    await inbox(state, "beta.example")

    async def by_domain(brain, company, cfg, client=None):
        return {**profile(language="en" if company.domain == "beta.example" else "de"),
                "researched_at": "2099-01-01T00:00:00"}

    monkeypatch.setattr(research, "research_company", by_domain)
    brain = Brain()
    await Writer(brain, state, config(tmp_path), None).run()
    assert brain.tasks().count("write_sequence") == 2


# ── the self-check ──

@pytest.mark.asyncio
async def test_a_draft_that_passes_the_check_is_staged(state, tmp_path):
    _, pid = await inbox(state)
    brain = Brain(checks=[[]])
    await Writer(brain, state, config(tmp_path, self_check=True), None).run()
    assert "self_check" in brain.tasks() and "self_check_fix" not in brain.tasks()[:4]
    assert await status(state, pid) == "queued"


@pytest.mark.asyncio
async def test_a_draft_fixed_after_one_rewrite_is_staged(state, tmp_path):
    _, pid = await inbox(state)
    brain = Brain(checks=[["opens with the industry"], []])
    await Writer(brain, state, config(tmp_path, self_check=True), None).run()
    first = brain.tasks()[:brain.tasks().index("personal_follow_ups")]
    assert first.count("self_check") == 2 and "self_check_fix" in first
    assert await status(state, pid) == "queued"


@pytest.mark.asyncio
async def test_a_draft_that_fails_twice_is_not_staged(state, tmp_path):
    _, pid = await inbox(state)
    brain = Brain(checks=[["invents a claim"], ["still invents it"]])
    await Writer(brain, state, config(tmp_path, self_check=True), None).run()
    assert await status(state, pid) == CHECK_FAILED_STATUS


# ── follow-ups written from the research ──

async def rows(state, pid):
    return {r["step"]: r for r in await state.get_outbox(status="pending_review") if r["prospect_id"] == pid}


@pytest.mark.asyncio
async def test_follow_ups_are_written_for_the_company_and_staged_after_email_one(state, tmp_path):
    _, pid = await inbox(state)
    brain = Brain()
    await Writer(brain, state, config(tmp_path), None).run()
    staged = await rows(state, pid)
    assert sorted(staged) == [1, 2, 3]
    assert staged[2]["subject"] == "noch ein Gedanke" and "Frau Muster" in staged[3]["body"]
    assert staged[1]["send_at"] < staged[2]["send_at"] < staged[3]["send_at"]
    prompt = dict(brain.prompts)["personal_follow_ups"]
    assert "No sentence about what the sender's company does" in prompt


@pytest.mark.asyncio
async def test_a_follow_up_that_fails_its_check_is_left_to_the_template(state, tmp_path):
    _, pid = await inbox(state)
    # email 1 passes; follow-up 2 fails twice; follow-up 3 passes.
    brain = Brain(checks=[[], ["pitches"], ["still pitches"], []])
    await Writer(brain, state, config(tmp_path, self_check=True), None).run()
    assert sorted(await rows(state, pid)) == [1, 3]


@pytest.mark.asyncio
async def test_no_follow_ups_are_written_when_follow_ups_are_off(state, tmp_path):
    _, pid = await inbox(state)
    cfg = config(tmp_path)
    cfg.channels.email.follow_ups = False
    brain = Brain()
    await Writer(brain, state, cfg, None).run()
    assert "personal_follow_ups" not in brain.tasks() and sorted(await rows(state, pid)) == [1]


@pytest.mark.asyncio
async def test_the_first_email_prompt_forbids_a_pitch(state, tmp_path):
    await inbox(state)
    brain = Brain()
    await Writer(brain, state, config(tmp_path), None).run()
    assert "Write no sentence about what you or your company do" in dict(brain.prompts)["personal_email"]


@pytest.mark.asyncio
async def test_follow_ups_must_keep_the_first_emails_greeting(state, tmp_path):
    await inbox(state)
    brain = Brain(checks=[[], [], []])
    await Writer(brain, state, config(tmp_path, self_check=True), None).run()
    checks = [p for t, p in brain.prompts if t == "self_check"]
    assert "It does not greet Erika Muster by name" in checks[0]
    assert "not exactly the first email's greeting: 'Guten Tag Frau Muster,'" in checks[1]
    assert "Exactly the first email's greeting line ('Guten Tag Frau Muster,')" in dict(brain.prompts)["personal_follow_ups"]
