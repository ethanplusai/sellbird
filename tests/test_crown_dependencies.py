"""Crown Dependencies care registers (Jersey today).

Every service, provider and person here is invented. The pages are written
by hand in the shape the Jersey Care Commission publishes, and the PDF is
built in the test, not copied.
"""

import json
import os
import time
import zlib

import httpx
import pytest

from harvey.collectors import cqc
from harvey.collectors import crown_dependencies as CD


@pytest.fixture(autouse=True)
def no_crawl_delay(monkeypatch):
    monkeypatch.setattr(CD, "CRAWL_DELAY", 0)
    CD._last_request.clear()


def _holder(slug, name, kind, street="1 Example Road", parish="St Helier",
            postcode="JE0 0AA", phone="01534 000001", provider="Example Care Ltd",
            report=""):
    link = f"https://carecommission.je/care_service_search/{slug}/"
    latest = (f'<a href="{report}" target="_blank" class="latest_report">Latest Report</a>'
              if report else "")
    return (
        f'<div class="post-holder post-no-media post-1 care_service_search type-care_service_search">'
        f'<div class="post-info"><div class="post-content post-excerpt"><div class="row">'
        f'<div class="col-lg-4"><h4 class="post-title"><a href="{link}">{name}</a></h4><br>'
        f'<div class="care_home">{kind}</div></div>'
        f'<div class="col-lg-3"><div class="addr_cols">{street}</div>{parish}<br>{postcode}<br></div>'
        f'<div class="col-lg-2"><a href="tel:{phone}">{phone}</a><br><b>Provider: </b><br>'
        f'<span style="float: left;">{provider}</span></div>'
        f'<div class="col-lg-3"><a href="{link}" class="view_details_butt">View Full Details</a>'
        f'<br>{latest}</div></div></div></div></div>')


def _listing(holders, found=None):
    return (f'<html><body><p>We found <b>{found if found is not None else len(holders)}</b> '
            f'results for you</p><form></form><div id="page-content">'
            + "".join(holders) + '</div><div id="sidebar"></div></body></html>')


HOMES = [
    _holder("gorse-lane", "Gorse Lane Care Home", "Care Home",
            street="Gorse Lane<br /> <br /> Example Hill", parish="St Saviour"),
    _holder("harbour-view", "Harbour View Nursing Home", "Care Home",
            provider="Harbour View Care Limited", parish="St Brelade"),
    _holder("bramble-cottage", "Bramble Cottage", "Care Home",
            provider="A. Example", parish="Trinity", phone=""),
]
HOME_CARE = [
    _holder("example-home-care", "Example Home Care", "Home Care", parish="St Helier",
            provider="Example Care Ltd"),
]
OTHER = [
    _holder("example-childrens-home", "Example Children's Home", "Children's Home Service"),
    _holder("example-day-centre", "Example Day Centre", "Adult Day Care"),
]


def jersey_handler(calls=None, fail=False):
    def handler(request):
        if calls is not None:
            calls.append(str(request.url))
        if fail:
            return httpx.Response(503)
        q = request.url.params
        kind, page = q.get("_sfm_service_type"), q.get("sf_paged", "1")
        if kind == "Care Home" and q.get("_sfm_type_of_care") == "Nursing":
            return httpx.Response(200, text=_listing([HOMES[1]]))
        if kind == "Care Home":
            # 30 results: the first page holds 25 (two here), the second the rest.
            body = HOMES[:2] + OTHER if page == "1" else HOMES[2:]
            return httpx.Response(200, text=_listing(body, found=30))
        if kind == "Home Care":
            return httpx.Response(200, text=_listing(HOME_CARE))
        return httpx.Response(404)
    return handler


async def _load(tmp_path, **kw):
    async with httpx.AsyncClient(transport=httpx.MockTransport(jersey_handler(**kw))) as c:
        return await CD.load_register(c, tmp_path)


# ── the listing ──

def test_listing_keeps_adult_care_homes_and_home_care_only():
    locs = CD.parse_jersey_listing(_listing(HOMES + HOME_CARE + OTHER))
    assert [l.id for l in locs] == ["je-gorse-lane", "je-harbour-view",
                                    "je-bramble-cottage", "je-example-home-care"]


