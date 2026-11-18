# REGRESSION SUITE

## OVERVIEW
Offline unit, integration, end-to-end and soak tests; score 11, distinct isolation and fake-provider domain.

## WHERE TO LOOK
| Task | Location | Reusable surface |
|------|----------|------------------|
| Global isolation | `conftest.py` | Home, token, Claude capability and Humanizer fixtures |
| Native mail workflows | `test_outbox_native.py` | FakeProvider, StubBrain, sender/handler constructors |
| Authenticated dashboard | `test_dashboard_signals.py` | Shared `client` fixture |
| Inbox registry | `test_mailboxes.py` | `boxes`, `add_inbox` |
| Scripted Codex process | `fake_codex.py`, `test_codex.py` | Protocol faults and temporary login setup |
| Real CLI/local fake API | `test_codex_cli_surface.py`, `fake_anthropic.py` | No external inference |
| Full heartbeat | `test_codex_e2e.py`, `test_pipeline.py` | Real coordination with controlled boundaries |
| Historical upgrade | `fixtures/pre_codex_harvey_0_2_1.sql` | Version-10 legacy schema fixture |

## CONVENTIONS
- Keep real temporary SQLite state and orchestration; fake HTTP, mail and model boundaries.
- Async database fixtures use pytest-asyncio; HTTP integration tests use MockTransport.
- Helpers are intentionally imported across test modules; check callers before moving them.
- `conftest.py` removes inherited SELLBIRD_HOME before Sellbird imports.
- Autouse fixtures redirect dashboard tokens and disable real home seeding/installed Humanizer loading.
- Default Claude help probing is a test-only stand-in; `real_claude_cli` opts into installed CLI checks.
- Send-window/booking tests pin clocks; concurrency tests exercise atomic outbox claims.
- Inbox completeness tests must retain malformed later-page failure cases, not just happy paths.
- Browser auth and inline UI behavior also have Node subprocess checks; CI provisions Node 20.
- CI source is `.github/workflows/tests.yml`: Python 3.11/3.12 on Linux, macOS and Windows.
- Standard gate: `python -m pytest -q` after activating the virtual environment.
- Installed-wheel opt-in: `SELLBIRD_PACKAGE_TESTS=1 python -m pytest -q tests/test_packaging.py`.
- Codex CLI opt-in: `SELLBIRD_CODEX_CLI_TESTS=1 python -m pytest -q tests/test_codex_cli_surface.py`.
- Claude CLI opt-in: `SELLBIRD_CLAUDE_CLI_TESTS=1 python -m pytest -q tests/test_claude_cli_isolation.py`.
- CLI opt-ins use throwaway credentials and loopback mocks, not subscription inference.

## ANTI-PATTERNS
- Do not bypass isolation in shipped code just because unit tests stub capability probing.
- Do not let test setup touch the checkout's real dashboard token or operator configuration.
- Do not use real mailbox providers or real startup-system runners for regression tests.
- Do not mistake the legacy SQL fixture's schema version for the current migration target.
- Do not collapse uncertain delivery into a definite failure that can be automatically resent.
- Do not mark partial inbox reads successful or remove company-wide stop-on-reply coverage.
