"""A key that runs out is shown every way to carry on that Precleared offers.

Precleared's 402 answer carries more than the pack's checkout link: a starter
pack for a first purchase, the monthly plan when it would cover what the key
sends, and the Growth plan for a key that needs more than 100 checks a day.
Sellbird showed only the pack. The hold on the Today page and in
`sellbird status` now lists each offered option with its price and its own
link. Labels are Sellbird's words around bounded figures, never the answer's
text; every link passes the same test as the pack's. No network: every
Precleared call goes to an httpx.MockTransport, and nothing is sent.
"""

import json

import httpx
import pytest

from sellbird.integrations.precleared import (
    BUY_URL,
    MAX_FIX_OPTIONS,
    Clearance,
    PreclearedClient,
    _buy_options,
    current_hold,
    hold_message,
    record_hold,
)
from tests.test_dashboard_signals import client  # noqa: F401 (fixture)
from tests.test_outbox_native import FakeProvider, make_sender
from tests.test_precleared import mock_status, with_check
from tests.test_precleared_hold import (  # noqa: F401 (state is a fixture)
    _now,
    _seed,
    check_on,
    state,
    stored,
    two_cold_and_a_reply,
)

PAY = "https://precleared.dev/checkout?t=AbCdEfGhIjKlMnOpQrStUvWx"
STARTER = PAY + "&p=starter&src=402"
PACK = PAY + "&p=standard&src=402"
MONTHLY = PAY + "&p=monthly&src=402"
GROWTH = PAY + "&p=growth&src=402"
MANAGE = "https://precleared.dev/manage"
KEY = "pc_live_Zq8vN3kW1xR7tB5yM2"


def answer(**changes):
    """A 402 body as Precleared sends it to a key on 25 checks a day that has
    never bought anything: starter and pack, and the monthly plan recommended."""
    body = {
        "error": "payment_required",
        "buy_url": PACK,
        "starter_url": STARTER,
        "plan_url": MONTHLY,
        "manage_url": MANAGE,
        "packs": [
            {"id": "starter", "name": "Starter pack", "price_gbp": 9, "checks": 300, "url": STARTER},
            {"id": "pack", "name": "Pack", "price_gbp": 39, "checks": 2000, "url": PACK},
        ],
        "recommended": {"plan": "monthly", "price": "£15 a month", "per_day": 100, "url": MONTHLY},
        "limit": 25,
    }
    body.update(changes)
    return {k: v for k, v in body.items() if v is not None}


GROWTH_OFFER = {"id": "growth", "name": "Growth plan", "price_gbp_per_month": 49,
                "checks_per_day": 500, "url": GROWTH}


def options(body, key=KEY):
    return list(_buy_options(httpx.Response(402, json=body), key))


# ── what is read from the answer ──


def test_starter_monthly_and_pack_are_listed_cheapest_commitment_first():
    assert options(answer()) == [
        {"label": "a starter pack, £9 for 300 checks", "url": STARTER},
        {"label": "the monthly plan, £15 a month for 100 checks a day", "url": MONTHLY},
        {"label": "a pack, £39 for 2,000 checks", "url": PACK},
    ]


def test_growth_is_listed_and_the_monthly_plan_dropped_when_growth_is_recommended():
    # A key in its first 14 days already has 100 a day, so the monthly plan
    # would send nothing today; the answer recommends Growth instead.
    body = answer(growth=GROWTH_OFFER, growth_url=GROWTH,
                  recommended={"plan": "growth", "price": "£49 a month", "per_day": 500, "url": GROWTH})

    assert options(body) == [
        {"label": "a starter pack, £9 for 300 checks", "url": STARTER},
        {"label": "a pack, £39 for 2,000 checks", "url": PACK},
        {"label": "the Growth plan, £49 a month for 500 checks a day", "url": GROWTH},
    ]


def test_a_key_on_the_monthly_plan_is_offered_the_switch_to_growth():
    body = answer(starter_url=None, plan_url=None, recommended=None,
                  packs=[{"id": "pack", "price_gbp": 39, "checks": 2000, "url": PACK}],
                  growth={**GROWTH_OFFER, "url": MANAGE, "switch": True})

    assert options(body) == [
        {"label": "a pack, £39 for 2,000 checks", "url": PACK},
        {"label": "a switch to the Growth plan, £49 a month for 500 checks a day", "url": MANAGE},
    ]