def test_listing_fields():
    gorse = CD.parse_jersey_listing(_listing(HOMES))[0]
    assert isinstance(gorse, cqc.Location)
    assert gorse.name == "Gorse Lane Care Home"
    assert gorse.address == "Gorse Lane, Example Hill"
    assert (gorse.city, gorse.postcode, gorse.local_authority) == ("St Saviour", "JE0 0AA", "Jersey")
    assert gorse.phone == "01534 000001"
    assert gorse.services == ["care home"]
    assert gorse.provider_name == "Example Care Ltd"
    assert gorse.ownership == "Organisation"
    assert gorse.rating == "" and gorse.website == ""
    assert gorse.lat == pytest.approx(49.197) and gorse.lng == pytest.approx(-2.085)


def test_ownership_left_blank_when_not_shown():
    bramble = CD.parse_jersey_listing(_listing(HOMES))[2]
    assert bramble.ownership == ""
    assert bramble.phone == ""
    assert CD.ownership_from_name("Example Homes Limited") == "Organisation"


def test_unknown_parish_has_no_point():
    loc = CD.parse_jersey_listing(_listing([_holder("x", "X", "Home Care", parish="Elsewhere")]))[0]
    assert loc.lat is None and loc.lng is None


# ── the register ──

@pytest.mark.asyncio
async def test_load_register_reads_every_page_and_marks_nursing(tmp_path):
    locs = await _load(tmp_path)
    by_id = {l.id: l for l in locs}
    # Bramble Cottage's provider could be a sole trader, so it's left out.
    assert set(by_id) == {"je-gorse-lane", "je-harbour-view", "je-example-home-care"}
    assert by_id["je-harbour-view"].services == ["nursing home"]
    assert by_id["je-gorse-lane"].services == ["care home"]
    assert by_id["je-example-home-care"].services == ["home care"]
    # Gorse Lane and the home care service share a provider.
    assert by_id["je-gorse-lane"].provider_locations == 2
    assert (tmp_path / CD.CACHE_FILE).exists()


@pytest.mark.asyncio
async def test_blank_ownership_left_out_but_kept_in_the_copy(tmp_path):
    locs = await _load(tmp_path)
    assert all(l.ownership == "Organisation" for l in locs)
    copy = CD.load_cache(tmp_path / CD.CACHE_FILE)
    assert "je-bramble-cottage" in {l.id for l in copy}
    assert "je-bramble-cottage" not in {l.id for l in locs}
    # From the copy too, not just a fresh read.
    assert {l.id for l in await _load(tmp_path)} == {l.id for l in locs}


@pytest.mark.asyncio
async def test_fresh_copy_is_read_without_the_network(tmp_path):
    first = await _load(tmp_path)
    calls = []
    again = await _load(tmp_path, calls=calls)
    assert again == first and calls == []


@pytest.mark.asyncio
async def test_old_copy_is_used_when_the_commission_is_down(tmp_path):
    first = await _load(tmp_path)
    old = time.time() - (CD.MAX_AGE_DAYS + 1) * 86400
    os.utime(tmp_path / CD.CACHE_FILE, (old, old))
    assert await _load(tmp_path, fail=True) == first


@pytest.mark.asyncio
async def test_no_copy_and_no_commission_raises(tmp_path):
    with pytest.raises(RuntimeError, match="Jersey Care Commission"):
        await _load(tmp_path, fail=True)


@pytest.mark.asyncio
async def test_search_by_island_parish_and_radius(tmp_path):
    locs = await _load(tmp_path)
    assert {l.id for l in CD.search(locs, "care home", "Jersey")} == {
        "je-gorse-lane", "je-harbour-view"}
    assert [l.id for l in CD.search(locs, "home care", "St Helier, Jersey")] == [
        "je-example-home-care"]
    assert [l.id for l in CD.search(locs, "nursing home", "Jersey")] == ["je-harbour-view"]
    near = CD.search(locs, "care home", coordinate="49.197,-2.085,2")
    assert [l.id for l in near] == ["je-gorse-lane"]


def test_service_url():
    assert CD.service_url("je-gorse-lane") == \
        "https://carecommission.je/care_service_search/gorse-lane/"
    assert CD.service_url("im-anything") == ""
    assert CD.service_url("gg-anything") == ""
    assert CD.service_url("") == ""


# ── reports ──

PDF_SUMMARY = "https://carecommission.je/wp-content/uploads/2026/05/RPT_GL_Summary_20260501.pdf"
PDF_FULL = "https://carecommission.je/wp-content/uploads/2026/05/RPT_GL_Inspection_20260501.pdf"
PDF_OLD = "https://carecommission.je/wp-content/uploads/2025/04/RPT_GL_Summary_20250401.pdf"


