# SELLBIRD CORE

## OVERVIEW
Runtime coordination, persistence, configuration and local API; score 13, distinct application boundary.

## STRUCTURE
- `agents/`: model-assisted workflow orchestration.
- `collectors/`: deterministic discovery and profiling.
- `integrations/`: external service and mailbox adapters.
- `models/`: Pydantic records and serialized campaign/thread shapes.
- `web/`: disk-served dashboard assets, not a frontend build project.
- `desktop/`: optional dashboard client and login-item integration.

## WHERE TO LOOK
| Task | Location | Boundary |
|------|----------|----------|
| Heartbeat ordering | `main.py` | Read and handle replies before sending |
| CLI dispatch | `cli.py`, `__main__.py` | Installed command and module entry |
| Model execution | `brain.py`, `claude_runtime.py`, `codex.py` | Provider health, isolation, usage |
| Persistence | `state.py`, `storage.py` | StateManager and backend ownership |
| Workspace paths | `workspace_home.py`, `paths.py`, `home.py` | Context-local homes and seeding |
| Configuration | `config.py`, `trainer.py`, `setup.py` | Validation, local override, training merge |
| Local HTTP API | `dashboard.py`, `dashboard_auth.py`, `agent_policy.py` | User versus agent authority |
| Outbound checks | `gate.py`, `send_hooks.py`, `sending_mode.py` | Deterministic checks before extension rules |
| Multi-inbox routing | `mailboxes.py`, `inbox_routes.py`, `secrets.py` | Sticky thread identity and keychain secrets |
| Public crawling | `public_http.py` | DNS/redirect checks and pinned public addresses |
| Extension registration | `extensions.py` | Ordered, one-time entry-point loading |

## CONVENTIONS
- Resolve runtime paths through the active `Home`; async tasks inherit its ContextVar.
- Hosted homes without injected config fail rather than borrowing checkout config.
- `StateManager` migrates SQLite by `PRAGMA user_version`; schema-owning backends are checked only.
- SQLite initialization enables WAL and synchronous NORMAL; connections use the storage boundary.
- Stages join through persisted database queries, not in-memory discovery result lists.
- Training replaces learned fields while preserving operator settings; saves use atomic replacement.
- Main mailbox identity is the empty string; extra inboxes are database rows with external secrets.
- New dashboard routes are user-only unless explicitly allowed by agent policy.
- Usage attribution belongs to Sellbird Brain calls, not unrelated interactive sessions.
- Focused checks: `python -m pytest -q tests/test_state.py tests/test_workspace_home.py tests/test_brain_provider.py`.

## ANTI-PATTERNS
- Do not add one company column per signal; governed observations carry facts and provenance.
- Do not let extension send rules override a built-in rejection or replace provider-owned headers.
- Do not scan other agent sessions to backfill usage.
- Do not weaken public-fetch validation through environment proxies or unchecked redirects.
- Do not let a removed thread inbox silently fall back to the main mailbox.
- Do not overwrite user defaults while seeding or migrate legacy files while the old process writes.
