"""Company names come from page titles and URLs; what reaches an email must
be the business's name, not a title tail, a URL or "Welcome to"."""

import asyncio
import os
import tempfile

import pytest

from sellbird.collectors.discover import clean_company_name, name_from_domain
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager


@pytest.mark.parametrize("raw,domain,expected", [
    ("Example Lions FC l Ladies, girls & juniors football l", "examplelions.co.uk", "Example Lions FC"),
    ("https://www.exampleclub.co.uk/", "exampleclub.co.uk", "Exampleclub"),
    ("www.exampleclub.co.uk", "", "Exampleclub"),
    ("Exfc Co", "exfc.co.uk", "Exfc"),
    ("Uniex Ac", "uniex.ac.uk", "Uniex"),
    ("Welcome to Example Academy", "exampleacademy.org", "Example Academy"),
    ("Welcome to The Example Academy", "theexampleacademy.co.uk", "The Example Academy"),
    ("Example Neptunes Basketball Club!", "exampleneptunes.co.uk", "Example Neptunes Basketball Club"),
    ("Example Arena :", "examplearena.co.uk", "Example Arena"),
    ("Harbor Dental | Home", "harbordental.example", "Harbor Dental"),
    ("Home | Harbor Dental", "harbordental.example", "Harbor Dental"),
    # Left alone
    ("St. Example's School", "stexample.org", "St. Example's School"),
    ("Example O.N. Rovers FC", "exrovers.com", "Example O.N. Rovers FC"),
    ("The King's", "kings.example.sch.uk", "The King's"),
    ("Example Talent Group", "exampletalent.com", "Example Talent Group"),
])
def test_clean_company_name(raw, domain, expected):
    assert clean_company_name(raw, domain) == expected


def test_url_name_ignores_mailbox_host():
    # A prospect's email may be at a webmail host; the URL names the business.
    assert clean_company_name("https://www.exampleclub.co.uk/", "mail.example") == "Exampleclub"


def test_empty_name_falls_back_to_domain():
    assert clean_company_name("", "exampleclub.co.uk") == "Exampleclub"
    assert clean_company_name("", "") == ""


def test_name_from_domain_drops_second_level_suffix():
    assert name_from_domain("exampleclub.co.uk") == "Exampleclub"
    assert name_from_domain("kings.example.sch.uk") == "Kings Example"
    assert name_from_domain("acme.com") == "Acme"


def test_state_cleans_names_on_every_write():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            state = StateManager(os.path.join(tmp, "t.db"))
            await state.init_db()
            cid = await state.add_company(Company(
                name="Welcome to Example Academy", domain="exampleacademy.org"))
            assert (await state.get_company(cid)).name == "Example Academy"
            pid = await state.add_prospect(Prospect(
                company_id=cid, first_name="Jane", last_name="Doe", title="Head of PE",
                company="Example Lions FC l Ladies l", email="jane@example.com"))
            assert (await state.get_prospect(pid)).company == "Example Lions FC"
            await state.rename_company(cid, "https://www.exampleacademy.org/")
            assert (await state.get_company(cid)).name == "Exampleacademy"
            assert (await state.get_prospect(pid)).company == "Exampleacademy"
    asyncio.run(go())
