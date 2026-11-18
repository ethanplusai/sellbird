"""Outbox filters: narrow the review desk by email in the series, the
company's website platform or tools, and whether it runs ads.

The server hands each draft its signals by code; the filtering itself runs in
app.js, which these tests run under node with the real helpers.
"""

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_outbox_review import _seed, client  # noqa: F401  (fixture)

APP_JS = Path(__file__).resolve().parent.parent / "sellbird" / "web" / "app.js"


def test_drafts_carry_signal_values_by_code(client):
    asyncio.run(_seed(client.db_path))
    ctx = client.get("/api/outbox").json()["pending"][0]["context"]
    assert ctx["signal_values"]["TECH_STACK"] == ["Lovable, Stripe"]
    assert ctx["signal_values"]["RUNNING_GOOGLE_ADS"] == ["yes"]


def test_meta_ads_shows_next_to_the_draft(client):
    import sellbird.dashboard as dash
    state, _ = asyncio.run(_seed(client.db_path))

    async def more():
        cid = (await dash.query_db("SELECT id FROM companies"))[0]["id"]
        await state.add_observations([
            {"company_id": cid, "signal_code": "RUNNING_META_ADS", "value_num": 0},
        ])
    asyncio.run(more())
    ctx = client.get("/api/outbox").json()["pending"][0]["context"]
    assert ctx["signal_values"]["RUNNING_META_ADS"] == ["no"]
    assert ctx["signals"]["Running Meta/Facebook Ads"] == "no"


def _run(expr: str) -> object:
    """Evaluate `expr` against app.js's Outbox filter code under node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    parts = []
    for pattern in (r"^function escHtml\(.*?^}\n", r"^function jsq\(.*?^}\n",
                    r"^// ── Outbox filters.*?(?=^function renderDesk\()"):
        match = re.search(pattern, source, re.S | re.M)
        assert match, pattern
        parts.append(match.group(0))
    script = ("const icon = n => '';\nconst emptyState = (g, t, c) => t + ' ' + c;\n" + "\n".join(parts) +
              f"\nprocess.stdout.write(JSON.stringify({expr}));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8",
                         check=True, timeout=30).stdout
    return json.loads(out)


def _draft(id, step=1, kind="sequence", tools=None, google=None, meta=None):
    values = {}
    if tools is not None:
        values["TECH_STACK"] = [tools]
    if google is not None:
        values["RUNNING_GOOGLE_ADS"] = ["yes" if google else "no"]
    if meta is not None:
        values["RUNNING_META_ADS"] = ["yes" if meta else "no"]
    return {"id": id, "step": step, "kind": kind, "subject": "s", "context": {"signal_values": values}}


QUEUE = [
    _draft("a", 2, tools="WordPress, Google Analytics", google=True, meta=False),
    _draft("b", 1, tools="Shopify", google=False, meta=False),
    _draft("c", 3, tools="WordPress", google=False, meta=True),
    _draft("d", 1),                          # signals never collected
    _draft("e", 1, kind="reply", tools="Shopify, Google Analytics", google=True),
]


def _ids(f):
    filters = {"step": "all", "tool": "", "ads": "", "sort": "send", **f}
    return _run(f"outboxView({json.dumps(QUEUE)}, {json.dumps(filters)}).map(x => x.id)")


def test_no_filter_keeps_the_send_order():
    assert _ids({}) == ["a", "b", "c", "d", "e"]


def test_filter_by_email_in_the_series():
    assert _ids({"step": "1"}) == ["b", "d"]
    assert _ids({"step": "3"}) == ["c"]
    assert _ids({"step": "reply"}) == ["e"]


def test_sort_by_email_in_the_series_keeps_send_order_within_a_step():
    assert _ids({"sort": "step"}) == ["b", "d", "a", "c", "e"]


def test_filter_by_one_tool_out_of_the_stack():
    assert _ids({"tool": "Google Analytics"}) == ["a", "e"]
    assert _ids({"tool": "WordPress"}) == ["a", "c"]
    assert _ids({"tool": "Shopify", "step": "1"}) == ["b"]


def test_filter_by_ads():
    assert _ids({"ads": "any"}) == ["a", "c", "e"]
    assert _ids({"ads": "google"}) == ["a", "e"]
    assert _ids({"ads": "meta"}) == ["c"]
    # Never checked is not the same as no ads.
    assert _ids({"ads": "none"}) == ["b"]


def _bar(f, queue=QUEUE):
    filters = {"step": "all", "tool": "", "ads": "", "sort": "send", **f}
    return _run(f"renderOutboxFilters({json.dumps(queue)}, {json.dumps(filters)})")


def test_the_bar_counts_what_each_choice_would_show():
    html = _bar({"tool": "WordPress"})
    assert re.search(r'Email 1 <span class="n">0</span>', html)
    assert re.search(r'Email 2 <span class="n">1</span>', html)
    assert ">Shopify (2)</option>" in html
    assert ">WordPress (2)</option>" in html
    assert "2 of 5 shown" in html
    assert "Clear filters" in html


def test_the_bar_escapes_tool_names_read_off_websites():
    queue = [_draft("x", tools='<img src=x onerror=alert(1)>'), _draft("y", 2)]
    html = _bar({}, queue)
    assert "<img" not in html
    assert "&lt;img" in html


def test_no_bar_for_a_single_draft_and_a_hint_when_signals_are_missing():
    assert _bar({}, [_draft("x")]) == ""
    html = _bar({}, [_draft("x"), _draft("y", 2)])
    assert "those signals" in html
    assert "Any platform or tool" not in html


def test_a_filter_with_nothing_left_still_shows_so_it_can_be_cleared():
    html = _bar({"step": "3"}, [_draft("x"), _draft("y", 1)])
    assert "Email 3" in html
    assert "0 of 2 shown" in html
