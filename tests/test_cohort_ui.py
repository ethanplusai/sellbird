"""The Signals tab's company filter (the cohort builder), run under node.

One control per signal (Any, Must have, Must not have), the filter said back
in words, and a clear next step when only exclusions are picked, since the
server never treats "nothing required" as "every company".
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "sellbird" / "web" / "app.js"

FUNCS = ("escHtml", "jsq", "renderCohortBuilder", "setCohortChoice", "clearCohort",
         "_cohortSummary", "_cohortRanges", "runCohort", "toggleCohort")

SIGNALS = {"groups": [{"signals": [
    {"code": "RUNNING_GOOGLE_ADS", "label": "Runs Google Ads", "status": "confirmed",
     "companies": 12},
    {"code": "INCUMBENT_AGENCY", "label": "Has an agency", "status": "confirmed",
     "companies": 1},
    {"code": "SERP_RANK", "label": "Organic rank", "status": "confirmed",
     "companies": 4, "value_type": "num"},
    {"code": "BLOCKS_AI_CRAWLERS", "label": "Blocks AI crawlers", "status": "proposed",
     "companies": 3},
]}]}


def _run(steps: str, cohort_response=None) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    parts = [re.search(r"^let _cohort = .*?^const COHORT_PAGE = \d+;\n", source,
                       re.S | re.M).group(0)]
    for name in FUNCS:
        match = re.search(rf"^(async )?function {name}\(.*?^}}\n", source, re.S | re.M)
        assert match, name
        parts.append(match.group(0))
    script = (
        "const els = {'cohort-builder': {innerHTML: ''}, 'cohort-result': {innerHTML: ''}};\n"
        "const document = {getElementById: id => els[id]};\n"
        f"let _signals = {json.dumps(SIGNALS)};\n"
        "const calls = [];\n"
        f"async function api(path, opts) {{ calls.push(JSON.parse(opts.body)); "
        f"return {json.dumps(cohort_response or {'size': 0, 'offset': 0, 'companies': []})}; }}\n"
        + "\n".join(parts)
        + f"\n(async () => {{ {steps}\n await new Promise(r => setTimeout(r, 0));\n"
        "process.stdout.write(JSON.stringify({builder: els['cohort-builder'].innerHTML, "
        "result: els['cohort-result'].innerHTML, calls})); })();"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         check=True, timeout=30).stdout
    return json.loads(out)


def test_each_confirmed_signal_gets_one_three_way_control():
    out = _run("renderCohortBuilder();")
    builder = out["builder"]
    assert builder.count("class=\"cohort-row\"") + builder.count("class=\"cohort-row is-") == 3  # proposed signals stay out
    assert builder.count(">Must have</button>") == 3
    assert builder.count(">Must not have</button>") == 3
    assert "12 companies have it" in builder and "1 company has it" in builder
    assert "Only between" in builder  # the numeric signal takes a range
    assert "Blocks AI crawlers" not in builder


def test_must_have_then_must_not_have_moves_the_signal():
    out = _run("setCohortChoice('RUNNING_GOOGLE_ADS', 'require');"
               "setCohortChoice('RUNNING_GOOGLE_ADS', 'exclude');")
    assert "is-exclude" in out["builder"]
    # Only an exclusion is left, so no query runs and the next step is said.
    assert "Must not have</b> only drops companies" in out["result"]


def test_matches_show_the_filter_in_words_and_a_clear_button():
    response = {"size": 2, "offset": 0, "companies": [
        {"name": "Acme", "domain": "acme.example", "industry": "", "location": ""},
        {"name": "Beta", "domain": "beta.example", "industry": "", "location": ""}]}
    out = _run("setCohortChoice('RUNNING_GOOGLE_ADS', 'require');"
               "setCohortChoice('INCUMBENT_AGENCY', 'exclude');", response)
    assert out["calls"][-1]["require"] == ["RUNNING_GOOGLE_ADS"]
    assert out["calls"][-1]["exclude"] == ["INCUMBENT_AGENCY"]
    result = out["result"]
    assert "companies match</div>" in result.replace(" right now", "")
    assert "Has Runs Google Ads. Doesn't have Has an agency." in result
    assert "Clear filters" in result and "Export CSV" in result


def test_clear_resets_every_choice():
    out = _run("setCohortChoice('RUNNING_GOOGLE_ADS', 'require'); clearCohort();")
    assert "is-require" not in out["builder"]
    assert "Set a signal to <b>Must have</b>" in out["result"]


def test_old_toggle_still_works():
    out = _run("toggleCohort('require', 'RUNNING_GOOGLE_ADS', true);")
    assert "is-require" in out["builder"]
