"""Request-level tenant isolation tests with independent hosted applications."""

import asyncio

import aiosqlite
import httpx
import pytest
from starlette.middleware import Middleware

from sellbird.dashboard import build_app
from sellbird.workspace_home import Home


class TrustedTestGuard:
    """The hosting application supplies authentication; testing scope only."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


@pytest.mark.asyncio
async def test_concurrent_http_requests_do_not_cross_workspaces(tmp_path):
    homes = [Home.for_root(tmp_path / name, mode="hosted")
             for name in ("tenant_alpha", "tenant_beta")]
    apps = []
    for home, comment in zip(homes, ("ALPHA-ONLY", "BETA-ONLY")):
        home.data_dir.mkdir(parents=True)
        async with aiosqlite.connect(home.db_path) as db:
            await db.execute(
                "CREATE TABLE feedback (entity_type TEXT, entity_id TEXT,"
                " comment TEXT, created_at TEXT)")
            await db.execute(
                "INSERT INTO feedback VALUES ('company', 'c1', ?, '2026-01-01')",
                (comment,))
            await db.commit()
        app = build_app(guard=Middleware(TrustedTestGuard))
        app.state.home = home
        apps.append(app)

    async def fetch(app, expected):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                    base_url="http://testserver") as client:
            for _ in range(5):
                response = await client.get("/api/feedback/company/c1")
                assert response.status_code == 200
                assert [r["comment"] for r in response.json()] == [expected]
                await asyncio.sleep(0)

    await asyncio.gather(
        fetch(apps[0], "ALPHA-ONLY"),
        fetch(apps[1], "BETA-ONLY"),
    )