def test_a_second_purchase_has_no_starter_pack():
    body = answer(starter_url=None,
                  packs=[{"id": "pack", "price_gbp": 39, "checks": 2000, "url": PACK}])

    assert [o["label"] for o in options(body)] == [
        "the monthly plan, £15 a month for 100 checks a day",
        "a pack, £39 for 2,000 checks",
    ]


def test_a_price_with_pence_keeps_them():
    body = answer(packs=[{"id": "pack", "price_gbp": 39.5, "checks": 2000, "url": PACK}],
                  recommended=None)

    assert options(body) == [{"label": "a pack, £39.50 for 2,000 checks", "url": PACK}]


@pytest.mark.parametrize("body", [
    {"error": "payment_required", "buy_url": PACK},          # an older answer
    {"buy_url": PACK, "packs": "two", "recommended": [], "growth": "yes"},
    {"buy_url": PACK, "packs": [None, 3, "pack", []]},
    [],
    "not an object",
])
def test_an_answer_without_the_fields_offers_nothing_extra(body):
    assert options(body) == []


def test_a_body_that_is_not_json_offers_nothing():
    assert list(_buy_options(httpx.Response(402, text="<html>busy</html>"), KEY)) == []


@pytest.mark.parametrize("pack", [
    {"id": "pack", "price_gbp": "39", "checks": 2000, "url": PACK},       # a price as text
    {"id": "pack", "price_gbp": True, "checks": 2000, "url": PACK},
    {"id": "pack", "price_gbp": 0, "checks": 2000, "url": PACK},
    {"id": "pack", "price_gbp": -39, "checks": 2000, "url": PACK},
    {"id": "pack", "price_gbp": 10_001, "checks": 2000, "url": PACK},
    {"id": "pack", "price_gbp": 39, "checks": 0, "url": PACK},
    {"id": "pack", "price_gbp": 39, "checks": 12.5, "url": PACK},
    {"id": "pack", "price_gbp": 39, "checks": "2000", "url": PACK},
    {"id": "pack", "price_gbp": 39, "checks": 10_000_001, "url": PACK},
    {"id": "pack", "price_gbp": 39, "checks": 2000},                       # no link
    {"id": "pack", "price_gbp": 39, "checks": 2000, "url": "https://evil.example/pay"},
    {"id": "pack", "price_gbp": 39, "checks": 2000, "url": "http://precleared.dev/buy"},
    {"id": "mega", "price_gbp": 39, "checks": 2000, "url": PACK},          # a kind Sellbird doesn't know
    {"id": ["pack"], "price_gbp": 39, "checks": 2000, "url": PACK},
])
def test_an_option_with_a_bad_figure_or_link_is_left_out(pack):
    assert options({"buy_url": PACK, "packs": [pack]}) == []


def test_figures_that_are_not_numbers_at_all_are_left_out():
    body = '{"packs": [{"id": "pack", "price_gbp": NaN, "checks": 2000, "url": "%s"}]}' % PACK

    assert list(_buy_options(httpx.Response(402, text=body), KEY)) == []


@pytest.mark.parametrize("recommended", [
    {"plan": "monthly", "price": "£15 a month", "per_day": 0, "url": MONTHLY},
    {"plan": "monthly", "price": "free today only!", "per_day": 100, "url": MONTHLY},
    {"plan": "monthly", "price": "£15 a month\x1b[2J", "per_day": 100, "url": MONTHLY},
    {"plan": "monthly", "price": 15, "per_day": 100, "url": MONTHLY},
    {"plan": "monthly", "price": "£15 a month", "per_day": 100, "url": "https://evil.example/plan"},
    {"plan": "yearly", "price": "£15 a month", "per_day": 100, "url": MONTHLY},
])
def test_a_monthly_plan_sellbird_cannot_vouch_for_is_left_out(recommended):
    body = answer(recommended=recommended, plan_url=None)

    assert [o["label"] for o in options(body)] == [
        "a starter pack, £9 for 300 checks", "a pack, £39 for 2,000 checks"]


def test_the_plan_link_is_used_when_the_recommendation_has_none():
    body = answer(recommended={"plan": "monthly", "price": "£15 a month", "per_day": 100})

    assert {"label": "the monthly plan, £15 a month for 100 checks a day", "url": MONTHLY} in options(body)


def test_the_answers_own_names_never_reach_a_label():
    body = answer(packs=[
        {"id": "starter", "name": "FREE!!! \x1b]8;;https://evil.example\x07click", "price_gbp": 9,
         "checks": 300, "url": STARTER},
        {"id": "pack", "name": "<b>Pack</b>", "price_gbp": 39, "checks": 2000, "url": PACK},
    ])

    text = json.dumps(options(body))
    assert "FREE" not in text and "evil" not in text and "<b>" not in text


