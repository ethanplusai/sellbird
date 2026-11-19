"""The possessive merge: "{{company}}'s" for a name ending in s."""

from types import SimpleNamespace

from harvey.agents.sender import Sender


def _render(text, company):
    s = Sender.__new__(Sender)
    prospect = SimpleNamespace(first_name="Dana", last_name="Lee", company=company,
                               title="", personalization_notes="",
                               is_company_inbox=False)
    return s._render(text, prospect)


# ── possessive merge ──

def test_possessive_for_company_ending_in_s():
    assert _render("in {{company}}'s voice", "Parkes Builders") == "in Parkes Builders' voice"


def test_possessive_for_other_companies_unchanged():
    assert _render("in {{company}}'s voice", "Acme Dental") == "in Acme Dental's voice"


# ── company names as a person would say them ──

def test_register_names_are_tidied_for_the_email():
    from harvey.agents.sender import spoken_company
    assert spoken_company("ACME 24 CARE NORTH") == "Acme 24 Care North"
    assert spoken_company("Example Homecare Ltd") == "Example Homecare"
    assert spoken_company("Example Care Solutions - Main Office") == "Example Care Solutions"
    assert spoken_company("EXAMPLE CARE (UK) LIMITED") == "Example Care (UK)"
    assert spoken_company("Example Care \ufffd Hill Lodge") == "Example Care – Hill Lodge"
    assert spoken_company("Example & Sons Care") == "Example & Sons Care"


def test_merge_uses_the_tidy_name():
    assert _render("at {{company}}", "EXAMPLE HOMECARE LTD") == "at Example Homecare"