def _row(date, kind, url):
    return (f'<tr> <td width="30%">{date}</td> <td width="30%">{kind}</td> '
            f'<!--<td width="30%"></td>--> <td width="40%"> <a href="{url}" target="_blank">'
            f'<img src="x.png"/></a> </td> </tr>')


SERVICE_PAGE = (
    '<ul class="servicesec_left"><li><a href="#" scroll_id="insp_report">INSPECTION REPORTS</a></li>'
    '<li><a href="#" scroll_id="provider_details">PROVIDER DETAILS</a></li></ul>'
    '<div class="service_section" id="service_details"><table>'
    "<tr> <td><b>Manager's Name:</b></td> <td align=\"right\">Jo Example </td> </tr></table></div>"
    '<div class="service_section" id="insp_report"><table><tr><th>Inspection Date</th></tr>'
    + _row("12 April, 2025", "Summary", PDF_OLD)
    + _row("1 May, 2026", "Full Report", PDF_FULL)
    + _row("1 May, 2026", "Summary", PDF_SUMMARY)
    + '</table></div><div class="service_section" id="provider_details"></div>')


def test_reports_newest_summary_first():
    rows = CD.jersey_reports(SERVICE_PAGE)
    assert [r["date"] for r in rows][:2] == ["1 May 2026", "1 May 2026"]
    assert CD.pick_report(rows) == {"date": "1 May 2026", "kind": "Summary", "url": PDF_SUMMARY}
    assert CD.pick_report([]) == {}


def _hex16(text):
    return "<" + "".join(f"{0x10 + i:04X}" for i in range(len(text))) + ">"


def make_pdf(lines, objstm=False):
    """A small Word-shaped PDF: one Identity-H font with a ToUnicode map
    (glyph ids that mean nothing without it) and one WinAnsi font.

    ``lines`` are (text, y); each line is written as two runs on the same
    line, the first in the CID font as a TJ array with kerning.
    """
    chars = sorted({c for text, _ in lines for c in text})
    gid = {c: 0x10 + i for i, c in enumerate(chars)}
    cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
            f"{len(chars)} beginbfchar "
            + " ".join(f"<{gid[c]:04X}> <{ord(c):04X}>" for c in chars)
            + " endbfchar endcmap end end").encode()
    ops = []
    for text, y in lines:
        half = len(text) // 2
        head, tail = text[:half], text[half:]
        hexs = "".join(f"{gid[c]:04X}" for c in head)
        ops.append(f"BT /F1 11 Tf 1 0 0 1 72 {y} Tm [<{hexs[:4]}>-7<{hexs[4:]}>] TJ ET")
        tail_escaped = tail.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"BT /F2 11 Tf 1 0 0 1 200 {y} Tm ({tail_escaped}) Tj ET")
    content = zlib.compress("\n".join(ops).encode("cp1252"))
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R /F2 7 0 R >> >> "
           b"/Contents 4 0 R >>",
        4: b"<< /Filter /FlateDecode /Length %d >>\nstream\n" % len(content)
           + content + b"\nendstream",
        5: b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+Example /Encoding /Identity-H "
           b"/ToUnicode 6 0 R >>",
        6: b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"\nendstream",
        7: b"<< /Type /Font /Subtype /TrueType /BaseFont /Example /Encoding /WinAnsiEncoding >>",
    }
    out = b"%PDF-1.7\n"
    if objstm:
        # The page and fonts live inside a compressed object stream.
        inner = [3, 5, 7]
        bodies, header, off = b"", [], 0
        for n in inner:
            header.append(f"{n} {off}")
            bodies += objects[n] + b" "
            off = len(bodies)
        head = " ".join(header).encode() + b" "
        packed = zlib.compress(head + bodies)
        for n in inner:
            del objects[n]
        objects[8] = (b"<< /Type /ObjStm /N 3 /First %d /Filter /FlateDecode /Length %d >>\n"
                      % (len(head), len(packed)) + b"stream\n" + packed + b"\nendstream")
    for num, body in objects.items():
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    return out + b"%%EOF\n"


