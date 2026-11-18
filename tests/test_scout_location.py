"""Scout records the place it searched, so the Writer can put priority places first.

Discovery already stamps the place onto the company it found. Scout's own
searches did not: a company found by a "Nashik" search arrived with no
location, and the Writer's priority ordering (icp.priority_geography) skips a
prospect whose place is unknown — silently, because an empty place is a
legitimate answer for a company found some other way.

These tests hold Scout to the discovery rule: the place a search was scoped to
is the place its results are recorded in, a company that predates the column
gets filled in rather than left blank, and the priority places are worked
through in order instead of the first one being searched forever.
"""

from types import SimpleNamespace

import pytest

from sellbird.agents.scout import Scout


NASHIK = "Nashik, India"
PUNE = "Pune, India"


class FakeState:
    def __init__(self, existing=None):
        self.existing = existing
        self.filled = []
        self.settings = {}

    async def company_exists(self, domain):
        return False

    async def get_company_by_domain(self, domain):
        return self.existing

    async def set_company_location(self, company_id, location):
        self.filled.append((company_id, location))

    async def add_company(self, company):
        return "new-" + company.domain

    async def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value


def make_scout(priority=(NASHIK, PUNE), geography=(NASHIK, PUNE, "Ludhiana, India")):
    config = SimpleNamespace(
        icp=SimpleNamespace(
            titles=["Owner"],
            industries=["Manufacturing"],
            company_size="20-500 employees",
            geography=list(geography),
            priority_geography=list(priority),
        ),
        channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)),
    )
    scout = Scout(brain=None, state=FakeState(), config=config, env=SimpleNamespace())
    scout._seen_domains = set()
    scout._seen_prospect_keys = set()
    scout._queries_this_cycle = 0
    scout._current_place = None
    return scout


def next_cycle(scout):
    """What the loop does between cycles."""
    scout._current_place = None


# ── The query itself ──


def test_the_search_reads_like_a_search_not_a_filter():
    scout = make_scout()
    queries = scout._build_company_search_queries(NASHIK)
    assert queries, "expected at least one search"
    query, place = queries[0]
    assert place == NASHIK
    assert "Nashik" in query
    # Quoted AND-ed terms are what came back as dictionary entries for the
    # word "manufacturing" from every backend.
    assert '"' not in query


def test_a_search_carries_the_place_it_was_scoped_to():
    scout = make_scout()
    assert all(place == PUNE for _, place in scout._build_company_search_queries(PUNE))


def test_no_place_leaves_the_place_blank_rather_than_guessing():
    scout = make_scout()
    queries = scout._build_company_search_queries("")
    assert queries
    assert all(place == "" for _, place in queries)


# ── The rotation across priority places ──


@pytest.mark.asyncio
async def test_the_search_place_advances_through_the_priority_list():
    scout = make_scout()
    assert await scout._cycle_place() == NASHIK
    next_cycle(scout)
    assert await scout._cycle_place() == PUNE
    next_cycle(scout)
    assert await scout._cycle_place() == NASHIK  # wraps


@pytest.mark.asyncio
async def test_the_chosen_place_is_kept_for_the_whole_cycle():
    scout = make_scout()
    first = await scout._cycle_place()
    # Every place-scoped strategy in one cycle must agree, or the Writer gets
    # companies from one city while the prospects claim another.
    assert await scout._cycle_place() == first
    assert await scout._cycle_place() == first


@pytest.mark.asyncio
async def test_without_a_priority_list_the_places_still_rotate():
    scout = make_scout(priority=())
    assert await scout._cycle_place() == NASHIK  # geography[0]
    next_cycle(scout)
    assert await scout._cycle_place() == PUNE


@pytest.mark.asyncio
async def test_the_rotation_position_survives_a_restart():
    # The loop restarts often. A counter held only in memory would put every
    # session back on the first city.
    scout = make_scout()
    await scout._cycle_place()
    state = scout.state

    restarted = make_scout()
    restarted.state = state
    next_cycle(restarted)
    assert await restarted._cycle_place() == PUNE


# ── Where the result is recorded ──


@pytest.mark.asyncio
async def test_a_company_found_by_a_place_search_records_the_place():
    scout = make_scout()

    async def search(query):
        return [("https://acme.example", "Acme makes things")]

    async def info(domain):
        return {"name": "Acme"}

    scout._web_search = search
    scout._scrape_company_info = info

    companies = await scout._find_companies_via_search()
    assert companies[0]["location"] == NASHIK


@pytest.mark.asyncio
async def test_the_place_scraped_off_the_site_is_not_overwritten():
    scout = make_scout()

    async def search(query):
        return [("https://acme.example", "")]

    async def info(domain):
        # The company's own page said where it is; that beats the search.
        return {"name": "Acme", "location": "Satpur, Nashik"}

    scout._web_search = search
    scout._scrape_company_info = info

    companies = await scout._find_companies_via_search()
    assert companies[0]["location"] == "Satpur, Nashik"


@pytest.mark.asyncio
async def test_an_existing_company_without_a_place_is_filled_in():
    existing = SimpleNamespace(id="c1", location="")
    scout = make_scout()
    scout.state = FakeState(existing)

    company_id = await scout._ensure_company(
        name="Acme", domain="acme.example", location=NASHIK
    )

    assert company_id == "c1"
    assert scout.state.filled == [("c1", NASHIK)]


@pytest.mark.asyncio
async def test_an_existing_company_that_already_has_a_place_is_left_alone():
    existing = SimpleNamespace(id="c1", location=PUNE)
    scout = make_scout()
    scout.state = FakeState(existing)

    company_id = await scout._ensure_company(
        name="Acme", domain="acme.example", location=NASHIK
    )

    assert company_id == "c1"
    assert scout.state.filled == []
