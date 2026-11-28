"""Leaving one person's page. The way back ("People") stays on the page while
it refreshes, and pressing People in the dock from that page goes to the list,
not to the same person again. Runs the real dashboard in Chromium against a
throwaway database; skipped where Playwright has no browser."""

import asyncio
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import sellbird.dashboard as dash
from sellbird.models.prospect import Prospect
from sellbird.state import StateManager


@pytest.fixture(scope="module")
def browser():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("Playwright has no Chromium here")
        yield b
        b.close()


@pytest.fixture
def server(monkeypatch):
    """The dashboard on a free loopback port, over a throwaway database."""
    uvicorn = pytest.importorskip("uvicorn")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "sellbird.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "sellbird" / "web")
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        srv = uvicorn.Server(uvicorn.Config(dash.app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=srv.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not srv.started and time.time() < deadline:
            time.sleep(0.05)
        yield {"base": f"http://127.0.0.1:{port}", "token": token, "db": db}
        srv.should_exit = True
        thread.join(5)


def _person(db) -> str:
    async def add():
        state = StateManager(str(db))
        await state.init_db()
        return await state.add_prospect(Prospect(first_name="Ana", last_name="Example",
                                                 company="Example Installs", email="ana@installs.example"))
    # Playwright's sync API keeps an event loop running on this thread.
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, add()).result()


def _open(browser, server, fragment):
    page = browser.new_page()
    page.goto(server["base"] + "/#token=" + server["token"])
    page.wait_for_function("typeof showTab === 'function'")
    page.evaluate("f => { location.hash = f }", fragment)
    return page


def test_the_way_back_survives_the_person_page_refreshing(browser, server):
    pid = _person(server["db"])
    page = _open(browser, server, "#prospects")
    page.locator("a.rowmain").first.click()
    page.wait_for_url("**#person/" + pid)
    back = page.locator("#person #screen-nav button.back")
    back.wait_for(state="visible")
    # The page redraws itself in the background; the way back must not go with it.
    for _ in range(3):
        page.evaluate("loadPerson()")
        page.wait_for_timeout(150)
    assert back.is_visible()
    back.click()
    page.wait_for_url("**#prospects")
    page.close()


def test_people_in_the_dock_leaves_a_person_for_the_list(browser, server):
    _person(server["db"])
    page = _open(browser, server, "#prospects")
    page.locator("a.rowmain").first.click()
    page.wait_for_url("**#person/*")
    page.locator('.navitem[data-place="people"]').click()
    page.wait_for_url("**#prospects")
    assert page.locator("#prospects").is_visible()
    # From the list, the dock still keeps the view you were on (Companies).
    page.locator("#screen-nav .pill", has_text="Companies").click()
    page.wait_for_url("**#companies")
    page.locator('.navitem[data-place="today"]').click()
    page.locator('.navitem[data-place="people"]').click()
    page.wait_for_url("**#companies")
    page.close()
