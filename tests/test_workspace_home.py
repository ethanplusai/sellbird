"""Home: the per-workspace paths and mode, and the current-home context."""

import asyncio

import pytest

from sellbird.workspace_home import (
    Home,
    NotOnHosted,
    current_home,
    default_home,
    require_self_hosted,
    use_home,
)


def test_for_root_derives_every_path(tmp_path):
    h = Home.for_root(tmp_path)
    assert h.root == tmp_path
    assert h.data_dir == tmp_path / "data"
    assert h.db_path == tmp_path / "data" / "sellbird.db"
    assert h.env_file == tmp_path / ".env"
    assert h.prompts_dir == tmp_path / "prompts"
    assert h.skills_dir == tmp_path / "skills"
    assert h.m365_token_file == tmp_path / "data" / "m365_token.json"
    assert h.gmail_token_file == tmp_path / "data" / "gmail_token.json"
    assert h.dashboard_token_file == tmp_path / "data" / "dashboard-token"
    assert h.agent_token_file == tmp_path / "data" / "agent-token"
    assert h.config_file == tmp_path / "sellbird.yaml"
    assert h.mode == "self_hosted" and not h.hosted
    assert h.secret_backend is None


def test_for_root_hosted_and_backend(tmp_path):
    backend = object()
    h = Home.for_root(tmp_path, mode="hosted", secret_backend=backend)
    assert h.hosted and h.secret_backend is backend


def test_default_home_follows_patched_dashboard_db_path(monkeypatch, tmp_path):
    import sellbird.dashboard as dash

    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "x" / "sellbird.db")
    assert current_home().db_path == tmp_path / "x" / "sellbird.db"
    assert current_home().data_dir == tmp_path / "x"


def test_default_home_reads_legacy_globals():
    import sellbird.dashboard as dash
    import sellbird.integrations.microsoft as ms
    from sellbird.paths import PROJECT_ROOT

    h = default_home()
    assert h.root == PROJECT_ROOT
    assert h.config_file is None and not h.hosted
    assert h.dashboard_token_file == dash.TOKEN_FILE
    assert h.agent_token_file == h.data_dir / "agent-token"
    assert h.m365_token_file == ms.TOKEN_FILE
    assert h.env_file == PROJECT_ROOT / ".env"
    assert h.prompts_dir == PROJECT_ROOT / "prompts"
    assert h.skills_dir == PROJECT_ROOT / "skills"


def test_use_home_sets_and_resets(tmp_path):
    a = Home.for_root(tmp_path / "a")
    with use_home(a):
        assert current_home() is a
    assert current_home() != a


def test_use_home_resets_after_exception(tmp_path):
    a = Home.for_root(tmp_path / "a")
    with pytest.raises(RuntimeError):
        with use_home(a):
            raise RuntimeError("boom")
    assert current_home() != a


@pytest.mark.asyncio
async def test_concurrent_tasks_keep_their_own_home(tmp_path):
    homes = [Home.for_root(tmp_path / n) for n in ("a", "b")]

    async def worker(h):
        seen = []
        with use_home(h):
            for _ in range(10):
                seen.append(current_home())
                await asyncio.sleep(0)
        return seen

    results = await asyncio.gather(*(worker(h) for h in homes))
    for h, seen in zip(homes, results):
        assert len(seen) == 10
        assert all(s is h for s in seen)


@pytest.mark.asyncio
async def test_to_thread_inherits_home(tmp_path):
    a = Home.for_root(tmp_path / "a")
    with use_home(a):
        seen = await asyncio.to_thread(current_home)
    assert seen is a


def test_require_self_hosted_raises_in_hosted(tmp_path):
    with use_home(Home.for_root(tmp_path, mode="hosted")):
        with pytest.raises(NotOnHosted) as e:
            require_self_hosted("local_training")
    assert e.value.payload["error"] == "not_on_hosted"
    message = e.value.payload["message"]
    assert message.startswith("This runs only on your own computer (training through")
    assert "on the server instead" in message


def test_require_self_hosted_passes_when_self_hosted(tmp_path):
    with use_home(Home.for_root(tmp_path)):
        require_self_hosted("local_training")


def test_an_unknown_local_only_feature_is_a_bug(tmp_path):
    with pytest.raises(ValueError, match="unknown local-only feature"):
        require_self_hosted("training")


def test_every_local_only_feature_says_why():
    from sellbird.workspace_home import HOSTED_REFUSED, LOCAL_ONLY

    assert HOSTED_REFUSED == tuple(LOCAL_ONLY)
    assert all(reason.strip() for reason in LOCAL_ONLY.values())


