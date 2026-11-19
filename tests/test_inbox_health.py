"""Check connection: one check per inbox (sign-in, the domain's DNS, the
warmup), one state from it, and editing an inbox in place."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from sellbird import inbox_health, inbox_routes
from sellbird.inbox_health import FAIL, PASS, UNKNOWN, WARN, run_check, warmup_line

from tests.test_inbox_routes import SMTP, _client, env, keychain  # noqa: F401  (fixtures)
from tests.test_mailboxes import boxes  # noqa: F401  (fixture)


def _dns(status=PASS, detail="ok"):
    return lambda domain, selector: [
        inbox_health._line(k, k.upper(), status if k == "spf" else PASS, detail, "fix it")
        for k in ("mx", "spf", "dkim", "dmarc")]


async def _ok():
    return True, "Connected as two@side.example"


async def _refused():
    return False, "SMTP login failed: (535, 'Username and Password not accepted')"


def _run(sign_in=_ok, started="", min_days=14):
    return asyncio.run(run_check(sign_in, address="two@side.example", connection="smtp",
                                 provider_name="smtp", warmup_started=started,
                                 min_days=min_days))


def test_a_clean_check_is_ready(monkeypatch):
    monkeypatch.setattr(inbox_health, "_dns_lines", _dns())
    result = _run()
    assert result["verdict"] == "ready" and result["summary"] == "Passed."
    assert [c["key"] for c in result["checks"]] == ["signin", "mx", "spf", "dkim", "dmarc", "warmup"]
    assert all(c["fix"] == "" for c in result["checks"])


def test_a_refused_sign_in_needs_attention_and_says_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(inbox_health, "_dns_lines", _dns())
    result = _run(_refused)
    signin = result["checks"][0]
    assert result["verdict"] == "needs_attention"
    assert signin["status"] == FAIL and "535" in signin["detail"]
    assert "app password" in signin["fix"]
    assert result["summary"] == "Failed: Signs in."


def test_missing_dns_needs_attention_and_unknown_dns_isnt_a_verdict(monkeypatch):
    monkeypatch.setattr(inbox_health, "_dns_lines", _dns(FAIL, "No SPF record."))
    assert _run()["verdict"] == "needs_attention"
    monkeypatch.setattr(inbox_health, "_dns_lines", _dns(UNKNOWN, "Couldn't look this up."))
    result = _run()
    assert result["verdict"] == "not_checked" and result["summary"].startswith("Couldn't finish")


def test_a_sign_in_that_raises_is_a_failed_line_not_an_error(monkeypatch):
    monkeypatch.setattr(inbox_health, "_dns_lines", _dns())

    async def boom():
        raise OSError("no route")
    assert _run(boom)["checks"][0]["status"] == FAIL


def test_warmup_days_make_it_warming(monkeypatch):
    monkeypatch.setattr(inbox_health, "_dns_lines", _dns())
    started = (datetime.now(UTC).date() - timedelta(days=3)).isoformat()
    result = _run(started=started)
    assert result["verdict"] == "warming" and result["summary"].startswith("Day 3 of 14")
    line = warmup_line("2026-11-01", 14, today=date(2026, 11, 5))
    assert line["status"] == WARN and "Nov 15" in line["detail"]
    assert warmup_line("2026-10-01", 14, today=date(2026, 11, 5))["status"] == PASS


def test_the_real_dns_checks_are_read_into_lines(monkeypatch):
    from sellbird import deliverability as d
    monkeypatch.setattr(d, "check_mx", lambda domain: d.Check("MX", d.OK, "mx.side.example"))
    monkeypatch.setattr(d, "check_spf", lambda domain: d.Check("SPF", d.FAIL, "No SPF record."))
    monkeypatch.setattr(d, "check_dkim", lambda domain, sel: d.Check("DKIM", d.WARN, "No key"))
    monkeypatch.setattr(d, "check_dmarc", lambda domain: d.Check(
        "DMARC", d.INFO, "Couldn't look this up", unknown=True))
    lines = {line["key"]: line for line in inbox_health._dns_lines("side.example", "")}
    assert [lines[k]["status"] for k in ("mx", "spf", "dkim", "dmarc")] == [PASS, FAIL, WARN, UNKNOWN]
    assert "v=spf1" in lines["spf"]["fix"] and lines["mx"]["fix"] == ""


# ── Through the routes ──

def _add(store, address, provider, connection, settings=None):
    from sellbird import mailboxes

    async def go():
        mid = mailboxes._new_id()
        await store.add_mailbox(mailbox_id=mid, address=address, provider=provider,
                                connection=connection, settings=settings)
        return mid
    return asyncio.run(go())


def _smtp_inbox(env, password="old-secret"):
    mid = _add(env["store"], "four@side.example", "smtp", "smtp",
               {"host": "smtp.side.example", "username": "four@side.example", "port": 587})
    inbox_routes._secrets().set(f"mailbox:{mid}:smtp", password)
    return mid

def test_check_connection_keeps_its_result_and_the_list_shows_one_state(env, keychain, monkeypatch):
    with _client(env["user"]) as c:
        r = c.post(f"/api/inboxes/{env['mid']}/test").json()
        assert r["ok"] and r["health"]["verdict"] == "ready"
        side = next(i for i in c.get("/api/inboxes").json()["inboxes"] if i["id"] == env["mid"])
        assert side["state"] == "ready" and side["health"]["checks"][0]["key"] == "signin"
        c.post(f"/api/inboxes/{env['mid']}/pause", json={})
        side = next(i for i in c.get("/api/inboxes").json()["inboxes"] if i["id"] == env["mid"])
    assert side["state"] == "paused" and side["health"]["verdict"] == "ready"


def test_the_main_mailbox_is_checked_too(env, keychain, monkeypatch):
    async def main_ok(config, env_):
        return True, "Connected"
    monkeypatch.setattr(inbox_routes, "_test_main", main_ok)
    with _client(env["user"]) as c:
        r = c.post("/api/inboxes/main/test").json()
        main = next(i for i in c.get("/api/inboxes").json()["inboxes"] if i["main"])
    assert r["ok"] and main["health"]["verdict"] == "ready"


def test_removing_an_inbox_forgets_its_check(env, keychain):
    with _client(env["user"]) as c:
        c.post(f"/api/inboxes/{env['mid']}/test")
        c.post(f"/api/inboxes/{env['mid']}/remove", json={"confirm": "two@side.co"})
    assert asyncio.run(env["store"].get_setting(f"inbox_health:{env['mid']}")) == ""


def test_adding_an_smtp_inbox_signs_in_before_saving(env, keychain, monkeypatch):
    async def refuse(settings, address, name, config, env_):
        return False, "SMTP login failed: (535, 'bad password')"
    monkeypatch.setattr(inbox_routes, "_try_smtp", refuse)
    with _client(env["user"]) as c:
        r = c.post("/api/inboxes/connect/smtp", json=SMTP)
        assert r.status_code == 400 and r.json()["sign_in_failed"]
        assert "nothing was saved" in r.json()["message"] and "535" in r.json()["message"]
        assert asyncio.run(env["store"].get_mailbox_by_address("three@side.example")) is None
        saved = c.post("/api/inboxes/connect/smtp", json={**SMTP, "save_anyway": True}).json()
    assert saved["success"] and saved["id"] and saved["health"]["checks"]


def test_settings_never_carry_the_password(env, keychain):
    mid = _smtp_inbox(env)
    with _client(env["user"]) as c:
        data = c.get(f"/api/inboxes/{mid}/settings").json()
    assert "old-secret" not in json.dumps(data)
    assert data["has_password"] is True and data["smtp"]["host"] == "smtp.side.example"
    assert data["smtp"]["port"] == 587 and data["address"] == "four@side.example"


def test_editing_checks_the_new_sign_in_first_and_keeps_the_inbox(env, keychain, monkeypatch):
    mid = _smtp_inbox(env)
    secrets = inbox_routes._secrets()
    tried = []

    async def refuse(settings, address, name, config, env_):
        tried.append((settings.host, settings.password))
        return False, "SMTP login failed"
    monkeypatch.setattr(inbox_routes, "_try_smtp", refuse)
    edit = {"smtp_host": "mail.side.example", "smtp_port": 465, "password": "new-secret",
            "name": "Sam", "warmup_started": "", "cap": 20}
    with _client(env["user"]) as c:
        r = c.post(f"/api/inboxes/{mid}/settings", json=edit)
        assert r.status_code == 400 and "nothing was saved" in r.json()["message"]
        row = asyncio.run(env["store"].get_mailbox(mid))
        assert "mail.side.example" not in row["settings_json"] and row["display_name"] != "Sam"
        assert secrets.get(f"mailbox:{mid}:smtp") == "old-secret"

        async def accept(settings, address, name, config, env_):
            return True, "Signed in."
        monkeypatch.setattr(inbox_routes, "_try_smtp", accept)
        r = c.post(f"/api/inboxes/{mid}/settings", json=edit)
        assert r.status_code == 200, r.text
        assert r.json()["health"]["verdict"] == "ready" and "new-secret" not in r.text
    assert tried == [("mail.side.example", "new-secret")]
    row = asyncio.run(env["store"].get_mailbox(mid))
    assert row["id"] == mid and row["state"] == "active"
    assert json.loads(row["settings_json"])["host"] == "mail.side.example"
    assert "new-secret" not in row["settings_json"]
    assert row["display_name"] == "Sam" and row["cap_override"] == 20
    assert secrets.get(f"mailbox:{mid}:smtp") == "new-secret"


def test_editing_only_the_name_needs_no_sign_in(env, keychain, monkeypatch):
    mid = _smtp_inbox(env)
    monkeypatch.setattr(inbox_routes, "_try_smtp",
                        lambda *a, **k: pytest.fail("signed in for a name change"))
    with _client(env["user"]) as c:
        r = c.post(f"/api/inboxes/{mid}/settings",
                   json={"name": "Sam", "warmup_started": "2026-11-01", "cap": None})
    assert r.status_code == 200, r.text
    row = asyncio.run(env["store"].get_mailbox(mid))
    assert row["display_name"] == "Sam" and str(row["warmup_started_at"]).startswith("2026-11-01")


def test_editing_refuses_bad_input(env, keychain):
    path = f"/api/inboxes/{_smtp_inbox(env)}/settings"
    with _client(env["user"]) as c:
        assert c.post(path, json={"smtp_port": 0}).status_code == 400
        assert c.post(path, json={"smtp_host": " "}).status_code == 400
        assert c.post(path, json={"cap": 0}).status_code == 400
        assert c.post(path, json={"warmup_started": "soon"}).status_code == 400
        assert c.post(path, json={"name": "a<b"}).status_code == 400
        assert c.post("/api/inboxes/nope/settings", json={}).status_code == 404


def test_the_agent_cannot_read_or_edit_settings(env, keychain):
    with _client(env["agent"]) as c:
        assert c.get(f"/api/inboxes/{env['mid']}/settings").status_code == 403
        assert c.post(f"/api/inboxes/{env['mid']}/settings", json={"name": "x"}).status_code == 403
        assert c.post(f"/api/inboxes/{env['mid']}/reconnect").status_code == 403
        assert c.post("/api/inboxes/main/test").status_code == 403


def test_signing_in_again_replaces_the_token_in_place(env, keychain, monkeypatch):
    from tests.test_inbox_routes import Cfg, Env

    class GEnv(Env):
        gmail_client_id = "123456789-abc123.apps.googleusercontent.com"
        gmail_client_secret = "secret"
    monkeypatch.setattr(inbox_routes, "_config_env", lambda: (Cfg(), GEnv()))
    monkeypatch.setattr(inbox_routes, "_sign_in",
                        lambda kind, address, env_, main=False: {"refresh_token": "rt-new"})
    store = env["store"]

    gid = _add(store, "g@side.example", "google", "oauth")
    with _client(env["user"]) as c:
        assert c.post(f"/api/inboxes/{env['mid']}/reconnect").status_code == 400  # not OAuth
        assert c.post(f"/api/inboxes/{gid}/reconnect").json()["started"]
        import time
        for _ in range(100):
            status = c.get("/api/inboxes/connect/status").json()
            if status["state"] != "waiting":
                break
            time.sleep(0.02)
    assert status["state"] == "connected" and status["id"] == gid
    assert "rt-new" in inbox_routes._secrets().get(f"mailbox:{gid}:oauth")
    assert asyncio.run(store.get_mailbox(gid))["id"] == gid