def test_a_link_that_carries_the_key_is_left_out():
    leaky = "https://precleared.dev/checkout?key=" + KEY
    body = answer(packs=[{"id": "starter", "price_gbp": 9, "checks": 300, "url": leaky},
                         {"id": "pack", "price_gbp": 39, "checks": 2000, "url": PACK}])

    found = options(body)
    assert KEY not in json.dumps(found)
    assert [o["url"] for o in found] == [MONTHLY, PACK]


def test_only_the_first_of_each_kind_counts_and_never_more_than_the_cap():
    body = answer(growth=GROWTH_OFFER, packs=[
        {"id": "starter", "price_gbp": 9, "checks": 300, "url": STARTER},
        {"id": "starter", "price_gbp": 1, "checks": 9999, "url": STARTER},
        {"id": "pack", "price_gbp": 39, "checks": 2000, "url": PACK},
        {"id": "pack", "price_gbp": 2, "checks": 9999, "url": PACK},
    ])

    found = options(body)
    assert len(found) == 4 == MAX_FIX_OPTIONS
    assert found[0]["label"] == "a starter pack, £9 for 300 checks"
    assert found[2]["label"] == "a pack, £39 for 2,000 checks"


# ── what the sender records ──


@pytest.mark.asyncio
async def test_the_sender_records_the_options_with_the_hold(state):  # noqa: F811
    await two_cold_and_a_reply(state)
    sender = with_check(make_sender(state, FakeProvider()), mock_status(402, answer()), api_key=KEY)

    await sender._drain_due()

    hold = await stored(state)
    assert hold["kind"] == "out_of_checks"
    assert hold["url"] == PACK
    assert [o["url"] for o in hold["options"]] == [STARTER, MONTHLY, PACK]
    assert KEY not in json.dumps(hold)


@pytest.mark.asyncio
async def test_the_outbox_reason_still_names_the_pack(state):  # noqa: F811
    client_ = PreclearedClient("GB", api_key=KEY, transport=mock_status(402, answer()))

    result = await client_.clear("person@example.com", state)

    assert result.held and not result.allowed
    assert f"add a pack ({PACK})" in result.reason
    assert len(result.fix_options) == 3


@pytest.mark.asyncio
async def test_an_older_answer_records_no_options(state):  # noqa: F811
    await two_cold_and_a_reply(state)
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_status(402, {"error": "payment_required", "buy_url": PACK}), api_key=KEY)

    await sender._drain_due()

    hold = await stored(state)
    assert hold["url"] == PACK
    assert "options" not in hold


@pytest.mark.asyncio
async def test_options_are_kept_only_for_a_key_out_of_checks(state):  # noqa: F811
    stray = ({"label": "a pack, £39 for 2,000 checks", "url": PACK},)
    await record_hold(state, Clearance(allowed=False, held=True, hold_kind="keyless_limit",
                                       fix_url="https://precleared.dev/get-key",
                                       fix_options=stray), waiting=2)

    assert "options" not in await stored(state)


@pytest.mark.asyncio
async def test_a_recorded_option_is_cleaned_and_capped(state):  # noqa: F811
    many = tuple({"label": f"a pack, £{n} for 100 checks", "url": PACK} for n in range(1, 9))
    dirty = ({"label": "a pack,\x1b £39\nfor\t2,000 checks" + "x" * 300, "url": PACK},
             {"label": "elsewhere", "url": "https://evil.example/pay"},
             {"label": "", "url": PACK}, "pack", None)

    await record_hold(state, Clearance(allowed=False, held=True, hold_kind="out_of_checks",
                                       fix_url=PACK, fix_options=dirty + many), waiting=1)

    kept = (await stored(state))["options"]
    assert len(kept) == MAX_FIX_OPTIONS
    assert kept[0]["label"].startswith("a pack, £39 for 2,000 checks")
    assert len(kept[0]["label"]) == 120 and "\x1b" not in kept[0]["label"]
    assert all(o["url"] == PACK for o in kept)


# ── what the user reads ──


def held(options_=None, **extra):
    hold = {"status": "held", "kind": "out_of_checks", "url": PACK, "waiting": 3,
            "since": _now(), "at": _now(), "reason": ""}
    if options_ is not None:
        hold["options"] = options_
    hold.update(extra)
    return hold