def test_brain_reads_prompts_and_skills_from_the_current_home(tmp_path):
    from sellbird.brain import Brain

    (tmp_path / "prompts").mkdir()
    (tmp_path / "skills").mkdir()
    (tmp_path / "prompts" / "writer.md").write_text("hi {{name}}", encoding="utf-8")
    (tmp_path / "skills" / "tone.md").write_text("be brief", encoding="utf-8")
    brain = Brain.__new__(Brain)  # load_prompt/load_skill need no config
    with use_home(Home.for_root(tmp_path)):
        assert brain.load_prompt("writer", name="Dana") == "hi Dana"
        assert brain.load_skill("tone") == "be brief"


@pytest.mark.asyncio
async def test_dashboard_query_db_uses_active_workspace_database(tmp_path):
    """An authenticated hosted request must not query the default workspace."""
    import sellbird.dashboard as dash
    import aiosqlite

    first, second = (Home.for_root(tmp_path / name, mode="hosted")
                     for name in ("first", "second"))
    for home, company_name in ((first, "FIRST"), (second, "SECOND")):
        home.data_dir.mkdir(parents=True)
        async with aiosqlite.connect(home.db_path) as db:
            await db.execute("CREATE TABLE probe (name TEXT)")
            await db.execute("INSERT INTO probe VALUES (?)", (company_name,))
            await db.commit()

    for home, expected in ((first, "FIRST"), (second, "SECOND")):
        with use_home(home):
            rows = await dash.query_db("SELECT name FROM probe")
            assert rows == [{"name": expected}]


def test_dashboard_state_uses_active_workspace_path(tmp_path):
    import sellbird.dashboard as dash

    for name in ("a", "b"):
        home = Home.for_root(tmp_path / name, mode="hosted")
        with use_home(home):
            assert dash._state().db_path == str(home.db_path)


def test_hosted_env_settings_are_refused(tmp_path):
    from fastapi.testclient import TestClient
    import sellbird.dashboard as dash
    from starlette.middleware import Middleware

    class Open:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)

    app = dash.build_app(guard=Middleware(Open))
    app.state.home = Home.for_root(tmp_path, mode="hosted")
    resp = TestClient(app).post("/api/settings/env", json={"SMTP_PASSWORD": "secret"})
    assert resp.status_code == 409
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_hosted_secret_resolution_is_tenant_scoped(monkeypatch, tmp_path):
    """Provider config must not inherit a different tenant's process secrets."""
    from sellbird.config import load_env
    from sellbird.secrets import MemorySecretBackend, SERVICE

    monkeypatch.setenv("INSTANTLY_API_KEY", "global-owner-secret")
    monkeypatch.setenv("SMTP_PASSWORD", "global-smtp-secret")
    homes = []
    for name, token in (("alpha", "alpha-only"), ("beta", "beta-only")):
        backend = MemorySecretBackend()
        backend.set_password(SERVICE, "env:instantly_api_key", token)
        homes.append((Home.for_root(tmp_path / name, mode="hosted",
                                    secret_backend=backend), token))

    async def worker(home, expected):
        with use_home(home):
            for _ in range(3):
                config = load_env()
                assert config.instantly_api_key == expected
                assert config.smtp_password == ""
                await asyncio.sleep(0)

    await asyncio.gather(*(worker(home, expected) for home, expected in homes))


def test_hosted_secret_resolution_without_backend_fails_closed(monkeypatch, tmp_path):
    from sellbird.config import load_env
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "global-secret")
    with use_home(Home.for_root(tmp_path, mode="hosted")):
        assert load_env().gmail_client_secret == ""


def test_hosted_environment_write_helper_refuses_even_internal_call(tmp_path):
    import sellbird.dashboard as dash

    home = Home.for_root(tmp_path, mode="hosted")
    with use_home(home), pytest.raises(NotOnHosted):
        dash._write_env_file({"TRULYINBOX_API_KEY": "must-not-write"})
    assert not home.env_file.exists()


def test_inbox_store_uses_active_workspace(tmp_path):
    from sellbird.inbox_routes import _store
    for name in ("a", "b"):
        home = Home.for_root(tmp_path / name, mode="hosted")
        with use_home(home):
            assert _store().db_path == str(home.db_path)


@pytest.mark.asyncio
async def test_hosted_settings_do_not_echo_global_process_credentials(tmp_path, monkeypatch):
    import sellbird.dashboard as dash

    monkeypatch.setenv("SMTP_USERNAME", "global@outside.example")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "global-google-client")
    with use_home(Home.for_root(tmp_path, mode="hosted")):
        settings = await dash.get_settings()
        assert settings["smtp_username"] == ""
        assert settings["gmail_client_id"] == ""
