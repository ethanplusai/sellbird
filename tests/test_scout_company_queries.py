from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sellbird.agents.scout import MAX_QUERIES_PER_CYCLE, Scout


def _scout(industries, geography, company_size="20-1000 employees", settings=None):
    settings = {} if settings is None else settings

    async def get_setting(key, default=""):
        return settings.get(key, default)

    async def set_setting(key, value):
        settings[key] = value

    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    state = SimpleNamespace(
        log_action=AsyncMock(), get_setting=get_setting, set_setting=set_setting,
        company_exists=AsyncMock(return_value=True),
    )
    icp = SimpleNamespace(industries=industries, geography=geography,
                          company_size=company_size)
    config = SimpleNamespace(icp=icp)
    return Scout(brain, state, config, SimpleNamespace()), settings


def test_company_size_is_never_quoted_into_the_query():
    """No page says "20-1000 employees" verbatim, so quoting it made every
    search come back empty."""
    scout, _ = _scout(["roofing"], ["Denver, CO"])
    queries = scout._build_company_search_queries()
    assert queries == ['"roofing" "Denver, CO" company']
    assert not any("employees" in q for q in queries)


def test_queries_reach_every_industry_and_place():
    industries = ["roofing", "plumbing", "hvac", "dental", "legal"]
    geos = ["Denver, CO", "Austin, TX"]
    scout, _ = _scout(industries, geos)
    pairs = len(industries) * len(geos)
    seen = set()
    for offset in range(0, pairs, MAX_QUERIES_PER_CYCLE):
        seen.update(scout._build_company_search_queries(offset))
    assert len(seen) == pairs
    assert '"legal" "Austin, TX" company' in seen


@pytest.mark.asyncio
async def test_each_cycle_advances_the_rotation():
    scout, settings = _scout(["roofing", "plumbing", "hvac", "dental"], ["Denver, CO"])
    scout._web_search = AsyncMock(return_value=[])
    await scout._find_companies_via_search()
    first = [c.args[0] for c in scout._web_search.await_args_list]
    assert settings["scout_query_offset"] == str(MAX_QUERIES_PER_CYCLE % 4)

    scout._web_search.reset_mock()
    await scout._find_companies_via_search()
    second = [c.args[0] for c in scout._web_search.await_args_list]
    assert first[0] != second[0]
    assert '"dental" "Denver, CO" company' in second


def test_the_company_word_follows_the_market_language():
    """A Spanish-language market searches "empresa"; "company" pulled in
    English-language directories instead of the companies themselves."""
    scout, _ = _scout(["construcción"], ["Ciudad Ejemplo"])
    scout.config.icp.company_search_word = "empresa"
    assert scout._build_company_search_queries() == ['"construcción" "Ciudad Ejemplo" empresa']


def test_empty_icp_builds_no_query():
    scout, _ = _scout([], [], company_size="")
    assert scout._build_company_search_queries() == []