def test_the_message_lists_each_option_with_its_link():
    message = hold_message(held(options(answer())))

    assert message["title"] == "Your Precleared key is out of checks for today"
    assert message["detail"] == (
        "3 cold emails are waiting. The key has used today's free checks and has no pack left. "
        f"To send them now, pick one: a starter pack, £9 for 300 checks ({STARTER}); "
        f"the monthly plan, £15 a month for 100 checks a day ({MONTHLY}); "
        f"or a pack, £39 for 2,000 checks ({PACK}). "
        "Otherwise they go out after the free checks reset at midnight UK time, or switch the "
        "check off (compliance.precleared.enabled: false). Replies are not held.")
    # The one link the Today item carries is still the pack's.
    assert message["url"] == PACK and message["url_label"] == "Buy checks"


def test_two_options_read_as_a_pair():
    message = hold_message(held(options(answer(recommended=None))))

    assert (f"pick one: a starter pack, £9 for 300 checks ({STARTER}); "
            f"or a pack, £39 for 2,000 checks ({PACK}).") in message["detail"]


@pytest.mark.parametrize("stored_options", [
    None, [], "pack", {"label": "a pack", "url": PACK},
    [{"label": "a pack, £39 for 2,000 checks", "url": PACK}],     # one option: nothing to choose
])
def test_without_a_choice_the_message_reads_as_before(stored_options):
    message = hold_message(held(stored_options))

    assert f"Buy checks to send them now: {PACK}." in message["detail"]
    assert "pick one" not in message["detail"]


def test_a_tampered_stored_hold_cannot_show_a_foreign_link_or_drive_a_terminal():
    message = hold_message(held([
        {"label": "a starter pack, £9 for 300 checks", "url": STARTER},
        {"label": "a better deal", "url": "https://evil.example/pay"},
        {"label": "a pack\x1b]8;;https://evil.example\x07, £39\x9b31m", "url": PACK},
        {"label": "see https://evil.example/pay", "url": PACK},
        {"label": "<a href=x>a pack</a>", "url": PACK},
        {"label": 7, "url": PACK},
        {"label": "a pack, £39 for 2,000 checks", "url": PACK},
    ]))

    assert "evil.example" not in message["detail"]
    assert "<a" not in message["detail"]
    assert not any(ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in message["detail"])
    assert (f"pick one: a starter pack, £9 for 300 checks ({STARTER}); "
            f"or a pack, £39 for 2,000 checks ({PACK}).") in message["detail"]


def test_other_holds_ignore_stored_options():
    message = hold_message({"kind": "keyless_limit", "waiting": 1,
                            "options": options(answer())})

    assert "pick one" not in message["detail"]
    assert message["url_label"] == "Get a free key"


@pytest.mark.asyncio
async def test_a_read_hold_keeps_its_options(state):  # noqa: F811
    await two_cold_and_a_reply(state)
    sender = with_check(make_sender(state, FakeProvider()),
                        mock_status(402, answer(growth=GROWTH_OFFER)), api_key=KEY)
    await sender._drain_due()

    detail = hold_message(await current_hold(state))["detail"]

    assert f"the Growth plan, £49 a month for 500 checks a day ({GROWTH})" in detail
    assert detail.startswith("2 cold emails are waiting.")


def test_today_lists_the_options(client, monkeypatch):  # noqa: F811
    check_on(monkeypatch)
    _seed(client.db_path, json.dumps(held(options(answer()))))

    items = client.get("/api/today").json()["items"]

    item = next(i for i in items if i["key"] == "precleared-hold")
    assert f"the monthly plan, £15 a month for 100 checks a day ({MONTHLY})" in item["detail"]
    assert item["url"] == PACK


def test_cli_status_lists_the_options(monkeypatch, tmp_path, capsys):
    check_on(monkeypatch)
    import sellbird.cli as cli
    import sellbird.state as state_mod
    from sellbird.state import StateManager

    db = tmp_path / "status.db"
    _seed(db, json.dumps(held(options(answer()))))
    monkeypatch.setattr(state_mod, "StateManager", lambda: StateManager(str(db)))

    cli.cmd_status(type("A", (), {"check": False})())

    out = " ".join(capsys.readouterr().out.split())
    assert "HOLDING COLD EMAIL" in out
    assert "a starter pack, £9 for 300 checks" in out
    assert "the monthly plan, £15 a month for 100 checks a day" in out


def test_the_fallback_buy_page_is_untouched():
    assert hold_message({"kind": "out_of_checks", "waiting": 1})["url"] == BUY_URL