REPORT_LINES = [
    ("Summary Report", 780),
    ("The following is a summary of what we found during this inspection.", 700),
    ("Care is kind and well led (overall).", 680),
    ("Medicines records were not always complete.", 660),
    ("SUMMARY OF INSPECTION FINDINGS", 640),
    ("Area for Improvement 1", 620),
    ("Ref: Standard 6.4 To be completed: by 01/09/2026", 600),
    ("The registered provider must ensure staff complete medication training.", 580),
    ("Response by the Registered Provider:", 560),
    ("We have booked the training.", 540),
    ("Area for Improvement 2", 520),
    ("To be completed: with immediate effect", 500),
    ("Falls must be reviewed after each incident.", 480),
    ("Response by the Registered Provider:", 460),
    ("Done.", 440),
]


@pytest.mark.parametrize("objstm", [False, True])
def test_pdf_text_reads_word_shaped_pdfs(objstm):
    text = CD.pdf_text(make_pdf(REPORT_LINES, objstm=objstm))
    assert text.startswith("Summary Report The following is a summary")
    assert "Care is kind and well led (overall)." in text
    assert "Medicines records were not always complete." in text


def test_pdf_text_of_junk_is_empty():
    assert CD.pdf_text(b"not a pdf") == ""
    assert CD.pdf_text(b"") == ""


def test_summary_keeps_findings_and_areas_not_the_providers_reply():
    summary = CD.report_summary_from(CD.pdf_text(make_pdf(REPORT_LINES)))
    assert summary.startswith("Care is kind and well led (overall).")
    assert "Medicines records were not always complete." in summary
    assert ("Area for improvement 1: The registered provider must ensure staff "
            "complete medication training.") in summary
    assert "Area for improvement 2: Falls must be reviewed after each incident." in summary
    assert "booked the training" not in summary
    assert "SUMMARY OF INSPECTION FINDINGS" not in summary


def test_summary_is_trimmed_at_a_sentence():
    text = "The following is a summary of what we found during this inspection. " + \
           "Staff were kind. " * 400
    summary = CD.report_summary_from(text, limit=100)
    assert len(summary) <= 100 and summary.endswith(".")


def report_handler(calls, pdf=None, fail=False):
    pdf = pdf if pdf is not None else make_pdf(REPORT_LINES)

    def handler(request):
        calls.append(str(request.url))
        if fail:
            return httpx.Response(500)
        url = str(request.url)
        if url == CD.service_url("je-gorse-lane"):
            return httpx.Response(200, text=SERVICE_PAGE)
        if url == PDF_SUMMARY:
            return httpx.Response(200, content=pdf)
        return httpx.Response(404)
    return handler


async def _report(tmp_path, service_id="je-gorse-lane", **kw):
    calls = kw.pop("calls", [])
    async with httpx.AsyncClient(transport=httpx.MockTransport(report_handler(calls, **kw))) as c:
        return await CD.latest_report(c, service_id, tmp_path)


@pytest.mark.asyncio
async def test_latest_report(tmp_path):
    report = await _report(tmp_path)
    assert set(report) == {"ratings", "summary", "url", "published", "manager"}
    assert report["ratings"] == {}
    assert report["url"] == PDF_SUMMARY
    assert report["published"] == "1 May 2026"
    assert report["manager"] == "Jo Example"
    assert "Falls must be reviewed" in report["summary"]
    cached = json.loads((tmp_path / CD.REPORTS_FILE).read_text(encoding="utf-8"))
    assert cached["je-gorse-lane"]["report"] == report


@pytest.mark.asyncio
async def test_latest_report_is_cached(tmp_path):
    first = await _report(tmp_path)
    calls = []
    assert await _report(tmp_path, calls=calls) == first
    assert calls == []


@pytest.mark.asyncio
async def test_latest_report_failures_are_empty(tmp_path):
    assert await _report(tmp_path, fail=True) == {}
    assert not (tmp_path / CD.REPORTS_FILE).exists()       # not cached: try again later
    calls = []
    assert await _report(tmp_path, service_id="im-anything", calls=calls) == {}
    assert await _report(tmp_path, service_id="gg-anything", calls=calls) == {}
    assert calls == []


@pytest.mark.asyncio
async def test_service_with_no_report_yet(tmp_path):
    def handler(request):
        return httpx.Response(200, text='<div id="insp_report"></div>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await CD.latest_report(c, "je-new-service", tmp_path) == {}


@pytest.mark.asyncio
async def test_crawl_delay_is_kept(monkeypatch):
    monkeypatch.setattr(CD, "CRAWL_DELAY", 0.2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="ok"))) as c:
        start = time.monotonic()
        await CD._get(c, "https://carecommission.je/a")
        await CD._get(c, "https://carecommission.je/b")
        assert time.monotonic() - start >= 0.19
